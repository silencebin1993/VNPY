"""quant_web.market：realtime 腾讯行情解析/缓存/交易状态，pools 股池与龙虎榜，info 个股资料，news 快讯与热度。

解析测试用的是 2026-09-24 实际抓到的数据片段（内联在本文件里）；带 network 标记的是联网冒烟测试，断网自动跳过。
"""
import json
from datetime import date, datetime, timedelta

import pandas as pd
import polars as pl
import pytest

from quant_web import config
from quant_web.market import info, news, pools, realtime


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """把 config 的路径指向临时目录，绝不碰真实 workspace"""
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


def _universe(rows: list[tuple[str, str, str | None]]) -> pl.DataFrame:
    from quant_web.market import universe

    return pl.DataFrame({
        "code": [r[0] for r in rows], "name": [r[1] for r in rows],
        "exchange": [universe.exchange_of(r[0]) for r in rows], "board": [universe.board_of(r[0]) for r in rows],
        "list_date": [date(2015, 1, 5)] * len(rows), "delist_date": [None] * len(rows),
        "is_st": ["ST" in r[1] for r in rows], "industry": [r[2] for r in rows], "status": [1] * len(rows),
    }, schema=universe.SCHEMA)


def _write_universe(rows: list[tuple[str, str, str | None]]) -> pl.DataFrame:
    df = _universe(rows)
    config.STOCK_LAB.mkdir(parents=True, exist_ok=True)
    df.write_parquet(config.STOCK_LAB / "universe.parquet")
    return df


# ================================================================ realtime

QUOTE_001216 = (
    'v_sz001216="51~华瓷股份~001216~28.40~28.31~28.05~428580~211159~217421~28.40~277~28.39~53~28.38~251~28.37~80'
    '~28.36~418~28.41~31~28.42~171~28.44~17~28.45~87~28.47~40~~20260924161430~0.09~0.32~29.50~27.80'
    '~28.40/428580/1222641481~428580~122264~17.36~40.75~~29.50~27.80~6.00~70.10~82.92~3.31~31.14~25.48~1.91~733'
    '~28.53~40.31~37.61~~~0.83~122264.1481~444.1760~1564~   A~GP-A~69.84~42.86~1.37~8.11~6.22~29.50~12.95~94.65'
    '~93.99~95.86~246836199~291958338~51.44~69.43~246836199~~~85.35~0.25~~CNY~0~~28.50~-307";'
)
QUOTE_688981 = (
    'v_sh688981="1~中芯国际~688981~119.98~121.39~121.10~18660976~8774952~9886024~119.98~506~119.97~34~119.96~56'
    '~119.95~42~119.94~2~119.99~19~120.00~91~120.01~16~120.03~10~120.04~60~~20260924161453~-1.41~-1.16~121.39'
    '~119.63~119.98/18660976/2246348480~18660976~224635~0.93~142.54~~121.39~119.63~1.45~2400.31~10273.01~5.99'
    '~145.67~97.11~0.67~444~120.38~114.98~203.80~~~2.03~224634.8480~165.9683~13833~A RA~GP-A-KCB~-2.32~1.15'
    '~0.00~4.20~2.64~176.34~91.20~0.67~-5.03~-16.74~2000593909~8562264585~53.11~-2.06~2000593909~~~-11.11~-0.07'
    '~~CNY~0~___D__F__NY~119.90~94~100";'
)
QUOTE_INDEX = (
    'v_sh000001="1~上证指数~000001~3888.37~3936.52~3925.32~438530412~0~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0'
    '~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~~20260924161401~-48.15~-1.22~3930.50~3888.37'
    '~3888.37/438530412/783613001376~438530412~78361300~0.90~16.97~~3930.50~3888.37~1.07~609039.81~692434.27'
    '~0.00~-1~-1~0.91~0~3910.69~~~~~~78361300.1376~0.0000~0~ ~ZS~-2.03~0.33~~~~4258.86~3741.11~-1.17~-1.72'
    '~-3.49~4850486083049~~7.49~-1.94~4850486083049~~~0.90~-0.07~~CNY~0~~0.00~0";'
)


def test_parse_quote_line_units_and_fields() -> None:
    q = realtime.parse_quote_line(QUOTE_001216)
    assert q["code"] == "001216" and q["name"] == "华瓷股份" and q["symbol"] == "sz001216"
    assert q["price"] == 28.40 and q["prev_close"] == 28.31 and q["open"] == 28.05
    assert q["high"] == 29.50 and q["low"] == 27.80
    assert q["volume"] == 428580 * 100                  # 手 → 股
    assert q["amount"] == 1222641481.0                  # 元
    assert q["pct"] == 0.32 and q["change"] == 0.09 and q["turnover"] == 17.36
    assert q["pe"] == 40.75 and q["pb"] == 3.31
    assert q["float_cap"] == pytest.approx(70.10e8) and q["total_cap"] == pytest.approx(82.92e8)
    assert q["limit_up"] == 31.14 and q["limit_down"] == 25.48
    assert q["time"] == "2026-09-24 16:14:30"
    assert q["bids"][0] == [28.40, 27700.0] and q["asks"][0] == [28.41, 3100.0]
    assert q["avg_price"] == pytest.approx(28.528, abs=1e-3)

    star = realtime.parse_quote_line(QUOTE_688981)
    assert star["volume"] == 18660976                   # 科创板本身就是股
    assert star["avg_price"] == pytest.approx(120.38, abs=0.01)


def test_parse_quote_text_skips_unknown() -> None:
    text = QUOTE_001216 + "\n" + 'v_pv_none_match="1";\n' + QUOTE_INDEX
    rows = realtime.parse_quote_text(text)
    assert set(rows) == {"sz001216", "sh000001"}
    assert rows["sh000001"]["price"] == 3888.37
    assert rows["sh000001"]["limit_up"] is None         # 指数没有涨跌停价（-1）


def test_quotes_cache_and_order(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_fetch(symbols: list[str]) -> dict[str, dict]:
        calls.append(list(symbols))
        return realtime.parse_quote_text(QUOTE_001216 + QUOTE_688981)

    realtime._quote_cache.clear()
    monkeypatch.setattr(realtime, "_fetch_quotes", fake_fetch)
    rows = realtime.quotes(["688981", "001216", "bad!"])
    assert [r["code"] for r in rows] == ["688981", "001216"]
    realtime.quotes(["001216"])
    assert len(calls) == 1                               # 3 秒内走缓存
    rows[0]["price"] = -1
    assert realtime.quotes(["688981"])[0]["price"] == 119.98     # 返回副本，不污染缓存
    realtime._quote_cache.clear()


def test_to_symbol() -> None:
    assert realtime.to_symbol("001216") == "sz001216"
    assert realtime.to_symbol("sh000001") == "sh000001"
    assert realtime.to_symbol("920047") == "bj920047"
    with pytest.raises(ValueError):
        realtime.to_symbol("900901")


KLINE_PAYLOAD = {"code": 0, "msg": "", "data": {"sz001216": {"qfqday": [
    ["2026-09-23", "27.66", "28.31", "29.12", "27.66", "570371.00", {}, "23.11", "162633.19", ""],
    ["2026-09-24", "28.05", "28.40", "29.50", "27.80", "428580.00", {}, "17.36", "122264.15", ""],
], "qt": {}}}}


def test_parse_kline_order_and_units() -> None:
    bars = realtime.parse_kline(KLINE_PAYLOAD, "sz001216", "day", "qfq")
    assert len(bars) == 2
    b = bars[-1]
    # 腾讯顺序是 开 收 高 低
    assert (b["open"], b["close"], b["high"], b["low"]) == (28.05, 28.40, 29.50, 27.80)
    assert b["volume"] == 42858000 and b["amount"] == pytest.approx(1222641500.0)
    assert b["date"] == "2026-09-24" and b["turnover"] == 17.36
    # 北交所请求 qfq 时只返回 day 键
    bj = {"data": {"bj920047": {"day": [["2026-09-24", "15.66", "15.30", "15.80", "15.18", "34267.00", {},
                                         "1.90", "5281.18", "0.00", "0.00"]]}}}
    assert realtime.parse_kline(bj, "bj920047", "day", "qfq")[0]["close"] == 15.30
    assert realtime.parse_kline({"data": {}}, "sz001216") == []


def test_kline_paginates_and_caches(monkeypatch) -> None:
    all_days = [date(2015, 1, 1) + timedelta(days=i) for i in range(4500)]
    calls: list[tuple] = []

    def fake_request(symbol, period, adjust, end, count):
        calls.append((end, count))
        days = [d for d in all_days if not end or d <= date.fromisoformat(end)][-count:]
        return [{"date": d.isoformat(), "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0,
                 "amount": 1.0, "turnover": None} for d in days]

    realtime._kline_cache.clear()
    monkeypatch.setattr(realtime, "_kline_request", fake_request)
    bars = realtime.kline("600000", count=4200)
    assert len(bars) == 4200 and bars[-1]["date"] == all_days[-1].isoformat()
    assert len({b["date"] for b in bars}) == 4200
    assert [c[1] for c in calls] == [2000, 2000, 200]
    realtime.kline("600000", count=4200)
    assert len(calls) == 3                               # 60 秒缓存
    short = realtime.kline("600000", adjust="none", count=10)
    assert len(short) == 10
    with pytest.raises(ValueError):
        realtime.kline("600000", period="minute")
    realtime._kline_cache.clear()


MINUTE_PAYLOAD = {"code": 0, "data": {"sz001216": {
    "data": {"date": "20260924", "data": [
        "0930 28.05 21264 59645520.00", "0931 28.54 66346 186941561.00", "0932 28.61 89912 254202456.00",
        "1500 28.40 428580 1222641481.00", "1506 28.40 428600 1222700000.00",
    ]},
    "qt": {"sz001216": QUOTE_001216.split('="', 1)[1].rstrip('";').split("~")},
}}}


def test_parse_minute() -> None:
    m = realtime.parse_minute(MINUTE_PAYLOAD, "sz001216")
    assert m["date"] == "2026-09-24" and m["prev_close"] == 28.31 and m["name"] == "华瓷股份"
    pts = m["points"]
    assert [p["time"] for p in pts] == ["09:30", "09:31", "09:32", "15:00"]     # 15:06 盘后不要
    assert pts[0]["volume"] == 2126400 and pts[1]["volume"] == (66346 - 21264) * 100
    assert pts[1]["amount"] == pytest.approx(186941561.0 - 59645520.0)
    assert pts[1]["avg_price"] == pytest.approx(186941561.0 / 6634600, abs=1e-3)
    # 科创板累计量是股
    star = realtime.parse_minute_points(["0930 121.10 115256 13957502.00", "0931 120.85 608376 73588219.00"])
    assert star[0]["volume"] == 115256 and star[1]["avg_price"] == pytest.approx(73588219 / 608376, abs=1e-3)
    idx = realtime.parse_minute_points(["0930 3925.32 4098553 5934196660.30"], is_index=True)
    assert idx[0]["avg_price"] is None and idx[0]["volume"] == 409855300


def test_parse_five_day() -> None:
    payload = {"data": {"sz001216": {"data": [
        {"date": "20260924", "prec": "28.31", "data": ["0930 28.05 21264 59645520.00"]},
        {"date": "20260923", "prec": "26.47", "data": ["0930 27.66 62883 173934378.00"]},
    ]}}}
    days = realtime.parse_five_day(payload, "sz001216")
    assert [d["date"] for d in days] == ["2026-09-23", "2026-09-24"]
    assert days[0]["prev_close"] == 26.47 and len(days[1]["points"]) == 1


def _fake_calendar(monkeypatch, days: list[date]) -> None:
    monkeypatch.setattr(realtime, "trade_calendar", lambda refresh=False: days)
    monkeypatch.setattr(realtime, "_panel_dates", lambda: set())


def test_is_trading_day_and_phase(monkeypatch) -> None:
    cal = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 10, 8),
           date(2026, 12, 31)]
    _fake_calendar(monkeypatch, cal)
    assert realtime.is_trading_day(date(2026, 9, 24))
    assert not realtime.is_trading_day(date(2026, 9, 25))      # 中秋节（周五）
    assert not realtime.is_trading_day(date(2026, 9, 26))      # 周六
    assert realtime.is_trading_day(date(2027, 1, 4))           # 日历没覆盖：工作日按交易日
    tz = config.CHINA_TZ
    assert realtime.market_phase(datetime(2026, 9, 24, 9, 20, tzinfo=tz)) == "盘前"
    assert realtime.market_phase(datetime(2026, 9, 24, 9, 30, tzinfo=tz)) == "交易中"
    assert realtime.market_phase(datetime(2026, 9, 24, 11, 45, tzinfo=tz)) == "午间休市"
    assert realtime.market_phase(datetime(2026, 9, 24, 14, 59, tzinfo=tz)) == "交易中"
    assert realtime.market_phase(datetime(2026, 9, 24, 15, 0, tzinfo=tz)) == "已收盘"
    assert realtime.market_phase(datetime(2026, 9, 25, 10, 0, tzinfo=tz)) == "休市"
    # 本机时区不是北京时间：传入纽约时间也要换算
    ny = datetime(2026, 9, 23, 22, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
    assert realtime.market_phase(ny) == "交易中"                  # = 北京 9/24 10:00
    assert realtime.recent_trading_days(3, until=date(2026, 9, 27)) == cal[1:4]


def test_trade_calendar_file_cache(workspace, monkeypatch) -> None:
    realtime._calendar.update(days=None, loaded_at=0.0, source="")
    days = [date(2026, 9, 24), date(2026, 12, 31)]
    monkeypatch.setattr(realtime, "_calendar_from_sina", lambda: days)
    assert realtime.trade_calendar(refresh=True) == days
    assert (workspace / "cache" / "trade_calendar.json").exists()
    realtime._calendar.update(days=None, loaded_at=0.0, source="")

    def boom():
        raise ConnectionError("x")

    monkeypatch.setattr(realtime, "_calendar_from_sina", boom)
    monkeypatch.setattr(realtime, "_calendar_from_baostock", boom)
    assert realtime.trade_calendar() == days                   # 读本地缓存
    realtime._calendar.update(days=None, loaded_at=0.0, source="")

    # 没有缓存且来源全失败：返回空表，并且短时间内不再反复请求
    (workspace / "cache" / "trade_calendar.json").unlink()
    tries: list[int] = []

    def boom_count():
        tries.append(1)
        raise ConnectionError("x")

    monkeypatch.setattr(realtime, "_calendar_from_sina", boom_count)
    assert realtime.trade_calendar() == []
    assert realtime.trade_calendar() == []
    assert len(tries) == 1
    monkeypatch.setattr(realtime, "_panel_dates", lambda: set())
    assert realtime.is_trading_day(date(2026, 9, 25))          # 退化为按工作日判断
    realtime._calendar.update(days=None, loaded_at=0.0, source="")


# ================================================================ pools

RAW_ZT = [{"c": "000498", "m": 0, "n": "山东路桥", "p": 5650, "zdp": 9.922179222106934, "amount": 314302752,
           "ltsz": 8188814951.35, "tshare": 8771297480.8, "hs": 3.839857339859009, "lbc": 1, "fbt": 92500,
           "lbt": 93136, "fund": 59982095, "zbc": 1, "hybk": "基础建设", "zttj": {"days": 1, "ct": 1}}]
RAW_PREV = [{"c": "000560", "m": 0, "n": "我爱我家", "p": 3660, "ztp": 4250, "zdp": -5.18, "amount": 2936520448,
             "ltsz": 8574238515.54, "tshare": 8621133103.68, "hs": 33.08, "zf": 13.21, "zs": 0.0, "yfbt": 103139,
             "ylbc": 3, "hybk": "房地产服", "zttj": {"days": 4, "ct": 3}}]
RAW_DT = [{"c": "000607", "m": 0, "n": "华媒控股", "p": 4030, "zdp": -10.04, "amount": 488481952,
           "ltsz": 3566800537.04, "tshare": 4101324616.48, "pe": -127.9, "hs": 13.42, "fund": 1981083,
           "lbt": 150000, "fba": 411448345, "days": 2, "oc": 4, "hybk": "广告营销"}]
RAW_STRONG = [{"c": "301311", "m": 0, "n": "昆船智能", "p": 17980, "ztp": 17980, "ztf": "1", "zdp": 20.03,
               "amount": 569676288, "ltsz": 4315200000.0, "tshare": 4315200000.0, "hs": 14.1, "nh": 1, "cc": 1,
               "lb": 3.97, "zs": 0.0, "zttj": {"days": 1, "ct": 1}, "hybk": "通用设备"}]
RAW_SUBNEW = [{"c": "920201", "m": 0, "n": "N百瑞吉", "p": 72350, "ztp": 1000000000, "ztf": "0", "zdp": 331.4,
               "amount": 608990448, "ltsz": 550472587.45, "tshare": 4936138945.2, "hs": 97.3, "ods": 1,
               "od": 20260924, "ipod": 20260924, "o": 1, "nh": 0, "zttj": {"days": 0, "ct": 0}, "hybk": "医疗器械"}]


def test_time_text() -> None:
    assert pools.time_text(92500) == "09:25:00"
    assert pools.time_text("092500") == "09:25:00"
    assert pools.time_text(150000) == "15:00:00"
    assert pools.time_text(0) is None and pools.time_text(None) is None and pools.time_text("") is None


def test_parse_pool_raw_all_kinds() -> None:
    day = date(2026, 9, 24)
    zt = pools.parse_pool_raw("zt", RAW_ZT, day).row(0, named=True)
    assert zt["code"] == "000498" and zt["price"] == 5.65 and zt["streak"] == 1 and zt["open_times"] == 1
    assert zt["first_time"] == "09:25:00" and zt["last_time"] == "09:31:36"
    assert zt["seal_amount"] == 59982095 and zt["stat"] == "1/1" and zt["industry"] == "基础建设"
    assert zt["date"] == day and zt["limit_price"] is None
    prev = pools.parse_pool_raw("prev", RAW_PREV, day).row(0, named=True)
    assert prev["streak"] == 3 and prev["first_time"] == "10:31:39" and prev["limit_price"] == 4.25
    assert (prev["stat_days"], prev["stat_count"]) == (4, 3)
    dt = pools.parse_pool_raw("dt", RAW_DT, day).row(0, named=True)
    assert dt["streak"] == 2 and dt["open_times"] == 4 and dt["board_amount"] == 411448345
    assert dt["last_time"] == "15:00:00" and dt["pe"] == -127.9
    strong = pools.parse_pool_raw("strong", RAW_STRONG, day).row(0, named=True)
    assert strong["reason"] == "60日新高" and strong["is_new_high"] is True and strong["volume_ratio"] == 3.97
    sub = pools.parse_pool_raw("sub_new", RAW_SUBNEW, day).row(0, named=True)
    assert sub["limit_price"] is None and sub["open_days"] == 1 and sub["list_date"] == day
    assert sub["open_date"] == day and sub["is_new_high"] is False
    assert pools.parse_pool_raw("zt", [], day).columns == list(pools.POOL_SCHEMA)


def test_parse_pool_ak_matches_raw() -> None:
    day = date(2026, 9, 24)
    pdf = pd.DataFrame([{
        "序号": 1, "代码": "000498", "名称": "山东路桥", "涨跌幅": 9.922179222106934, "最新价": 5.65,
        "成交额": 314302752, "流通市值": 8188814951.35, "总市值": 8771297480.8, "换手率": 3.839857339859009,
        "封板资金": 59982095, "首次封板时间": "092500", "最后封板时间": "093136", "炸板次数": 1, "涨停统计": "1/1",
        "连板数": 1, "所属行业": "基础建设",
    }])
    a = pools.parse_pool_ak(pdf, day).drop("fetched_at")
    b = pools.parse_pool_raw("zt", RAW_ZT, day).drop("fetched_at")
    assert a.equals(b)
    assert pools.parse_pool_ak(pd.DataFrame(), day).is_empty()


def test_archive_and_load(workspace, monkeypatch) -> None:
    day = date(2026, 9, 24)
    raw = {"zt": RAW_ZT, "prev": RAW_PREV, "dt": RAW_DT, "strong": RAW_STRONG, "sub_new": RAW_SUBNEW, "zb": []}
    monkeypatch.setattr(pools, "_fetch_raw", lambda kind, d: pools.parse_pool_raw(kind, raw[kind], d))
    monkeypatch.setattr(realtime, "is_trading_day", lambda d: d.weekday() < 5 and d != date(2026, 9, 25))
    monkeypatch.setattr(pools, "china_now", lambda: datetime(2026, 9, 24, 14, 0, tzinfo=config.CHINA_TZ))
    res = pools.archive(day)
    assert res["skipped"] and not (workspace / "stock_lab" / "pools" / "zt").exists()   # 未收盘不存
    monkeypatch.setattr(pools, "china_now", lambda: datetime(2026, 9, 25, 9, 0, tzinfo=config.CHINA_TZ))
    assert "不是交易日" in pools.archive(date(2026, 9, 25))["skipped"]
    res = pools.archive(day)
    assert res["counts"]["zt"] == 1 and res["counts"]["zb"] == 0 and not res["failed"]
    assert (workspace / "stock_lab" / "pools" / "zt" / "20260924.parquet").exists()
    assert not (workspace / "stock_lab" / "pools" / "zb").exists()          # 空池不写文件
    zt = pools.load_archive("zt")
    assert zt.height == 1 and zt["date"][0] == day and zt.columns == list(pools.POOL_SCHEMA)
    assert pools.load_archive("zt", start="2026-09-25").is_empty()
    assert pools.load_archive("zb").is_empty()
    assert pools.archived_days("prev") == [day]
    assert pools.get_pool("zt", day).height == 1


LHB_ROWS = [
    {"SECURITY_CODE": "000504", "SECUCODE": "000504.SZ", "SECURITY_NAME_ABBR": "南华生物",
     "TRADE_DATE": "2026-09-24 00:00:00", "EXPLAIN": "2家机构买入，成功率42.58%", "CLOSE_PRICE": 13.73,
     "CHANGE_RATE": 7.6019, "BILLBOARD_NET_AMT": 117078833, "BILLBOARD_BUY_AMT": 234403387,
     "BILLBOARD_SELL_AMT": 117324554, "BILLBOARD_DEAL_AMT": 351727941, "ACCUM_AMOUNT": 1525092476,
     "DEAL_NET_RATIO": 7.68, "DEAL_AMOUNT_RATIO": 23.06, "TURNOVERRATE": 35.0013, "FREE_MARKET_CAP": 4518850524.54,
     "EXPLANATION": "日振幅值达到15%的前5只证券"},
    {"SECURITY_CODE": "200505", "SECURITY_NAME_ABBR": "京粮B", "TRADE_DATE": "2026-09-24 00:00:00",
     "EXPLANATION": "B股", "BILLBOARD_NET_AMT": 1},
]


def test_parse_lhb_rows_filters_b_shares() -> None:
    df = pools.parse_lhb_rows(LHB_ROWS)
    assert df.height == 1
    r = df.row(0, named=True)
    assert r["date"] == date(2026, 9, 24) and r["code"] == "000504" and r["net_buy"] == 117078833
    assert r["reason"] == "日振幅值达到15%的前5只证券" and r["amount_total"] == 351727941
    assert r["market_amount"] == 1525092476 and r["turnover"] == 35.0013 and r["pct"] == 7.6019


def test_update_lhb_incremental_and_dedupe(workspace, monkeypatch) -> None:
    calls: list[tuple[date, date]] = []

    def fake_fetch(start: date, end: date, code: str | None = None) -> pl.DataFrame:
        calls.append((start, end))
        rows = []
        for i, d in enumerate([start, end]):
            rows.append({**LHB_ROWS[0], "TRADE_DATE": d.isoformat(), "SECURITY_CODE": f"00050{i}"})
        return pools.parse_lhb_rows(rows)

    monkeypatch.setattr(pools, "fetch_lhb", fake_fetch)
    monkeypatch.setattr(pools, "china_now", lambda: datetime(2026, 9, 24, 20, 0, tzinfo=config.CHINA_TZ))
    res = pools.update_lhb(start=date(2026, 7, 15))
    assert res["months"] == 3 and res["rows"] == 6 and not res["failed"]
    assert sorted(calls)[0] == (date(2026, 7, 15), date(2026, 7, 31))
    calls.clear()
    res2 = pools.update_lhb()                      # 增量：从最后日期前7天开始，重复记录去重
    assert calls == [(date(2026, 9, 17), date(2026, 9, 24))]
    df = pools.load_lhb()
    assert df.height == res2["rows"] == 7          # 新增 9/17 一条，9/24 重复
    assert df.select(["date", "code", "reason"]).is_duplicated().sum() == 0
    stamp: int = pools._lhb_file().stat().st_mtime_ns
    res3 = pools.update_lhb()                      # 没有新记录：不重写文件
    assert res3["added"] == 0 and pools._lhb_file().stat().st_mtime_ns == stamp
    assert pools._months(date(2025, 12, 20), date(2026, 1, 3)) == [
        (date(2025, 12, 20), date(2025, 12, 31)), (date(2026, 1, 1), date(2026, 1, 3))]


# ================================================================ info

THS_CONCEPT_HTML = """
<table class="gnContent"><tbody>
<tr><td>1</td><td class="gnName"  clid="885926">
    牙科医疗     </td><td class="" ><a class="gnltg" code="001216">华瓷股份</a></td></tr>
<tr class="extend_content"><td colspan="4">说明</td></tr>
<tr><td>2</td><td class="gnName" clid="885642">跨境电商</td><td></td></tr>
<tr><td>3</td><td class="gnName" clid="1">股权转让(并购重组)</td><td></td></tr>
</tbody></table>
"""
EM_CORE = {"ssbk": [
    {"BOARD_NAME": "轻工制造", "IS_PRECISE": None, "BOARD_RANK": 1},
    {"BOARD_NAME": "湖南板块", "IS_PRECISE": "0", "BOARD_RANK": 4},
    {"BOARD_NAME": "特高压", "IS_PRECISE": "1", "BOARD_RANK": 17},
    {"BOARD_NAME": "跨境电商", "IS_PRECISE": "1", "BOARD_RANK": 15},
], "hxtc": [
    {"KEY_CLASSIF": "经营范围", "KEYWORD": "经营范围"},
    {"KEY_CLASSIF": "主营业务", "KEYWORD": "日用陶瓷制品的研发、设计、生产和销售"},
]}
SINA_FLOW = [
    {"opendate": "2026-09-24", "trade": "28.3600", "netamount": "84590839.0800", "r0": "635790441.1400",
     "r1": "332430424.0000", "r2": "181121637.9600", "r3": "40980495.3000", "r0_net": "116502078.3400",
     "r1_net": "-24983724.0000", "r2_net": "-9609652.9600", "r3_net": "2682137.7000"},
    {"opendate": "2026-09-23", "trade": "28.3100", "netamount": "11220195.2500", "r0": "1306882517.5600",
     "r1": "306841564.4000", "r2": "144323223.1000", "r3": "42770126.4900", "r0_net": "-27004078.3400",
     "r1_net": "3904913.6800", "r2_net": "24802507.4200", "r3_net": "9516852.4900"},
]


def test_info_parsers() -> None:
    assert info.parse_ths_concepts(THS_CONCEPT_HTML) == ["牙科医疗", "跨境电商", "股权转让(并购重组)"]
    concepts, business = info.parse_em_core(EM_CORE)
    assert concepts == ["特高压", "跨境电商"] and business == "日用陶瓷制品的研发、设计、生产和销售"
    flow = info.parse_sina_flow(SINA_FLOW)
    assert [f["date"] for f in flow] == ["2026-09-23", "2026-09-24"]
    last = flow[-1]
    assert last["main_net"] == pytest.approx(116502078.34 - 24983724.0)
    total = 635790441.14 + 332430424.0 + 181121637.96 + 40980495.3
    assert last["main_pct"] == pytest.approx((116502078.34 - 24983724.0) / total * 100, abs=0.01)
    assert last["super_net"] == pytest.approx(116502078.34) and last["small_net"] == pytest.approx(2682137.7)
    assert info.parse_sina_flow({"__ERROR": 3}) == []


def test_latest_finance() -> None:
    fund = pl.DataFrame({"code": ["001216", "001216", "600000"],
                         "report_date": [date(2025, 12, 31), date(2026, 6, 30), date(2026, 6, 30)],
                         "revenue": [1.0e9, 5.76e8, 9.0e10], "eps": [0.8, 0.39, 0.89], "eps_ttm": [0.8, 0.8999999999, 1.4]})
    fin = info.latest_finance(fund, "001216")
    assert fin["report_date"] == date(2026, 6, 30) and fin["eps"] == 0.39 and fin["eps_ttm"] == 0.9
    assert fin["roe"] is None
    assert info.latest_finance(fund, "000001") is None


def test_profile_fail_soft_and_cache(workspace, monkeypatch) -> None:
    _write_universe([("001216", "华瓷股份", "非金属矿物制品业")])
    calls: dict[str, int] = {}

    def stub(name, value):
        def fn(code):
            calls[name] = calls.get(name, 0) + 1
            if isinstance(value, Exception):
                raise value
            return value
        return fn

    monkeypatch.setattr(info, "_valuation", stub("valuation", {"pe_ttm": 40.75, "pb": 3.31, "total_cap": 8.3e9,
                                                                 "float_cap": 7.0e9, "price": 28.4, "pct": 0.32}))
    monkeypatch.setattr(info, "_finance", stub("finance", {"report_date": date(2026, 6, 30), "eps": 0.39}))
    monkeypatch.setattr(info, "_concepts", stub("concepts", {"concepts": ["特高压"], "business": "日用陶瓷",
                                                               "error": None}))
    monkeypatch.setattr(info, "_fund_flow", stub("fund_flow", ConnectionError("新浪不可用")))
    monkeypatch.setattr(info, "_lhb", stub("lhb", [{"date": date(2026, 9, 24), "reason": "x", "net_buy": 1.0,
                                                    "buy": 2.0, "sell": 1.0}]))
    monkeypatch.setattr(info, "_news", stub("news", []))
    monkeypatch.setattr(info, "_limit_history", stub("limit_history", [{"date": date(2026, 9, 22), "streak": 6,
                                                                        "one_word": True}]))
    info._mem_cache.clear()
    p = info.profile("1216")
    assert p["code"] == "001216" and p["name"] == "华瓷股份" and p["board"] == "main" and p["exchange"] == "SZSE"
    assert p["industry"] == "非金属矿物制品业" and p["list_date"] == "2015-01-05"
    assert p["valuation"]["pe_ttm"] == 40.75 and p["finance"]["report_date"] == "2026-06-30"
    assert p["concepts"] == ["特高压"] and p["fund_flow"] == [] and "fund_flow" in p["errors"]
    assert "资金流向" in p["errors"]["fund_flow"]
    assert p["lhb"][0]["date"] == "2026-09-24" and p["limit_history"][0]["streak"] == 6
    json.dumps(p, ensure_ascii=False)
    assert (workspace / "cache" / "profile" / "001216.json").exists()
    info.profile("001216")
    assert calls["valuation"] == 1                       # 走缓存
    info._mem_cache.clear()
    info.profile("001216")
    assert calls["valuation"] == 1                       # 文件缓存
    info.profile("001216", refresh=True)
    assert calls["valuation"] == 2
    with pytest.raises(ValueError):
        info.profile("900901")
    info._mem_cache.clear()


# ================================================================ news

def test_split_title_key_and_tags() -> None:
    assert news.split_title("", "【习近平将同美国总统特朗普会谈】当地时间9月24日上午…")[0] == "习近平将同美国总统特朗普会谈"
    t, _ = news.split_title("", "财联社9月24日电，阿联酋民航局确认伊朗航班暂停。")
    assert t.startswith("阿联酋民航局")
    assert news.news_key("华瓷股份：股票交易异常波动！", "") == news.news_key("华瓷股份 股票交易异常波动", "")
    assert "国家政策" in news.tag_text("国务院常务会议部署加快发展新质生产力")
    assert "部委" in news.tag_text("工信部印发人形机器人产业发展行动方案")
    assert "产业热点" in news.tag_text("人形机器人概念持续走高")
    assert "部委" not in news.tag_text("美国财政部宣布新一轮制裁")     # 外国机构不算国内政策
    assert "国际" in news.tag_text("美国财政部宣布新一轮制裁")
    assert "货币财政" in news.tag_text("央行开展5000亿元逆回购操作")
    assert "货币财政" not in news.tag_text("日本央行表态")
    assert news.tag_text("今天天气不错") == []


CLS_ROWS = [
    {"id": 1, "ctime": 1790265600, "title": "", "content": "【中际旭创：完成50亿元股份回购】财联社9月24日电，…",
     "level": "B", "shareurl": "https://api3.cls.cn/share/article/1",
     "stock_list": [{"name": "中际旭创", "StockID": "sz300308"}, {"name": "某港股", "StockID": "hk00700"}]},
    {"id": 2, "ctime": 1790265000, "title": "工信部发布通知", "content": "大力支持通信设备产业发展", "level": "C",
     "stock_list": []},
]
EM_ROWS = [
    {"code": "202609243883", "showTime": "2026-09-24 22:01:00", "title": "中际旭创：完成50亿元股份回购",
     "summary": "【中际旭创：完成50亿元股份回购】…", "stockList": ["0.300308", "90.BK1361"], "titleColor": 0},
]
SINA_ROWS = [
    {"id": 9, "create_time": "2026-09-24 21:59:00", "rich_text": "【华瓷股份：股票交易异常波动 提示多项风险】…",
     "ext": json.dumps({"stocks": [{"market": "cn", "symbol": "sz000625", "key": "长安"}],
                        "docurl": "https://finance.sina.com.cn/x.shtml"}), "is_focus": 0},
]


def test_parse_sources() -> None:
    cls = news.parse_cls(CLS_ROWS)
    assert cls[0]["title"] == "中际旭创：完成50亿元股份回购" and cls[0]["codes"] == ["300308"]
    assert cls[0]["time"] == datetime.fromtimestamp(1790265600, config.CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    assert cls[0]["important"] is True and cls[1]["important"] is False
    em = news.parse_em(EM_ROWS)
    assert em[0]["codes"] == ["300308"] and em[0]["url"].endswith("202609243883.html")
    sina = news.parse_sina(SINA_ROWS)
    assert sina[0]["codes"] == [] and sina[0]["title"].startswith("华瓷股份")      # 不用新浪的机器标签
    assert sina[0]["url"] == "https://finance.sina.com.cn/x.shtml"


def test_merge_items_dedupe_and_cutoff() -> None:
    now = datetime(2026, 9, 24, 23, 0, tzinfo=config.CHINA_TZ)
    items = news.parse_em(EM_ROWS) + news.parse_cls(CLS_ROWS) + news.parse_sina(SINA_ROWS)
    items.append({"time": "2026-09-20 10:00:00", "title": "很久以前", "content": "", "url": "", "source": "em",
                  "codes": [], "important": False})
    store = news.merge_items(news._empty_store(), items, now=now)
    assert store.height == 3                              # 两条"中际旭创回购"合并，过期的丢弃
    row = store.filter(pl.col("title").str.contains("中际旭创")).row(0, named=True)
    assert row["source"] == "cls" and row["codes"] == ["300308"] and row["important"] is True
    assert store["time"].to_list() == sorted(store["time"].to_list(), reverse=True)
    again = news.merge_items(store, items, now=now)
    assert again.height == 3


def test_stock_matcher_rules() -> None:
    uni = _universe([
        ("300308", "中际旭创", "计算机、通信和其他电子设备制造业"), ("000002", "万科Ａ", "房地产业"),
        ("000528", "柳工", "专用设备制造业"), ("300024", "机器人", "通用设备制造业"),
        ("688498", "XD源杰科", "计算机、通信和其他电子设备制造业"), ("600146", "*ST商赢", "纺织服装、服饰业"),
    ])
    m = news.StockMatcher(uni)
    assert m.match("中际旭创完成回购") == ["300308"]
    assert m.match("万科今日发布公告") == []                       # 两字简称没有明确上下文
    assert m.match("万科A：销售额同比下降") == ["000002"]
    assert m.match("万科（000002）销售额下降") == ["000002"]
    assert m.match("柳工股份") == ["000528"]
    assert m.match("稚晖君任越影机器人公司法定代表人") == []        # 易混淆简称
    assert m.match("机器人：签订重大合同") == ["300024"]
    assert m.match("行业龙头机器人(300024)涨停") == ["300024"]
    assert m.match("源杰科技：股东拟减持") == ["688498"]
    assert m.match("*ST商赢收到退市决定") == ["600146"]
    assert news.StockMatcher.aliases("万科Ａ") == ["万科A", "万科"]


def test_industry_keywords() -> None:
    assert news.industry_keywords("货币金融服务") == ["银行"]
    assert news.industry_keywords("仪器仪表制造业") == ["仪器仪表"]
    assert news.industry_keywords("互联网和相关服务") == ["互联网", "平台经济"]
    assert news.industry_keywords("纺织服装、服饰业") == ["纺织服装", "服饰"]
    assert news.industry_keywords(None) == []


def test_compute_heat() -> None:
    uni = _universe([
        ("300308", "中际旭创", "计算机、通信和其他电子设备制造业"), ("600000", "浦发银行", "货币金融服务"),
        ("000002", "万科Ａ", "房地产业"), ("600519", "贵州茅台", "酒、饮料和精制茶制造业"),
    ])
    items = [
        {"title": "中际旭创：完成回购", "content": "", "tags": [], "stocks": [{"code": "300308", "name": "中际旭创"}]},
        {"title": "工信部：支持通信产业", "content": "中际旭创受益", "tags": ["部委", "产业扶持"],
         "stocks": [{"code": "300308", "name": "中际旭创"}]},
        {"title": "人民银行开展逆回购", "content": "", "tags": ["货币财政"], "stocks": []},
        {"title": "国务院：促进房地产市场平稳", "content": "", "tags": ["国家政策"], "stocks": []},
    ]
    h = news.compute_heat(items, uni, ["300308", "600000", "000002", "600519", "999999"])
    d = {r["code"]: r for r in h.iter_rows(named=True)}
    assert d["300308"]["news_count"] == 2 and d["300308"]["policy_count"] == 2      # 直接政策1 + 行业政策1
    assert d["600000"]["policy_count"] == 0          # "人民银行"不算银行业消息，且货币财政不按行业加
    assert d["000002"]["policy_count"] == 1 and d["000002"]["news_count"] == 0
    assert d["300308"]["news_z"] > d["000002"]["news_z"] > d["600519"]["news_z"]
    assert d["999999"]["news_z"] == 0.0 and d["999999"]["news_count"] == 0
    empty = news.compute_heat([], uni, ["300308"])
    assert empty["news_z"].to_list() == [0.0]


def test_flash_and_heat_with_fake_sources(workspace, monkeypatch) -> None:
    _write_universe([("300308", "中际旭创", "计算机、通信和其他电子设备制造业"),
                     ("001216", "华瓷股份", "非金属矿物制品业")])
    now = datetime.now(config.CHINA_TZ)
    fresh = [dict(r, ctime=int(now.timestamp()) - 60 * i) for i, r in enumerate(CLS_ROWS)]
    sina = [dict(SINA_ROWS[0], create_time=(now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"))]
    calls: list[str] = []
    monkeypatch.setattr(news, "_FETCHERS", {
        "cls": lambda pages: calls.append("cls") or news.parse_cls(fresh),
        "em": lambda pages: (_ for _ in ()).throw(ConnectionError("down")),
        "sina": lambda pages: news.parse_sina(sina),
    })
    monkeypatch.setattr(news, "_ak_fallback", lambda source: (_ for _ in ()).throw(ConnectionError("ak down")))
    news._state.update(store=None, fetched_at=0.0, errors={})
    news._heat_cache.clear()
    items = news.flash(10)
    assert len(items) == 3 and "em" in news.last_errors()
    assert items[0]["time"] >= items[-1]["time"]
    by_title = {x["title"]: x for x in items}
    assert by_title["中际旭创：完成50亿元股份回购"]["stocks"] == [{"code": "300308", "name": "中际旭创"}]
    assert by_title["工信部发布通知"]["tags"] == ["部委", "产业扶持"]
    assert by_title["华瓷股份：股票交易异常波动 提示多项风险"]["stocks"][0]["code"] == "001216"
    news.flash(10)
    assert calls == ["cls"]                               # 60 秒内不重复抓取
    assert (workspace / "cache" / "news_store.parquet").exists()
    h = news.news_heat(["300308", "001216", "600000"], hours=24)
    d = {r["code"]: r for r in h.iter_rows(named=True)}
    assert d["300308"]["news_count"] == 1 and d["300308"]["policy_count"] == 1     # 通信行业政策
    assert d["001216"]["news_count"] == 1 and d["600000"]["news_z"] == 0.0
    assert h.columns == ["code", "news_count", "policy_count", "news_z"]
    news._state.update(store=None, fetched_at=0.0, errors={})
    news._heat_cache.clear()


# ================================================================ 联网冒烟测试

def _online() -> bool:
    from quant_web import net

    try:
        net.get("https://qt.gtimg.cn/q=sh600000", timeout=5, retries=1)
        return True
    except ConnectionError:
        return False


@pytest.mark.network
def test_live_smoke(workspace) -> None:
    if not _online():
        pytest.skip("没有网络")
    realtime._quote_cache.clear()
    q = realtime.quote("600000")
    assert q and q["name"] and q["price"] and q["prev_close"] and q["time"]
    idx = realtime.index_quotes()
    assert {r["code"] for r in idx} >= {"sh000001", "sz399006"}
    bars = realtime.kline("600000", count=30)
    assert len(bars) == 30 and bars[-1]["close"] > 0 and bars[-1]["volume"] > 0
    assert len(realtime.kline("600000", period="week", adjust="", count=5)) == 5
    m = realtime.minute("600000")
    assert m["prev_close"] and len(m["points"]) >= 1
    assert realtime.market_phase() in {"盘前", "交易中", "午间休市", "已收盘", "休市"}
    assert len(realtime.trade_calendar()) > 1000

    last_day = realtime.recent_trading_days(1, until=datetime.now(config.CHINA_TZ).date() - timedelta(days=1))[0]
    zt = pools.fetch_pool("zt", last_day)
    assert zt.height > 0 and zt["first_time"].drop_nulls().str.contains(r"^\d{2}:\d{2}:\d{2}$").all()
    lhb = pools.fetch_lhb(last_day - timedelta(days=7), last_day)
    assert lhb.height > 0 and lhb["code"].str.len_chars().eq(6).all()

    items = news.flash(20)
    assert items and all(x["time"] and x["title"] for x in items)

    p = info.profile("600000", refresh=True)
    assert p["name"] and p["valuation"]["pe_ttm"] is not None and p["fund_flow"] and p["concepts"]
    assert set(p) >= {"code", "name", "exchange", "board", "industry", "list_date", "concepts", "valuation",
                      "finance", "fund_flow", "lhb", "news", "limit_history", "errors"}
