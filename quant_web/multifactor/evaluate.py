"""
因子检验与组合回测（研究和页面展示共用同一套口径）。

时间约定：第 t 天收盘后算信号 → 第 t+1 天开盘买入；持有到下一个调仓日 t' 的次日开盘卖出。
- 可交易股票池：主板、当天非 ST、上市满 250 个交易日、当天有成交、近 20 日日均成交额 ≥ min_amount；
- 买不进：次日停牌或开盘即涨停 → 顺延到排名下一只；卖不出：停牌或开盘即跌停 → 继续持有到能卖为止；
- 费用：佣金双边 0.025%、印花税（卖出，2023-08-28 前 0.1%，之后 0.05%）、滑点双边 slippage；
- 对照组"随机"：同一可交易股票池里等权持有全部股票（= 随机挑股票的期望收益），
  以及 N 次蒙特卡洛随机挑同样数量股票、同样换手规则的组合。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..predict.costs import COMMISSION, stamp_duty
from .panel import Panel


@dataclass
class Universe:
    min_listed: int = 250
    min_amount: float = 2e7          # 近20日日均成交额（元）
    exclude_st: bool = True


def rebalance_dates(dates: pd.DatetimeIndex, freq: str, start: str | None = None, end: str | None = None) -> pd.DatetimeIndex:
    """调仓信号日：W = 每周最后一个交易日；M = 每月最后一个交易日；2W = 隔周"""
    s = pd.Series(dates, index=dates)
    if freq in ("W", "2W"):
        key = dates.to_period("W-FRI")
    elif freq == "M":
        key = dates.to_period("M")
    else:
        raise ValueError(freq)
    last = s.groupby(key).max()
    out = pd.DatetimeIndex(last.values)
    if freq == "2W":
        out = out[::2]
    out = out[out < dates[-1]]           # 最后一天没有"次日开盘"
    if start:
        out = out[out >= pd.Timestamp(start)]
    if end:
        out = out[out <= pd.Timestamp(end)]
    return out


def eligible(p: Panel, u: Universe, avg_amount: pd.DataFrame | None = None) -> pd.DataFrame:
    amt = avg_amount if avg_amount is not None else p.px["amount"].rolling(20, min_periods=15).mean()
    ok = p.trading & (p.listed_days >= u.min_listed) & (amt >= u.min_amount)
    if u.exclude_st:
        ok &= ~p.st
    return ok


def forward_open_returns(p: Panel, sig_dates: pd.DatetimeIndex) -> pd.DataFrame:
    """每个信号日 t → (t+1 开盘 买入) 到 (下一个信号日 t' 的次日开盘 卖出) 的复权收益。

    买入日停牌/开盘涨停 → NaN（买不到）；卖出日停牌 → 顺延到之后第一个开盘价（退市则用最后收盘价）。
    """
    pos = p.dates.get_indexer(sig_dates)
    entry_pos = pos + 1
    exit_pos = np.append(pos[1:] + 1, len(p.dates) - 1)
    exit_pos = np.minimum(exit_pos, len(p.dates) - 1)
    ao = p.adj_open.values
    # 卖出价：第一个 ≥ exit_pos 的有效开盘价；之后都没有 → 最后的复权收盘价
    ao_bf = pd.DataFrame(ao).bfill().values
    last_close = p.adj_close.ffill().values
    buyable = p.trading.values & ~p.limit_up_open.values
    rows = []
    for e, x in zip(entry_pos, exit_pos, strict=True):
        if e >= len(p.dates):
            rows.append(np.full(len(p.codes), np.nan))
            continue
        buy = np.where(buyable[e], ao[e], np.nan)
        sell = ao_bf[x]
        sell = np.where(np.isnan(sell), last_close[-1], sell)
        rows.append(sell / buy - 1.0)
    return pd.DataFrame(np.vstack(rows), index=sig_dates, columns=p.codes)


def rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame, mask: pd.DataFrame) -> pd.Series:
    f = factor.reindex(fwd.index).where(mask.reindex(fwd.index))
    y = fwd.where(f.notna())
    f = f.where(y.notna())
    fr = f.rank(axis=1)
    yr = y.rank(axis=1)
    fr = fr.sub(fr.mean(axis=1), axis=0)
    yr = yr.sub(yr.mean(axis=1), axis=0)
    num = (fr * yr).sum(axis=1)
    den = np.sqrt((fr ** 2).sum(axis=1) * (yr ** 2).sum(axis=1))
    ic = num / den
    ic[f.notna().sum(axis=1) < 100] = np.nan
    return ic


def nw_t(x: pd.Series, lags: int = 0) -> float:
    """Newey-West t 值（不重叠的持有期收益用 lags=0 即普通 t）"""
    x = x.dropna().values
    n = len(x)
    if n < 5:
        return float("nan")
    d = x - x.mean()
    s = (d @ d) / n
    for lag in range(1, lags + 1):
        w = 1 - lag / (lags + 1)
        s += 2 * w * (d[lag:] @ d[:-lag]) / n
    return float(x.mean() / np.sqrt(s / n))


def quantile_returns(score: pd.DataFrame, fwd: pd.DataFrame, mask: pd.DataFrame, q: int = 5) -> pd.DataFrame:
    """每期按分数分 q 组的等权收益（第 q 组分数最高），附 'all' = 全池等权"""
    s = score.reindex(fwd.index).where(mask.reindex(fwd.index))
    y = fwd.where(s.notna())
    s = s.where(y.notna())
    pct = s.rank(axis=1, pct=True)
    out = {}
    for k in range(q):
        lo, hi = k / q, (k + 1) / q
        sel = (pct > lo) & (pct <= hi)
        out[f"Q{k + 1}"] = y.where(sel).mean(axis=1)
    out["all"] = y.mean(axis=1)
    return pd.DataFrame(out)


def zscore(df: pd.DataFrame, mask: pd.DataFrame | None = None) -> pd.DataFrame:
    """截面标准化：先转成百分位排名（抗极值），再映射到 [-1.73, 1.73] 的均匀分布 z 分"""
    x = df if mask is None else df.where(mask)
    pct = x.rank(axis=1, pct=True)
    n = x.notna().sum(axis=1)
    pct = pct.sub(0.5 / n.replace(0, np.nan), axis=0)
    return (pct - 0.5) * np.sqrt(12)


def composite(factors: dict[str, pd.DataFrame], weights: dict[str, float], directions: dict[str, int],
              mask: pd.DataFrame, dates: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """加权合成分：每个因子乘方向后截面标准化，缺失按 0（中性）处理；至少要有一半权重的因子有值"""
    total = None
    wsum = None
    for key, w in weights.items():
        if w == 0:
            continue
        f = factors[key] if dates is None else factors[key].reindex(dates)
        m = mask if dates is None else mask.reindex(dates)
        z = zscore(f * directions[key], m)
        has = z.notna().astype(float) * abs(w)
        z = z.fillna(0.0) * w
        total = z if total is None else total + z
        wsum = has if wsum is None else wsum + has
    wabs = sum(abs(w) for w in weights.values())
    score = total.where(wsum >= 0.5 * wabs)
    m = mask if dates is None else mask.reindex(dates)
    return score.where(m)


@dataclass
class BacktestResult:
    nav: pd.Series                         # 策略净值（扣费）
    bench_nav: pd.Series                   # 同池等权（"随机"期望）
    period_ret: pd.Series
    bench_ret: pd.Series
    turnover: pd.Series
    holdings: dict[pd.Timestamp, list[str]] = field(default_factory=dict)
    random_navs: pd.DataFrame | None = None

    def stats(self, start: str | None = None, end: str | None = None, periods_per_year: float = 12) -> dict:
        r = self.period_ret
        b = self.bench_ret
        if start:
            r, b = r[r.index >= pd.Timestamp(start)], b[b.index >= pd.Timestamp(start)]
        if end:
            r, b = r[r.index <= pd.Timestamp(end)], b[b.index <= pd.Timestamp(end)]
        ex = r - b
        n = len(r)
        yrs = n / periods_per_year
        nav = (1 + r).cumprod()
        bnav = (1 + b).cumprod()
        dd = (nav / nav.cummax() - 1).min()
        bdd = (bnav / bnav.cummax() - 1).min()
        out = {
            "periods": n,
            "cagr": float(nav.iloc[-1] ** (1 / yrs) - 1) if n else float("nan"),
            "bench_cagr": float(bnav.iloc[-1] ** (1 / yrs) - 1) if n else float("nan"),
            "excess_ann": float(ex.mean() * periods_per_year),
            "excess_t": nw_t(ex),
            "hit": float((ex > 0).mean()),
            "maxdd": float(dd),
            "bench_maxdd": float(bdd),
            "vol": float(r.std() * np.sqrt(periods_per_year)),
            "turnover": float(self.turnover.reindex(r.index).mean()),
        }
        if self.random_navs is not None and len(self.random_navs.columns):
            rr = self.random_navs.pct_change().fillna(self.random_navs.iloc[0] - 1)
            rr = rr.loc[r.index]
            rnav = (1 + rr).prod()
            out["random_pct"] = float((rnav < nav.iloc[-1]).mean())      # 策略跑赢了多少比例的随机组合
            out["random_median_cagr"] = float(np.median(rnav ** (1 / yrs) - 1))
        return out


def impact_slippage(p: Panel, capital: float, top_n: int, base: float = 0.0005, k: float = 0.8,
                    cap: float = 0.02) -> np.ndarray:
    """按资金规模估算每只股票每天的单边冲击成本（平方根模型）：
    滑点 = 0.05%（约半个买卖价差）+ k × 20日波动率 × sqrt(单只下单金额 / 20日日均成交额)，上限 2%"""
    adv = p.px["amount"].rolling(20, min_periods=10).mean().values
    vol = p.ret.clip(-0.2, 0.2).rolling(20, min_periods=10).std().values
    part = (capital / max(top_n, 1)) / adv
    slip = base + k * np.nan_to_num(vol, nan=0.03) * np.sqrt(np.clip(np.nan_to_num(part, nan=1.0), 0, None))
    return np.clip(slip, base, cap)


def participation(p: Panel, capital: float, top_n: int) -> pd.DataFrame:
    """单只下单金额占 20 日日均成交额的比例"""
    adv = p.px["amount"].rolling(20, min_periods=10).mean()
    return (capital / max(top_n, 1)) / adv


def backtest(p: Panel, score: pd.DataFrame, mask: pd.DataFrame, sig_dates: pd.DatetimeIndex,
             top_n: int = 30, keep_rank: int | None = None, slippage: float | np.ndarray = 0.0015,
             n_random: int = 0, seed: int = 7, industry_cap: int | None = None) -> BacktestResult:
    """等权持有分数最高的 top_n 只；已持有的只要排名还在 keep_rank 以内就不卖（降低换手）。

    逐期模拟：t+1 开盘按顺序买，停牌/开盘涨停的跳过换下一只；要卖的股票停牌或开盘跌停就继续拿着。
    同时算同池等权基准（每期全池等权，按同样的开盘价买卖，但不扣费：对基准更有利，结论更保守）。
    """
    keep_rank = keep_rank or top_n
    D = p.dates
    ao = p.adj_open.values
    ao_bf = pd.DataFrame(ao).bfill().values
    last_close = p.adj_close.ffill().values
    buyable = p.trading.values & ~p.limit_up_open.values
    sellable = p.trading.values & ~p.limit_down_open.values
    codes = np.asarray(p.codes)
    col = {c: i for i, c in enumerate(codes)}
    ind_arr = p.industry.reindex(p.codes).fillna("未知").values
    slip_arr = slippage if isinstance(slippage, np.ndarray) else None
    pos = D.get_indexer(sig_dates)

    def value_at(i: int, cols: list[int]) -> np.ndarray:
        v = ao_bf[i, cols]
        return np.where(np.isnan(v), last_close[-1, cols], v)

    def one_run(pick_fn) -> tuple[list[float], list[float], dict]:
        held: list[int] = []
        rets, turns = [], []
        hold_log: dict = {}
        for k, t in enumerate(pos):
            e = t + 1
            x = pos[k + 1] + 1 if k + 1 < len(pos) else len(D) - 1
            x = min(x, len(D) - 1)
            if e >= len(D):
                break
            want = pick_fn(k, t)                   # 排好序的候选列号
            rank_of = {c: i for i, c in enumerate(want)}
            # 卖出：不在 keep 范围内且今天能卖
            keep, sell = [], []
            for c in held:
                if rank_of.get(c, 10 ** 9) < keep_rank or not sellable[e, c]:
                    keep.append(c)
                else:
                    sell.append(c)
            slots = top_n - len(keep)
            buys = []
            ind_n: dict = {}
            if industry_cap:
                for c in keep:
                    ind_n[ind_arr[c]] = ind_n.get(ind_arr[c], 0) + 1
            for c in want:
                if slots <= 0:
                    break
                if c in keep or not buyable[e, c]:
                    continue
                if industry_cap and ind_n.get(ind_arr[c], 0) >= industry_cap:
                    continue
                buys.append(c)
                if industry_cap:
                    ind_n[ind_arr[c]] = ind_n.get(ind_arr[c], 0) + 1
                slots -= 1
            new = keep + buys
            # 本期收益：等权（每期再平衡到等权；保留的股票不产生交易费用，只对新买/卖出的部分收费）
            if new:
                sell_px = value_at(x, new)
                # 新买入：t+1 开盘价；继续持有：与上期卖出估值同一口径（第一个可用开盘价），收益首尾相接
                buy_px = np.where(np.isin(new, keep), value_at(e, new), ao[e, new])
                r = sell_px / buy_px - 1
                r = np.where(np.isnan(r), 0.0, r)
                gross = float(np.mean(r))
            else:
                gross = 0.0
            n_trade = len(sell) + len(buys)
            tw = n_trade / max(top_n, 1) / 2                     # 单边换手率
            if slip_arr is None:
                sb, ss = np.full(len(buys), float(slippage)), np.full(len(sell), float(slippage))
            else:
                sb = slip_arr[e, buys] if buys else np.zeros(0)
                ss = slip_arr[e, sell] if sell else np.zeros(0)
            cost = (float(np.sum(COMMISSION + sb)) + float(np.sum(COMMISSION + ss + stamp_duty(D[e].date())))) / max(top_n, 1)
            if not new:
                cost = 0.0
            rets.append((1 + gross) * (1 - cost) - 1)
            turns.append(tw)
            hold_log[D[t]] = [codes[c] for c in new]
            held = new
        return rets, turns, hold_log

    sc = score.reindex(sig_dates)
    mk = mask.reindex(sig_dates)

    def pick_score(k: int, t: int) -> list[int]:
        row = sc.iloc[k].where(mk.iloc[k])
        row = row.dropna().sort_values(ascending=False)
        return [col[c] for c in row.index[: max(keep_rank, top_n) * 3]]

    rets, turns, hold_log = one_run(pick_score)
    idx = sig_dates[: len(rets)]
    period_ret = pd.Series(rets, index=idx)
    fwd = forward_open_returns(p, sig_dates).reindex(idx)
    bench = fwd.where(mk.reindex(idx)).mean(axis=1)

    rand_navs = None
    if n_random:
        # 随机对照：每只股票一个随机分，每期按策略的平均换手率重新抽一部分，使随机组合的换手与策略相近
        rng = np.random.default_rng(seed)
        navs = {}
        mk_arr = mk.fillna(False).values.astype(bool)
        redraw = float(np.clip(np.mean(turns) if turns else 1.0, 0.02, 1.0))
        for j in range(n_random):
            keys = rng.random(len(codes))

            def pick_rand(k: int, t: int) -> list[int]:
                nonlocal keys
                if k:
                    flip = rng.random(len(codes)) < redraw
                    keys = np.where(flip, rng.random(len(codes)), keys)
                elig = np.flatnonzero(mk_arr[k])
                order = elig[np.argsort(-keys[elig])]
                return [int(c) for c in order[: max(keep_rank, top_n) * 3]]

            rr, _, _ = one_run(pick_rand)
            navs[j] = (1 + pd.Series(rr, index=idx)).cumprod()
        rand_navs = pd.DataFrame(navs)
    return BacktestResult(nav=(1 + period_ret).cumprod(), bench_nav=(1 + bench).cumprod(), period_ret=period_ret,
                          bench_ret=bench, turnover=pd.Series(turns, index=idx), holdings=hold_log,
                          random_navs=rand_navs)
