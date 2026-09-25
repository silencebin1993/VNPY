"""
数据源：tencent（腾讯行情：qt.gtimg.cn / ifzq.gtimg.cn / proxy.finance.qq.com）。

个股日线复用 market.history 里已核对过昨收算法的下载逻辑；实时行情、分钟线复用 market.realtime。
指数日线另写了一个小的直连请求（复用 history.KLINE_URL 常量），因为 realtime.kline() 对非五大指数
（沪深300/中证500/中证1000等）会走"手/股自动识别"，指数的高低价是点位而不是价格，识别会不准。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from .. import config, net
from ..market import history
from ..market import universe as uni_mod
from .base import SCHEMAS, DataProvider, Unavailable


MINUTE_URL: str = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
# 这几个前缀的指数（沪深300/中证500/中证1000/深证成指/创业板指等）腾讯K线的成交量本身已经是股，不用 ×100
_RAW_VOLUME_PREFIXES: tuple[str, ...] = ("sh688", "sz399", "sh000", "sz000")


def _parse_day(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _minute_schema() -> dict[str, pl.DataType]:
    return {**{k: v for k, v in SCHEMAS["minute_bars"].items() if k != "time"}, "time": pl.Utf8}


# ---------------------------------------------------------------- 纯解析（便于单测）

def parse_quotes(rows: list[dict]) -> pl.DataFrame:
    """realtime.quotes() 返回的行 → SCHEMAS["quotes"]（取五档买卖的第一档）"""
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["quotes"])
    out: list[dict] = []
    for r in rows:
        bids: list[list[float]] = r.get("bids") or []
        asks: list[list[float]] = r.get("asks") or []
        out.append({
            "code": r.get("code"), "name": r.get("name"), "price": r.get("price"),
            "preclose": r.get("prev_close"), "open": r.get("open"), "high": r.get("high"),
            "low": r.get("low"), "volume": r.get("volume"), "amount": r.get("amount"),
            "pct": r.get("pct"), "limit_up": r.get("limit_up"), "limit_down": r.get("limit_down"),
            "bid1": bids[0][0] if bids else None, "ask1": asks[0][0] if asks else None,
            "bid1_vol": bids[0][1] if bids else None, "ask1_vol": asks[0][1] if asks else None,
            "time": r.get("time"),
        })
    return pl.DataFrame(out, schema=SCHEMAS["quotes"], strict=False)


def parse_minute1(points: list[dict], day: str, code: str) -> pl.DataFrame:
    """realtime.minute() 的 points（逐分钟最新价，无独立开高低收）→ SCHEMAS["minute_bars"]（价格同时充当开高低收）"""
    rows: list[dict] = [{
        "time": f"{day} {p['time']}:00", "code": str(code).zfill(6),
        "open": p["price"], "high": p["price"], "low": p["price"], "close": p["price"],
        "volume": p.get("volume"), "amount": p.get("amount"),
    } for p in points if p.get("price") is not None]
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["minute_bars"])
    df = pl.DataFrame(rows, schema=_minute_schema())
    return df.with_columns(pl.col("time").str.to_datetime("%Y-%m-%d %H:%M:%S")).select(list(SCHEMAS["minute_bars"]))


def parse_mkline(bars: list[list], code: str, board: str) -> pl.DataFrame:
    """mkline 接口的 m5/m15/m30/m60（[time,open,close,high,low,volume,{},换手%]）→ SCHEMAS["minute_bars"]。

    没有独立的成交额字段（只有逐bar换手率%），amount 留空；volume 主板/创业板是手（×100换股），
    科创板已经是股（不用换算），和日线的手/股约定一致。
    """
    if not bars:
        return pl.DataFrame(schema=SCHEMAS["minute_bars"])
    scale: float = 1.0 if board == "star" else 100.0
    rows: list[dict] = []
    for b in bars:
        if not isinstance(b, list) or len(b) < 6:
            continue
        try:
            t: str = str(b[0])
            rows.append({
                "time": f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:00", "code": str(code).zfill(6),
                "open": float(b[1]), "close": float(b[2]), "high": float(b[3]), "low": float(b[4]),
                "volume": float(b[5]) * scale, "amount": None,
            })
        except (TypeError, ValueError, IndexError):
            continue
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["minute_bars"])
    df = pl.DataFrame(rows, schema=_minute_schema())
    return df.with_columns(pl.col("time").str.to_datetime("%Y-%m-%d %H:%M:%S")).select(list(SCHEMAS["minute_bars"]))


def parse_index_kline(bars: list[list], index: str, start_d: date | None, end_d: date | None) -> pl.DataFrame:
    """newfqkline 的 day/qfqday（[date,open,close,high,low,volume,{},turn,amount(万)]）→ SCHEMAS["index_bars"]"""
    if not bars:
        return pl.DataFrame(schema=SCHEMAS["index_bars"])
    scale: float = 1.0 if index.startswith(_RAW_VOLUME_PREFIXES) else 100.0
    rows: list[dict] = []
    for b in bars:
        try:
            rows.append({
                "date": b[0], "open": float(b[1]), "close": float(b[2]), "high": float(b[3]),
                "low": float(b[4]), "volume": float(b[5]) * scale,
                "amount": round(float(b[8]) * 10000, 2) if len(b) > 8 and b[8] not in (None, "") else None,
            })
        except (TypeError, ValueError, IndexError):
            continue
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["index_bars"])
    df = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date("%Y-%m-%d"), pl.lit(index).alias("index"))
    if start_d is not None:
        df = df.filter(pl.col("date") >= start_d)
    if end_d is not None:
        df = df.filter(pl.col("date") <= end_d)
    return df.select(list(SCHEMAS["index_bars"]))


# ---------------------------------------------------------------- 抓取

def _index_kline_page(sym: str, end: str, count: int) -> list[list]:
    j = net.get_json(history.KLINE_URL, params={"param": f"{sym},day,,{end},{count},"}, timeout=10)
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict):
        if isinstance(j, dict) and j.get("msg"):
            raise ConnectionError(f"腾讯K线接口报错：{j.get('msg')}")
        return []
    item = data.get(sym) or {}
    bars = item.get("day") or item.get("qfqday") or []
    return [b for b in bars if isinstance(b, list) and len(b) >= 6]


def _fetch_index_kline(sym: str, start_d: date | None, first_count: int) -> list[list]:
    """从最新往回翻页，直到取到 start_d 之前的一根或翻到没有更多数据为止"""
    bars: list[list] = []
    end: str = ""
    count: int = max(2, min(first_count, history.RAW_PAGE))
    while True:
        chunk: list[list] = _index_kline_page(sym, end, count)
        if bars:
            chunk = [b for b in chunk if b[0] < bars[0][0]]
        if not chunk:
            return bars
        bars = chunk + bars
        if start_d is None or len(chunk) < count:
            return bars
        earliest: date = datetime.strptime(bars[0][0], "%Y-%m-%d").date()
        if earliest <= start_d:
            return bars
        end = (earliest - timedelta(days=1)).isoformat()
        count = history.RAW_PAGE


class TencentProvider(DataProvider):
    name = "tencent"
    label = "腾讯行情"
    description = "腾讯财经行情接口：个股日线/分钟线、实时报价、指数日线"
    capabilities = ("daily_bars", "quotes", "minute_bars", "index_bars")

    def fetch_daily_bars(self, code: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        code = str(code).zfill(6)
        start_d: date = _parse_day(start) or date.fromisoformat(config.HISTORY_START)
        end_d: date | None = _parse_day(end)
        df, _first = history.fetch_stock(code, need_from=start_d, end=end_d)
        return df.select(list(SCHEMAS["daily_bars"]))

    def fetch_quotes(self, codes: list[str]) -> pl.DataFrame:
        from ..market import realtime

        return parse_quotes(realtime.quotes(codes))

    def fetch_minute_bars(self, code: str, period: str = "5", count: int = 320) -> pl.DataFrame:
        period = str(period)
        if period in ("1", "1min"):
            from ..market import realtime

            data: dict = realtime.minute(code)
            day: str = data.get("date") or datetime.now(config.CHINA_TZ).date().isoformat()
            return parse_minute1(data.get("points") or [], day, code)
        if period not in ("5", "15", "30", "60"):
            raise Unavailable(f"腾讯分钟线不支持周期「{period}」（支持 1/5/15/30/60）")
        symbol: str = uni_mod.market_symbol(code)
        j = net.get_json(MINUTE_URL, params={"param": f"{symbol},m{period},,{count}"}, timeout=10)
        data = ((j or {}).get("data") or {}).get(symbol) or {}
        return parse_mkline(data.get(f"m{period}") or [], code, uni_mod.board_of(code))

    def fetch_index_bars(self, index: str = "sh000300", start: str | date | None = None,
                         end: str | date | None = None) -> pl.DataFrame:
        sym: str = str(index).strip().lower()
        start_d, end_d = _parse_day(start), _parse_day(end)
        span_days: int = (end_d - start_d).days if start_d and end_d else 3650
        bars: list[list] = _fetch_index_kline(sym, start_d, min(max(span_days + 10, 30), history.RAW_PAGE))
        return parse_index_kline(bars, sym, start_d, end_d)
