"""quant_web 第三版：筹码分布估算（守恒、平台/趋势形态、除权平移、增量与全量一致；全部离线）"""
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_web.indicators import chips


def bars(prices: list[float], turn: float = 5.0, code: str = "A", pre: list[float] | None = None,
         start: date = date(2026, 1, 1)) -> pl.DataFrame:
    n = len(prices)
    p = np.array(prices, dtype=float)
    return pl.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)], "code": [code] * n,
        "high": p * 1.01, "low": p * 0.99, "close": p, "volume": np.full(n, 1e6), "amount": p * 1e6,
        "turn": np.full(n, turn), "preclose": pre if pre is not None else [None] + list(p[:-1]),
    })


def test_mass_conserved_and_flat_platform() -> None:
    eng = chips.ChipEngine()
    df = bars([10.0] * 100, turn=10.0)
    for day in df.iter_slices(1):
        eng.step(day)
    assert eng.state.sum(axis=1)[0] == pytest.approx(1.0, abs=1e-4)
    s = eng.stats(np.array([0]), np.array([10.0]))
    assert s["avg_cost"][0] == pytest.approx(10.0, rel=0.01)
    assert s["winner"][0] > 0.5                       # 收盘价在平台中间：约一半以上在"成本不高于现价"
    assert s["conc90"][0] < 0.03                      # 筹码非常集中


def test_uptrend_cost_below_price_and_winner_high() -> None:
    prices = list(np.linspace(10, 20, 120))
    out = chips.single(bars(prices, turn=4.0))
    last = out["last"]
    assert last["avg_cost"] < 20 * 0.95
    assert last["winner"] > 0.9
    assert len(out["series"]["winner"]) == 120 and out["dist"]["prices"]
    assert abs(sum(out["dist"]["shares"]) - 1) < 1e-3


def test_downtrend_trapped_chips() -> None:
    prices = list(np.linspace(20, 10, 120))
    last = chips.single(bars(prices, turn=4.0))["last"]
    assert last["avg_cost"] > 10 * 1.05 and last["winner"] < 0.2      # 大部分人被套


def test_ex_rights_shift_matches_adjusted_series() -> None:
    """原始价格 + 除权平移（10 送 10）与"事先把历史价格减半"的前复权序列，最终分布应基本一致"""
    raw = [10.0] * 40 + [10.5] * 40 + [5.3] * 40         # 第 81 天除权：昨收 10.5 → 参考价 5.25
    pre = [None] + raw[:-1]
    pre[80] = 5.25
    a = chips.single(bars(raw, turn=6.0, pre=pre))
    adj = [p / 2 for p in raw[:80]] + raw[80:]
    b = chips.single(bars(adj, turn=6.0))
    assert a["last"]["avg_cost"] is not None and b["last"]["avg_cost"] is not None
    assert 5.0 < b["last"]["avg_cost"] < 5.5
    assert a["last"]["avg_cost"] == pytest.approx(b["last"]["avg_cost"], rel=0.01)
    assert a["last"]["p50"] == pytest.approx(b["last"]["p50"], rel=0.02)
    assert a["last"]["winner"] == pytest.approx(b["last"]["winner"], abs=0.03)


def test_incremental_equals_full(tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    frames = []
    for code, start in (("A", 10.0), ("B", 30.0)):
        p = list(start * np.exp(np.cumsum(rng.normal(0, 0.02, 90))))
        frames.append(bars(p, turn=float(rng.uniform(2, 8)), code=code))
    panel = pl.concat(frames)
    full, _ = chips.compute_panel(panel)
    days = sorted(panel["date"].unique().to_list())
    part1, eng = chips.compute_panel(panel.filter(pl.col("date") <= days[59]))
    path = tmp_path / "chips.npz"
    eng.save(path)
    eng2 = chips.ChipEngine.load(path)
    part2, _ = chips.compute_panel(panel.filter(pl.col("date") > days[59]), engine=eng2)
    inc = pl.concat([part1, part2]).sort(["date", "code"])
    full = full.sort(["date", "code"])
    assert inc.height == full.height == 180
    for col in ("winner", "avg_cost", "conc90"):
        assert np.allclose(inc[col].to_numpy(), full[col].to_numpy(), rtol=1e-5, equal_nan=True), col
    only_last, _ = chips.compute_panel(panel, want={days[-1]})
    assert only_last.height == 2 and only_last["date"].to_list() == [days[-1]] * 2


def test_suspended_day_keeps_state_and_one_price_day() -> None:
    eng = chips.ChipEngine()
    df = bars([10.0] * 30, turn=5.0)
    for day in df.iter_slices(1):
        eng.step(day)
    before = eng.state.copy()
    stop = df.tail(1).with_columns(pl.lit(0.0).alias("volume"), pl.lit(0.0).alias("turn"))
    eng.step(stop)                                      # 停牌：不变
    assert np.allclose(before, eng.state)
    one = df.tail(1).with_columns(pl.lit(11.0).alias("high"), pl.lit(11.0).alias("low"), pl.lit(11.0).alias("close"),
                                  pl.lit(20.0).alias("turn"))
    eng.step(one)                                       # 一字板：20% 筹码放在 11 元那一格
    s = eng.stats(np.array([0]), np.array([10.9]))
    assert s["winner"][0] == pytest.approx(0.8, abs=0.02)
