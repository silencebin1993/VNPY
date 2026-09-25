"""
数据源：akshare（通用兜底，只用不碰 push2/push2his 的函数：新浪、腾讯、中证指数官网）。

_guarded() 在真正发请求前检查一遍函数源码里有没有 push2/push2his 的域名，
防止以后 akshare 升级、内部实现换了接口却没人发现（本机这两个域名不通）。
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, TypeVar

import polars as pl

from .. import config, net
from ..market import universe as uni_mod
from .base import SCHEMAS, DataProvider, Unavailable


T = TypeVar("T")

_BLOCKED_HOSTS: tuple[str, ...] = ("push2.eastmoney.com", "push2his.eastmoney.com")
_CSINDEX_CODE: dict[str, str] = {"hs300": "000300", "zz500": "000905", "zz1000": "000852"}


def _guarded(func: Callable[..., T]) -> Callable[..., T]:
    """调用前检查函数源码没有引用被墙的域名，命中就直接抛 Unavailable（不发请求）"""
    try:
        src: str = inspect.getsource(func)
    except (OSError, TypeError):
        src = ""
    hit: str | None = next((h for h in _BLOCKED_HOSTS if h in src), None)
    if hit:
        raise Unavailable(f"{getattr(func, '__name__', func)} 请求的是本机无法访问的 {hit}")
    return func


def _parse_day(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------- 纯解析（便于单测）

def parse_stock_list(pdf: Any) -> pl.DataFrame:
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["stock_list"])
    df = pl.from_pandas(pdf[["code", "name"]])
    return df.with_columns(pl.col("code").cast(pl.Utf8).str.zfill(6)).select(list(SCHEMAS["stock_list"]))


def parse_trade_calendar(pdf: Any) -> pl.DataFrame:
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["trade_calendar"])
    df = pl.from_pandas(pdf[["trade_date"]]).rename({"trade_date": "date"})
    return df.select(list(SCHEMAS["trade_calendar"]))


def parse_index_members_csindex(pdf: Any, index: str) -> pl.DataFrame:
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["index_members"])
    df = pl.from_pandas(pdf[["成分券代码", "成分券名称"]]).rename({"成分券代码": "code", "成分券名称": "name"})
    return df.with_columns(pl.lit(index).alias("index"), pl.lit(None, dtype=pl.Float64).alias("weight")) \
        .select(list(SCHEMAS["index_members"]))


def parse_daily_bars_tx(pdf: Any, code: str) -> pl.DataFrame:
    """ak.stock_zh_a_hist_tx（不复权）→ SCHEMAS["daily_bars"]；akshare 已把 volume 换成股、amount 换成元，
    但 turnover 被它除了 100（变成小数），这里再乘回本项目"百分数"的约定，preclose 用上一行收盘价近似"""
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["daily_bars"])
    df = pl.from_pandas(pdf).rename({"turnover": "turn"})
    df = df.with_columns(
        pl.col("date").cast(pl.Date), pl.lit(str(code).zfill(6)).alias("code"), (pl.col("turn") * 100).alias("turn"),
    ).sort("date")
    df = df.with_columns(pl.col("close").shift(1).alias("preclose"))
    return df.select(list(SCHEMAS["daily_bars"]))


def parse_index_bars_tx(pdf: Any, index: str) -> pl.DataFrame:
    """ak.stock_zh_index_daily_tx：只有 date/open/close/high/low/amount，没有 volume"""
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["index_bars"])
    df = pl.from_pandas(pdf).with_columns(pl.col("date").cast(pl.Date), pl.lit(index).alias("index"))
    if "volume" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("volume"))
    return df.select(list(SCHEMAS["index_bars"]))


def parse_index_bars_sina(pdf: Any, index: str) -> pl.DataFrame:
    """ak.stock_zh_index_daily：只有 date/open/high/low/close/volume，没有 amount"""
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["index_bars"])
    df = pl.from_pandas(pdf).with_columns(pl.col("date").cast(pl.Date), pl.lit(index).alias("index"))
    if "amount" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("amount"))
    return df.select(list(SCHEMAS["index_bars"]))


# ---------------------------------------------------------------- 数据源

class AkshareProvider(DataProvider):
    name = "akshare"
    label = "akshare(通用)"
    description = "akshare 里不走 push2/push2his 的接口：新浪、腾讯、中证指数官网，作为通用兜底"
    capabilities = ("stock_list", "trade_calendar", "index_members", "index_bars", "daily_bars")

    def fetch_stock_list(self) -> pl.DataFrame:
        import akshare as ak

        fn = _guarded(ak.stock_info_a_code_name)      # 先检查源码（一次），再交给重试逻辑
        pdf = net.call_with_fallback(lambda: fn())
        return parse_stock_list(pdf)

    def fetch_trade_calendar(self) -> pl.DataFrame:
        import akshare as ak

        fn = _guarded(ak.tool_trade_date_hist_sina)
        pdf = net.call_with_fallback(lambda: fn())
        return parse_trade_calendar(pdf)

    def fetch_index_members(self, index: str) -> pl.DataFrame:
        import akshare as ak

        code: str | None = _CSINDEX_CODE.get(index)
        if code is None:
            raise Unavailable(f"akshare(中证指数) 不支持指数「{index}」（只支持 {'/'.join(_CSINDEX_CODE)}）")
        fn = _guarded(ak.index_stock_cons_csindex)
        pdf = net.call_with_fallback(lambda: fn(symbol=code))
        return parse_index_members_csindex(pdf, index)

    def fetch_daily_bars(self, code: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        import akshare as ak

        sym: str = uni_mod.market_symbol(code)
        start_d: date = _parse_day(start) or date.fromisoformat(config.HISTORY_START)
        end_d: date = _parse_day(end) or date.today()
        fn = _guarded(ak.stock_zh_a_hist_tx)
        pdf = net.call_with_fallback(lambda: fn(
            symbol=sym, start_date=start_d.strftime("%Y%m%d"), end_date=end_d.strftime("%Y%m%d"), adjust=""))
        return parse_daily_bars_tx(pdf, code)

    def fetch_index_bars(self, index: str = "sh000300", start: str | date | None = None,
                         end: str | date | None = None) -> pl.DataFrame:
        import akshare as ak

        start_d: date = _parse_day(start) or date.fromisoformat(config.HISTORY_START)
        end_d: date = _parse_day(end) or date.today()
        tx_fn = _guarded(ak.stock_zh_index_daily_tx)
        try:
            pdf = net.call_with_fallback(lambda: tx_fn(
                symbol=index, start_date=start_d.strftime("%Y%m%d"), end_date=end_d.strftime("%Y%m%d")))
            df: pl.DataFrame = parse_index_bars_tx(pdf, index)
            if not df.is_empty():
                return df
        except Exception:  # noqa: BLE001  腾讯这条路失败/没数据再试新浪
            pass
        sina_fn = _guarded(ak.stock_zh_index_daily)
        pdf = net.call_with_fallback(lambda: sina_fn(symbol=index))
        df = parse_index_bars_sina(pdf, index)
        if df.is_empty():
            return df
        return df.filter((pl.col("date") >= start_d) & (pl.col("date") <= end_d))
