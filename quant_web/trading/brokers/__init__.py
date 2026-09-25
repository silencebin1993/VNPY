"""
券商接口注册表：名称 → (模块, 类名)。延迟导入，某个接口的依赖没装不影响其他接口。
"""
from __future__ import annotations

import importlib

from .base import Broker

BROKERS: dict[str, tuple[str, str]] = {
    "paper": ("paper", "PaperBroker"),
    "manual": ("manual", "ManualBroker"),
    "qmt": ("qmt", "QmtBroker"),
    "easytrader": ("easytrader_broker", "EasytraderBroker"),
    "vnpy_gateway": ("vnpy_bridge", "VnpyGatewayBroker"),
}


def broker_class(name: str) -> type[Broker]:
    if name not in BROKERS:
        raise ValueError(f"不认识的券商接口「{name}」")
    module, cls = BROKERS[name]
    return getattr(importlib.import_module(f"{__name__}.{module}"), cls)


def get_broker(account: dict, settings: dict | None = None) -> Broker:
    return broker_class(account["broker"])(account, settings or {})


def listing(settings: dict | None = None) -> list[dict]:
    """给页面看的接口清单（能不能用、为什么）"""
    out: list[dict] = []
    for name in BROKERS:
        try:
            cls = broker_class(name)
            ok, why = cls({"id": "", "broker": name}, settings or {}).available()
            out.append({"name": name, "label": cls.label, "description": cls.description, "is_live": cls.is_live,
                        "can_auto": cls.can_auto, "optional": cls.optional, "available": ok, "reason": why})
        except Exception as e:  # noqa: BLE001  接口文件本身出错
            out.append({"name": name, "label": name, "description": "", "is_live": True, "can_auto": False, "optional": True,
                        "available": False, "reason": f"接口加载失败：{e}"})
    return out
