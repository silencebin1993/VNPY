"""
指标目录：每个指标的中文名、分类、画在主图还是副图、参数（默认值和范围）、参考线，以及给新手看的白话说明
（是什么 / 怎么用 / 常见陷阱）。页面的指标下拉框和 "?" 提示都读这里。

compute(bars, ids, params) 在一只股票的 K 线上计算指标，返回可直接给前端画图的结构。
"""
from __future__ import annotations

from typing import Any

import polars as pl

from . import ta
from .funcs import Ctx


# params：[(键, 中文名, 默认值, 最小值, 最大值)]
CATALOG: dict[str, dict] = {
    # ------------------------------------------------ 主图（叠加在 K 线上）
    "ma": {
        "name": "均线 MA", "cat": "趋势", "pane": "main", "default": True,
        "params": [("n1", "周期1", 5, 1, 250), ("n2", "周期2", 10, 1, 250), ("n3", "周期3", 20, 1, 250), ("n4", "周期4", 60, 1, 500)],
        "explain": "最近 N 天收盘价的平均值。股价在均线上方、均线向上，说明这段时间买入的人大多赚钱，趋势向上。",
        "usage": "短期均线在长期均线上方依次排列（多头排列）是上升趋势；股价回踩 20 日线不破常被看作洗盘后的买点。",
        "trap": "均线是“滞后”的，震荡行情里股价上下穿越均线很频繁，信号大多是假的。",
    },
    "expma": {
        "name": "指数均线 EXPMA", "cat": "趋势", "pane": "main",
        "params": [("n1", "短周期", 12, 1, 250), ("n2", "长周期", 50, 1, 250)],
        "explain": "越近的价格权重越大的均线，比普通均线反应更快。",
        "usage": "短线上穿长线偏多，下穿偏空。",
        "trap": "反应快也意味着假信号更多。",
    },
    "boll": {
        "name": "布林带 BOLL", "cat": "波动", "pane": "main",
        "params": [("n", "周期", 20, 5, 120), ("k", "倍数", 2.0, 1.0, 3.0)],
        "explain": "中轨是 20 日均线，上下轨是中轨加减 2 倍标准差，大约 95% 的时间股价在上下轨之间。",
        "usage": "带子收窄说明波动变小、即将选方向；放量突破上轨并沿上轨走是强势；跌破中轨转弱。",
        "trap": "碰到上轨不等于要跌，强势股会沿着上轨一直涨；碰到下轨也不等于要涨。",
    },
    "ene": {
        "name": "轨道线 ENE", "cat": "波动", "pane": "main",
        "params": [("n", "周期", 25, 5, 120), ("m1", "上轨%", 6, 1, 30), ("m2", "下轨%", 6, 1, 30)],
        "explain": "在均线上下各留一个固定百分比的通道。",
        "usage": "价格在通道里来回时，靠近下轨偏低、靠近上轨偏高；突破通道说明走出了趋势。",
        "trap": "趋势行情里通道会被持续突破，不能机械地“下轨买上轨卖”。",
    },
    "sar": {
        "name": "抛物线 SAR", "cat": "趋势", "pane": "main", "params": [],
        "explain": "图上的一串点：点在价格下方表示上升趋势，翻到价格上方表示可能转跌。",
        "usage": "常用来设移动止损：点在下方时，跌破点就卖。",
        "trap": "震荡行情里会反复翻转，频繁止损。",
    },
    # ------------------------------------------------ 副图
    "vol": {
        "name": "成交量 VOL", "cat": "量能", "pane": "sub", "default": True,
        "params": [("m1", "均量1", 5, 1, 120), ("m2", "均量2", 10, 1, 120)],
        "explain": "每天成交的股数，红柱是上涨日、绿柱是下跌日，两条线是 5 日、10 日平均成交量。",
        "usage": "上涨放量、回调缩量是健康走势；低位放量上涨可能有资金进场；高位放量不涨要警惕出货。",
        "trap": "放量本身不分好坏，一定要结合价格位置和涨跌看。",
    },
    "macd": {
        "name": "MACD", "cat": "趋势", "pane": "sub", "default": True,
        "params": [("short", "快线", 12, 2, 60), ("long", "慢线", 26, 5, 120), ("mid", "信号线", 9, 2, 60)],
        "refs": [0],
        "explain": "快慢两条均线的差（DIF）和它的平均（DEA），红绿柱是两者之差×2。",
        "usage": "DIF 上穿 DEA 叫金叉（偏多），下穿叫死叉（偏空）；在零轴上方的金叉更可靠；价格创新高而 MACD 没创新高叫顶背离，是减仓信号。",
        "trap": "震荡行情里金叉死叉非常频繁，单独使用胜率不高。",
    },
    "kdj": {
        "name": "KDJ", "cat": "摆动", "pane": "sub",
        "params": [("n", "周期", 9, 3, 60), ("m1", "K 平滑", 3, 1, 20), ("m2", "D 平滑", 3, 1, 20)],
        "refs": [20, 80],
        "explain": "看收盘价在最近 9 天高低区间里的位置。20 以下算超卖（跌多了），80 以上算超买（涨多了）。",
        "usage": "低位（20 以下）K 上穿 D 常被当作反弹信号；高位死叉提示回调。",
        "trap": "强势股会长时间“超买”还继续涨，弱势股会长时间“超卖”还继续跌——不能看到超买就卖、超卖就买。",
    },
    "rsi": {
        "name": "RSI", "cat": "摆动", "pane": "sub",
        "params": [("n1", "周期1", 6, 2, 60), ("n2", "周期2", 12, 2, 60), ("n3", "周期3", 24, 2, 120)],
        "refs": [30, 70],
        "explain": "最近 N 天里上涨的力量占全部波动的比例（0–100）。70 以上偏热，30 以下偏冷。",
        "usage": "看强弱：RSI 长期在 50 以上是强势；与价格背离时提示趋势可能变化。",
        "trap": "和 KDJ 一样，强势股的 RSI 会长期处在高位。",
    },
    "wr": {
        "name": "威廉 WR", "cat": "摆动", "pane": "sub",
        "params": [("n1", "周期1", 10, 2, 60), ("n2", "周期2", 6, 2, 60)],
        "refs": [20, 80],
        "explain": "收盘价在最近区间里的位置（通达信口径：数值越大越接近区间低点，80 以上算超卖）。",
        "usage": "和 KDJ 配合看短期超买超卖。",
        "trap": "方向和 KDJ 相反，容易看反。",
    },
    "cci": {
        "name": "CCI", "cat": "摆动", "pane": "sub", "params": [("n", "周期", 14, 5, 60)],
        "refs": [-100, 100],
        "explain": "价格偏离平均水平的程度。+100 以上说明走强，-100 以下说明走弱。",
        "usage": "从下向上突破 +100 常被看作进入强势。",
        "trap": "波动大的股票会频繁越过 ±100。",
    },
    "dmi": {
        "name": "趋向 DMI", "cat": "趋势", "pane": "sub", "params": [("n", "周期", 14, 5, 60), ("m", "平滑", 6, 2, 30)],
        "explain": "PDI 是上涨力量，MDI 是下跌力量，ADX 是趋势强度（不分涨跌）。",
        "usage": "PDI 在 MDI 上方且 ADX 向上，说明上涨趋势在加强；ADX 很低说明是震荡市。",
        "trap": "ADX 高只说明趋势强，不说明是涨还是跌。",
    },
    "bias": {
        "name": "乖离率 BIAS", "cat": "摆动", "pane": "sub",
        "params": [("n1", "周期1", 6, 2, 60), ("n2", "周期2", 12, 2, 120), ("n3", "周期3", 24, 2, 250)],
        "refs": [0],
        "explain": "股价偏离均线的百分比。",
        "usage": "偏离太大（比如高出 20 日线 15% 以上）往往会回调，不宜追高。",
        "trap": "不同股票的正常偏离幅度差别很大，要和它自己的历史比。",
    },
    "obv": {
        "name": "能量潮 OBV", "cat": "量能", "pane": "sub", "params": [("m", "均线", 30, 2, 120)],
        "explain": "上涨日加上成交量、下跌日减去成交量，累加成一条线，反映资金进出的累计方向。",
        "usage": "股价横着走而 OBV 持续向上，说明有资金在悄悄买（吸筹迹象）；股价创新高而 OBV 不创新高是顶背离。",
        "trap": "OBV 的绝对数值没有意义，只看方向和背离。",
    },
    "mfi": {
        "name": "资金流量 MFI", "cat": "量能", "pane": "sub", "params": [("n", "周期", 14, 2, 60)],
        "refs": [20, 80],
        "explain": "结合价格和成交量的强弱指标。80 以上偏热，20 以下偏冷。",
        "usage": "价格新低而 MFI 没有新低（底背离）可能是资金在低位承接。",
        "trap": "同样会在单边行情里长时间钝化。",
    },
    "vr": {
        "name": "成交量比率 VR", "cat": "量能", "pane": "sub", "params": [("n", "周期", 26, 5, 120), ("m", "均线", 6, 2, 60)],
        "refs": [70, 150],
        "explain": "上涨日成交量与下跌日成交量之比（×100）。",
        "usage": "70 以下说明卖盘枯竭、可能见底；远高于 150 说明过热。",
        "trap": "只反映成交量的多空对比，不考虑涨跌幅度。",
    },
    "atr": {
        "name": "真实波幅 ATR", "cat": "波动", "pane": "sub", "params": [("n", "周期", 14, 2, 60)],
        "explain": "这只股票平均每天波动多少元（虚线是占股价的百分比）。",
        "usage": "用来设止损距离：比如买入价下方 2 倍 ATR，波动大的股票止损放宽、买少一点。",
        "trap": "ATR 变大不代表要涨或要跌，只代表波动变大。",
    },
    "trix": {
        "name": "三重指数 TRIX", "cat": "趋势", "pane": "sub", "params": [("n", "周期", 12, 3, 60), ("m", "均线", 9, 2, 60)],
        "refs": [0],
        "explain": "对收盘价做三次指数平滑后的变化率，过滤掉了很多短期波动。",
        "usage": "TRIX 上穿均线偏多，适合看中期趋势。",
        "trap": "很滞后，不适合短线。",
    },
    "dma": {
        "name": "平行线差 DMA", "cat": "趋势", "pane": "sub", "params": [("n1", "短期", 10, 2, 60), ("n2", "长期", 50, 5, 250), ("m", "均线", 10, 2, 60)],
        "refs": [0],
        "explain": "短期均线减长期均线的差。",
        "usage": "DIF 在零轴上方且上穿 DIFMA 偏多。",
        "trap": "和均线一样滞后。",
    },
    "psy": {
        "name": "心理线 PSY", "cat": "摆动", "pane": "sub", "params": [("n", "周期", 12, 3, 60), ("m", "均线", 6, 2, 30)],
        "refs": [25, 75],
        "explain": "最近 N 天里上涨天数的比例。",
        "usage": "75 以上说明连续上涨、情绪偏热；25 以下说明连续下跌、情绪低迷。",
        "trap": "只数天数不管涨跌幅度。",
    },
    "brar": {
        "name": "人气意愿 BRAR", "cat": "摆动", "pane": "sub", "params": [("n", "周期", 26, 5, 120)],
        "refs": [100],
        "explain": "AR 看当天开盘后多空力量，BR 看相对昨天收盘的多空力量。100 左右为平衡。",
        "usage": "BR、AR 同时从低位上升偏多；过高说明情绪过热。",
        "trap": "数值波动大，要看趋势而不是某一天。",
    },
    "cr": {
        "name": "能量指标 CR", "cat": "摆动", "pane": "sub", "params": [("n", "周期", 26, 5, 120)],
        "refs": [100],
        "explain": "以前一天的中间价为基准，比较多空力量。",
        "usage": "CR 低于 40 常处于底部区域，高于 300 偏热。",
        "trap": "单独使用参考价值有限。",
    },
    "turnover": {
        "name": "换手率", "cat": "量能", "pane": "sub", "params": [("m", "均线", 5, 2, 60)],
        "explain": "每天成交的股数占流通股的百分比。",
        "usage": "低位换手率逐步放大说明资金开始活跃；高位连续极高换手（比如超过 20%）要警惕筹码松动。",
        "trap": "小盘股天然换手高，要和它自己的历史比较。",
    },
    "volratio": {
        "name": "量比", "cat": "量能", "pane": "sub", "params": [("n", "对比天数", 5, 2, 30)],
        "refs": [1, 2],
        "explain": "今天的成交量是前几天平均成交量的几倍。大于 1 比平时活跃，大于 2 算明显放量。",
        "usage": "突破时量比大于 1.5 更可靠；下跌时量比很大要小心。",
        "trap": "盘中的量比按分钟折算，和这里的日线量比略有不同。",
    },
    "updown": {
        "name": "阳量阴量比", "cat": "主力", "pane": "sub", "params": [("n", "天数", 20, 5, 120)],
        "refs": [1],
        "explain": "最近 N 天上涨日的成交量总和 ÷ 下跌日的成交量总和。",
        "usage": "明显大于 1（比如 1.3 以上）说明买的力量更强，低位出现时常是吸筹迹象；高位跌破 1 要警惕。",
        "trap": "几天的大成交就能拉高这个比值，要结合位置和 K 线看。",
    },
}

CATS: list[str] = ["趋势", "量能", "摆动", "波动", "主力"]


def spec(ind_id: str) -> dict:
    if ind_id not in CATALOG:
        raise ValueError(f"没有「{ind_id}」这个指标，可选：{'、'.join(CATALOG)}")
    return CATALOG[ind_id]


def defaults(ind_id: str) -> dict[str, Any]:
    return {k: d for k, _, d, _, _ in spec(ind_id)["params"]}


def clean_params(ind_id: str, raw: dict | None) -> dict[str, Any]:
    """把前端传来的参数限制在允许范围内（类型跟默认值一致）；不认识的参数忽略"""
    out: dict[str, Any] = defaults(ind_id)
    for key, label, default, lo, hi in spec(ind_id)["params"]:
        if not raw or key not in raw or raw[key] in (None, ""):
            continue
        try:
            val: float = float(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"{spec(ind_id)['name']} 的「{label}」要填数字") from None
        if not lo <= val <= hi:
            raise ValueError(f"{spec(ind_id)['name']} 的「{label}」要在 {lo} 到 {hi} 之间")
        out[key] = int(round(val)) if isinstance(default, int) else val
    return out


def listing() -> list[dict]:
    """给前端的目录（不含计算函数）"""
    out: list[dict] = []
    for ind_id, s in CATALOG.items():
        out.append({
            "id": ind_id, "name": s["name"], "cat": s["cat"], "pane": s["pane"], "default": bool(s.get("default")),
            "params": [{"key": k, "label": lab, "default": d, "min": lo, "max": hi} for k, lab, d, lo, hi in s["params"]],
            "refs": s.get("refs", []), "explain": s["explain"], "usage": s["usage"], "trap": s["trap"],
        })
    return out


def compute(bars: pl.DataFrame | list[dict], ids: list[str], params: dict[str, dict] | None = None) -> dict[str, dict]:
    """在一只股票的 K 线（按日期升序）上计算指标。

    返回 {指标id: {name, pane, refs, params, lines: [{key, label, style, data: [与 bars 等长的数值或 None]}], error?}}；
    某个指标算不了（比如缺换手率）只在它自己的 error 里说明，不影响其他指标。
    """
    df: pl.DataFrame = bars if isinstance(bars, pl.DataFrame) else pl.DataFrame(bars)
    out: dict[str, dict] = {}
    if df.height == 0:
        return out
    ctx = Ctx(None, df.height)
    params = params or {}
    for ind_id in ids:
        s: dict = spec(ind_id)
        item: dict = {"name": s["name"], "pane": s["pane"], "refs": s.get("refs", []), "lines": []}
        try:
            p: dict = clean_params(ind_id, params.get(ind_id))
            item["params"] = p
            for key, label, series, style in ta.COMPUTE[ind_id](df, ctx, **p):
                data: list = [None if v is None or v != v or v in (float("inf"), float("-inf")) else round(float(v), 6)
                              for v in series.to_list()]
                item["lines"].append({"key": key, "label": label, "style": style, "data": data})
        except ValueError as e:
            item["error"] = str(e)
        out[ind_id] = item
    return out
