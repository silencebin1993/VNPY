"""
数据源：baostock（证券宝，http://baostock.com，登录制、限流，只作兜底）。

每次调用独立登录/登出，复用 market.universe 里的全局锁（baostock 是进程内单连接，不能并发）和
guard_baostock_socket（连接被服务器断开时不会一直卡住）。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import polars as pl

from .. import net
from ..market import universe as uni_mod
from .base import SCHEMAS, DataProvider, Unavailable


_INDEX_QUERY: dict[str, str] = {"hs300": "query_hs300_stocks", "zz500": "query_zz500_stocks",
                                "sz50": "query_sz50_stocks"}

DAILY_FIELDS: str = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"


def _parse_day(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _rows(rs: Any) -> list[list[str]]:
    out: list[list[str]] = []
    while rs.error_code == "0" and rs.next():
        out.append(rs.get_row_data())
    if rs.error_code != "0":
        raise ConnectionError(f"baostock 查询失败：{rs.error_msg}")
    return out


def _un_prefix(code: str) -> str:
    """"sh.600000" → "600000" """
    return str(code).split(".")[-1].zfill(6)


def _industry_label(text: str) -> str | None:
    """"C39计算机、通信和其他电子设备制造业" → "计算机、通信和其他电子设备制造业" """
    text = re.sub(r"^[A-Z]\d*", "", str(text or "")).strip()
    return text or None


# ---------------------------------------------------------------- 纯解析（便于单测）

def parse_index_members(rows: list[list[str]], index: str) -> pl.DataFrame:
    """query_hs300/zz500/sz50_stocks 的行（updateDate, code, code_name）→ SCHEMAS["index_members"]（不含权重）"""
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["index_members"])
    out: list[dict] = [{"index": index, "code": _un_prefix(r[1]), "name": r[2] if len(r) > 2 else None,
                        "weight": None} for r in rows]
    return pl.DataFrame(out, schema=SCHEMAS["index_members"], orient="row")


def parse_industry(rows: list[list[str]]) -> pl.DataFrame:
    """query_stock_industry 的行（updateDate, code, code_name, industry, industryClassification）→ SCHEMAS["industry"]"""
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["industry"])
    out: list[dict] = [{"code": _un_prefix(r[1]), "industry": _industry_label(r[3]) if len(r) > 3 else None}
                       for r in rows]
    df = pl.DataFrame(out, schema=SCHEMAS["industry"], orient="row")
    return df.filter(pl.col("industry").is_not_null())


def parse_trade_calendar(rows: list[list[str]]) -> pl.DataFrame:
    """query_trade_dates 的行（calendar_date, is_trading_day）→ SCHEMAS["trade_calendar"]"""
    days: list[date] = [date.fromisoformat(r[0]) for r in rows if len(r) > 1 and r[1] == "1"]
    if not days:
        return pl.DataFrame(schema=SCHEMAS["trade_calendar"])
    return pl.DataFrame({"date": sorted(days)}, schema=SCHEMAS["trade_calendar"])


def parse_daily_bars(rows: list[list[str]], code: str) -> pl.DataFrame:
    """query_history_k_data_plus（DAILY_FIELDS 顺序）的行 → SCHEMAS["daily_bars"]（preclose/volume/amount/turn 已是标准单位）"""
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["daily_bars"])
    out: list[dict] = []
    for r in rows:
        if len(r) > 11 and r[11] == "0":       # tradestatus=0 停牌，价格是空/0，跳过
            continue
        try:
            out.append({
                "date": r[0], "code": code, "open": float(r[2]), "high": float(r[3]), "low": float(r[4]),
                "close": float(r[5]), "preclose": float(r[6]) if r[6] not in ("", None) else None,
                "volume": float(r[7]) if r[7] not in ("", None) else None,
                "amount": float(r[8]) if r[8] not in ("", None) else None,
                "turn": float(r[10]) if r[10] not in ("", None) else None,
            })
        except (ValueError, IndexError):
            continue
    if not out:
        return pl.DataFrame(schema=SCHEMAS["daily_bars"])
    return pl.DataFrame(out).with_columns(pl.col("date").str.to_date("%Y-%m-%d")).select(list(SCHEMAS["daily_bars"]))


# ---------------------------------------------------------------- 抓取

def _query(fn_name: str, timeout: float = 60.0, **kwargs: Any) -> list[list[str]]:
    """登录 → 查询 → 登出，全程加锁 + domestic_direct + 断线保护"""
    import baostock as bs

    with uni_mod.BAOSTOCK_LOCK, net.domestic_direct():
        lg = bs.login()
        if lg.error_code != "0":
            raise ConnectionError(f"baostock 登录失败：{lg.error_msg}")
        uni_mod.guard_baostock_socket(timeout)
        try:
            rs = getattr(bs, fn_name)(**kwargs)
            return _rows(rs)
        finally:
            bs.logout()


class BaostockProvider(DataProvider):
    name = "baostock"
    label = "证券宝(baostock)"
    description = "baostock 免费行情接口，需要登录、限流、较慢，只作最后的兜底"
    capabilities = ("index_members", "industry", "trade_calendar", "daily_bars")

    def fetch_index_members(self, index: str) -> pl.DataFrame:
        fn_name: str | None = _INDEX_QUERY.get(index)
        if fn_name is None:
            raise Unavailable(f"baostock 不支持指数「{index}」（只支持 {'/'.join(_INDEX_QUERY)}）")
        return parse_index_members(_query(fn_name), index)

    def fetch_industry(self) -> pl.DataFrame:
        return parse_industry(_query("query_stock_industry", timeout=90.0))

    def fetch_trade_calendar(self) -> pl.DataFrame:
        end: str = date(datetime.now().year, 12, 31).isoformat()
        return parse_trade_calendar(_query("query_trade_dates", start_date="2014-01-01", end_date=end))

    def fetch_daily_bars(self, code: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        code = str(code).zfill(6)
        start_d: str = _parse_day(start).isoformat() if start else "2019-01-01"
        end_d: str = _parse_day(end).isoformat() if end else date.today().isoformat()
        rows = _query("query_history_k_data_plus", code=uni_mod.bs_symbol(code), fields=DAILY_FIELDS,
                      start_date=start_d, end_date=end_d, frequency="d", adjustflag="3")
        return parse_daily_bars(rows, code)
