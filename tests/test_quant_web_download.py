"""全市场下载相关：东方财富批量财报、同花顺限流停止、分批续传的 baostock 复核、除权日昨收校准与涨停价取舍"""
from datetime import date, timedelta

import polars as pl
import pytest

from quant_web import config
from quant_web.market import fundamentals, history


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


def _bar(day: str, o: float, c: float, h: float, lo: float, info: dict | None = None) -> list:
    return [day, f"{o:.2f}", f"{c:.2f}", f"{h:.2f}", f"{lo:.2f}", "1000.00", info or {}, "1.50", "100.00",
            "0.00", "0.00"]


# ---------------------------------------------------------------- 东方财富批量财报

def _fake_em(pages: dict[str, list[list[dict]]]):
    """按报表名返回分页数据；没有的报表返回 9201（数据为空）"""
    calls: list[tuple[str, str]] = []

    def get_json(url: str, params: dict | None = None, **kw):
        report, page = params["reportName"], int(params["pageNumber"])
        calls.append((report, params["filter"]))
        data = pages.get(report)
        if not data:
            return {"result": None, "success": False, "message": "返回数据为空", "code": 9201}
        return {"result": {"pages": len(data), "count": sum(len(p) for p in data), "data": data[page - 1]},
                "success": True}

    return get_json, calls


def test_fetch_period_merges_reports_and_pages(monkeypatch) -> None:
    cpd = [
        [{"SECURITY_CODE": "600519", "BASIC_EPS": 65.66, "BPS": 195.35, "WEIGHTAVG_ROE": 32.53,
          "TOTAL_OPERATE_INCOME": 172054171890.91, "YSTZ": -1.2, "PARENT_NETPROFIT": 82320067101.68,
          "SJLTZ": -4.53, "XSMLL": 91.18, "MGJYXJJE": 49.13, "NOTICE_DATE": "2026-04-17 00:00:00"}],
        [{"SECURITY_CODE": "000001", "BASIC_EPS": 2.07, "BPS": 23.25, "WEIGHTAVG_ROE": 9.15,
          "TOTAL_OPERATE_INCOME": 131442000000, "YSTZ": -10.4, "PARENT_NETPROFIT": 42633000000, "SJLTZ": -4.2,
          "XSMLL": None, "MGJYXJJE": 16.28, "NOTICE_DATE": "2026-03-21 00:00:00"},
         {"SECURITY_CODE": "900901", "BASIC_EPS": 1.0}],           # B 股：丢弃
    ]
    bal = [[{"SECURITY_CODE": "600519", "DEBT_ASSET_RATIO": 15.19}, {"SECURITY_CODE": "000001", "DEBT_ASSET_RATIO": 90.9}]]
    get_json, calls = _fake_em({"RPT_LICO_FN_CPD": cpd, "RPT_DMSK_FN_BALANCE": bal})     # 利润表：9201 空
    monkeypatch.setattr(fundamentals.net, "get_json", get_json)
    df = fundamentals.fetch_period(date(2025, 12, 31)).sort("code")
    assert df.columns == list(fundamentals.SCHEMA)
    assert df["code"].to_list() == ["000001", "600519"]
    row = df.row(1, named=True)
    assert row["eps"] == 65.66 and row["debt_ratio"] == 15.19 and row["net_profit_deducted"] is None
    assert row["report_date"] == date(2025, 12, 31) and row["avail_date"] == date(2026, 5, 1)
    assert row["notice_date"] == date(2026, 4, 17) and row["source"] == "em"
    assert row["net_margin"] == pytest.approx(82320067101.68 / 172054171890.91 * 100)
    assert df.row(0, named=True)["gross_margin"] is None
    assert sum(1 for r, _ in calls if r == "RPT_LICO_FN_CPD") == 2                     # 翻了两页
    assert all("REPORTDATE='2025-12-31'" in f or "REPORT_DATE='2025-12-31'" in f for _, f in calls)


def test_report_periods() -> None:
    periods = fundamentals.report_periods(date(2026, 9, 25))
    assert periods[0] == date(int(config.HISTORY_START[:4]) - 2, 3, 31)
    assert periods[-1] == date(2026, 6, 30) and len(periods) == 38


def _period_frame(period: date, codes: list[str], eps: float) -> pl.DataFrame:
    df = pl.DataFrame({"code": codes}).with_columns(
        pl.lit(period).alias("report_date"), pl.lit(fundamentals.avail_date_of(period)).alias("avail_date"),
        pl.lit(eps).alias("eps"), pl.lit(eps * 10).alias("net_profit"), pl.lit(eps * 100).alias("revenue"),
        pl.lit("em").alias("source"), pl.lit(date(2026, 9, 25)).alias("fetched_at"),
    )
    for col, dtype in fundamentals.SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    return df.select([pl.col(c).cast(t) for c, t in fundamentals.SCHEMA.items()])


def test_update_fundamentals_incremental_ttm_and_fallback(workspace, monkeypatch) -> None:
    periods = [date(2024, 9, 30), date(2024, 12, 31), date(2025, 9, 30)]
    monkeypatch.setattr(fundamentals, "report_periods", lambda today=None: periods)
    fetched: list[date] = []

    def fake_period(p: date) -> pl.DataFrame:
        fetched.append(p)
        return _period_frame(p, ["600519", "000001"], {2024: 3.0, 2025: 3.3}[p.year] + (p.month == 12))

    monkeypatch.setattr(fundamentals, "fetch_period", fake_period)
    res = fundamentals.update_fundamentals()
    assert res["periods"] == 3 and res["updated"] == 2 and res["source"] == "em" and sorted(fetched) == periods
    fd = fundamentals.load_fundamentals()
    assert fd.height == 6 and fd.filter(pl.col("code") == "600519")["eps_ttm"].to_list() == [None, 4.0, 3.3 + 4.0 - 3.0]

    # 再次运行：早已过披露期的报告期不重下；fetched_at 太早的"披露期内"报告期（满 max_age_days）才重下
    fetched.clear()
    monkeypatch.setattr(fundamentals, "datetime", _FixedNow(date(2025, 11, 20)))
    stale = fd.with_columns(
        pl.when(pl.col("report_date") == date(2025, 9, 30)).then(pl.lit(date(2025, 11, 2)))
        .otherwise(pl.lit(date(2025, 11, 2))).alias("fetched_at"))
    fundamentals._save(stale)
    res2 = fundamentals.update_fundamentals()
    assert fetched == [date(2025, 9, 30)] and res2["skipped"] == 2

    # codes 显式给出：全部重下，但只合并这些股票
    fetched.clear()
    res3 = fundamentals.update_fundamentals(codes=["000001"])
    assert sorted(fetched) == periods and res3["updated"] == 1

    # 东方财富整体不可用 → 退回同花顺逐只
    def boom(p: date) -> pl.DataFrame:
        raise ConnectionError("down")

    monkeypatch.setattr(fundamentals, "fetch_period", boom)
    monkeypatch.setattr(fundamentals.time, "sleep", lambda s: None)
    monkeypatch.setattr(fundamentals, "_update_ths", lambda *a, **k: {"updated": 5, "failed": [], "blocked": False})
    res4 = fundamentals.update_fundamentals(max_age_days=0)
    assert res4["source"] == "ths" and res4["updated"] == 5


class _FixedNow:
    """替换模块里的 datetime：now() 返回固定日期，其余照常"""

    def __init__(self, day: date) -> None:
        self.day = day

    def now(self, tz=None):
        from datetime import datetime

        return datetime(self.day.year, self.day.month, self.day.day, 20, 0, tzinfo=tz)

    def strptime(self, *a):
        from datetime import datetime

        return datetime.strptime(*a)


def test_update_ths_stops_when_blocked(workspace, monkeypatch) -> None:
    monkeypatch.setattr(fundamentals, "MIN_INTERVAL", 0.0)
    seen: list[str] = []

    def fake_one(code: str) -> pl.DataFrame:
        seen.append(code)
        if int(code) >= 3:
            raise fundamentals.Blocked("403")
        return _period_frame(date(2025, 12, 31), [code], 1.0)

    monkeypatch.setattr(fundamentals, "fetch_one", fake_one)
    codes = [f"{i:06d}" for i in range(1, 40)]
    res = fundamentals._update_ths(codes=codes, workers=1)
    assert res["blocked"] and res["updated"] == 2
    assert len(seen) == 2 + fundamentals.BLOCK_ABORT          # 连续 3 次 403 后不再请求
    assert len(res["failed"]) == len(codes) - 2
    assert fundamentals.load_fundamentals()["code"].to_list() == ["000001", "000002"]


# ---------------------------------------------------------------- 昨收：涨停价取舍、复核、校准

def test_compute_preclose_prefers_candidate_that_hits_limit() -> None:
    """603214 2024-10-08：公式 15.41（交易所值），腾讯前复权估算 15.42；收盘 16.95 = 15.41 的涨停价"""
    bars = [
        _bar("2024-09-27", 15.0, 15.52, 15.6, 14.9),
        _bar("2024-10-08", 16.95, 16.95, 16.95, 16.2, info={"fh_sh": "1.09", "FHcontent": "10派1.09元"}),
    ]
    pre, review = history.compute_preclose(bars, lambda i: (15.42, 0.0), move_limit=lambda d: 0.10,
                                           review_near=True)
    assert pre[1] == 15.41 and review == [1]
    pre2, review2 = history.compute_preclose(bars, lambda i: (15.42, 0.0), move_limit=lambda d: 0.10)
    assert pre2[1] == 15.41 and review2 == []
    # 不贴近涨跌停：仍按原规则采用前复权精确值（库存股"虚拟分派"），也不需要复核
    calm = bars[:1] + [_bar("2024-10-08", 15.5, 15.6, 15.7, 15.3, info={"fh_sh": "1.09", "FHcontent": "10派1.09元"})]
    pre3, review3 = history.compute_preclose(calm, lambda i: (15.42, 0.0), move_limit=lambda d: 0.10,
                                             precise_from=0, review_near=True)
    assert pre3[1] == 15.42 and review3 == []


def _rows(code: str, days: list[date], closes: list[float], precloses: list[float | None]) -> pl.DataFrame:
    return pl.DataFrame({
        "date": days, "code": [code] * len(days), "open": closes, "high": closes, "low": closes, "close": closes,
        "preclose": precloses, "volume": [1e6] * len(days), "amount": [1e7] * len(days), "turn": [1.0] * len(days),
        "tradestatus": [1] * len(days), "is_st": [False] * len(days), "source": ["tx"] * len(days),
    }).cast(history.PANEL_SCHEMA)


def test_pending_approx_days_survive_interruption(workspace, monkeypatch) -> None:
    days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
    history._save_pending([_rows("600519", days, [10.0, 9.8, 9.9], [None, 9.7, 9.8])], [], "t",
                          [("600519", days[1]), ("830799", days[1])])
    asked: list[list[tuple[str, date]]] = []

    def fake_bs(items, timeout=None, report=None, processed=None):
        asked.append(list(items))
        if processed is not None:
            processed.extend(items)
        return {("600519", days[1]): 9.71}

    monkeypatch.setattr(history, "_baostock_preclose", fake_bs)
    assert history._merge_pending() == 3                  # 模拟"下次运行先合并上次中断的部分"
    assert asked == [[("600519", days[1]), ("830799", days[1])]]
    assert history.load_panel()["preclose"].to_list() == [None, 9.71, 9.8]
    checked = history.load_checked_preclose()
    assert checked.height == 2 and checked.filter(pl.col("code") == "600519")["bs_preclose"].item() == 9.71
    assert not history._pending_dir().exists()


def test_calibrate_ex_rights_resume_and_replay(workspace, monkeypatch) -> None:
    d = [date(2024, 6, 3) + timedelta(days=i) for i in range(4)]
    # 600519 在 d[2] 除息（昨收 ≠ 上一根收盘），000001 在 d[1]、d[3] 除息；北交所不查
    history._write_rows(pl.concat([
        _rows("600519", d, [10.0, 10.1, 9.9, 10.0], [None, 10.0, 9.8, 9.9]),
        _rows("000001", d, [5.0, 4.9, 5.0, 4.8], [None, 4.8, 4.9, 4.7]),
        _rows("920002", d, [8.0, 7.9, 8.0, 8.1], [None, 7.8, 7.9, 8.0]),
    ]))
    truth = {("600519", d[2]): 9.81, ("000001", d[1]): 4.8, ("000001", d[3]): 4.71}
    asked: list[tuple[str, date]] = []
    state = {"calls": 0, "down_after": 1}

    def fake_bs(items, timeout=None, report=None, processed=None):
        state["calls"] += 1
        if state["calls"] > state["down_after"]:        # 模拟 baostock 挂了：一个都查不到
            return {}
        items = items[:2]                                  # 模拟中途断线：只查完前两个
        asked.extend(items)
        processed.extend(items)
        return {k: truth[k] for k in items}

    monkeypatch.setattr(history, "_baostock_preclose", fake_bs)
    res = history.calibrate_ex_rights(batch=10)
    assert res["events"] == 3 and asked == [("000001", d[3]), ("600519", d[2])]    # 新的先查
    assert res["changed"] == 2 and res["remaining"] == 1 and state["calls"] == 2    # 断线的重排队，但服务挂了就停
    state.update(calls=0, down_after=99)
    res2 = history.calibrate_ex_rights(batch=10)            # 续跑：只查剩下的
    assert asked[2:] == [("000001", d[1])] and res2["remaining"] == 0 and res2["changed"] == 0
    # 同一次运行里：断线没查到的排到队尾，换新连接继续
    asked.clear()
    history._checked_file().unlink()
    state.update(calls=0, down_after=99)
    res_q = history.calibrate_ex_rights(batch=10)
    assert res_q["remaining"] == 0 and state["calls"] == 2 and len(asked) == 3
    panel = history.load_panel().filter(pl.col("code") != "920002")
    assert panel.filter((pl.col("code") == "600519") & (pl.col("date") == d[2]))["preclose"].item() == 9.81

    # 面板重建（值被覆盖回旧的）后：离线重放已核对的值，不再联网
    history._write_rows(_rows("600519", d, [10.0, 10.1, 9.9, 10.0], [None, 10.0, 9.8, 9.9]))
    asked.clear()
    res3 = history.calibrate_ex_rights()
    assert asked == [] and res3["reapplied"] == 1
    assert history.load_panel(codes=["600519"])["preclose"].to_list()[2] == 9.81


def test_checked_skips_recent_not_found(workspace) -> None:
    """baostock 当晚才更新：最近几天查不到的不记为"已核对"，下次还会再查"""
    today = history.china_now().date()
    old_day, new_day = today - timedelta(days=30), today - timedelta(days=1)
    history._save_checked([("600519", old_day), ("000001", old_day), ("600036", new_day)],
                          {("600519", old_day): 1400.0})
    checked = history.load_checked_preclose().sort("code")
    assert checked["code"].to_list() == ["000001", "600519"]
    assert checked["bs_preclose"].to_list() == [None, 1400.0]


def test_guarded_socket_raises_on_disconnect() -> None:
    """baostock 连接被服务器关闭时 recv 返回空：包装后抛异常，而不是让 baostock 无限循环"""
    from quant_web.market import universe

    class FakeSock:
        def __init__(self) -> None:
            self.timeout: float | None = None
            self.chunks = [b"abc", b""]

        def settimeout(self, t: float) -> None:
            self.timeout = t

        def recv(self, n: int) -> bytes:
            return self.chunks.pop(0)

        def close(self) -> None:
            self.closed = True

    raw = FakeSock()
    guarded = universe._GuardedSocket(raw, 20.0)
    assert raw.timeout == 20.0 and guarded.recv(10) == b"abc"
    with pytest.raises(ConnectionError):
        guarded.recv(10)
    guarded.close()
    assert raw.closed
