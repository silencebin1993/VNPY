"""
ta 库扩展指标（x_ 前缀）：用开源技术分析库 ta（https://github.com/bukosabino/ta，pip install ta）补充
quant_web.indicators.ta 里没有的指标——Aroon、Vortex、KST、一目均衡表、STC、梅斯线、DPO、随机 RSI、TSI、
终极震荡、动量震荡 AO、ROC、PPO/PVO、肯特纳通道、唐奇安通道、蔡金资金流 CMF、强力指数、负量指标 NVI、VWAP。

和 factors_builtin 的约定一致：
- 输入按 (code, date) 排好序的 frame（open/high/low/close 前复权价，volume 股）；
- 输出行数、顺序、(code, date) 与输入完全一致，特征列 Float32，NaN/±inf → null；
- 每只股票只用自己当天及更早的数据（ta 库的指标都是向后看的滚动/指数平滑，逐股票算，不跨股票）。
价格量纲的输出（DPO、AO、一目均衡表各线、通道上下轨、VWAP）换成相对收盘价的比例，成交量量纲的（强力指数、NVI）
做了标准化，方便跨股票比较。ta 库里用 Python 循环实现、全市场跑起来太慢的指标（抛物线 PSAR、KAMA、溃疡指数）没有收。

ta 库是可选依赖：没有安装时这组因子在页面上显示为不可用（原因里写着安装命令），其它因子组不受影响。
"""
from __future__ import annotations

import importlib.util
import warnings

import numpy as np
import pandas as pd
import polars as pl

INSTALL_HINT: str = "没有安装 ta 技术分析库，在命令行运行 pip install ta 后重启量化助手即可使用"

# 因子名 → (中文名, 白话说明)
TAPKG_INFO: dict[str, tuple[str, str]] = {
    "x_aroon_up": ("阿隆上升线 Aroon Up(25)", "最近25天的最高价出现在多久以前，0~100，越高说明新高越近。"),
    "x_aroon_down": ("阿隆下降线 Aroon Down(25)", "最近25天的最低价出现在多久以前，0~100，越高说明新低越近。"),
    "x_aroon_ind": ("阿隆指标 Aroon(25)", "上升线减下降线，正数偏多、负数偏空。"),
    "x_vortex_pos": ("涡旋指标 VI+(14)", "向上的波动力量，大于 VI- 说明上涨力量占优。"),
    "x_vortex_neg": ("涡旋指标 VI-(14)", "向下的波动力量。"),
    "x_vortex_diff": ("涡旋指标差 VI+ - VI-", "正数说明上涨力量占优。"),
    "x_kst": ("确然指标 KST", "把 4 个不同周期的涨跌幅加权合在一起的长线动量。"),
    "x_kst_diff": ("KST 减信号线", "KST 高于自己的 9 日均线为正，常被当作动量转强。"),
    "x_ichi_conv": ("一目均衡表转换线偏离", "收盘价相对转换线（9日高低点中值）高/低百分之多少。"),
    "x_ichi_base": ("一目均衡表基准线偏离", "收盘价相对基准线（26日高低点中值）高/低百分之多少。"),
    "x_ichi_a": ("一目均衡表先行带A偏离", "收盘价相对先行带A高/低百分之多少（只用当天及以前的数据，没有前移）。"),
    "x_ichi_b": ("一目均衡表先行带B偏离", "收盘价相对先行带B（52日高低点中值）高/低百分之多少。"),
    "x_stc": ("沙夫趋势周期 STC", "MACD 再做两次随机指标平滑，0~100，比 MACD 反应更快。"),
    "x_mass": ("梅斯线 Mass Index", "高低价差的扩张收缩程度，数值突然升高常预示趋势可能反转。"),
    "x_dpo": ("区间震荡线 DPO", "去掉长期趋势后的短期起伏，换算成占股价的比例。"),
    "x_stochrsi_k": ("随机RSI-K", "RSI 在自己最近14天区间里的位置，0~1，比 RSI 更敏感。"),
    "x_stochrsi_d": ("随机RSI-D", "随机RSI-K 的 3 日平滑。"),
    "x_tsi": ("真实强度指数 TSI", "涨跌幅做两次指数平滑后的动量，正数偏多，范围约 -100~100。"),
    "x_uo": ("终极震荡指标 UO", "把 7/14/28 天三个周期的买盘力量加权合在一起，0~100。"),
    "x_ao": ("动量震荡 AO", "5日和34日中间价均线的差，换算成占股价的比例。"),
    "x_roc12": ("变动率 ROC(12)", "今天收盘价比12天前涨跌了百分之多少。"),
    "x_ppo": ("价格震荡百分比 PPO", "12日和26日指数均线之差占26日均线的百分比（MACD 的百分比版本）。"),
    "x_ppo_hist": ("PPO 柱", "PPO 减它的 9 日信号线。"),
    "x_pvo": ("成交量震荡百分比 PVO", "成交量的12日和26日指数均线之差占26日均线的百分比，正数说明近期放量。"),
    "x_pvo_hist": ("PVO 柱", "PVO 减它的 9 日信号线。"),
    "x_kc_pband": ("肯特纳通道%B", "收盘价在肯特纳通道（均线±平均波幅）里的位置，0贴下轨，1贴上轨。"),
    "x_kc_width": ("肯特纳通道宽度", "通道上下轨的宽度占中轨的百分比，越大波动越大。"),
    "x_dc_pband": ("唐奇安通道%B", "收盘价在最近20天最高价和最低价之间的位置，0贴低点，1贴高点。"),
    "x_dc_width": ("唐奇安通道宽度", "最近20天最高价和最低价的差占中线的百分比。"),
    "x_cmf": ("蔡金资金流 CMF(20)", "按收盘价在当天高低区间的位置给成交量加权，正数说明资金在买入，范围 -1~1。"),
    "x_fi": ("强力指数 Force Index(13)", "涨跌幅乘成交量的13日指数平滑，除以日均成交额做了标准化。"),
    "x_nvi_dev": ("负量指标 NVI 偏离", "只在缩量日累计涨跌的指数，相对它自己255日指数均线高/低百分之多少，常用来看“聪明钱”。"),
    "x_vwap_dev": ("VWAP 偏离(14)", "收盘价相对最近14天成交量加权均价高/低百分之多少。"),
}

TAPKG_NAMES: list[str] = list(TAPKG_INFO)


def available() -> tuple[bool, str]:
    if importlib.util.find_spec("ta") is None:
        return False, INSTALL_HINT
    return True, ""


def feature_names() -> list[str]:
    ok, reason = available()
    if not ok:
        raise RuntimeError(reason)
    return list(TAPKG_NAMES)


KST_WARMUP: int = 30 + 15 - 1 + 9        # ta 的 KST 最长 ROC 30 天 + 均线 15 天 + 信号线 9 天


def _ratio(a: pd.Series, b: pd.Series) -> pd.Series:
    return a / b.where(b != 0)


def aroon(high: pd.Series, low: pd.Series, window: int = 25) -> tuple[pd.Series, pd.Series]:
    """和 ta.trend.AroonIndicator 结果一致（窗口含当天共 window+1 天，最高/最低价第一次出现的位置），
    用 numpy 滑动窗口代替 ta 里逐窗口调 Python 函数的 rolling.apply，快很多"""
    from numpy.lib.stride_tricks import sliding_window_view

    n: int = len(high)
    up = np.full(n, np.nan)
    down = np.full(n, np.nan)
    if n > window:
        up[window:] = sliding_window_view(high.to_numpy(dtype="float64"), window + 1).argmax(axis=1) / window * 100
        down[window:] = sliding_window_view(low.to_numpy(dtype="float64"), window + 1).argmin(axis=1) / window * 100
    return pd.Series(up, index=high.index), pd.Series(down, index=low.index)


def negative_volume_index(close: pd.Series, volume: pd.Series) -> pd.Series:
    """和 ta.volume.NegativeVolumeIndexIndicator 结果一致（缩量日按涨跌幅累乘，起点 1000），
    ta 里是逐行 Python 循环，这里改成向量化的累乘"""
    ret: pd.Series = close.pct_change()
    step: pd.Series = (1 + ret).where(volume < volume.shift(1), 1.0)
    step.iloc[:1] = 1.0
    return 1000 * step.cumprod()


def _one_stock(df: pd.DataFrame) -> dict[str, pd.Series]:
    """一只股票（按日期排好序）的全部 x_ 因子"""
    import ta

    h, lo, c, v = df["high"], df["low"], df["close"], df["volume"]
    out: dict[str, pd.Series] = {}

    out["x_aroon_up"], out["x_aroon_down"] = aroon(h, lo, 25)
    out["x_aroon_ind"] = out["x_aroon_up"] - out["x_aroon_down"]
    vi = ta.trend.VortexIndicator(high=h, low=lo, close=c, window=14)
    out["x_vortex_pos"], out["x_vortex_neg"], out["x_vortex_diff"] = vi.vortex_indicator_pos(), vi.vortex_indicator_neg(), vi.vortex_indicator_diff()
    # ta 的 KST 在序列开头用整段收盘价的均值补 shift 出来的空位（会偷看未来），热身期整段置空
    kst = ta.trend.KSTIndicator(close=c)
    out["x_kst"], out["x_kst_diff"] = kst.kst().copy(), kst.kst_diff().copy()
    out["x_kst"].iloc[:KST_WARMUP] = np.nan
    out["x_kst_diff"].iloc[:KST_WARMUP] = np.nan
    # visual=False：先行带不向前平移 26 天（平移会用到“未来”的位置），只用当天及以前的数据
    ichi = ta.trend.IchimokuIndicator(high=h, low=lo, window1=9, window2=26, window3=52, visual=False)
    out["x_ichi_conv"] = _ratio(c, ichi.ichimoku_conversion_line()) - 1
    out["x_ichi_base"] = _ratio(c, ichi.ichimoku_base_line()) - 1
    out["x_ichi_a"] = _ratio(c, ichi.ichimoku_a()) - 1
    out["x_ichi_b"] = _ratio(c, ichi.ichimoku_b()) - 1
    out["x_stc"] = ta.trend.STCIndicator(close=c).stc()
    out["x_mass"] = ta.trend.MassIndex(high=h, low=lo).mass_index()
    out["x_dpo"] = _ratio(ta.trend.DPOIndicator(close=c, window=20).dpo(), c)

    srsi = ta.momentum.StochRSIIndicator(close=c)
    out["x_stochrsi_k"], out["x_stochrsi_d"] = srsi.stochrsi_k(), srsi.stochrsi_d()
    out["x_tsi"] = ta.momentum.TSIIndicator(close=c).tsi()
    out["x_uo"] = ta.momentum.UltimateOscillator(high=h, low=lo, close=c).ultimate_oscillator()
    out["x_ao"] = _ratio(ta.momentum.AwesomeOscillatorIndicator(high=h, low=lo).awesome_oscillator(), c)
    out["x_roc12"] = ta.momentum.ROCIndicator(close=c, window=12).roc()
    ppo = ta.momentum.PercentagePriceOscillator(close=c)
    out["x_ppo"], out["x_ppo_hist"] = ppo.ppo(), ppo.ppo_hist()
    pvo = ta.momentum.PercentageVolumeOscillator(volume=v)
    out["x_pvo"], out["x_pvo_hist"] = pvo.pvo(), pvo.pvo_hist()

    kc = ta.volatility.KeltnerChannel(high=h, low=lo, close=c, window=20, original_version=False)
    out["x_kc_pband"], out["x_kc_width"] = kc.keltner_channel_pband(), kc.keltner_channel_wband()
    dc = ta.volatility.DonchianChannel(high=h, low=lo, close=c, window=20)
    out["x_dc_pband"], out["x_dc_width"] = dc.donchian_channel_pband(), dc.donchian_channel_wband()

    out["x_cmf"] = ta.volume.ChaikinMoneyFlowIndicator(high=h, low=lo, close=c, volume=v, window=20).chaikin_money_flow()
    fi = ta.volume.ForceIndexIndicator(close=c, volume=v, window=13).force_index()
    out["x_fi"] = _ratio(fi, (c * v).rolling(20, min_periods=1).mean())
    nvi = negative_volume_index(c, v)
    out["x_nvi_dev"] = _ratio(nvi, nvi.ewm(span=255, adjust=False).mean()) - 1
    out["x_vwap_dev"] = _ratio(c, ta.volume.VolumeWeightedAveragePrice(high=h, low=lo, close=c, volume=v, window=14)
                               .volume_weighted_average_price()) - 1
    return out


def tapkg_features(frame: pl.DataFrame) -> pl.DataFrame:
    """ta 库扩展指标（x_ 前缀，33 个）。逐股票算（每只股票只看自己的历史），结果按输入顺序拼回去。"""
    feature_names()                                   # 没装 ta 库时抛中文说明
    cols: dict[str, list[np.ndarray]] = {n: [] for n in TAPKG_NAMES}
    pdf: pd.DataFrame = frame.select(["code", "open", "high", "low", "close", "volume"]).to_pandas()
    for c in ("open", "high", "low", "close", "volume"):
        pdf[c] = pdf[c].astype("float64")
    # frame 已按 (code, date) 排序：每只股票是连续的一段，按出现顺序切片，拼回去顺序不变
    codes: np.ndarray = pdf["code"].to_numpy()
    bounds: list[int] = [0, *list(np.flatnonzero(codes[1:] != codes[:-1]) + 1), len(codes)]
    with warnings.catch_warnings(), np.errstate(divide="ignore", invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        for a, b in zip(bounds[:-1], bounds[1:], strict=True):
            if b <= a:
                continue
            sub = pdf.iloc[a:b].reset_index(drop=True)
            feats = _one_stock(sub)
            for n in TAPKG_NAMES:
                cols[n].append(feats[n].to_numpy(dtype="float64", na_value=np.nan))
    data: dict[str, pl.Series] = {"code": frame["code"], "date": frame["date"]}
    for n in TAPKG_NAMES:
        arr: np.ndarray = np.concatenate(cols[n]) if cols[n] else np.empty(0)
        arr[~np.isfinite(arr)] = np.nan
        data[n] = pl.Series(n, arr, dtype=pl.Float64, nan_to_null=True).cast(pl.Float32)
    return pl.DataFrame(data)
