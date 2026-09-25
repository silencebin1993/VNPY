"""
按用户设置、A股真实规则回放样本外预测（只用滚动训练中没参与训练的时间段）。

规则：
- T 日收盘出信号（按综合分从高到低取 top_n 且 ≥ threshold）→ T+1 开盘买入；
  T+1 停牌、开盘即涨停（含一字）买不到；开盘涨幅 > max_gap_pct(%) 放弃；持仓已满/已持有/资金不足跳过；
- 每只买入金额 = 前一日收盘总资产 × position_pct，按 100 股一手向下取整（科创板也按 100 股近似）；
- 卖出（T+1 买入当天不能卖）：next_open = 买入后下一个交易日开盘；next_close = 下一个交易日收盘；
  until_break = 某天收盘没涨停则下一个交易日开盘卖，最多持有 10 个交易日；
  until_break_close = 从买入后第 2 个交易日起，第一个收盘没涨停的交易日按收盘价卖，第 5 个交易日收盘强制卖
  （收盘跌停卖不出顺延到下一个收盘——已决定卖出就锁定，下一个收盘不管涨停与否都卖；第 7 个交易日仍跌停也按收盘价计）；
- 止损（stop_loss_pct>0）：买入后的交易日若最低价 ≤ 买入价×(1-stop_loss_pct)，按 min(开盘价, 止损价) 卖出；
- 要在开盘卖但开盘跌停：当天最高价高于跌停价（打开过）按跌停价（开盘价）成交；一字跌停卖不出 → 下一个交易日开盘再卖；
  要在收盘卖但收盘跌停、盘中止损遇一字跌停 → 下一个交易日开盘再卖；
- 持仓股票退市（之后没有行情）→ 按最后收盘价卖出；
- 费用：佣金 fee_rate 双边（每笔最低 5 元）、印花税（默认按日期：2023-08-28 前 0.1%，之后 0.05%）、滑点 slippage 双边；
- 卖出所得当天可用于买入（A股资金 T+0 可用）；每日按收盘价计算总资产（停牌按最后收盘价）。

另外给出：按日收益 t 值（metrics.daily_t）、同一候选池同一规则随机挑选的基准（baseline：每天都随机挑 top_n 只，
不看门槛——包括策略因门槛"不操作"的日子，20 次平均）、同日同数量随机基准（baseline_matched：只在策略出信号的日子、
每天随机挑和策略同样多只，把"挑股票"和"挑日子"分开）、选择期/留出期分开的结果（by_period，2025-07-01 为分界，
策略和两种随机基准在每一段都各用一个全新账户单独回测）。
skipped_lot：单只仓位预算连一手都买不起（本金小、股价高时常见）。
metrics.fill_rate：买得进的信号占比（买不进 = 开盘涨停/一字 unfilled + 开盘涨幅超限 skipped_gap + 停牌 suspended；
因仓位满/资金不足/一手太贵/已持有跳过的不算买不进）；metrics.nw_t：按日收益的 Newey-West t 值（滞后 5 天，
持有多天的策略相邻几天收益相关，比 daily_t 保守）；metrics.per_trade_uncapped：同一批信号逐笔成交
（simulate_trades，不受资金/仓位/一手限制、按比例收费）的 {trades, avg_return, win_rate}；
notes：因资金不足/一手太贵跳过的信号超过 20% 时给出中文提示（整体结果主要代表前一段时间）。

simulate_trades：逐笔（不受资金/仓位限制）的同规则成交模拟，给模拟盘用（见 execution 模块）。
"""
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from . import costs as costs_mod
from . import stats
from .costs import Costs
from .execution import (
    DELAYED_SUFFIX, MAX_HOLD_DAYS, R_BREAK, R_BREAK_CLOSE, R_DELISTED, R_FORCE_CLOSE, R_LD_FORCE, R_MAX_HOLD,
    R_NEXT_CLOSE, R_NEXT_OPEN, R_STOP_INTRADAY, R_STOP_OPEN, REASON_TEXT, SWING_FORCE_K, SWING_MAX_K,
    load_delisted, simulate,
)
from .model import calibration
from .scoring import _as_dict, apply_weights, filter_candidates, is_regression


TOLERANCE: float = 0.005
MIN_COMMISSION: float = costs_mod.MIN_COMMISSION
LOT: int = 100
ANNUAL_DAYS: int = 244
MAX_TRADES_OUT: int = 500
TOPN: tuple[int, ...] = (1, 3, 5, 10)
BASELINE_SEEDS: int = 20
HOLDOUT_START: date = date(2025, 7, 1)          # 选择期/留出期分界（研究时冻结配置的日期）
REG_CALIB_EDGES: list[float] = [-1.0, -0.01, 0.0, 0.01, 0.02, 0.03, 0.05, 1.0]
EXIT_REASONS: dict[str, str] = {
    "next_open": REASON_TEXT[R_NEXT_OPEN],
    "next_close": REASON_TEXT[R_NEXT_CLOSE],
    "break": REASON_TEXT[R_BREAK],
    "max_hold": REASON_TEXT[R_MAX_HOLD],
    "stop_open": REASON_TEXT[R_STOP_OPEN],
    "stop_intraday": REASON_TEXT[R_STOP_INTRADAY],
    "break_close": REASON_TEXT[R_BREAK_CLOSE],
    "force_close": REASON_TEXT[R_FORCE_CLOSE],
    "ld_force": REASON_TEXT[R_LD_FORCE],
    "delisted": REASON_TEXT[R_DELISTED],
}


@dataclass
class _Position:
    code: str
    signal_date: date
    score: float
    entry_day: int              # 日期序号（1970 以来天数）
    entry_price: float          # 含滑点的买入价
    shares: int
    cost: float                 # 买入金额 + 佣金
    stop_price: float | None
    rows_held: int = 0          # 买入后已经过的该股交易日数（含买入当天）
    pending_open: str | None = None       # 计划在下一次开盘卖出的原因
    close_exit: bool = False              # 计划在下一个交易日收盘卖出
    delayed: bool = False                 # 曾因跌停卖不出而顺延
    commit_close: str | None = None       # until_break_close：已决定收盘卖出、因跌停顺延时的原因（下一个收盘必卖）
    last_close: float = 0.0


@dataclass
class _Prices:
    """候选股票按 (code, date) 排好的价格数组；按 (code, 日期序号) 查行号"""
    days: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.int64))
    span: dict[str, tuple[int, int]] = field(default_factory=dict)
    cols: dict[str, np.ndarray] = field(default_factory=dict)

    def index(self, code: str, day: int) -> int:
        s = self.span.get(code)
        if s is None:
            return -1
        lo, hi = s
        i: int = lo + int(np.searchsorted(self.days[lo:hi], day))
        return i if i < hi and self.days[i] == day else -1

    def last_day(self, code: str) -> int | None:
        s = self.span.get(code)
        return int(self.days[s[1] - 1]) if s else None

    def value(self, col: str, i: int) -> float:
        return float(self.cols[col][i])


_PRICE_COLS: list[str] = ["open", "high", "low", "close", "preclose", "limit_up", "limit_down"]


def _load_prices(panel_lim: pl.DataFrame, codes: list[str], start: date) -> _Prices:
    df: pl.DataFrame = (
        panel_lim.filter(pl.col("code").is_in(codes) & (pl.col("date") >= start))
        .select(["code", "date", *_PRICE_COLS])
        .sort(["code", "date"])
    )
    prices = _Prices()
    if df.is_empty():
        return prices
    prices.days = df["date"].cast(pl.Int32).cast(pl.Int64).to_numpy()
    prices.cols = {c: df[c].cast(pl.Float64).fill_null(np.nan).to_numpy() for c in _PRICE_COLS}
    code_arr: np.ndarray = df["code"].to_numpy()
    starts: np.ndarray = np.flatnonzero(np.r_[True, code_arr[1:] != code_arr[:-1]])
    ends: np.ndarray = np.r_[starts[1:], len(code_arr)]
    prices.span = {str(code_arr[s]): (int(s), int(e)) for s, e in zip(starts, ends, strict=True)}
    return prices


_EPOCH: int = date(1970, 1, 1).toordinal()


def _day(d: date) -> int:
    return d.toordinal() - _EPOCH


def _from_day(n: int) -> date:
    return date.fromordinal(n + _EPOCH)


def _commission(amount: float, rate: float) -> float:
    return max(MIN_COMMISSION, amount * rate) if amount > 0 else 0.0


def _names(oos: pl.DataFrame) -> dict[str, str]:
    if "name" in oos.columns:
        return dict(zip(oos["code"].to_list(), oos["name"].to_list(), strict=False))
    try:
        from ..market.universe import load_universe

        uni: pl.DataFrame = load_universe()
        return dict(zip(uni["code"].to_list(), uni["name"].to_list(), strict=False))
    except Exception:  # noqa: BLE001
        return {}


def _topn_hit(cand: pl.DataFrame) -> list[dict]:
    lab: pl.DataFrame = cand.filter(pl.col("y").is_not_null()).with_columns(
        pl.col("score").rank("ordinal", descending=True).over("date").alias("_r")
    )
    out: list[dict] = []
    for n in TOPN:
        sel: pl.DataFrame = lab.filter(pl.col("_r") <= n)
        row: dict = {"n": n, "hit_rate": round(float(sel["y"].mean()), 4) if sel.height else None, "picks": sel.height}
        if "net" in sel.columns:
            net = sel.filter(pl.col("net").is_not_null())["net"]
            row["avg_return"] = round(float(net.mean()), 5) if net.len() else None
        out.append(row)
    return out


def _calibration(cand: pl.DataFrame, regression: bool) -> list[dict]:
    """分类模型：按综合概率分桶的实际涨停率；回归模型（波段）：按预测收益分桶的实际平均净收益"""
    if not cand.height or "y" not in cand.columns:
        return []
    if not regression:
        return calibration(cand, col="score")
    if "net" not in cand.columns:
        return []
    lab: pl.DataFrame = cand.filter(pl.col("net").is_not_null() & pl.col("score").is_not_null())
    rows: list[dict] = []
    for lo, hi in zip(REG_CALIB_EDGES[:-1], REG_CALIB_EDGES[1:], strict=False):
        part: pl.DataFrame = lab.filter((pl.col("score") >= lo) & (pl.col("score") < hi))
        if part.is_empty():
            continue
        name: str = (f"<{hi * 100:g}%" if lo <= -1 else f"≥{lo * 100:g}%" if hi >= 1
                     else f"{lo * 100:g}~{hi * 100:g}%")
        rows.append({"bucket": name, "pred": round(float(part["score"].mean()), 5),
                     "actual": round(float(part["net"].mean()), 5), "n": part.height})
    return rows


def _round(v: float | None, digits: int = 4) -> float | None:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return round(float(v), digits)


# ---------------------------------------------------------------- 参数

@dataclass
class _Params:
    capital: float
    position_pct: float
    max_positions: int
    max_gap: float
    exit_rule: str
    stop_loss: float
    costs: Costs


def _params(ts: dict) -> _Params:
    return _Params(
        capital=float(ts.get("capital", 100_000)),
        position_pct=float(ts.get("position_pct", 0.2)),
        max_positions=int(ts.get("max_positions", 5)),
        max_gap=float(ts.get("max_gap_pct", 7.0) if ts.get("max_gap_pct") is not None else 7.0),
        exit_rule=str(ts.get("exit_rule", "next_close")),
        stop_loss=float(ts.get("stop_loss_pct", 0.0) or 0.0),
        costs=costs_mod.from_trade(ts),
    )


def _pick(cand: pl.DataFrame, col: str, threshold: float | None, top_n: int) -> pl.DataFrame:
    d: pl.DataFrame = cand if threshold is None else cand.filter(pl.col(col) >= threshold)
    return (
        d.with_columns(pl.col(col).rank("ordinal", descending=True).over("date").alias("_r"))
        .filter(pl.col("_r") <= top_n)
        .sort(["date", "_r"])
    )


# ---------------------------------------------------------------- 组合模拟

@dataclass
class _SimResult:
    curve: list[tuple[int, float]]
    trades: list[dict]
    positions: list[_Position]
    counters: dict[str, int]
    fees: float


def _simulate_portfolio(picks: pl.DataFrame, prices: _Prices, cal: list[int], p: _Params, delisted: set[str],
                        score_col: str = "score") -> _SimResult:
    signals: dict[int, list[dict]] = {}
    for r in picks.select(["date", "code", score_col]).iter_rows(named=True):
        signals.setdefault(_day(r["date"]), []).append({"date": r["date"], "code": r["code"], "score": r[score_col]})
    cash: float = p.capital
    positions: list[_Position] = []
    trades: list[dict] = []
    curve: list[tuple[int, float]] = []
    counters: dict[str, int] = {"unfilled": 0, "skipped_gap": 0, "skipped_full": 0, "skipped_cash": 0,
                                "skipped_lot": 0,
                                "skipped_held": 0, "suspended": 0}
    fees_total: float = 0.0
    prev_equity: float = p.capital
    fee_rate: float = p.costs.commission
    slip: float = p.costs.slippage
    rule: str = p.exit_rule
    col = prices.cols

    cut: int = _day(costs_mod.STAMP_CUT_DATE)
    fixed_stamp: float | None = p.costs.stamp

    def sell(pos: _Position, day: int, price: float, reason: str, at_close: bool = False,
             exit_day: int | None = None) -> None:
        nonlocal cash, fees_total
        xday: int = day if exit_day is None else exit_day
        fill: float = price * (1 - slip)
        gross: float = fill * pos.shares
        stamp: float = fixed_stamp if fixed_stamp is not None else             (costs_mod.STAMP_BEFORE if xday < cut else costs_mod.STAMP_AFTER)
        fee: float = _commission(gross, fee_rate) + gross * stamp
        cash += gross - fee
        fees_total += fee
        net: float = gross - fee
        # 原始数值，输出时再格式化（_format_trade），随机基准要模拟很多次，这里越省越好
        trades.append({
            "signal_date": pos.signal_date, "code": pos.code, "score": pos.score, "entry_day": pos.entry_day,
            "entry": pos.entry_price, "exit_day": xday, "exit": fill, "ret": net / pos.cost - 1,
            "reason": reason, "delayed": pos.delayed,
            "hold_days": pos.rows_held - (1 if at_close else 0),          # 买入到卖出之间隔了几个交易日
            "pnl": net - pos.cost,
        })

    prev_day: int | None = None
    for day in cal:
        # 0) 已退市（之后没有行情）的持仓：按最后收盘价卖出
        if delisted:
            keep0: list[_Position] = []
            for pos in positions:
                last: int | None = prices.last_day(pos.code)
                if pos.code in delisted and last is not None and last < day and pos.entry_day <= last:
                    sell(pos, day, pos.last_close, "delisted", at_close=True, exit_day=last)
                    continue
                keep0.append(pos)
            positions = keep0
        # 1) 开盘：计划卖出、开盘止损
        keep: list[_Position] = []
        for pos in positions:
            i: int = prices.index(pos.code, day)
            if i < 0 or pos.entry_day >= day:
                keep.append(pos)
                continue
            o: float = col["open"][i]
            reason: str | None = pos.pending_open
            if reason is None and pos.stop_price is not None and o <= pos.stop_price:
                reason = "stop_open"
            if reason is not None:
                ld: float = col["limit_down"][i]
                if o <= ld + TOLERANCE and col["high"][i] <= ld + TOLERANCE:     # 一字跌停卖不出
                    pos.pending_open, pos.delayed = reason, True
                    keep.append(pos)
                    continue
                sell(pos, day, o, reason)          # 开盘跌停但打开过：按开盘（跌停）价成交
                continue
            keep.append(pos)
        positions = keep

        # 2) 开盘：买入上一交易日的信号
        todays: list[dict] = signals.get(prev_day, []) if prev_day is not None else []
        held: set[str] = {q.code for q in positions}
        for sig in todays:
            code: str = sig["code"]
            if code in held:
                counters["skipped_held"] += 1
                continue
            if len(positions) >= p.max_positions:
                counters["skipped_full"] += 1
                continue
            i = prices.index(code, day)
            if i < 0:
                counters["suspended"] += 1
                continue
            o = col["open"][i]
            if o >= col["limit_up"][i] - TOLERANCE or not o > 0:            # 开盘涨停/一字买不到
                counters["unfilled"] += 1
                continue
            pre: float = col["preclose"][i]
            if pre > 0 and (o / pre - 1) * 100 > p.max_gap:
                counters["skipped_gap"] += 1
                continue
            price: float = o * (1 + slip)
            budget: float = min(prev_equity * p.position_pct, cash)
            shares: int = int(budget / price / LOT) * LOT
            while shares > 0 and shares * price + _commission(shares * price, fee_rate) > cash:
                shares -= LOT
            if shares <= 0:
                # 单只仓位预算连一手（100 股）都买不起 → skipped_lot；否则是现金不够 → skipped_cash
                counters["skipped_lot" if prev_equity * p.position_pct < price * LOT else "skipped_cash"] += 1
                continue
            amount: float = shares * price
            fee: float = _commission(amount, fee_rate)
            cash -= amount + fee
            fees_total += fee
            positions.append(_Position(
                code=code, signal_date=sig["date"], score=float(sig["score"]), entry_day=day, entry_price=price,
                shares=shares, cost=amount + fee,
                stop_price=price * (1 - p.stop_loss) if p.stop_loss > 0 else None, last_close=o,
            ))
            held.add(code)

        # 3) 盘中与收盘
        keep = []
        for pos in positions:
            i = prices.index(pos.code, day)
            if i < 0:
                keep.append(pos)
                continue
            c: float = col["close"][i]
            lu: float = col["limit_up"][i]
            ld = col["limit_down"][i]
            pos.rows_held += 1
            pos.last_close = c
            if pos.entry_day < day:
                if pos.stop_price is not None and col["low"][i] <= pos.stop_price:
                    if col["high"][i] <= ld + TOLERANCE:          # 一字跌停，卖不出
                        pos.pending_open, pos.delayed = "stop_intraday", True
                        keep.append(pos)
                        continue
                    sell(pos, day, min(col["open"][i], pos.stop_price), "stop_intraday", at_close=True)
                    continue
                if pos.close_exit:
                    if c <= ld + TOLERANCE:         # 收盘跌停，卖不出
                        pos.close_exit, pos.pending_open, pos.delayed = False, "next_close", True
                        keep.append(pos)
                        continue
                    sell(pos, day, c, "next_close", at_close=True)
                    continue
            if pos.pending_open is None and not pos.close_exit:
                if rule == "next_open" and pos.entry_day == day:
                    pos.pending_open = "next_open"
                elif rule == "next_close" and pos.entry_day == day:
                    pos.close_exit = True
                elif rule == "until_break":
                    if c < lu - TOLERANCE:
                        pos.pending_open = "break"
                    elif pos.rows_held >= MAX_HOLD_DAYS:
                        pos.pending_open = "max_hold"
                elif rule == "until_break_close" and pos.entry_day < day:
                    not_lu: bool = not (c >= lu - TOLERANCE)
                    if pos.commit_close is not None or not_lu or pos.rows_held >= SWING_FORCE_K:
                        base_why: str = pos.commit_close or ("break_close" if not_lu else "force_close")
                        ld_close: bool = c <= ld + TOLERANCE
                        if ld_close and pos.rows_held < SWING_MAX_K:
                            pos.delayed = True
                            pos.commit_close = base_why        # 锁定：下一个收盘不管涨停与否都卖
                        else:
                            sell(pos, day, c, "ld_force" if ld_close else base_why, at_close=True)
                            continue
            keep.append(pos)
        positions = keep

        equity: float = cash + sum(q.shares * q.last_close for q in positions)
        curve.append((day, equity))
        prev_equity = equity
        prev_day = day
    return _SimResult(curve, trades, positions, counters, fees_total)


# ---------------------------------------------------------------- 主函数

def run_backtest(oos: pl.DataFrame, panel_lim: pl.DataFrame, s: Any, t: Any,
                 daily: pl.DataFrame | None = None, *, delisted: set[str] | None = None,
                 baseline_seeds: int = BASELINE_SEEDS) -> dict:
    """oos：样本外预测（含 prob/bias/contrib_*（波段为 pred）、y（波段另有 net）及 close、float_cap、board、is_st、
    streak、one_word 等展示列）。s：PredictSettings（或同名属性对象/字典），t：TradeSettings。返回见 ARCHITECTURE 3.10/7.3。
    daily：[date, n, positives] 每日全部候选数与次日涨停数（oos 只保存了每天前几百名时用它算 base_rate）。
    delisted：已退市股票代码（None 时读股票列表）；baseline_seeds：随机基准次数（0 不算）。"""
    ps: dict = _as_dict(s)
    ts: dict = _as_dict(t)
    p: _Params = _params(ts)
    top_n: int = int(ps.get("top_n", 5))
    threshold: float = float(ps.get("threshold", 0.0) or 0.0)
    regression: bool = is_regression(oos, ps.get("kind"))

    scored: pl.DataFrame = apply_weights(oos, ps.get("weights"), None, 0.0, regression=regression)
    base_rate: float | None = _round(scored.filter(pl.col("y").is_not_null())["y"].mean()) \
        if scored.height and "y" in scored.columns else None
    if daily is not None and not daily.is_empty() and scored.height:
        d: pl.DataFrame = daily.filter(pl.col("date").is_between(scored["date"].min(), scored["date"].max()))
        total: int = int(d["n"].sum())
        base_rate = _round(int(d["positives"].sum()) / total) if total else base_rate
    cand: pl.DataFrame = filter_candidates(scored, {**ps, "threshold": 0.0})
    picks: pl.DataFrame = _pick(cand, "score", threshold, top_n)
    hit_rate: float | None = _round(picks.filter(pl.col("y").is_not_null())["y"].mean()) \
        if picks.height and "y" in picks.columns else None
    names: dict[str, str] = _names(oos)

    cal_dates: list[date] = sorted(panel_lim["date"].unique().to_list()) if panel_lim.height else []
    empty: dict = _result_empty(base_rate, hit_rate, cand, regression)
    if picks.is_empty() or not cal_dates:
        return empty
    first_signal: int = _day(picks["date"].min())
    cal: list[int] = [_day(x) for x in cal_dates if _day(x) >= first_signal]
    prices: _Prices = _load_prices(panel_lim, cand["code"].unique().to_list() if baseline_seeds
                                   else picks["code"].unique().to_list(), _from_day(first_signal))
    dl: set[str] = load_delisted() if delisted is None else set(delisted)
    sim: _SimResult = _simulate_portfolio(picks, prices, cal, p, dl)
    out: dict = _summarize(sim, p.capital, base_rate, hit_rate, cand, picks.height, regression, names)
    out["metrics"]["per_trade_uncapped"] = _per_trade_uncapped(picks, panel_lim, p, dl, cal)
    out["notes"] = _notes(out["metrics"])
    out["by_period"] = _by_period(picks, prices, cal, p, dl)
    out["baseline"], out["baseline_matched"] = (_baselines(cand, picks, top_n, prices, cal, p, dl, baseline_seeds)
                                                if baseline_seeds else (None, None))
    # 分段的随机基准：和策略一样，每一段都用全新账户、只在该段的日子里单独回测（见 _baselines）
    for key in ("baseline", "baseline_matched"):
        per: dict = (out[key] or {}).pop("by_period", None) or {}
        for name, m in out["by_period"].items():
            if m is not None and per.get(name):
                m[key] = per[name]
    return out


BASELINE_KEYS: tuple[str, ...] = ("avg_return", "win_rate", "total_return", "cagr", "trades", "daily_t", "nw_t")
SKIPPED_NOTE_SHARE: float = 0.2     # 因资金不足/一手太贵跳过的信号超过这个比例时提示


def _per_trade_uncapped(picks: pl.DataFrame, panel_lim: pl.DataFrame, p: _Params, delisted: set[str],
                        cal: list[int]) -> dict:
    """同一批信号逐笔成交（不受资金、仓位上限、一手限制，按比例收费）：只统计已卖出的 {trades, avg_return, win_rate}"""
    empty: dict = {"trades": 0, "avg_return": None, "win_rate": None}
    if picks.is_empty():
        return empty
    sig: pl.DataFrame = picks.select(pl.col("date").alias("signal_date"), "code")
    calendar: np.ndarray = np.array([np.datetime64(_from_day(d), "D") for d in cal], dtype="datetime64[D]")
    res: pl.DataFrame = simulate(sig, panel_lim, costs=p.costs, exit_rule=p.exit_rule, stop_loss_pct=p.stop_loss,
                                 max_gap_pct=p.max_gap, delisted=delisted, calendar=calendar if len(calendar) else None)
    rets: np.ndarray = res.filter(pl.col("status") == "closed")["ret"].drop_nulls().to_numpy()
    if not len(rets):
        return empty
    return {"trades": int(len(rets)), "avg_return": _round(float(rets.mean()), 5),
            "win_rate": _round(float((rets > 0).mean()))}


def _notes(metrics: dict) -> list[str]:
    """回测结果的中文提示（资金不足/一手太贵跳过太多信号时）"""
    notes: list[str] = []
    signals: int = int(metrics.get("signals") or 0)
    skipped: int = int(metrics.get("skipped_lot") or 0) + int(metrics.get("skipped_cash") or 0)
    if signals and skipped / signals > SKIPPED_NOTE_SHARE:
        notes.append(f"资金不足/一手太贵跳过了 {skipped / signals * 100:.0f}% 的信号，整体结果主要代表前一段时间"
                     f"（{skipped} / {signals} 个信号因此没买；账户越亏、股价越高越容易买不起，后面的信号常常买不进）。")
    return notes


def _mean_metrics(res: list[dict], seeds: int) -> dict | None:
    """多次随机回测的指标取平均（某个指标全部为空时为 None）"""
    if not res:
        return None
    out: dict = {}
    for k in BASELINE_KEYS:
        vals: list[float] = [float(m[k]) for m in res if m.get(k) is not None]
        out[k] = _round(float(np.mean(vals)), 5) if vals else None
    out["seeds"] = seeds
    return out


def _baselines(cand: pl.DataFrame, picks: pl.DataFrame, top_n: int, prices: _Prices, cal: list[int], p: _Params,
               delisted: set[str], seeds: int) -> tuple[dict | None, dict | None]:
    """两种随机基准，都用同一候选池（同样的筛选条件）、同样的交易规则，seeds 次平均：
    - baseline：每天都随机挑 top_n 只（不看门槛，包括策略因门槛"不操作"的日子）——"随便挑、天天做"；
    - baseline_matched：只在策略出信号的日子，每天随机挑和策略同样多只——只比"挑股票"，不比"挑日子"。
      门槛不起作用（策略每天都挑满 top_n）时两者完全相同，直接复用。
    整段结果用一个账户；by_period 的每一段和策略的 by_period 一样用全新账户、只在该段的日子里单独回测
    （整段随机账户可能在前一段就亏光了，后一段只剩寥寥几笔，拿来和策略的全新账户比会得出相反的结论）。"""
    if cand.is_empty():
        return None, None
    base: pl.DataFrame = cand.select("date", "code").sort(["date", "code"])
    avail: pl.DataFrame = base.group_by("date").agg(pl.len().alias("_avail"))
    want: pl.DataFrame = picks.group_by("date").agg(pl.len().alias("_want"))
    need: pl.DataFrame = avail.join(want, on="date", how="left").with_columns(pl.col("_want").fill_null(0))
    same: bool = bool(need.select((pl.col("_want") == pl.min_horizontal("_avail", pl.lit(top_n))).all()).item())
    res: dict[str, dict[str, list[dict]]] = {k: {"all": [], "selection": [], "holdout": []} for k in ("rnd", "match")}
    for sd in range(seeds):
        rnd: np.ndarray = np.random.default_rng(1000 + sd).random(base.height)
        ranked: pl.DataFrame = base.with_columns(pl.Series("_u", rnd)).with_columns(
            pl.col("_u").rank("ordinal", descending=True).over("date").alias("_r"))
        variants: list[tuple[str, pl.DataFrame]] = [("rnd", ranked.filter(pl.col("_r") <= top_n).sort(["date", "_r"]))]
        if not same:
            matched: pl.DataFrame = ranked.join(want, on="date", how="inner").filter(pl.col("_r") <= pl.col("_want"))
            variants.append(("match", matched.sort(["date", "_r"])))
        for key, rp in variants:
            sim: _SimResult = _simulate_portfolio(rp, prices, cal, p, delisted, score_col="_u")
            res[key]["all"].append(_curve_metrics(sim.curve, sim.trades, p.capital))
            for name, part, days in _period_parts(rp, cal):
                ps: _SimResult = _simulate_portfolio(part, prices, days, p, delisted, score_col="_u")
                res[key][name].append(_curve_metrics(ps.curve, ps.trades, p.capital))

    def pack(r: dict[str, list[dict]]) -> dict | None:
        out: dict | None = _mean_metrics(r["all"], seeds)
        if out is not None:
            out["by_period"] = {name: _mean_metrics(r[name], len(r[name])) for name in ("selection", "holdout")}
        return out

    rnd_out: dict | None = pack(res["rnd"])
    if same:
        match_out: dict | None = None if rnd_out is None else {
            **{k: v for k, v in rnd_out.items() if k != "by_period"}, "same_as_baseline": True,
            "by_period": {k: (dict(v, same_as_baseline=True) if v else None)
                          for k, v in (rnd_out.get("by_period") or {}).items()}}
    else:
        match_out = pack(res["match"])
        if match_out is not None:
            match_out["same_as_baseline"] = False
    return rnd_out, match_out


def _curve_metrics(curve: list[tuple[int, float]], trades: list[dict], start_value: float) -> dict:
    """一段资金曲线 + 该段交易 → 收益指标"""
    values: np.ndarray = np.array([v for _, v in curve], dtype=float)
    rets: np.ndarray = np.array([tr["ret"] for tr in trades], dtype=float)
    out: dict[str, Any] = {
        "trades": len(trades),
        "win_rate": _round(float((rets > 0).mean())) if len(rets) else None,
        "avg_return": _round(float(rets.mean()), 5) if len(rets) else None,
        "median_return": _round(float(np.median(rets)), 5) if len(rets) else None,
        "total_return": None, "cagr": None, "max_drawdown": None, "daily_t": None, "nw_t": None,
    }
    if not len(values):
        return out
    series: np.ndarray = np.r_[start_value, values]
    daily: np.ndarray = series[1:] / series[:-1] - 1
    total: float = float(values[-1] / start_value - 1)
    years: float = max(len(values) / ANNUAL_DAYS, 1e-9)
    peak: np.ndarray = np.maximum.accumulate(series)
    std: float = float(daily.std(ddof=1)) if len(daily) > 2 else 0.0
    out.update(
        total_return=_round(total),
        cagr=_round((1 + total) ** (1 / years) - 1 if total > -1 else -1.0),
        max_drawdown=_round(float((series / peak - 1).min())),
        daily_t=_round(float(daily.mean() / std * math.sqrt(len(daily))), 3) if std > 0 else None,
        nw_t=stats.rounded(stats.nw_t(daily)),
    )
    return out


PERIOD_TAIL_DAYS: int = 20         # 分段回测：分界后再多走这么多个交易日，让段内买入的持仓卖完


def _period_parts(picks: pl.DataFrame, cal: list[int]) -> list[tuple[str, pl.DataFrame, list[int]]]:
    """按信号日切成 选择期（2025-07-01 之前）/ 留出期（之后）：[(名称, 该段的信号, 该段账户要走的交易日)]。
    交易日从该段第一个信号日开始，到分界后再多走 PERIOD_TAIL_DAYS 天（让段内买入的持仓卖完）；该段没有信号时不返回"""
    cut: int = _day(HOLDOUT_START)
    out: list[tuple[str, pl.DataFrame, list[int]]] = []
    for name, lo, hi in (("selection", -10**9, cut), ("holdout", cut, 10**9)):
        part: pl.DataFrame = picks.filter(pl.col("date").is_between(_from_day(max(lo, 0)), _from_day(min(hi, 10**6)),
                                                                    closed="left"))
        if part.is_empty():
            continue
        first: int = _day(part["date"].min())
        after: list[int] = [d for d in cal if d >= hi][:PERIOD_TAIL_DAYS]
        out.append((name, part, [d for d in cal if first <= d < hi] + after))
    return out


def _by_period(picks: pl.DataFrame, prices: _Prices, cal: list[int], p: _Params, delisted: set[str]) -> dict:
    """选择期（2025-07-01 之前的信号）/ 留出期（之后的信号）各用一个全新账户（同样本金、同样规则）单独回测，
    互不影响（整段回测里前面亏光了，后面就没钱买，看不出后一段的真实表现）"""
    out: dict[str, dict | None] = {"selection": None, "holdout": None}
    for name, part, days in _period_parts(picks, cal):
        sim: _SimResult = _simulate_portfolio(part, prices, days, p, delisted)
        m: dict = _curve_metrics(sim.curve, sim.trades, p.capital)
        m["start"] = _from_day(days[0]).isoformat() if days else None
        m["end"] = _from_day(days[-1]).isoformat() if days else None
        m["signals"] = part.height
        out[name] = m
    return out


def _result_empty(base_rate: float | None, hit_rate: float | None, cand: pl.DataFrame, regression: bool) -> dict:
    return {
        "metrics": {"trades": 0, "win_rate": None, "avg_return": None, "median_return": None, "profit_factor": None,
                    "total_return": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "hit_rate": hit_rate,
                    "base_rate": base_rate, "unfilled": 0, "avg_hold_days": None, "sharpe": None, "signals": 0,
                    "daily_t": None, "nw_t": None, "fill_rate": None,
                    "per_trade_uncapped": {"trades": 0, "avg_return": None, "win_rate": None}},
        "equity": [], "drawdown": [], "yearly": [], "notes": [],
        "calibration": _calibration(cand, regression),
        "topn_hit": _topn_hit(cand) if cand.height and "y" in cand.columns else [],
        "trades": [], "baseline": None, "baseline_matched": None, "by_period": {"selection": None, "holdout": None},
    }


def _summarize(sim: _SimResult, capital: float, base_rate: float | None, hit_rate: float | None,
               cand: pl.DataFrame, n_signals: int, regression: bool, names: dict[str, str]) -> dict:
    curve, trades, open_positions, counters = sim.curve, sim.trades, sim.positions, sim.counters
    values: np.ndarray = np.array([v for _, v in curve], dtype=float)
    days: list[str] = [_from_day(d).isoformat() for d, _ in curve]
    peak: np.ndarray = np.maximum.accumulate(values)
    dd: np.ndarray = values / peak - 1
    daily: np.ndarray = values[1:] / values[:-1] - 1 if len(values) > 1 else np.array([])
    total_return: float = float(values[-1] / capital - 1)
    years: float = max(len(values) / ANNUAL_DAYS, 1e-9)
    cagr: float = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
    std: float = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0          # 和 t 值同一口径（样本标准差）
    sharpe: float | None = float(daily.mean() / std * math.sqrt(ANNUAL_DAYS)) if std > 0 else None
    cm: dict = _curve_metrics(curve, trades, capital)

    rets: np.ndarray = np.array([tr["ret"] for tr in trades], dtype=float)
    pnl: np.ndarray = np.array([tr["pnl"] for tr in trades], dtype=float)
    gains: float = float(pnl[pnl > 0].sum()) if len(pnl) else 0.0
    losses: float = float(-pnl[pnl < 0].sum()) if len(pnl) else 0.0
    metrics: dict[str, Any] = {
        "trades": len(trades),
        "win_rate": _round(float((rets > 0).mean())) if len(rets) else None,
        "avg_return": _round(float(rets.mean()), 5) if len(rets) else None,
        "median_return": _round(float(np.median(rets)), 5) if len(rets) else None,
        "profit_factor": _round(gains / losses, 3) if losses > 0 else None,
        "total_return": _round(total_return),
        "cagr": _round(cagr),
        "max_drawdown": _round(float(dd.min())),
        "hit_rate": hit_rate,
        "base_rate": base_rate,
        "unfilled": counters["unfilled"],
        "avg_hold_days": _round(float(np.mean([tr["hold_days"] for tr in trades])), 2) if trades else None,
        "sharpe": _round(sharpe, 3),
        "daily_t": cm["daily_t"],
        "nw_t": cm["nw_t"],
        "signals": n_signals,
        # 买得进的比例：买不进 = 开盘涨停/一字 + 开盘涨幅超限 + 停牌（因资金/仓位/已持有跳过的不算）
        "fill_rate": _round(1 - (counters["unfilled"] + counters["skipped_gap"] + counters["suspended"]) / n_signals)
        if n_signals else None,
        "skipped_gap": counters["skipped_gap"],
        "skipped_full": counters["skipped_full"],
        "skipped_cash": counters["skipped_cash"],
        "skipped_lot": counters["skipped_lot"],
        "skipped_held": counters["skipped_held"],
        "suspended": counters["suspended"],
        "open_positions": len(open_positions),
        "final_equity": round(float(values[-1]), 2),
        "total_fees": round(sim.fees, 2),
        "start": days[0],
        "end": days[-1],
    }
    # 年度
    yearly: list[dict] = []
    years_list: list[int] = sorted({int(d[:4]) for d in days})
    prev_end: float = capital
    for y in years_list:
        idx: list[int] = [i for i, d in enumerate(days) if int(d[:4]) == y]
        end_value: float = float(values[idx[-1]])
        yt: list[dict] = [tr for tr in trades if _from_day(tr["entry_day"]).year == y]
        yr: np.ndarray = np.array([tr["ret"] for tr in yt], dtype=float)
        yearly.append({"year": y, "return": _round(end_value / prev_end - 1), "trades": len(yt),
                       "win_rate": _round(float((yr > 0).mean())) if len(yr) else None})
        prev_end = end_value
    return {
        "metrics": metrics,
        "equity": [{"date": d, "value": round(float(v), 2)} for d, v in zip(days, values, strict=False)],
        "drawdown": [{"date": d, "value": round(float(v), 5)} for d, v in zip(days, dd, strict=False)],
        "yearly": yearly,
        "calibration": _calibration(cand, regression),
        "topn_hit": _topn_hit(cand) if "y" in cand.columns else [],
        "trades": [_format_trade(tr, names) for tr in trades[-MAX_TRADES_OUT:]],
    }


def _format_trade(tr: dict, names: dict[str, str]) -> dict:
    return {
        "signal_date": tr["signal_date"].isoformat(), "code": tr["code"], "name": names.get(tr["code"], ""),
        "score": _round(tr["score"], 5), "entry_date": _from_day(tr["entry_day"]).isoformat(),
        "entry": round(tr["entry"], 3), "exit_date": _from_day(tr["exit_day"]).isoformat(), "exit": round(tr["exit"], 3),
        "ret": round(tr["ret"], 5),
        "reason": EXIT_REASONS.get(tr["reason"], tr["reason"]) + (DELAYED_SUFFIX if tr["delayed"] else ""),
        "hold_days": tr["hold_days"], "pnl": round(tr["pnl"], 2),
    }


# ---------------------------------------------------------------- 逐笔模拟（模拟盘）

def simulate_trades(signals: pl.DataFrame, panel_lim: pl.DataFrame, trade: Any = None, *,
                    delisted: set[str] | None = None, calendar: Any = None) -> pl.DataFrame:
    """逐笔成交模拟（不受资金/仓位限制），规则与费用同 run_backtest。

    signals：signal_date(Date), code(Utf8), kind(Utf8), exit_rule(Utf8)，可选 stop_loss_pct(Float64，小数)。
    trade：TradeSettings / dict / None（默认交易规则；用其中的 max_gap_pct、fee_rate、slippage、印花税设置；
    exit_rule/stop_loss_pct 以信号表的列为准，列不存在或为空时用 trade 里的）。
    返回：输入列 + status("pending"/"unfilled"/"holding"/"closed"), entry_date, entry, exit_date, exit,
    ret（扣除全部费用的小数；holding 为按最新收盘价估算的浮动收益）, hold_days(Int32), exit_reason(中文)。
    面板在持仓中途结束时为 holding；delisted 为已退市代码（None 时读股票列表）。
    calendar：市场交易日（date 列表/数组）。None 时用 panel_lim 里出现过的日期——只传了少数几只股票的面板时，
    某只股票买入日停牌、当天其他股票也都没有行情，就认不出停牌；这种情况请传全市场日历（如 history.trade_dates()）。"""
    ts: dict = _as_dict(trade)
    p: _Params = _params(ts)
    cal: np.ndarray | None = None
    if calendar is not None:
        cal = np.sort(np.unique(np.asarray(pl.Series(list(calendar), dtype=pl.Date).to_numpy(), "datetime64[D]")))
    return simulate(signals, panel_lim, costs=p.costs, exit_rule=p.exit_rule, stop_loss_pct=p.stop_loss,
                    max_gap_pct=p.max_gap, delisted=delisted, calendar=cal)
