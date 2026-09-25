"""
vnpy 时序算子的向量化版本（结果和 vnpy 原版一致，由 tests/test_quant_web_lab_fast_ts.py 逐个对照验证）。
vnpy.alpha 的 ts_mean / ts_std / ts_rank / ts_argmax / ts_argmin / ts_quantile / ts_decay_linear / ts_product
用 rolling_map + Python 回调逐行计算，全市场几百万行要几个小时；这里改用 numpy 滑动窗口一次算完。
只在实验室计算因子时临时替换（patched()），不改 vnpy 的文件；替换的函数语义相同，所以同一进程里别的地方同时用到也不受影响。

和原版对齐的细节：
- 在每只股票内部按"当前行顺序"滚动（和 .over("vt_symbol") 一样，不重新按日期排序），输出保持输入的行顺序；
- ts_mean / ts_std：min_samples=1——窗口里只要有一个非 null 值就算（NaN 算"有值"，np.nanmean/np.nanstd 会忽略它）；
  全是 null 时结果为 null；
- 其余算子：窗口里必须有 window 个非 null 值才算（开头不足一个窗口 → null）；窗口里有 NaN 时按原版的规则处理。
"""
from __future__ import annotations

import threading
import warnings
from collections.abc import Callable
from contextlib import contextmanager

import numpy as np
import polars as pl

CHUNK_ELEMS: int = 40_000_000           # 每次最多处理多少个"行 × 窗口"元素（控制内存）
_lock = threading.RLock()
_depth: int = 0
_saved: dict[str, Callable] = {}


def _prep(df: pl.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(按股票稳定排序的行号, 排好序的值（null→NaN）, 是否 null, 每行在本股票里的序号)"""
    sym = df["vt_symbol"].to_numpy()
    order = np.argsort(sym, kind="stable")                   # 同一只股票内部保持当前行顺序
    s = sym[order]
    data = df["data"].cast(pl.Float64)
    isnull = data.is_null().to_numpy()[order]
    vals = data.fill_null(np.nan).to_numpy().astype(np.float64)[order]
    n = len(s)
    change = np.ones(n, dtype=bool)
    if n > 1:
        change[1:] = s[1:] != s[:-1]
    gid = np.cumsum(change) - 1
    starts = np.flatnonzero(change)
    pos = np.arange(n) - (starts[gid] if n else 0)
    return order, vals, isnull, pos


def _windows(vals: np.ndarray, window: int) -> np.ndarray:
    """每行一个长度为 window 的窗口（最后一个元素是本行）；前面补 NaN"""
    padded = np.concatenate([np.full(window - 1, np.nan), vals])
    return np.lib.stride_tricks.sliding_window_view(padded, window)


def _apply(df: pl.DataFrame, window: int, fn: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray],
           min_samples: int | None = None) -> pl.DataFrame:
    """fn(窗口值, 窗口里哪些是 null, 本行序号) → 每行的结果；不满足 min_samples 的行为 null"""
    window = int(window)
    order, vals, isnull, pos = _prep(df)
    n = len(vals)
    out = np.full(n, np.nan)
    ok_rows = np.zeros(n, dtype=bool)
    if n and window > 0:
        need = window if min_samples is None else int(min_samples)
        nullw = _windows(isnull.astype(np.float64), window)
        valw = _windows(vals, window)
        step = max(1, CHUNK_ELEMS // window)
        cols = np.arange(window)
        for a in range(0, n, step):
            b = min(n, a + step)
            w = valw[a:b]
            inside = cols[None, :] >= (window - 1 - np.minimum(pos[a:b], window - 1))[:, None]   # 窗口元素在本股票内
            nl = (nullw[a:b] > 0.5) | ~inside
            cnt = (~nl).sum(axis=1)
            ok = cnt >= need
            if min_samples is None:
                ok &= pos[a:b] >= window - 1
            ok_rows[a:b] = ok
            if ok.any():
                res = fn(np.where(nl, np.nan, w)[ok], nl[ok], pos[a:b][ok])
                out[a:b][ok] = res
    result = np.full(n, np.nan)
    result[order] = out
    keep = np.zeros(n, dtype=bool)
    keep[order] = ok_rows
    s = pl.select(pl.when(pl.lit(pl.Series(keep))).then(pl.lit(pl.Series("data", result))).otherwise(None)).to_series().alias("data")
    return df.select(pl.col("datetime"), pl.col("vt_symbol")).with_columns(s)


def _proxy(df: pl.DataFrame):
    from vnpy.alpha.dataset.utility import DataProxy
    return DataProxy(df)


def _quiet(fn):
    def wrap(*a, **kw):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return fn(*a, **kw)
    return wrap


# ---------------------------------------------------------------- 各个算子（签名和 vnpy 原版一致）

@_quiet
def ts_mean(feature, window: int):
    return _proxy(_apply(feature.df, window, lambda w, nl, p: np.nanmean(w, axis=1), min_samples=1))


@_quiet
def ts_std(feature, window: int):
    return _proxy(_apply(feature.df, window, lambda w, nl, p: np.nanstd(w, axis=1, ddof=0), min_samples=1))


@_quiet
def ts_rank(feature, window: int):
    """scipy.stats.percentileofscore(窗口, 最后一个值, kind="rank") / 100；窗口里有 NaN → NaN"""
    def fn(w: np.ndarray, nl: np.ndarray, p: np.ndarray) -> np.ndarray:
        last = w[:, -1:]
        left = (w < last).sum(axis=1)
        right = (w <= last).sum(axis=1)
        pct = (left + right + (right > left)) * 50.0 / w.shape[1] / 100
        return np.where(np.isnan(w).any(axis=1), np.nan, pct)
    return _proxy(_apply(feature.df, window, fn))


def _arg(feature, window: int, largest: bool):
    def fn(w: np.ndarray, nl: np.ndarray, p: np.ndarray) -> np.ndarray:
        # polars 的 arg_max / arg_min 跳过 NaN、并列取第一个；全是 NaN 时返回 0
        nan = np.isnan(w)
        filled = np.where(nan, -np.inf if largest else np.inf, w)
        idx = filled.argmax(axis=1) if largest else filled.argmin(axis=1)
        idx = np.where(nan.all(axis=1), 0, idx)
        return idx.astype(np.float64) + 1
    return _proxy(_apply(feature.df, window, fn))


@_quiet
def ts_argmax(feature, window: int):
    return _arg(feature, window, True)


@_quiet
def ts_argmin(feature, window: int):
    return _arg(feature, window, False)


@_quiet
def ts_quantile(feature, window: int, quantile: float):
    def fn(w: np.ndarray, nl: np.ndarray, p: np.ndarray) -> np.ndarray:
        # polars 的 quantile：NaN 排在最后、也算在个数里；插值碰到 NaN 就是 NaN；位置正好是整数时直接取那个值
        srt = np.sort(w, axis=1)
        k = w.shape[1]
        pos = quantile * (k - 1)
        lo, hi = int(np.floor(pos)), int(np.ceil(pos))
        if lo == hi:
            return srt[:, lo]
        return srt[:, lo] + (pos - lo) * (srt[:, hi] - srt[:, lo])
    return _proxy(_apply(feature.df, window, fn))


@_quiet
def ts_decay_linear(feature, window: int):
    """vnpy 原版的权重是 window, window−1, …, 1（最早的值权重最大），这里照原版"""
    weights = np.arange(window, 0, -1, dtype=np.float64)
    denom = window * (window + 1) // 2
    return _proxy(_apply(feature.df, window, lambda w, nl, p: (w * weights).sum(axis=1) / denom))


@_quiet
def ts_product(feature, window: int):
    return _proxy(_apply(feature.df, window, lambda w, nl, p: np.prod(w, axis=1)))


FAST: dict[str, Callable] = {"ts_mean": ts_mean, "ts_std": ts_std, "ts_rank": ts_rank, "ts_argmax": ts_argmax,
                             "ts_argmin": ts_argmin, "ts_quantile": ts_quantile, "ts_decay_linear": ts_decay_linear,
                             "ts_product": ts_product}


@contextmanager
def patched():
    """with patched(): 期间 vnpy 的 calculate_by_expression 用上面这些向量化算子（可以嵌套，线程安全）"""
    global _depth
    import vnpy.alpha.dataset.ts_function as tsf
    with _lock:
        if _depth == 0:
            for name, fn in FAST.items():
                _saved[name] = getattr(tsf, name)
                setattr(tsf, name, fn)
        _depth += 1
    try:
        yield
    finally:
        with _lock:
            _depth -= 1
            if _depth == 0:
                for name, fn in _saved.items():
                    setattr(tsf, name, fn)
                _saved.clear()
