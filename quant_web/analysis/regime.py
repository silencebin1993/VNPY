"""
大盘环境：强势 / 震荡 / 弱势 + 建议总仓位上限（默认 80% / 50% / 20%，设置 risk.regime_caps 可改）。

打分（每天，只用当天及以前的数据；规则事先定好，没有调参）：
- 指数趋势：在 60 日线上且 60 日线向上 +2；在 60 日线下且向下 −2；站上 20 日线 +1，跌破 −1
  （指数优先用上证指数的日线存档；没有时用全市场等权指数）；
- 市场宽度：站上 20 日线的股票 ≥60% +1、≤35% −1；站上 60 日线 ≥55% +1、≤30% −1；
- 新高新低：60 日新高家数是新低的 1.5 倍以上 +1，反过来 −1；
- 成交额：比 20 日均额放大 15% 且指数在 20 日线上 +1；缩小 20% 且在 20 日线下 −1。
总分 ≥3 为强势，≤−2 为弱势，其余为震荡。
历史验证：每种环境之后 20 个交易日全市场等权指数的平均涨跌（分选择期 / 留出期）。
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import polars as pl

from ..predict import stats
from ..predict.backtest import HOLDOUT_START

REGIMES: dict[str, dict] = {
    "strong": {"label": "强势", "tone": "good", "advice": "多数股票在涨，可以按计划操作；注意历史上强势之后并不一定涨得更多，不要因此加大仓位。"},
    "neutral": {"label": "震荡", "tone": "neutral", "advice": "方向不明，按你的计划操作。"},
    "weak": {"label": "弱势", "tone": "bad", "advice": "大多数股票在跌、波动变大。仓位上限只是控制波动的经验规则，不是涨跌预测；"
                                                       "“量化选股”的周组合按大盘环境减仓，回测里反而更差（弱势周的收益并不低），它按固定仓位执行。"},
}
FWD: int = 20


def market_table(frame: pl.DataFrame) -> pl.DataFrame:
    """由公式表（前复权、按 code/date 排序）算每天的市场宽度和等权指数"""
    df = frame.select("code", "date", "close", "raw_close", "preclose", "amount").with_columns(
        (pl.col("raw_close") / pl.col("preclose") - 1).alias("ret"),
        pl.col("close").rolling_mean(20, min_samples=20).over("code").alias("m20"),
        pl.col("close").rolling_mean(60, min_samples=60).over("code").alias("m60"),
        pl.col("close").rolling_max(60, min_samples=60).over("code").alias("h60"),
        pl.col("close").rolling_min(60, min_samples=60).over("code").alias("l60"),
    )
    daily = df.group_by("date").agg(
        pl.col("ret").filter(pl.col("ret").abs() < 0.31).mean().alias("ew_ret"),
        (pl.col("close") > pl.col("m20")).filter(pl.col("m20").is_not_null()).mean().alias("above20"),
        (pl.col("close") > pl.col("m60")).filter(pl.col("m60").is_not_null()).mean().alias("above60"),
        (pl.col("close") >= pl.col("h60")).sum().alias("new_high"),
        (pl.col("close") <= pl.col("l60")).sum().alias("new_low"),
        (pl.col("raw_close") > pl.col("preclose")).sum().alias("n_up"),
        (pl.col("raw_close") < pl.col("preclose")).sum().alias("n_down"),
        pl.col("amount").sum().alias("amount"),
    ).sort("date")
    return daily.with_columns((1 + pl.col("ew_ret").fill_null(0)).cum_prod().alias("ew_index"))


def score_table(daily: pl.DataFrame, index: pl.DataFrame | None = None) -> pl.DataFrame:
    """每天的分项得分和环境"""
    df = daily
    if index is not None and index.height:
        df = df.join(index.select("date", pl.col("close").alias("idx")), on="date", how="left").with_columns(
            pl.col("idx").forward_fill())
    else:
        df = df.with_columns(pl.col("ew_index").alias("idx"))
    df = df.with_columns(
        pl.col("idx").rolling_mean(20, min_samples=20).alias("idx_m20"),
        pl.col("idx").rolling_mean(60, min_samples=60).alias("idx_m60"),
        pl.col("amount").rolling_mean(20, min_samples=10).alias("amt_m20"),
    ).with_columns((pl.col("idx_m60") / pl.col("idx_m60").shift(5) - 1).alias("m60_slope"))
    trend = (pl.when((pl.col("idx") > pl.col("idx_m60")) & (pl.col("m60_slope") > 0)).then(2)
             .when((pl.col("idx") < pl.col("idx_m60")) & (pl.col("m60_slope") < 0)).then(-2).otherwise(0))
    short = pl.when(pl.col("idx") > pl.col("idx_m20")).then(1).when(pl.col("idx") < pl.col("idx_m20")).then(-1).otherwise(0)
    b20 = pl.when(pl.col("above20") >= 0.6).then(1).when(pl.col("above20") <= 0.35).then(-1).otherwise(0)
    b60 = pl.when(pl.col("above60") >= 0.55).then(1).when(pl.col("above60") <= 0.30).then(-1).otherwise(0)
    hl = (pl.when(pl.col("new_high") > pl.col("new_low") * 1.5).then(1)
          .when(pl.col("new_low") > pl.col("new_high") * 1.5).then(-1).otherwise(0))
    ratio = pl.col("amount") / pl.col("amt_m20")
    amt = (pl.when((ratio >= 1.15) & (pl.col("idx") > pl.col("idx_m20"))).then(1)
           .when((ratio <= 0.8) & (pl.col("idx") < pl.col("idx_m20"))).then(-1).otherwise(0))
    df = df.with_columns(trend.alias("p_trend"), short.alias("p_short"), b20.alias("p_b20"), b60.alias("p_b60"),
                         hl.alias("p_hl"), amt.alias("p_amt"), ratio.alias("amt_ratio"))
    df = df.with_columns((pl.col("p_trend") + pl.col("p_short") + pl.col("p_b20") + pl.col("p_b60") + pl.col("p_hl")
                          + pl.col("p_amt")).alias("score"))
    ready = pl.col("idx_m60").is_not_null()
    return df.with_columns(pl.when(~ready).then(None).when(pl.col("score") >= 3).then(pl.lit("strong"))
                           .when(pl.col("score") <= -2).then(pl.lit("weak")).otherwise(pl.lit("neutral")).alias("regime"))


def history_stats(sc: pl.DataFrame) -> dict:
    """每种环境之后 FWD 个交易日等权指数的涨跌（按日重叠，用 Newey-West t）"""
    df = sc.with_columns((pl.col("ew_index").shift(-FWD) / pl.col("ew_index") - 1).alias("fwd")).drop_nulls(["regime", "fwd"])
    out: dict = {}
    for rg in REGIMES:
        seg: dict = {}
        for name, part in (("selection", df.filter(pl.col("date") < HOLDOUT_START)), ("holdout", df.filter(pl.col("date") >= HOLDOUT_START)),
                           ("all", df)):
            p = part.filter(pl.col("regime") == rg)
            if p.height < 5:
                seg[name] = {"n": p.height}
                continue
            x = p["fwd"].to_numpy()
            seg[name] = {"n": p.height, "mean": float(x.mean()), "win": float((x > 0).mean()),
                         "t": stats.rounded(stats.nw_t(x, lag=FWD)),
                         "p10": float(np.quantile(x, 0.1)), "loss5": float((x < -0.05).mean()),
                         "std": float(x.std(ddof=1))}
        out[rg] = seg
    return out


def compute(frame: pl.DataFrame, index: pl.DataFrame | None = None, caps: dict | None = None,
            index_name: str | None = None, progress: Callable[[float, str], None] | None = None) -> dict:
    """由公式表直接算（测试和一次性计算用）；日常由 analysis.market 读缓存的宽度表再调用 summarize"""
    return summarize(score_table(market_table(frame), index), caps, index_name)


def summarize(sc: pl.DataFrame, caps: dict | None = None, index_name: str | None = None) -> dict:
    """score_table 的结果 → 页面要的结构（最新环境、分项、最近 250 天、历史统计）"""
    caps = caps or {"strong": 0.7, "neutral": 0.7, "weak": 0.4}
    last = sc.drop_nulls("regime").tail(1)
    if last.is_empty():
        raise ValueError("日线数据太少（至少需要 60 个交易日），暂时判断不了大盘环境")
    r = last.row(0, named=True)
    rg: str = r["regime"]
    pct = lambda v: f"{v * 100:.0f}%" if v is not None else "—"         # noqa: E731
    comps: list[dict] = [
        {"name": "指数趋势（60 日线）", "points": r["p_trend"],
         "text": f"{index_name or '全市场等权指数'}{'在' if r['idx'] > r['idx_m60'] else '跌破'} 60 日线，60 日线{'向上' if (r['m60_slope'] or 0) > 0 else '向下'}"},
        {"name": "短期趋势（20 日线）", "points": r["p_short"], "text": f"{'站上' if r['idx'] > r['idx_m20'] else '跌破'} 20 日线"},
        {"name": "站上 20 日线的股票", "points": r["p_b20"], "text": pct(r["above20"])},
        {"name": "站上 60 日线的股票", "points": r["p_b60"], "text": pct(r["above60"])},
        {"name": "60 日新高 / 新低家数", "points": r["p_hl"], "text": f"{r['new_high']} / {r['new_low']}"},
        {"name": "成交额（比 20 日平均）", "points": r["p_amt"], "text": f"{r['amt_ratio']:.2f} 倍" if r["amt_ratio"] else "—"},
    ]
    hist = sc.drop_nulls("regime").tail(250).select("date", "regime", "score", "idx", "above20", "above60")
    st: dict = history_stats(sc)
    return {
        "date": str(r["date"]), "regime": rg, **REGIMES[rg], "cap": caps.get(rg), "score": r["score"],
        "components": comps, "index_name": index_name or "全市场等权指数",
        "history": [{**h, "date": str(h["date"])} for h in hist.to_dicts()],
        "stats": st, "insights": insights(st), "fwd_days": FWD, "holdout_start": str(HOLDOUT_START),
        "breadth": {"above20": r["above20"], "above60": r["above60"], "n_up": r["n_up"], "n_down": r["n_down"]},
        "note": "大盘环境只用来控制“敢用多少仓位”（风险），不用来预测涨跌；规则是事先定好的，历史表现见下方统计。",
    }


def insights(st: dict) -> list[str]:
    """由历史统计自动生成的白话结论（数据说什么就写什么）"""
    out: list[str] = []
    m = {rg: (st.get(rg, {}).get("all") or {}) for rg in REGIMES}
    if all(m[rg].get("n", 0) >= 20 for rg in REGIMES):
        means = {rg: m[rg]["mean"] for rg in REGIMES}
        if means["strong"] <= max(means["neutral"], means["weak"]):
            out.append(f"历史上“强势”之后 {FWD} 天平均涨 {means['strong'] * 100:.1f}%，并不比其他环境多"
                       f"（震荡 {means['neutral'] * 100:.1f}%、弱势 {means['weak'] * 100:.1f}%）："
                       "这个判断不能用来预测涨跌，不要因为“行情好”就加大仓位。")
        else:
            out.append(f"历史上“强势”之后 {FWD} 天平均涨 {means['strong'] * 100:.1f}%，比弱势（{means['weak'] * 100:.1f}%）多。")
        stds = {rg: m[rg].get("std") for rg in REGIMES}
        if stds["weak"] and stds["neutral"] and stds["weak"] > stds["neutral"] * 1.15:
            out.append(f"“弱势”之后的波动明显更大（{FWD} 天涨跌幅标准差 {stds['weak'] * 100:.1f}%，震荡时 {stds['neutral'] * 100:.1f}%），"
                       "上下都可能很大：这是弱势时降低仓位的主要理由（少受大起大落的伤害），代价是可能错过反弹。")
        worst = {rg: m[rg].get("loss5") for rg in REGIMES}
        if all(v is not None for v in worst.values()):
            out.append("之后 20 天跌超 5% 的概率：" + "、".join(f"{REGIMES[rg]['label']} {worst[rg] * 100:.0f}%" for rg in REGIMES) + "。")
    return out
