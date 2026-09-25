"""
quant_web.providers：注册表机制（顺序/失败切换/空数据/用户顺序/conform/status/probe）、本地存档 store，
各适配器的列名映射与单位换算纯函数（用造好的原始表测，不联网），updates 的更新汇总。

默认离线（不带 network 标记的用例）。真实探测见文件末尾 @pytest.mark.network 的用例：
    .venv\\Scripts\\python -m pytest tests/test_quant_web_providers.py -q -m "not network"
    .venv\\Scripts\\python -m pytest tests/test_quant_web_providers.py -q -m network
"""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta

import pandas as pd
import polars as pl
import pytest

from quant_web import config
from quant_web.providers import (
    akshare_src,
    baostock_src,
    em_datacenter_src,
    exchange_src,
    news_src,
    qmt_src,
    sina_src,
    store,
    tencent_src,
    tushare_src,
    updates,
)
from quant_web.providers.base import (
    CAPABILITIES,
    SCHEMAS,
    DataProvider,
    ProviderError,
    ProviderRegistry,
    Unavailable,
    conform,
    sample_kwargs,
)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """把 config 的路径指向临时目录，绝不碰真实 workspace（同 test_quant_web_history.py）"""
    stock_lab = tmp_path / "stock_lab"
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "STOCK_LAB", stock_lab)
    monkeypatch.setattr(config, "PANEL_DIR", stock_lab / "daily")
    monkeypatch.setattr(config, "UNIVERSE_FILE", stock_lab / "universe.parquet")
    monkeypatch.setattr(config, "POOLS_DIR", stock_lab / "pools")
    monkeypatch.setattr(config, "MODEL_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    config.ensure_dirs()
    return tmp_path


# =================================================================================================
# 一、注册表机制（用假数据源，不涉及真实适配器）
# =================================================================================================

class _Ok(DataProvider):
    name = "ok"
    label = "OK源"
    capabilities = ("daily_bars",)

    def fetch_daily_bars(self, **kwargs) -> pl.DataFrame:
        return pl.DataFrame({"date": [date(2026, 1, 2)], "code": ["600000"], "close": [10.0]})


class _Fail(DataProvider):
    name = "fail"
    label = "坏源"
    capabilities = ("daily_bars",)

    def fetch_daily_bars(self, **kwargs) -> pl.DataFrame:
        raise RuntimeError("网络错误")


class _EmptySource(DataProvider):
    name = "empty"
    label = "空源"
    capabilities = ("daily_bars",)

    def fetch_daily_bars(self, **kwargs) -> pl.DataFrame:
        return pl.DataFrame(schema=SCHEMAS["daily_bars"])


class _NotAvail(DataProvider):
    name = "notavail"
    label = "不可用源"
    capabilities = ("daily_bars",)

    def available(self) -> tuple[bool, str]:
        return False, "没装依赖"

    def fetch_daily_bars(self, **kwargs) -> pl.DataFrame:
        raise AssertionError("不该被调用：available()=False 就该跳过")


class _Unav(DataProvider):
    name = "unav"
    label = "参数不支持源"
    capabilities = ("daily_bars",)

    def fetch_daily_bars(self, **kwargs) -> pl.DataFrame:
        raise Unavailable("不支持这个参数")


def _reg(*providers: DataProvider) -> ProviderRegistry:
    r = ProviderRegistry()
    for p in providers:
        r.register(p)
    return r


def test_fallback_order_first_success_wins():
    r = _reg(_Fail(), _Ok())
    res = r.fetch("daily_bars", chain=["fail", "ok"], code="600000")
    assert res.source == "ok"
    assert res.tried == [("坏源", "网络错误")]
    assert res.data.height == 1


def test_unavailable_provider_skipped_with_reason():
    r = _reg(_NotAvail(), _Ok())
    res = r.fetch("daily_bars", chain=["notavail", "ok"], code="600000")
    assert res.source == "ok"
    assert res.tried == [("不可用源", "没装依赖")]


def test_unavailable_exception_treated_like_skip():
    r = _reg(_Unav(), _Ok())
    res = r.fetch("daily_bars", chain=["unav", "ok"], code="600000")
    assert res.source == "ok"
    assert res.tried[0] == ("参数不支持源", "不支持这个参数")


def test_all_empty_returns_empty_without_raising():
    r = _reg(_EmptySource())
    res = r.fetch("daily_bars", chain=["empty"], code="600000")
    assert res.data.is_empty()
    assert res.source == "empty"


def test_empty_falls_through_to_next_source():
    r = _reg(_EmptySource(), _Ok())
    res = r.fetch("daily_bars", chain=["empty", "ok"], code="600000")
    assert res.source == "ok"
    assert res.data.height == 1


def test_all_fail_raises_providererror_with_reasons():
    r = _reg(_Fail(), _Unav())
    with pytest.raises(ProviderError) as exc:
        r.fetch("daily_bars", chain=["fail", "unav"], code="600000")
    assert "网络错误" in str(exc.value)
    assert "不支持这个参数" in str(exc.value)


def test_unknown_capability_raises_valueerror():
    r = _reg(_Ok())
    with pytest.raises(ValueError):
        r.chain("not_a_capability")


def test_user_chain_overrides_default():
    r = _reg(_Ok(), _Fail())
    r.set_chains_source(lambda: {"daily_bars": ["fail", "ok"]})
    assert r.chain("daily_bars") == ["fail", "ok"]


def test_user_chain_ignores_unknown_or_unsupported_names():
    r = _reg(_Ok())
    r.set_chains_source(lambda: {"daily_bars": ["ghost", "ok", "ok"]})
    assert r.chain("daily_bars") == ["ok"]      # 去重、丢掉不认识的源


def test_default_chain_used_when_no_user_chain():
    r = _reg(_Ok(), _Fail())
    r.set_chains_source(lambda: {})
    assert set(r.chain("daily_bars")) == {"ok", "fail"}


def test_status_reports_all_capabilities_and_sources():
    r = _reg(_Ok())
    statuses = r.status()
    assert {s["capability"] for s in statuses} == set(CAPABILITIES)
    daily = next(s for s in statuses if s["capability"] == "daily_bars")
    assert any(src["name"] == "ok" for src in daily["sources"])
    assert daily["chain"] == ["ok"]


def test_probe_uses_sample_kwargs_and_reports_rows():
    r = _reg(_Ok())
    result = r.probe("daily_bars", "ok")
    assert result["ok"] is True
    assert result["rows"] == 1
    assert result["params"] == sample_kwargs("daily_bars")
    assert result["sample"]


def test_probe_reports_error_when_all_fail():
    r = _reg(_Fail())
    result = r.probe("daily_bars", "fail")
    assert result["ok"] is False
    assert result["rows"] == 0
    assert result["error"]


def test_probe_unknown_provider_raises():
    r = _reg(_Ok())
    with pytest.raises(ValueError):
        r.probe("daily_bars", "ghost")


def test_probe_unsupported_capability_raises():
    r = _reg(_Ok())
    with pytest.raises(ValueError):
        r.probe("quotes", "ok")


# =================================================================================================
# 二、conform()
# =================================================================================================

def test_conform_fills_missing_columns_and_casts_types():
    df = pl.DataFrame({"date": ["2026-01-02"], "code": ["600000"], "close": ["10.5"]})
    out = conform(df, "daily_bars")
    assert list(out.columns) == list(SCHEMAS["daily_bars"])
    assert out["date"][0] == date(2026, 1, 2)
    assert out["close"][0] == 10.5
    assert out["open"][0] is None


def test_conform_drops_extra_columns():
    df = pl.DataFrame({"date": ["2026-01-02"], "code": ["600000"], "junk": [1]})
    out = conform(df, "daily_bars")
    assert "junk" not in out.columns


def test_conform_unknown_capability_passthrough():
    df = pl.DataFrame({"a": [1]})
    out = conform(df, "lhb")     # lhb 没有登记 SCHEMAS，原样返回
    assert out.equals(df)


def test_conform_none_becomes_empty_dataframe():
    out = conform(None, "daily_bars")
    assert out.is_empty()
    assert list(out.columns) == list(SCHEMAS["daily_bars"])


# =================================================================================================
# 三、本地存档 store.py
# =================================================================================================

def test_store_save_and_load_merge_dedupe(workspace):
    df1 = pl.DataFrame({"code": ["600000"], "date": [date(2026, 1, 2)], "rzye": [100.0]})
    store.save("margin", df1)
    df2 = pl.DataFrame({"code": ["600000"], "date": [date(2026, 1, 2)], "rzye": [200.0]})   # 同一天覆盖
    n = store.save("margin", df2)
    loaded = store.load("margin")
    assert n == 1
    assert loaded.height == 1
    assert loaded["rzye"][0] == 200.0


def test_store_save_appends_new_dates(workspace):
    store.save("margin", pl.DataFrame({"code": ["600000"], "date": [date(2026, 1, 2)], "rzye": [100.0]}))
    store.save("margin", pl.DataFrame({"code": ["600000"], "date": [date(2026, 1, 3)], "rzye": [110.0]}))
    loaded = store.load("margin").sort("date")
    assert loaded.height == 2
    assert loaded["date"].to_list() == [date(2026, 1, 2), date(2026, 1, 3)]


def test_store_save_replace_where(workspace):
    store.save("index_members", pl.DataFrame({
        "index": ["hs300", "hs300"], "code": ["600000", "600001"], "name": ["a", "b"], "weight": [None, None],
    }))
    store.save("index_members", pl.DataFrame({"index": ["hs300"], "code": ["600002"], "name": ["c"], "weight": [None]}),
              replace_where=pl.col("index") == "hs300")
    loaded = store.load("index_members")
    assert loaded["code"].to_list() == ["600002"]


def test_store_save_replace_where_keeps_other_groups(workspace):
    store.save("index_members", pl.DataFrame({"index": ["hs300"], "code": ["600000"], "name": ["a"], "weight": [None]}))
    store.save("index_members", pl.DataFrame({"index": ["zz500"], "code": ["600001"], "name": ["b"], "weight": [None]}),
              replace_where=pl.col("index") == "zz500")
    loaded = store.load("index_members").sort("index")
    assert loaded["index"].to_list() == ["hs300", "zz500"]


def test_store_info_reports_rows_codes_and_date_range(workspace):
    store.save("margin", pl.DataFrame({
        "code": ["600000", "600000"], "date": [date(2026, 1, 2), date(2026, 1, 5)], "rzye": [1.0, 2.0],
    }))
    info = {i["name"]: i for i in store.info()}
    assert info["margin"]["exists"] is True
    assert info["margin"]["rows"] == 2
    assert info["margin"]["codes"] == 1
    assert info["margin"]["start"] == date(2026, 1, 2)
    assert info["margin"]["end"] == date(2026, 1, 5)
    assert info["pledge"]["exists"] is False
    assert info["pledge"]["rows"] == 0


def test_store_load_missing_archive_returns_empty(workspace):
    assert store.load("pledge").is_empty()


def test_store_save_empty_df_is_noop(workspace):
    store.save("margin", pl.DataFrame({"code": ["600000"], "date": [date(2026, 1, 2)], "rzye": [1.0]}))
    n = store.save("margin", pl.DataFrame(schema={"code": pl.Utf8, "date": pl.Date, "rzye": pl.Float64}))
    assert n == store.load("margin").height == 1


def test_store_path_of_unknown_name_raises():
    with pytest.raises(ValueError):
        store.path_of("not_an_archive")


# =================================================================================================
# 四、适配器：exchange_src（沪深交易所融资融券）
# =================================================================================================

def test_parse_sse_margin_maps_columns_no_rzrqye():
    pdf = pd.DataFrame({
        "信用交易日期": ["20260922"], "标的证券代码": ["600000"], "标的证券简称": ["浦发银行"],
        "融资余额": [100.0], "融资买入额": [10.0], "融资偿还额": [5.0], "融券余量": [1000.0], "融券卖出量": [200.0],
    })
    out = exchange_src.parse_sse_margin(pdf, date(2026, 9, 22))
    assert list(out.columns) == list(SCHEMAS["margin"])
    row = out.row(0, named=True)
    assert row["code"] == "600000" and row["date"] == date(2026, 9, 22)
    assert row["rzye"] == 100.0 and row["rzche"] == 5.0
    assert row["rzrqye"] is None      # 上交所明细没有两融余额


def test_parse_szse_margin_maps_columns_no_rzche():
    pdf = pd.DataFrame({
        "证券代码": ["1"], "证券简称": ["平安银行"], "融资买入额": [10.0], "融资余额": [100.0],
        "融券卖出量": [200.0], "融券余量": [1000.0], "融券余额": [500.0], "融资融券余额": [600.0],
    })
    out = exchange_src.parse_szse_margin(pdf, date(2026, 9, 22))
    row = out.row(0, named=True)
    assert row["code"] == "000001"    # zfill(6)
    assert row["rzrqye"] == 600.0
    assert row["rzche"] is None       # 深交所明细没有融资偿还额


def test_parse_margin_empty_input():
    assert exchange_src.parse_sse_margin(pd.DataFrame(), date(2026, 9, 22)).is_empty()
    assert exchange_src.parse_szse_margin(None, date(2026, 9, 22)).is_empty()


def test_exchange_fetch_margin_one_side_fails_keeps_other(monkeypatch):
    import akshare as ak

    def bad(date):
        raise ConnectionError("boom")

    def good(date):
        return pd.DataFrame({
            "证券代码": ["000001"], "证券简称": ["平安银行"], "融资买入额": [1.0], "融资余额": [2.0],
            "融券卖出量": [3.0], "融券余量": [4.0], "融券余额": [5.0], "融资融券余额": [6.0],
        })

    monkeypatch.setattr(ak, "stock_margin_detail_sse", bad)
    monkeypatch.setattr(ak, "stock_margin_detail_szse", good)
    provider = exchange_src.ExchangeProvider()
    out = provider.fetch_margin(date(2026, 9, 22))
    assert out.height == 1
    assert out["code"][0] == "000001"


# =================================================================================================
# 五、适配器：em_datacenter_src
# =================================================================================================

def test_parse_holder_count():
    pdf = pd.DataFrame({
        "代码": ["688759"], "名称": ["必贝特"], "最新价": [10.0], "涨跌幅": [1.0],
        "股东户数-本次": [11180.0], "股东户数-上次": [11113.0], "股东户数-增减": [67.0],
        "股东户数-增减比例": [0.6029], "区间涨跌幅": [6.2],
        "股东户数统计截止日-本次": [date(2026, 9, 18)], "股东户数统计截止日-上次": [date(2026, 9, 10)],
        "户均持股市值": [900000.0], "户均持股数量": [40253.7], "总市值": [1e10], "总股本": [4.5e8],
        "公告日期": [date(2026, 9, 24)],
    })
    out = em_datacenter_src.parse_holder_count(pdf)
    assert list(out.columns) == list(SCHEMAS["holder_count"])
    row = out.row(0, named=True)
    assert row["code"] == "688759"
    assert row["end_date"] == date(2026, 9, 18)
    assert row["notice_date"] == date(2026, 9, 24)
    assert row["holders"] == 11180.0
    assert row["avg_shares"] == pytest.approx(40253.7)


def test_parse_unlock_scales_float_ratio_to_percent():
    pdf = pd.DataFrame({
        "股票代码": ["300124"], "股票简称": ["汇川技术"], "解禁时间": [date(2026, 9, 21)],
        "限售股类型": ["股权激励限售股份"], "解禁数量": [305000.0], "实际解禁数量": [0.0],
        "实际解禁市值": [0.0], "占解禁前流通市值比例": [0.0036408], "解禁前一交易日收盘价": [52.84],
        "解禁前20日涨跌幅": [-11.1], "解禁后20日涨跌幅": [-1.09],
    })
    out = em_datacenter_src.parse_unlock(pdf)
    row = out.row(0, named=True)
    assert row["code"] == "300124"
    assert row["shares"] == 305000.0
    assert row["float_ratio"] == pytest.approx(0.36408)
    assert row["kind"] == "股权激励限售股份"


def test_parse_pledge_scales_shares_by_10000():
    pdf = pd.DataFrame({
        "股票代码": ["600370"], "股票简称": ["*ST三房"], "交易日期": [date(2026, 9, 24)],
        "所属行业": ["化学纤维"], "质押比例": [78.74], "质押股数": [316745.56], "质押市值": [465615.9732],
        "质押笔数": [34.0], "无限售股质押数": [316745.56], "限售股质押数": [0.0], "近一年涨跌幅": [-27.94],
        "所属行业代码": ["471"],
    })
    out = em_datacenter_src.parse_pledge(pdf)
    row = out.row(0, named=True)
    assert row["code"] == "600370"
    assert row["pledge_ratio"] == 78.74
    assert row["pledge_shares"] == pytest.approx(3167455600.0)


def test_parse_forecast_uses_single_value_for_both_bounds():
    pdf = pd.DataFrame({
        "股票代码": ["600187"], "股票简称": ["*ST国中"], "预测指标": ["归属于上市公司股东的净利润"],
        "业绩变动": ["预计2026年1-6月净利润盈利:265万元至315万元"], "预测数值": [None],
        "业绩变动幅度": [115.83], "业绩变动原因": ["投资收益影响"], "预告类型": ["扭亏"],
        "上年同期值": [-18320000.0], "公告日期": [date(2026, 8, 19)],
    })
    out = em_datacenter_src.parse_forecast(pdf, date(2026, 6, 30))
    row = out.row(0, named=True)
    assert row["code"] == "600187"
    assert row["period"] == date(2026, 6, 30)
    assert row["kind"] == "扭亏"
    assert row["change_low"] == row["change_high"] == 115.83
    assert "净利润" in row["summary"]


def test_parse_holder_trades_scales_shares_by_10000():
    rows = [{
        "SECURITY_CODE": "688603", "SECURITY_NAME_ABBR": "N公司", "HOLDER_NAME": "某某资管计划",
        "DIRECTION": "减持", "CHANGE_NUM": 31.179, "CHANGE_FREE_RATIO": 0.25,
        "NOTICE_DATE": "2026-09-25 00:00:00", "START_DATE": "2026-09-22 00:00:00", "END_DATE": "2026-09-24 00:00:00",
    }]
    out = em_datacenter_src.parse_holder_trades(rows)
    row = out.row(0, named=True)
    assert row["code"] == "688603"
    assert row["direction"] == "减持"
    assert row["shares"] == pytest.approx(311790.0)
    assert row["ratio_float"] == 0.25
    assert row["notice_date"] == date(2026, 9, 25)
    assert row["start"] == date(2026, 9, 22) and row["end"] == date(2026, 9, 24)


def test_parse_holder_trades_skips_rows_without_code():
    assert em_datacenter_src.parse_holder_trades([{"SECURITY_CODE": ""}]).is_empty()
    assert em_datacenter_src.parse_holder_trades([]).is_empty()


def test_em_fetch_pledge_explicit_day_propagates_real_error(monkeypatch):
    import akshare as ak

    def boom(date):
        raise ConnectionError("网络不通")

    monkeypatch.setattr(ak, "stock_gpzy_pledge_ratio_em", boom)
    provider = em_datacenter_src.EmDatacenterProvider()
    with pytest.raises(ConnectionError):
        provider.fetch_pledge(date(2026, 9, 22))


def test_em_fetch_pledge_auto_search_tries_recent_days_then_raises(monkeypatch):
    import akshare as ak

    from quant_web.market import realtime

    calls: list[str] = []

    def always_empty(date):
        calls.append(date)
        return pd.DataFrame()

    monkeypatch.setattr(ak, "stock_gpzy_pledge_ratio_em", always_empty)
    monkeypatch.setattr(realtime, "recent_trading_days", lambda n, until=None: [date(2026, 9, 20 + i) for i in range(n)])
    provider = em_datacenter_src.EmDatacenterProvider()
    with pytest.raises(Unavailable):
        provider.fetch_pledge(None)
    assert len(calls) == 8      # 试了 8 个候选交易日


# =================================================================================================
# 六、适配器：baostock_src
# =================================================================================================

def test_parse_index_members_strips_prefix():
    rows = [["2026-09-21", "sh.600000", "浦发银行"], ["2026-09-21", "sz.000001", "平安银行"]]
    out = baostock_src.parse_index_members(rows, "hs300")
    assert out["code"].to_list() == ["600000", "000001"]
    assert out["index"].to_list() == ["hs300", "hs300"]
    assert out["weight"].null_count() == 2


def test_parse_industry_strips_leading_code_and_drops_null():
    rows = [
        ["2026-09-21", "sh.600000", "浦发银行", "C39计算机、通信和其他电子设备制造业", "1"],
        ["2026-09-21", "sh.600001", "无行业", "", "1"],
    ]
    out = baostock_src.parse_industry(rows)
    assert out.height == 1
    assert out.row(0, named=True) == {"code": "600000", "industry": "计算机、通信和其他电子设备制造业"}


def test_parse_trade_calendar_keeps_only_trading_days():
    rows = [["2026-09-19", "1"], ["2026-09-20", "0"], ["2026-09-21", "1"]]
    out = baostock_src.parse_trade_calendar(rows)
    assert out["date"].to_list() == [date(2026, 9, 19), date(2026, 9, 21)]


def test_parse_daily_bars_skips_suspended_rows():
    rows = [
        ["2026-09-22", "sh.600519", "1252.15", "1265.88", "1248.1", "1253.8", "1251.24", "2457300", "3088526100",
         "3", "0.11", "1", "0.2", "0"],
        ["2026-09-23", "sh.600519", "0", "0", "0", "0", "0", "0", "0", "3", "0", "0", "0", "0"],   # 停牌
    ]
    out = baostock_src.parse_daily_bars(rows, "600519")
    assert out.height == 1
    assert out["date"][0] == date(2026, 9, 22)
    assert out["close"][0] == 1253.8


def test_baostock_index_members_unknown_index_raises_unavailable():
    provider = baostock_src.BaostockProvider()
    with pytest.raises(Unavailable):
        provider.fetch_index_members("nasdaq100")


# =================================================================================================
# 七、适配器：akshare_src
# =================================================================================================

def test_guarded_blocks_push2_function():
    def fake_push2():
        return "https://push2.eastmoney.com/api"   # 源码里带这个域名

    with pytest.raises(Unavailable):
        akshare_src._guarded(fake_push2)()


def test_guarded_allows_normal_function():
    def fine():
        return 1

    assert akshare_src._guarded(fine)() == 1


def test_parse_stock_list_zfills_code():
    pdf = pd.DataFrame({"code": ["1", "600000"], "name": ["平安银行", "浦发银行"]})
    out = akshare_src.parse_stock_list(pdf)
    assert out["code"].to_list() == ["000001", "600000"]


def test_parse_trade_calendar():
    pdf = pd.DataFrame({"trade_date": [date(2026, 9, 21), date(2026, 9, 22)]})
    out = akshare_src.parse_trade_calendar(pdf)
    assert out["date"].to_list() == [date(2026, 9, 21), date(2026, 9, 22)]


def test_parse_index_members_csindex():
    pdf = pd.DataFrame({"成分券代码": ["000001"], "成分券名称": ["平安银行"], "日期": [date(2026, 9, 24)]})
    out = akshare_src.parse_index_members_csindex(pdf, "hs300")
    assert out.row(0, named=True) == {"index": "hs300", "code": "000001", "name": "平安银行", "weight": None}


def test_parse_daily_bars_tx_fixes_turn_and_preclose():
    pdf = pd.DataFrame({
        "date": [date(2026, 9, 22), date(2026, 9, 23)], "open": [1252.15, 1255.03], "close": [1253.8, 1251.24],
        "high": [1265.88, 1271.5], "low": [1248.1, 1250.89], "volume": [2457300.0, 3098100.0],
        "turnover": [0.002, 0.0025], "amount": [3088526100.0, 3894630800.0],
    })
    out = akshare_src.parse_daily_bars_tx(pdf, "600519")
    assert list(out.columns) == list(SCHEMAS["daily_bars"])
    assert out["turn"].to_list() == pytest.approx([0.2, 0.25])
    assert out["preclose"][0] is None
    assert out["preclose"][1] == 1253.8


def test_parse_index_bars_tx_leaves_volume_null():
    pdf = pd.DataFrame({"date": [date(2026, 9, 22)], "open": [4569.87], "close": [4544.59], "high": [4583.38],
                       "low": [4537.04], "amount": [177863876.0]})
    out = akshare_src.parse_index_bars_tx(pdf, "sh000300")
    assert out["volume"][0] is None
    assert out["amount"][0] == 177863876.0


def test_parse_index_bars_sina_leaves_amount_null():
    pdf = pd.DataFrame({"date": [date(2026, 9, 22)], "open": [4569.87], "high": [4583.38], "low": [4537.04],
                       "close": [4544.59], "volume": [1000000.0]})
    out = akshare_src.parse_index_bars_sina(pdf, "sh000300")
    assert out["amount"][0] is None
    assert out["volume"][0] == 1000000.0


# =================================================================================================
# 八、适配器：tencent_src
# =================================================================================================

def test_tencent_parse_quotes_takes_first_level():
    rows = [{
        "code": "600000", "name": "浦发银行", "price": 10.0, "prev_close": 9.5, "open": 9.6, "high": 10.2,
        "low": 9.5, "volume": 1000.0, "amount": 10000.0, "pct": 5.0, "limit_up": 10.45, "limit_down": 8.55,
        "bids": [[9.99, 100.0], [9.98, 200.0]], "asks": [[10.0, 300.0]], "time": "2026-09-24 10:00:00",
    }]
    out = tencent_src.parse_quotes(rows)
    row = out.row(0, named=True)
    assert row["bid1"] == 9.99 and row["bid1_vol"] == 100.0
    assert row["ask1"] == 10.0 and row["ask1_vol"] == 300.0
    assert row["preclose"] == 9.5


def test_tencent_parse_quotes_handles_no_book():
    rows = [{"code": "600000", "name": "x", "price": 1.0, "bids": [], "asks": []}]
    out = tencent_src.parse_quotes(rows)
    assert out["bid1"][0] is None and out["ask1"][0] is None


def test_tencent_parse_minute1_uses_price_for_ohlc():
    points = [{"time": "09:31", "price": 10.0, "volume": 100.0, "amount": 1000.0}]
    out = tencent_src.parse_minute1(points, "2026-09-24", "600000")
    row = out.row(0, named=True)
    assert row["open"] == row["high"] == row["low"] == row["close"] == 10.0
    assert str(row["time"]) == "2026-09-24 09:31:00"


def test_tencent_parse_mkline_scales_volume_by_board():
    bars = [["202609241030", "10.0", "10.1", "10.2", "9.9", "500", {}, "0.1"]]
    main = tencent_src.parse_mkline(bars, "600000", "main")
    star = tencent_src.parse_mkline(bars, "688001", "star")
    assert main["volume"][0] == 50000.0
    assert star["volume"][0] == 500.0
    assert main["amount"][0] is None       # mkline 没有成交额


def test_tencent_parse_index_kline_scales_amount_and_volume():
    bars = [["2026-09-24", "4500", "4510", "4520", "4490", "1000000", {}, "0.5", "150000"]]
    out = tencent_src.parse_index_kline(bars, "sh000300", None, None)
    row = out.row(0, named=True)
    assert row["amount"] == 1500000000.0        # 150000万 -> 元
    assert row["volume"] == 1000000.0           # sh000300 已经是股，不用 ×100
    out2 = tencent_src.parse_index_kline(bars, "sh999999", None, None)
    assert out2["volume"][0] == 100000000.0     # 不在免乘前缀里才 ×100


def test_tencent_parse_index_kline_filters_by_date_range():
    bars = [["2026-09-22", "1", "1", "1", "1", "1", {}, "0", "1"], ["2026-09-24", "1", "1", "1", "1", "1", {}, "0", "1"]]
    out = tencent_src.parse_index_kline(bars, "sh000300", date(2026, 9, 23), date(2026, 9, 25))
    assert out["date"].to_list() == [date(2026, 9, 24)]


def test_tencent_fetch_minute_bars_rejects_bad_period():
    provider = tencent_src.TencentProvider()
    with pytest.raises(Unavailable):
        provider.fetch_minute_bars("600000", period="7")


# =================================================================================================
# 九、适配器：sina_src
# =================================================================================================

def test_sina_parse_fund_flow_adds_code_column():
    rows = [{"opendate": "2026-09-24", "r0_net": 100.0, "r1_net": 50.0, "r2_net": -20.0, "r3_net": -10.0,
            "r0": 1000.0, "r1": 500.0, "r2": 200.0, "r3": 100.0, "netamount": 120.0}]
    out = sina_src.parse_fund_flow(rows, "600000")
    row = out.row(0, named=True)
    assert row["code"] == "600000"
    assert row["date"] == date(2026, 9, 24)
    assert row["main_net"] == 150.0


def test_sina_parse_fund_flow_error_payload_is_caller_responsibility():
    assert sina_src.parse_fund_flow([], "600000").is_empty()


def test_sina_parse_minute_bars_all_columns_present():
    pdf = pd.DataFrame({"day": ["2026-09-24 14:55:00"], "open": ["1239.18"], "high": ["1239.33"],
                        "low": ["1237.29"], "close": ["1238.10"], "volume": ["66194"], "amount": ["81961384.3672"]})
    out = sina_src.parse_minute_bars(pdf, "600519")
    row = out.row(0, named=True)
    assert row["code"] == "600519"
    assert row["volume"] == 66194.0
    assert row["amount"] == pytest.approx(81961384.3672)


def test_sina_fetch_fund_flow_raises_unavailable_on_error_payload(monkeypatch):
    provider = sina_src.SinaProvider()
    monkeypatch.setattr(sina_src.net, "get_json", lambda *a, **k: {"__ERROR": True, "__ERRORMSG": "限流"})
    with pytest.raises(Unavailable):
        provider.fetch_fund_flow("600000")


def test_sina_fetch_minute_bars_rejects_bad_period():
    provider = sina_src.SinaProvider()
    with pytest.raises(Unavailable):
        provider.fetch_minute_bars("600000", period="2")


# =================================================================================================
# 十、local_src（读本地文件，不联网；本地没有时返回空）
# =================================================================================================

def test_local_daily_bars_empty_when_no_panel(workspace):
    from quant_web.providers.local_src import LocalProvider

    out = LocalProvider().fetch_daily_bars("600000")
    assert out.is_empty()
    assert list(out.columns) == list(SCHEMAS["daily_bars"])


def test_local_stock_list_and_industry_filter_live_only(workspace):
    from quant_web.market import universe as uni_mod
    from quant_web.providers.local_src import LocalProvider

    uni = pl.DataFrame({
        "code": ["600000", "600001"], "name": ["浦发银行", "已退市"], "exchange": ["SSE", "SSE"],
        "board": ["main", "main"], "list_date": [date(2000, 1, 1), date(2000, 1, 1)],
        "delist_date": [None, date(2020, 1, 1)], "is_st": [False, False],
        "industry": ["银行业", None], "status": [1, 0],
    }, schema=uni_mod.SCHEMA)
    uni.write_parquet(config.STOCK_LAB / "universe.parquet")
    provider = LocalProvider()
    assert provider.fetch_stock_list()["code"].to_list() == ["600000"]
    assert provider.fetch_industry()["code"].to_list() == ["600000"]


def test_local_lhb_date_filter(workspace):
    from quant_web.market import pools
    from quant_web.providers.local_src import LocalProvider

    df = pl.DataFrame({"date": [date(2026, 1, 1), date(2026, 2, 1)], "code": ["600000", "600000"]},
                      schema={"date": pl.Date, "code": pl.Utf8})
    for col, dtype in pools.LHB_SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    df = df.select(list(pools.LHB_SCHEMA))
    config.STOCK_LAB.mkdir(parents=True, exist_ok=True)
    df.write_parquet(config.STOCK_LAB / "lhb.parquet")
    out = LocalProvider().fetch_lhb(start=date(2026, 1, 15), end=date(2026, 2, 15))
    assert out["date"].to_list() == [date(2026, 2, 1)]


def test_local_fundamentals_empty_when_missing(workspace):
    from quant_web.providers.local_src import LocalProvider

    assert LocalProvider().fetch_fundamentals().is_empty()


# =================================================================================================
# 十一、news_src
# =================================================================================================

def test_news_provider_delegates_to_market_news(monkeypatch):
    from quant_web.market import news as news_mod

    monkeypatch.setattr(news_mod, "flash", lambda limit=100: [{"title": "test", "limit": limit}])
    provider = news_src.BuiltinNewsProvider()
    out = provider.fetch_news(limit=5)
    assert out == [{"title": "test", "limit": 5}]


# =================================================================================================
# 十二、tushare_src（可选源；假模块）
# =================================================================================================

def test_tushare_unavailable_when_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "tushare", None)   # 确保 import tushare 失败
    provider = tushare_src.TushareProvider()
    ok, reason = provider.available()
    assert ok is False
    assert "安装" in reason


@pytest.fixture()
def fake_tushare(monkeypatch):
    module = types.ModuleType("tushare")
    module.pro_api = lambda token: types.SimpleNamespace()   # 占位，测试里各自替换需要的方法
    monkeypatch.setitem(sys.modules, "tushare", module)
    return module


def test_tushare_unavailable_without_token(fake_tushare, monkeypatch):
    from quant_web import settings as settings_mod

    monkeypatch.setattr(settings_mod, "load", lambda: settings_mod.Settings())
    provider = tushare_src.TushareProvider()
    ok, reason = provider.available()
    assert ok is False
    assert "token" in reason


def test_tushare_available_with_token(fake_tushare, monkeypatch):
    from quant_web import settings as settings_mod

    s = settings_mod.Settings()
    s.providers.tushare_token = "abc123"
    monkeypatch.setattr(settings_mod, "load", lambda: s)
    provider = tushare_src.TushareProvider()
    assert provider.available() == (True, "")


def test_tushare_ts_code_and_from_ts_code():
    assert tushare_src.ts_code("600519") == "600519.SH"
    assert tushare_src.ts_code("000001") == "000001.SZ"
    assert tushare_src.from_ts_code("600519.SH") == "600519"


def test_tushare_parse_daily_bars_converts_units_and_leaves_turn_null():
    pdf = pd.DataFrame({
        "ts_code": ["600519.SH"], "trade_date": ["20260922"], "open": [1252.15], "high": [1265.88],
        "low": [1248.1], "close": [1253.8], "pre_close": [1251.24], "pct_chg": [0.2],
        "vol": [24573.0], "amount": [308852.61],
    })
    out = tushare_src.parse_daily_bars(pdf, "600519")
    row = out.row(0, named=True)
    assert row["volume"] == 2457300.0          # 手 -> 股
    assert row["amount"] == pytest.approx(308852610.0)   # 千元 -> 元
    assert row["turn"] is None                 # pro.daily 没有换手率，不能拿 pct_chg 充数


def test_tushare_parse_index_bars():
    pdf = pd.DataFrame({"ts_code": ["000300.SH"], "trade_date": ["20260922"], "close": [4544.59],
                       "open": [4569.87], "high": [4583.38], "low": [4537.04], "vol": [10000.0], "amount": [1000.0]})
    out = tushare_src.parse_index_bars(pdf, "sh000300")
    assert out["volume"][0] == 1000000.0
    assert out["amount"][0] == 1000000.0


def test_tushare_parse_stock_list_from_symbol_column():
    pdf = pd.DataFrame({"ts_code": ["600519.SH"], "symbol": ["600519"], "name": ["贵州茅台"]})
    out = tushare_src.parse_stock_list(pdf)
    assert out.row(0, named=True) == {"code": "600519", "name": "贵州茅台"}


def test_tushare_fetch_daily_bars_with_fake_pro(fake_tushare, monkeypatch):
    from quant_web import settings as settings_mod

    s = settings_mod.Settings()
    s.providers.tushare_token = "abc"
    monkeypatch.setattr(settings_mod, "load", lambda: s)
    pdf = pd.DataFrame({
        "ts_code": ["600519.SH"], "trade_date": ["20260922"], "open": [1.0], "high": [1.0], "low": [1.0],
        "close": [1.0], "pre_close": [1.0], "pct_chg": [0.1], "vol": [10.0], "amount": [1.0],
    })
    fake_pro = types.SimpleNamespace(daily=lambda **kw: pdf)
    fake_tushare.pro_api = lambda token: fake_pro
    provider = tushare_src.TushareProvider()
    out = provider.fetch_daily_bars("600519", start="2026-09-01", end="2026-09-22")
    assert out.height == 1


# =================================================================================================
# 十三、qmt_src（可选源；假模块）
# =================================================================================================

def test_qmt_unavailable_when_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "xtquant", None)
    provider = qmt_src.QmtProvider()
    ok, reason = provider.available()
    assert ok is False
    assert "xtquant" in reason


@pytest.fixture()
def fake_xtquant(monkeypatch):
    xtdata_mod = types.ModuleType("xtquant.xtdata")
    xtquant_mod = types.ModuleType("xtquant")
    xtquant_mod.xtdata = xtdata_mod
    monkeypatch.setitem(sys.modules, "xtquant", xtquant_mod)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", xtdata_mod)
    return xtdata_mod


def test_qmt_available_when_installed(fake_xtquant):
    provider = qmt_src.QmtProvider()
    assert provider.available() == (True, "")


def test_qmt_symbol():
    assert qmt_src.qmt_symbol("600519") == "600519.SH"
    assert qmt_src.qmt_symbol("000001") == "000001.SZ"


def test_qmt_parse_bars_daily():
    raw = pd.DataFrame({"open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2], "volume": [1000.0],
                        "amount": [10000.0]}, index=pd.Index(["20260924"], name="time"))
    out = qmt_src.parse_bars(raw, "600519")
    row = out.row(0, named=True)
    assert row["date"] == date(2026, 9, 24)
    assert row["close"] == 10.2
    assert list(out.columns) == list(SCHEMAS["daily_bars"])


def test_qmt_parse_bars_minute():
    raw = pd.DataFrame({"open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2], "volume": [1000.0],
                        "amount": [10000.0]}, index=pd.Index(["20260924103000"], name="time"))
    out = qmt_src.parse_bars(raw, "600519")
    assert list(out.columns) == list(SCHEMAS["minute_bars"])
    import datetime as _dt
    assert out["time"][0] == _dt.datetime(2026, 9, 24, 10, 30, 0)


def test_qmt_parse_bars_index():
    raw = pd.DataFrame({"open": [4500.0], "high": [4510.0], "low": [4490.0], "close": [4505.0], "volume": [1.0],
                        "amount": [1.0]}, index=pd.Index(["20260924"], name="time"))
    out = qmt_src.parse_bars(raw, "sh000300", index=True)
    assert list(out.columns) == list(SCHEMAS["index_bars"])
    assert out["index"][0] == "sh000300"


def test_qmt_parse_bars_empty():
    assert qmt_src.parse_bars(None, "600519").is_empty()
    assert qmt_src.parse_bars(pd.DataFrame(), "600519").is_empty()


def test_qmt_parse_quotes():
    ticks = {"600519.SH": {"lastPrice": 10.0, "lastClose": 9.5, "open": 9.6, "high": 10.2, "low": 9.5,
                          "volume": 1000.0, "amount": 10000.0, "bidPrice": [9.99], "askPrice": [10.0],
                          "bidVol": [100], "askVol": [200], "timetag": "20260924103000"}}
    out = qmt_src.parse_quotes(ticks, ["600519"])
    row = out.row(0, named=True)
    assert row["code"] == "600519"
    assert row["bid1"] == 9.99 and row["ask1"] == 10.0


def test_qmt_parse_quotes_skips_missing_symbol():
    out = qmt_src.parse_quotes({}, ["600519"])
    assert out.is_empty()


def test_qmt_fetch_minute_bars_rejects_bad_period(fake_xtquant):
    provider = qmt_src.QmtProvider()
    with pytest.raises(Unavailable):
        provider.fetch_minute_bars("600519", period="7")


# =================================================================================================
# 十四、updates.py（假 registry，永不抛异常）
# =================================================================================================

class _FakeResult:
    def __init__(self, data: pl.DataFrame, source: str = "fake") -> None:
        self.data = data
        self.source = source


class _FakeRegistry:
    """update_all 用的假注册表：按能力名返回预先安排好的结果或异常，记录每次调用"""

    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.calls: list[tuple[str, dict]] = []

    def fetch(self, capability: str, **kwargs):
        self.calls.append((capability, kwargs))
        outcome = self.plan.get(capability)
        if outcome is None:
            raise ProviderError(f"没有配置「{capability}」")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _small_frame(capability: str) -> pl.DataFrame:
    schema = SCHEMAS[capability]

    def value(col: str, dtype) -> object:
        if dtype == pl.Date:
            return date(2026, 9, 24)
        if dtype == pl.Utf8:
            return "600000" if col == "code" else "x"
        return 1.0

    row = {c: value(c, t) for c, t in schema.items()}
    if "index" in row:
        row["index"] = "sh000300" if capability == "index_bars" else "hs300"
    return pl.DataFrame([row], schema=schema)


@pytest.fixture()
def fake_registry_plan(workspace):
    plan = {
        "margin": _FakeResult(_small_frame("margin")),
        "holder_count": _FakeResult(_small_frame("holder_count")),
        "unlock": _FakeResult(_small_frame("unlock")),
        "pledge": ProviderError("模拟：股权质押全部失败"),      # 故意安排一个失败
        "forecast": _FakeResult(_small_frame("forecast")),
        "holder_trades": _FakeResult(_small_frame("holder_trades")),
        "index_bars": _FakeResult(_small_frame("index_bars")),
        "index_members": _FakeResult(_small_frame("index_members")),
        "fund_flow": _FakeResult(_small_frame("fund_flow")),
    }
    return _FakeRegistry(plan)


def test_update_all_never_raises_and_summarizes_each_archive(monkeypatch, fake_registry_plan):
    from quant_web.market import realtime

    monkeypatch.setattr(updates, "registry", lambda: fake_registry_plan)
    monkeypatch.setattr(realtime, "recent_trading_days", lambda n, until=None: [date(2026, 9, 22), date(2026, 9, 23)])
    out = updates.update_all(flow_codes=["600000", "600001"])
    assert set(out) == {"margin", "holder_count", "unlock", "pledge", "forecast", "holder_trades",
                        "index_bars", "index_members", "fund_flow"}
    for summary in out.values():
        assert set(summary) == {"ok", "rows", "source", "error"}
    assert out["margin"]["ok"] is True and out["margin"]["rows"] >= 1
    assert out["pledge"]["ok"] is False
    assert out["pledge"]["error"]


def test_update_all_without_flow_codes_skips_fund_flow(monkeypatch, fake_registry_plan):
    from quant_web.market import realtime

    monkeypatch.setattr(updates, "registry", lambda: fake_registry_plan)
    monkeypatch.setattr(realtime, "recent_trading_days", lambda n, until=None: [date(2026, 9, 22)])
    out = updates.update_all()
    assert "fund_flow" not in out


def test_update_all_survives_registry_that_always_raises(monkeypatch, workspace):
    from quant_web.market import realtime

    class _AlwaysFail:
        def fetch(self, capability, **kwargs):
            raise ProviderError("全部数据源都不可用")

    monkeypatch.setattr(updates, "registry", lambda: _AlwaysFail())
    monkeypatch.setattr(realtime, "recent_trading_days", lambda n, until=None: [date(2026, 9, 22)])
    out = updates.update_all()      # 不应该抛异常
    assert all(v["ok"] is False for v in out.values())


def test_update_holder_count_reports_ok_and_rows(monkeypatch, workspace):
    fake = _FakeRegistry({"holder_count": _FakeResult(_small_frame("holder_count"))})
    monkeypatch.setattr(updates, "registry", lambda: fake)
    out = updates.update_holder_count()
    assert out["ok"] is True and out["rows"] == 1 and out["source"] == "fake"


def test_update_pledge_reports_failure_without_raising(monkeypatch, workspace):
    fake = _FakeRegistry({"pledge": ProviderError("没有数据")})
    monkeypatch.setattr(updates, "registry", lambda: fake)
    out = updates.update_pledge()
    assert out["ok"] is False
    assert "没有数据" in out["error"]


def test_update_index_members_replaces_each_index(monkeypatch, workspace):
    def make(idx):
        return _FakeResult(pl.DataFrame({"index": [idx], "code": ["600000"], "name": ["x"], "weight": [None]}))

    calls: list[str] = []

    class _Reg:
        def fetch(self, capability, **kwargs):
            calls.append(kwargs["index"])
            return make(kwargs["index"])

    monkeypatch.setattr(updates, "registry", lambda: _Reg())
    out = updates.update_index_members(indices=("hs300", "zz500"))
    assert out["ok"] is True
    assert calls == ["hs300", "zz500"]
    assert store.load("index_members").height == 2


def test_update_fund_flow_respects_limit(monkeypatch, workspace):
    calls: list[str] = []

    class _Reg:
        def fetch(self, capability, **kwargs):
            calls.append(kwargs["code"])
            return _FakeResult(_small_frame("fund_flow"))

    monkeypatch.setattr(updates, "registry", lambda: _Reg())
    out = updates.update_fund_flow([f"{i:06d}" for i in range(10)], limit=3)
    assert len(calls) == 3
    assert out["ok"] is True


# =================================================================================================
# 十五、真实网络探测（默认不跑；每个能力至少一个适配器）
#      .venv\Scripts\python -m pytest tests/test_quant_web_providers.py -q -m network
# =================================================================================================

@pytest.mark.network
def test_network_local_and_registry_status():
    from quant_web.providers import registry as global_registry

    reg = global_registry()
    statuses = reg.status()
    assert {s["capability"] for s in statuses} == set(CAPABILITIES)


@pytest.mark.network
def test_network_tencent_daily_bars():
    provider = tencent_src.TencentProvider()
    out = provider.fetch_daily_bars("600519", start=date.today().replace(day=1))
    assert out.height >= 0     # 只要不抛异常、格式对即可（是否有数据取决于当天是否已收盘）
    assert list(out.columns) == list(SCHEMAS["daily_bars"])


@pytest.mark.network
def test_network_tencent_quotes():
    provider = tencent_src.TencentProvider()
    out = provider.fetch_quotes(["600519", "000001"])
    assert out.height >= 1


@pytest.mark.network
def test_network_tencent_index_bars():
    provider = tencent_src.TencentProvider()
    out = provider.fetch_index_bars("sh000300", start=date.today().replace(day=1), end=date.today())
    assert out.height >= 1


@pytest.mark.network
def test_network_tencent_minute_bars():
    provider = tencent_src.TencentProvider()
    out = provider.fetch_minute_bars("600519", period="5")
    assert list(out.columns) == list(SCHEMAS["minute_bars"])


@pytest.mark.network
def test_network_sina_fund_flow():
    provider = sina_src.SinaProvider()
    out = provider.fetch_fund_flow("600519")
    assert list(out.columns) == list(SCHEMAS["fund_flow"])


@pytest.mark.network
def test_network_exchange_margin():
    from quant_web.market import realtime

    provider = exchange_src.ExchangeProvider()
    day = realtime.recent_trading_days(1)[-1]
    out = provider.fetch_margin(day)
    assert out.height >= 1


@pytest.mark.network
def test_network_em_holder_count():
    provider = em_datacenter_src.EmDatacenterProvider()
    out = provider.fetch_holder_count()
    assert out.height >= 1


@pytest.mark.network
def test_network_em_unlock():
    provider = em_datacenter_src.EmDatacenterProvider()
    out = provider.fetch_unlock(date.today(), date.today() + timedelta(days=30))
    assert list(out.columns) == list(SCHEMAS["unlock"])


@pytest.mark.network
def test_network_em_pledge():
    provider = em_datacenter_src.EmDatacenterProvider()
    out = provider.fetch_pledge()
    assert out.height >= 1


@pytest.mark.network
def test_network_em_forecast():
    provider = em_datacenter_src.EmDatacenterProvider()
    out = provider.fetch_forecast()
    assert out.height >= 1


@pytest.mark.network
def test_network_em_holder_trades():
    provider = em_datacenter_src.EmDatacenterProvider()
    out = provider.fetch_holder_trades(date.today() - timedelta(days=30), date.today())
    assert list(out.columns) == list(SCHEMAS["holder_trades"])


@pytest.mark.network
def test_network_baostock_index_members():
    provider = baostock_src.BaostockProvider()
    out = provider.fetch_index_members("hs300")
    assert out.height >= 250


@pytest.mark.network
def test_network_baostock_daily_bars():
    provider = baostock_src.BaostockProvider()
    out = provider.fetch_daily_bars("600519", start=date.today().replace(day=1))
    assert list(out.columns) == list(SCHEMAS["daily_bars"])


@pytest.mark.network
def test_network_baostock_industry():
    provider = baostock_src.BaostockProvider()
    out = provider.fetch_industry()
    assert out.height > 3000


@pytest.mark.network
def test_network_baostock_trade_calendar():
    provider = baostock_src.BaostockProvider()
    out = provider.fetch_trade_calendar()
    assert out.height > 0


@pytest.mark.network
def test_network_em_lhb():
    provider = em_datacenter_src.EmDatacenterProvider()
    from quant_web.market import realtime

    day = realtime.recent_trading_days(1)[-1]
    out = provider.fetch_lhb(day, day)
    assert set(out.columns) >= {"date", "code", "name", "net_buy"}


@pytest.mark.network
def test_network_sina_minute_bars():
    provider = sina_src.SinaProvider()
    out = provider.fetch_minute_bars("600519", period="5")
    assert list(out.columns) == list(SCHEMAS["minute_bars"])


@pytest.mark.network
def test_network_news_flash():
    provider = news_src.BuiltinNewsProvider()
    out = provider.fetch_news(limit=10)
    assert isinstance(out, list)


@pytest.mark.network
def test_network_akshare_stock_list():
    provider = akshare_src.AkshareProvider()
    out = provider.fetch_stock_list()
    assert out.height > 3000


@pytest.mark.network
def test_network_akshare_index_members():
    provider = akshare_src.AkshareProvider()
    out = provider.fetch_index_members("zz1000")
    assert out.height >= 900


@pytest.mark.network
def test_network_akshare_daily_bars():
    provider = akshare_src.AkshareProvider()
    out = provider.fetch_daily_bars("600519", start=date.today().replace(day=1))
    assert list(out.columns) == list(SCHEMAS["daily_bars"])


@pytest.mark.network
def test_network_akshare_trade_calendar():
    provider = akshare_src.AkshareProvider()
    out = provider.fetch_trade_calendar()
    assert out.height > 1000


@pytest.mark.network
def test_network_akshare_index_bars():
    provider = akshare_src.AkshareProvider()
    out = provider.fetch_index_bars("sh000300", start=date.today().replace(day=1), end=date.today())
    assert list(out.columns) == list(SCHEMAS["index_bars"])
