"""
数据源：exchange（上交所 query.sse.com.cn + 深交所 www.szse.cn 融资融券明细，官方数据源，最权威）。

两个交易所字段不完全一致：
- 上交所有"融资偿还额"，没有"融资融券余额"（用 rzrqye 留空，因为它没给融券的金额只有股数，算不出准确的两融余额）；
- 深交所有"融资融券余额"，没有"融资偿还额"。
两边字段单位实测都已是 元/股，不用换算（详见 tests / 报告）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import polars as pl

from .. import net
from .base import SCHEMAS, DataProvider


log = logging.getLogger("quant_web.providers.exchange")


def _parse_day(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def parse_sse_margin(pdf, day: date) -> pl.DataFrame:
    """上交所 stock_margin_detail_sse 返回的中文列 → SCHEMAS["margin"]"""
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["margin"])
    df = pl.from_pandas(pdf).rename({
        "标的证券代码": "code", "融资余额": "rzye", "融资买入额": "rzmre", "融资偿还额": "rzche",
        "融券余量": "rqyl", "融券卖出量": "rqmcl",
    })
    return df.with_columns(
        pl.col("code").cast(pl.Utf8).str.zfill(6), pl.lit(day).alias("date"),
        pl.lit(None, dtype=pl.Float64).alias("rzrqye"),
    ).select(list(SCHEMAS["margin"]))


def parse_szse_margin(pdf, day: date) -> pl.DataFrame:
    """深交所 stock_margin_detail_szse 返回的中文列 → SCHEMAS["margin"]"""
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["margin"])
    df = pl.from_pandas(pdf).rename({
        "证券代码": "code", "融资余额": "rzye", "融资买入额": "rzmre",
        "融券余量": "rqyl", "融券卖出量": "rqmcl", "融资融券余额": "rzrqye",
    })
    return df.with_columns(
        pl.col("code").cast(pl.Utf8).str.zfill(6), pl.lit(day).alias("date"),
        pl.lit(None, dtype=pl.Float64).alias("rzche"),
    ).select(list(SCHEMAS["margin"]))


class ExchangeProvider(DataProvider):
    name = "exchange"
    label = "交易所"
    description = "上交所/深交所官网的融资融券明细（最权威，但只有个股汇总，没有股东户数等其他数据）"
    capabilities = ("margin",)

    def fetch_margin(self, day: str | date) -> pl.DataFrame:
        import akshare as ak

        day_d: date = _parse_day(day)
        ymd: str = day_d.strftime("%Y%m%d")
        frames: list[pl.DataFrame] = []
        try:
            frames.append(parse_sse_margin(net.call_with_fallback(lambda: ak.stock_margin_detail_sse(date=ymd)), day_d))
        except Exception as e:  # noqa: BLE001  单边失败不影响另一边
            log.warning("上交所融资融券明细获取失败（%s）：%s", day_d, e)
        try:
            frames.append(parse_szse_margin(net.call_with_fallback(lambda: ak.stock_margin_detail_szse(date=ymd)), day_d))
        except Exception as e:  # noqa: BLE001
            log.warning("深交所融资融券明细获取失败（%s）：%s", day_d, e)
        frames = [f for f in frames if not f.is_empty()]
        if not frames:
            return pl.DataFrame(schema=SCHEMAS["margin"])
        return pl.concat(frames, how="vertical_relaxed")
