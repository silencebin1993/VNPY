"""etf_quant 核心逻辑测试（不需要联网）"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from etf_quant.allocator import compute_allocations, weekly_rebalance_dates
from etf_quant.config import StrategyParams, Universe
from etf_quant.rebalance import plan_rebalance


CASH = "511880.SSE"


def make_prices(n_days: int = 900, seed: int = 7, drift: dict[str, float] | None = None) -> pd.DataFrame:
    """为资产池生成随机游走价格"""
    rng = np.random.default_rng(seed)
    universe = Universe()
    index = pd.bdate_range("2020-01-01", periods=n_days)
    data: dict[str, np.ndarray] = {}
    for symbol in universe.vt_symbols:
        mu: float = (drift or {}).get(symbol, 0.0003)
        sigma: float = 0.001 if symbol == CASH else (0.003 if symbol in universe.bonds else 0.015)
        data[symbol] = 100 * np.exp(np.cumsum(rng.normal(mu, sigma, n_days)))
    return pd.DataFrame(data, index=index)


# ---------------- plan_rebalance ----------------

def test_plan_rebalance_rounds_to_lots_and_parks_cash() -> None:
    prices = {"A": 3.0, "B": 140.0, CASH: 100.0}
    targets = plan_rebalance({"A": 0.3, "B": 0.2, CASH: 0.5}, {}, prices, 100_000, CASH, band=0.03)
    assert targets["A"] == 10_000
    assert targets["B"] == 100
    assert targets[CASH] == 500
    assert all(v % 100 == 0 for v in targets.values())
    assert sum(v * prices[s] for s, v in targets.items()) <= 100_000


def test_plan_rebalance_band_keeps_small_deviation() -> None:
    prices = {"A": 10.0, CASH: 100.0}
    positions = {"A": 3_100, CASH: 600}          # A 占 31,000 / 100,000 = 31%
    targets = plan_rebalance({"A": 0.30, CASH: 0.69}, positions, prices, 9_000, CASH, band=0.03)
    assert targets["A"] == 3_100
    assert targets[CASH] == 600


def test_plan_rebalance_exits_and_enters() -> None:
    prices = {"A": 10.0, "B": 5.0, CASH: 100.0}
    targets = plan_rebalance({"B": 0.5, CASH: 0.5}, {"A": 5_000}, prices, 50_000, CASH, band=0.03)
    assert targets["A"] == 0
    assert targets["B"] == 10_000


def test_plan_rebalance_never_overspends() -> None:
    rng = np.random.default_rng(1)
    for _ in range(300):
        symbols = [f"S{i}" for i in range(6)]
        prices = {s: float(rng.uniform(0.5, 150)) for s in symbols} | {CASH: 100.0}
        raw = rng.dirichlet(np.ones(7))
        weights = dict(zip(symbols + [CASH], raw, strict=True))
        positions = {s: int(rng.integers(0, 50)) * 100 for s in symbols + [CASH]}
        cash = float(rng.uniform(0, 50_000))
        total = cash + sum(positions[s] * prices[s] for s in positions)
        targets = plan_rebalance(weights, positions, prices, cash, CASH, band=0.03)
        spent = sum(v * prices[s] for s, v in targets.items())
        assert spent <= total + 1e-6
        assert all(v >= 0 and v % 100 == 0 for v in targets.values())


# ---------------- 调仓日 ----------------

def test_weekly_dates_drop_incomplete_week() -> None:
    index = pd.bdate_range("2026-09-07", "2026-09-23")       # 最后一天是周三
    dates = weekly_rebalance_dates(index, today=date(2026, 9, 23))
    assert dates[-1] == pd.Timestamp("2026-09-18")


def test_weekly_dates_keep_finished_week() -> None:
    index = pd.bdate_range("2026-09-07", "2026-09-18")       # 周五收盘
    assert weekly_rebalance_dates(index, today=date(2026, 9, 18))[-1] == pd.Timestamp("2026-09-18")
    # 周五休市（节假日），周末运行时周四就是本周最后交易日
    index = index.drop(pd.Timestamp("2026-09-18"))
    assert weekly_rebalance_dates(index, today=date(2026, 9, 19))[-1] == pd.Timestamp("2026-09-17")


# ---------------- 目标仓位 ----------------

def test_allocation_constraints() -> None:
    params = StrategyParams()
    universe = Universe()
    allocations = compute_allocations(make_prices(), universe, params)
    assert allocations
    for a in allocations:
        assert sum(a.weights.values()) == pytest.approx(1.0, abs=1e-9)
        for s, w in a.weights.items():
            if s != universe.cash:
                assert w <= params.max_weight + 1e-9
        assert sum(a.weights.get(b, 0) for b in universe.bonds) <= params.bond_max_total + 1e-9
        # 每个大类最多一只
        for symbols in universe.classes.values():
            assert sum(1 for s in symbols if s in a.weights and s not in universe.bonds) <= 1


def test_no_lookahead() -> None:
    """用截至某日的数据算出的仓位，与用全部数据算出的同一日仓位完全一致"""
    params = StrategyParams()
    universe = Universe()
    prices = make_prices(seed=3)
    full = {a.date: a.weights for a in compute_allocations(prices, universe, params)}
    cutoff = prices.index[600]
    partial = compute_allocations(prices.loc[:cutoff], universe, params)
    assert partial
    for a in partial:
        if a.date < cutoff:
            assert a.weights == pytest.approx(full[a.date])


def test_downtrend_moves_to_bonds_and_cash() -> None:
    universe = Universe()
    drift = {s: -0.004 for symbols in universe.classes.values() for s in symbols}
    drift.update({b: 0.0004 for b in universe.bonds})
    allocations = compute_allocations(make_prices(drift=drift, seed=11), universe, StrategyParams())
    last = allocations[-1]
    assert set(last.weights) <= set(universe.bonds) | {universe.cash}
