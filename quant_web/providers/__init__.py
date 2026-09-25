"""
通用数据接口（第三版）：数据源注册表 + 统一数据格式 + 扩展数据存档。

    from quant_web.providers import fetch, registry
    res = fetch("margin", day=date(2026, 9, 23))   # res.data（polars 表）/ res.source / res.warnings

详见 base.py（注册表、统一格式 SCHEMAS）、builtin.py（内置数据源）、store.py（本地存档）、updates.py（更新任务）。
"""
from .base import (
    CAPABILITIES, DEFAULT_CHAINS, SCHEMAS, DataProvider, FetchResult, ProviderError, ProviderRegistry, Unavailable,
    conform, fetch, registry,
)

__all__ = [
    "CAPABILITIES", "DEFAULT_CHAINS", "SCHEMAS", "DataProvider", "FetchResult", "ProviderError", "ProviderRegistry",
    "Unavailable", "conform", "fetch", "registry",
]
