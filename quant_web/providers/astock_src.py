"""
数据源：a-stock-data（https://github.com/silencebin1993/a-stock-data，原作者 simonlin1212，Apache-2.0）。

a-stock-data 本身是一个“Skill 文档 + 内嵌 Python”，不是 pip 包，没法直接 import。这里按它记录的官方/公开
接口（地址、参数、字段、单位都照它的实测说明）重写成本项目的数据源适配器，只取对本项目有增量的几项：

- 交易日历：深交所官网整月日历（官方口径，不靠工作日推断）；
- 指数成分股：中证指数 / 国证指数官网文件，**带权重**（别的数据源只有成分没有权重）；
- 新能力 指数估值：中证官网的市盈率、股息率（两种股本口径）；
- 新能力 个股研报：东方财富研报库（标题、机构、评级、未来三年 EPS 预测、PDF 地址）；
- 新能力 强势股题材：同花顺“强势股”每日人工整理的上涨原因（题材标签）；
- 新能力 机构调研：东方财富数据中心按公告日的全市场机构调研记录。

东财接口有风控（每秒 >5 次、1 分钟 >200 次会临时封 IP），这里所有 eastmoney.com 请求都串行、至少间隔 1 秒。
"""
from __future__ import annotations

import calendar
import re
import threading
import time
from datetime import date, datetime
from io import BytesIO
from typing import Any

import polars as pl

from .. import config, net
from .base import SCHEMAS, DataProvider, Unavailable

UA: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CSI_FILE: str = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/{kind}/{code}{kind}.xls"
CNI_FILE: str = "https://www.cnindex.com.cn/sample-detail/download-history"
SZSE_CALENDAR: str = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
EM_REPORT: str = "https://reportapi.eastmoney.com/report/list"
EM_DATACENTER: str = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_PDF: str = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
THS_HOT: str = "http://zx.10jqka.com.cn/event/api/getharden/date/{day}/orderby/date/orderway/desc/charset/GBK/"

# 本项目的指数简称 → (官网, 6 位指数代码)。创业板指由国证指数公司编制
INDEX_CODES: dict[str, tuple[str, str]] = {
    "hs300": ("csi", "000300"), "zz500": ("csi", "000905"), "zz1000": ("csi", "000852"),
    "zz2000": ("csi", "932000"), "sz50": ("csi", "000016"), "kc50": ("csi", "000688"),
    "cyb": ("cni", "399006"), "cyb50": ("cni", "399673"),
}

EM_MIN_INTERVAL: float = 1.0
_em_lock = threading.Lock()
_em_last: list[float] = [0.0]


def _day(value: str | date | None) -> date:
    if value is None:
        return datetime.now(config.CHINA_TZ).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text: str = str(value).strip()
    return datetime.strptime(text[:10], "%Y%m%d" if re.fullmatch(r"\d{8}", text) else "%Y-%m-%d").date()


def resolve_index(index: str) -> tuple[str, str]:
    """hs300 / 000300 / 399006 → (官网 csi/cni, 6 位代码)"""
    key: str = str(index).strip().lower()
    if key in INDEX_CODES:
        return INDEX_CODES[key]
    if re.fullmatch(r"\d{6}", key):
        return ("cni", key) if key.startswith("399") else ("csi", key)
    raise Unavailable(f"a-stock-data 不认识指数「{index}」（可用：{'/'.join(INDEX_CODES)}，或 6 位指数代码）")


def _em_get_json(url: str, params: dict, headers: dict | None = None) -> Any:
    """东财请求：全局串行 + 最小间隔 1 秒（a-stock-data 的防封做法）"""
    with _em_lock:
        wait: float = EM_MIN_INTERVAL - (time.time() - _em_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            return net.get_json(url, params=params, headers={"User-Agent": UA, **(headers or {})}, timeout=20)
        finally:
            _em_last[0] = time.time()


def _read_excel(content: bytes) -> Any:
    import pandas as pd

    try:
        frame = pd.read_excel(BytesIO(content), dtype=str)
    except ImportError as e:
        raise Unavailable("读取官网 Excel 文件需要 xlrd，在命令行运行 pip install xlrd") from e
    except Exception as e:  # noqa: BLE001  没发布/不是 Excel/缺 xlrd
        raise ConnectionError(f"官网没有返回可以解析的 Excel 文件（{type(e).__name__}）") from e
    frame.columns = [re.sub(r"\s+", "", str(c)) for c in frame.columns]
    return frame


def _num(value: Any) -> float | None:
    if value is None:
        return None
    text: str = str(value).replace(",", "").strip()
    if text in ("", "-", "--", "nan", "None", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- 纯解析（便于单测）

def parse_szse_month(payload: Any, year: int, month: int) -> list[date]:
    """深交所整月日历 JSON → 当月开市日。缺日、字段异常时报错（不能把没发布当作全月休市）"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        raise ConnectionError(f"深交所还没有发布 {year} 年 {month} 月的日历")
    seen: dict[date, bool] = {}
    for rec in data:
        flag: str = str(rec.get("jybz"))
        if flag not in ("0", "1") or not rec.get("jyrq"):
            raise ConnectionError("深交所日历字段格式变了")
        seen[_day(rec["jyrq"])] = flag == "1"
    last: int = calendar.monthrange(year, month)[1]
    if set(seen) != {date(year, month, d) for d in range(1, last + 1)}:
        raise ConnectionError(f"深交所 {year} 年 {month} 月的日历不完整")
    return sorted(d for d, is_open in seen.items() if is_open)


def parse_index_members(frame: Any, index: str, provider: str, code: str) -> pl.DataFrame:
    """中证 closeweight/cons 或国证 download-history 的 Excel（pandas 表）→ SCHEMAS["index_members"]。
    weight 是百分数（0.43 = 0.43%）；中证 cons 文件没有权重，weight 为空"""
    cols: set[str] = set(frame.columns)
    rows: list[dict] = []
    if provider == "csi":
        need = {"指数代码IndexCode", "成份券代码ConstituentCode", "成份券名称ConstituentName"}
        if not need <= cols:
            raise ConnectionError("中证指数文件的列变了：缺 " + "、".join(sorted(need - cols)))
        for rec in frame.to_dict("records"):
            if str(rec["指数代码IndexCode"]).zfill(6) != code:
                raise ConnectionError("中证指数官网返回了别的指数的文件")
            rows.append({"index": index, "code": str(rec["成份券代码ConstituentCode"]).zfill(6),
                         "name": rec["成份券名称ConstituentName"], "weight": _num(rec.get("权重(%)weight"))})
    else:
        need = {"样本代码", "样本简称"}
        if not need <= cols:
            raise ConnectionError("国证指数文件的列变了：缺 " + "、".join(sorted(need - cols)))
        for rec in frame.to_dict("records"):
            rows.append({"index": index, "code": str(rec["样本代码"]).zfill(6), "name": rec["样本简称"],
                         "weight": _num(rec.get("权重（%）"))})
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["index_members"])
    df = pl.DataFrame(rows, schema=SCHEMAS["index_members"])
    if df["code"].n_unique() != df.height:
        raise ConnectionError("指数成分文件里有重复的股票，不能当作完整名单")
    total: float | None = df["weight"].sum() if df["weight"].null_count() == 0 else None
    if total is not None and not 99 <= total <= 101:
        raise ConnectionError(f"指数权重合计 {total:.1f}% 不对，文件可能不完整")
    return df


def parse_index_valuation(frame: Any, index: str, code: str) -> pl.DataFrame:
    """中证 indicator 文件 → SCHEMAS["index_valuation"]（pe 总股本口径，pe_calc 计算用股本口径；股息率为百分数）"""
    mapping: dict[str, str] = {"市盈率1（总股本）P/E1": "pe", "市盈率2（计算用股本）P/E2": "pe_calc",
                               "股息率1（总股本）D/P1": "dy", "股息率2（计算用股本）D/P2": "dy_calc"}
    need: set[str] = {"日期Date", "指数代码IndexCode", *mapping}
    if not need <= set(frame.columns):
        raise ConnectionError("中证估值文件的列变了：缺 " + "、".join(sorted(need - set(frame.columns))))
    rows: list[dict] = []
    for rec in frame.to_dict("records"):
        if str(rec["指数代码IndexCode"]).zfill(6) != code:
            raise ConnectionError("中证指数官网返回了别的指数的估值文件")
        rows.append({"date": _day(rec["日期Date"]), "index": index, **{v: _num(rec[k]) for k, v in mapping.items()}})
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["index_valuation"])
    return pl.DataFrame(rows, schema=SCHEMAS["index_valuation"]).sort("date")


def parse_research(records: list[dict], code: str) -> pl.DataFrame:
    """东财研报 list 接口的原始记录 → SCHEMAS["research"]（按发布日倒序）"""
    rows: list[dict] = []
    for r in records or []:
        info: str = str(r.get("infoCode") or "")
        rows.append({
            "code": code, "publish_date": str(r.get("publishDate") or "")[:10] or None, "title": r.get("title"),
            "org": r.get("orgSName"), "rating": r.get("emRatingName") or r.get("sRatingName"),
            "eps_this": _num(r.get("predictThisYearEps")), "eps_next": _num(r.get("predictNextYearEps")),
            "eps_next2": _num(r.get("predictNextTwoYearEps")), "industry": r.get("indvInduName"),
            "pdf_url": EM_PDF.format(info_code=info) if info else None,
        })
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["research"])
    df = pl.DataFrame(rows).with_columns(pl.col("publish_date").str.to_date("%Y-%m-%d", strict=False))
    return df.select(list(SCHEMAS["research"])).sort("publish_date", descending=True, nulls_last=True)


def parse_hot_reason(payload: Any, day: date) -> pl.DataFrame:
    """同花顺强势股 JSON → SCHEMAS["hot_reason"]（pct/turn 为百分数，amount 元）"""
    if not isinstance(payload, dict):
        raise ConnectionError("同花顺强势股接口返回格式异常")
    if payload.get("errocode", 0) not in (0, "0"):
        raise ConnectionError(f"同花顺强势股接口报错：{payload.get('errormsg', '')}")
    rows: list[dict] = []
    for r in payload.get("data") or []:
        rows.append({"date": day, "code": str(r.get("code") or "").zfill(6), "name": r.get("name"),
                     "reason": r.get("reason"), "pct": _num(r.get("zhangfu")), "turn": _num(r.get("huanshou")),
                     "amount": _num(r.get("chengjiaoe")), "close": _num(r.get("close"))})
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["hot_reason"])
    return pl.DataFrame(rows, schema=SCHEMAS["hot_reason"])


def parse_institution_survey(records: list[dict]) -> pl.DataFrame:
    """东财 RPT_ORG_SURVEYNEW（一次调研一行）→ SCHEMAS["institution_survey"]"""
    rows: list[dict] = []
    for r in records or []:
        code: str = str(r.get("SECURITY_CODE") or "").zfill(6)
        if code == "000000":
            continue
        rows.append({"code": code, "name": r.get("SECURITY_NAME_ABBR"),
                     "notice_date": str(r.get("NOTICE_DATE") or "")[:10] or None,
                     "survey_date": str(r.get("RECEIVE_START_DATE") or "")[:10] or None,
                     "org_count": _num(r.get("SUM")), "survey_way": r.get("RECEIVE_WAY_EXPLAIN"),
                     "place": r.get("RECEIVE_PLACE")})
    if not rows:
        return pl.DataFrame(schema=SCHEMAS["institution_survey"])
    df = pl.DataFrame(rows)
    for col in ("notice_date", "survey_date"):
        df = df.with_columns(pl.col(col).str.to_date("%Y-%m-%d", strict=False))
    return df.select(list(SCHEMAS["institution_survey"])).unique(["code", "notice_date", "survey_date"], keep="first",
                                                                  maintain_order=True)


# ---------------------------------------------------------------- 数据源

class AStockDataProvider(DataProvider):
    name = "astock"
    label = "a-stock-data"
    description = ("按 a-stock-data 整理的官方/公开接口：深交所交易日历、中证/国证指数成分和权重、中证指数估值、"
                   "东财研报、同花顺强势股题材、东财机构调研")
    capabilities = ("trade_calendar", "index_members", "index_valuation", "research", "hot_reason",
                    "institution_survey")

    def fetch_trade_calendar(self) -> pl.DataFrame:
        """从 HISTORY_START 那年起逐月取深交所日历（每月一次请求，全部取完要一两分钟，所以排在最后兜底）。
        本月以后的月份还没发布就停。"""
        today: date = datetime.now(config.CHINA_TZ).date()
        start: date = _day(config.HISTORY_START)
        days: list[date] = []
        y, m = start.year, start.month
        while (y, m) <= (today.year, 12):
            try:
                payload = net.get_json(SZSE_CALENDAR, params={"month": f"{y}-{m}"},
                                       headers={"User-Agent": UA, "Referer": "https://www.szse.cn/"}, timeout=15)
                days += parse_szse_month(payload, y, m)
            except ConnectionError:
                if (y, m) <= (today.year, today.month):
                    raise
                break                                       # 以后的月份还没发布
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return pl.DataFrame({"date": days}, schema=SCHEMAS["trade_calendar"])

    def fetch_index_members(self, index: str) -> pl.DataFrame:
        provider, code = resolve_index(index)
        if provider == "csi":
            try:
                content: bytes = net.get(CSI_FILE.format(kind="closeweight", code=code), headers={"User-Agent": UA},
                                         timeout=30).content
            except ConnectionError:                        # 有的指数不公开权重文件，退回只有成分的文件
                content = net.get(CSI_FILE.format(kind="cons", code=code), headers={"User-Agent": UA}, timeout=30).content
        else:
            content = net.get(CNI_FILE, params={"indexcode": code}, headers={"User-Agent": UA}, timeout=30).content
        return parse_index_members(_read_excel(content), index, provider, code)

    def fetch_index_valuation(self, index: str = "hs300") -> pl.DataFrame:
        provider, code = resolve_index(index)
        if provider != "csi":
            raise Unavailable("指数估值只有中证指数官网公布（创业板指等国证指数没有）")
        content: bytes = net.get(CSI_FILE.format(kind="indicator", code=code), headers={"User-Agent": UA},
                                 timeout=30).content
        return parse_index_valuation(_read_excel(content), index, code)

    def fetch_research(self, code: str, max_pages: int = 3) -> pl.DataFrame:
        code = str(code).strip()[-6:].zfill(6)              # 东财研报接口只认纯 6 位代码
        records: list[dict] = []
        for page in range(1, max_pages + 1):
            payload = _em_get_json(EM_REPORT, params={
                "industryCode": "*", "pageSize": "100", "industry": "*", "rating": "*", "ratingChange": "*",
                "beginTime": "2000-01-01", "endTime": "2099-01-01", "pageNo": str(page), "fields": "", "qType": "0",
                "orgCode": "", "code": code, "rcode": "", "p": str(page), "pageNum": str(page),
                "pageNumber": str(page),
            }, headers={"Referer": "https://data.eastmoney.com/"})
            if not isinstance(payload, dict):
                raise ConnectionError("东方财富研报接口返回格式异常")
            rows: list[dict] = payload.get("data") or []
            records += rows
            if not rows or page >= int(payload.get("TotalPage") or 1):
                break
        return parse_research(records, code)

    def fetch_hot_reason(self, day: str | date | None = None) -> pl.DataFrame:
        d: date = _day(day)
        payload = net.get_json(THS_HOT.format(day=d.isoformat()), headers={"User-Agent": UA}, timeout=15)
        return parse_hot_reason(payload, d)

    def fetch_institution_survey(self, start: str | date, end: str | date, max_pages: int = 20) -> pl.DataFrame:
        s, e = _day(start), _day(end)
        if s > e:
            raise ValueError("开始日期不能晚于结束日期")
        records: list[dict] = []
        for page in range(1, max_pages + 1):
            payload = _em_get_json(EM_DATACENTER, params={
                "reportName": "RPT_ORG_SURVEYNEW", "columns": "ALL", "pageSize": "500", "pageNumber": str(page),
                "sortColumns": "NOTICE_DATE,SECURITY_CODE,RECEIVE_START_DATE", "sortTypes": "-1,1,-1",
                "filter": f'(IS_SOURCE="1")(NUMBERNEW="1")(NOTICE_DATE>=\'{s.isoformat()}\')(NOTICE_DATE<=\'{e.isoformat()}\')',
                "source": "WEB", "client": "WEB",
            })
            if not isinstance(payload, dict):
                raise ConnectionError("东方财富机构调研接口返回格式异常")
            if payload.get("code") == 9201:                 # 东财：查询结果为空
                break
            result = payload.get("result")
            if not result:
                break
            records += result.get("data") or []
            if page >= int(result.get("pages") or 1):
                break
        return parse_institution_survey(records)
