"""quant_web 实盘前的安全检查（2026-09-25 全面审计后补的回归测试，全部离线）：
换仓买单在盘中撮合也不透支、排队委托过期、回测缓存键含资金、批量排雷和单只诊断同一口径、止损下限、
量化选股的下单清单（流动性 / 候补 / 停牌）和下单前自检。"""
import json
import warnings
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from quant_web import config
from quant_web.trading import engine, ledger
from quant_web.trading.engine import OrderRequest


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    return tmp_path


def _quote(px: float) -> dict:
    return {"price": px, "bid1": px, "ask1": px, "bid1_vol": 10_000, "ask1_vol": 10_000}


def test_credit_buy_waits_for_sale_proceeds_in_intraday_matching(ws: Path) -> None:
    """先卖后买的换仓买单（credit）：盘中实时撮合时，卖出还没成交就不能先买（以前只有日终撮合检查，盘中会透支）"""
    day = date(2026, 9, 28)
    with ledger.connect() as conn:
        aid = ledger.create_account("换仓", "paper", "paper", 100_000)["id"]
        engine.place(conn, aid, OrderRequest("600001", "buy", 9000, "market"), ref_price=10.0, trade_date=date(2026, 9, 25))
        engine.match_with_quotes(conn, aid, date(2026, 9, 25), {"600001": _quote(10.0)})
        engine.settle_day(conn, aid, date(2026, 9, 25), {"600001": 10.0})
        cash0 = ledger.one(conn, "SELECT cash FROM accounts WHERE id=?", (aid,))["cash"]
        assert cash0 < 20_000
        engine.place(conn, aid, OrderRequest("600001", "sell", 9000, "market"), trade_date=day)
        buy = engine.place(conn, aid, OrderRequest("600002", "buy", 9000, "market"), ref_price=10.0, trade_date=day, credit=95_000)
        # 卖出这一侧没有行情（停牌 / 跌停卖不出）：买单不能成交
        engine.match_with_quotes(conn, aid, day, {"600002": _quote(10.0)})
        assert ledger.one(conn, "SELECT status FROM orders WHERE id=?", (buy["id"],))["status"] == "submitted"
        assert ledger.one(conn, "SELECT cash FROM accounts WHERE id=?", (aid,))["cash"] >= 0
        # 卖出成交、钱回笼之后再撮合：买进来
        engine.match_with_quotes(conn, aid, day, {"600001": _quote(10.0), "600002": _quote(10.0)})
        assert ledger.one(conn, "SELECT status FROM orders WHERE id=?", (buy["id"],))["status"] == "filled"
        assert ledger.one(conn, "SELECT cash FROM accounts WHERE id=?", (aid,))["cash"] >= 0


def test_queued_orders_expire_at_end_of_their_trade_day(ws: Path) -> None:
    with ledger.connect() as conn:
        aid = ledger.create_account("实盘", "live", "manual", 100_000)["id"]
        q = engine.place(conn, aid, OrderRequest("600001", "buy", 100, "limit", 10.0), status="queued", trade_date=date(2026, 9, 24))
        later = engine.place(conn, aid, OrderRequest("600001", "buy", 100, "limit", 10.0), status="queued", trade_date=date(2026, 9, 28))
        engine.settle_day(conn, aid, date(2026, 9, 24), {})
        assert ledger.one(conn, "SELECT status FROM orders WHERE id=?", (q["id"],))["status"] == "expired"
        assert ledger.one(conn, "SELECT status FROM orders WHERE id=?", (later["id"],))["status"] == "queued"
        assert ledger.one(conn, "SELECT frozen FROM accounts WHERE id=?", (aid,))["frozen"] > 0      # 只释放过期那笔


def test_strategy_backtest_key_changes_with_capital_and_risk() -> None:
    from quant_web.strategy import backtest as SB
    from quant_web.strategy import templates as T
    spec = T.resolve("reversal_value")
    s1 = {"profile": {"risk_per_trade": 0.01}, "risk": {"max_single_pct": 0.2}}
    s2 = {"profile": {"risk_per_trade": 0.02}, "risk": {"max_single_pct": 0.2}}
    k = SB.key_of(spec, ["main"], "", 100_000, s1)
    assert k != SB.key_of(spec, ["main"], "", 5_000_000, s1) and k != SB.key_of(spec, ["main"], "", 100_000, s2)
    assert k == SB.key_of(spec, ["main"], "", 100_000, s1)


def test_scan_many_marks_loss_forecasts_red_like_single_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_web.analysis import riskscan
    today = date(2026, 9, 24)
    fc = pl.DataFrame({"code": ["600001", "600002"], "notice_date": [date(2026, 7, 15)] * 2, "kind": ["首亏", "预减"]})
    monkeypatch.setattr(riskscan, "_ext", lambda name: fc if name == "forecast" else pl.DataFrame())
    df = pl.DataFrame({"code": ["600001", "600002", "600003"], "name": ["甲", "乙", "丙"], "raw_close": [10.0] * 3, "amt20": [1e9] * 3})
    out = {r["code"]: r for r in riskscan.scan_many(df, today).to_dicts()}
    assert out["600001"]["risk"] == "red" and "业绩预告亏损" in out["600001"]["risk_reasons"]
    assert out["600002"]["risk"] == "yellow" and out["600003"]["risk"] == "green"
    # 亏损这类是"软"红灯（只提示）；退市 / ST 这类是硬伤
    soft = riskscan.item("loss", "连续亏损", "red", "x")
    hard = riskscan.item("st", "ST 风险警示", "red", "x")
    assert soft["level_text"] == "风险提示" and hard["level_text"] == "建议回避"
    assert riskscan.hard_red([soft, hard]) == [hard]


def test_suggest_stop_respects_small_configured_stop() -> None:
    from quant_web.trading import sizing
    st = sizing.suggest_stop(10.0, atr=None, ma20=None, max_pct=0.01)
    assert st["stop"] == pytest.approx(9.9) and st["pct"] == pytest.approx(0.01, abs=1e-6)


def test_swing_holdout_matches_generic_backtest() -> None:
    from quant_web.predict import backtest, swing
    assert swing.HOLDOUT_START == backtest.HOLDOUT_START


def test_stage_advice_is_descriptive_not_prescriptive() -> None:
    from quant_web.analysis import stage
    assert stage.STAGES["markup"]["tone"] != "good" and "跑输" in stage.STAGES["markup"]["advice"]
    assert "不要抄底" not in stage.STAGES["decline"]["advice"]


# ---------------------------------------------------------------- 量化选股：下单清单与下单前自检

@pytest.fixture()
def mf_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from quant_web.api import server
    from quant_web.multifactor import service
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("QUANT_WEB_NO_SCHEDULER", "1")
    d = tmp_path / "multifactor"
    d.mkdir(parents=True)
    for name in ("DIR", "TODAY_FILE", "REPORT_FILE", "TRACK_FILE"):
        monkeypatch.setattr(service, name, d if name == "DIR" else d / {"TODAY_FILE": "today.json", "REPORT_FILE": "report.json",
                                                                        "TRACK_FILE": "track.json"}[name])
    server.CACHE.clear()
    with TestClient(server.create_app(background=False)) as c:
        yield c, d


def test_mf_plan_flags_liquidity_backups_and_halted_sells(mf_client, monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_web.market import history
    c, d = mf_client
    rows = [{"code": "600001", "name": "甲", "close": 10.0, "amount20": 1e8, "rank": 1, "industry": "A", "limit_up": True},
            {"code": "600002", "name": "乙", "close": 10.0, "amount20": 2e5, "rank": 2, "industry": "B"},
            {"code": "600004", "name": "丁", "close": 8.0, "amount20": 1e8, "rank": 5, "industry": "C"}]
    today = {"date": "2026-09-24", "next_trade_day": "2026-09-28", "rebalance_day": True, "capital": 20_000, "rows": rows,
             "target": ["600001", "600002"], "prev_target": ["600001", "600003"]}
    (d / "today.json").write_text(json.dumps(today, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(history, "load_panel", lambda **k: pl.DataFrame({"code": [], "date": [], "close": []},
                                                                      schema={"code": pl.Utf8, "date": pl.Date, "close": pl.Float64}))
    plan = c.get("/api/mf/plan", params={"capital": 200_000}).json()
    items = {x["code"]: x for x in plan["items"]}
    assert items["600001"]["limit_up"] and not items["600001"]["too_big"]
    assert items["600002"]["too_big"]                                     # 每只 10 万 > 日均成交 20 万 × 5%
    assert [b["code"] for b in plan["backups"]] == ["600004"] and plan["backups"][0]["shares"] == 12500
    assert plan["sells"] == [{"code": "600003", "name": "", "action": "卖出", "halted": True}]      # 最新一天没有成交 = 停牌
    assert any("重新打分" in n for n in plan["notes"]) and any("每天成交额" in n for n in plan["notes"])


def test_mf_health_flags_stale_data_partial_download_st_and_halts(monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_web.market import history, universe
    from quant_web.multifactor import service
    from quant_web.trading import calendar as tcal
    tcal.set_calendar([date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 28)])
    try:
        monkeypatch.setattr(tcal, "china_now", lambda: datetime(2026, 9, 28, 20, 0, tzinfo=config.CHINA_TZ))
        monkeypatch.setattr(history, "last_date", lambda: date(2026, 9, 24))
        bars = pl.DataFrame({"code": ["600001", "600002", "600001"], "date": [date(2026, 9, 23), date(2026, 9, 23), date(2026, 9, 24)],
                             "close": [1.0, 1.0, 1.0]})
        monkeypatch.setattr(history, "load_panel", lambda **k: bars)
        monkeypatch.setattr(universe, "load_universe", lambda: pl.DataFrame({"code": ["600001", "600002"], "name": ["甲", "*ST乙"]}))
        today = {"date": "2026-09-24", "generated_at": "2026-09-24 16:00", "target": ["600001", "600002"],
                 "rows": [{"code": "600001", "name": "甲"}, {"code": "600002", "name": "乙"}]}
        h = service.health(today)
        lv = {i["key"]: i["level"] for i in h["items"]}
        assert not h["ok"] and h["expected"] == "2026-09-28"
        assert lv["data"] == "bad" and lv["coverage"] == "bad" and lv["st"] == "bad" and lv["halt"] == "warn" and lv["size"] == "warn"
        # 数据最新、完整、名单按最新数据打分 → 通过
        monkeypatch.setattr(tcal, "china_now", lambda: datetime(2026, 9, 24, 20, 0, tzinfo=config.CHINA_TZ))
        full = pl.DataFrame({"code": ["600001", "600002"] * 2, "date": [date(2026, 9, 23)] * 2 + [date(2026, 9, 24)] * 2, "close": [1.0] * 4})
        monkeypatch.setattr(history, "load_panel", lambda **k: full)
        monkeypatch.setattr(universe, "load_universe", lambda: pl.DataFrame({"code": ["600001", "600002"], "name": ["甲", "乙"]}))
        h2 = service.health(today)
        assert h2["ok"], h2
    finally:
        tcal.set_calendar(None)
