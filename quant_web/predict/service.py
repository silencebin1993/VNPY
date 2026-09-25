"""
预测编排：读面板→涨跌停列→市场情绪（进程内缓存）→特征→滚动训练→最终模型→保存；预测最新交易日；回测；每日流水线。

三个模型：streak 连板晋级、first 首板潜力（分类，预测次日涨停，定位"观察名单"）；swing 强势股波段（回归，
预测按规则买卖的扣费净收益，定位"模拟跟踪中"，见 swing 模块）。

缓存：面板+涨停列+情绪按面板文件（修改时间/大小）与股票列表失效；最新一天的模型预测按 (模型训练时间, 面板) 缓存，
调权重/筛选条件时不用重算模型。
"""
import gc
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import polars as pl

from .. import config
from ..market import history, sentiment
from ..market import universe as uni_mod
from . import backtest as backtest_mod
from . import features, labels, limits, model, scoring, swing
from .backtest import run_backtest


Progress = Callable[[float, str], None] | None

PANEL_COLUMNS: list[str] = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "is_st"]
KIND_LABELS: dict[str, str] = {"streak": "连板晋级", "first": "首板潜力", "swing": "强势股波段"}
TRAIN_ORDER: list[str] = ["streak", "first", "swing"]         # train("all") / 每日流水线的顺序
REGRESSION_KINDS: set[str] = {"swing"}
WARMUP_DAYS: int = 60               # 面板开头这么多个交易日只用来"热身"（60日均线等），不做样本
PREDICT_TAIL_DAYS: int = 130        # 预测最新一天时只取最近这么多个交易日计算特征（窗口最长 61 天）
NEG_SAMPLE: float = 0.05            # 首板模型训练时负样本抽样比例
CONTRIB_TOP_FIRST: int = 300        # 首板样本外预测每天只给概率前 N 名做贡献分解（全部候选都算概率）
MAX_ROWS: int = 200
MODEL_MAX_AGE_DAYS: int = 30
NEWS_TIMEOUT: float = 15.0
NOTES: list[str] = [
    "所有命中率、收益都只来自样本外预测（滚动训练中未参与训练的时间段）。",
    "行业使用当前归属，存在轻微后视偏差；题材面用行业代替概念板块（东方财富概念成分接口本机不可用）。",
    "ST 状态用当前名称近似全部历史（名称带“退”的已退市股票视同 ST）。",
    "消息面只有实时数据，不参与训练与回测；消息热度≠利好，默认不参与排序。",
]
KIND_NOTES: dict[str, list[str]] = {
    "streak": ["定位为观察名单：模型能预测次日涨停，但按真实规则（次日开盘买入）历史上平均每笔是亏钱的，不作为买入信号。"],
    "first": ["定位为观察名单：模型能预测次日涨停，但按真实规则（次日开盘买入）历史上平均每笔是亏钱的，不作为买入信号。"],
    "swing": [
        "强势股波段预测的是“按规则买卖、扣除全部费用后的净收益”（次日开盘买，首个收盘没涨停的交易日收盘卖，最多5天）。",
        "训练用主板、创业板、科创板的强势股，默认只在主板里挑选；ST（含名称带“退”）和北交所股票不参与。",
    ],
}


def _pct(v: Any, digits: int = 2) -> str:
    return f"{float(v) * 100:+.{digits}f}%" if isinstance(v, int | float) else "—"


def _swing_record_note(meta: dict | None) -> str:
    """波段模型检验成绩（数字取自 meta.trade_oos，重新训练 / refresh_evaluation 后自动更新）"""
    tail: str = ("按你的账户（每笔佣金最低 5 元、一手 100 股）回测通常明显更低——请以“按你的本金回测”的数字为准。"
                 "处于模拟跟踪阶段，不建议直接用真钱。")
    t: dict = (meta or {}).get("trade_oos") or {}
    if t.get("mean") is None:
        return "模型检验（每笔按比例收费、不受资金多少限制）的成绩在训练后显示。" + tail
    rnd: dict = t.get("random") or {}
    mat: dict = t.get("random_matched") or {}
    hold: dict = t.get("holdout") or {}
    cfg: dict = t.get("config") or {}
    text: str = (f"模型检验（每笔按比例收费、不受资金多少限制；主板、每天前 {cfg.get('top_n', 5)} 名、"
                 f"预期净收益 ≥{float(cfg.get('threshold', 0.01)) * 100:g}%）：样本外 {t.get('n')} 笔平均每笔 "
                 f"{_pct(t['mean'])}，天天随便挑 {_pct(rnd.get('mean'))}，同日同数量随便挑 {_pct(mat.get('mean'))}")
    if isinstance(mat.get("mean"), int | float) and isinstance(rnd.get("mean"), int | float):
        text += (f"——只比挑股票多 {(t['mean'] - mat['mean']) * 100:.2f} 个百分点，"
                 f"其余 {(mat['mean'] - rnd['mean']) * 100:.2f} 个百分点来自“哪天做、哪天不做”的择时")
    text += "。"
    if hold.get("mean") is not None:
        hold_mat: dict = hold.get("random_matched") or {}
        text += (f"留出期（{str(cfg.get('holdout_start') or '2025-07-01')[:7]} 以后；没有参与训练；但方案是在研究中比较多种做法后选出的，"
                 f"结果可能偏乐观）{hold.get('n')} 笔平均 {_pct(hold['mean'])}")
        if isinstance(hold_mat.get("mean"), int | float):
            text += (f"，同日同数量随便挑 {_pct(hold_mat['mean'])}"
                     f"（{'模型并不比随便挑好' if hold['mean'] <= hold_mat['mean'] else '模型只多 %.2f 个百分点' % ((hold['mean'] - hold_mat['mean']) * 100)}）")
        text += (f"，t 值 {hold.get('daily_t', '—')}（Newey-West {hold.get('nw_t', '—')}；"
                 "持有几天的交易前后相关，NW 更保守，不到 2 就还不够可信）。")
    return text + tail


def _swing_notes(meta: dict | None) -> list[str]:
    try:
        from .. import settings as settings_mod

        d: dict = settings_mod.defaults_for("swing")["trade"]
    except Exception:  # noqa: BLE001
        d = {"capital": 100_000, "position_pct": 0.04, "fee_rate": 0.00025, "max_gap_pct": 30.0}
    per: float = float(d["capital"]) * float(d["position_pct"])
    min_fee: float = backtest_mod.MIN_COMMISSION
    extra: float = 2 * max(0.0, min_fee / per - float(d["fee_rate"])) if per > 0 else 0.0
    return [
        KIND_NOTES["swing"][0],
        _swing_record_note(meta),
        f"每一轮滚动训练用 {len(model.REG_SEEDS)} 个随机种子各训练一次、预测取平均，每个至少 {model.REG_MIN_ROUNDS} 棵树"
        "（减小重训波动，不是按结果调参）；交易笔数不多，每次重新训练成绩仍会有波动。",
        *KIND_NOTES["swing"][1:],
        f"按你的规则回测时每笔佣金最低 {min_fee:g} 元：本金 {float(d['capital']) / 1e4:g} 万、"
        f"每只 {float(d['position_pct']) * 100:g}%（约 {per:,.0f} 元）时一买一卖约多花 {extra * 100:.1f}%；"
        f"股价高于约 {per / backtest_mod.LOT:.0f} 元的股票一手就超过 {per:,.0f} 元，会因买不起而跳过（skipped_lot）。"
        "模拟盘按比例收费、不受这个限制。",
        f"开盘涨幅上限默认 {float(d.get('max_gap_pct', 30.0)):g}%（主板最多涨 10%，等于不设上限）：模型的标签和检验都没有这个"
        "限制，改小会放弃一部分模型算过的买入。",
    ]


def notes_for(kind: str, meta: dict | None = None) -> list[str]:
    """模型说明。波段的成绩数字来自 meta.trade_oos（meta 为空时读已保存的模型 meta）"""
    if kind == "swing":
        if meta is None:
            meta = model.load_meta(kind)
        return [*_swing_notes(meta), *NOTES]
    return [*KIND_NOTES.get(kind, []), *NOTES]


def _report(progress: Progress, frac: float, msg: str) -> None:
    if progress:
        progress(min(max(frac, 0.0), 1.0), msg)


def _sub(progress: Progress, lo: float, hi: float) -> Progress:
    if progress is None:
        return None
    return lambda f, m: progress(lo + (hi - lo) * min(max(f, 0.0), 1.0), m)


def _now() -> datetime:
    return datetime.now(config.CHINA_TZ)


# ---------------------------------------------------------------- 缓存的面板上下文

@dataclass
class Context:
    key: tuple
    panel_lim: pl.DataFrame
    universe: pl.DataFrame
    sentiment: pl.DataFrame
    dates: list[date]
    loaded_at: str
    seconds: float

    @property
    def last_date(self) -> date | None:
        return self.dates[-1] if self.dates else None


_ctx: Context | None = None
_ctx_lock = threading.Lock()
_pred_cache: dict[tuple, pl.DataFrame] = {}
_pred_lock = threading.Lock()


def _file_key() -> tuple:
    files: list[tuple] = []
    if config.PANEL_DIR.exists():
        for p in sorted(config.PANEL_DIR.glob("*.parquet")):
            st = p.stat()
            files.append((p.name, st.st_mtime_ns, st.st_size))
    uni = config.UNIVERSE_FILE
    return (str(config.PANEL_DIR), tuple(files), uni.stat().st_mtime_ns if uni.exists() else 0)


def context(progress: Progress = None, force: bool = False) -> Context:
    """面板（去停牌）+ 涨跌停列 + 每日情绪，按文件变化自动失效"""
    global _ctx
    key: tuple = _file_key()
    with _ctx_lock:
        if _ctx is not None and _ctx.key == key and not force:
            return _ctx
        t0: float = time.time()
        _report(progress, 0.0, "正在读取日线面板…")
        _ctx = None
        gc.collect()
        panel: pl.DataFrame = history.load_panel(columns=PANEL_COLUMNS)
        uni: pl.DataFrame = uni_mod.load_universe()
        _report(progress, 0.4, f"正在计算涨跌停价与连板数（{panel.height:,} 行）…")
        panel_lim: pl.DataFrame = limits.add_limit_columns(panel, uni) if panel.height else panel
        del panel
        _report(progress, 0.8, "正在计算每日市场情绪…")
        sent: pl.DataFrame = sentiment.daily_sentiment(panel_lim) if panel_lim.height else sentiment._empty()
        dates: list[date] = sent["date"].to_list()
        _ctx = Context(key=key, panel_lim=panel_lim, universe=uni, sentiment=sent, dates=dates,
                       loaded_at=_now().isoformat(timespec="seconds"), seconds=round(time.time() - t0, 1))
        _pred_cache.clear()
        _report(progress, 1.0, "数据已就绪")
        return _ctx


def invalidate() -> None:
    """数据更新后调用：丢弃缓存（下次使用时重新读取）"""
    global _ctx
    with _ctx_lock:
        _ctx = None
        _pred_cache.clear()
    gc.collect()


# ---------------------------------------------------------------- 状态

def _model_summary(kind: str) -> dict | None:
    meta: dict | None = model.load_meta(kind)
    if meta is None:
        return None
    oos: dict = meta.get("oos") or {}
    trained_at: str | None = meta.get("trained_at")
    age: int | None = None
    if trained_at:
        try:
            age = (_now() - datetime.fromisoformat(trained_at)).days
        except ValueError:
            age = None
    out: dict[str, Any] = {
        "kind": kind, "label": KIND_LABELS.get(kind, kind), "trained_at": trained_at,
        "data_start": meta.get("data_start"), "data_end": meta.get("data_end"),
        "n_samples": meta.get("n_samples"), "base_rate": meta.get("base_rate"),
        "auc": oos.get("auc"), "top5_hit": oos.get("top5_hit"), "top1_hit": oos.get("top1_hit"),
        "oos_start": meta.get("oos_start"), "oos_end": meta.get("oos_end"),
        "age_days": age, "stale": age is None or age > MODEL_MAX_AGE_DAYS,
        "train_seconds": meta.get("train_seconds"),
    }
    if kind in REGRESSION_KINDS:
        out["trade"] = _trade_summary(meta)
    return out


def _trade_summary(meta: dict) -> dict | None:
    """波段模型样本外逐笔交易的摘要（默认决策规则：主板、每天前5名、预期净收益≥1%）"""
    t: dict | None = meta.get("trade_oos")
    if not t:
        return None
    sel: dict = t.get("selection") or {}
    hold: dict = t.get("holdout") or {}
    rnd: dict = t.get("random") or {}
    mat: dict = t.get("random_matched") or {}
    return {
        "n": t.get("n"), "avg_return": t.get("mean"), "median_return": t.get("median"), "win_rate": t.get("win_rate"),
        "daily_t": t.get("daily_t"), "nw_t": t.get("nw_t"), "held_skipped": t.get("held_skipped"),
        "fill_rate": t.get("fill_rate"), "trades_per_day": t.get("trades_per_day"),
        "cagr": t.get("cagr"), "max_drawdown": t.get("max_drawdown"),
        "baseline_avg_return": rnd.get("mean"), "baseline_win_rate": rnd.get("win_rate"),
        # 同日同数量随机（只在出信号的日子挑同样多只）：策略减它 = 挑股票的本事，它减 baseline = 择时
        "baseline_matched_avg_return": mat.get("mean"), "baseline_matched_win_rate": mat.get("win_rate"),
        "selection_avg_return": sel.get("mean"), "selection_daily_t": sel.get("daily_t"), "selection_n": sel.get("n"),
        "selection_nw_t": sel.get("nw_t"),
        "selection_baseline_avg_return": (sel.get("random") or {}).get("mean"),
        "selection_baseline_matched_avg_return": (sel.get("random_matched") or {}).get("mean"),
        "holdout_avg_return": hold.get("mean"), "holdout_daily_t": hold.get("daily_t"), "holdout_n": hold.get("n"),
        "holdout_nw_t": hold.get("nw_t"),
        "holdout_baseline_avg_return": (hold.get("random") or {}).get("mean"),
        "holdout_baseline_matched_avg_return": (hold.get("random_matched") or {}).get("mean"),
        "config": t.get("config"),
        # 这里按比例收费、不受资金限制；按用户账户（最低佣金 5 元、一手 100 股）回测见 /api/predict/backtest，通常更低
        "basis": "per_trade",
    }


def dataset_status() -> dict:
    """面板起止、股票数、最后更新时间、模型状态（只扫描文件统计，不加载整个面板）"""
    files = sorted(config.PANEL_DIR.glob("*.parquet")) if config.PANEL_DIR.exists() else []
    panel: dict[str, Any] = {"start": None, "end": None, "stocks": 0, "rows": 0, "updated_at": None}
    if files:
        st: pl.DataFrame = pl.scan_parquet([str(p) for p in files]).filter(pl.col("tradestatus") != 0).select(
            pl.col("date").min().alias("start"), pl.col("date").max().alias("end"),
            pl.col("code").n_unique().alias("stocks"), pl.len().alias("rows"),
        ).collect()
        r: dict = st.row(0, named=True)
        mtime: float = max(p.stat().st_mtime for p in files)
        panel = {
            "start": r["start"].isoformat() if r["start"] else None,
            "end": r["end"].isoformat() if r["end"] else None,
            "stocks": int(r["stocks"]), "rows": int(r["rows"]),
            "updated_at": datetime.fromtimestamp(mtime, config.CHINA_TZ).isoformat(timespec="seconds"),
        }
    uni: pl.DataFrame = uni_mod.load_universe()
    fund_path = config.STOCK_LAB.joinpath("fundamentals.parquet")
    fund: dict[str, Any] = {"rows": 0, "stocks": 0}
    if fund_path.exists():
        f = pl.scan_parquet(str(fund_path)).select(pl.len().alias("rows"), pl.col("code").n_unique().alias("stocks"))
        fund = f.collect().row(0, named=True)
    lhb_path = config.STOCK_LAB.joinpath("lhb.parquet")
    lhb: dict[str, Any] = {"rows": 0, "start": None, "end": None}
    if lhb_path.exists():
        try:
            lr: dict = pl.scan_parquet(str(lhb_path)).select(
                pl.len().alias("rows"), pl.col("date").min().alias("start"), pl.col("date").max().alias("end")
            ).collect().row(0, named=True)
            lhb = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in lr.items()}
        except Exception:  # noqa: BLE001
            pass
    return {
        "panel": panel,
        "universe": {"stocks": uni.height, "listed": int((uni["status"] == 1).sum()) if uni.height else 0},
        "fundamentals": fund,
        "lhb": lhb,
        "models": {k: _model_summary(k) for k in KIND_LABELS},
        "context_loaded": _ctx is not None,
    }


# ---------------------------------------------------------------- 训练

def _dataset(ctx: Context, kind: str, neg_sample: float | None, progress: Progress = None
             ) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    """(训练样本（已加标签，首板为抽样）, 全市场逐行特征 base, 可选数据源)"""
    _report(progress, 0.0, "正在计算逐日技术面/资金面特征…")
    base: pl.DataFrame = features.compute_base(ctx.panel_lim, ctx.universe)
    extras: dict = {"fund": features._load_fund(), "zt_pool": features._load_zt_pool()}
    _report(progress, 0.6, "正在组装候选股票的五维特征…")
    start: date = ctx.dates[min(WARMUP_DAYS, len(ctx.dates) - 1)]
    ds: pl.DataFrame = features.build_features(
        ctx.panel_lim, ctx.universe, ctx.sentiment, kind, neg_sample=neg_sample, base=base, **extras
    ).filter(pl.col("date") >= start)
    ds = labels.add_labels(ds, ctx.panel_lim)
    _report(progress, 1.0, f"样本 {ds.height:,} 条")
    return ds, base, extras


def train(kind: str, progress: Progress = None, neg_sample: float | None = None,
          first_test_year: int = 2022) -> dict:
    """构建特征→滚动训练（样本外预测）→最终模型→保存 meta 与 oos。返回训练摘要。
    kind="all" 时依次训练 streak → first → swing，返回 {kind: 摘要}"""
    if kind == "all":
        results: dict[str, dict] = {}
        for i, k in enumerate(TRAIN_ORDER):
            results[k] = train(k, _sub(progress, i / len(TRAIN_ORDER), (i + 1) / len(TRAIN_ORDER)), neg_sample,
                               first_test_year)
        return results
    if kind not in KIND_LABELS:
        raise ValueError(f"未知的预测类型：{kind}（可选 streak/first/swing）")
    if kind in REGRESSION_KINDS:
        return _train_swing(progress, first_test_year)
    t0: float = time.time()
    label: str = KIND_LABELS[kind]
    _report(progress, 0.0, f"开始训练「{label}」模型")
    ctx: Context = context(_sub(progress, 0.0, 0.1))
    if ctx.panel_lim.is_empty() or len(ctx.dates) <= WARMUP_DAYS:
        raise RuntimeError("日线数据不足，请先到数据中心更新数据")
    ns: float | None = (neg_sample or NEG_SAMPLE) if kind == "first" else None
    ds, base, extras = _dataset(ctx, kind, ns, _sub(progress, 0.1, 0.25))
    holder: dict[str, pl.DataFrame] = {"base": base}
    del base
    cols: list[str] = features.feature_columns(kind)
    offset: float = math.log(ns) if ns else 0.0
    folds: list[model.Fold] = model.make_folds(ds["date"].unique().to_list(), first_test_year)
    if not folds:
        raise RuntimeError("数据时间太短，无法做滚动训练（需要 2022 年以前的数据）")

    test_ds: Callable[[model.Fold], pl.DataFrame] | None = None
    if ns:
        def test_ds(fold: model.Fold) -> pl.DataFrame:
            days: list[date] = [d for d in ctx.dates if fold.test_start <= d <= fold.test_end]
            te: pl.DataFrame = features.build_features(
                ctx.panel_lim, ctx.universe, ctx.sentiment, kind, dates=days, base=holder["base"], **extras)
            return labels.add_labels(te, ctx.panel_lim)

    oos, fold_metrics = model.train_walk_forward(
        ds, cols, folds, progress=_sub(progress, 0.25, 0.85), test_ds=test_ds, offset=offset,
        contrib_top=CONTRIB_TOP_FIRST if ns else None, keep_cols=features.DISPLAY_COLUMNS,
    )
    holder.clear()
    gc.collect()
    _report(progress, 0.86, "正在用全部数据训练最终模型…")
    iters: list[int] = [m["best_iter"] for m in fold_metrics if "best_iter" in m]
    rounds: int | None = int(np.median(iters) * 1.1) if iters else None
    booster = model.train_final(ds, cols, rounds)
    booster.qw_logit_offset = offset                        # type: ignore[attr-defined]
    imp, top = model.importance(booster, cols)
    overall: dict = model.evaluate(oos)
    lab_ds: pl.DataFrame = ds.filter(pl.col("y").is_not_null())
    meta: dict[str, Any] = {
        "kind": kind,
        "label": label,
        "trained_at": _now().isoformat(timespec="seconds"),
        "data_start": ds["date"].min().isoformat(),
        "data_end": ctx.last_date.isoformat() if ctx.last_date else None,
        "n_samples": lab_ds.height,
        "n_positive": int(lab_ds["y"].sum()),
        "base_rate": overall.get("base_rate"),
        "train_base_rate": float(lab_ds["y"].mean()) if lab_ds.height else None,
        "neg_sample": ns,
        "logit_offset": offset,
        "n_rounds": booster.current_iteration(),
        "feature_cols": cols,
        "feature_labels": {c: features.FEATURE_LABELS.get(c, c) for c in cols},
        "group_labels": features.GROUP_LABELS,
        "folds": fold_metrics,
        "oos": overall,
        "oos_start": oos["date"].min().isoformat() if oos.height else None,
        "oos_end": oos["date"].max().isoformat() if oos.height else None,
        "topn_hit": [{"n": k, "hit_rate": overall.get(f"top{k}_hit")} for k in model.TOP_KS],
        "calibration": model.calibration(oos),
        "importance": imp,
        "top_features": top,
        "notes": notes_for(kind),
        "seconds": None,
    }
    daily: pl.DataFrame | None = None
    if ns:
        # 首板：全部候选都算了概率（上面的指标用全部候选），但只保存每天概率前 CONTRIB_TOP_FIRST 名
        # （有贡献分解，可以调权重回测），另存每日候选数/涨停数供回测算基准率
        daily = oos.group_by("date").agg(
            pl.col("y").is_not_null().sum().alias("n"), pl.col("y").sum().cast(pl.Int64).alias("positives")
        ).sort("date")
        oos = oos.filter(pl.col("bias").is_not_null())
    meta["n_oos_saved"] = oos.height
    meta["seconds"] = round(time.time() - t0, 1)
    _report(progress, 0.97, "正在保存模型…")
    model.save(kind, booster, meta, oos, daily)
    with _pred_lock:
        _pred_cache.clear()
    msg: str = (f"「{label}」训练完成：样本外 AUC {overall['auc']:.3f}，每天前5名次日涨停率 "
                f"{(overall.get('top5_hit') or 0) * 100:.1f}%（全部候选平均 {(overall.get('base_rate') or 0) * 100:.1f}%）"
                if overall.get("auc") is not None else f"「{label}」训练完成")
    _report(progress, 1.0, msg)
    return {
        "kind": kind, "message": msg, "seconds": meta["seconds"], "n_samples": meta["n_samples"],
        "n_oos": oos.height, "folds": len([m for m in fold_metrics if "best_iter" in m]),
        "oos": overall, "importance": imp, "data_end": meta["data_end"],
    }


def _train_swing(progress: Progress = None, first_test_year: int = 2022) -> dict:
    """强势股波段：分块算特征 → 可实现收益标签 → 回归滚动训练 → 最终模型 → 逐笔交易评价 → 保存"""
    kind: str = "swing"
    t0: float = time.time()
    label: str = KIND_LABELS[kind]
    _report(progress, 0.0, f"开始训练「{label}」模型")
    ctx: Context = context(_sub(progress, 0.0, 0.1))
    if ctx.panel_lim.is_empty() or len(ctx.dates) <= WARMUP_DAYS:
        raise RuntimeError("日线数据不足，请先到数据中心更新数据")
    start: date = ctx.dates[min(WARMUP_DAYS, len(ctx.dates) - 1)]
    booster, meta, oos = swing.train_model(ctx.panel_lim, ctx.universe, ctx.sentiment, start,
                                           _sub(progress, 0.1, 0.95), first_test_year)
    meta = {
        "kind": kind, "label": label, "trained_at": _now().isoformat(timespec="seconds"),
        "data_end": ctx.last_date.isoformat() if ctx.last_date else None, **meta,
        "n_oos_saved": oos.height, "seconds": round(time.time() - t0, 1),
    }
    meta["notes"] = notes_for(kind, meta)
    _report(progress, 0.97, "正在保存模型…")
    model.save(kind, booster, meta, oos, None)
    _oos_cache.pop(kind, None)
    with _pred_lock:
        _pred_cache.clear()
    tr: dict = meta.get("trade_oos") or {}
    rnd: dict = tr.get("random") or {}
    hold: dict = tr.get("holdout") or {}
    msg: str = f"「{label}」训练完成"
    if tr.get("mean") is not None:
        msg += (f"：模型检验（按比例收费、不受资金限制）样本外按默认规则每笔平均 {tr['mean'] * 100:+.2f}%（天天随便挑 "
                f"{(rnd.get('mean') or 0) * 100:+.2f}%），留出期每笔 {(hold.get('mean') or 0) * 100:+.2f}%"
                f"（同日同数量随便挑 {((hold.get('random_matched') or {}).get('mean') or 0) * 100:+.2f}%；"
                f"t {hold.get('daily_t') if hold.get('daily_t') is not None else '—'}，"
                f"Newey-West t {hold.get('nw_t') if hold.get('nw_t') is not None else '—'}），共 {tr.get('n')} 笔；"
                "按你的本金回测通常更低，见预测页")
    _report(progress, 1.0, msg)
    return {
        "kind": kind, "message": msg, "seconds": meta["seconds"], "n_samples": meta["n_samples"],
        "n_oos": oos.height, "folds": len([m for m in meta["folds"] if "best_iter" in m]),
        "oos": meta["oos"], "trade": _trade_summary(meta), "importance": meta["importance"],
        "data_end": meta["data_end"],
    }


# ---------------------------------------------------------------- 预测

def _family(kind: str | None) -> str:
    return "ret" if kind in REGRESSION_KINDS else "prob"


def _settings_for(kind: str, settings: Any) -> tuple[dict, dict]:
    """某个模型要用的 (predict, trade) 设置。保存的设置是另一类模型（概率类 ↔ 波段）时，门槛/每天几只/板块/仓位/
    最多持有/卖出规则换成该模型的默认值（settings.defaults_for），其余保留"""
    ps, ts = _settings(settings)
    saved: str | None = ps.get("kind")
    if saved and _family(saved) != _family(kind):
        try:
            from .. import settings as settings_mod

            d: dict = settings_mod.defaults_for(kind)
            for part, fields in settings_mod.KIND_FIELDS.items():
                target: dict = ps if part == "predict" else ts
                for f in fields:
                    target[f] = d[part][f]
        except Exception:  # noqa: BLE001
            pass
    ps["kind"] = kind
    return ps, ts


def _settings(settings: Any) -> tuple[dict, dict]:
    """(predict 设置 dict, trade 设置 dict)；接受 Settings / 同名属性对象 / dict / None（读保存的设置）"""
    if settings is None:
        try:
            from .. import settings as settings_mod

            settings = settings_mod.load()
        except Exception:  # noqa: BLE001
            settings = {}
    if isinstance(settings, dict):
        return scoring._as_dict(settings.get("predict")), scoring._as_dict(settings.get("trade"))
    return scoring._as_dict(getattr(settings, "predict", None)), scoring._as_dict(getattr(settings, "trade", None))


def _latest_pred(kind: str, ctx: Context, booster: Any, meta: dict) -> pl.DataFrame:
    """最新交易日全部候选的模型预测（含展示列与 reasons），按 (面板, 模型) 缓存。
    分类模型给 prob；波段模型给 pred（预测净收益，prob 为空）"""
    key: tuple = (kind, ctx.key, meta.get("trained_at"))
    with _pred_lock:
        hit: pl.DataFrame | None = _pred_cache.get(key)
        if hit is not None:
            return hit
        last: date = ctx.dates[-1]
        tail_start: date = ctx.dates[max(0, len(ctx.dates) - PREDICT_TAIL_DAYS)]
        panel_tail: pl.DataFrame = ctx.panel_lim.filter(pl.col("date") >= tail_start)
        feat: pl.DataFrame = features.build_features(panel_tail, ctx.universe, ctx.sentiment, kind, dates=[last])
        if kind in REGRESSION_KINDS:
            pred: pl.DataFrame = model.predict_reg(booster, feat, meta["feature_cols"])
        else:
            pred = model.predict(booster, feat, meta["feature_cols"])
        pred = pred.join(feat.select(["date", "code", *features.DISPLAY_COLUMNS]), on=["date", "code"],
                         how="left", maintain_order="left")
        _pred_cache[key] = pred
        return pred


def _news_heat(codes: list[str]) -> tuple[pl.DataFrame | None, str | None]:
    """消息热度（有超时保护）；失败返回 (None, 警告)"""
    if not codes:
        return None, None
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        from ..market import news

        fut = pool.submit(news.news_heat, codes, 24)
        df: pl.DataFrame = fut.result(timeout=NEWS_TIMEOUT)
        if df is None or df.is_empty():
            return None, "最近24小时没有抓到相关消息，消息面评分按中性(50)处理"
        return df, None
    except FutureTimeout:
        return None, "消息面数据获取超时，消息面评分按中性(50)处理"
    except Exception:  # noqa: BLE001
        return None, "消息面数据暂时拿不到，消息面评分按中性(50)处理"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _num(v: Any, digits: int = 4) -> Any:
    if v is None:
        return None
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else round(v, digits)
    return v


def _model_block(kind: str, meta: dict, top_n: int) -> dict:
    oos: dict = meta.get("oos") or {}
    if kind in REGRESSION_KINDS:
        k: int = min(model.REG_TOP_KS, key=lambda x: (abs(x - top_n), x))
        return {
            "trained_at": meta.get("trained_at"), "data_end": meta.get("data_end"),
            "oos_topn_hit": oos.get(f"top{k}_win"), "oos_topn_return": oos.get(f"top{k}_mean"), "oos_topn": k,
            "base_rate": meta.get("base_rate"), "base_return": meta.get("base_return"), "auc": None,
            "ic": oos.get("ic"), "oos_start": meta.get("oos_start"), "oos_end": meta.get("oos_end"),
            "trade": _trade_summary(meta),
        }
    k = min(model.TOP_KS, key=lambda x: (abs(x - top_n), x))
    return {
        "trained_at": meta.get("trained_at"), "data_end": meta.get("data_end"),
        "oos_topn_hit": oos.get(f"top{k}_hit"), "oos_topn": k, "base_rate": meta.get("base_rate"),
        "auc": oos.get("auc"), "oos_start": meta.get("oos_start"), "oos_end": meta.get("oos_end"),
    }


def predict_latest(kind: str, settings: Any = None) -> dict:
    """/api/predict/today 的返回（见 ARCHITECTURE 第4节、7.2）。

    波段（swing）：列表是全部候选（按设置筛选，但不按门槛删除），按预测净收益排序；pick = 前 top_n 且 ≥ 门槛；
    行里 prob 为空、score 为预测净收益（小数）、exp_ret 为预测净收益（%）；顶层另有 gate（今天操作/不操作）与
    plan（买入、卖出、仓位三句话）。"""
    if kind not in KIND_LABELS:
        raise ValueError(f"未知的预测类型：{kind}（可选 streak/first/swing）")
    regression: bool = kind in REGRESSION_KINDS
    ps, ts = _settings_for(kind, settings)
    out: dict[str, Any] = {
        "kind": kind, "label": KIND_LABELS[kind], "signal_date": None,
        "generated_at": _now().isoformat(timespec="seconds"), "model": None, "count": 0, "rows": [],
        "filtered_out": 0, "warnings": [], "notes": notes_for(kind),
        "settings_used": {"predict": dict(ps), "trade": dict(ts)},     # 这个名单实际用的设置（按 kind 换过默认值）
    }
    if regression:
        gate, plan = swing.gate_and_plan([], ps, ts, None, ok=False)
        out.update(gate=gate, plan=plan)
    meta_now: dict | None = None
    ctx: Context = context()
    if not ctx.dates:
        out["warnings"].append("还没有日线数据，请先到数据中心点「更新数据」")
        return out
    out["signal_date"] = ctx.dates[-1].isoformat()
    loaded = model.load(kind)
    if loaded is None:
        out["warnings"].append(f"还没有训练「{KIND_LABELS[kind]}」模型，请先到模型中心点「开始训练」")
        return out
    booster, meta = loaded
    meta_now = meta
    out["notes"] = notes_for(kind, meta)
    top_n: int = int(ps.get("top_n", 5))
    threshold: float = float(ps.get("threshold") or 0.0)
    out["model"] = _model_block(kind, meta, top_n)
    summary: dict | None = _model_summary(kind)
    if summary and summary.get("stale"):
        out["warnings"].append(f"模型已经 {summary.get('age_days')} 天没有重新训练，建议到模型中心重新训练")
    today: datetime = _now()
    if today.weekday() < 5 and today.hour * 60 + today.minute > 15 * 60 + 30 and ctx.dates[-1] < today.date():
        out["warnings"].append(f"数据最后一天是 {ctx.dates[-1]}，今天的收盘数据可能还没更新")
    missing: list[str] = [c for c in meta["feature_cols"] if c not in features.ALL_FEATURES]
    if missing:
        out["warnings"].append("模型与当前特征版本不一致，建议重新训练")

    pred: pl.DataFrame = _latest_pred(kind, ctx, booster, meta)
    if pred.is_empty():
        out["warnings"].append("今天没有符合条件的候选股票")
        if regression:
            out["gate"], out["plan"] = swing.gate_and_plan([], ps, ts, out["signal_date"], meta=meta_now)
        return out
    news_df, warn = _news_heat(pred["code"].to_list())
    if warn:
        out["warnings"].append(warn)
    scored: pl.DataFrame = scoring.apply_weights(pred, ps.get("weights"), news_df, float(ps.get("news_weight", 0.0)),
                                                 regression=regression)
    flt: dict = {**ps, "threshold": 0.0} if regression else ps
    kept: pl.DataFrame = scoring.filter_candidates(scored, flt).sort("score", descending=True)
    out["filtered_out"] = scored.height - kept.height
    out["count"] = kept.height
    names: dict[str, str] = dict(zip(ctx.universe["code"].to_list(), ctx.universe["name"].to_list(), strict=False)) \
        if ctx.universe.height else {}
    rows: list[dict] = []
    for i, r in enumerate(kept.head(MAX_ROWS).iter_rows(named=True)):
        score: float | None = r["score"]
        pick: bool = i < top_n and (not regression or (score is not None and score >= threshold))
        row: dict[str, Any] = {
            "code": r["code"], "name": names.get(r["code"], ""), "industry": r.get("industry"),
            "board": r.get("board"), "close": _num(r.get("close"), 3),
            "pct": _num(r["pct"] * 100 if r.get("pct") is not None else None, 2),
            "streak": r.get("streak"), "turn": _num(r.get("turn"), 2), "float_cap": _num(r.get("float_cap"), 2),
            "one_word": r.get("one_word"), "is_st": r.get("is_st"),
            "prob": _num(r.get("prob")), "score": _num(score, 5 if regression else 4),
            "dims": {**{g: _num(r[f"dim_{g}"], 1) for g in features.GROUPS}, "news": _num(r.get("dim_news"), 1)},
            "reasons": scoring.reasons_text(r.get("reasons"), regression=regression),
            "news_count": int(r["news_count"]) if r.get("news_count") is not None else 0,
            "policy_count": int(r["policy_count"]) if r.get("policy_count") is not None else 0,
            "pick": pick,
        }
        if regression:
            row["exp_ret"] = _num(score * 100 if score is not None else None, 2)
        rows.append(row)
    out["rows"] = rows
    if regression:
        out["gate"], out["plan"] = swing.gate_and_plan([r for r in rows if r["pick"]], ps, ts, out["signal_date"],
                                                       meta=meta_now)
    return out


def prediction_for(code: str, settings: Any = None) -> dict | None:
    """该股若在今天任一模型的候选中，返回预测行（个股资料页用），否则 None。按 swing → streak → first 的顺序找。

    in_list/rank/pick：按用户的权重与筛选条件（不含消息面），它在今天名单里的位置；
    不在名单里（如流通市值超过上限）时 in_list=False，rank 为空。
    base_prob：今天全部候选的平均模型概率，用来判断这只股票的概率算高还是低（波段为空，另给 exp_ret / base_exp_ret（%））。
    """
    for kind in ("swing", "streak", "first"):
        try:
            loaded = model.load(kind)
            if loaded is None:
                continue
            ctx: Context = context()
            pred: pl.DataFrame = _latest_pred(kind, ctx, *loaded)
        except Exception:  # noqa: BLE001
            continue
        if pred.filter(pl.col("code") == code).is_empty():
            continue
        regression: bool = kind in REGRESSION_KINDS
        kps, _ = _settings_for(kind, settings)
        scored: pl.DataFrame = scoring.apply_weights(pred, kps.get("weights"), regression=regression)
        r: dict = scored.filter(pl.col("code") == code).row(0, named=True)
        flt: dict = {**kps, "threshold": 0.0} if regression else kps
        kept: pl.DataFrame = scoring.filter_candidates(scored, flt).sort("score", descending=True)
        pos: list[int] = [i for i, c in enumerate(kept["code"].to_list()) if c == code]
        rank: int | None = pos[0] + 1 if pos else None
        top_n: int = int(kps.get("top_n", 5))
        pick: bool = rank is not None and rank <= top_n
        if regression:
            pick = pick and r["score"] is not None and r["score"] >= float(kps.get("threshold") or 0.0)
        out: dict[str, Any] = {
            "kind": kind, "label": KIND_LABELS[kind], "signal_date": r["date"].isoformat(),
            "prob": _num(r.get("prob")), "score": _num(r["score"], 5 if regression else 4), "streak": r.get("streak"),
            "dims": {g: _num(r[f"dim_{g}"], 1) for g in features.GROUPS},
            "reasons": scoring.reasons_text(r.get("reasons"), regression=regression),
            "in_list": rank is not None, "rank": rank, "pick": pick,
            "n_candidates": pred.height, "n_list": kept.height,
            "base_prob": _num(float(pred["prob"].mean())) if pred.height and not regression else None,
        }
        if regression:
            out["exp_ret"] = _num(r["score"] * 100 if r["score"] is not None else None, 2)
            out["base_exp_ret"] = _num(float(pred["pred"].mean()) * 100, 2) if pred.height else None
        return out
    return None


# ---------------------------------------------------------------- 回测

_oos_cache: dict[str, tuple[int, tuple[pl.DataFrame, pl.DataFrame | None]]] = {}


def _oos(kind: str) -> tuple[pl.DataFrame, pl.DataFrame | None] | None:
    """(样本外预测, 每日候选统计或 None)，按文件修改时间缓存"""
    p = model.paths(kind)
    try:
        stamp: int = p["oos"].stat().st_mtime_ns
    except OSError:
        return None
    hit = _oos_cache.get(kind)
    if hit and hit[0] == stamp:
        return hit[1]
    daily: pl.DataFrame | None = pl.read_parquet(p["daily"]) if p["daily"].exists() else None
    value: tuple[pl.DataFrame, pl.DataFrame | None] = (pl.read_parquet(p["oos"]), daily)
    _oos_cache[kind] = (stamp, value)
    return value


def backtest(kind: str, settings: Any = None) -> dict:
    """用缓存的样本外预测按设置回测（消息面没有历史，回测中权重恒为 0）。
    另有 metrics.daily_t、baseline（同池随机 20 次平均）、by_period（选择期/留出期）"""
    if kind not in KIND_LABELS:
        raise ValueError(f"未知的预测类型：{kind}（可选 streak/first/swing）")
    loaded = _oos(kind)
    if loaded is None or loaded[0].is_empty():
        raise RuntimeError(f"还没有「{KIND_LABELS[kind]}」模型的样本外预测，请先到模型中心训练")
    oos, daily = loaded
    ps, ts = _settings_for(kind, settings)
    ctx: Context = context()
    t0: float = time.time()
    delisted: set[str] = set(ctx.universe.filter(pl.col("status") == 0)["code"].to_list()) \
        if ctx.universe.height and "status" in ctx.universe.columns else set()
    result: dict = run_backtest(oos, ctx.panel_lim, ps, ts, daily, delisted=delisted)
    result["kind"] = kind
    result["settings_used"] = {"predict": ps, "trade": ts}
    result["tuned"] = tuned_fields(kind, ps, ts)
    result["seconds"] = round(time.time() - t0, 2)
    # 本次回测自己的提示（如资金不足跳过太多信号）在前，模型说明在后
    result["notes"] = [*(result.get("notes") or []), *notes_for(kind)]
    return result


# 算"按历史结果调过的设置"时不算的字段：本金、费用是账户的事实（不是挑方案）；消息面不参与回测；kind 只选模型
TUNING_SKIP: dict[str, set[str]] = {
    "predict": {"kind", "news_weight"},
    "trade": {"capital", "fee_rate", "stamp_duty", "stamp_by_date", "slippage"},
}
TUNING_LABELS: dict[str, str] = {
    "weights": "五维权重", "exclude_st": "排除ST", "boards": "板块", "min_float_cap": "流通市值下限",
    "max_float_cap": "流通市值上限", "min_price": "最低股价", "max_price": "最高股价", "streak_min": "连板数下限",
    "streak_max": "连板数上限", "exclude_one_word": "排除一字板", "threshold": "门槛", "top_n": "每天几只",
    "position_pct": "单只仓位", "max_positions": "最多持有", "max_gap_pct": "开盘涨太多不买", "exit_rule": "卖出规则",
    "stop_loss_pct": "止损",
}


def tuned_fields(kind: str, ps: dict, ts: dict) -> dict:
    """这套设置和该模型推荐设置（settings.defaults_for）不一样的选股/买卖字段。
    改过的设置如果是看着历史检验结果挑的，历史成绩（含留出期）会更偏乐观（留出期没有参与训练；但推荐方案本身也是在研究中比较多种做法后选出的）。
    返回 {"modified": bool, "fields": [中文名...]}"""
    from .. import settings as settings_mod

    try:
        d: dict = settings_mod.defaults_for(kind)
    except Exception:  # noqa: BLE001
        return {"modified": False, "fields": []}
    changed: list[str] = []
    for part, cur in (("predict", ps), ("trade", ts)):
        for key, dv in d[part].items():
            if key in TUNING_SKIP[part] or key not in cur:
                continue
            v: Any = cur[key]
            same: bool
            if isinstance(dv, float | int) and not isinstance(dv, bool) and isinstance(v, float | int):
                same = abs(float(v) - float(dv)) <= 1e-9 * max(1.0, abs(float(dv)))
            elif isinstance(dv, dict) and isinstance(v, dict):
                same = all(abs(float(v.get(k, 1.0)) - float(x)) <= 1e-9 for k, x in dv.items())
            elif isinstance(dv, list) and isinstance(v, list | tuple):
                same = sorted(map(str, v)) == sorted(map(str, dv))
            else:
                same = v == dv
            if not same:
                changed.append(TUNING_LABELS.get(key, key))
    return {"modified": bool(changed), "fields": changed}


def refresh_evaluation(kind: str = "swing") -> dict:
    """用已保存的样本外预测按当前口径重算波段模型的逐笔评价（meta.trade_oos，含同日同数量随机基准），只重写 meta，
    不重新训练（重新训练成绩会随早停轮数波动）。返回新的 trade_oos 摘要"""
    if kind not in REGRESSION_KINDS:
        raise ValueError(f"只有波段模型需要重算逐笔评价（{kind} 不是）")
    meta: dict | None = model.load_meta(kind)
    oos: pl.DataFrame | None = model.load_oos(kind)
    if meta is None or oos is None or oos.is_empty():
        raise RuntimeError(f"还没有「{KIND_LABELS[kind]}」模型的样本外预测，请先训练")
    meta["trade_oos"] = swing.trade_oos(oos)
    meta["notes"] = notes_for(kind, meta)
    meta["eval_refreshed_at"] = _now().isoformat(timespec="seconds")
    model.save_meta(kind, meta)
    _oos_cache.pop(kind, None)
    with _pred_lock:
        _pred_cache.clear()
    return _trade_summary(meta) or {}


# ---------------------------------------------------------------- 每日流水线

def update_data(progress: Progress = None) -> dict:
    """股票列表（超过20小时才刷新）→ 日线增量 → 涨停池存档 → 龙虎榜 → 财报摘要（每只每周一次）"""
    out: dict[str, Any] = {"warnings": []}
    key_before: tuple = _file_key()
    uni_path = config.UNIVERSE_FILE
    stale: bool = not uni_path.exists() or time.time() - uni_path.stat().st_mtime > 20 * 3600
    if stale:
        _report(progress, 0.0, "正在更新股票列表…")
        try:
            out["universe"] = uni_mod.refresh_universe().height
        except Exception as e:  # noqa: BLE001
            out["warnings"].append(f"股票列表更新失败：{e}")
    _report(progress, 0.05, "正在更新日线行情…")
    try:
        out["history"] = history.update_history(_sub(progress, 0.05, 0.6))
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"日线更新失败：{e}")
    try:
        from ..market import pools

        _report(progress, 0.6, "正在存档今天的涨停池…")
        last: date | None = history.last_date()
        if last is not None and last == _now().date():
            out["pools"] = pools.archive(last)
        _report(progress, 0.65, "正在更新龙虎榜…")
        out["lhb"] = pools.update_lhb()
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"涨停池/龙虎榜更新失败：{e}")
    try:
        from ..market import fundamentals

        _report(progress, 0.7, "正在更新财报摘要（每只股票每周更新一次）…")
        out["fundamentals"] = fundamentals.update_fundamentals(_sub(progress, 0.7, 0.95))
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"财报摘要更新失败：{e}")
    if _file_key() != key_before:
        invalidate()                # 面板/股票列表有变化：下次使用时重新读取
    else:
        with _pred_lock:            # 面板没变（数据本来就是最新）：保留内存里的面板，只清预测缓存（龙虎榜/财报可能有更新）
            _pred_cache.clear()
    _report(progress, 1.0, "数据更新完成")
    return out


def _kind_settings(kind: str) -> Any:
    """保存的设置切到某个模型：与网页 /api/predict/today 相同（paper.settings_for：连板/首板共用保存的设置，
    波段 ↔ 概率类之间按 settings.for_kind 换随模型切换的字段），这样自动记进模拟盘的名单就是网页上看到的名单；
    失败时返回 None（predict_latest 会自己读）"""
    try:
        from .. import paper

        return paper.settings_for(kind)
    except Exception:  # noqa: BLE001
        try:
            from .. import settings as settings_mod

            return settings_mod.for_kind(settings_mod.load(), kind)
        except Exception:  # noqa: BLE001
            return None


def daily_pipeline(progress: Progress = None) -> dict:
    """一键：更新数据 →（模型不存在或超过30天未训练则重训，顺序 streak→first→swing）→ 预测三个模型的最新交易日 →
    （有模拟盘模块时）记录今天的正式信号"""
    t0: float = time.time()
    out: dict[str, Any] = {"trained": [], "predictions": {}, "warnings": []}
    upd: dict = update_data(_sub(progress, 0.0, 0.4))
    out["update"] = upd
    out["warnings"].extend(upd.get("warnings", []))
    _report(progress, 0.4, "正在加载数据…")
    context(_sub(progress, 0.4, 0.45))
    kinds: list[str] = list(TRAIN_ORDER)
    for i, kind in enumerate(kinds):
        summary: dict | None = _model_summary(kind)
        if summary is None or summary.get("stale"):
            lo: float = 0.45 + 0.45 * i / len(kinds)
            try:
                out["trained"].append(train(kind, _sub(progress, lo, lo + 0.45 / len(kinds))))
            except Exception as e:  # noqa: BLE001
                out["warnings"].append(f"「{KIND_LABELS[kind]}」模型训练失败：{e}")
    _report(progress, 0.92, "正在预测最新交易日…")
    results: dict[str, tuple[dict, Any]] = {}
    for kind in kinds:
        try:
            ks: Any = _kind_settings(kind)
            res: dict = predict_latest(kind, ks)
            results[kind] = (res, ks)
            item: dict[str, Any] = {
                "signal_date": res["signal_date"], "count": res["count"],
                "picks": [{"code": r["code"], "name": r["name"], "score": r["score"]} for r in res["rows"] if r["pick"]],
                "warnings": res["warnings"],
            }
            if "gate" in res:
                item["gate"] = res["gate"]
            out["predictions"][kind] = item
        except Exception as e:  # noqa: BLE001
            out["warnings"].append(f"「{KIND_LABELS[kind]}」预测失败：{e}")
    try:
        from .. import paper      # 模拟盘（前向跟踪）；没有这个模块时跳过
    except ImportError:
        paper = None
    if paper is not None and hasattr(paper, "record_signals"):
        out["paper"] = {}
        for kind, (res, ks) in results.items():
            try:
                out["paper"][kind] = paper.record_signals(kind, res, ks)
            except Exception as e:  # noqa: BLE001
                out["warnings"].append(f"「{KIND_LABELS[kind]}」模拟盘记录失败：{e}")
    out["seconds"] = round(time.time() - t0, 1)
    _report(progress, 1.0, "每日流程完成")
    return out


def warm_up() -> None:
    """服务启动后可在后台调用，提前把面板读进内存"""
    try:
        context()
    except Exception:  # noqa: BLE001
        pass


def models_meta(kind: str) -> dict | None:
    """模型中心页用的完整 meta（notes 用当前版本的说明文字，而不是训练时存下的）"""
    meta: dict | None = model.load_meta(kind)
    if meta is not None and kind in KIND_LABELS:
        meta["notes"] = notes_for(kind, meta)
    return meta

