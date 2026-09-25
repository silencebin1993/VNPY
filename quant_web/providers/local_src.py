"""
数据源：local（本机已下载的数据）。

离线优先：只读本地文件（日线面板、股票列表、龙虎榜、财务存档），不发任何网络请求。
本地没有这份数据时返回空表（不算错误），registry 会自动换下一个数据源。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from ..market import fundamentals as fundamentals_mod
from ..market import history, pools
from ..market import universe as uni_mod
from .base import SCHEMAS, DataProvider


def _parse_day(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


class LocalProvider(DataProvider):
    name = "local"
    label = "本地数据"
    description = "本机已下载的数据（日线面板、股票列表、龙虎榜、财务存档等），不联网，最快"
    capabilities = ("daily_bars", "stock_list", "industry", "trade_calendar", "lhb", "fundamentals")

    def fetch_daily_bars(self, code: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        df: pl.DataFrame = history.load_panel(
            start=start, end=end, codes=[str(code).zfill(6)],
            columns=["open", "high", "low", "close", "preclose", "volume", "amount", "turn"],
        )
        return df.select(list(SCHEMAS["daily_bars"]))

    def fetch_stock_list(self) -> pl.DataFrame:
        uni: pl.DataFrame = uni_mod.load_universe()
        if uni.is_empty():
            return pl.DataFrame(schema=SCHEMAS["stock_list"])
        return uni.filter(pl.col("status") == 1).select(["code", "name"])

    def fetch_industry(self) -> pl.DataFrame:
        uni: pl.DataFrame = uni_mod.load_universe()
        if uni.is_empty():
            return pl.DataFrame(schema=SCHEMAS["industry"])
        return uni.filter((pl.col("status") == 1) & pl.col("industry").is_not_null()).select(["code", "industry"])

    def fetch_trade_calendar(self) -> pl.DataFrame:
        """交易日历：复用 realtime.trade_calendar()（内存/文件多级缓存，通常不联网；缓存也没有时它会自己联网兜底）"""
        from ..market import realtime

        days: list[date] = realtime.trade_calendar()
        if not days:
            return pl.DataFrame(schema=SCHEMAS["trade_calendar"])
        return pl.DataFrame({"date": days}, schema=SCHEMAS["trade_calendar"])

    def fetch_lhb(self, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        df: pl.DataFrame = pools.load_lhb()
        if df.is_empty():
            return df
        start_d, end_d = _parse_day(start), _parse_day(end)
        if start_d is not None:
            df = df.filter(pl.col("date") >= start_d)
        if end_d is not None:
            df = df.filter(pl.col("date") <= end_d)
        return df

    def fetch_fundamentals(self) -> pl.DataFrame:
        return fundamentals_mod.load_fundamentals()
