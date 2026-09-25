"""vnpy 时序算子的向量化版本 == vnpy 原版（同样的 null / NaN / 并列值 / 窗口不足 / 行顺序）"""
import warnings

import numpy as np
import polars as pl
import pytest

from quant_web.modellab import fast_ts

vnpy_ts = pytest.importorskip("vnpy.alpha.dataset.ts_function")
from vnpy.alpha.dataset.utility import DataProxy  # noqa: E402

ORIG = {name: getattr(vnpy_ts, name) for name in fast_ts.FAST}


def proxy(seed: int, nan: bool = True) -> DataProxy:
    rng = np.random.default_rng(seed)
    rows = []
    lens = {"600000.SH": 60, "600001.SH": 45, "000001.SZ": 4, "000002.SZ": 30}      # 有一只比窗口还短
    for sym, n in lens.items():
        v = np.round(rng.normal(0, 1, n), 1)                                          # 保留 1 位小数 → 很多并列值
        for i in range(n):
            x = float(v[i])
            if nan and rng.random() < 0.06:
                x = float("nan")
            rows.append((i, sym, x, rng.random() < 0.05))
    rng.shuffle(rows)                                                                   # 打乱股票之间的顺序
    rows.sort(key=lambda r: r[0])                                                       # 同一只股票内部按时间顺序，股票交错
    df = pl.DataFrame({"datetime": [r[0] for r in rows], "vt_symbol": [r[1] for r in rows],
                       "data": [None if r[3] else r[2] for r in rows]}, schema={"datetime": pl.Int64, "vt_symbol": pl.Utf8, "data": pl.Float64})
    return DataProxy(df)


def same(a: pl.DataFrame, b: pl.DataFrame) -> None:
    assert a["datetime"].to_list() == b["datetime"].to_list() and a["vt_symbol"].to_list() == b["vt_symbol"].to_list()
    x = a["data"].cast(pl.Float64).fill_nan(None).to_list()
    y = b["data"].cast(pl.Float64).fill_nan(None).to_list()
    bad = [(i, u, v) for i, (u, v) in enumerate(zip(x, y, strict=True))
           if not ((u is None and v is None) or (u is not None and v is not None and abs(u - v) <= 1e-9 * max(1.0, abs(u))))]
    assert not bad, bad[:5]


@pytest.mark.parametrize("name", sorted(fast_ts.FAST))
@pytest.mark.parametrize("window", [1, 3, 5, 20])
@pytest.mark.parametrize("nan", [False, True])
def test_fast_equals_vnpy(name: str, window: int, nan: bool) -> None:
    p = proxy(window * 7 + len(name), nan=nan)
    args = (0.25,) if name == "ts_quantile" else ()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        want = ORIG[name](p, window, *args).df
        got = fast_ts.FAST[name](p, window, *args).df
    same(got, want)


def test_patched_swaps_and_restores() -> None:
    from vnpy.alpha.dataset.utility import calculate_by_expression
    p = proxy(1, nan=False).df.rename({"data": "close"})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        want = calculate_by_expression(p, "ts_mean(close, 5) / ts_std(close, 10) + ts_rank(close, 5)")
        with fast_ts.patched():
            assert vnpy_ts.ts_rank is fast_ts.ts_rank
            with fast_ts.patched():                                                      # 可以嵌套
                pass
            assert vnpy_ts.ts_mean is fast_ts.ts_mean
            got = calculate_by_expression(p, "ts_mean(close, 5) / ts_std(close, 10) + ts_rank(close, 5)")
    assert vnpy_ts.ts_rank is ORIG["ts_rank"] and vnpy_ts.ts_mean is ORIG["ts_mean"]
    same(got, want)
