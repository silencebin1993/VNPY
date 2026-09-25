"""
样本外评估（输入是滚动训练得到的预测：每天用"之前的数据训练的模型"打分，从没用过未来）：
- IC / RankIC：每天"模型分数"和"之后的真实净收益"的相关系数（普通 / 排名），再看平均、稳定性（ICIR）和 t 值；
- 分五组：每天按分数从低到高分成 5 组，看每组之后比同日平均多赚多少——好模型应该一组比一组高；
- 前 K 名组合：每隔 N 天（和持有天数一样，不重叠）买分数最高的 K 只，和同一天范围内随便买的平均比；
- 全部分"选择期"（2025-07 之前）和"留出期"（之后）各算一遍，每段从新账户开始；结论以留出期为准。
t 值 = min(普通 t, Newey-West t)（样本有重叠时后者更保守），小于 2 标"还不够可信"。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from ..predict import stats
from ..predict.backtest import HOLDOUT_START

MIN_PER_DAY: int = 10               # 一天至少这么多只股票有标签，才算这天的 IC
YEAR_DAYS: int = 243


def _t(x: np.ndarray, lag: int) -> dict:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return {"t": None, "daily_t": None, "nw_t": None}
    dt = stats.daily_t(x)
    nwt = stats.nw_t(x, lag=max(1, lag))
    t = min(dt, nwt) if dt is not None and nwt is not None else (dt if nwt is None else nwt)
    return {"t": stats.rounded(t), "daily_t": stats.rounded(dt), "nw_t": stats.rounded(nwt)}


def _curve_stats(rets: np.ndarray, hold: int) -> dict:
    """按期收益（不重叠）→ 年化、最大回撤（从 1 元开始）"""
    if len(rets) == 0:
        return {"cagr": None, "max_dd": None, "total": None}
    eq = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    dd = float(np.min(eq / peak - 1))
    years = len(rets) * hold / YEAR_DAYS
    cagr = float(eq[-1] ** (1 / years) - 1) if years > 0 and eq[-1] > 0 else None
    return {"cagr": cagr, "max_dd": dd, "total": float(eq[-1] - 1)}


def daily_ic(lab: pl.DataFrame) -> pl.DataFrame:
    """每天的 IC、RankIC、股票数（lab：有标签的行，列 date, pred, net）"""
    return (lab.group_by("date").agg(pl.corr("pred", "net").alias("ic"),
                                     pl.corr("pred", "net", method="spearman").alias("rank_ic"), pl.len().alias("n"))
            .filter(pl.col("n") >= MIN_PER_DAY).sort("date"))


def quintiles(lab: pl.DataFrame) -> pl.DataFrame:
    """每天按分数分 5 组（0 = 最低），每组的平均超额。列 date, q0..q4"""
    q = lab.with_columns(((pl.col("pred").rank("ordinal").over("date") - 1) * 5 // pl.len().over("date")).cast(pl.Int8).alias("q"))
    g = q.group_by(["date", "q"]).agg(pl.col("excess").mean().alias("ex"))
    wide = g.pivot(on="q", index="date", values="ex", sort_columns=True).sort("date")
    rename = {c: f"q{c}" for c in wide.columns if c != "date"}
    wide = wide.rename(rename)
    for i in range(5):
        if f"q{i}" not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(f"q{i}"))
    return wide.select(["date", *[f"q{i}" for i in range(5)]])


def topk_periods(oos: pl.DataFrame, hold: int, top_k: int) -> pl.DataFrame:
    """每隔 hold 个交易日调仓：分数最高的 top_k 只（第二天开盘买不进的不算，按买进的平均）vs 同日范围内全部买得进的平均"""
    days: list[date] = sorted(oos["date"].unique().to_list())
    reb: list[date] = days[::max(hold, 1)]
    sub = oos.filter(pl.col("date").is_in(reb))
    ranked = sub.with_columns(pl.col("pred").rank("ordinal", descending=True).over("date").alias("_r"))
    picks = (ranked.filter(pl.col("_r") <= top_k).group_by("date")
             .agg(pl.col("net").mean().alias("port"), pl.col("net").is_not_null().sum().alias("filled"),
                  pl.col("code").sort_by("_r").alias("codes")))
    base = sub.filter(pl.col("net").is_not_null()).group_by("date").agg(pl.col("net").mean().alias("base"), pl.len().alias("universe"))
    out = picks.join(base, on="date", how="inner").filter(pl.col("port").is_not_null()).sort("date")
    return out.with_columns((pl.col("port") - pl.col("base")).alias("excess"))


def _segment(ic: pl.DataFrame, qd: pl.DataFrame, per: pl.DataFrame, hold: int) -> dict:
    out: dict = {"days": ic.height}
    if ic.height:
        for col in ("ic", "rank_ic"):
            x = ic[col].drop_nulls().to_numpy()
            mean = float(np.mean(x)) if len(x) else None
            sd = float(np.std(x, ddof=1)) if len(x) > 1 else None
            out[col] = {"mean": mean, "std": sd, "ir": (mean / sd) if mean is not None and sd else None,
                        "pos": float(np.mean(x > 0)) if len(x) else None, **_t(x, hold)}
        out["start"], out["end"] = str(ic["date"].min()), str(ic["date"].max())
    if qd.height:
        means = [float(qd[f"q{i}"].drop_nulls().mean() or 0.0) for i in range(5)]
        ls = (qd["q4"] - qd["q0"]).drop_nulls().to_numpy()
        out["quintiles"] = means
        out["long_short"] = {"mean": float(np.mean(ls)) if len(ls) else None, **_t(ls, hold)}
        mono = sum(1 for a, b in zip(means, means[1:], strict=False) if b > a)
        out["monotonic"] = mono                                  # 5 组里相邻两组"后一组更高"的次数（满分 4）
    if per.height:
        port = per["port"].to_numpy()
        base = per["base"].to_numpy()
        ex = per["excess"].to_numpy()
        pc = _curve_stats(port, hold)
        bc = _curve_stats(base, hold)
        out["topk"] = {"periods": per.height, "port": float(np.mean(port)), "base": float(np.mean(base)),
                       "excess": float(np.mean(ex)), "win": float(np.mean(ex > 0)), **_t(ex, 2),
                       "cagr": pc["cagr"], "max_dd": pc["max_dd"], "base_cagr": bc["cagr"], "base_max_dd": bc["max_dd"],
                       "filled": float(per["filled"].mean())}
    return out


def verdict(seg: dict, hold: int) -> dict:
    ho: dict = seg.get("holdout") or {}
    sel: dict = seg.get("selection") or {}
    tk: dict = ho.get("topk") or {}
    ric: dict = ho.get("rank_ic") or {}
    if not tk.get("periods") or tk["periods"] < 6:
        return {"key": "short", "credible": False,
                "text": "留出期（2025-07 之后）的调仓次数太少，还不能下结论。先看选择期的结果，但不要据此加仓。"}
    t = tk.get("t")
    ex = tk.get("excess") or 0.0
    sel_ex = (sel.get("topk") or {}).get("excess")
    if t is not None and t >= 2 and ex > 0 and (ric.get("mean") or 0) > 0 and (sel_ex is None or sel_ex > 0):
        return {"key": "good", "credible": True,
                "text": f"留出期（样本外）每期比同日随便买多赚 {ex * 100:.2f}%，t 值 {t:.1f}，排序能力（RankIC）为正。"
                        "有一定可信度——但试的配置越多，越可能是碰巧，仍不保证以后有效。"}
    if ex > 0:
        return {"key": "weak", "credible": False,
                "text": f"留出期每期比同日随便买多赚 {ex * 100:.2f}%，但 t 值 {t if t is not None else '—'}，还不够可信"
                        "（可能是运气）。不建议据此买股票。"}
    return {"key": "bad", "credible": False,
            "text": f"留出期每期比同日随便买还少赚 {abs(ex) * 100:.2f}%：这个模型在最近的行情里没有用。"}


def evaluate(oos: pl.DataFrame, hold: int, top_k: int) -> dict:
    """oos：date, code, pred, net（开盘买不进为空）, excess"""
    df = oos.filter(pl.col("pred").is_not_null() & pl.col("pred").is_finite())
    lab = df.filter(pl.col("net").is_not_null())
    ic = daily_ic(lab)
    qd = quintiles(lab)
    per = topk_periods(df, hold, top_k)
    segs: dict = {}
    for key, cond in (("selection", pl.col("date") < HOLDOUT_START), ("holdout", pl.col("date") >= HOLDOUT_START),
                      ("all", pl.lit(True))):
        segs[key] = _segment(ic.filter(cond), qd.filter(cond), per.filter(cond), hold)
    eq_p = np.cumprod(1 + per["port"].to_numpy()) if per.height else np.array([])
    eq_b = np.cumprod(1 + per["base"].to_numpy()) if per.height else np.array([])
    curves: dict = {
        "ic_dates": [str(d) for d in ic["date"].to_list()],
        "cum_rank_ic": np.round(np.nancumsum(ic["rank_ic"].fill_null(0).to_numpy()), 4).tolist() if ic.height else [],
        "topk_dates": [str(d) for d in per["date"].to_list()],
        "topk_port": np.round(eq_p - 1, 4).tolist(), "topk_base": np.round(eq_b - 1, 4).tolist(),
    }
    return {"segments": segs, "verdict": verdict(segs, hold), "curves": curves, "hold": hold, "top_k": top_k,
            "holdout_start": str(HOLDOUT_START), "stocks_per_day": float(lab.group_by("date").len()["len"].mean() or 0) if lab.height else 0.0}
