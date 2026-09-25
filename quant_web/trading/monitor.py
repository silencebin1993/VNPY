"""
盘中监控线程：程序开着时，交易时段每隔 monitor.interval_sec 秒调用一次 service.run_intraday
（模拟盘撮合、实盘止损触发、开盘提交排队委托、异动提醒）。非交易时段只是等待。
和 scheduler.py 相互独立（互不影响）；时钟、间隔、执行函数都可以注入，方便测试。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

log = logging.getLogger("quant_web.monitor")


class Monitor:
    def __init__(self, now_fn: Callable[[], datetime] | None = None, tick_fn: Callable[[datetime], dict] | None = None,
                 enabled_fn: Callable[[], tuple[bool, int]] | None = None) -> None:
        from . import calendar as tcal
        self.now_fn = now_fn or tcal.china_now
        self.tick_fn = tick_fn
        self.enabled_fn = enabled_fn or _enabled_from_settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: str | None = None
        self.last_result: dict | None = None
        self.last_error: str | None = None
        self.ticks: int = 0

    def tick(self) -> dict | None:
        from . import calendar as tcal
        now = self.now_fn()
        if tcal.session(now) not in ("auction", "open"):
            return None
        fn = self.tick_fn
        if fn is None:
            from . import service
            fn = service.run_intraday
        try:
            self.last_result = fn(now)
            self.last_error = None
        except Exception as e:  # noqa: BLE001  单次失败只记录，下次继续
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("盘中监控出错")
        self.last_tick = now.strftime("%Y-%m-%d %H:%M:%S")
        self.ticks += 1
        return self.last_result

    def _loop(self) -> None:
        self._stop.wait(10)
        while not self._stop.is_set():
            enabled, interval = self.enabled_fn()
            if enabled:
                self.tick()
            self._stop.wait(max(10, interval))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="quant-web-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        enabled, interval = self.enabled_fn()
        return {"running": bool(self._thread and self._thread.is_alive()), "enabled": enabled, "interval_sec": interval,
                "last_tick": self.last_tick, "last_error": self.last_error, "ticks": self.ticks, "last_result": self.last_result}


def _enabled_from_settings() -> tuple[bool, int]:
    try:
        from .. import settings as settings_mod
        m = settings_mod.load().monitor
        return bool(m.enabled), int(m.interval_sec)
    except Exception:  # noqa: BLE001
        return True, 30


MONITOR: Monitor = Monitor()


def start() -> None:
    MONITOR.start()


def stop() -> None:
    MONITOR.stop()


def wait_idle(seconds: float) -> None:        # 测试辅助
    time.sleep(seconds)
