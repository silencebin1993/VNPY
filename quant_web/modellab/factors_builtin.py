"""
内置因子库（模型实验室 P7）：在 quant_web.formula.engine.prepare_frame 产出的表上，批量算出几组特征。

四组因子：
- basic（b_ 前缀）：基础量价特征。动量反转、波动率、换手、流动性、均线偏离、高低点距离、形态等，全用
  quant_web.indicators.funcs 里的分组滚动函数（Ctx + F.*），整表一次性算，不逐股票循环，5 百万行也是几秒的事。
- ta（t_ 前缀）：quant_web.indicators.ta 里的通达信技术指标。价格/成交量"量纲"的输出（MACD 的 DIF/DEA、BOLL
  轨道、ATR、DMA、OBV 等）换算成比例或斜率，方便跨股票比较；本来就是 0~100 或比例的震荡指标原样保留。
- alpha158（a158_ 前缀）/ alpha101（a101_ 前缀）：vnpy 自带的 Alpha158（纯时序）、Alpha101（含截面算子）表达式
  库，直接复用 vnpy.alpha.dataset 的表达式字符串和 calculate_by_expression 求值，不调用 AlphaDataset.prepare_data
  （它按表达式起子进程、把整张表 pickle 过去，因子一多就很慢，这里用不上）。Alpha158 按股票分块（每只股票的
  时序特征只用自己的历史，分块大小不影响结果）；Alpha101 按日期分块、每块前面补一段回看期（含截面排名，要同
  一天所有股票的数据）。哪些表达式在当前 vnpy 版本上跑不通，记在 SKIPPED 里，跳过不影响其它因子。

统一契约（四个 builder 一致）：
- 输入一张按 (code, date) 排序的 frame（prepare_frame 的产出，停牌日已经去掉）；
- 输出行数、顺序、(code, date) 和输入完全一致，特征列一律 Float32，NaN/±inf 换成 null；
- 只用当天及更早的数据（截面算子只用同一天其它股票的数据，不用任何未来行）。

progress 统一是 Callable[[float, str], None]（进度 0~1，中文说明），和仓库其它地方一致；basic/ta 不分块、很
快，没有 progress 参数。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import numpy as np
import polars as pl

from ..indicators import funcs as F
from ..indicators import ta
from ..indicators.funcs import Ctx

Progress = Callable[[float, str], None]


# ---------------------------------------------------------------- 小工具

def _f64(s: pl.Series) -> pl.Series:
    return s.cast(pl.Float64, strict=False)


def _clean(s: pl.Series, name: str) -> pl.Series:
    """NaN / ±inf → null，转 Float32"""
    s = _f64(s)
    out = pl.select(pl.when(s.is_nan() | s.is_infinite()).then(None).otherwise(s)).to_series()
    return out.cast(pl.Float32).alias(name)


def _finish(frame: pl.DataFrame, feats: dict[str, pl.Series], names: list[str]) -> pl.DataFrame:
    data: dict[str, pl.Series] = {"code": frame["code"], "date": frame["date"]}
    for n in names:
        data[n] = _clean(feats[n], n)
    return pl.DataFrame(data)


def _safe_log(x: pl.Series) -> pl.Series:
    return pl.select(pl.when(x > 0).then(x.log()).otherwise(None)).to_series()


def _rolling_corr_by_code(a: pl.Series, b: pl.Series, code: pl.Series, n: int) -> pl.Series:
    """funcs.py 没有跨两列的滚动相关系数，借 polars 的 rolling_corr + .over(code) 自己算"""
    df = pl.DataFrame({"_code": code, "_a": _f64(a), "_b": _f64(b)})
    return df.select(pl.rolling_corr("_a", "_b", window_size=n, min_samples=n).over("_code")).to_series()


def _rolling_skew_by_code(x: pl.Series, code: pl.Series, n: int) -> pl.Series:
    """funcs.py 没有滚动偏度，借 polars 的 rolling_skew + .over(code) 自己算"""
    df = pl.DataFrame({"_code": code, "_x": _f64(x)})
    return df.select(pl.col("_x").rolling_skew(window_size=n, min_samples=n).over("_code")).to_series()


# ---------------------------------------------------------------- basic（基础量价）

_BASIC_NAMES: list[str] = [
    "b_ret_1", "b_ret_5", "b_ret_10", "b_ret_20", "b_ret_60", "b_ret_120",
    "b_vol_5", "b_vol_20", "b_vol_60",
    "b_turn_5", "b_turn_20", "b_turn_ratio_5_60",
    "b_liq_log_amount_20", "b_amount_ratio_5_60", "b_amihud_20",
    "b_ma_dev_5", "b_ma_dev_10", "b_ma_dev_20", "b_ma_dev_60", "b_ma_dev_120", "b_ma_dev_250",
    "b_dist_high_20", "b_dist_low_20", "b_dist_high_60", "b_dist_low_60", "b_dist_high_250", "b_dist_low_250",
    "b_ret_max_20", "b_ret_min_20",
    "b_skew_20", "b_up_frac_20", "b_hl_range_20", "b_gap", "b_corr_ret_vol_20",
]


def basic_features(frame: pl.DataFrame) -> pl.DataFrame:
    """基础量价特征（b_ 前缀，34 个）：动量反转、波动率、换手、流动性、均线偏离、高低点距离、形态等。
    收益全部用前复权收盘价算；今日缺口用真实开盘价/真实昨收（含除权跳空）。"""
    ctx = Ctx(frame["code"])
    code = frame["code"]
    close = _f64(frame["close"])
    high = _f64(frame["high"])
    low = _f64(frame["low"])
    volume = _f64(frame["volume"])
    amount = _f64(frame["amount"])
    turn = _f64(frame["turn"]) if "turn" in frame.columns else pl.Series([None] * frame.height, dtype=pl.Float64)
    raw_open = _f64(frame["raw_open"])
    preclose = _f64(frame["preclose"])

    def ret(n: int) -> pl.Series:
        return F.safe_div(close, F.ref(close, n, ctx)) - 1

    r1 = ret(1)
    feats: dict[str, pl.Series] = {f"b_ret_{n}": ret(n) for n in (1, 5, 10, 20, 60, 120)}
    for n in (5, 20, 60):
        feats[f"b_vol_{n}"] = F.std(r1, n, ctx)

    turn_ma = {n: F.ma(turn, n, ctx) for n in (5, 20, 60)}
    feats["b_turn_5"] = turn_ma[5]
    feats["b_turn_20"] = turn_ma[20]
    feats["b_turn_ratio_5_60"] = F.safe_div(turn_ma[5], turn_ma[60])

    amt_ma = {n: F.ma(amount, n, ctx) for n in (5, 20, 60)}
    feats["b_liq_log_amount_20"] = _safe_log(amt_ma[20])
    feats["b_amount_ratio_5_60"] = F.safe_div(amt_ma[5], amt_ma[60])
    feats["b_amihud_20"] = F.ma(F.safe_div(r1.abs(), amount) * 1e8, 20, ctx)

    for n in (5, 10, 20, 60, 120, 250):
        feats[f"b_ma_dev_{n}"] = F.safe_div(close, F.ma(close, n, ctx)) - 1
    for n in (20, 60, 250):
        feats[f"b_dist_high_{n}"] = F.safe_div(close, F.hhv(high, n, ctx)) - 1
        feats[f"b_dist_low_{n}"] = F.safe_div(close, F.llv(low, n, ctx)) - 1

    feats["b_ret_max_20"] = F.hhv(r1, 20, ctx)
    feats["b_ret_min_20"] = F.llv(r1, 20, ctx)
    feats["b_skew_20"] = _rolling_skew_by_code(r1, code, 20)
    feats["b_up_frac_20"] = F.count(close > F.ref(close, 1, ctx), 20, ctx) / 20
    feats["b_hl_range_20"] = F.ma(F.safe_div(high - low, close), 20, ctx)
    feats["b_gap"] = F.safe_div(raw_open, preclose) - 1
    vol_diff = volume - F.ref(volume, 1, ctx)
    feats["b_corr_ret_vol_20"] = _rolling_corr_by_code(r1, vol_diff, code, 20)

    return _finish(frame, feats, _BASIC_NAMES)


# ---------------------------------------------------------------- ta（技术指标）

_TA_NAMES: list[str] = [
    "t_macd_dif", "t_macd_dea", "t_macd_hist",
    "t_kdj_k", "t_kdj_d", "t_kdj_j",
    "t_rsi6", "t_rsi12", "t_rsi24",
    "t_wr10", "t_wr6",
    "t_cci14",
    "t_dmi_pdi", "t_dmi_mdi", "t_dmi_adx", "t_dmi_adxr",
    "t_bias6", "t_bias12", "t_bias24",
    "t_obv_dev", "t_obv_slope",
    "t_mfi14",
    "t_vr", "t_vr_ma6",
    "t_atr14",
    "t_trix", "t_trix_ma",
    "t_dma_dif", "t_dma_difma",
    "t_psy", "t_psy_ma",
    "t_br", "t_ar",
    "t_cr",
    "t_boll_pctb", "t_boll_width",
    "t_volratio",
    "t_updown",
]

_TA_WANTED: tuple[str, ...] = (
    "macd", "kdj", "rsi", "wr", "cci", "dmi", "bias", "obv", "mfi", "vr", "atr",
    "trix", "dma", "psy", "brar", "cr", "boll", "volratio", "updown",
)


def ta_features(frame: pl.DataFrame) -> pl.DataFrame:
    """通达信技术指标（t_ 前缀，38 个），全用 quant_web.indicators.ta 的默认参数算。价格/成交量量纲的输出
    （MACD、DMA 换成股价比例；BOLL 换成 %B 和带宽；OBV 换成偏离度和标准化斜率；ATR 用它自带的占股价百分比）；
    本来就是 0~100 或比例的震荡指标（KDJ/RSI/WR/MFI/PSY/DMI/BIAS/CCI/VR/BR/AR/CR）原样保留。"""
    ctx = Ctx(frame["code"])
    close = _f64(frame["close"])
    volume = _f64(frame["volume"])
    d = {"open": frame["open"], "high": frame["high"], "low": frame["low"], "close": frame["close"], "volume": frame["volume"]}
    lines: dict[str, dict[str, pl.Series]] = {}
    for key in _TA_WANTED:
        fn = ta.COMPUTE.get(key)
        if fn is not None:
            lines[key] = {k: s for k, _, s, _ in fn(d, ctx)}

    feats: dict[str, pl.Series] = {}
    if "macd" in lines:
        m = lines["macd"]
        feats["t_macd_dif"] = F.safe_div(m["dif"], close)
        feats["t_macd_dea"] = F.safe_div(m["dea"], close)
        feats["t_macd_hist"] = F.safe_div(m["macd"], close)
    if "kdj" in lines:
        k = lines["kdj"]
        feats["t_kdj_k"], feats["t_kdj_d"], feats["t_kdj_j"] = k["k"], k["d"], k["j"]
    if "rsi" in lines:
        r = lines["rsi"]
        feats["t_rsi6"], feats["t_rsi12"], feats["t_rsi24"] = r["rsi1"], r["rsi2"], r["rsi3"]
    if "wr" in lines:
        w = lines["wr"]
        feats["t_wr10"], feats["t_wr6"] = w["wr1"], w["wr2"]
    if "cci" in lines:
        feats["t_cci14"] = lines["cci"]["cci"]
    if "dmi" in lines:
        dm = lines["dmi"]
        feats["t_dmi_pdi"], feats["t_dmi_mdi"] = dm["pdi"], dm["mdi"]
        feats["t_dmi_adx"], feats["t_dmi_adxr"] = dm["adx"], dm["adxr"]
    if "bias" in lines:
        b = lines["bias"]
        feats["t_bias6"], feats["t_bias12"], feats["t_bias24"] = b["bias1"], b["bias2"], b["bias3"]
    if "obv" in lines:
        o = lines["obv"]
        feats["t_obv_dev"] = F.safe_div(o["obv"] - o["maobv"], F.sum_(volume, 30, ctx))
        feats["t_obv_slope"] = F.safe_div(F.slope(o["obv"], 20, ctx), F.ma(volume, 20, ctx))
    if "mfi" in lines:
        feats["t_mfi14"] = lines["mfi"]["mfi"]
    if "vr" in lines:
        v = lines["vr"]
        feats["t_vr"], feats["t_vr_ma6"] = v["vr"], v["mavr"]
    if "atr" in lines:
        feats["t_atr14"] = lines["atr"]["atrp"]
    if "trix" in lines:
        t = lines["trix"]
        feats["t_trix"], feats["t_trix_ma"] = t["trix"], t["matrix"]
    if "dma" in lines:
        dma_ = lines["dma"]
        feats["t_dma_dif"] = F.safe_div(dma_["dif"], close)
        feats["t_dma_difma"] = F.safe_div(dma_["difma"], close)
    if "psy" in lines:
        p = lines["psy"]
        feats["t_psy"], feats["t_psy_ma"] = p["psy"], p["psyma"]
    if "brar" in lines:
        br = lines["brar"]
        feats["t_br"], feats["t_ar"] = br["br"], br["ar"]
    if "cr" in lines:
        feats["t_cr"] = lines["cr"]["cr"]
    if "boll" in lines:
        bo = lines["boll"]
        feats["t_boll_pctb"] = F.safe_div(close - bo["lower"], bo["upper"] - bo["lower"])
        feats["t_boll_width"] = F.safe_div(bo["upper"] - bo["lower"], bo["mid"])
    if "volratio" in lines:
        feats["t_volratio"] = lines["volratio"]["vratio"]
    if "updown" in lines:
        feats["t_updown"] = lines["updown"]["udr"]

    return _finish(frame, feats, _TA_NAMES)


# ---------------------------------------------------------------- 中文说明（novice 用）

FEATURE_INFO: dict[str, dict[str, str]] = {
    "b_ret_1": {"label": "1日涨跌幅", "desc": "今天收盘价比昨天涨/跌了百分之多少（前复权价，剔除除权除息的影响）。"},
    "b_ret_5": {"label": "5日涨跌幅", "desc": "最近5个交易日的累计涨跌幅，短线动量强不强看这个。"},
    "b_ret_10": {"label": "10日涨跌幅", "desc": "最近10个交易日的累计涨跌幅。"},
    "b_ret_20": {"label": "20日涨跌幅", "desc": "最近20个交易日（约一个月）的累计涨跌幅。"},
    "b_ret_60": {"label": "60日涨跌幅", "desc": "最近60个交易日（约一个季度）的累计涨跌幅，看中期趋势。"},
    "b_ret_120": {"label": "120日涨跌幅", "desc": "最近120个交易日（约半年）的累计涨跌幅。"},
    "b_vol_5": {"label": "5日波动率", "desc": "最近5天每日涨跌幅的波动大小，数值越大说明这几天越颠簸。"},
    "b_vol_20": {"label": "20日波动率", "desc": "最近20天每日涨跌幅的波动大小。"},
    "b_vol_60": {"label": "60日波动率", "desc": "最近60天每日涨跌幅的波动大小，反映中期的风险高低。"},
    "b_turn_5": {"label": "5日平均换手率", "desc": "最近5天平均每天有百分之多少的股份换手，越高说明交易越活跃。"},
    "b_turn_20": {"label": "20日平均换手率", "desc": "最近20天平均每天的换手率。"},
    "b_turn_ratio_5_60": {"label": "换手率短长比", "desc": "最近5天的平均换手率相对最近60天的倍数，大于1说明最近交易明显比平时活跃。"},
    "b_liq_log_amount_20": {"label": "20日成交额对数", "desc": "最近20天平均每天成交金额取对数，用来衡量股票的“体量”，方便不同价格的股票互相比较。"},
    "b_amount_ratio_5_60": {"label": "成交额短长比", "desc": "最近5天平均成交额相对最近60天的倍数，看资金关注度是不是在升温。"},
    "b_amihud_20": {"label": "20日非流动性(Amihud)", "desc": "每单位成交金额能带来多大的价格波动，数值越大说明股票越“难成交”（流动性越差）。"},
    "b_ma_dev_5": {"label": "乖离率(5日)", "desc": "收盘价比5日均线高/低百分之多少。"},
    "b_ma_dev_10": {"label": "乖离率(10日)", "desc": "收盘价比10日均线高/低百分之多少。"},
    "b_ma_dev_20": {"label": "乖离率(20日)", "desc": "收盘价比20日均线高/低百分之多少。"},
    "b_ma_dev_60": {"label": "乖离率(60日)", "desc": "收盘价比60日均线高/低百分之多少。"},
    "b_ma_dev_120": {"label": "乖离率(120日)", "desc": "收盘价比120日均线高/低百分之多少。"},
    "b_ma_dev_250": {"label": "乖离率(250日/年线)", "desc": "收盘价比年线（250日均线）高/低百分之多少。"},
    "b_dist_high_20": {"label": "距20日高点", "desc": "收盘价比最近20天最高价低百分之多少（0表示正处于20天新高）。"},
    "b_dist_low_20": {"label": "距20日低点", "desc": "收盘价比最近20天最低价高百分之多少（0表示正处于20天新低）。"},
    "b_dist_high_60": {"label": "距60日高点", "desc": "收盘价比最近60天最高价低百分之多少。"},
    "b_dist_low_60": {"label": "距60日低点", "desc": "收盘价比最近60天最低价高百分之多少。"},
    "b_dist_high_250": {"label": "距250日高点(年内)", "desc": "收盘价比最近一年最高价低百分之多少。"},
    "b_dist_low_250": {"label": "距250日低点(年内)", "desc": "收盘价比最近一年最低价高百分之多少。"},
    "b_ret_max_20": {"label": "20日最大单日涨幅", "desc": "最近20天里涨得最猛的一天涨了多少。"},
    "b_ret_min_20": {"label": "20日最大单日跌幅", "desc": "最近20天里跌得最狠的一天跌了多少。"},
    "b_skew_20": {"label": "20日收益偏度", "desc": "最近20天每日涨跌幅分布的偏斜程度：正数说明偶尔有大涨、多数小跌；负数相反。"},
    "b_up_frac_20": {"label": "20日上涨天数占比", "desc": "最近20个交易日里，收盘价比前一天高的天数占比。"},
    "b_hl_range_20": {"label": "20日平均振幅", "desc": "最近20天，每天最高价和最低价的差占收盘价的比例，平均下来有多大。"},
    "b_gap": {"label": "今日开盘缺口", "desc": "今天开盘价相对昨天收盘价高开/低开了百分之多少（用真实价，含除权跳空）。"},
    "b_corr_ret_vol_20": {"label": "20日量价相关性", "desc": "最近20天，每天涨跌幅和成交量变化的相关程度：正数说明放量上涨/缩量下跌更常见。"},
    "t_macd_dif": {"label": "MACD快线(DIF)", "desc": "MACD指标的快线，短期和长期均线的差，换算成占股价的比例，方便跨股票比较。"},
    "t_macd_dea": {"label": "MACD慢线(DEA)", "desc": "DIF的平滑线，同样换算成占股价的比例。"},
    "t_macd_hist": {"label": "MACD柱", "desc": "DIF减DEA再放大，柱子由负转正常被当作短线转强的信号，换算成占股价的比例。"},
    "t_kdj_k": {"label": "KDJ-K值", "desc": "KDJ随机指标的K值，0~100之间，越高说明近期收盘价越接近区间高点。"},
    "t_kdj_d": {"label": "KDJ-D值", "desc": "K值的平滑线。"},
    "t_kdj_j": {"label": "KDJ-J值", "desc": "对K、D的偏离做了放大，波动比K、D更剧烈，经常冲出0~100区间。"},
    "t_rsi6": {"label": "RSI(6日)", "desc": "6日相对强弱指标，0~100之间，越高说明近期上涨的力量越强。"},
    "t_rsi12": {"label": "RSI(12日)", "desc": "12日相对强弱指标，0~100之间。"},
    "t_rsi24": {"label": "RSI(24日)", "desc": "24日相对强弱指标，0~100之间。"},
    "t_wr10": {"label": "威廉指标WR(10日)", "desc": "0~100之间，数值越大说明收盘价越接近最近10天的低点（超卖）。"},
    "t_wr6": {"label": "威廉指标WR(6日)", "desc": "0~100之间，数值越大说明收盘价越接近最近6天的低点（超卖）。"},
    "t_cci14": {"label": "顺势指标CCI(14日)", "desc": "衡量价格偏离均值的程度，正常在±100之间波动，超过说明可能超买或超卖。"},
    "t_dmi_pdi": {"label": "DMI-上升动向(+DI)", "desc": "多空力量对比中，多头力量的强弱，百分比。"},
    "t_dmi_mdi": {"label": "DMI-下降动向(-DI)", "desc": "多空力量对比中，空头力量的强弱，百分比。"},
    "t_dmi_adx": {"label": "DMI-趋势强度(ADX)", "desc": "不管涨跌方向，只看趋势强不强，数值越大趋势越明显。"},
    "t_dmi_adxr": {"label": "DMI-趋势强度平滑(ADXR)", "desc": "ADX的平滑版本。"},
    "t_bias6": {"label": "乖离率BIAS(6日)", "desc": "收盘价偏离6日均线的百分比。"},
    "t_bias12": {"label": "乖离率BIAS(12日)", "desc": "收盘价偏离12日均线的百分比。"},
    "t_bias24": {"label": "乖离率BIAS(24日)", "desc": "收盘价偏离24日均线的百分比。"},
    "t_obv_dev": {"label": "OBV偏离度", "desc": "累计能量潮(OBV)偏离它自己均线的程度，换算成相当于多少天成交量的比例，正数说明近期资金流入比均值猛。"},
    "t_obv_slope": {"label": "OBV斜率", "desc": "OBV最近20天的变化速度，除以日均成交量做了标准化，方便跨股票比较。"},
    "t_mfi14": {"label": "资金流量指标MFI(14日)", "desc": "0~100之间，把RSI的算法加上了成交量权重，越高说明资金净流入越明显。"},
    "t_vr": {"label": "成交量比率VR", "desc": "近期上涨日成交量和下跌日成交量的比值（放大到百分比），大于100说明上涨日更放量。"},
    "t_vr_ma6": {"label": "VR的6日均线", "desc": "VR指标的6日均线，更平滑。"},
    "t_atr14": {"label": "真实波幅ATR占股价比", "desc": "最近14天平均每天的真实波动幅度，占股价的百分比，衡量绝对波动大小。"},
    "t_trix": {"label": "三重指数平滑TRIX", "desc": "对收盘价做三次指数平滑后再算变化率，过滤掉短期噪音后的趋势方向。"},
    "t_trix_ma": {"label": "TRIX的9日均线", "desc": "TRIX指标的9日均线，更平滑。"},
    "t_dma_dif": {"label": "平行线差DMA", "desc": "短期均线减长期均线，换算成占股价的比例，判断中短期趋势的强弱方向。"},
    "t_dma_difma": {"label": "DMA的10日均线", "desc": "DMA指标的10日均线，更平滑。"},
    "t_psy": {"label": "心理线PSY", "desc": "最近12天里上涨天数的占比（0~100），反映追涨情绪高不高。"},
    "t_psy_ma": {"label": "PSY的6日均线", "desc": "PSY指标的6日均线，更平滑。"},
    "t_br": {"label": "人气意愿指标BR", "desc": "以昨收盘为基准，衡量多空双方力量对比的百分比指标。"},
    "t_ar": {"label": "人气指标AR", "desc": "以当天开盘价为基准，衡量多空双方力量对比的百分比指标。"},
    "t_cr": {"label": "能量指标CR", "desc": "衡量最近一段时间多空双方争夺力度的百分比指标。"},
    "t_boll_pctb": {"label": "布林带%B", "desc": "收盘价在布林带上下轨之间的位置，0表示贴下轨，1表示贴上轨，可以大于1或小于0。"},
    "t_boll_width": {"label": "布林带宽度", "desc": "布林带上下轨的宽度占中轨的比例，越大说明近期波动越剧烈。"},
    "t_volratio": {"label": "量比", "desc": "今天成交量相对最近5天平均成交量的倍数，衡量今天是不是明显放量/缩量。"},
    "t_updown": {"label": "阳量阴量比", "desc": "最近20天，上涨日的成交量总和除以下跌日的成交量总和，大于1说明上涨更受资金支持。"},
}

# alpha158/alpha101 跳过的表达式名字（第一次用到那组因子时才会填，见 _probe_alpha）
SKIPPED: dict[str, list[str]] = {}


# ---------------------------------------------------------------- alpha158 / alpha101（vnpy 自带表达式库）

_ALPHA_PREFIX: dict[str, str] = {"alpha158": "a158", "alpha101": "a101"}


def _vnpy_frame(sub: pl.DataFrame) -> pl.DataFrame:
    """把 frame 的一段行转成 vnpy 表达式函数要的列：datetime/vt_symbol + 前复权 OHLCV + vwap。
    vwap 用真实成交额/真实成交量算出真实均价，再乘 close/raw_close（前复权比例）换成前复权口径；
    成交量为 0（prepare_frame 已经把这种行过滤掉了，这里只是防御）时 vwap 为空。"""
    vwap = (
        pl.when(pl.col("volume") > 0)
        .then(pl.col("amount").cast(pl.Float64) / pl.col("volume").cast(pl.Float64) * pl.col("close") / pl.col("raw_close"))
        .otherwise(None)
        .alias("vwap")
    )
    return sub.select(
        pl.col("date").alias("datetime"),
        pl.col("code").alias("vt_symbol"),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        vwap,
    )


def _raw_expressions(set_key: str) -> dict[str, str]:
    """实例化 vnpy 的 Alpha158/Alpha101 只为了读 feature_expressions，不跑 prepare_data（那个按表达式起子进程、
    把整张表 pickle 过去，因子一多非常慢，这里用不到）。"""
    dummy = pl.DataFrame({"datetime": [1], "vt_symbol": ["_dummy"], "close": [1.0]})
    period = ("2020-01-01", "2020-01-01")
    if set_key == "alpha158":
        from vnpy.alpha.dataset.datasets.alpha_158 import Alpha158
        ds = Alpha158(dummy, period, period, period)
    elif set_key == "alpha101":
        from vnpy.alpha.dataset.datasets.alpha_101 import Alpha101
        ds = Alpha101(dummy, period, period, period)
    else:
        raise ValueError(f"不认识的因子组：{set_key}")
    return {name: expr for name, expr in ds.feature_expressions.items() if isinstance(expr, str)}


def _eval_expressions(vd: pl.DataFrame, expressions: dict[str, str], prefix: str) -> tuple[pl.DataFrame, list[str]]:
    """在一段已经改好列名的表上依次求值表达式，按 (datetime, vt_symbol) 对齐拼成宽表（不能直接按位置拼列：
    vnpy 的 ts_corr/quesval/cs_scale 等函数内部会做 join，结果的行顺序不一定和输入一致）。
    返回 (结果表, 这次算不出来的表达式名字)。

    用 eval() 执行表达式字符串是 calculate_by_expression 自己做的事；这里传进去的字符串全部来自 vnpy 自带、写死在
    vnpy/alpha/dataset/datasets/alpha_158.py、alpha_101.py 里的因子库，不接受任何外部/用户输入，可以放心用。
    """
    from vnpy.alpha.dataset.utility import calculate_by_expression

    from . import fast_ts

    keys = vd.select(["datetime", "vt_symbol"])
    failed: list[str] = []
    for name, expr in expressions.items():
        try:
            with fast_ts.patched():                 # 逐行 Python 回调的时序算子换成向量化版本（结果一致，快几百倍）
                res = calculate_by_expression(vd, expr)
        except Exception:  # noqa: BLE001  vnpy 表达式库里个别表达式算不出来，跳过，不影响其它因子
            failed.append(name)
            continue
        col = f"{prefix}_{name}"
        keys = keys.join(
            res.select(["datetime", "vt_symbol", pl.col("data").alias(col)]),
            on=["datetime", "vt_symbol"], how="left",
        )
    return keys, failed


def _tiny_synthetic_frame(n_codes: int = 3, n_days: int = 270, seed: int = 0) -> pl.DataFrame:
    """探测 alpha158/101 表达式用的一张很小的合成表（不是给用户用的功能）：prepare_frame 契约的列都有，没有
    除权（raw_close 等于 close），长度够覆盖库里最长的时序窗口（250 天）。"""
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    for i in range(n_codes):
        code = f"60{i:04d}.SH"
        price = 10.0 + float(rng.random()) * 10
        d = date(2022, 1, 3)
        prev = price
        for _ in range(n_days):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            o_ = prev * (1 + float(rng.normal(0, 0.004)))
            price = max(o_ * (1 + float(rng.normal(0, 0.018))), 0.2)
            h_ = max(o_, price) * (1 + abs(float(rng.normal(0, 0.004))))
            l_ = min(o_, price) * (1 - abs(float(rng.normal(0, 0.004))))
            vol = max(float(rng.normal(2_000_000, 500_000)), 1_000.0)
            amount = vol * price
            turn = float(rng.uniform(0.5, 8.0))
            rows.append((code, d, o_, h_, l_, price, price, o_, prev, vol, amount, turn, False))
            prev = price
            d += timedelta(days=1)
    cols = ["code", "date", "open", "high", "low", "close", "raw_close", "raw_open", "preclose", "volume", "amount", "turn", "is_st"]
    out = pl.DataFrame(rows, schema=cols, orient="row")
    return out.with_columns(pl.col("date").cast(pl.Date), pl.col("is_st").cast(pl.Boolean)).sort(["code", "date"])


_ALPHA_CACHE: dict[str, tuple[dict[str, str], list[str]]] = {}


def _probe_alpha(set_key: str) -> tuple[dict[str, str], list[str]]:
    """第一次用到某个 alpha 因子组时才做：在一张很小的合成表上把 vnpy 自带的表达式全跑一遍，跑不通的记到
    SKIPPED 里并排除，剩下的（加前缀）就是这个因子组最终会产出的列名；顺便给 FEATURE_INFO 补上通用说明。
    只做一次，缓存。"""
    hit = _ALPHA_CACHE.get(set_key)
    if hit is not None:
        return hit
    prefix = _ALPHA_PREFIX[set_key]
    raw = _raw_expressions(set_key)
    probe = _vnpy_frame(_tiny_synthetic_frame())
    _, failed = _eval_expressions(probe, raw, prefix)
    active = {name: expr for name, expr in raw.items() if name not in failed}
    names = [f"{prefix}_{name}" for name in active]
    SKIPPED[set_key] = list(failed)
    tag = "Alpha158" if set_key == "alpha158" else "Alpha101"
    for n in names:
        FEATURE_INFO.setdefault(n, {
            "label": f"{tag} 因子 {n[len(prefix) + 1:].upper()}",
            "desc": f"vnpy 内置 {tag} 因子库里的一个量价表达式因子，交给模型自动学习它和未来收益的关系，不用逐条看公式含义。",
        })
    result = (active, names)
    _ALPHA_CACHE[set_key] = result
    return result


def _clean_col(name: str) -> pl.Expr:
    c = pl.col(name).cast(pl.Float64, strict=False)
    return pl.when(c.is_infinite() | c.is_nan()).then(None).otherwise(c).cast(pl.Float32).alias(name)


def _assemble(frame: pl.DataFrame, parts: list[pl.DataFrame], names: list[str]) -> pl.DataFrame:
    """把各块的结果按 (code, date) 对齐回 frame 原来的行序；缺的列（表达式在这次真实数据上也没算出来，或者
    整段就没有块）补成 null，保证输出列名永远等于 names（= feature_names(set_key)）。"""
    keys = frame.select(["code", "date"])
    if parts:
        combined = pl.concat(parts, how="diagonal_relaxed").rename({"datetime": "date", "vt_symbol": "code"})
        out = keys.join(combined, on=["code", "date"], how="left")
    else:
        out = keys
    missing = [n for n in names if n not in out.columns]
    if missing:
        out = out.with_columns([pl.lit(None, dtype=pl.Float32).alias(n) for n in missing])
    out = out.with_columns([_clean_col(n) for n in names])
    return out.select(["code", "date", *names])


def _run_chunks_by_code(
    frame: pl.DataFrame, expressions: dict[str, str], prefix: str, chunk_codes: int, progress: Progress | None,
) -> list[pl.DataFrame]:
    codes = frame["code"].unique(maintain_order=True).to_list()
    step = max(1, int(chunk_codes))
    groups = [codes[i:i + step] for i in range(0, len(codes), step)]
    parts: list[pl.DataFrame] = []
    for i, group in enumerate(groups):
        sub = frame.filter(pl.col("code").is_in(group))
        vd = _vnpy_frame(sub)
        result, _failed = _eval_expressions(vd, expressions, prefix)
        parts.append(result)
        if progress is not None:
            progress((i + 1) / len(groups), f"Alpha158：第 {i + 1}/{len(groups)} 块（按股票分块，本块 {len(group)} 只）")
    return parts


def _run_chunks_by_date(
    frame: pl.DataFrame, expressions: dict[str, str], prefix: str, chunk_days: int, lookback_days: int,
    progress: Progress | None,
) -> list[pl.DataFrame]:
    all_dates = frame["date"].unique().sort().to_list()
    step = max(1, int(chunk_days))
    date_chunks = [all_dates[i:i + step] for i in range(0, len(all_dates), step)]
    parts: list[pl.DataFrame] = []
    for i, dchunk in enumerate(date_chunks):
        idx0 = i * step
        d0, d1 = dchunk[0], dchunk[-1]
        lb_start = all_dates[max(0, idx0 - int(lookback_days))]
        sub = frame.filter((pl.col("date") >= lb_start) & (pl.col("date") <= d1))
        vd = _vnpy_frame(sub)
        result, _failed = _eval_expressions(vd, expressions, prefix)
        result = result.filter((pl.col("datetime") >= d0) & (pl.col("datetime") <= d1))
        parts.append(result)
        if progress is not None:
            progress((i + 1) / len(date_chunks), f"Alpha101：第 {i + 1}/{len(date_chunks)} 块（{d0} ~ {d1}）")
    return parts


def alpha158_features(frame: pl.DataFrame, chunk_codes: int = 200, progress: Progress | None = None) -> pl.DataFrame:
    """Alpha158（vnpy 内置、qlib 风格的纯时序因子，a158_ 前缀，158 个）：每只股票的特征只用自己的历史，所以
    按股票分块算，分块大小只影响内存和速度、不影响结果（chunk_codes 多大都一样）。"""
    active, names = _probe_alpha("alpha158")
    parts = _run_chunks_by_code(frame, active, "a158", chunk_codes, progress)
    return _assemble(frame, parts, names)


def alpha101_features(
    frame: pl.DataFrame, chunk_days: int = 120, lookback_days: int = 260, progress: Progress | None = None,
) -> pl.DataFrame:
    """Alpha101（vnpy 内置、WorldQuant 风格，a101_ 前缀，能算的约 80 个）：含截面算子（cs_rank 等，同一天所有
    股票一起排名，用 .over("datetime")），所以按日期分块，每块前面补 lookback_days 个交易日当预热（只用来把
    时序窗口算准，预热部分不算进结果，也不会不同股票混在一起用未来数据）。
    默认 lookback_days=260：库里最长的时序窗口是 250 天（alpha19/alpha39 的 ts_sum(收益率,250)、alpha32 的
    ts_corr(...,230)），多留 10 天缓冲；以后如果换了更长窗口的表达式，要相应调大这个默认值。"""
    active, names = _probe_alpha("alpha101")
    parts = _run_chunks_by_date(frame, active, "a101", chunk_days, lookback_days, progress)
    return _assemble(frame, parts, names)


def feature_names(set_key: str) -> list[str]:
    """某个因子组最终会产出的列名（不含 code/date），算内存估算、UI 展示用，不用真的跑数据（alpha158/101 除外：
    第一次调用要在一张很小的合成表上探测一遍哪些表达式能算，之后缓存，不是每次都算）。"""
    if set_key == "basic":
        return list(_BASIC_NAMES)
    if set_key == "ta":
        return list(_TA_NAMES)
    if set_key in _ALPHA_PREFIX:
        _, names = _probe_alpha(set_key)
        return list(names)
    raise ValueError(f"不认识的因子组：{set_key}")


__all__ = [
    "FEATURE_INFO",
    "SKIPPED",
    "alpha101_features",
    "alpha158_features",
    "basic_features",
    "feature_names",
    "ta_features",
]
