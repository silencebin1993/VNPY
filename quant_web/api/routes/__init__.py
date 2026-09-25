"""
第三版新接口：每个功能一个 APIRouter 模块，server.create_app() 在旧路由之后、静态文件兜底路由之前调用 register(app)。

某个路由模块导入出错时只记录日志、跳过它（其余接口照常工作），与旧接口"模块出错只影响对应接口"的约定一致。
"""
import importlib
import logging

from fastapi import FastAPI


log = logging.getLogger("quant_web.api")

# 按顺序登记；新增功能在这里加一行
MODULES: list[str] = ["system", "providers", "indicators", "formula", "analysis", "screener", "trading", "lab", "strategy"]


def register(app: FastAPI) -> list[str]:
    loaded: list[str] = []
    for name in MODULES:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
            app.include_router(module.router)
            loaded.append(name)
        except Exception:  # noqa: BLE001  单个功能的接口出错不影响其他接口
            log.exception("加载接口模块 %s 失败", name)
    return loaded
