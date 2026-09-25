"""
数据源：em_datacenter（东方财富数据中心 datacenter-web.eastmoney.com / datacenter.eastmoney.com，
只用这两个域名，绝不碰被墙的 push2/push2his）。

股东户数、股权质押走 akshare 现成函数（本身按页数不大，不算"批量下载"）；
限售解禁走 akshare 现成函数（已按日期区间过滤）；业绩预告走 akshare 现成函数（已按报告期过滤）；
股东增减持（stock_ggcg_em）akshare 会翻遍全部历史，改成自己按公告日过滤 + 翻页上限直接请求。
龙虎榜直接复用 market.pools 按日期区间抓取的函数。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import polars as pl

from .. import config, net
from .base import SCHEMAS, DataProvider, Unavailable


log = logging.getLogger("quant_web.providers.em_datacenter")

EM_URL: str = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HOLDER_TRADES_COLUMNS: str = (
    "SECURITY_CODE,SECURITY_NAME_ABBR,HOLDER_NAME,DIRECTION,CHANGE_NUM,CHANGE_FREE_RATIO,"
    "NOTICE_DATE,START_DATE,END_DATE"
)
HOLDER_TRADES_PAGE_CAP: int = 20     # 每页500行，够覆盖普通的日期区间查询


def _parse_day(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------- 纯解析（便于单测）

def parse_holder_count(pdf: Any) -> pl.DataFrame:
    """ak.stock_zh_a_gdhs 返回的中文列 → SCHEMAS["holder_count"]（单位已是户数/股，不用换算）"""
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["holder_count"])
    df = pl.from_pandas(pdf).rename({
        "代码": "code", "股东户数统计截止日-本次": "end_date", "公告日期": "notice_date",
        "股东户数-本次": "holders", "股东户数-上次": "holders_prev",
        "股东户数-增减比例": "change_pct", "户均持股数量": "avg_shares",
    })
    return df.with_columns(pl.col("code").cast(pl.Utf8).str.zfill(6)).select(list(SCHEMAS["holder_count"]))


def parse_unlock(pdf: Any) -> pl.DataFrame:
    """ak.stock_restricted_release_detail_em 返回的中文列 → SCHEMAS["unlock"]。

    解禁数量/市值 akshare 自己已经 ×10000（万→个/元）；占比字段"占解禁前流通市值比例"实测是小数
    （如 0.0036 表示 0.36%），×100 换成本项目"百分数"的约定（1.5 = 1.5%）。
    """
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["unlock"])
    df = pl.from_pandas(pdf).rename({
        "股票代码": "code", "解禁时间": "date", "解禁数量": "shares",
        "占解禁前流通市值比例": "float_ratio", "限售股类型": "kind",
    })
    return df.with_columns(
        pl.col("code").cast(pl.Utf8).str.zfill(6), (pl.col("float_ratio") * 100).alias("float_ratio"),
    ).select(list(SCHEMAS["unlock"]))


def parse_pledge(pdf: Any) -> pl.DataFrame:
    """ak.stock_gpzy_pledge_ratio_em 返回的中文列 → SCHEMAS["pledge"]。

    "质押比例"已是百分数（如78.74=78.74%），不用换算；"质押股数"实测是万股（用质押市值/质押股数
    反推出隐含股价，和该股实际股价一致才确认过，如 *ST三房 2026-09-24 隐含价1.47元=实际股价1.47元），
    ×10000 换成股。
    """
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["pledge"])
    df = pl.from_pandas(pdf).rename({"股票代码": "code", "交易日期": "date", "质押比例": "pledge_ratio",
                                     "质押股数": "pledge_shares"})
    return df.with_columns(
        pl.col("code").cast(pl.Utf8).str.zfill(6), (pl.col("pledge_shares") * 10000).alias("pledge_shares"),
    ).select(list(SCHEMAS["pledge"]))


def parse_forecast(pdf: Any, period: date) -> pl.DataFrame:
    """ak.stock_yjyg_em 返回的中文列 → SCHEMAS["forecast"]。

    period 由调用方传入（查询用的报告期，函数返回的表本身不含报告期这一列）。
    "业绩变动幅度"只有单值（没有区间），change_low / change_high 先都填这个值——
    背后的 datacenter-web 报表其实有 ADD_AMP_LOWER / ADD_AMP_UPPER 两个字段，但 akshare 这个函数没有取。
    """
    if pdf is None or pdf.empty:
        return pl.DataFrame(schema=SCHEMAS["forecast"])
    df = pl.from_pandas(pdf).rename({
        "股票代码": "code", "公告日期": "notice_date", "预告类型": "kind",
        "业绩变动幅度": "change_low", "业绩变动": "summary",
    })
    return df.with_columns(
        pl.col("code").cast(pl.Utf8).str.zfill(6), pl.lit(period).alias("period"),
        pl.col("change_low").alias("change_high"),
    ).select(list(SCHEMAS["forecast"]))


def parse_holder_trades(rows: list[dict]) -> pl.DataFrame:
    """datacenter-web RPT_SHARE_HOLDER_INCREASE 原始行 → SCHEMAS["holder_trades"]。

    CHANGE_NUM 实测是万股（变动后持股数 AFTER_HOLDER_NUM 换算出的隐含持股比例与 AFTER_CHANGE_RATE
    一致，确认过是万），×10000 换成股；CHANGE_FREE_RATIO 已是百分数。
    """
    out: list[dict] = []
    for r in rows or []:
        code = str(r.get("SECURITY_CODE") or "").zfill(6)
        if not code or code == "000000":
            continue
        notice = str(r.get("NOTICE_DATE") or "")[:10]
        start = str(r.get("START_DATE") or "")[:10]
        end = str(r.get("END_DATE") or "")[:10]
        shares = r.get("CHANGE_NUM")
        out.append({
            "code": code, "notice_date": notice or None, "holder": r.get("HOLDER_NAME"),
            "direction": r.get("DIRECTION"), "shares": (float(shares) * 10000) if shares is not None else None,
            "ratio_float": r.get("CHANGE_FREE_RATIO"), "start": start or None, "end": end or None,
        })
    if not out:
        return pl.DataFrame(schema=SCHEMAS["holder_trades"])
    df = pl.DataFrame(out)
    for col in ("notice_date", "start", "end"):
        df = df.with_columns(pl.col(col).str.to_date("%Y-%m-%d", strict=False))
    return df.select(list(SCHEMAS["holder_trades"]))


# ---------------------------------------------------------------- 抓取

def _holder_trades_page(start: date, end: date, page: int) -> tuple[list[dict], int]:
    payload = net.get_json(EM_URL, params={
        "sortColumns": "NOTICE_DATE", "sortTypes": "-1", "pageSize": "500", "pageNumber": str(page),
        "reportName": "RPT_SHARE_HOLDER_INCREASE", "columns": HOLDER_TRADES_COLUMNS, "source": "WEB", "client": "WEB",
        "filter": f"(NOTICE_DATE>='{start.isoformat()}')(NOTICE_DATE<='{end.isoformat()}')",
    }, timeout=20)
    if not isinstance(payload, dict):
        raise ConnectionError("东方财富股东增减持接口返回格式异常")
    result = payload.get("result")
    if not result:
        return [], 0
    return result.get("data") or [], int(result.get("pages") or 0)


class EmDatacenterProvider(DataProvider):
    name = "em_datacenter"
    label = "东方财富数据中心"
    description = "东方财富数据中心（datacenter-web）：股东户数、限售解禁、股权质押、业绩预告、股东增减持、龙虎榜"
    capabilities = ("holder_count", "unlock", "pledge", "forecast", "holder_trades", "lhb")

    def fetch_holder_count(self, period: str | date | None = None) -> pl.DataFrame:
        import akshare as ak

        symbol: str = "最新" if period is None else _parse_day(period).strftime("%Y%m%d")
        pdf = net.call_with_fallback(lambda: ak.stock_zh_a_gdhs(symbol=symbol))
        return parse_holder_count(pdf)

    def fetch_unlock(self, start: str | date, end: str | date) -> pl.DataFrame:
        import akshare as ak

        start_d, end_d = _parse_day(start), _parse_day(end)
        pdf = net.call_with_fallback(
            lambda: ak.stock_restricted_release_detail_em(start_date=start_d.strftime("%Y%m%d"),
                                                           end_date=end_d.strftime("%Y%m%d"))
        )
        return parse_unlock(pdf)

    def fetch_pledge(self, day: str | date | None = None) -> pl.DataFrame:
        import akshare as ak

        if day is not None:
            d: date = _parse_day(day)
            return parse_pledge(net.call_with_fallback(lambda: ak.stock_gpzy_pledge_ratio_em(date=d.strftime("%Y%m%d"))))

        from ..market import realtime

        candidates: list[date] = realtime.recent_trading_days(8, until=datetime.now(config.CHINA_TZ).date())[::-1]
        errors: list[str] = []
        for d in candidates:
            try:
                pdf = net.call_with_fallback(lambda d=d: ak.stock_gpzy_pledge_ratio_em(date=d.strftime("%Y%m%d")))
            except Exception as e:  # noqa: BLE001  这天报错就试下一天
                errors.append(f"{d}: {e}")
                continue
            parsed: pl.DataFrame = parse_pledge(pdf)
            if not parsed.is_empty():
                return parsed
        raise Unavailable(f"最近 {len(candidates)} 个交易日都没有股权质押数据：" + "；".join(errors[-3:]))

    def fetch_forecast(self, period: str | date | None = None) -> pl.DataFrame:
        import akshare as ak

        if period is not None:
            p0: date = _parse_day(period)
            return parse_forecast(net.call_with_fallback(lambda: ak.stock_yjyg_em(date=p0.strftime("%Y%m%d"))), p0)

        from ..market import fundamentals as fundamentals_mod

        candidates: list[date] = list(reversed(fundamentals_mod.report_periods()[-6:]))
        errors: list[str] = []
        for p in candidates:
            try:
                pdf = net.call_with_fallback(lambda p=p: ak.stock_yjyg_em(date=p.strftime("%Y%m%d")))
            except Exception as e:  # noqa: BLE001  这期报错就试上一期
                errors.append(f"{p}: {e}")
                continue
            parsed: pl.DataFrame = parse_forecast(pdf, p)
            if not parsed.is_empty():
                return parsed
        raise Unavailable(f"最近 {len(candidates)} 个报告期都没有业绩预告数据：" + "；".join(errors[-3:]))

    def fetch_holder_trades(self, start: str | date, end: str | date) -> pl.DataFrame:
        start_d, end_d = _parse_day(start), _parse_day(end)
        rows, pages = _holder_trades_page(start_d, end_d, 1)
        for page in range(2, min(pages, HOLDER_TRADES_PAGE_CAP) + 1):
            more, _ = _holder_trades_page(start_d, end_d, page)
            rows.extend(more)
        if pages > HOLDER_TRADES_PAGE_CAP:
            log.warning("股东增减持 %s~%s 共 %d 页，只取了前 %d 页", start_d, end_d, pages, HOLDER_TRADES_PAGE_CAP)
        return parse_holder_trades(rows)

    def fetch_lhb(self, start: str | date, end: str | date) -> pl.DataFrame:
        from ..market import pools

        return pools.fetch_lhb(_parse_day(start), _parse_day(end))
