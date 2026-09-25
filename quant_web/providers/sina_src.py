"""
数据源：sina（新浪财经：资金流 vip.stock.finance.sina.com.cn、分钟线 quotes.sina.cn）。
"""
from __future__ import annotations

from typing import Any

import polars as pl

from .. import net
from ..market import info as info_mod
from ..market import universe as uni_mod
from .base import SCHEMAS, DataProvider, Unavailable


# ---------------------------------------------------------------- 纯解析（便于单测）

def parse_fund_flow(rows: Any, code: str) -> pl.DataFrame:
    """info.parse_sina_flow() 解析后的行 → SCHEMAS["fund_flow"]（补上 code 列）"""
    parsed: list[dict] = info_mod.parse_sina_flow(rows)
    if not parsed:
        return pl.DataFrame(schema=SCHEMAS["fund_flow"])
    df = pl.DataFrame(parsed)
    return df.with_columns(pl.col("date").str.to_date("%Y-%m-%d"), pl.lit(str(code).zfill(6)).alias("code")) \
        .select(list(SCHEMAS["fund_flow"]))


def parse_minute_bars(pdf: Any, code: str) -> pl.DataFrame:
    """ak.stock_zh_a_minute 返回（day/open/high/low/close/volume/amount，均已是股/元）→ SCHEMAS["minute_bars"]"""
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["minute_bars"])
    df = pl.from_pandas(pdf[["day", "open", "high", "low", "close", "volume", "amount"]])
    df = df.with_columns(
        pl.col("day").str.to_datetime("%Y-%m-%d %H:%M:%S").alias("time"),
        pl.lit(str(code).zfill(6)).alias("code"),
        pl.col(["open", "high", "low", "close", "volume", "amount"]).cast(pl.Float64),
    )
    return df.select(list(SCHEMAS["minute_bars"]))


# ---------------------------------------------------------------- 抓取

class SinaProvider(DataProvider):
    name = "sina"
    label = "新浪财经"
    description = "新浪财经接口：个股资金流向、分钟线"
    capabilities = ("fund_flow", "minute_bars")

    def fetch_fund_flow(self, code: str) -> pl.DataFrame:
        code = str(code).zfill(6)
        rows = net.get_json(info_mod.SINA_FLOW_URL, params={
            "page": 1, "num": info_mod.FUND_FLOW_DAYS, "sort": "opendate", "asc": 0,
            "daima": uni_mod.market_symbol(code),
        }, timeout=10, retries=2)
        if isinstance(rows, dict) and rows.get("__ERROR"):
            raise Unavailable(f"新浪资金流接口报错：{rows.get('__ERRORMSG')}")
        return parse_fund_flow(rows, code)

    def fetch_minute_bars(self, code: str, period: str = "5") -> pl.DataFrame:
        import akshare as ak

        period = str(period)
        if period not in ("1", "5", "15", "30", "60"):
            raise Unavailable(f"新浪分钟线不支持周期「{period}」（支持 1/5/15/30/60）")
        symbol: str = uni_mod.market_symbol(code)
        pdf = net.call_with_fallback(lambda: ak.stock_zh_a_minute(symbol=symbol, period=period, adjust=""))
        return parse_minute_bars(pdf, code)
