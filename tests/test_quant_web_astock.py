"""
quant_web.providers.astock_src（a-stock-data 数据源）离线测试：纯解析函数用按 a-stock-data 文档里的字段造的原始数据，
fetch 用替换掉的 net 请求，不联网。真实探测见文件末尾 @pytest.mark.network 的用例。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import polars as pl
import pytest

from quant_web import config
from quant_web.providers import astock_src as A
from quant_web.providers.base import CAPABILITIES, SCHEMAS, ProviderRegistry, Unavailable, conform, sample_kwargs
from quant_web.providers.builtin import register_all

NEW_CAPS: tuple[str, ...] = ("index_valuation", "research", "hot_reason", "institution_survey")


# ---------------------------------------------------------------- 注册

def test_new_capabilities_declared() -> None:
    for cap in NEW_CAPS:
        assert cap in CAPABILITIES and cap in SCHEMAS
        assert sample_kwargs(cap)


def test_registered_and_chains() -> None:
    reg = ProviderRegistry()
    reg.set_chains_source(lambda: {})
    register_all(reg)
    p = reg.get("astock")
    assert p is not None and p.available() == (True, "")
    for cap in A.AStockDataProvider.capabilities:
        assert p.supports(cap), cap
    assert reg.chain("index_members")[0] == "astock"          # 带权重，排第一
    cal = reg.chain("trade_calendar")                         # 逐月请求较慢，排在内置免费源后面兜底
    assert cal.index("astock") > cal.index("baostock")
    for cap in NEW_CAPS:
        assert reg.chain(cap) == ["astock"]


def test_resolve_index() -> None:
    assert A.resolve_index("hs300") == ("csi", "000300")
    assert A.resolve_index("cyb") == ("cni", "399006")
    assert A.resolve_index("000905") == ("csi", "000905")
    assert A.resolve_index("399673") == ("cni", "399673")
    with pytest.raises(Unavailable):
        A.resolve_index("nasdaq")


# ---------------------------------------------------------------- 交易日历

def _szse_month(year: int, month: int, closed: set[int] = frozenset()) -> dict:
    import calendar as cal
    last = cal.monthrange(year, month)[1]
    data = []
    for d in range(1, last + 1):
        day = date(year, month, d)
        is_open = day.weekday() < 5 and d not in closed
        data.append({"jyrq": day.isoformat(), "jybz": "1" if is_open else "0"})
    return {"data": data}


def test_parse_szse_month() -> None:
    days = A.parse_szse_month(_szse_month(2026, 10, closed={1, 2, 5, 6, 7}), 2026, 10)
    assert date(2026, 10, 1) not in days and date(2026, 10, 8) in days
    assert all(d.weekday() < 5 for d in days)


def test_parse_szse_month_rejects_incomplete() -> None:
    payload = _szse_month(2026, 9)
    payload["data"] = payload["data"][:-1]
    with pytest.raises(ConnectionError):
        A.parse_szse_month(payload, 2026, 9)
    with pytest.raises(ConnectionError):
        A.parse_szse_month({"data": []}, 2026, 9)


def test_fetch_trade_calendar_stops_at_unpublished(monkeypatch: pytest.MonkeyPatch) -> None:
    today = date.today()
    monkeypatch.setattr(config, "HISTORY_START", f"{today.year}-01-01")
    calls: list[str] = []

    def fake_get_json(url: str, params: dict | None = None, **_: Any) -> dict:
        y, m = (int(x) for x in params["month"].split("-"))
        calls.append(params["month"])
        if (y, m) > (today.year, today.month):
            return {"data": []}                             # 以后的月份还没发布
        return _szse_month(y, m)

    monkeypatch.setattr(A.net, "get_json", fake_get_json)
    df = conform(A.AStockDataProvider().fetch_trade_calendar(), "trade_calendar")
    assert df.height > 0 and df["date"].max() <= date(today.year, today.month, 28) + timedelta(days=3)
    assert len(calls) == today.month + (1 if today.month < 12 else 0)


# ---------------------------------------------------------------- 指数

def _csi_weights(code: str = "000300", weights: tuple[float, ...] = (60.0, 40.0)) -> pd.DataFrame:
    return pd.DataFrame({
        "日期Date": ["20260925"] * len(weights), "指数代码IndexCode": [code] * len(weights),
        "成份券代码ConstituentCode": [f"{600000 + i}" for i in range(len(weights))],
        "成份券名称ConstituentName": [f"股票{i}" for i in range(len(weights))],
        "交易所Exchange": ["上海证券交易所"] * len(weights), "权重(%)weight": [str(w) for w in weights],
    })


def test_parse_index_members_csi_with_weights() -> None:
    df = A.parse_index_members(_csi_weights(), "hs300", "csi", "000300")
    assert df.columns == list(SCHEMAS["index_members"])
    assert df["index"].to_list() == ["hs300", "hs300"] and df["weight"].to_list() == [60.0, 40.0]


def test_parse_index_members_checks() -> None:
    with pytest.raises(ConnectionError):                    # 权重合计不对
        A.parse_index_members(_csi_weights(weights=(30.0, 20.0)), "hs300", "csi", "000300")
    with pytest.raises(ConnectionError):                    # 返回了别的指数
        A.parse_index_members(_csi_weights(code="000905"), "hs300", "csi", "000300")
    cons = _csi_weights().drop(columns=["权重(%)weight"])  # cons 文件没有权重：允许
    assert A.parse_index_members(cons, "hs300", "csi", "000300")["weight"].null_count() == 2


def test_parse_index_members_cni() -> None:
    raw = pd.DataFrame({"日期": ["2026-08-31"] * 2, "样本代码": ["300750", "300059"], "样本简称": ["宁德时代", "东方财富"],
                        "权重（%）": ["55.5", "44.5"]})
    df = A.parse_index_members(raw, "cyb", "cni", "399006")
    assert df["code"].to_list() == ["300750", "300059"] and df["weight"].sum() == pytest.approx(100)


def test_fetch_index_members_falls_back_to_cons(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    class Resp:
        content = b"xls"

    def fake_get(url: str, **_: Any) -> Resp:
        urls.append(url)
        if "closeweight" in url:
            raise ConnectionError("404")
        return Resp()

    monkeypatch.setattr(A.net, "get", fake_get)
    monkeypatch.setattr(A, "_read_excel", lambda content: _csi_weights("000688").drop(columns=["权重(%)weight"]))
    df = A.AStockDataProvider().fetch_index_members("kc50")
    assert df.height == 2 and "closeweight" in urls[0] and "cons" in urls[1]


def test_parse_index_valuation() -> None:
    raw = pd.DataFrame({"日期Date": ["20260925", "20260924"], "指数代码IndexCode": ["000300", "000300"],
                        "市盈率1（总股本）P/E1": ["13.2", "13.1"], "市盈率2（计算用股本）P/E2": ["12.9", "12.8"],
                        "股息率1（总股本）D/P1": ["2.8", "2.9"], "股息率2（计算用股本）D/P2": ["2.7", "--"]})
    df = A.parse_index_valuation(raw, "hs300", "000300")
    assert df.columns == list(SCHEMAS["index_valuation"])
    assert df["date"].to_list() == [date(2026, 9, 24), date(2026, 9, 25)]
    assert df["pe"].to_list() == [13.1, 13.2] and df["dy_calc"].to_list() == [None, 2.7]


def test_index_valuation_only_csi() -> None:
    with pytest.raises(Unavailable):
        A.AStockDataProvider().fetch_index_valuation("cyb")


# ---------------------------------------------------------------- 研报 / 题材 / 调研

def test_parse_research() -> None:
    raw = [
        {"title": "旧报告", "publishDate": "2026-01-02 00:00:00.000", "orgSName": "甲证券", "infoCode": "AP1",
         "emRatingName": "增持", "predictThisYearEps": "60.1", "predictNextYearEps": 66.2, "predictNextTwoYearEps": None,
         "indvInduName": "白酒Ⅱ"},
        {"title": "新报告", "publishDate": "2026-09-20 00:00:00.000", "orgSName": "乙证券", "infoCode": "",
         "emRatingName": "买入", "predictThisYearEps": "", "indvInduName": "白酒Ⅱ"},
    ]
    df = A.parse_research(raw, "600519")
    assert df.columns == list(SCHEMAS["research"])
    assert df["title"].to_list() == ["新报告", "旧报告"]                      # 新的在前
    assert df["eps_this"].to_list() == [None, 60.1] and df["eps_next"][1] == 66.2
    assert df["pdf_url"].to_list() == [None, "https://pdf.dfcfw.com/pdf/H3_AP1_1.pdf"]
    assert A.parse_research([], "600519").height == 0


def test_fetch_research_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[str] = []

    def fake(url: str, params: dict, headers: dict | None = None) -> dict:
        pages.append(params["pageNo"])
        rows = [{"title": f"p{params['pageNo']}", "publishDate": "2026-09-01", "infoCode": "X"}]
        return {"data": rows, "TotalPage": 2}

    monkeypatch.setattr(A, "_em_get_json", fake)
    df = A.AStockDataProvider().fetch_research("SH600519")
    assert pages == ["1", "2"] and df.height == 2 and set(df["code"]) == {"600519"}


def test_parse_hot_reason() -> None:
    payload = {"errocode": 0, "data": [
        {"code": "2", "name": "万科A", "reason": "地产+国企改革", "zhangfu": "10.01", "huanshou": "3.2",
         "chengjiaoe": "123456789", "close": "8.8"}]}
    df = A.parse_hot_reason(payload, date(2026, 9, 25))
    assert df.columns == list(SCHEMAS["hot_reason"])
    assert df.row(0) == (date(2026, 9, 25), "000002", "万科A", "地产+国企改革", 10.01, 3.2, 123456789.0, 8.8)
    with pytest.raises(ConnectionError):
        A.parse_hot_reason({"errocode": 1, "errormsg": "bad"}, date(2026, 9, 25))


def test_parse_institution_survey() -> None:
    rows = [
        {"SECURITY_CODE": "688062", "SECURITY_NAME_ABBR": "迈威生物", "NOTICE_DATE": "2026-09-22 00:00:00",
         "RECEIVE_START_DATE": "2026-09-18 00:00:00", "SUM": "35", "RECEIVE_WAY_EXPLAIN": "电话会议",
         "RECEIVE_PLACE": "线上"},
        {"SECURITY_CODE": "688062", "SECURITY_NAME_ABBR": "迈威生物", "NOTICE_DATE": "2026-09-22 00:00:00",
         "RECEIVE_START_DATE": "2026-09-18 00:00:00", "SUM": "35"},          # 重复一行：去重
        {"SECURITY_CODE": None},
    ]
    df = A.parse_institution_survey(rows)
    assert df.columns == list(SCHEMAS["institution_survey"])
    assert df.height == 1 and df["org_count"][0] == 35.0 and df["survey_date"][0] == date(2026, 9, 18)


def test_fetch_institution_survey(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def fake(url: str, params: dict, headers: dict | None = None) -> dict:
        seen.append(params)
        if params["pageNumber"] == "1":
            return {"code": 0, "result": {"pages": 2, "data": [{"SECURITY_CODE": "000001", "NOTICE_DATE": "2026-09-20",
                                                                  "RECEIVE_START_DATE": "2026-09-18", "SUM": 3}]}}
        return {"code": 0, "result": {"pages": 2, "data": [{"SECURITY_CODE": "000002", "NOTICE_DATE": "2026-09-19",
                                                              "RECEIVE_START_DATE": "2026-09-17", "SUM": 5}]}}

    monkeypatch.setattr(A, "_em_get_json", fake)
    df = A.AStockDataProvider().fetch_institution_survey("2026-09-15", "2026-09-22")
    assert df["code"].to_list() == ["000001", "000002"]
    assert "NOTICE_DATE>='2026-09-15'" in seen[0]["filter"] and "NOTICE_DATE<='2026-09-22'" in seen[0]["filter"]
    monkeypatch.setattr(A, "_em_get_json", lambda *a, **k: {"code": 9201, "result": None})
    assert A.AStockDataProvider().fetch_institution_survey("2026-09-15", "2026-09-22").height == 0
    with pytest.raises(ValueError):
        A.AStockDataProvider().fetch_institution_survey("2026-09-22", "2026-09-15")


def test_em_requests_are_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    stamps: list[float] = []
    monkeypatch.setattr(A, "EM_MIN_INTERVAL", 0.2)
    monkeypatch.setattr(A.net, "get_json", lambda *a, **k: stamps.append(__import__("time").time()) or {})
    for _ in range(3):
        A._em_get_json(A.EM_REPORT, params={})
    assert all(b - a >= 0.19 for a, b in zip(stamps, stamps[1:], strict=False))


def test_probe_uses_sample_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = ProviderRegistry()
    reg.set_chains_source(lambda: {})
    reg.register(A.AStockDataProvider())
    monkeypatch.setattr(A.net, "get_json", lambda *a, **k: {"errocode": 0, "data": [{"code": "600519", "name": "贵州茅台"}]})
    res = reg.probe("hot_reason", "astock")
    assert res["ok"] and res["rows"] == 1 and isinstance(res["sample"][0]["date"], date)
    assert isinstance(pl.DataFrame(res["sample"]), pl.DataFrame)


# ---------------------------------------------------------------- 联网冒烟（断网/被墙时跳过）

@pytest.mark.network
@pytest.mark.parametrize("cap", ["index_members", "index_valuation", "research", "hot_reason", "institution_survey"])
def test_live_probe(cap: str) -> None:
    reg = ProviderRegistry()
    reg.set_chains_source(lambda: {})
    reg.register(A.AStockDataProvider())
    res = reg.probe(cap, "astock")
    if not res["ok"] and res["error"] and ("无法访问" in res["error"] or "连接" in res["error"]):
        pytest.skip(f"网络不通：{res['error'][:80]}")
    assert res["ok"] or cap == "hot_reason", res       # 强势股接口休市日可能为空
