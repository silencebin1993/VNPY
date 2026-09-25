"""
API 公共工具：JSON 转换、中文错误处理、线程安全缓存、交易时段/交易日、参数校验。

从 server.py 抽出（行为不变），供 server.py 和 api/routes/* 新路由共用；
server.py 重新导出这些名字，旧代码和测试里的 server.xxx 照常可用。
"""
import importlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import polars as pl
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import config
from .. import settings as settings_mod


log = logging.getLogger("quant_web.api")


# ================================================================ JSON 转换

def jsonable(obj: Any) -> Any:
    """转换成可直接 json.dumps 的结构：NaN/inf → null，日期 → 字符串，表格 → 行列表"""
    if obj is None or isinstance(obj, str | bool | int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k if isinstance(k, str) else str(jsonable(k)): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset | deque):
        return [jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        try:
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:          # pandas NaT
            return None
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dtime):
        return obj.strftime("%H:%M:%S")
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    if isinstance(obj, Decimal):
        return jsonable(float(obj))
    if isinstance(obj, Enum):
        return jsonable(obj.value)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, BaseModel):
        return jsonable(obj.model_dump())
    if is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(asdict(obj))
    if isinstance(obj, pl.DataFrame):
        return jsonable(obj.to_dicts())
    if isinstance(obj, pl.Series):
        return jsonable(obj.to_list())
    module: str = type(obj).__module__
    if module.startswith("pandas"):
        if hasattr(obj, "to_dict") and hasattr(obj, "columns"):
            return jsonable(obj.to_dict("records"))
        if hasattr(obj, "tolist"):
            return jsonable(obj.tolist())
        return None if str(obj) in ("NaT", "<NA>", "nan") else str(obj)
    if module.startswith("numpy"):
        if type(obj).__name__ == "datetime64":
            text: str = str(obj)
            return None if text == "NaT" else text[:19].replace("T", " ")
        if hasattr(obj, "tolist"):
            return jsonable(obj.tolist())
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(
            jsonable(content), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")


def ok(obj: Any, status_code: int = 200) -> SafeJSONResponse:
    return SafeJSONResponse(obj, status_code=status_code)


# ================================================================ 错误处理

def describe_error(e: BaseException) -> tuple[int, str]:
    """异常 → (HTTP 状态码, 中文说明)"""
    if isinstance(e, settings_mod.SettingsError):
        return 400, str(e)
    if isinstance(e, ImportError):
        name: str = getattr(e, "name", None) or str(e)
        return 503, f"这个功能需要的模块还没有准备好（{name}），请稍后再试或更新程序"
    if isinstance(e, NotImplementedError):
        return 501, "这个功能还没有实现"
    if isinstance(e, ConnectionError | TimeoutError):
        return 502, f"网络数据源暂时连不上，请稍后重试。{e}"
    if isinstance(e, FileNotFoundError):
        name = Path(e.filename).name if e.filename else str(e)
        return 409, f"缺少需要的数据文件（{name}），请先更新数据或训练模型"
    if isinstance(e, PermissionError):
        return 409, "文件被其他程序占用（比如用 Excel 打开了持仓文件），请关闭后重试"
    if isinstance(e, OSError) and type(e).__module__.startswith(("requests", "urllib3")):
        return 502, f"网络数据源暂时连不上，请稍后重试。{e}"
    if isinstance(e, ValueError):
        return 400, str(e) or "参数不正确"
    if isinstance(e, RuntimeError):
        return 409, str(e) or "当前状态下不能执行这个操作"
    if isinstance(e, MemoryError):
        return 500, "内存不足，请关闭其他程序后重试"
    return 500, f"服务器内部出错：{type(e).__name__}: {e}"


class ErrorMiddleware:
    """兜底：任何未处理的异常都转成 {"detail": 中文说明}，不让服务器返回 HTML 错误页或断开连接"""

    def __init__(self, app: ASGIApp) -> None:
        self.app: ASGIApp = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started: bool = False

        async def _send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as e:  # noqa: BLE001  统一转成 JSON 错误
            if started:
                raise
            status, detail = describe_error(e)
            if status >= 500:
                log.exception("接口出错 %s", scope.get("path"))
            else:
                log.info("接口返回 %s %s：%s", status, scope.get("path"), detail)
            await SafeJSONResponse({"detail": detail}, status_code=status)(scope, receive, send)


# ================================================================ 缓存与公共工具

class Cache:
    """线程安全的小缓存：ttl 秒内或 sig（数据签名）不变时直接返回；同一个 key 同时只计算一次"""

    def __init__(self, max_items: int = 256) -> None:
        self._data: OrderedDict[Any, tuple[float, Any, Any]] = OrderedDict()
        self._locks: dict[Any, threading.Lock] = {}
        self._lock = threading.Lock()
        self._max: int = max_items

    def _valid(self, hit: tuple | None, ttl: float | None, sig: Any) -> bool:
        return hit is not None and hit[1] == sig and (ttl is None or time.monotonic() - hit[0] < ttl)

    def get(self, key: Any, fn: Callable[[], Any], ttl: float | None = None, sig: Any = None) -> Any:
        with self._lock:
            hit = self._data.get(key)
            key_lock: threading.Lock = self._locks.setdefault(key, threading.Lock())
        if self._valid(hit, ttl, sig):
            return hit[2]           # type: ignore[index]
        with key_lock:
            with self._lock:
                hit = self._data.get(key)
            if self._valid(hit, ttl, sig):
                return hit[2]       # type: ignore[index]
            value: Any = fn()
            with self._lock:
                self._data[key] = (time.monotonic(), sig, value)
                self._data.move_to_end(key)
                while len(self._data) > self._max:
                    old, _ = self._data.popitem(last=False)
                    self._locks.pop(old, None)
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


CACHE = Cache()


def mod(name: str) -> Any:
    """延迟导入 quant_web 子模块，如 mod("market.realtime")"""
    return importlib.import_module(f"quant_web.{name}")


def china_now() -> datetime:
    return datetime.now(config.CHINA_TZ)


def now_text() -> str:
    return china_now().strftime("%Y-%m-%d %H:%M:%S")


def _file_sig(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def panel_sig() -> tuple:
    """日线面板 + 股票列表的文件签名（修改时间+大小），用于缓存失效"""
    items: list[tuple] = []
    try:
        with os.scandir(config.PANEL_DIR) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(".parquet"):
                    st = entry.stat()
                    items.append((entry.name, st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    return (str(config.PANEL_DIR), tuple(sorted(items)), _file_sig(config.UNIVERSE_FILE))

def fallback_phase(now: datetime) -> str:
    """不看节假日的简易交易状态（行情模块不可用时使用）"""
    if now.weekday() >= 5:
        return "休市"
    minute: int = now.hour * 60 + now.minute
    if minute < 9 * 60 + 15:
        return "盘前"
    if minute < 11 * 60 + 30:
        return "交易中"
    if minute < 13 * 60:
        return "午间休市"
    if minute < 15 * 60:
        return "交易中"
    return "已收盘"


def phase() -> str:
    def build() -> str:
        try:
            return str(mod("market.realtime").market_phase())
        except Exception:  # noqa: BLE001  行情模块不可用时按时间估算
            return fallback_phase(china_now())
    return CACHE.get("phase", build, ttl=20)


def latest_trading_day() -> date:
    """最近一个有行情的交易日：交易时段和收盘后是今天，盘前和休市日是上一个交易日"""
    today: date = china_now().date()
    if phase() in ("交易中", "午间休市", "已收盘"):
        return today
    try:
        days: list[date] = mod("market.realtime").recent_trading_days(1, until=today - timedelta(days=1))
        if days:
            return days[-1]
    except Exception:  # noqa: BLE001  日历不可用时看面板
        pass
    try:
        last: date | None = mod("market.history").last_date()
        if last is not None:
            return last
    except Exception:  # noqa: BLE001
        pass
    day: date = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def temperature_label(t: float | None) -> str | None:
    if t is None or not isinstance(t, int | float) or not math.isfinite(t):
        return None
    try:
        return str(mod("market.sentiment").temperature_label(float(t)))
    except Exception:  # noqa: BLE001  与契约相同的分档
        return "冰点" if t < 20 else "低迷" if t < 40 else "正常" if t < 60 else "活跃" if t < 80 else "过热"


def safe(fn: Callable[[], Any], default: Any, warnings: list[str], label: str) -> Any:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001  单个数据源失败只记警告
        warnings.append(f"{label}暂时取不到：{describe_error(e)[1]}")
        return default

def check_code(code: str) -> str:
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(400, f"「{code}」不是有效的股票代码，应为6位数字，例如 600519")
    try:
        valid: bool = mod("market.universe").is_a_share(code)
    except ImportError:
        valid = True
    if not valid:
        raise HTTPException(400, f"「{code}」不是沪深京A股代码（不支持B股、基金等）")
    return code


def parse_day(text: str | None) -> date | None:
    if not text:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise HTTPException(400, f"日期「{text}」格式不对，应为 YYYYMMDD，例如 20260924")
