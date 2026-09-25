"""
数据源：qmt（可选，券商 miniQMT 的 xtquant.xtdata 本地接口；本机没装，无法实测）。

xtdata 的行情字段（open/high/low/close/volume/amount）按官方文档已经是股/元，不用换算；
分钟线/日线用 get_market_data_ex，实时报价用 get_full_tick。测试用注入 sys.modules 的假 xtquant 模块。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from typing import Any

import polars as pl

from .. import config
from ..market import universe as uni_mod
from .base import SCHEMAS, DataProvider, Unavailable


_EXCHANGE_SUFFIX: dict[str, str] = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
_PERIOD_MAP: dict[str, str] = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "60m"}
_FIELDS: list[str] = ["open", "high", "low", "close", "volume", "amount"]


def _parse_day(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def qmt_symbol(code: str) -> str:
    """6位代码 → QMT 代码，如 600519 → 600519.SH"""
    code = str(code).zfill(6)
    return f"{code}.{_EXCHANGE_SUFFIX[uni_mod.exchange_of(code)]}"


# ---------------------------------------------------------------- 纯解析（便于单测）

def parse_bars(raw: Any, code: str, index: bool = False) -> pl.DataFrame:
    """get_market_data_ex()[symbol] 返回的表（索引是 "YYYYMMDD" 或 "YYYYMMDDHHMMSS"，列是 _FIELDS）
    → SCHEMAS["daily_bars"] / SCHEMAS["index_bars"] / SCHEMAS["minute_bars"]（按 index 参数和索引长度判断）"""
    schema_name: str = "index_bars" if index else "daily_bars"
    if raw is None or getattr(raw, "empty", True):
        return pl.DataFrame(schema=SCHEMAS[schema_name])
    df = pl.from_pandas(raw.reset_index().rename(columns={raw.index.name or "index": "_t"}))
    stamp: str = str(df["_t"][0])
    minute: bool = len(stamp) > 8
    df = df.with_columns(
        pl.col("_t").cast(pl.Utf8).str.slice(0, 8).str.to_date("%Y%m%d").alias("date"),
    )
    if minute:
        df = df.with_columns(pl.col("_t").cast(pl.Utf8).str.to_datetime("%Y%m%d%H%M%S").alias("time"),
                             pl.lit(str(code).zfill(6)).alias("code"))
        return df.select(list(SCHEMAS["minute_bars"]))
    if index:
        df = df.with_columns(pl.lit(str(code)).alias("index"))
    else:
        df = df.with_columns(pl.lit(str(code).zfill(6)).alias("code"), pl.col("close").shift(1).alias("preclose"),
                             pl.lit(None, dtype=pl.Float64).alias("turn"))
    return df.select(list(SCHEMAS[schema_name]))


def parse_quotes(ticks: dict[str, dict], codes: list[str]) -> pl.DataFrame:
    """get_full_tick() 返回的 {symbol: tick字典} → SCHEMAS["quotes"]"""
    if not ticks:
        return pl.DataFrame(schema=SCHEMAS["quotes"])
    rows: list[dict] = []
    for code in codes:
        sym: str = qmt_symbol(code)
        t: dict | None = ticks.get(sym)
        if not t:
            continue
        bid: list = t.get("bidPrice") or []
        ask: list = t.get("askPrice") or []
        bid_vol: list = t.get("bidVol") or []
        ask_vol: list = t.get("askVol") or []
        rows.append({
            "code": str(code).zfill(6), "name": None, "price": t.get("lastPrice"), "preclose": t.get("lastClose"),
            "open": t.get("open"), "high": t.get("high"), "low": t.get("low"), "volume": t.get("volume"),
            "amount": t.get("amount"), "pct": None, "limit_up": None,
            "limit_down": None, "bid1": bid[0] if bid else None, "ask1": ask[0] if ask else None,
            "bid1_vol": bid_vol[0] if bid_vol else None, "ask1_vol": ask_vol[0] if ask_vol else None,
            "time": str(t.get("timetag") or ""),
        })
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["quotes"])
    return pl.DataFrame(rows, schema=SCHEMAS["quotes"], orient="row", strict=False)


class QmtProvider(DataProvider):
    name = "qmt"
    label = "QMT"
    description = "券商 miniQMT 本地行情接口（xtquant.xtdata，需要装 QMT 客户端，可选）"
    capabilities = ("daily_bars", "minute_bars", "quotes", "index_bars")
    optional = True

    def available(self) -> tuple[bool, str]:
        try:
            from .. import settings as settings_mod

            qmt_path: str = (settings_mod.load().providers.qmt_path or "").strip()
            if qmt_path and qmt_path not in sys.path:
                sys.path.append(qmt_path)
            import xtquant  # noqa: F401
        except ImportError:
            return False, "没有安装/找不到 xtquant（可选，需要安装 QMT 客户端并在设置里填安装目录）"
        return True, ""

    def fetch_daily_bars(self, code: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        from xtquant import xtdata

        start_d: date = _parse_day(start) or date.fromisoformat(config.HISTORY_START)
        end_d: date = _parse_day(end) or date.today()
        sym: str = qmt_symbol(code)
        data = xtdata.get_market_data_ex(_FIELDS, [sym], period="1d", start_time=start_d.strftime("%Y%m%d"),
                                         end_time=end_d.strftime("%Y%m%d"))
        return parse_bars((data or {}).get(sym), code)

    def fetch_minute_bars(self, code: str, period: str = "5", count: int = 320) -> pl.DataFrame:
        from xtquant import xtdata

        xt_period: str | None = _PERIOD_MAP.get(str(period))
        if xt_period is None:
            raise Unavailable(f"QMT 不支持分钟周期「{period}」（支持 1/5/15/30/60）")
        sym: str = qmt_symbol(code)
        data = xtdata.get_market_data_ex(_FIELDS, [sym], period=xt_period, count=count)
        return parse_bars((data or {}).get(sym), code)

    def fetch_quotes(self, codes: list[str]) -> pl.DataFrame:
        from xtquant import xtdata

        symbols: list[str] = [qmt_symbol(c) for c in codes]
        ticks: dict = xtdata.get_full_tick(symbols) or {}
        return parse_quotes(ticks, codes)

    def fetch_index_bars(self, index: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        from xtquant import xtdata

        start_d: date = _parse_day(start) or date.fromisoformat(config.HISTORY_START)
        end_d: date = _parse_day(end) or date.today()
        sym: str = str(index).upper()
        if sym[:2] in ("SH", "SZ", "BJ") and "." not in sym:
            sym = f"{sym[2:]}.{sym[:2]}"
        data = xtdata.get_market_data_ex(_FIELDS, [sym], period="1d", start_time=start_d.strftime("%Y%m%d"),
                                         end_time=end_d.strftime("%Y%m%d"))
        return parse_bars((data or {}).get(sym), index, index=True)
