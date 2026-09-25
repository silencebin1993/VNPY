"""
回测与样本外评价共用的小统计工具。

- daily_t：按日收益的普通 t 值 = 均值 / 标准差 × √天数（假设各天独立）；
- nw_t：Newey-West（Bartlett 核，默认滞后 5 天）t 值。持有多天的策略相邻几天的收益来自同一批持仓，
  彼此相关，普通 t 值会偏高；NW 把前后 5 天的自相关也算进方差里，更保守。
"""
import math
from typing import Any

import numpy as np


NW_LAG: int = 5


def _clean(x: Any) -> np.ndarray:
    arr: np.ndarray = np.asarray(x, dtype=float)
    return arr[np.isfinite(arr)]


def _flat(d: np.ndarray, mean: float) -> bool:
    """去均值后全是（浮点误差级的）0：常数序列，t 值没有意义"""
    return float(np.abs(d).max()) <= 1e-12 * max(1.0, abs(mean))


def daily_t(x: Any) -> float | None:
    """均值 / 样本标准差 × √n；少于 3 个点或标准差为 0 时为 None"""
    arr: np.ndarray = _clean(x)
    if len(arr) < 3 or _flat(arr - arr.mean(), float(arr.mean())):
        return None
    std: float = float(arr.std(ddof=1))
    return float(arr.mean() / std * math.sqrt(len(arr))) if std > 0 else None


def nw_t(x: Any, lag: int = NW_LAG) -> float | None:
    """均值的 Newey-West t 值：长期方差 = γ0 + 2 Σ_{l=1..lag} (1 - l/(lag+1)) γl（γl 为 l 阶自协方差，除以 n）；
    点数不足（< lag + 3）或方差不为正时为 None"""
    arr: np.ndarray = _clean(x)
    n: int = len(arr)
    if n < max(3, lag + 3):
        return None
    d: np.ndarray = arr - arr.mean()
    if _flat(d, float(arr.mean())):
        return None
    lrv: float = float(d @ d) / n
    for k in range(1, min(lag, n - 1) + 1):
        lrv += 2.0 * (1.0 - k / (lag + 1)) * float(d[k:] @ d[:-k]) / n
    if not lrv > 0:
        return None
    return float(arr.mean() / math.sqrt(lrv / n))


def rounded(v: float | None, digits: int = 3) -> float | None:
    return None if v is None or not math.isfinite(v) else round(float(v), digits)
