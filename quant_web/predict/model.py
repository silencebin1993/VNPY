"""
LightGBM：二分类（连板/首板）与回归（强势股波段，预测扣费净收益）的滚动（walk-forward）训练、样本外预测、
按五维汇总的贡献分解、保存加载。

- 每折只用 date <= train_end 且"标签揭晓日" label_end <= train_end 的样本训练（净化：避免训练样本的标签
  用到测试期的行情；连板/首板的 label_end 是下一交易日，波段是实际卖出日），训练窗口最后 10% 的交易日做早停验证；
  预测 [test_start, test_end]；
- 贡献用 pred_contrib（对数几率空间），按 FEATURE_GROUPS 汇总成 contrib_<group>，bias 为基准项；
  prob = sigmoid(bias + Σ contrib_<group>)；
- 首板模型训练时负样本抽样（比例 r），概率按"先验校正"还原：对数几率加 log(r)（记在 bias 里）；
- 回归模型：标签 clip(net, ±0.15)，早停看验证段"日内去均值 IC"（预测与收益各自减去当天均值后的相关系数），
  贡献分解在收益空间（pred = bias + Σ contrib_<group>）。滚动训练的每一折用 REG_SEEDS（7/8/9）三个随机种子
  各训练一次、预测取平均，早停选出的轮数少于 REG_MIN_ROUNDS（50）时按 50 轮重训——只为减小重训波动
  （单个种子时有的半年只训练 19~29 棵树、几乎不出信号），不是按结果调参；最终模型（全部数据、固定轮数）用单个种子。
"""
import json
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import polars as pl

from .. import config
from .features import FEATURE_LABELS, FEATURE_TO_GROUP, GROUPS, feature_matrix


PARAMS: dict[str, Any] = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "is_unbalance": False,
    "metric": ["binary_logloss", "auc"],
    "verbose": -1,
    "seed": 7,
    "force_col_wise": True,
}
MAX_ROUNDS: int = 2000
EARLY_STOP: int = 100
VALID_FRAC: float = 0.1
FALLBACK_ROUNDS: int = 200
TOP_KS: tuple[int, ...] = (1, 3, 5, 10)
CALIB_EDGES: list[float] = [0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]
CONTRIB_COLUMNS: list[str] = [f"contrib_{g}" for g in GROUPS]


@dataclass
class Fold:
    train_end: date
    test_start: date
    test_end: date


def _add_months(d: date, months: int) -> date:
    m: int = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def make_folds(dates: list[date], first_test_year: int = 2022, step_months: int = 6) -> list[Fold]:
    """半年一个测试窗口（从 first_test_year 年 1 月起），训练用测试窗口之前的全部数据。
    净化（训练样本的 label_end ≤ train_end）在 train_walk_forward / train_walk_forward_reg 里用 purge_mask 做"""
    days: list[date] = sorted(set(dates))
    if not days:
        return []
    folds: list[Fold] = []
    start: date = date(first_test_year, 1, 1)
    while start <= days[-1]:
        end: date = _add_months(start, step_months)
        test: list[date] = [d for d in days if start <= d < end]
        train: list[date] = [d for d in days if d < start]
        if test and train:
            folds.append(Fold(train_end=train[-1], test_start=test[0], test_end=test[-1]))
        start = end
    return folds


def purge_mask(ds: pl.DataFrame, train_end: date) -> pl.Expr:
    """训练行条件：date ≤ train_end，且标签揭晓日（label_end，没有时用 next_date）≤ train_end"""
    cond: pl.Expr = pl.col("date") <= train_end
    end_col: str | None = "label_end" if "label_end" in ds.columns else "next_date" if "next_date" in ds.columns         else None
    if end_col is not None:
        cond = cond & pl.col(end_col).is_not_null() & (pl.col(end_col) <= train_end)
    return cond


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def n_threads(n_rows: int) -> int:
    """线程数：小数据少开线程（OpenMP 线程过多时，机器上有别的程序占 CPU 会慢几十倍），大数据用物理核数"""
    physical: int = max(1, (os.cpu_count() or 2) // 2)
    if n_rows < 20_000:
        return 1
    if n_rows < 200_000:
        return min(4, physical)
    return physical


def _params(n_train: int, params: dict | None) -> dict:
    p: dict = {**PARAMS, **(params or {})}
    p.setdefault("num_threads", n_threads(n_train))
    if not params or "min_child_samples" not in params:
        # 样本少时（如小样本测试）放宽叶子最小样本数，大样本保持 200
        p["min_child_samples"] = int(min(PARAMS["min_child_samples"], max(20, n_train // 100)))
    return p


def fit(
    df: pl.DataFrame,
    feature_cols: list[str],
    params: dict | None = None,
    num_boost_round: int | None = None,
) -> tuple[lgb.Booster, int]:
    """训练一个模型。num_boost_round 为空时用时间上最后 10% 的交易日早停；返回 (booster, 树的数量)"""
    df = df.filter(pl.col("y").is_not_null()).sort("date")
    X: np.ndarray = feature_matrix(df, feature_cols)
    y: np.ndarray = df["y"].cast(pl.Float64).to_numpy()
    p: dict = _params(len(y), params)
    if num_boost_round:
        booster = lgb.train(p, lgb.Dataset(X, y, feature_name=feature_cols), num_boost_round)
        return booster, num_boost_round
    days: np.ndarray = df["date"].unique().sort().to_numpy()
    cut = days[min(len(days) - 1, int(len(days) * (1 - VALID_FRAC)))]
    tr: np.ndarray = df["date"].to_numpy() < cut
    va: np.ndarray = ~tr
    ok: bool = tr.sum() > 0 and va.sum() > 0 and len(np.unique(y[tr])) == 2 and len(np.unique(y[va])) == 2
    if not ok:
        booster = lgb.train(p, lgb.Dataset(X, y, feature_name=feature_cols), FALLBACK_ROUNDS)
        return booster, FALLBACK_ROUNDS
    dtr = lgb.Dataset(X[tr], y[tr], feature_name=feature_cols)
    dva = lgb.Dataset(X[va], y[va], reference=dtr)
    booster = lgb.train(
        p, dtr, MAX_ROUNDS, valid_sets=[dva],
        callbacks=[lgb.early_stopping(EARLY_STOP, first_metric_only=True, verbose=False)],
    )
    best: int = int(booster.best_iteration or booster.current_iteration())
    booster = lgb.Booster(model_str=booster.model_to_string(num_iteration=best))
    return booster, best


def _offset_of(booster: lgb.Booster, offset: float | None) -> float:
    if offset is not None:
        return float(offset)
    return float(getattr(booster, "qw_logit_offset", 0.0))


def _group_index(feature_cols: list[str]) -> dict[str, np.ndarray]:
    return {g: np.array([i for i, f in enumerate(feature_cols) if FEATURE_TO_GROUP.get(f) == g], dtype=int)
            for g in GROUPS}


def _contribs(booster: lgb.Booster, X: np.ndarray, feature_cols: list[str], offset: float) -> tuple[np.ndarray, dict]:
    """返回 (逐特征贡献矩阵 n×F, {bias, contrib_<group>})"""
    raw: np.ndarray = np.asarray(booster.predict(X, pred_contrib=True, num_threads=n_threads(len(X) * 20)))
    feat_c: np.ndarray = raw[:, :-1]
    out: dict[str, np.ndarray] = {"bias": raw[:, -1] + offset}
    for g, idx in _group_index(feature_cols).items():
        out[f"contrib_{g}"] = feat_c[:, idx].sum(axis=1) if len(idx) else np.zeros(len(X))
    return feat_c, out


def _top_per_day(dates: pl.Series, prob: np.ndarray, k: int) -> np.ndarray:
    ranks = pl.DataFrame({"d": dates, "p": prob}).select(
        pl.col("p").rank("ordinal", descending=True).over("d")
    ).to_series().to_numpy()
    return ranks <= k


def predict_frame(
    booster: lgb.Booster,
    feat: pl.DataFrame,
    feature_cols: list[str],
    offset: float | None = None,
    contrib_top: int | None = None,
) -> pl.DataFrame:
    """[date, code, prob, bias, contrib_<group>...]；contrib_top 给定时只算每天概率前 N 名的贡献（其余为空）"""
    off: float = _offset_of(booster, offset)
    n: int = feat.height
    if n == 0:
        return pl.DataFrame(schema={"date": pl.Date, "code": pl.Utf8, "prob": pl.Float64, "bias": pl.Float64,
                                    **{c: pl.Float64 for c in CONTRIB_COLUMNS}})
    X: np.ndarray = feature_matrix(feat, feature_cols)
    raw: np.ndarray = np.asarray(booster.predict(X, raw_score=True, num_threads=n_threads(n))) + off
    cols: dict[str, np.ndarray] = {"bias": np.full(n, np.nan), **{c: np.full(n, np.nan) for c in CONTRIB_COLUMNS}}
    mask: np.ndarray = np.ones(n, dtype=bool) if contrib_top is None else _top_per_day(feat["date"], raw, contrib_top)
    if mask.any():
        _, part = _contribs(booster, X[mask], feature_cols, off)
        for c, v in part.items():
            cols[c][mask] = v
    return pl.DataFrame({
        "date": feat["date"], "code": feat["code"], "prob": _sigmoid(raw), **cols,
    }).with_columns([pl.col(c).fill_nan(None) for c in ["bias", *CONTRIB_COLUMNS]])


def predict(
    booster: lgb.Booster,
    feat: pl.DataFrame,
    feature_cols: list[str],
    offset: float | None = None,
    n_reasons: int = 5,
) -> pl.DataFrame:
    """[date, code, prob, bias, contrib_<group>..., reasons]；reasons 为 |贡献| 最大的 n_reasons 个特征
    list[struct{feature, label, value, contrib}]（contrib 为对数几率）"""
    off: float = _offset_of(booster, offset)
    n: int = feat.height
    reason_type = pl.List(pl.Struct({"feature": pl.Utf8, "label": pl.Utf8, "value": pl.Float64,
                                     "contrib": pl.Float64}))
    if n == 0:
        return predict_frame(booster, feat, feature_cols, off).with_columns(
            pl.lit(None, reason_type).alias("reasons"))
    X: np.ndarray = feature_matrix(feat, feature_cols)
    feat_c, part = _contribs(booster, X, feature_cols, off)
    logit: np.ndarray = part["bias"] + sum(part[c] for c in CONTRIB_COLUMNS)
    order: np.ndarray = np.argsort(-np.abs(feat_c), axis=1)[:, :n_reasons]
    reasons: list[list[dict]] = []
    for i in range(n):
        row: list[dict] = []
        for j in order[i]:
            c: float = float(feat_c[i, j])
            if c == 0.0:
                continue
            v: float = float(X[i, j])
            f: str = feature_cols[j]
            row.append({"feature": f, "label": FEATURE_LABELS.get(f, f),
                        "value": None if math.isnan(v) else v, "contrib": c})
        reasons.append(row)
    return pl.DataFrame({
        "date": feat["date"], "code": feat["code"], "prob": _sigmoid(logit), **part,
        "reasons": pl.Series("reasons", reasons, dtype=reason_type),
    })


# ---------------------------------------------------------------- 评估

def evaluate(pred: pl.DataFrame) -> dict:
    """AUC、每天前 k 名的次日涨停率、基准率（只用有标签的行）"""
    lab: pl.DataFrame = pred.filter(pl.col("y").is_not_null() & pl.col("prob").is_not_null())
    out: dict[str, Any] = {"n": lab.height, "days": lab["date"].n_unique() if lab.height else 0,
                           "positives": int(lab["y"].sum()) if lab.height else 0}
    if lab.is_empty():
        return {**out, "auc": None, "base_rate": None, **{f"top{k}_hit": None for k in TOP_KS}}
    y: np.ndarray = lab["y"].to_numpy().astype(float)
    auc: float | None = None
    if 0 < y.sum() < len(y):
        from sklearn.metrics import roc_auc_score

        auc = float(roc_auc_score(y, lab["prob"].to_numpy()))
    ranked: pl.DataFrame = lab.with_columns(pl.col("prob").rank("ordinal", descending=True).over("date").alias("_r"))
    for k in TOP_KS:
        sel = ranked.filter(pl.col("_r") <= k)
        out[f"top{k}_hit"] = float(sel["y"].mean()) if sel.height else None
    out["auc"] = auc
    out["base_rate"] = float(y.mean())
    return out


def calibration(pred: pl.DataFrame, col: str = "prob", edges: list[float] | None = None) -> list[dict]:
    """按概率分桶：每桶平均预测概率 vs 实际涨停比例"""
    edges = edges or CALIB_EDGES
    lab: pl.DataFrame = pred.filter(pl.col("y").is_not_null() & pl.col(col).is_not_null())
    rows: list[dict] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=False):
        cond = (pl.col(col) >= lo) & ((pl.col(col) < hi) if hi < 1 else (pl.col(col) <= hi))
        part: pl.DataFrame = lab.filter(cond)
        if part.is_empty():
            continue
        rows.append({
            "bucket": f"{lo * 100:g}-{hi * 100:g}%",
            "pred": round(float(part[col].mean()), 4),
            "actual": round(float(part["y"].mean()), 4),
            "n": part.height,
        })
    return rows


def importance(booster: lgb.Booster, feature_cols: list[str]) -> tuple[dict[str, float], list[dict]]:
    """(各维度增益占比, 前 15 个特征)"""
    gain: np.ndarray = np.asarray(booster.feature_importance(importance_type="gain"), dtype=float)
    total: float = float(gain.sum()) or 1.0
    groups: dict[str, float] = {g: 0.0 for g in GROUPS}
    for f, v in zip(feature_cols, gain, strict=False):
        groups[FEATURE_TO_GROUP.get(f, "technical")] += v / total
    order = np.argsort(-gain)[:15]
    top: list[dict] = [
        {"feature": feature_cols[i], "label": FEATURE_LABELS.get(feature_cols[i], feature_cols[i]),
         "group": FEATURE_TO_GROUP.get(feature_cols[i]), "share": round(float(gain[i] / total), 4)}
        for i in order if gain[i] > 0
    ]
    return {g: round(v, 4) for g, v in groups.items()}, top


# ---------------------------------------------------------------- 滚动训练

def train_walk_forward(
    ds: pl.DataFrame,
    feature_cols: list[str],
    folds: list[Fold],
    progress: Callable[[float, str], None] | None = None,
    test_ds: pl.DataFrame | Callable[[Fold], pl.DataFrame] | None = None,
    offset: float = 0.0,
    contrib_top: int | None = None,
    params: dict | None = None,
    keep_cols: list[str] | None = None,
) -> tuple[pl.DataFrame, list[dict]]:
    """逐折训练并预测测试窗口。返回 (样本外预测 [date, code, prob, bias, contrib_<group>..., y, fold], 每折指标)。

    test_ds：测试窗口用的样本（首板模型训练集是抽样的，测试要用全部候选），可以是表或"按折生成测试样本"的函数；
    为空时用 ds。keep_cols：从测试样本带到预测结果里的展示列。
    """
    preds: list[pl.DataFrame] = []
    metrics: list[dict] = []
    for i, fold in enumerate(folds):
        if progress:
            progress(i / max(len(folds), 1),
                     f"第 {i + 1}/{len(folds)} 轮：用 {fold.train_end} 及以前的数据训练，"
                     f"预测 {fold.test_start} ~ {fold.test_end}")
        t0: float = time.time()
        tr: pl.DataFrame = ds.filter(purge_mask(ds, fold.train_end) & pl.col("y").is_not_null())
        info: dict[str, Any] = {"fold": i + 1, **{k: v.isoformat() for k, v in asdict(fold).items()},
                                "n_train": tr.height}
        if tr.height < 200 or int(tr["y"].sum()) < 10:
            metrics.append({**info, "skipped": "训练样本太少"})
            continue
        booster, best = fit(tr, feature_cols, params)
        if callable(test_ds):
            te: pl.DataFrame = test_ds(fold)
        else:
            src: pl.DataFrame = ds if test_ds is None else test_ds
            te = src.filter((pl.col("date") >= fold.test_start) & (pl.col("date") <= fold.test_end))
        pred: pl.DataFrame = predict_frame(booster, te, feature_cols, offset, contrib_top).with_columns(
            te["y"].cast(pl.Int8).alias("y"), pl.lit(i + 1, pl.Int16).alias("fold"),
            *[te[c] for c in (keep_cols or []) if c in te.columns],
        )
        preds.append(pred)
        metrics.append({**info, "best_iter": best, **evaluate(pred), "seconds": round(time.time() - t0, 1)})
    if progress:
        progress(1.0, "滚动训练完成")
    schema: dict = {"date": pl.Date, "code": pl.Utf8, "prob": pl.Float64, "bias": pl.Float64,
                    **{c: pl.Float64 for c in CONTRIB_COLUMNS}, "y": pl.Int8, "fold": pl.Int16}
    oos: pl.DataFrame = pl.concat(preds, how="vertical_relaxed") if preds else pl.DataFrame(schema=schema)
    return oos, metrics


def train_final(
    ds: pl.DataFrame,
    feature_cols: list[str],
    num_boost_round: int | None = None,
    params: dict | None = None,
) -> lgb.Booster:
    """用全部有标签的样本训练最终模型；num_boost_round 为空时先早停确定树的数量再用全部数据重训"""
    if not num_boost_round:
        _, num_boost_round = fit(ds, feature_cols, params)
    booster, _ = fit(ds, feature_cols, params, num_boost_round=num_boost_round)
    return booster


# ---------------------------------------------------------------- 回归（强势股波段：预测扣费净收益）

REG_PARAMS: dict[str, Any] = {
    "objective": "regression",
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_child_samples": 200,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 10.0,
    "metric": "None",
    "verbose": -1,
    "seed": 7,
    "force_col_wise": True,
}
REG_MAX_ROUNDS: int = 1500
REG_EARLY_STOP: int = 150
REG_MIN_ROUNDS: int = 50            # 每折早停选出的轮数少于这么多时按这么多轮重训；最终模型也至少这么多轮
REG_SEEDS: tuple[int, ...] = (7, 8, 9)      # 每折用这几个随机种子各训练一次、预测取平均（减小波动，不是调参）
REG_CLIP: float = 0.15              # 训练标签 clip(net, ±15%)
REG_MIN_TRAIN: int = 300            # 一折训练样本少于这么多就跳过
REG_TOP_KS: tuple[int, ...] = (1, 3, 5, 10)


def _reg_params(n_train: int, params: dict | None) -> dict:
    p: dict = {**REG_PARAMS, **(params or {})}
    p.setdefault("num_threads", n_threads(n_train))
    if not params or "min_child_samples" not in params:
        p["min_child_samples"] = int(min(REG_PARAMS["min_child_samples"], max(20, n_train // 100)))
    return p


def _day_demean(v: np.ndarray, g: np.ndarray, ng: int) -> np.ndarray:
    s: np.ndarray = np.bincount(g, weights=v, minlength=ng)
    c: np.ndarray = np.bincount(g, minlength=ng)
    return v - (s / np.maximum(c, 1))[g]


def day_ic(pred: np.ndarray, y: np.ndarray, dates: np.ndarray) -> float | None:
    """日内去均值 IC：预测与收益各自减去当天均值后的相关系数（衡量"当天挑哪只"的能力，不含择时）"""
    if len(pred) < 3:
        return None
    g: np.ndarray = np.unique(dates, return_inverse=True)[1]
    ng: int = int(g.max()) + 1
    pd_: np.ndarray = _day_demean(np.asarray(pred, float), g, ng)
    yd: np.ndarray = _day_demean(np.asarray(y, float), g, ng)
    den: float = float(pd_.std() * yd.std())
    return float((pd_ * yd).mean() / den) if den > 0 else None


def _dic_feval(dates: np.ndarray, y: np.ndarray) -> Callable:
    g: np.ndarray = np.unique(dates, return_inverse=True)[1]
    ng: int = int(g.max()) + 1
    ydm: np.ndarray = _day_demean(y, g, ng)
    ysd: float = float(ydm.std()) + 1e-12

    def feval(preds: np.ndarray, _data: Any) -> tuple[str, float, bool]:
        pdm: np.ndarray = _day_demean(np.asarray(preds, float), g, ng)
        return "dIC", float((pdm * ydm).mean() / (pdm.std() + 1e-12) / ysd), True
    return feval


def fit_regression(
    df: pl.DataFrame,
    feature_cols: list[str],
    label: str = "net",
    params: dict | None = None,
    num_boost_round: int | None = None,
) -> tuple[lgb.Booster, int]:
    """回归模型：标签 clip(label, ±REG_CLIP)。num_boost_round 为空时用最后 10% 交易日的日内去均值 IC 早停
    （最多 REG_MAX_ROUNDS 轮，耐心 REG_EARLY_STOP 轮）；返回 (booster, 树的数量)。
    注意：早停时 LightGBM 返回的模型只保留到最佳轮，验证段上一开始就最好时模型只有几棵树、预测几乎是常数；
    滚动训练用 fit_regression_seeds（少于 REG_MIN_ROUNDS 轮时按该轮数重训，并对多个种子取平均）避免这种情况。"""
    df = df.filter(pl.col(label).is_not_null()).sort("date")
    X: np.ndarray = feature_matrix(df, feature_cols)
    net: np.ndarray = df[label].cast(pl.Float64).to_numpy()
    y: np.ndarray = np.clip(net, -REG_CLIP, REG_CLIP)
    p: dict = _reg_params(len(y), params)
    if num_boost_round:
        return lgb.train(p, lgb.Dataset(X, y, feature_name=feature_cols), num_boost_round), num_boost_round
    days: np.ndarray = df["date"].unique().sort().to_numpy()
    cut = days[min(len(days) - 1, int(len(days) * (1 - VALID_FRAC)))]
    d: np.ndarray = df["date"].to_numpy()
    tr: np.ndarray = d < cut
    va: np.ndarray = ~tr
    if tr.sum() < 20 or va.sum() < 20:
        return lgb.train(p, lgb.Dataset(X, y, feature_name=feature_cols), FALLBACK_ROUNDS), FALLBACK_ROUNDS
    dtr = lgb.Dataset(X[tr], y[tr], feature_name=feature_cols)
    dva = lgb.Dataset(X[va], y[va], reference=dtr)
    booster = lgb.train(p, dtr, REG_MAX_ROUNDS, valid_sets=[dva], feval=_dic_feval(d[va], y[va]),
                        callbacks=[lgb.early_stopping(REG_EARLY_STOP, verbose=False)])
    best: int = int(booster.best_iteration or booster.current_iteration())
    best = min(best, booster.current_iteration())
    return lgb.Booster(model_str=booster.model_to_string(num_iteration=best)), best


def fit_regression_seeds(
    df: pl.DataFrame,
    feature_cols: list[str],
    label: str = "net",
    params: dict | None = None,
    seeds: tuple[int, ...] | list[int] = REG_SEEDS,
    min_rounds: int = REG_MIN_ROUNDS,
) -> tuple[list[lgb.Booster], list[int]]:
    """同一份训练数据按每个随机种子各训练一个早停模型；早停选出的轮数 < min_rounds 时按 min_rounds 轮重训。
    返回 ([booster...], [每个的树数量])"""
    boosters: list[lgb.Booster] = []
    iters: list[int] = []
    for sd in (seeds or (REG_PARAMS["seed"],)):
        p: dict = {**(params or {}), "seed": int(sd)}
        booster, best = fit_regression(df, feature_cols, label, p)
        if best < min_rounds:
            booster, best = fit_regression(df, feature_cols, label, p, num_boost_round=min_rounds)
        boosters.append(booster)
        iters.append(int(best))
    return boosters, iters


def predict_reg_frame_avg(boosters: list[lgb.Booster], feat: pl.DataFrame, feature_cols: list[str]) -> pl.DataFrame:
    """多个模型的 predict_reg_frame 取平均（pred、bias、各维贡献都是线性的，平均后仍满足 pred = bias + Σ contrib）"""
    frames: list[pl.DataFrame] = [predict_reg_frame(b, feat, feature_cols) for b in boosters]
    if len(frames) == 1 or feat.height == 0:
        return frames[0]
    cols: list[str] = ["pred", "bias", *CONTRIB_COLUMNS]
    return frames[0].with_columns([
        pl.Series(c, np.mean([f[c].to_numpy() for f in frames], axis=0)) for c in cols
    ])


def predict_reg_frame(booster: lgb.Booster, feat: pl.DataFrame, feature_cols: list[str]) -> pl.DataFrame:
    """[date, code, pred, bias, contrib_<group>...]（收益空间：pred = bias + Σ contrib_<group>）"""
    n: int = feat.height
    if n == 0:
        return pl.DataFrame(schema={"date": pl.Date, "code": pl.Utf8, "pred": pl.Float64, "bias": pl.Float64,
                                    **{c: pl.Float64 for c in CONTRIB_COLUMNS}})
    X: np.ndarray = feature_matrix(feat, feature_cols)
    _, part = _contribs(booster, X, feature_cols, 0.0)
    pred: np.ndarray = part["bias"] + sum(part[c] for c in CONTRIB_COLUMNS)
    return pl.DataFrame({"date": feat["date"], "code": feat["code"], "pred": pred, **part})


def predict_reg(booster: lgb.Booster, feat: pl.DataFrame, feature_cols: list[str], n_reasons: int = 5
                ) -> pl.DataFrame:
    """[date, code, pred, prob(空), bias, contrib_<group>..., reasons]；reasons 的 contrib 为收益（小数）"""
    reason_type = pl.List(pl.Struct({"feature": pl.Utf8, "label": pl.Utf8, "value": pl.Float64,
                                     "contrib": pl.Float64}))
    base: pl.DataFrame = predict_reg_frame(booster, feat, feature_cols)
    if feat.height == 0:
        return base.with_columns(pl.lit(None, pl.Float64).alias("prob"), pl.lit(None, reason_type).alias("reasons"))
    X: np.ndarray = feature_matrix(feat, feature_cols)
    feat_c, _ = _contribs(booster, X, feature_cols, 0.0)
    order: np.ndarray = np.argsort(-np.abs(feat_c), axis=1)[:, :n_reasons]
    reasons: list[list[dict]] = []
    for i in range(feat.height):
        row: list[dict] = []
        for j in order[i]:
            c: float = float(feat_c[i, j])
            if c == 0.0:
                continue
            v: float = float(X[i, j])
            f: str = feature_cols[j]
            row.append({"feature": f, "label": FEATURE_LABELS.get(f, f),
                        "value": None if math.isnan(v) else v, "contrib": c})
        reasons.append(row)
    return base.with_columns(pl.lit(None, pl.Float64).alias("prob"),
                             pl.Series("reasons", reasons, dtype=reason_type))


def evaluate_reg(pred: pl.DataFrame, label: str = "net", col: str = "pred") -> dict:
    """回归预测的样本外评价（只用有标签的行）：日内去均值 IC、每天前 k 名的平均净收益与胜率、全部平均"""
    lab: pl.DataFrame = pred.filter(pl.col(label).is_not_null() & pl.col(col).is_not_null())
    out: dict[str, Any] = {"n": lab.height, "days": lab["date"].n_unique() if lab.height else 0}
    if lab.is_empty():
        return {**out, "ic": None, "base_mean": None, "base_win": None,
                **{f"top{k}_mean": None for k in REG_TOP_KS}, **{f"top{k}_win": None for k in REG_TOP_KS}}
    out["ic"] = day_ic(lab[col].to_numpy(), lab[label].to_numpy(), lab["date"].to_numpy())
    out["base_mean"] = float(lab[label].mean())
    out["base_win"] = float((lab[label] > 0).mean())
    ranked: pl.DataFrame = lab.with_columns(pl.col(col).rank("ordinal", descending=True).over("date").alias("_r"))
    for k in REG_TOP_KS:
        sel: pl.DataFrame = ranked.filter(pl.col("_r") <= k)
        out[f"top{k}_mean"] = float(sel[label].mean()) if sel.height else None
        out[f"top{k}_win"] = float((sel[label] > 0).mean()) if sel.height else None
    return out


def train_walk_forward_reg(
    ds: pl.DataFrame,
    feature_cols: list[str],
    folds: list[Fold],
    progress: Callable[[float, str], None] | None = None,
    label: str = "net",
    params: dict | None = None,
    keep_cols: list[str] | None = None,
    train_filter: pl.Expr | None = None,
    seeds: tuple[int, ...] | list[int] = REG_SEEDS,
    min_rounds: int = REG_MIN_ROUNDS,
) -> tuple[pl.DataFrame, list[dict]]:
    """回归版滚动训练。训练行：purge_mask（date 与 label_end 都 ≤ train_end）、label 非空、train_filter（如 fill）；
    测试行：测试窗口内 ds 的全部行（买不进的也预测，决策时不知道明天能不能买进）。
    每折按 seeds 各训练一个模型（fit_regression_seeds，至少 min_rounds 轮），预测取平均；
    每折指标 best_iter = 各种子树数量的中位数，best_iters = 各种子的树数量。
    返回 (样本外 [date, code, pred, bias, contrib_<group>..., fold, keep_cols...], 每折指标)"""
    preds: list[pl.DataFrame] = []
    metrics: list[dict] = []
    for i, fold in enumerate(folds):
        if progress:
            progress(i / max(len(folds), 1),
                     f"第 {i + 1}/{len(folds)} 轮：用 {fold.train_end} 及以前的数据训练，"
                     f"预测 {fold.test_start} ~ {fold.test_end}")
        t0: float = time.time()
        cond: pl.Expr = purge_mask(ds, fold.train_end) & pl.col(label).is_not_null()
        if train_filter is not None:
            cond = cond & train_filter
        tr: pl.DataFrame = ds.filter(cond)
        info: dict[str, Any] = {"fold": i + 1, **{k: v.isoformat() for k, v in asdict(fold).items()},
                                "n_train": tr.height}
        if tr.height < REG_MIN_TRAIN:
            metrics.append({**info, "skipped": "训练样本太少"})
            continue
        boosters, iters = fit_regression_seeds(tr, feature_cols, label, params, seeds, min_rounds)
        best: int = int(np.median(iters))
        del tr
        te: pl.DataFrame = ds.filter((pl.col("date") >= fold.test_start) & (pl.col("date") <= fold.test_end))
        pred: pl.DataFrame = predict_reg_frame_avg(boosters, te, feature_cols).with_columns(
            pl.lit(i + 1, pl.Int16).alias("fold"),
            *[te[c] for c in (keep_cols or []) if c in te.columns],
        )
        preds.append(pred)
        ev: dict = evaluate_reg(pred, label) if label in pred.columns else {}
        metrics.append({**info, "best_iter": best, "best_iters": iters, "seeds": [int(x) for x in seeds], **ev,
                        "seconds": round(time.time() - t0, 1)})
    if progress:
        progress(1.0, "滚动训练完成")
    schema: dict = {"date": pl.Date, "code": pl.Utf8, "pred": pl.Float64, "bias": pl.Float64,
                    **{c: pl.Float64 for c in CONTRIB_COLUMNS}, "fold": pl.Int16}
    oos: pl.DataFrame = pl.concat(preds, how="vertical_relaxed") if preds else pl.DataFrame(schema=schema)
    return oos, metrics


# ---------------------------------------------------------------- 保存与加载

_cache: dict[str, tuple[tuple, tuple[lgb.Booster, dict]]] = {}
_lock = threading.Lock()


def paths(kind: str) -> dict[str, Path]:
    d: Path = config.MODEL_DIR
    return {"model": d.joinpath(f"{kind}.txt"), "meta": d.joinpath(f"{kind}_meta.json"),
            "oos": d.joinpath(f"{kind}_oos.parquet"), "daily": d.joinpath(f"{kind}_oos_daily.parquet")}


def _json_default(v: Any) -> Any:
    if isinstance(v, date | datetime):
        return v.isoformat()
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    return str(v)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp: Path = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save(kind: str, booster: lgb.Booster, meta: dict, oos: pl.DataFrame | None = None,
         daily: pl.DataFrame | None = None) -> None:
    """保存 {kind}.txt、{kind}_meta.json（以及样本外预测 {kind}_oos.parquet、每日候选统计 {kind}_oos_daily.parquet）"""
    p: dict[str, Path] = paths(kind)
    p["model"].parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if daily is None and oos is not None:
            p["daily"].unlink(missing_ok=True)
        for key, df in (("oos", oos), ("daily", daily)):
            if df is not None:
                tmp: Path = p[key].with_name(p[key].name + ".tmp")
                df.write_parquet(tmp)
                tmp.replace(p[key])
        _atomic_write_text(p["model"], booster.model_to_string())
        _atomic_write_text(p["meta"], json.dumps(meta, ensure_ascii=False, indent=1, default=_json_default))
        _cache.pop(kind, None)


def save_meta(kind: str, meta: dict) -> None:
    """只重写 {kind}_meta.json（模型和样本外预测不变，例如按新口径重算评价指标时用）"""
    p: dict[str, Path] = paths(kind)
    with _lock:
        _atomic_write_text(p["meta"], json.dumps(meta, ensure_ascii=False, indent=1, default=_json_default))
        _cache.pop(kind, None)


def load_meta(kind: str) -> dict | None:
    path: Path = paths(kind)["meta"]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(kind: str) -> tuple[lgb.Booster, dict] | None:
    """读取模型与 meta（按文件修改时间缓存）；不存在返回 None"""
    p: dict[str, Path] = paths(kind)
    try:
        stamp: tuple = (p["model"].stat().st_mtime_ns, p["meta"].stat().st_mtime_ns)
    except OSError:
        return None
    with _lock:
        hit = _cache.get(kind)
        if hit and hit[0] == stamp:
            return hit[1]
        meta: dict | None = load_meta(kind)
        if meta is None:
            return None
        booster = lgb.Booster(model_str=p["model"].read_text(encoding="utf-8"))
        booster.qw_logit_offset = float(meta.get("logit_offset", 0.0))      # type: ignore[attr-defined]
        _cache[kind] = (stamp, (booster, meta))
        return booster, meta


def load_oos(kind: str) -> pl.DataFrame | None:
    path: Path = paths(kind)["oos"]
    if not path.exists():
        return None
    return pl.read_parquet(path)
