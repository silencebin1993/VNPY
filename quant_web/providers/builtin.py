"""
注册全部内置数据源。每个适配器单独 try/except：一个坏了不影响其他的注册（页面仍能看到其余数据源可用）。
"""
from __future__ import annotations

import logging

from .base import DataProvider, ProviderRegistry


log = logging.getLogger("quant_web.providers.builtin")

# (模块名, 类名)：懒加载，一个模块导入/实例化出错不影响其他
_ADAPTERS: list[tuple[str, str]] = [
    ("local_src", "LocalProvider"),
    ("tencent_src", "TencentProvider"),
    ("sina_src", "SinaProvider"),
    ("exchange_src", "ExchangeProvider"),
    ("em_datacenter_src", "EmDatacenterProvider"),
    ("baostock_src", "BaostockProvider"),
    ("akshare_src", "AkshareProvider"),
    ("news_src", "BuiltinNewsProvider"),
    ("tushare_src", "TushareProvider"),
    ("qmt_src", "QmtProvider"),
]


def register_all(reg: ProviderRegistry) -> None:
    for module_name, class_name in _ADAPTERS:
        try:
            module = __import__(f"{__package__}.{module_name}", fromlist=[class_name])
            provider_cls: type[DataProvider] = getattr(module, class_name)
            reg.register(provider_cls())
        except Exception:  # noqa: BLE001  单个数据源坏了不影响其他数据源注册
            log.exception("注册数据源 %s.%s 失败", module_name, class_name)
