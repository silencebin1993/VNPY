"""
选股因子库（全部只用当天收盘及以前的数据；宽表：行 = 交易日，列 = 股票）。

方向 direction 来自公开研究的先验（+1 数值越大越好，-1 越小越好），不根据本机回测结果调整：
回测只用来检验"文献里说有效的东西在最近几年的主板上还有没有效"，不用来挑方向，避免数据挖掘。
"""
from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .panel import Panel, fund_asof, load_fund_long, single_quarter


@dataclass(frozen=True)
class Factor:
    key: str
    label: str
    group: str
    direction: int
    desc: str
    fn: Callable[[Ctx], pd.DataFrame]


GROUPS: dict[str, str] = {
    "reversal": "反转",
    "activity": "低关注度（换手）",
    "risk": "低波动/低彩票性",
    "value": "估值",
    "quality": "盈利质量",
    "surprise": "业绩超预期",
    "size": "市值",
    "sentiment": "情绪/资金行为",
    "trend": "趋势",
}


class Ctx:
    """因子计算的公共中间量（懒加载、缓存）"""

    def __init__(self, panel: Panel) -> None:
        self.p = panel
        self._cache: dict[str, pd.DataFrame] = {}

    def get(self, key: str, make: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    @property
    def r(self) -> pd.DataFrame:
        return self.p.ret

    @property
    def lr(self) -> pd.DataFrame:
        return self.get("lr", lambda: np.log1p(self.p.ret.clip(lower=-0.95)))

    @property
    def mkt(self) -> pd.Series:
        """主板等权市场日收益（剔除当日 ST、上市不满 60 天）"""
        def make() -> pd.DataFrame:
            ok = self.p.trading & ~self.p.st & (self.p.listed_days >= 60)
            return self.p.ret.where(ok).clip(-0.2, 0.2).mean(axis=1).to_frame("m")
        return self.get("mkt", make)["m"]

    @property
    def float_cap(self) -> pd.DataFrame:
        """流通市值（元）：流通股本 = 成交量 / 换手率，取 20 日中位数去掉换手率四舍五入的噪声"""
        def make() -> pd.DataFrame:
            turn = self.p.px["turn"]
            shares = (self.p.px["volume"] / (turn / 100.0)).where(turn >= 0.05)
            shares = shares.rolling(20, min_periods=3).median().ffill(limit=60)
            return shares * self.p.px["close"]
        return self.get("float_cap", make)

    @property
    def fund(self) -> dict[str, pd.DataFrame]:
        return self.get("fund", self._make_fund)  # type: ignore[return-value]

    def _make_fund(self) -> dict[str, pd.DataFrame]:  # type: ignore[override]
        f = load_fund_long()
        f = f[f["code"].isin(self.p.codes)].copy()
        # 单季度净利润与同比变化 → SUE（最近 8 个同比变化的标准差做分母）
        f["np_q"] = single_quarter(f, "net_profit")
        f["q_key"] = f["report_date"].dt.month
        f = f.sort_values(["code", "report_date"])
        f["np_q_ly"] = f.groupby(["code", "q_key"])["np_q"].shift(1)
        f["d_np"] = f["np_q"] - f["np_q_ly"]
        f["d_std"] = f.groupby("code")["d_np"].transform(lambda s: s.rolling(8, min_periods=4).std())
        f["sue"] = f["d_np"] / f["d_std"].replace(0, np.nan)
        f["rev_q"] = single_quarter(f, "revenue")
        f["rev_q_ly"] = f.groupby(["code", "q_key"])["rev_q"].shift(1)
        f["rev_q_yoy"] = (f["rev_q"] - f["rev_q_ly"]) / f["rev_q_ly"].abs().replace(0, np.nan)
        f["accrual"] = (f["eps"] - f["ocfps"]) / f["bvps"].where(f["bvps"] > 0)
        cols = ["sue", "rev_q_yoy", "net_profit_yoy", "revenue_yoy", "gross_margin", "debt_ratio", "accrual",
                "roe", "net_margin", "net_profit_ttm", "revenue_ttm"]
        out = fund_asof(f, {c: f[c] for c in cols}, self.p.dates, self.p.codes)
        return out  # type: ignore[return-value]

    def val(self, key: str) -> pd.DataFrame:
        v = self.p.valuation.get(key)
        if v is None:
            return pd.DataFrame(np.nan, index=self.p.dates, columns=self.p.codes)
        return v.ffill(limit=5)


def _roll(df: pd.DataFrame, w: int, how: str, min_frac: float = 0.75) -> pd.DataFrame:
    r = df.rolling(w, min_periods=max(2, int(w * min_frac)))
    return getattr(r, how)()


def _sliding(arr: np.ndarray, w: int) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(arr, w, axis=0)       # (T-w+1, N, w)


def ideal_amplitude(c: Ctx, w: int = 20, q: float = 0.25) -> pd.DataFrame:
    """理想振幅（开源证券 2020）：近 w 日按收盘价高低分组，高价日平均振幅 - 低价日平均振幅"""
    p = c.p
    amp = ((p.px["high"] - p.px["low"]) / p.px["preclose"]).values
    close = p.adj_close.where(p.trading).values
    out = np.full(amp.shape, np.nan)
    k = max(1, int(round(w * q)))
    step = 200
    for s in range(w - 1, len(amp), step):
        e = min(len(amp), s + step)
        cw = _sliding(close[s - w + 1:e], w)          # (e-s, N, w)
        aw = _sliding(amp[s - w + 1:e], w)
        valid = ~np.isnan(cw) & ~np.isnan(aw)
        cnt = valid.sum(axis=2)
        cw2 = np.where(valid, cw, np.nan)
        order = np.argsort(np.where(valid, cw2, np.inf), axis=2)          # 低 → 高，无效值排最后
        a_sorted = np.take_along_axis(np.where(valid, aw, np.nan), order, axis=2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)          # 全部停牌的窗口：均值为空，下面置 NaN
            low = np.nanmean(a_sorted[:, :, :k], axis=2)
            # 高价日：每行有效值的最后 k 个
            idx_hi = (cnt[:, :, None] - 1 - np.arange(k)[None, None, :]).clip(min=0)
            high = np.nanmean(np.take_along_axis(a_sorted, idx_hi, axis=2), axis=2)
        res = high - low
        res[cnt < int(w * 0.75)] = np.nan
        out[s:e] = res
    return pd.DataFrame(out, index=p.dates, columns=p.codes)


def turnover_split_return(c: Ctx, w: int = 20) -> pd.DataFrame:
    """高换手日收益（华泰"换手率切割反转"的日线版）：近 w 日里换手最高的一半交易日的对数收益之和"""
    p = c.p
    turn = p.px["turn"].values
    lr = c.lr.values
    out = np.full(turn.shape, np.nan)
    half = w // 2
    step = 200
    for s in range(w - 1, len(turn), step):
        e = min(len(turn), s + step)
        tw = _sliding(turn[s - w + 1:e], w)
        rw = _sliding(lr[s - w + 1:e], w)
        valid = ~np.isnan(tw) & ~np.isnan(rw)
        order = np.argsort(np.where(valid, -tw, np.inf), axis=2)          # 换手从高到低
        r_sorted = np.take_along_axis(np.where(valid, rw, 0.0), order, axis=2)
        res = r_sorted[:, :, :half].sum(axis=2)
        res[valid.sum(axis=2) < int(w * 0.75)] = np.nan
        out[s:e] = res
    return pd.DataFrame(out, index=p.dates, columns=p.codes)


def ideal_reversal(c: Ctx, w: int = 20) -> pd.DataFrame:
    """理想反转（国盛金工"量价淘金"系列的日线版）：高换手那一半交易日的收益和 - 低换手那一半的收益和。

    放量日的涨跌多是情绪推动、容易回吐（反转），缩量日的涨跌更像信息慢慢被消化（延续），两者相减把反转部分提纯。
    """
    p = c.p
    turn = p.px["turn"].values
    lr = c.lr.values
    out = np.full(turn.shape, np.nan)
    half = w // 2
    step = 200
    for s in range(w - 1, len(turn), step):
        e = min(len(turn), s + step)
        tw = _sliding(turn[s - w + 1:e], w)
        rw = _sliding(lr[s - w + 1:e], w)
        valid = ~np.isnan(tw) & ~np.isnan(rw)
        cnt = valid.sum(axis=2)
        order = np.argsort(np.where(valid, -tw, np.inf), axis=2)
        r_sorted = np.take_along_axis(np.where(valid, rw, 0.0), order, axis=2)
        pos = np.arange(w)[None, None, :]
        k = (cnt // 2)[:, :, None]
        hi = np.where(pos < k, r_sorted, 0.0).sum(axis=2)
        lo = np.where((pos >= cnt[:, :, None] - k) & (pos < cnt[:, :, None]), r_sorted, 0.0).sum(axis=2)
        res = hi - lo
        res[cnt < int(w * 0.75)] = np.nan
        out[s:e] = res
    del half
    return pd.DataFrame(out, index=p.dates, columns=p.codes)


def overnight_turn_corr(c: Ctx, w: int = 20) -> pd.DataFrame:
    """隔夜跳空与前一日换手的相关性（国盛金工 2022 "MIF" 的日线版）：放量后第二天大幅跳空的股票后续偏弱"""
    gap = np.log(c.p.px["open"] / c.p.px["preclose"]).abs()
    prev_turn = c.p.px["turn"].shift(1)
    return gap.rolling(w, min_periods=int(w * 0.75)).corr(prev_turn)


def ivol(c: Ctx, w: int = 20) -> pd.DataFrame:
    """特质波动率：对等权市场收益回归的残差标准差 = σ_i × sqrt(1-ρ²)"""
    r = c.r.clip(-0.2, 0.2)
    m = c.mkt
    sd = _roll(r, w, "std")
    rho = r.rolling(w, min_periods=int(w * 0.75)).corr(m)
    return sd * np.sqrt((1 - rho ** 2).clip(lower=0))


def beta(c: Ctx, w: int = 60) -> pd.DataFrame:
    r = c.r.clip(-0.2, 0.2)
    m = c.mkt
    cov = r.rolling(w, min_periods=int(w * 0.75)).cov(m)
    var = m.rolling(w, min_periods=int(w * 0.75)).var()
    return cov.div(var, axis=0)


def _inv(x: pd.DataFrame) -> pd.DataFrame:
    """估值倒数（EP/BP/SP）：市盈率为负时 EP 为负，0 → NaN"""
    return 1.0 / x.where(x != 0)


def _sum_top_k(r: pd.DataFrame, w: int, k: int) -> pd.DataFrame:
    arr = r.values
    out = np.full(arr.shape, np.nan)
    step = 300
    for s in range(w - 1, len(arr), step):
        e = min(len(arr), s + step)
        win = _sliding(arr[s - w + 1:e], w)
        valid = ~np.isnan(win)
        srt = np.sort(np.where(valid, win, -np.inf), axis=2)[:, :, ::-1][:, :, :k]
        res = np.where(np.isinf(srt), np.nan, srt).mean(axis=2)
        res[valid.sum(axis=2) < int(w * 0.75)] = np.nan
        out[s:e] = res
    return pd.DataFrame(out, index=r.index, columns=r.columns)


FACTORS: list[Factor] = [
    # 反转
    Factor("rev_5", "5日涨幅", "reversal", -1, "近5个交易日累计对数收益；A股短期反转最强（Jansen 等 2021）",
           lambda c: _roll(c.lr, 5, "sum", 0.6)),
    Factor("rev_20", "20日涨幅", "reversal", -1, "近1个月累计收益；A股月度反转（Liu-Stambaugh-Yuan 2019、Hou 等 2023）",
           lambda c: _roll(c.lr, 20, "sum")),
    Factor("rev_60", "60日涨幅", "reversal", -1, "近3个月累计收益（中期反转）",
           lambda c: _roll(c.lr, 60, "sum")),
    Factor("rev_hiturn", "高换手日涨幅", "reversal", -1, "近20日换手最高的10天的收益之和：放量上涨更容易回吐（换手率切割反转）",
           turnover_split_return),
    Factor("ideal_rev", "理想反转", "reversal", -1, "高换手日收益和 - 低换手日收益和（国盛金工 理想反转，日线版）",
           ideal_reversal),
    # 低关注度
    Factor("turn_20", "20日换手率", "activity", -1, "近1个月日均换手率；低换手股长期跑赢（CH-4 换手因子）",
           lambda c: _roll(c.p.px["turn"], 20, "mean")),
    Factor("abn_turn", "异常换手", "activity", -1, "近1个月日均换手 / 近1年日均换手（Liu-Stambaugh-Yuan 情绪因子）",
           lambda c: _roll(c.p.px["turn"], 20, "mean") / _roll(c.p.px["turn"], 250, "mean", 0.5)),
    Factor("turn_std", "换手率波动", "activity", -1, "近1个月日换手率的标准差（东吴/华泰 换手率波动）",
           lambda c: _roll(c.p.px["turn"], 20, "std")),
    # 低风险 / 彩票性
    Factor("vol_20", "20日波动率", "risk", -1, "近1个月日收益标准差（低波动异象）",
           lambda c: _roll(c.r.clip(-0.2, 0.2), 20, "std")),
    Factor("ivol_20", "特质波动率", "risk", -1, "剔除市场涨跌后的残差波动（Ang 等 2006；A股显著）", ivol),
    Factor("max_20", "最大日涨幅", "risk", -1, "近1个月涨幅最大的3天的平均（彩票型股票被高估，Bali 等 2011）",
           lambda c: _sum_top_k(c.r.clip(-0.2, 0.2), 20, 3)),
    Factor("skew_20", "收益偏度", "risk", -1, "近1个月日收益偏度（正偏的彩票股未来收益低）",
           lambda c: _roll(c.r.clip(-0.2, 0.2), 20, "skew")),
    Factor("amp_20", "20日振幅", "risk", -1, "近1个月日均振幅（最高-最低）/昨收",
           lambda c: _roll((c.p.px["high"] - c.p.px["low"]) / c.p.px["preclose"], 20, "mean")),
    Factor("ideal_amp", "理想振幅", "risk", -1, "高价日振幅减低价日振幅（开源证券 2020）", ideal_amplitude),
    Factor("beta_60", "Beta", "risk", -1, "近3个月对市场的 Beta（低 Beta 异象）", beta),
    Factor("zt_60", "近期涨停次数", "risk", -1, "近3个月收盘涨停次数（炒作过的股票后续偏弱）",
           lambda c: _roll(c.p.limit_up_close.astype(float).where(c.p.trading), 60, "sum", 0.5)),
    # 情绪 / 资金行为
    Factor("overnight_20", "隔夜收益", "sentiment", -1, "近1个月开盘跳空收益之和（散户情绪，Aboody 等 2018）",
           lambda c: _roll(np.log(c.p.px["open"] / c.p.px["preclose"]), 20, "sum")),
    Factor("intraday_20", "日内收益", "sentiment", -1, "近1个月开盘到收盘收益之和",
           lambda c: _roll(np.log(c.p.px["close"] / c.p.px["open"]), 20, "sum")),
    Factor("gap_turn_corr", "跳空-换手相关", "sentiment", -1, "近1个月 |隔夜跳空| 与前一日换手率的相关系数（国盛金工 MIF）",
           overnight_turn_corr),
    Factor("pv_corr", "价量相关性", "sentiment", -1, "近1个月收盘价与成交量的相关系数（量价背离的更好）",
           lambda c: c.p.px["close"].rolling(20, min_periods=15).corr(c.p.px["volume"])),
    Factor("amihud", "非流动性", "sentiment", 1, "近1个月 |收益|/成交额（Amihud 2002，流动性溢价）",
           lambda c: _roll(c.r.abs() / c.p.px["amount"] * 1e8, 20, "mean")),
    # 趋势（A股文献中动量弱，作为对照组）
    Factor("mom_12_1", "12-1月动量", "trend", 1, "过去12个月剔除最近1个月的涨幅（美股动量；A股通常无效，作对照）",
           lambda c: _roll(c.lr, 230, "sum", 0.6).shift(20)),
    Factor("high_250", "距一年新高", "trend", 1, "收盘价 / 近一年最高收盘价（52周新高效应）",
           lambda c: c.p.adj_close / _roll(c.p.adj_close, 250, "max", 0.5)),
    # 市值 / 价格
    Factor("size", "流通市值", "size", -1, "流通市值取对数（小市值效应；注意壳价值与流动性风险）",
           lambda c: np.log(c.float_cap)),
    Factor("low_price", "股价", "size", -1, "收盘价取对数（低价股效应）",
           lambda c: np.log(c.p.px["close"].ffill(limit=5))),
    # 估值（baostock 官方口径）
    Factor("ep", "盈利收益率 E/P", "value", 1, "市盈率TTM 的倒数（CH-3 价值因子用 E/P，Liu-Stambaugh-Yuan 2019）",
           lambda c: _inv(c.val("pe_ttm"))),
    Factor("bp", "账面市值比 B/P", "value", 1, "市净率的倒数", lambda c: _inv(c.val("pb"))),
    Factor("sp", "营收市值比 S/P", "value", 1, "市销率TTM 的倒数", lambda c: _inv(c.val("ps_ttm"))),
    Factor("cfp", "现金流市值比", "value", 1, "市现率TTM 的倒数", lambda c: _inv(c.val("pcf"))),
    # 盈利质量
    Factor("roe_ttm", "ROE(TTM)", "quality", 1, "市净率/市盈率TTM = 近四季净利润/净资产",
           lambda c: (c.val("pb") / c.val("pe_ttm")).where(c.val("pb") > 0)),
    Factor("gross_margin", "毛利率", "quality", 1, "最新财报毛利率（Novy-Marx 盈利因子）", lambda c: c.fund["gross_margin"]),
    Factor("accrual", "应计利润", "quality", -1, "(每股收益-每股经营现金流)/每股净资产：利润里现金含量低的更差（Sloan 1996）",
           lambda c: c.fund["accrual"]),
    Factor("debt_ratio", "资产负债率", "quality", -1, "最新财报资产负债率", lambda c: c.fund["debt_ratio"]),
    # 业绩超预期 / 成长
    Factor("sue", "标准化未预期盈利 SUE", "surprise", 1,
           "单季净利润同比增量 / 过去8个季度增量的标准差（盈余公告后漂移 PEAD）", lambda c: c.fund["sue"]),
    Factor("np_yoy", "净利润同比", "surprise", 1, "最新财报累计净利润同比增速", lambda c: c.fund["net_profit_yoy"]),
    Factor("rev_q_yoy", "单季营收同比", "surprise", 1, "最新单季营业收入同比增速", lambda c: c.fund["rev_q_yoy"]),
]

BY_KEY: dict[str, Factor] = {f.key: f for f in FACTORS}


def compute(ctx: Ctx, keys: list[str] | None = None) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for f in FACTORS:
        if keys is not None and f.key not in keys:
            continue
        v = f.fn(ctx)
        out[f.key] = v.replace([np.inf, -np.inf], np.nan).astype(np.float32)
    return out
