"""
滚动训练（后台任务调用；开发期间只在合成数据上测试）：
1. 按配置建数据（dataset.build）；
2. 滚动切分：测试窗口从起始年份 + 2 年开始，每隔 N 个月（预设）一段；每段只用"标签在测试窗口开始前就已揭晓"的行训练
   （predict.model.purge_mask），验证集是训练期最后 10% 的交易日（同样净化），慢模型每期抽样；
3. 每段在测试窗口打分 → 拼成样本外预测 → evaluate；
4. 用全部数据训练最终模型（树模型轮数 = 各段最佳轮数中位数 × 1.1），给最新一天打分，保存。
"""
from __future__ import annotations

import gc
import os
import time
from collections.abc import Callable
from datetime import date, datetime

import numpy as np
import polars as pl

from ..predict.model import Fold, make_folds, purge_mask
from . import dataset as D
from . import evaluate as E
from . import factors as F
from . import models as M
from . import options as O
from . import store

Progress = Callable[[float, str], None]
VALID_FRAC: float = 0.1
SEED: int = 7
MIN_TRAIN_ROWS: int = 2000
LATEST_TOP: int = 50


def folds_for(days: list[date], cfg: dict) -> list[Fold]:
    """按预设的步长切滚动窗口；数据太短（试跑）时按交易日切：前一半训练，后一半分三段依次测试"""
    folds: list[Fold] = make_folds(days, first_test_year=cfg["start_year"] + 2, step_months=O.PRESETS[cfg["preset"]]["step_months"])
    if len(folds) >= 2:
        return folds
    n: int = len(days)
    if n < 40:
        raise ValueError("数据太少（不到 40 个交易日），没法做滚动训练")
    first: int = n // 2
    size: int = max(10, (n - first + 2) // 3)
    out: list[Fold] = []
    for s in range(first, n, size):
        test = days[s:s + size]
        out.append(Fold(train_end=days[s - 1], test_start=test[0], test_end=test[-1]))
    return out


def _threads() -> int:
    from .. import settings as settings_mod
    n = int(settings_mod.load().lab.threads or 0)
    return n if n > 0 else max(1, (os.cpu_count() or 2) - 1)


def _matrix(df: pl.DataFrame, features: list[str], spec: M.ModelSpec) -> np.ndarray:
    X = df.select([pl.col(f).cast(pl.Float32) for f in features]).to_numpy()
    X = np.ascontiguousarray(X, dtype=np.float32)
    return X if spec.nan_ok else np.nan_to_num(X, nan=0.0)


def _gid(dates: pl.Series) -> np.ndarray:
    return (dates.rank("dense").cast(pl.Int64) - 1).to_numpy()


def _split(tr: pl.DataFrame, spec: M.ModelSpec, preset: str) -> tuple[pl.DataFrame, pl.DataFrame, int | None]:
    """训练 / 验证（最后 10% 的交易日，训练行的标签要在验证期开始前揭晓）；超过行数上限时按行随机抽样"""
    days: list[date] = sorted(tr["date"].unique().to_list())
    n_val: int = max(1, int(len(days) * VALID_FRAC)) if len(days) >= 20 else 0
    if n_val:
        v0: date = days[-n_val]
        valid = tr.filter(pl.col("date") >= v0)
        train = tr.filter((pl.col("date") < v0) & (pl.col("label_end") < v0))
    else:
        valid, train = tr.head(0), tr
    cap: int | None = spec.max_rows(preset)
    sampled: int | None = None
    if cap and train.height > cap:
        train = train.sample(n=cap, seed=SEED).sort("date")
        sampled = cap
    return train, valid, sampled


def _target(df: pl.DataFrame, task: str, lo: float | None, hi: float | None) -> np.ndarray:
    y = df["target"].to_numpy().astype(np.float32)
    if task == "reg" and lo is not None and hi is not None:
        y = np.clip(y, lo, hi)
    return y


def _arrays(spec: M.ModelSpec, cfg: dict, train: pl.DataFrame, valid: pl.DataFrame, features: list[str], threads: int,
            rounds: int | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """训练用的 numpy 数组（之后调用方就可以丢掉 DataFrame，降低内存峰值）"""
    task: str = O.task_of(cfg)
    lo = hi = None
    if task == "reg":
        qs = train["target"].quantile(0.01), train["target"].quantile(0.99)
        lo, hi = (float(qs[0]), float(qs[1])) if qs[0] is not None and qs[1] is not None else (None, None)
    X, y = _matrix(train, features, spec), _target(train, task, lo, hi)
    kw: dict = {"g": _gid(train["date"]), "threads": threads, "seed": SEED, "names": features, "rounds": rounds}
    if rounds is None and valid.height:
        kw.update(Xv=_matrix(valid, features, spec), yv=_target(valid, task, lo, hi), gv=_gid(valid["date"]))
    return X, y, kw


def run(cfg: dict, progress: Progress | None = None, *, base: tuple[pl.DataFrame, pl.DataFrame] | None = None) -> dict:
    say: Progress = progress or (lambda f, m: None)
    t0: float = time.time()
    cfg = O.validate_config(cfg)
    spec: M.ModelSpec = M.get(cfg["model"])
    ok, why = spec.available()
    if not ok:
        raise ValueError(why)
    if base is None:
        est: dict = D.estimate(cfg)
        if est["blocked"]:
            raise ValueError(est["reason"])
    task, hold = O.task_of(cfg), O.hold_of(cfg)
    threads: int = _threads()
    run_id: str = store.new_run_id(cfg["model"])
    say(0.01, f"开始：{O.describe(cfg)}")
    data: D.Dataset = D.build(cfg, progress=lambda f, m: say(0.01 + 0.34 * f, m), base=base)
    ds, feats = data.ds, data.features
    days: list[date] = sorted(ds["date"].unique().to_list())
    folds: list[Fold] = folds_for(days, cfg)
    preds: list[pl.DataFrame] = []
    fold_info: list[dict] = []
    best_iters: list[int] = []
    for i, f in enumerate(folds):
        say(0.36 + 0.44 * i / len(folds), f"滚动训练第 {i + 1}/{len(folds)} 段：用 {f.train_end} 以前的数据，测试 {f.test_start} ~ {f.test_end}")
        tr = ds.filter(purge_mask(ds, f.train_end) & pl.col("target").is_not_null())
        te = ds.filter((pl.col("date") >= f.test_start) & (pl.col("date") <= f.test_end))
        if tr.height < MIN_TRAIN_ROWS or te.is_empty():
            fold_info.append({"train_end": str(f.train_end), "test_start": str(f.test_start), "test_end": str(f.test_end),
                              "skipped": f"训练样本太少（{tr.height} 行）"})
            continue
        train, valid, sampled = _split(tr, spec, cfg["preset"])
        n_train, n_valid = train.height, valid.height
        del tr
        X, y, kw = _arrays(spec, cfg, train, valid, feats, threads)
        del train, valid                                           # 只留 numpy 数组，降低内存峰值
        gc.collect()
        fitted = spec.adapter.fit(X, y, task, cfg["preset"], **kw)
        del X, y, kw
        p = spec.adapter.predict(fitted, _matrix(te, feats, spec), task)
        preds.append(te.select(["date", "code", "net", "excess"]).with_columns(pl.Series("pred", p, dtype=pl.Float64),
                                                                                 pl.lit(i, dtype=pl.Int16).alias("fold")))
        if fitted.best_iter:
            best_iters.append(int(fitted.best_iter))
        fold_info.append({"train_end": str(f.train_end), "test_start": str(f.test_start), "test_end": str(f.test_end),
                          "train_rows": n_train, "valid_rows": n_valid, "sampled": sampled,
                          "best_iter": fitted.best_iter, "test_rows": te.height})
        del te, fitted
        gc.collect()
    if not preds:
        raise ValueError("每一段的训练样本都太少，没法评估：请放宽股票范围或提前起始年份")
    oos: pl.DataFrame = pl.concat(preds).sort(["date", "code"])
    say(0.82, "样本外评估：IC、分五组、前 K 名和同日随机比……")
    ev: dict = E.evaluate(oos, hold, cfg["top_k"])

    say(0.86, "用全部数据训练最终模型（给最新一天打分用）……")
    last: date = days[-1]
    full = ds.filter(purge_mask(ds, last) & pl.col("target").is_not_null())
    rounds: int | None = int(np.median(best_iters) * 1.1) if best_iters and spec.family == "gbdt" else None
    cap = spec.max_rows(cfg["preset"])
    if cap and full.height > cap:
        full = full.sample(n=cap, seed=SEED).sort("date")
    X, y, kw = _arrays(spec, cfg, full, full.head(0), feats, threads, rounds=rounds)
    del full
    gc.collect()
    final = spec.adapter.fit(X, y, task, cfg["preset"], **kw)
    del X, y, kw
    imp: dict[str, float] | None = spec.adapter.importance(final, feats)
    latest_df: pl.DataFrame = ds.filter(pl.col("date") == last).select(["date", "code", *feats])
    latest: list[dict] = score_rows(spec, final, latest_df, feats, task)
    store.save_run(run_id, {
        "id": run_id, "created": datetime.now().strftime("%Y-%m-%d %H:%M"), "config": cfg, "describe": O.describe(cfg),
        "model": {"key": spec.key, "label": spec.label, "explain": spec.explain}, "task": task, "hold": hold,
        "features": feats, "sets": data.sets, "data": data.info, "folds": fold_info, "final_rounds": rounds,
        "evaluation": ev, "importance": _importance_view(imp), "latest": {"date": str(last), "rows": latest[:LATEST_TOP]},
        "seconds": round(time.time() - t0, 1), "threads": threads,
    }, oos.select(["date", "code", "pred", "net", "excess"]), spec, final)
    store.save_latest(run_id, str(last), latest)
    say(1.0, f"完成：{ev['verdict']['text']}")
    return store.summary(store.get_run(run_id))


def score_rows(spec: M.ModelSpec, fitted: M.Fitted, rows: pl.DataFrame, feats: list[str], task: str,
               with_contrib: bool = True) -> list[dict]:
    """给一批行打分（从高到低），LightGBM 另给每只股票贡献最大的 3 个因子"""
    if rows.is_empty():
        return []
    X = _matrix(rows, feats, spec)
    p = spec.adapter.predict(fitted, X, task)
    order = np.argsort(-p)
    contrib = spec.adapter.contrib(fitted, X[order[:LATEST_TOP]]) if with_contrib and spec.explain else None
    codes = rows["code"].to_list()
    out: list[dict] = []
    n: int = len(p)
    for rank, idx in enumerate(order):
        item: dict = {"code": codes[idx], "score": float(p[idx]), "rank": rank + 1, "pct": float(1 - rank / max(n - 1, 1))}
        if contrib is not None and rank < len(contrib):
            c = contrib[rank]
            top = np.argsort(-np.abs(c))[:3]
            item["why"] = [{"name": feats[j], "label": F.feature_label(feats[j]), "value": float(c[j])} for j in top]
        out.append(item)
    return out


def _importance_view(imp: dict[str, float] | None) -> dict | None:
    if not imp:
        return None
    items = sorted(imp.items(), key=lambda kv: -kv[1])
    groups: dict[str, float] = {}
    for k, v in items:
        g = F.set_of(k)
        groups[g] = groups.get(g, 0.0) + v
    return {"top": [{"name": k, "label": F.feature_label(k), "value": v} for k, v in items[:30]],
            "groups": [{"key": g, "label": F.FACTOR_SETS.get(g, {}).get("label", g), "value": v}
                       for g, v in sorted(groups.items(), key=lambda kv: -kv[1])]}
