"""
数据源：tushare（可选，需要 pip install tushare 并在设置里填 pro token；本机没装，无法实测）。

tushare pro.daily 的 vol 单位是"手"（×100换成股），amount 单位是"千元"（×1000换成元）；
pro.index_daily 同样约定。这两个换算没有本机环境验证，只是按 tushare 官方文档的字段说明实现，
测试用注入 sys.modules 的假 tushare 模块（见 tests）。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import polars as pl

from .. import config
from ..market import universe as uni_mod
from .base import SCHEMAS, DataProvider, Unavailable


_EXCHANGE_SUFFIX: dict[str, str] = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}


def _parse_day(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def ts_code(code: str) -> str:
    """6位代码 → tushare 代码，如 600519 → 600519.SH"""
    code = str(code).zfill(6)
    return f"{code}.{_EXCHANGE_SUFFIX[uni_mod.exchange_of(code)]}"


def from_ts_code(value: str) -> str:
    """600519.SH → 600519"""
    return str(value).split(".")[0].zfill(6)


# ---------------------------------------------------------------- 纯解析（便于单测）

def parse_daily_bars(pdf: Any, code: str) -> pl.DataFrame:
    """pro.daily 返回 → SCHEMAS["daily_bars"]；vol(手)×100→股，amount(千元)×1000→元。

    pro.daily 不带换手率（那是 pro.daily_basic 的 turnover_rate，要另调一次接口，权限/积分消耗更大），
    这里 turn 留空；不要跟 pct_chg（涨跌幅，含义完全不同）混用。
    """
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["daily_bars"])
    df = pl.from_pandas(pdf).rename({"trade_date": "date", "pre_close": "preclose", "vol": "volume"})
    df = df.with_columns(
        pl.col("date").cast(pl.Utf8).str.to_date("%Y%m%d"), pl.lit(str(code).zfill(6)).alias("code"),
        (pl.col("volume") * 100).alias("volume"), (pl.col("amount") * 1000).alias("amount"),
        pl.lit(None, dtype=pl.Float64).alias("turn"),
    )
    return df.sort("date").select(list(SCHEMAS["daily_bars"]))


def parse_index_bars(pdf: Any, index: str) -> pl.DataFrame:
    """pro.index_daily 返回 → SCHEMAS["index_bars"]；vol(手)×100→股，amount(千元)×1000→元"""
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["index_bars"])
    df = pl.from_pandas(pdf).rename({"trade_date": "date", "vol": "volume"})
    df = df.with_columns(
        pl.col("date").cast(pl.Utf8).str.to_date("%Y%m%d"), pl.lit(index).alias("index"),
        (pl.col("volume") * 100).alias("volume"), (pl.col("amount") * 1000).alias("amount"),
    )
    return df.sort("date").select(list(SCHEMAS["index_bars"]))


def parse_stock_list(pdf: Any) -> pl.DataFrame:
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["stock_list"])
    col: str = "symbol" if "symbol" in pdf.columns else "ts_code"
    df = pl.from_pandas(pdf[[col, "name"]]).rename({col: "code"})
    if col == "ts_code":
        df = df.with_columns(pl.col("code").map_elements(from_ts_code, return_dtype=pl.Utf8))
    return df.with_columns(pl.col("code").cast(pl.Utf8).str.zfill(6)).select(list(SCHEMAS["stock_list"]))


def parse_trade_calendar(pdf: Any) -> pl.DataFrame:
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["trade_calendar"])
    df = pl.from_pandas(pdf[["cal_date"]]).rename({"cal_date": "date"})
    return df.with_columns(pl.col("date").cast(pl.Utf8).str.to_date("%Y%m%d")).select(list(SCHEMAS["trade_calendar"]))


class TushareProvider(DataProvider):
    name = "tushare"
    label = "tushare"
    description = "tushare pro 接口（需要安装 tushare 并填写 token，可选）"
    capabilities = ("daily_bars", "index_bars", "stock_list", "trade_calendar")
    optional = True

    def available(self) -> tuple[bool, str]:
        try:
            import tushare  # noqa: F401
        except ImportError:
            return False, "没有安装 tushare（可选）：pip install tushare"
        from .. import settings as settings_mod

        token: str = (settings_mod.load().providers.tushare_token or "").strip()
        if not token:
            return False, "没有填写 tushare token（可选，在设置的数据源页填写后启用）"
        return True, ""

    def _pro(self) -> Any:
        import tushare as ts

        from .. import settings as settings_mod

        token: str = settings_mod.load().providers.tushare_token.strip()
        return ts.pro_api(token)

    def fetch_daily_bars(self, code: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        start_d: date = _parse_day(start) or date.fromisoformat(config.HISTORY_START)
        end_d: date = _parse_day(end) or date.today()
        pdf = self._pro().daily(ts_code=ts_code(code), start_date=start_d.strftime("%Y%m%d"),
                                end_date=end_d.strftime("%Y%m%d"))
        return parse_daily_bars(pdf, code)

    def fetch_index_bars(self, index: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
        start_d: date = _parse_day(start) or date.fromisoformat(config.HISTORY_START)
        end_d: date = _parse_day(end) or date.today()
        code: str = str(index).upper()
        if code[:2] in ("SH", "SZ") and "." not in code:
            code = f"{code[2:]}.{code[:2]}"
        pdf = self._pro().index_daily(ts_code=code, start_date=start_d.strftime("%Y%m%d"),
                                      end_date=end_d.strftime("%Y%m%d"))
        return parse_index_bars(pdf, index)

    def fetch_stock_list(self) -> pl.DataFrame:
        pdf = self._pro().stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name")
        return parse_stock_list(pdf)

    def fetch_trade_calendar(self) -> pl.DataFrame:
        pdf = self._pro().trade_cal(exchange="SSE", is_open="1")
        if pdf is None or pdf.empty:
            raise Unavailable("tushare 交易日历没有返回数据")
        return parse_trade_calendar(pdf)
