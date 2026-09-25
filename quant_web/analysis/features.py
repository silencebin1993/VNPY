"""
量价/主力分析用的逐日特征（全市场一次算完，也可以只算一只股票）。

输入：formula.engine.prepare_frame 的结果（按 (code, date) 排序；open/high/low/close 为前复权价，
raw_close 为真实收盘价，volume 股、amount 元、turn %、adj_factor）。可选 chips（与行对齐的筹码统计）。
所有特征只用当天及以前的数据。列名与含义见 FEATURE_DOC。
"""
from __future__ import annotations

import polars as pl

from ..indicators import funcs as F
from ..indicators import ta
from ..indicators.funcs import Ctx

FEATURE_DOC: dict[str, str] = {
    "ma5": "5日均线", "ma10": "10日均线", "ma20": "20日均线", "ma60": "60日均线", "ma120": "120日均线", "ma250": "250日均线",
    "mav5": "5日均量", "mav20": "20日均量", "vr20": "今天成交量 ÷ 20日均量",
    "pos250": "在一年高低区间中的位置（0=最低，1=最高）", "range30": "30天振幅（最高/最低−1）",
    "atr_ratio50": "现在的平均波幅 ÷ 50天前的平均波幅（<1 说明波动在收缩）",
    "udr20": "20天阳量÷阴量", "udr10_avg": "10天里上涨日平均量÷下跌日平均量",
    "obv_slope": "OBV 20日斜率（以20日均量为单位）", "price_slope": "股价20日斜率（每天百分比）",
    "big_drop20": "20天内放量大跌（跌5%以上且量>1.5倍均量）的天数",
    "rally60": "20日最高收盘 ÷ 60日最低价 − 1（前期拉升幅度）", "dd20": "收盘价相对20日最高收盘的回撤",
    "rise_low120": "收盘价相对120日最低价的涨幅", "bias20": "收盘价相对20日线的偏离",
    "black_vol5": "5天内出现放量长阴", "low_shadow5": "5天内出现长下影线",
    "breakout5": "5天内出现放量突破60日高点", "breakout20": "20天内出现放量突破60日高点",
    "newhigh5": "5天内收盘创60日新高", "stall15": "15天内出现高位放量滞涨", "pos_max20": "最近20天在一年区间中的最高位置", "ma_bull": "均线多头排列且20日线向上",
    "stall10": "10天内出现高位放量滞涨", "div10": "10天内出现量价顶背离", "brk10": "10天内出现带量（>1.2倍60日均量）跌破20日线",
    "top10": "10天内出现120日天量阴线", "bear_ma": "均线空头排列", "lower_lows": "低点不断下移",
    "rps120": "120日涨幅在全市场的百分位（0~100）",
    "winner": "获利比例", "conc90": "90%筹码集中度", "conc_chg20": "集中度相对20天前的变化（倍数−1）",
    "cost_chg5": "平均成本相对5天前的变化", "ret5": "5日涨幅", "ret20": "20日涨幅", "ret60": "60日涨幅",
    "atr14": "14日平均波幅（前复权价）", "atr_pct": "平均波幅占股价", "vol_shrink": "5日均量÷20日均量", "dist_ma20": "离20日线的距离（绝对值）", "turn20": "20日平均换手率（%）", "cost_chg20": "平均成本相对20天前的变化", "avg_cost_q": "平均成本（前复权）",
}


def compute(frame: pl.DataFrame, chips: pl.DataFrame | None = None, rps: bool = True) -> pl.DataFrame:
    """返回 frame 加上 FEATURE_DOC 里的列"""
    if frame.is_empty():
        return frame
    df: pl.DataFrame = frame
    ctx = Ctx(df["code"])
    OP, H, L, C = (df[c].cast(pl.Float64) for c in ("open", "high", "low", "close"))
    V: pl.Series = df["volume"].cast(pl.Float64)
    ref = lambda x, n: F.ref(x, n, ctx)                          # noqa: E731
    ma = lambda x, n: F.ma(x, n, ctx)                            # noqa: E731
    hhv = lambda x, n: F.hhv(x, n, ctx)                          # noqa: E731
    llv = lambda x, n: F.llv(x, n, ctx)                          # noqa: E731
    exist = lambda x, n: F.exist(x, n, ctx)                      # noqa: E731
    sdiv = F.safe_div
    out: dict[str, pl.Series] = {}
    for n in (5, 10, 20, 60, 120, 250):
        out[f"ma{n}"] = ma(C, n)
    mav5, mav20 = ma(V, 5), ma(V, 20)
    out["mav5"], out["mav20"] = mav5, mav20
    out["vr20"] = sdiv(V, mav20)
    lo250, hi250 = llv(L, 250), hhv(H, 250)
    out["pos250"] = sdiv(C - lo250, hi250 - lo250)
    out["range30"] = sdiv(hhv(H, 30), llv(L, 30)) - 1
    ohlcv = {"open": OP, "high": H, "low": L, "close": C, "volume": V}
    atr = {k: s for k, _, s, _ in ta.atr(ohlcv, ctx)}["atr"]
    out["atr_ratio50"] = sdiv(atr, ref(atr, 50))
    pc = ref(C, 1)
    up, dn = (C > pc).fill_null(False), (C < pc).fill_null(False)
    upv, dnv = F.sum_(F.if_(up, V, 0.0, ctx), 20, ctx), F.sum_(F.if_(dn, V, 0.0, ctx), 20, ctx)
    out["udr20"] = sdiv(upv, dnv)
    upv10, dnv10 = F.sum_(F.if_(up, V, 0.0, ctx), 10, ctx), F.sum_(F.if_(dn, V, 0.0, ctx), 10, ctx)
    out["udr10_avg"] = sdiv(sdiv(upv10, F.count(up, 10, ctx)), sdiv(dnv10, F.count(dn, 10, ctx)))
    obv = F.sum_(F.if_(up, V, F.if_(dn, -V, 0.0, ctx), ctx), 0, ctx)
    out["obv_slope"] = sdiv(F.slope(obv, 20, ctx), mav20)
    out["price_slope"] = sdiv(F.slope(C, 20, ctx), C) * 100
    out["big_drop20"] = F.count((C < pc * 0.95) & (V > mav20 * 1.5), 20, ctx)
    hc20 = hhv(C, 20)
    out["rally60"] = sdiv(hc20, llv(L, 60)) - 1
    out["dd20"] = sdiv(C, hc20) - 1
    out["rise_low120"] = sdiv(C, llv(L, 120)) - 1
    out["bias20"] = sdiv(C, out["ma20"]) - 1
    out["black_vol5"] = exist((C < OP * 0.96) & (V > mav20 * 1.8), 5)
    rng = H - L
    lower_shadow = sdiv(F.min_(OP, C, ctx) - L, rng)
    out["low_shadow5"] = exist((lower_shadow > 0.5) & (rng > 0), 5)
    brk_up = ((C > ref(hhv(H, 60), 1)) & (V > mav20 * 1.5)).fill_null(False)
    out["breakout5"] = exist(brk_up, 5)
    out["breakout20"] = exist(brk_up, 20)
    out["newhigh5"] = exist((C >= hhv(C, 60)).fill_null(False), 5)
    ma5, ma10, ma20, ma60 = out["ma5"], out["ma10"], out["ma20"], out["ma60"]
    out["ma_bull"] = ((ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60) & (ma20 > ref(ma20, 1))).fill_null(False)
    upper_shadow = sdiv(H - F.max_(OP, C, ctx), rng)
    mav60 = ma(V, 60)
    # 放量用 60 日均量衡量：连续几天巨量后 20 日均量被抬高，只和 20 日均量比会漏掉后面的滞涨日
    stall = (out["pos250"] > 0.75) & (V > mav60 * 1.8) & ((sdiv(C, pc) - 1).abs() < 0.02) & (upper_shadow > 0.4)
    out["stall10"] = exist(stall.fill_null(False), 10)
    out["stall15"] = exist(stall.fill_null(False), 15)
    macd = {k: s for k, _, s, _ in ta.macd(ohlcv, ctx)}
    dif = macd["dif"]
    div = (H >= hhv(H, 60)) & (mav5 < ref(hhv(mav5, 60), 5) * 0.8) & (dif < ref(hhv(dif, 60), 5))
    out["div10"] = exist(div.fill_null(False), 10)
    brk = (C < ma20) & (pc >= ref(ma20, 1)) & (V > mav60 * 1.2)          # 同样用 60 日均量衡量"带量"
    out["brk10"] = exist(brk.fill_null(False), 10)
    out["top10"] = exist(((V >= hhv(V, 120)) & (C < OP)).fill_null(False), 10)
    out["bear_ma"] = ((ma5 < ma10) & (ma10 < ma20) & (ma20 < ma60)).fill_null(False)
    lo10 = llv(L, 10)
    out["lower_lows"] = (lo10 < ref(lo10, 10)).fill_null(False)
    out["ma60_slope5"] = sdiv(ma60, ref(ma60, 5)) - 1
    out["ma20_slope1"] = sdiv(ma20, ref(ma20, 1)) - 1
    out["ret120"] = sdiv(C, ref(C, 120)) - 1
    out["ret5"] = sdiv(C, ref(C, 5)) - 1
    out["ret20"] = sdiv(C, ref(C, 20)) - 1
    out["ret60"] = sdiv(C, ref(C, 60)) - 1
    out["atr14"] = atr
    out["atr_pct"] = sdiv(atr, C)
    out["vol_shrink"] = sdiv(mav5, mav20)
    out["dist_ma20"] = out["bias20"].abs()
    turn: pl.Series = df["turn"].cast(pl.Float64) if "turn" in df.columns else pl.Series([None] * df.height, dtype=pl.Float64)
    out["turn20"] = ma(turn, 20)
    out["rev20"] = out["ret20"]          # 打分用：同一个数，方向相反（越跌得多越靠前）
    out["pos_max20"] = hhv(out["pos250"], 20)
    res: pl.DataFrame = df.with_columns([s.alias(k) for k, s in out.items()])
    if rps:
        res = res.with_columns(
            (pl.col("ret120").rank("average").over("date") / pl.col("ret120").count().over("date") * 100)
            .alias("rps120"))
    else:
        res = res.with_columns(pl.lit(None, dtype=pl.Float64).alias("rps120"))
    if chips is not None and chips.height == res.height:
        fac: pl.Series = res["adj_factor"] if "adj_factor" in res.columns else pl.Series([1.0] * res.height)
        winner, conc90 = chips["winner"].cast(pl.Float64), chips["conc90"].cast(pl.Float64)
        cost: pl.Series = chips["avg_cost"].cast(pl.Float64) * fac                 # 换算到前复权口径再比较
        res = res.with_columns(
            winner.alias("winner"), conc90.alias("conc90"), (sdiv(conc90, ref(conc90, 20)) - 1).alias("conc_chg20"),
            cost.alias("avg_cost_q"), (sdiv(cost, ref(cost, 5)) - 1).alias("cost_chg5"),
            (sdiv(cost, ref(cost, 20)) - 1).alias("cost_chg20"),
        )
    else:
        res = res.with_columns([pl.lit(None, dtype=pl.Float64).alias(k)
                                for k in ("winner", "conc90", "conc_chg20", "avg_cost_q", "cost_chg5", "cost_chg20")])
    return res
