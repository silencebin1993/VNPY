"""quant_web 第三版：通达信口径基础函数、技术指标、前复权（全部离线；与逐行循环的参考实现逐值核对）"""
import math

import numpy as np
import polars as pl
import pytest

from quant_web.indicators import adjust, catalog, ta
from quant_web.indicators import funcs as F
from quant_web.indicators.funcs import Ctx


def walk(n: int, seed: int = 1, start: float = 10.0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    open_ = close * np.exp(rng.normal(0, 0.01, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
    vol = rng.integers(1_000, 100_000, n).astype(float) * 100
    return {"open": open_, "high": high, "low": low, "close": close, "volume": vol}


def near(a, b, tol: float = 1e-8) -> bool:
    if a is None or b is None or (isinstance(b, float) and math.isnan(b)):
        return (a is None or (isinstance(a, float) and math.isnan(a))) and (b is None or (isinstance(b, float) and math.isnan(b)))
    return abs(a - b) <= tol * max(1.0, abs(b))


def check(series: pl.Series, expected: list, tol: float = 1e-8, skip: int = 0) -> None:
    got = series.to_list()
    assert len(got) == len(expected)
    for i in range(skip, len(got)):
        assert near(got[i], expected[i], tol), (i, got[i], expected[i])


# ---------------------------------------------------------------- 参考实现（逐行循环，照通达信定义写）

def r_ma(x, n):
    return [None if i + 1 < n else sum(x[i - n + 1:i + 1]) / n for i in range(len(x))]


def r_ema(x, n):
    out, y = [], None
    for v in x:
        y = v if y is None else (2 * v + (n - 1) * y) / (n + 1)
        out.append(y)
    return out


def r_sma(x, n, m):
    out, y = [], None
    for v in x:
        y = v if y is None else (m * v + (n - m) * y) / n
        out.append(y)
    return out


def r_hhv(x, n):
    return [max(x[max(0, i - n + 1):i + 1]) for i in range(len(x))]


def r_llv(x, n):
    return [min(x[max(0, i - n + 1):i + 1]) for i in range(len(x))]


def r_sum(x, n):
    return [sum(x[:i + 1]) if n == 0 else sum(x[max(0, i - n + 1):i + 1]) for i in range(len(x))]


def r_std(x, n):
    return [None if i + 1 < n else float(np.std(x[i - n + 1:i + 1], ddof=1)) for i in range(len(x))]


def r_ref(x, n):
    return [None if i < n else x[i - n] for i in range(len(x))]


# ---------------------------------------------------------------- 基础函数

def test_basic_functions_match_reference() -> None:
    d = walk(300)
    x = list(d["close"])
    s = pl.Series("c", x)
    ctx = Ctx(None, len(x))
    check(F.ma(s, 5, ctx), r_ma(x, 5))
    check(F.ema(s, 12, ctx), r_ema(x, 12))
    check(F.sma(s, 9, 2, ctx), r_sma(x, 9, 2))
    check(F.hhv(s, 10, ctx), r_hhv(x, 10))
    check(F.llv(s, 10, ctx), r_llv(x, 10))
    check(F.sum_(s, 7, ctx), r_sum(x, 7))
    check(F.sum_(s, 0, ctx), r_sum(x, 0), tol=1e-9)
    check(F.std(s, 20, ctx), r_std(x, 20))
    check(F.ref(s, 3, ctx), r_ref(x, 3))
    # SLOPE / WMA / AVEDEV
    for i in (30, 150, 299):
        w = np.array(x[i - 9:i + 1])
        assert near(F.slope(s, 10, ctx)[i], float(np.polyfit(np.arange(10), w, 1)[0]), 1e-7)
        assert near(F.wma(s, 10, ctx)[i], float((w * np.arange(1, 11)).sum() / 55), 1e-9)
        assert near(F.avedev(s, 10, ctx)[i], float(np.abs(w - w.mean()).mean()), 1e-9)
    assert F.slope(s, 10, ctx)[8] is None and F.wma(s, 10, ctx)[8] is None and F.avedev(s, 10, ctx)[8] is None


def test_logic_functions() -> None:
    a = pl.Series([1.0, 2, 3, 2, 1, 2, 3, 4])
    b = pl.Series([2.0, 2, 2, 2, 2, 2, 2, 2])
    ctx = Ctx(None, 8)
    assert F.cross(a, b, ctx).to_list() == [False, False, True, False, False, False, True, False]
    cond = pl.Series([False, True, False, False, True, False, False, False])
    assert F.barslast(cond, ctx).to_list() == [None, 0, 1, 2, 0, 1, 2, 3]
    assert F.count(cond, 3, ctx).to_list() == [0, 1, 1, 1, 1, 1, 1, 0]
    assert F.every(a > 1, 3, ctx).to_list() == [False, False, False, True, False, False, False, True]
    assert F.exist(a >= 4, 2, ctx).to_list() == [False] * 7 + [True]
    sig = pl.Series([True, True, False, True, True, False, True, True])
    assert F.filter_(sig, 2, ctx).to_list() == [True, False, False, True, False, False, True, False]
    assert F.barscount(ctx).to_list() == [1, 2, 3, 4, 5, 6, 7, 8]


def test_groups_do_not_leak_between_stocks() -> None:
    """两只股票拼在一张表里算，结果必须与各自单独算完全一样（窗口不跨股票）"""
    d1, d2 = walk(120, seed=2, start=10), walk(80, seed=3, start=50)
    both = pl.DataFrame({"code": ["A"] * 120 + ["B"] * 80,
                         **{k: np.concatenate([d1[k], d2[k]]) for k in d1}})
    ctx = Ctx(both["code"])
    assert ctx.groups == 2 and ctx.pos[120] == 0 and ctx.pos[199] == 79
    for fn in (lambda s, c: F.ma(s, 10, c), lambda s, c: F.ema(s, 12, c), lambda s, c: F.hhv(s, 20, c),
               lambda s, c: F.sum_(s, 5, c), lambda s, c: F.sum_(s, 0, c), lambda s, c: F.slope(s, 6, c),
               lambda s, c: F.avedev(s, 14, c), lambda s, c: F.ref(s, 2, c), lambda s, c: F.std(s, 20, c)):
        whole = fn(both["close"], ctx).to_list()
        single = fn(pl.Series(d1["close"]), Ctx(None, 120)).to_list() + fn(pl.Series(d2["close"]), Ctx(None, 80)).to_list()
        for i, (a, b) in enumerate(zip(whole, single)):
            assert near(a, b, 1e-9), (i, a, b)
    cond = both["close"] > both["open"]
    got = F.barslast(cond, ctx).to_list()
    exp = F.barslast(cond[:120], Ctx(None, 120)).to_list() + F.barslast(cond[120:], Ctx(None, 80)).to_list()
    assert got == exp


# ---------------------------------------------------------------- 指标

def test_macd_kdj_rsi_boll_match_formulas() -> None:
    d = walk(250, seed=5)
    df = pl.DataFrame(d)
    ctx = Ctx(None, df.height)
    c, h, lo = list(d["close"]), list(d["high"]), list(d["low"])
    lines = {k: s for k, _, s, _ in ta.macd(df, ctx)}
    dif = [a - b for a, b in zip(r_ema(c, 12), r_ema(c, 26))]
    dea = r_ema(dif, 9)
    check(lines["dif"], dif)
    check(lines["dea"], dea)
    check(lines["macd"], [(a - b) * 2 for a, b in zip(dif, dea)])

    lines = {k: s for k, _, s, _ in ta.kdj(df, ctx)}
    ll, hh = r_llv(lo, 9), r_hhv(h, 9)
    rsv = [(ci - a) / (b - a) * 100 if b != a else 50.0 for ci, a, b in zip(c, ll, hh)]
    k = r_sma(rsv, 3, 1)
    dd = r_sma(k, 3, 1)
    check(lines["k"], k)
    check(lines["d"], dd)
    check(lines["j"], [3 * a - 2 * b for a, b in zip(k, dd)])

    lines = {k: s for k, _, s, _ in ta.rsi(df, ctx)}
    diff = [0.0] + [c[i] - c[i - 1] for i in range(1, len(c))]
    up = r_sma([max(v, 0) for v in diff], 6, 1)
    ab = r_sma([abs(v) for v in diff], 6, 1)
    check(lines["rsi1"], [a / b * 100 if b else None for a, b in zip(up, ab)], skip=1)

    lines = {k: s for k, _, s, _ in ta.boll(df, ctx)}
    mid, sd = r_ma(c, 20), r_std(c, 20)
    check(lines["upper"], [None if m is None else m + 2 * s for m, s in zip(mid, sd)])


def test_obv_dmi_cci_formulas() -> None:
    d = walk(200, seed=7)
    df = pl.DataFrame(d)
    ctx = Ctx(None, df.height)
    c, h, lo, v = list(d["close"]), list(d["high"]), list(d["low"]), list(d["volume"])
    obv = {k: s for k, _, s, _ in ta.obv(df, ctx)}["obv"]
    exp, acc = [], 0.0
    for i in range(len(c)):
        if i:
            acc += v[i] if c[i] > c[i - 1] else (-v[i] if c[i] < c[i - 1] else 0)
        exp.append(acc)
    check(obv, exp)

    lines = {k: s for k, _, s, _ in ta.dmi(df, ctx)}
    tr = [None] + [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(c[i - 1] - lo[i])) for i in range(1, len(c))]
    hd = [None] + [h[i] - h[i - 1] for i in range(1, len(c))]
    ld = [None] + [lo[i - 1] - lo[i] for i in range(1, len(c))]
    dmp = [0.0] + [hd[i] if hd[i] > 0 and hd[i] > ld[i] else 0.0 for i in range(1, len(c))]
    i = 150
    mtr_i = sum(max(h[j] - lo[j], abs(h[j] - c[j - 1]), abs(c[j - 1] - lo[j])) for j in range(i - 13, i + 1))
    assert near(lines["pdi"][i], sum(dmp[i - 13:i + 1]) * 100 / mtr_i, 1e-9)
    assert tr[0] is None

    cc = {k: s for k, _, s, _ in ta.cci(df, ctx)}["cci"]
    typ = [(a + b + x) / 3 for a, b, x in zip(h, lo, c)]
    w = np.array(typ[i - 13:i + 1])
    assert near(cc[i], (typ[i] - w.mean()) / (0.015 * np.abs(w - w.mean()).mean()), 1e-9)


def test_catalog_compute_and_params() -> None:
    d = walk(120, seed=9)
    bars = [{"date": f"2026-01-{i % 28 + 1:02d}", **{k: float(v[i]) for k, v in d.items()}} for i in range(120)]
    res = catalog.compute(bars, ["ma", "macd", "turnover", "kdj"], {"ma": {"n1": 3}})
    assert res["ma"]["lines"][0]["label"] == "MA3" and len(res["ma"]["lines"][0]["data"]) == 120
    assert res["ma"]["lines"][0]["data"][1] is None and res["ma"]["lines"][0]["data"][2] is not None
    assert "error" in res["turnover"] and "换手率" in res["turnover"]["error"]    # 缺列只影响自己
    assert res["macd"]["pane"] == "sub" and {ln["style"] for ln in res["macd"]["lines"]} == {"line", "macd"}
    with pytest.raises(ValueError, match="要在 1 到 250 之间"):
        catalog.clean_params("ma", {"n1": 999})
    with pytest.raises(ValueError, match="没有「xx」这个指标"):
        catalog.compute(bars, ["xx"])
    ids = {x["id"] for x in catalog.listing()}
    assert set(ta.COMPUTE) == ids                                 # 目录与计算函数一一对应
    for item in catalog.listing():
        assert item["explain"] and item["usage"] and item["trap"]    # 每个指标都有白话说明


def test_all_indicators_run_on_multi_stock_frame() -> None:
    d1, d2 = walk(90, seed=11), walk(60, seed=12)
    df = pl.DataFrame({"code": ["A"] * 90 + ["B"] * 60, **{k: np.concatenate([d1[k], d2[k]]) for k in d1},
                       "turnover": np.full(150, 2.0)})
    ctx = Ctx(df["code"])
    for ind_id, fn in ta.COMPUTE.items():
        for key, _, s, _ in fn(df, ctx):
            assert len(s) == 150, (ind_id, key)
            vals = [v for v in s.to_list() if v is not None]
            assert all(math.isfinite(v) for v in vals), (ind_id, key)


# ---------------------------------------------------------------- 前复权

def test_qfq_anchor_continuity_and_truncation() -> None:
    # 第 3 天 10 送 10：昨收 20 → 除权参考价 10，当天收 10.5（真实涨 5%）
    df = pl.DataFrame({
        "code": ["A"] * 5, "date": [1, 2, 3, 4, 5],
        "open": [19.0, 20.0, 10.2, 10.5, 11.0], "high": [20.0, 20.5, 10.6, 11.0, 11.5],
        "low": [18.5, 19.5, 10.0, 10.4, 10.9], "close": [19.5, 20.0, 10.5, 10.8, 11.2],
        "preclose": [19.0, 19.5, 10.0, 10.5, 10.8],
    })
    out = adjust.add_qfq(df)
    q = out["qclose"].to_list()
    assert q[-1] == pytest.approx(11.2)                           # 最新一天等于真实价
    assert q[2] / q[1] == pytest.approx(10.5 / 10.0)              # 除权日按真实涨跌连续
    assert q[1] == pytest.approx(10.0)                            # 除权前价格减半
    assert out["qhigh"][1] == pytest.approx(20.5 * q[1] / 20.0)
    tail = adjust.add_qfq(df.tail(3))                             # 截取一段（含最新一天）结果不变
    assert tail["qclose"].to_list() == pytest.approx(q[2:])
    f = out["adj_factor"].to_list()
    assert adjust.adjust_price(9.0, f[1], f[4]) == pytest.approx(4.5)     # 送股前的止损 9 元 → 4.5 元


def test_ctx_and_performance_smoke() -> None:
    rng = np.random.default_rng(0)
    n_codes, n_days = 400, 500
    codes = np.repeat(np.arange(n_codes), n_days)
    close = pl.Series(np.exp(np.cumsum(rng.normal(0, 0.02, n_codes * n_days))))
    ctx = Ctx(pl.Series(codes))
    assert ctx.groups == n_codes and ctx.pos.max() == n_days - 1
    for fn in (F.ma, F.hhv, F.sum_, F.std):
        out = fn(close, 20, ctx)
        assert out.null_count() >= 0
