"""
交易日与交易时段（北京时间）。优先用行情模块的交易日历，取不到时按"周一到周五"估算。
测试可以用 set_calendar() 注入固定的交易日列表。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .. import config

_override: list[date] | None = None


def set_calendar(days: list[date] | None) -> None:
    global _override
    _override = sorted(days) if days else None


def _days() -> list[date] | None:
    if _override is not None:
        return _override
    try:
        from ..market import realtime
        cal = realtime.trade_calendar()
        return sorted(cal) if cal else None
    except Exception:  # noqa: BLE001  日历不可用时按工作日估算
        return None


def is_trading_day(d: date) -> bool:
    days = _days()
    if days:
        import bisect
        i = bisect.bisect_left(days, d)
        if i < len(days) and days[i] == d:
            return True
        if d <= days[-1]:
            return False
    return d.weekday() < 5


def next_trading_day(d: date) -> date:
    x: date = d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(x):
            return x
        x += timedelta(days=1)
    return x


def prev_trading_day(d: date) -> date:
    x: date = d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(x):
            return x
        x -= timedelta(days=1)
    return x


def china_now() -> datetime:
    return datetime.now(config.CHINA_TZ)


def session(now: datetime | None = None) -> str:
    """pre（开盘前）/ auction（9:15–9:25 集合竞价）/ open（连续竞价）/ noon（午休）/ closed（收盘后）/ holiday"""
    now = now or china_now()
    if not is_trading_day(now.date()):
        return "holiday"
    t: time = now.time()
    if t < time(9, 15):
        return "pre"
    if t < time(9, 25):
        return "auction"
    if t < time(9, 30):
        return "pre_open"
    if t < time(11, 30):
        return "open"
    if t < time(13, 0):
        return "noon"
    if t < time(15, 0):
        return "open"
    return "closed"


def order_trade_date(now: datetime | None = None) -> date:
    """现在下单，委托属于哪个交易日：交易日收盘前 → 今天；收盘后/休市 → 下一个交易日"""
    now = now or china_now()
    s: str = session(now)
    if s in ("pre", "auction", "pre_open", "open", "noon"):
        return now.date()
    return next_trading_day(now.date())
