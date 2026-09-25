"""
通达信口径的基础函数：指标库（ta.py）、公式系统（formula/）、主力分析共用。

数据约定：一张表可以包含多只股票，必须已按 (code, date) 排好序，同一只股票的行连在一起。
函数都是"Series 进、Series 出"，结果和输入逐行对齐。每只股票单独计算，窗口不会跨到上一只股票。

与通达信/同花顺一致的口径：
- MA(X,N)：简单平均，不足 N 天为空；STD(X,N)：样本标准差（除以 N-1），不足 N 天为空；
- EMA(X,N)：Y=(2X+(N-1)Y')/(N+1)，第一天 Y=X；SMA(X,N,M)：Y=(M·X+(N-M)·Y')/N，第一天 Y=X；
- HHV/LLV/SUM/COUNT/EXIST：不足 N 天时用已有的天数（N=0 表示从第一天累计）；EVERY 不足 N 天为假；
- REF(X,N)：N 天前的值，前面没有数据时为空；CROSS(A,B)：今天 A>B 且昨天 A<=B；
- BARSLAST(X)：上一次 X 成立到今天的天数（今天成立 = 0；从没成立过为空）；
- SLOPE(X,N)：最近 N 天的线性回归斜率；WMA(X,N)：加权平均（越近权重越大，权重 1..N）；
- AVEDEV(X,N)：平均绝对偏差；FILTER(X,N)：X 成立后，其后 N 天内的信号全部忽略。

实现要点（全市场约 900 万行也要快）：
- 窗口类函数在整列上一次性计算，再把"窗口跨到上一只股票"的行改为分组内累计值或空值，不做 group_by；
- 递推类函数（EMA/SMA/累计）用 .over(分组)。
"""
from __future__ import annotations

import numpy as np
import polars as pl


class Ctx:
    """分组上下文：记录每行属于第几只股票（gid）和它在该股票里是第几天（pos，从 0 开始）"""

    def __init__(self, codes: pl.Series | np.ndarray | list | None, n: int | None = None) -> None:
        if codes is None:
            if n is None:
                raise ValueError("单只股票时要给出行数 n")
            self.n: int = int(n)
            self.gid: np.ndarray = np.zeros(self.n, dtype=np.int64)
            self.start: np.ndarray = np.zeros(self.n, dtype=np.int64)
        else:
            arr: np.ndarray = codes.to_numpy() if isinstance(codes, pl.Series) else np.asarray(codes)
            self.n = len(arr)
            change: np.ndarray = np.ones(self.n, dtype=bool)
            if self.n > 1:
                change[1:] = arr[1:] != arr[:-1]
            self.gid = np.cumsum(change) - 1
            starts: np.ndarray = np.flatnonzero(change)
            self.start = starts[self.gid] if self.n else np.zeros(0, dtype=np.int64)
        self.idx: np.ndarray = np.arange(self.n, dtype=np.int64)
        self.pos: np.ndarray = self.idx - self.start
        self._gid_s: pl.Series = pl.Series("_g", self.gid)
        self._pos_s: pl.Series = pl.Series("_p", self.pos)

    @property
    def groups(self) -> int:
        return int(self.gid[-1]) + 1 if self.n else 0

    def over(self, x: pl.Series, fn) -> pl.Series:
        """在每只股票内部做递推计算：fn(pl.col("x")) -> pl.Expr"""
        df = pl.DataFrame({"_g": self._gid_s, "x": x})
        return df.select(fn(pl.col("x")).over("_g")).to_series().alias(x.name)

    def warm(self, n: int) -> pl.Series:
        """窗口已满 n 天的行（True）"""
        return self._pos_s >= (n - 1)


# ---------------------------------------------------------------- 工具

def _f(x: pl.Series) -> pl.Series:
    return x.cast(pl.Float64, strict=False)


def _mask(x: pl.Series, ok: pl.Series) -> pl.Series:
    """ok 为 False 的行置空"""
    return pl.select(pl.when(ok).then(x).otherwise(None)).to_series().alias(x.name)


def _where(cond: pl.Series, a: pl.Series, b: pl.Series) -> pl.Series:
    return pl.select(pl.when(cond).then(a).otherwise(b)).to_series()


def as_series(x, ctx: Ctx, name: str = "x") -> pl.Series:
    """常数或 Series → 与数据等长的 Float64 Series"""
    if isinstance(x, pl.Series):
        return x
    return pl.Series(name, np.full(ctx.n, float(x)), dtype=pl.Float64)


def _cum_within(x: pl.Series, ctx: Ctx, how: str) -> pl.Series:
    if how == "sum":
        return ctx.over(x, lambda e: e.fill_null(0).cum_sum())
    if how == "max":
        return ctx.over(x, lambda e: e.cum_max())
    return ctx.over(x, lambda e: e.cum_min())


# ---------------------------------------------------------------- 基础函数

def ref(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = int(n)
    if n <= 0:
        return x
    return _mask(x.shift(n), pl.Series(ctx.pos >= n))


def ref_dynamic(x: pl.Series, n: pl.Series, ctx: Ctx) -> pl.Series:
    """REF(X,N) 的 N 逐行不同（例如 REF(C,BARSLAST(条件)+1)）：取不到（空值、负数、跨到上一只股票）时为空"""
    xs: np.ndarray = _f(x).to_numpy()
    nv: np.ndarray = _f(n).to_numpy()
    ok: np.ndarray = np.isfinite(nv) & (nv >= 0)
    k: np.ndarray = np.where(ok, nv, 0).astype(np.int64)
    src: np.ndarray = ctx.idx - k
    ok &= src >= ctx.start
    out: np.ndarray = np.full(ctx.n, np.nan)
    out[ok] = xs[src[ok]]
    return pl.Series(x.name, out).fill_nan(None)


def _bars_since_extreme(x: pl.Series, n: int, ctx: Ctx, highest: bool, chunk: int = 1_000_000) -> pl.Series:
    """N 日内最高（最低）值出现到今天的天数；不足 N 天时用已有的天数；并列取最近的一天"""
    n = max(int(n), 1)
    xs: np.ndarray = _f(x).to_numpy().astype(np.float64)
    fill: float = -np.inf if highest else np.inf
    xs = np.where(np.isnan(xs), fill, xs)
    pad: np.ndarray = np.concatenate([np.full(n - 1, fill), xs])
    out: np.ndarray = np.full(len(xs), np.nan)
    for s in range(0, len(xs), chunk):
        e: int = min(s + chunk, len(xs))
        win: np.ndarray = np.lib.stride_tricks.sliding_window_view(pad[s:e + n - 1], n).copy()
        j: np.ndarray = (np.arange(s, e)[:, None] - (n - 1)) + np.arange(n)[None, :]      # 窗口里每个位置的全局行号
        win[j < ctx.start[s:e, None]] = fill                                              # 跨到上一只股票的部分作废
        rev: np.ndarray = win[:, ::-1]                                                    # 反过来找，并列时取最近
        pos: np.ndarray = np.argmax(rev, axis=1) if highest else np.argmin(rev, axis=1)
        out[s:e] = pos
    return pl.Series(x.name, out)


def hhvbars(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    return _bars_since_extreme(x, n, ctx, True)


def llvbars(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    return _bars_since_extreme(x, n, ctx, False)


def ma(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = max(int(n), 1)
    return _mask(_f(x).rolling_mean(n, min_samples=n), ctx.warm(n))


def std(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = max(int(n), 2)
    return _mask(_f(x).rolling_std(n, min_samples=n, ddof=1), ctx.warm(n))


def ema(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = max(int(n), 1)
    return ctx.over(_f(x), lambda e: e.ewm_mean(alpha=2.0 / (n + 1), adjust=False, ignore_nulls=True))


def sma(x: pl.Series, n: int, m: int, ctx: Ctx) -> pl.Series:
    n, m = max(int(n), 1), max(int(m), 1)
    if m > n:
        raise ValueError(f"SMA(X,N,M) 要求 M 不大于 N（现在 N={n}, M={m}）")
    return ctx.over(_f(x), lambda e: e.ewm_mean(alpha=m / n, adjust=False, ignore_nulls=True))


def dma(x: pl.Series, a: pl.Series | float, ctx: Ctx) -> pl.Series:
    """DMA(X,A)：Y=A·X+(1-A)·Y'（A 可以逐行变化，逐只股票递推）"""
    xs: np.ndarray = _f(x).to_numpy()
    av: np.ndarray = as_series(a, ctx).cast(pl.Float64).to_numpy()
    out: np.ndarray = np.full(ctx.n, np.nan)
    prev: float = np.nan
    for i in range(ctx.n):
        if ctx.pos[i] == 0:
            prev = np.nan
        xi, ai = xs[i], av[i]
        if np.isnan(xi):
            out[i] = prev
            continue
        if np.isnan(ai):
            ai = 1.0
        prev = xi if np.isnan(prev) else ai * xi + (1 - ai) * prev
        out[i] = prev
    return pl.Series(x.name, out).fill_nan(None)


def sum_(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = int(n)
    xs: pl.Series = _f(x)
    cum: pl.Series = _cum_within(xs, ctx, "sum")
    if n <= 0:
        return cum
    return _where(ctx.warm(n), xs.fill_null(0).rolling_sum(n, min_samples=1), cum).alias(x.name)


def hhv(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = int(n)
    xs: pl.Series = _f(x)
    cum: pl.Series = _cum_within(xs, ctx, "max")
    if n <= 0:
        return cum
    return _where(ctx.warm(n), xs.rolling_max(n, min_samples=1), cum).alias(x.name)


def llv(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = int(n)
    xs: pl.Series = _f(x)
    cum: pl.Series = _cum_within(xs, ctx, "min")
    if n <= 0:
        return cum
    return _where(ctx.warm(n), xs.rolling_min(n, min_samples=1), cum).alias(x.name)


def _bool(x: pl.Series) -> pl.Series:
    if x.dtype == pl.Boolean:
        return x.fill_null(False)
    return (_f(x).fill_null(0) != 0)


def count(cond: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    return sum_(_bool(cond).cast(pl.Float64), n, ctx)


def every(cond: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = max(int(n), 1)
    c: pl.Series = _bool(cond).cast(pl.Int8)
    full: pl.Series = c.rolling_min(n, min_samples=n) == 1
    return _where(ctx.warm(n), full.fill_null(False), pl.Series(np.zeros(ctx.n, dtype=bool))).alias("every")


def exist(cond: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = int(n)
    c: pl.Series = _bool(cond).cast(pl.Float64)
    return (hhv(c, n, ctx).fill_null(0) > 0).alias("exist")


def cross(a: pl.Series, b: pl.Series, ctx: Ctx) -> pl.Series:
    a, b = _f(a), _f(b)
    now: pl.Series = (a > b).fill_null(False)
    before: pl.Series = (ref(a, 1, ctx) <= ref(b, 1, ctx)).fill_null(False)
    return (now & before).alias("cross")


def barslast(cond: pl.Series, ctx: Ctx) -> pl.Series:
    c: np.ndarray = _bool(cond).to_numpy()
    last: np.ndarray = np.maximum.accumulate(np.where(c, ctx.idx, -1)) if ctx.n else np.zeros(0, dtype=np.int64)
    ok: np.ndarray = last >= ctx.start
    out: np.ndarray = np.where(ok, ctx.idx - last, -1).astype(np.float64)
    out[~ok] = np.nan
    return pl.Series("barslast", out).fill_nan(None)


def barscount(ctx: Ctx) -> pl.Series:
    """从第一天到今天共有几天（含今天）"""
    return pl.Series("barscount", (ctx.pos + 1).astype(np.float64))


def _rolling_k_sums(x: pl.Series, n: int) -> tuple[pl.Series, pl.Series, pl.Series]:
    """(Σx, Σk·x, k_end)：k 为全局行号；用于 SLOPE/WMA 的窗口内线性加权"""
    xs: pl.Series = _f(x)
    k: pl.Series = pl.Series(np.arange(len(xs), dtype=np.float64))
    sx: pl.Series = xs.rolling_sum(n, min_samples=n)
    skx: pl.Series = (xs * k).rolling_sum(n, min_samples=n)
    return sx, skx, k


def slope(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = max(int(n), 2)
    sx, skx, k = _rolling_k_sums(x, n)
    stx: pl.Series = skx - (k - (n - 1)) * sx          # Σ t·x，t = 0..n-1
    st: float = n * (n - 1) / 2.0
    stt: float = (n - 1) * n * (2 * n - 1) / 6.0
    out: pl.Series = (n * stx - st * sx) / (n * stt - st * st)
    return _mask(out, ctx.warm(n)).alias(x.name)


def wma(x: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    n = max(int(n), 1)
    sx, skx, k = _rolling_k_sums(x, n)
    stx: pl.Series = skx - (k - (n - 1)) * sx
    out: pl.Series = (stx + sx) / (n * (n + 1) / 2.0)   # 权重 t+1：最早一天 1，今天 n
    return _mask(out, ctx.warm(n)).alias(x.name)


def avedev(x: pl.Series, n: int, ctx: Ctx, chunk: int = 2_000_000) -> pl.Series:
    """平均绝对偏差：mean(|X - mean(X)|)，窗口 N 天；分块计算，避免一次占用太多内存"""
    n = max(int(n), 1)
    xs: np.ndarray = _f(x).to_numpy().astype(np.float64)
    out: np.ndarray = np.full(len(xs), np.nan)
    if len(xs) >= n:
        for s in range(0, len(xs) - n + 1, chunk):
            e: int = min(s + chunk, len(xs) - n + 1)
            win: np.ndarray = np.lib.stride_tricks.sliding_window_view(xs[s:e + n - 1], n)
            m: np.ndarray = win.mean(axis=1, keepdims=True)
            out[s + n - 1:e + n - 1] = np.abs(win - m).mean(axis=1)
    out[ctx.pos < n - 1] = np.nan
    return pl.Series(x.name, out).fill_nan(None)


def filter_(cond: pl.Series, n: int, ctx: Ctx) -> pl.Series:
    """FILTER(X,N)：X 成立后，其后 N 天内再成立的信号忽略（只依赖过去，不是未来函数）"""
    n = max(int(n), 0)
    c: np.ndarray = _bool(cond).to_numpy()
    out: np.ndarray = np.zeros(ctx.n, dtype=bool)
    last_kept: int = -10**12
    last_gid: int = -1
    for i in np.flatnonzero(c):                         # 只遍历成立的行（通常很少）
        g: int = int(ctx.gid[i])
        if g != last_gid:
            last_gid, last_kept = g, -10**12
        if i - last_kept > n:
            out[i] = True
            last_kept = int(i)
    return pl.Series("filter", out)


def sar(high: pl.Series, low: pl.Series, ctx: Ctx, step: float = 0.02, limit: float = 0.2) -> pl.Series:
    """抛物线转向 SAR（Wilder 算法，加速因子起始 step、每次 +step、最大 limit），逐只股票递推"""
    h: np.ndarray = _f(high).to_numpy()
    lo: np.ndarray = _f(low).to_numpy()
    out: np.ndarray = np.full(ctx.n, np.nan)
    i: int = 0
    while i < ctx.n:
        j: int = i
        while j + 1 < ctx.n and ctx.gid[j + 1] == ctx.gid[i]:
            j += 1
        _sar_one(h[i:j + 1], lo[i:j + 1], out[i:j + 1], step, limit)
        i = j + 1
    return pl.Series("sar", out).fill_nan(None)


def _sar_one(h: np.ndarray, lo: np.ndarray, out: np.ndarray, step: float, limit: float) -> None:
    n: int = len(h)
    if n < 2:
        return
    up: bool = h[1] + lo[1] >= h[0] + lo[0]
    af: float = step
    ep: float = max(h[0], h[1]) if up else min(lo[0], lo[1])
    s: float = min(lo[0], lo[1]) if up else max(h[0], h[1])
    out[1] = s
    for t in range(2, n):
        if np.isnan(h[t]) or np.isnan(lo[t]):
            out[t] = s
            continue
        s = s + af * (ep - s)
        if up:
            s = min(s, lo[t - 1], lo[t - 2])
            if lo[t] < s:                               # 跌破 SAR：转为下降趋势
                up, s, ep, af = False, ep, lo[t], step
            elif h[t] > ep:
                ep, af = h[t], min(af + step, limit)
        else:
            s = max(s, h[t - 1], h[t - 2])
            if h[t] > s:                                # 突破 SAR：转为上升趋势
                up, s, ep, af = True, ep, h[t], step
            elif lo[t] < ep:
                ep, af = lo[t], min(af + step, limit)
        out[t] = s


# ---------------------------------------------------------------- 逐元素函数（无分组）

def max_(a, b, ctx: Ctx) -> pl.Series:
    a, b = as_series(a, ctx), as_series(b, ctx)
    return pl.select(pl.max_horizontal(_f(a), _f(b))).to_series()


def min_(a, b, ctx: Ctx) -> pl.Series:
    a, b = as_series(a, ctx), as_series(b, ctx)
    return pl.select(pl.min_horizontal(_f(a), _f(b))).to_series()


def if_(cond, a, b, ctx: Ctx) -> pl.Series:
    c: pl.Series = _bool(as_series(cond, ctx))
    return _where(c, as_series(a, ctx).cast(pl.Float64), as_series(b, ctx).cast(pl.Float64)).alias("if")


def safe_div(a: pl.Series, b: pl.Series) -> pl.Series:
    """除数为 0 时结果为空（不产生 inf）"""
    b = _f(b)
    return _where((b != 0).fill_null(False), _f(a) / b, pl.Series([None] * len(b), dtype=pl.Float64))
