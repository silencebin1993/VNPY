"""
核心：目标仓位计算。回测和实盘建议调用的是同一个函数，保证两者逻辑完全一致。

只使用计算日及之前的数据（不存在"未来函数"），见 tests/test_etf_quant.py。
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import ETF_MAP, StrategyParams, Universe


@dataclass
class ClassDecision:
    """单个资产大类的决策，用于向使用者解释"为什么这么配"。"""

    asset_class: str
    chosen: str | None
    weight: float
    reason: str
    candidates: list[dict] = field(default_factory=list)


@dataclass
class Allocation:
    date: pd.Timestamp
    weights: dict[str, float]                   # vt_symbol -> 仓位比例（合计为1，含货币ETF）
    decisions: list[ClassDecision]
    portfolio_vol: float                        # 风险资产部分的预估年化波动
    vol_scale: float                            # 因波动超限而整体降仓的比例（1=未降仓）


def weekly_rebalance_dates(index: pd.DatetimeIndex, today: date | None = None) -> list[pd.Timestamp]:
    """每周最后一个交易日。传入 today 时（实盘），尚未结束的本周不计入。"""
    if len(index) == 0:
        return []
    s: pd.Series = pd.Series(index, index=index)
    dates: list[pd.Timestamp] = [pd.Timestamp(d) for d in s.groupby(index.to_period("W-SUN")).max()]
    if today is not None:
        final: pd.Timestamp = dates[-1]
        friday: date = final.date() + timedelta(days=4 - final.weekday())
        if final.date() < friday and today <= friday:
            dates = dates[:-1]
    return dates


def _name(vt_symbol: str) -> str:
    etf = ETF_MAP.get(vt_symbol)
    return etf.name if etf else vt_symbol


def compute_allocations(
    close: pd.DataFrame,
    universe: Universe,
    params: StrategyParams,
    dates: list[pd.Timestamp] | None = None,
) -> list[Allocation]:
    """
    close: 后复权收盘价宽表（index=日期，columns=vt_symbol）
    dates: 调仓日（默认每周最后一个交易日）
    """
    close = close.sort_index()
    for vt_symbol in universe.vt_symbols:
        if vt_symbol not in close:
            close[vt_symbol] = np.nan

    ret: pd.DataFrame = close.pct_change(fill_method=None)
    mom: pd.DataFrame = sum(close / close.shift(n) - 1 for n in params.momentum_windows) / len(params.momentum_windows)
    vol: pd.DataFrame = ret.rolling(params.vol_window).std() * np.sqrt(params.annual_days)
    ma: pd.DataFrame = close.rolling(params.trend_window).mean()
    trend: pd.DataFrame = close > ma
    # 已持有的ETF用更宽松的条件，避免价格在均线附近来回穿越时反复买卖
    trend_hold: pd.DataFrame = close > ma * (1 - params.exit_buffer)
    history_ok: pd.DataFrame = close.notna().cumsum() >= params.min_history

    if dates is None:
        dates = weekly_rebalance_dates(close.index)

    allocations: list[Allocation] = []
    prev_pick: dict[str, str] = {}
    prev_held: set[str] = set()

    def qualified(s: str, t: pd.Timestamp) -> bool:
        if s in prev_held:
            return bool(trend_hold.at[t, s]) and mom.at[t, s] > -params.exit_buffer
        return bool(trend.at[t, s]) and mom.at[t, s] > 0

    for t in dates:
        budgets: dict[str, float] = {}
        picks: dict[str, str] = {}
        decisions: dict[str, ClassDecision] = {}

        for cname, symbols in universe.classes.items():
            available: list[str] = [
                s for s in symbols if history_ok.at[t, s] and np.isfinite(vol.at[t, s]) and vol.at[t, s] > 0
            ]
            candidates: list[dict] = [
                {"vt_symbol": s, "name": _name(s), "momentum": float(mom.at[t, s]), "trend": bool(trend.at[t, s])}
                for s in available
            ]
            if not available:
                decisions[cname] = ClassDecision(cname, None, 0.0, "上市时间不足，暂不参与", candidates)
                continue

            budgets[cname] = float(np.mean([vol.at[t, s] for s in available])) ** -params.risk_alpha

            passed: list[str] = [s for s in available if qualified(s, t)]
            if not passed:
                prev_pick.pop(cname, None)
                decisions[cname] = ClassDecision(
                    cname, None, 0.0, "全部处于下跌趋势（低于200日均线或动量为负），暂不持有", candidates
                )
                continue

            best: str = max(passed, key=lambda s: mom.at[t, s])
            current: str | None = prev_pick.get(cname)
            if current in passed and current != best and mom.at[t, best] - mom.at[t, current] < params.switch_margin:
                reason: str = (
                    f"继续持有{_name(current)}：{_name(best)}动量略高但差距不足"
                    f"{params.switch_margin:.0%}，不值得换仓"
                )
                best = current
            elif current == best and not trend.at[t, best]:
                reason = (
                    f"继续持有{_name(best)}：略低于200日均线，但未跌破"
                    f"{params.exit_buffer:.0%}的卖出缓冲线"
                )
            elif current == best:
                reason = f"继续持有{_name(best)}：趋势向上，仍是同类中最强"
            else:
                reason = f"选择{_name(best)}：趋势向上（在200日均线之上），同类中动量最强"
            picks[cname] = best
            prev_pick[cname] = best
            decisions[cname] = ClassDecision(cname, best, 0.0, reason, candidates)

        weights: dict[str, float] = {}
        pvol: float = 0.0
        scale: float = 1.0
        if budgets:
            total_budget: float = sum(budgets.values())
            weights = {picks[c]: budgets[c] / total_budget for c in picks}

            # 组合波动控制：只降不升
            if weights:
                chosen: list[str] = list(weights)
                window: pd.DataFrame = ret[chosen].loc[:t].tail(params.vol_window).fillna(0)
                w: np.ndarray = np.array([weights[s] for s in chosen])
                pvol = float(np.sqrt(w @ window.cov().to_numpy() @ w * params.annual_days))
                if pvol > params.target_vol:
                    scale = params.target_vol / pvol
                    weights = {s: v * scale for s, v in weights.items()}

            weights = {s: min(v, params.max_weight) for s, v in weights.items()}

        # 避险：空余资金优先配置处于上升趋势的国债ETF
        rest: float = 1 - sum(weights.values())
        good_bonds: list[str] = [
            b for b in universe.bonds
            if history_ok.at[t, b] and (trend_hold.at[t, b] if b in prev_held else trend.at[t, b])
        ]
        good_bonds.sort(key=lambda b: vol.at[t, b] if np.isfinite(vol.at[t, b]) else np.inf)
        for b in good_bonds:
            bond_total: float = sum(weights.get(x, 0.0) for x in universe.bonds)
            room: float = min(params.max_weight - weights.get(b, 0.0), params.bond_max_total - bond_total, rest)
            if room > 1e-9:
                weights[b] = weights.get(b, 0.0) + room
                rest -= room

        if rest > 1e-9:
            if np.isfinite(close.at[t, universe.cash]):
                weights[universe.cash] = weights.get(universe.cash, 0.0) + rest
            # 货币ETF尚未上市时，剩余部分保持现金

        for decision in decisions.values():
            if decision.chosen:
                decision.weight = weights.get(decision.chosen, 0.0)

        for b in universe.bonds:
            if b in weights and b not in picks.values():
                decisions[f"避险仓（{_name(b)}）"] = ClassDecision(
                    "避险仓", b, weights[b], f"部分大类走弱，空出的资金转入趋势向上的{_name(b)}"
                )

        final_weights: dict[str, float] = {s: v for s, v in weights.items() if v > 1e-9}
        prev_held = set(final_weights) - {universe.cash}

        allocations.append(Allocation(
            date=pd.Timestamp(t),
            weights=final_weights,
            decisions=list(decisions.values()),
            portfolio_vol=pvol,
            vol_scale=scale,
        ))

    return allocations
