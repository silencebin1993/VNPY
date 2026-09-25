"""quant_web.market：history 昨收/除权/面板读写/增量更新，universe 代码工具，fundamentals 解析"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest

from quant_web import config
from quant_web.market import fundamentals, history, universe


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


# ---------------------------------------------------------------- universe

def test_code_helpers() -> None:
    assert universe.exchange_of("600519") == "SSE"
    assert universe.exchange_of("001216") == "SZSE"
    assert universe.exchange_of("300750") == "SZSE"
    assert universe.exchange_of("430047") == "BSE"
    assert universe.exchange_of("920002") == "BSE"
    assert universe.board_of("688981") == "star"
    assert universe.board_of("689009") == "star"
    assert universe.board_of("301001") == "chinext"
    assert universe.board_of("302132") == "chinext"
    assert universe.board_of("000001") == "main"
    assert universe.board_of("920002") == "bj"
    assert universe.market_symbol("001216") == "sz001216"
    assert universe.market_symbol("600000") == "sh600000"
    assert universe.market_symbol("430047") == "bj430047"
    assert universe.bs_symbol("600000") == "sh.600000"
    assert universe.vt_symbol("001216") == "001216.SZSE"
    with pytest.raises(ValueError):
        universe.exchange_of("900901")       # B股
    with pytest.raises(ValueError):
        universe.exchange_of("200002")
    assert not universe.is_a_share("900901")
    assert not universe.is_a_share("12345")
    assert universe.is_a_share("603259")
    assert universe.clean_name("万  科Ａ") == "万科Ａ"
    assert universe.name_is_st("*ST美丽") and universe.name_is_st("ST晨鸣") and not universe.name_is_st("华瓷股份")


def test_search(workspace) -> None:
    uni = pl.DataFrame({
        "code": ["000002", "001216", "600519", "000004"],
        "name": ["万科Ａ", "华瓷股份", "贵州茅台", "国华退"],
        "exchange": ["SZSE", "SZSE", "SSE", "SZSE"],
        "board": ["main"] * 4,
        "list_date": [date(1991, 1, 29), date(2021, 10, 19), date(2001, 8, 27), date(1991, 1, 14)],
        "delist_date": [None, None, None, date(2026, 7, 14)],
        "is_st": [False] * 4,
        "industry": ["房地产业", "非金属矿物制品业", "酒、饮料和精制茶制造业", None],
        "status": [1, 1, 1, 0],
    }, schema=universe.SCHEMA)
    uni.write_parquet(config.STOCK_LAB / "universe.parquet")
    assert universe.load_universe().height == 4
    assert [r["code"] for r in universe.search("0012")] == ["001216"]
    assert universe.search("600519")[0]["name"] == "贵州茅台"
    assert universe.search("茅台")[0]["code"] == "600519"
    assert universe.search("万科a")[0]["code"] == "000002"       # 全角/大小写
    assert [r["code"] for r in universe.search("000")] == ["000002", "000004"]   # 退市排后
    assert set(universe.search("600519")[0]) == {"code", "name", "board", "industry"}
    assert universe.search("") == []


def test_load_universe_empty_has_columns(workspace) -> None:
    df = universe.load_universe()
    assert df.is_empty() and list(df.columns) == list(universe.SCHEMA)


def test_first_bars_fill_list_dates(workspace) -> None:
    uni = pl.DataFrame({"code": ["920002", "600519"], "name": ["万达轴承", "贵州茅台"],
                        "exchange": ["BSE", "SSE"], "board": ["bj", "main"],
                        "list_date": [None, date(2001, 8, 27)], "delist_date": [None, None],
                        "is_st": [False, False], "industry": [None, None], "status": [1, 1]},
                       schema=universe.SCHEMA)
    uni.write_parquet(config.STOCK_LAB / "universe.parquet")
    universe.save_first_bars(pl.DataFrame({"code": ["920002", "600519"],
                                           "first_date": [date(2024, 5, 30), date(2018, 7, 3)],
                                           "complete": [True, False]}))
    out = universe.fill_list_dates()
    assert dict(zip(out["code"], out["list_date"], strict=True)) == {
        "920002": date(2024, 5, 30), "600519": date(2001, 8, 27)}

    # 没有新内容时不重写文件（文件修改时间是面板缓存的失效依据）
    uni_file = config.STOCK_LAB / "universe.parquet"
    bars_file = config.STOCK_LAB / "first_bars.parquet"
    stamps = (uni_file.stat().st_mtime_ns, bars_file.stat().st_mtime_ns)
    universe.save_first_bars(pl.DataFrame({"code": ["920002"], "first_date": [date(2024, 6, 3)], "complete": [True]}))
    universe.fill_list_dates()
    assert (uni_file.stat().st_mtime_ns, bars_file.stat().st_mtime_ns) == stamps


def test_search_pinyin_initials_without_pypinyin(workspace, monkeypatch) -> None:
    """没装 pypinyin 时按 GB2312 编码估算拼音首字母（常用字），常见多音词单独处理"""
    monkeypatch.setitem(__import__("sys").modules, "pypinyin", None)       # import 会抛 ImportError
    monkeypatch.setattr(universe, "_initials_cache", {})
    assert universe._initials("贵州茅台") == "gzmt"
    assert universe._initials("*ST中程") == "stzc"
    assert universe._initials("平安银行") == "payh" and universe._initials("重庆啤酒") == "cqpj"
    assert universe._initials("比亚迪") == "byd" and universe._initials("东方财富") == "dfcf"
    uni = pl.DataFrame({
        "code": ["600519", "000001", "300750"], "name": ["贵州茅台", "平安银行", "宁德时代"],
        "exchange": ["SSE", "SZSE", "SZSE"], "board": ["main", "main", "chinext"],
        "list_date": [date(2001, 8, 27), date(1991, 4, 3), date(2018, 6, 11)], "delist_date": [None] * 3,
        "is_st": [False] * 3, "industry": [None] * 3, "status": [1, 1, 1],
    }, schema=universe.SCHEMA)
    uni.write_parquet(config.STOCK_LAB / "universe.parquet")
    assert [r["code"] for r in universe.search("gzmt")] == ["600519"]
    assert [r["code"] for r in universe.search("NDSD")] == ["300750"]
    assert [r["code"] for r in universe.search("pa")] == ["000001"]


# ---------------------------------------------------------------- 除权与昨收

def test_parse_dividend_and_ex_rights_price() -> None:
    assert history.parse_dividend({"fh_sh": "1.46", "FHcontent": "10派1.46元"}) == (Decimal("0.146"), Decimal(0))
    cash, shares = history.parse_dividend({"fh_sh": "39.74", "FHcontent": "10送8股, 10转12股, 10派39.74元"})
    assert (cash, shares) == (Decimal("3.974"), Decimal(2))
    assert history.parse_dividend({"fh_sh": "0", "FHcontent": ""}) is None          # 配股/特殊
    assert history.parse_dividend({"fh_sh": "0", "FHcontent": "10配3股"}) is None
    # 与 baostock/交易所核对过的真实例子
    assert history.ex_rights_price(337.00, cash, shares) == 111.01                     # 002594 2025-07-29
    assert history.ex_rights_price(1212.10, Decimal("28.0242"), Decimal(0)) == 1184.08  # 600519 2026-06-26
    assert history.ex_rights_price(21.83, Decimal("0.07"), Decimal("0.2")) == 18.13     # 300059 2023-04-18
    assert history.ex_rights_price(385.90, Decimal("2.52"), Decimal("0.8")) == 212.99   # 300750 2023-04-26
    assert history.ex_rights_price(91.10, Decimal("0.58002"), Decimal("0.4")) == 64.66  # 603259 2019-07-02


def _bar(day: str, o: float, c: float, h: float, lo: float, vol: float = 1000, info: dict | None = None,
         amount: float | None = None) -> list:
    amt = amount if amount is not None else round(vol * 100 * c / 10000, 2)
    return [day, f"{o:.2f}", f"{c:.2f}", f"{h:.2f}", f"{lo:.2f}", f"{vol:.2f}", info or {}, "1.50", f"{amt:.2f}",
            "0.00", "0.00"]


def test_compute_preclose_dividend_rights_and_ipo() -> None:
    bars = [
        _bar("2024-01-02", 10.0, 10.0, 10.2, 9.9),
        _bar("2024-01-03", 10.0, 10.5, 10.6, 9.9),
        _bar("2024-01-04", 10.0, 10.2, 10.3, 9.9, info={"fh_sh": "3", "FHcontent": "10转2股, 10派3元"}),
        _bar("2024-01-10", 10.1, 10.3, 10.4, 10.0),                              # 停牌后复牌
        _bar("2024-01-11", 8.9, 9.0, 9.1, 8.8, info={"fh_sh": "0", "FHcontent": ""}),   # 配股
    ]
    calls: list[int] = []

    def qfq_ref(i: int) -> float | None:
        calls.append(i)
        return None if i == 2 else 9.123       # 第2根模拟前复权取不到 → 用公式

    pre, review = history.compute_preclose(bars, qfq_ref)
    assert pre[0] is None                     # 第一根没有昨收（完整历史时就是上市首日）
    assert pre[1] == 10.0
    assert pre[2] == 8.5                      # (10.5-0.3)/1.2
    assert pre[3] == 10.2                     # 复牌：停牌前收盘
    assert pre[4] == 9.123 and calls == [2, 4] and review == [4]   # 送转与配股都先试前复权；配股需复核
    # 纯现金分红不请求前复权
    calls.clear()
    cash_bars = bars[:2] + [_bar("2024-01-04", 10.0, 10.2, 10.3, 9.9, info={"fh_sh": "3", "FHcontent": "10派3元"})]
    pre_cash, _ = history.compute_preclose(cash_bars, qfq_ref)
    assert pre_cash[2] == 10.2 and calls == []
    # need_from 之前的除权不处理、不请求前复权
    calls.clear()
    pre2, _ = history.compute_preclose(bars, qfq_ref, need_from=date(2024, 1, 5))
    assert pre2[2] == 10.5 and calls == [4]


def test_qfq_estimate_linear_segments() -> None:
    """前复权 = A·raw + B 的分段线性变换，估算出的参考价应还原交易所参考价"""
    raw = [
        _bar("2024-06-03", 20.0, 20.0, 20.3, 19.8),
        _bar("2024-06-04", 14.5, 14.8, 15.0, 14.2, info={"fh_sh": "2", "FHcontent": "10转4股, 10派2元"}),
        _bar("2024-06-05", 14.9, 15.3, 15.5, 14.7),
        _bar("2024-06-06", 15.3, 15.1, 15.6, 15.0),
    ]
    true_ref = 14.15            # 交易所按"虚拟"转增比例算出的参考价（不同于公式 (20-0.2)/1.4=14.14）
    a, b = 0.8, -0.5            # 之后的除权造成的前复权变换

    def q(x: float) -> str:
        return f"{a * x + b:.2f}"

    # 除权前一根：前复权 = 变换(参考价对应的价格)，即 q_prev = A·(raw_prev·ref/raw_prev) + B = A·ref + B
    qfq = [[raw[0][0], q(true_ref), q(true_ref), q(true_ref), q(true_ref)]]
    for bar in raw[1:]:
        qfq.append([bar[0]] + [q(float(bar[k])) for k in (1, 2, 3, 4)])
    est = history.qfq_estimate(raw, 1, qfq)
    assert est is not None and est[0] == pytest.approx(true_ref, abs=0.011) and est[1] == 0.03
    assert history.qfq_estimate(raw, 1, []) is None
    # 之后只有现金分红（q = raw - 累计派息）：斜率为 1，估算精确，能分辨 1 分钱的"虚拟"差异
    qfq_add = [[raw[0][0]] + [f"{true_ref - 0.2873:.2f}"] * 4]
    for bar in raw[1:]:
        qfq_add.append([bar[0]] + [f"{float(bar[k]) - 0.2873:.2f}" for k in (1, 2, 3, 4)])
    assert history.qfq_estimate(raw, 1, qfq_add) == (true_ref, 0.0)
    pre, _ = history.compute_preclose(raw, lambda i: history.qfq_estimate(raw, i, qfq_add))
    assert pre[1] == true_ref                    # 精确估算与公式 14.14 不同 → 采用估算
    pre_noisy, _ = history.compute_preclose(raw, lambda i: history.qfq_estimate(raw, i, qfq))
    assert pre_noisy[1] == 14.14                 # 估算误差 0.03 内无法分辨 → 用公式


def test_compute_preclose_consistency_repairs() -> None:
    # 300577 2024-11-28：10派1.46元，公式得 22.03 但收盘 26.45 超过 22.03 的 20% 涨停价 26.44 → 虚拟分派，调为 22.04
    bars = [
        _bar("2024-11-27", 21.36, 22.18, 22.19, 20.71),
        _bar("2024-11-28", 23.50, 26.45, 26.45, 22.81, info={"fh_sh": "1.46", "FHcontent": "10派1.46元"}),
    ]
    pre, _ = history.compute_preclose(bars, None, move_limit=lambda d: 0.20)
    assert pre[1] == 22.04
    pre_plain, _ = history.compute_preclose(bars, None)
    assert pre_plain[1] == 22.03
    # 002323 2022-01-06：没有除权信息但跌了 47%（重整转增）→ 用前复权估算 2.98
    bars2 = [_bar("2021-12-29", 5.17, 5.38, 5.38, 5.16), _bar("2022-01-06", 2.83, 2.83, 2.83, 2.83)]
    pre2, rev2 = history.compute_preclose(bars2, lambda i: 2.98, move_limit=lambda d: 0.10)
    assert pre2[1] == 2.98 and rev2 == [1]
    # 新股前几根（free_bars）大涨不触发；估算变化 <3% 也不采用
    bars3 = [_bar("2024-01-02", 10, 30, 30, 10), _bar("2024-01-03", 30, 60, 60, 30)]
    pre3, est3 = history.compute_preclose(bars3, lambda i: 30.1, move_limit=lambda d: 0.10, free_bars=5)
    assert pre3[1] == 30.0 and est3 == []
    pre4, est4 = history.compute_preclose(bars3, lambda i: 30.1, move_limit=lambda d: 0.10)
    assert pre4[1] == 30.0 and est4 == []


def test_bars_to_frame_units() -> None:
    bars = [
        _bar("2026-09-23", 28.0, 28.31, 28.5, 27.9, vol=400000),
        _bar("2026-09-24", 28.05, 28.40, 29.50, 27.80, vol=428580, amount=122264.15),
    ]
    df = history.bars_to_frame("001216", bars, [None, 28.31])
    assert df.schema == pl.Schema(history.PANEL_SCHEMA)
    row = df.row(1, named=True)
    assert (row["open"], row["close"], row["high"], row["low"]) == (28.05, 28.40, 29.50, 27.80)   # 开 收 高 低
    assert row["volume"] == 42858000                  # 手 → 股
    assert row["amount"] == 1222641500                # 万元 → 元
    assert row["turn"] == 1.5 and row["tradestatus"] == 1 and row["source"] == "tx"
    # 科创板：腾讯成交量本身就是股
    star = history.bars_to_frame("688981", [_bar("2026-09-24", 121.1, 119.98, 121.39, 119.63, vol=18660976,
                                                  amount=224634.85)], [121.39])
    assert star["volume"][0] == 18660976


def test_parse_quote_line() -> None:
    fields = [""] * 50
    fields[0], fields[1], fields[2] = "51", "华瓷股份", "001216"
    fields[3], fields[4], fields[5], fields[6] = "28.40", "28.31", "28.05", "428580"
    fields[30] = "20260924161430"
    fields[31], fields[32], fields[33], fields[34] = "0.09", "0.32", "29.50", "27.80"
    fields[35] = "28.40/428580/1222641504"
    fields[37], fields[38], fields[39] = "122264", "17.36", "35.2"
    fields[44], fields[45], fields[47], fields[48] = "20.1", "30.5", "31.14", "25.48"
    q = history._parse_quote_line('v_sz001216="' + "~".join(fields) + '";')
    assert q["code"] == "001216" and q["price"] == 28.40 and q["prev_close"] == 28.31
    assert q["volume"] == 42858000 and q["amount"] == 1222641504
    assert q["time"] == "2026-09-24 16:14:30" and q["limit_up"] == 31.14
    assert q["float_cap"] == pytest.approx(20.1e8)


# ---------------------------------------------------------------- 面板读写与增量

def _rows(code: str, days: list[date], start_price: float = 10.0, source: str = "tx") -> pl.DataFrame:
    closes = [start_price + i * 0.1 for i in range(len(days))]
    return pl.DataFrame({
        "date": days, "code": [code] * len(days),
        "open": closes, "high": closes, "low": closes, "close": closes,
        "preclose": [None] + closes[:-1],
        "volume": [1e6] * len(days), "amount": [1e7] * len(days), "turn": [1.0] * len(days),
        "tradestatus": [1] * len(days), "is_st": [False] * len(days), "source": [source] * len(days),
    }).cast(history.PANEL_SCHEMA)


def _weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_write_load_roundtrip(workspace) -> None:
    days = _weekdays(date(2019, 12, 26), 6)          # 跨年 → 两个年文件
    history._write_rows(pl.concat([_rows("600519", days), _rows("000001", days[2:])]))
    assert sorted(p.name for p in config.PANEL_DIR.glob("*.parquet")) == ["2019.parquet", "2020.parquet"]
    # 覆盖写 + 停牌行过滤
    suspended = _rows("000001", [days[-1]]).with_columns(pl.lit(0, dtype=pl.Int8).alias("tradestatus"))
    history._write_rows(suspended)
    df = history.load_panel()
    assert df.height == 6 + 4 - 1
    assert df["code"].to_list() == ["000001"] * 3 + ["600519"] * 6          # 按 code, date 排序
    assert df.filter(pl.col("code") == "600519")["date"].is_sorted()
    part = history.load_panel(start="2020-01-01", codes=["600519"], columns=["close"])
    assert part.columns == ["date", "code", "close"] and part["date"].min() >= date(2020, 1, 1)
    assert history.last_date() == days[-1]
    assert history.trade_dates() == days
    assert history.trade_dates(start=date(2020, 1, 1)) == [d for d in days if d.year == 2020]


def test_append_realtime_snapshot_does_not_override_tx(workspace) -> None:
    day = date(2024, 3, 1)
    history._write_rows(_rows("600519", [day]))
    snap = pl.DataFrame({
        "code": ["600519", "000001", "000002"], "price": [1.0, 11.0, 5.0], "prev_close": [1.0, 10.0, 5.0],
        "open": [1.0, 10.5, 0.0], "high": [1.0, 11.0, 0.0], "low": [1.0, 10.4, 0.0],
        "volume": [100.0, 5e6, 0.0], "amount": [100.0, 5e7, 0.0], "turnover": [1.0, 2.0, 0.0],
        "time": ["2024-03-01 16:00:00"] * 3,
    })
    n = history.append_realtime_snapshot(snap, day)
    assert n == 1                                      # 600519 已有腾讯K线不覆盖；000002 停牌跳过
    df = history.load_panel()
    assert df.filter(pl.col("code") == "000001").row(0, named=True)["source"] == "rt"
    assert df.filter(pl.col("code") == "600519")["close"][0] == 10.0
    # 未收盘的当天不写
    today = datetime.now(config.CHINA_TZ).date()
    if datetime.now(config.CHINA_TZ).hour < 15:
        assert history.append_realtime_snapshot(snap.with_columns(pl.lit(f"{today} 10:00:00").alias("time")),
                                                today) == 0


def test_update_history_full_incremental_and_resume(workspace, monkeypatch) -> None:
    cal = _weekdays(date(2024, 1, 2), 30)
    target = {"day": cal[19]}
    calls: list[tuple[str, date, int]] = []

    def fake_latest(calendar=None):
        closed = [d for d in cal if d <= target["day"]]
        return closed[-1], closed

    def fake_fetch(code: str, need_from: date, first_count: int = 2000, end: date | None = None):
        calls.append((code, need_from, first_count))
        days = [d for d in cal if need_from <= d <= (end or cal[-1])]
        if code == "000009":
            raise ConnectionError("模拟失败")
        return _rows(code, days), {"code": code, "first_date": cal[0], "complete": code == "301999",
                                   "approx_days": []}

    monkeypatch.setattr(history, "latest_closed_day", fake_latest)
    monkeypatch.setattr(history, "fetch_stock", fake_fetch)
    monkeypatch.setattr(history, "_rt_days", lambda: 99)        # 不走快照
    monkeypatch.setattr(history.time, "sleep", lambda s: None)
    res = history.update_history(codes=["600519", "301999", "000009"], start="2024-01-01", workers=2)
    assert res["updated"] == 2 and res["failed"] == ["000009"] and res["mode"] == "full"
    assert res["last_date"] == cal[19].isoformat()
    assert history.load_panel().height == 40
    assert universe.load_first_bars().filter(pl.col("complete"))["code"].to_list() == ["301999"]

    # 增量：往后推3个交易日，只补缺的部分
    calls.clear()
    target["day"] = cal[22]
    res2 = history.update_history(codes=["600519", "301999"], start="2024-01-01", workers=2)
    assert res2["mode"] == "kline"
    assert {c[1] for c in calls} == {cal[19] + timedelta(days=1)}
    assert all(c[2] == 3 + 6 for c in calls)
    assert history.load_panel().height == 46
    assert not config.PANEL_DIR.joinpath("_pending").exists()

    # 续传：模拟上次中断留下的分批文件，下次运行先合并
    history._save_pending([_rows("000010", cal[:23])], [{"code": "000010", "first_date": cal[0],
                                                         "complete": True}], "t")
    calls.clear()
    res3 = history.update_history(codes=["600519", "301999", "000010"], start="2024-01-01", workers=2)
    assert calls == [] and res3["updated"] == 0
    assert history.load_panel(codes=["000010"]).height == 23


def test_update_history_snapshot_path(workspace, monkeypatch) -> None:
    cal = _weekdays(date(2024, 1, 2), 10)
    history._write_rows(pl.concat([_rows("600519", cal[:9]), _rows("000001", cal[:9])]))
    monkeypatch.setattr(history, "latest_closed_day", lambda calendar=None: (cal[9], cal))

    def fake_snapshot(codes: list[str]) -> pl.DataFrame:
        return pl.DataFrame({
            "code": codes, "price": [12.0] * len(codes), "prev_close": [10.8] * len(codes),
            "open": [11.0] * len(codes), "high": [12.0] * len(codes), "low": [10.9] * len(codes),
            "volume": [1e6] * len(codes), "amount": [1e7] * len(codes), "turnover": [1.0] * len(codes),
            "time": [f"{cal[9]} 16:00:00"] * len(codes),
        })

    monkeypatch.setattr(history, "_fetch_snapshot", fake_snapshot)
    monkeypatch.setattr(history, "fetch_stock", lambda *a, **k: pytest.fail("不应逐只下载"))
    res = history.update_history(codes=["600519", "000001"], start="2024-01-01")
    assert res["mode"] == "snapshot" and res["rows"] == 2
    last = history.load_panel(start=cal[9])
    assert last["source"].to_list() == ["rt", "rt"] and last["preclose"].to_list() == [10.8, 10.8]


def test_fetch_stock_with_fake_kline(monkeypatch) -> None:
    raw = [_bar(f"2018-12-{d:02d}", 10, 10, 10.1, 9.9) for d in (27, 28)] + [
        _bar("2019-01-02", 10, 11, 11, 10),
        _bar("2019-01-03", 11, 10.5, 11, 10.4, info={"fh_sh": "5", "FHcontent": "10派5元"}),
    ]
    qfq_rows: list[list] = []
    requests: list[tuple[str, int, str]] = []

    def fake_request(sym: str, end: str, count: int, adjust: str = "") -> list[list]:
        assert sym == "sz001216"
        requests.append((end, count, adjust))
        if adjust == "qfq":
            return qfq_rows
        return raw[-count:]

    monkeypatch.setattr(history, "_request", fake_request)
    df, first = history.fetch_stock("001216", date(2019, 1, 1), 2000)
    assert first["complete"] and first["first_date"] == date(2018, 12, 27)
    assert df["date"].to_list() == [date(2019, 1, 2), date(2019, 1, 3)]
    assert df["preclose"].to_list() == [10.0, 10.5]            # 前复权取不到 → 公式 11-0.5
    assert ("2019-01-03", 2, "qfq") in requests                 # 增量（新K线少）时现金分红也查前复权
    # 前复权给出交易所"虚拟分派"后的参考价 10.51（最新一段 q = raw）
    qfq_rows.extend([["2019-01-02", "10.51", "10.51", "10.51", "10.51"],
                     ["2019-01-03", "11.00", "10.50", "11.00", "10.40"]])
    df_q, _ = history.fetch_stock("001216", date(2019, 1, 1), 2000)
    assert df_q["preclose"].to_list() == [10.0, 10.51]
    # 分页：每页2根，直到拿到 need_from 之前的一根
    df2, first2 = history.fetch_stock("001216", date(2019, 1, 3), 2)
    assert df2["date"].to_list() == [date(2019, 1, 3)] and not first2["complete"]


# ---------------------------------------------------------------- fundamentals

def test_fundamentals_parse_and_avail_date() -> None:
    assert fundamentals.parse_value("268.47亿") == pytest.approx(268.47e8)
    assert fundamentals.parse_value("5442.58万") == pytest.approx(5442.58e4)
    assert fundamentals.parse_value("1.2万亿") == pytest.approx(1.2e12)
    assert fundamentals.parse_value("-4.53%") == pytest.approx(-4.53)
    assert fundamentals.parse_value("21.3800") == pytest.approx(21.38)
    assert fundamentals.parse_value("False") is None
    assert fundamentals.parse_value("--") is None
    assert fundamentals.parse_value(False) is None
    assert fundamentals.avail_date_of(date(2025, 3, 31)) == date(2025, 5, 1)
    assert fundamentals.avail_date_of(date(2025, 6, 30)) == date(2025, 9, 1)
    assert fundamentals.avail_date_of(date(2025, 9, 30)) == date(2025, 11, 1)
    assert fundamentals.avail_date_of(date(2025, 12, 31)) == date(2026, 5, 1)


def test_fundamentals_ttm_and_asof_join() -> None:
    rds = [date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31), date(2025, 9, 30)]
    fund = pl.DataFrame({
        "code": ["600519"] * 4, "report_date": rds,
        "avail_date": [fundamentals.avail_date_of(d) for d in rds],
        "eps": [3.0, 4.0, 1.2, 3.3], "net_profit": [30.0, 40.0, 12.0, 33.0], "revenue": [300.0, 400.0, 110.0, 330.0],
    })
    for col, dtype in fundamentals.SCHEMA.items():
        if col not in fund.columns:
            fund = fund.with_columns(pl.lit(None, dtype=dtype).alias(col))
    fund = fundamentals.add_ttm(fund.select(list(fundamentals.SCHEMA)))
    ttm = dict(zip(fund["report_date"], fund["eps_ttm"], strict=True))
    assert ttm[date(2024, 12, 31)] == 4.0
    assert ttm[date(2024, 9, 30)] is None                     # 缺上一年年报
    assert ttm[date(2025, 9, 30)] == pytest.approx(3.3 + 4.0 - 3.0)

    panel = pl.DataFrame({"code": ["600519"] * 4 + ["000001"],
                          "date": [date(2025, 4, 30), date(2025, 5, 1), date(2025, 10, 31), date(2025, 11, 1),
                                   date(2025, 11, 1)]})
    out = fundamentals.asof_join(panel, fund)
    assert out["code"].to_list() == panel["code"].to_list()   # 保持原顺序
    # 4/30 当天年报与一季报都还不能用 → 仍是 2024 三季报；5/1 起取报告期最新的一季报
    assert out["report_date"].to_list() == [date(2024, 9, 30), date(2025, 3, 31), date(2025, 3, 31),
                                            date(2025, 9, 30), None]


def test_bj_recent_ex_rights_from_qfq(monkeypatch) -> None:
    """北交所K线没有分红信息：最近一次除权日的参考价 = 前一根的前复权收盘价"""
    raw = [_bar("2026-09-21", 35.0, 35.2, 35.5, 34.8), _bar("2026-09-22", 35.2, 35.0, 35.4, 34.9),
           _bar("2026-09-23", 35.0, 35.28, 35.6, 34.9), _bar("2026-09-24", 34.5, 33.56, 34.7, 33.4)]
    qfq = [[b[0], b[1], f"{float(b[2]) - 0.62:.3f}", b[3], b[4]] for b in raw[:3]] + [raw[3][:5]]

    monkeypatch.setattr(history, "_request", lambda sym, end, count, adjust="": qfq if adjust == "qfq" else raw)
    pre: list = [None, 35.2, 35.0, 35.28]
    history._bj_recent_ex_rights("bj920165", raw, pre, date(2026, 9, 1))
    assert pre == [None, 35.2, 35.0, 34.66]
    pre2: list = [None, 35.2, 35.0, 35.28]
    history._bj_recent_ex_rights("bj920165", raw, pre2, date(2026, 9, 25))     # 只改 need_from 之后的
    assert pre2[3] == 35.28


def test_update_history_remembers_empty_codes(workspace, monkeypatch) -> None:
    """下载后一根新K线都没有的股票：5 个交易日内不再下载（force 除外），之后再查；有了新K线就忘掉"""
    cal = _weekdays(date(2024, 1, 2), 40)
    target = {"day": cal[19]}
    calls: list[str] = []
    empty_codes: set[str] = {"600888", "000999"}

    def fake_latest(calendar=None):
        closed = [d for d in cal if d <= target["day"]]
        return closed[-1], closed

    def fake_fetch(code: str, need_from: date, first_count: int = 2000, end: date | None = None):
        calls.append(code)
        days = [] if code in empty_codes else [d for d in cal if need_from <= d <= (end or cal[-1])]
        df = _rows(code, days) if days else pl.DataFrame(schema=history.PANEL_SCHEMA)
        return df, {"code": code, "first_date": days[0] if days else None, "complete": True, "approx_days": []}

    monkeypatch.setattr(history, "latest_closed_day", fake_latest)
    monkeypatch.setattr(history, "fetch_stock", fake_fetch)
    monkeypatch.setattr(history, "_rt_days", lambda: 99)
    # 000999 面板里已有旧K线（长期停牌，增量任务）；600888 面板里没有（全量任务）
    history._write_rows(_rows("000999", cal[:5]))
    codes = ["600519", "600888", "000999"]
    res = history.update_history(codes=codes, start="2024-01-01", workers=2)
    assert sorted(calls) == sorted(codes) and res["skipped_empty"] == 0
    assert history.load_empty_fetch() == {"600888": cal[19], "000999": cal[19]}

    calls.clear()
    target["day"] = cal[23]                        # 4 个交易日后：还在"冷却"中
    res = history.update_history(codes=codes, start="2024-01-01", workers=2)
    assert calls == ["600519"] and res["skipped_empty"] == 2
    calls.clear()
    res = history.update_history(codes=codes, start="2024-01-01", workers=2, force=True)
    assert sorted(calls) == ["000999", "600888"] and res["skipped_empty"] == 0       # 600519 已是最新
    assert history.load_empty_fetch()["600888"] == cal[23]

    calls.clear()
    target["day"] = cal[28]                        # 满 5 个交易日：再查一次；000999 复牌有了新K线 → 从记录里去掉
    empty_codes.discard("000999")
    res = history.update_history(codes=codes, start="2024-01-01", workers=2)
    assert sorted(calls) == sorted(codes) and res["skipped_empty"] == 0
    assert history.load_empty_fetch() == {"600888": cal[28]}
    assert history.load_panel(codes=["000999"])["date"].max() == cal[28]      # 没丢数据：从最后一根之后补齐
    # 记录文件损坏：当作没有记录
    config.STOCK_LAB.joinpath(history.EMPTY_FILE_NAME).write_text("{bad", encoding="utf-8")
    assert history.load_empty_fetch() == {}
