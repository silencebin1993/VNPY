"""quant_web 第三版：交易核心（成交规则、账本、T+1、费用、交易计划与止损、除权、风控、手动实盘、日终与盘中、一键停止、通知；全部离线）"""
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

import quant_web.market
from quant_web import config
from quant_web import settings as settings_mod
from quant_web.trading import calendar as tcal
from quant_web.trading import engine, ledger, plans, review, risk, rules, service
from quant_web.trading.engine import OrderRequest

D0 = date(2026, 9, 21)                  # 周一
DAYS = [D0 + timedelta(days=i) for i in range(12) if (D0 + timedelta(days=i)).weekday() < 5]


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "WATCHLIST_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(config, "STOCK_LAB", tmp_path / "stock_lab")
    monkeypatch.setattr(config, "PANEL_DIR", tmp_path / "stock_lab" / "daily")
    tcal.set_calendar(DAYS)
    yield tmp_path
    tcal.set_calendar(None)


def bar(day: date, o: float, h: float, lo: float, c: float, pc: float) -> rules.Bar:
    return rules.Bar(day, o, h, lo, c, pc, round(pc * 1.1, 2), round(pc * 0.9, 2))


# ---------------------------------------------------------------- 规则

def test_fees_and_qty_rules() -> None:
    f = rules.fees("buy", 10_000, date(2026, 1, 5))
    assert f["commission"] == 5.0 and f["stamp"] == 0 and f["transfer"] == 0.1          # 不足 5 元按 5 元
    f2 = rules.fees("sell", 100_000, date(2026, 1, 5))
    assert f2["commission"] == 25.0 and f2["stamp"] == 50.0                              # 2023-08-28 后 0.05%
    assert rules.fees("sell", 100_000, date(2023, 1, 5))["stamp"] == 100.0
    assert rules.check_qty("600000", "buy", 150) and rules.check_qty("600000", "buy", 200) is None
    assert rules.check_qty("688001", "buy", 150) and rules.check_qty("688001", "buy", 201) is None
    assert rules.check_qty("600000", "sell", 150, 250) and rules.check_qty("600000", "sell", 250, 250) is None


def test_match_bar_rules() -> None:
    b = bar(DAYS[0], 10.0, 10.5, 9.6, 10.2, 10.0)
    assert rules.match_bar("buy", "limit", b, 10.1) == 10.0                   # 开盘价更优
    assert rules.match_bar("buy", "limit", b, 9.8) == 9.8
    assert rules.match_bar("buy", "limit", b, 9.5) is None
    up = bar(DAYS[0], 11.0, 11.0, 11.0, 11.0, 10.0)                           # 一字涨停
    assert rules.match_bar("buy", "limit", up, 11.0) is None and rules.match_bar("buy", "market", up) is None
    gap = bar(DAYS[0], 9.0, 9.3, 8.9, 9.1, 10.0)                              # 跳空低开到止损价下方
    assert rules.match_bar("sell", "stop", gap, trigger=9.5) == 9.0
    down = bar(DAYS[0], 9.0, 9.0, 9.0, 9.0, 10.0)                             # 一字跌停卖不掉
    assert rules.match_bar("sell", "stop", down, trigger=9.5) is None
    assert rules.match_bar("sell", "take_profit", b, trigger=10.4) == 10.4
    ex = rules.ex_rights(20.0, 10.0)
    assert ex["type"] == "shares" and abs(ex["multiplier"] - 2) < 1e-9
    assert rules.ex_rights(20.0, 19.5)["type"] == "cash" and rules.ex_rights(20.0, 20.0) is None


# ---------------------------------------------------------------- 账本：T+1、冻结、费用、平仓复盘

def test_paper_lifecycle_t1_fees_and_journal(ws: Path) -> None:
    acc = ledger.create_account("练习", "paper", "paper", 100_000)
    with ledger.connect() as c:
        p = plans.create(c, acc["id"], "600000", "浦发", 10.0, 9.0, 12.0, "none", 20, "测试")
        engine.place(c, acc["id"], OrderRequest("600000", "buy", 1000, "limit", 10.0, plan_id=p["id"]), trade_date=DAYS[0])
        assert engine.snapshot(c, acc["id"])["frozen"] > 10_000
        fills = engine.match_with_bars(c, acc["id"], DAYS[0], {"600000": bar(DAYS[0], 9.9, 10.2, 9.8, 10.1, 9.9)})
        assert fills and fills[0]["price"] == 9.9
        pos = ledger.one(c, "SELECT * FROM positions WHERE account_id=?", (acc["id"],))
        assert pos["qty"] == 1000 and pos["available"] == 0                              # T+1
        snap = engine.snapshot(c, acc["id"])
        assert snap["frozen"] == 0 and abs(snap["cash"] - (100_000 - 9900 - 5 - 0.099)) < 0.02
        with pytest.raises(ValueError, match="可卖数量"):
            engine.place(c, acc["id"], OrderRequest("600000", "sell", 1000, "limit", 10.5), trade_date=DAYS[0])
        stop = ledger.one(c, "SELECT * FROM orders WHERE plan_id=? AND kind='stop'", (p["id"],))
        assert stop and stop["status"] == "waiting_trigger" and stop["trigger"] == 9.0
        engine.settle_day(c, acc["id"], DAYS[0], {"600000": 10.1})
        assert ledger.one(c, "SELECT available FROM positions WHERE account_id=?", (acc["id"],))["available"] == 1000
        # 第二天跳空低开到止损下方：按开盘价止损
        engine.match_with_bars(c, acc["id"], DAYS[1], {"600000": bar(DAYS[1], 8.8, 9.0, 8.7, 8.9, 9.6)})     # 跌停价 8.64，没封死
        assert ledger.one(c, "SELECT * FROM positions WHERE account_id=?", (acc["id"],)) is None
        j = ledger.one(c, "SELECT * FROM journal WHERE account_id=?", (acc["id"],))
        assert j["exit"] == 8.8 and j["r_multiple"] < -1 and j["exit_reason"] == "计划止损"
        assert ledger.one(c, "SELECT status FROM plans WHERE id=?", (p["id"],))["status"] == "closed"
    st = review.stats(acc["id"])
    assert st["n"] == 1 and st["win_rate"] == 0 and st["max_losing_streak"] == 1


def test_ex_rights_adjusts_stop_before_matching(ws: Path) -> None:
    acc = ledger.create_account("除权", "paper", "paper", 100_000)
    with ledger.connect() as c:
        p = plans.create(c, acc["id"], "600001", "测试", 20.0, 18.0, None, "none", 20)
        engine.place(c, acc["id"], OrderRequest("600001", "buy", 1000, "limit", 20.0, plan_id=p["id"]), trade_date=DAYS[0])
        engine.match_with_bars(c, acc["id"], DAYS[0], {"600001": bar(DAYS[0], 20.0, 20.2, 19.8, 20.0, 20.0)})
        engine.settle_day(c, acc["id"], DAYS[0], {"600001": 20.0})
        # 10 送 10：今天参考价 10 元；最低 9.5 元仍高于调整后的止损 9 元，不应触发
        b = bar(DAYS[1], 10.0, 10.3, 9.5, 10.1, 10.0)
        engine.apply_ex_rights(c, acc["id"], {"600001": b.preclose})
        pos = ledger.one(c, "SELECT * FROM positions WHERE account_id=?", (acc["id"],))
        assert pos["qty"] == 2000 and abs(pos["cost"] * 2 - (20_000 + 5 + 0.2) / 1000) < 1e-6
        assert abs(plans.get(c, p["id"])["stop"] - 9.0) < 1e-9
        assert engine.match_with_bars(c, acc["id"], DAYS[1], {"600001": b}) == []
        assert engine.apply_ex_rights(c, acc["id"], {"600001": b.preclose}) == []        # 同一天不重复处理
        engine.settle_day(c, acc["id"], DAYS[1], {"600001": 10.1})
        cash0 = engine.snapshot(c, acc["id"])["cash"]
        engine.apply_ex_rights(c, acc["id"], {"600001": 9.9})                            # 每股派 0.2 元
        assert abs(engine.snapshot(c, acc["id"])["cash"] - cash0 - 2000 * 0.2) < 0.01


# ---------------------------------------------------------------- 风控

def _ctx(**kw) -> dict:
    base = {"settings": {"profile": {"boards": ["main"], "risk_per_trade": 0.01}, "risk": settings_mod.RiskSettings().model_dump(),
                         "live": {"enabled": False}},
            "quote": {"price": 10.0, "pct": 1.0, "limit_up": 11.0, "limit_down": 9.0}, "plan": {"stop": 9.3}, "regime_cap": 0.7}
    base.update(kw)
    return base


def test_risk_rules(ws: Path) -> None:
    acc = ledger.create_account("风控", "paper", "paper", 100_000)
    live = ledger.create_account("实盘", "live", "manual", 100_000)
    with ledger.connect() as c:
        codes = lambda r: {x["code"] for x in r["items"]}                      # noqa: E731
        ok = risk.check(c, acc, OrderRequest("600000", "buy", 1000, "limit", 10.0), _ctx())
        assert not ok["blocked"] and not ok["need_confirm"], ok
        r = risk.check(c, acc, OrderRequest("300001", "buy", 100, "limit", 10.0), _ctx())
        assert r["blocked"] and "board" in codes(r)
        r = risk.check(c, acc, OrderRequest("600000", "buy", 1000, "limit", 10.0), _ctx(plan={}))
        assert "no_stop" in codes(r)
        r = risk.check(c, acc, OrderRequest("600000", "buy", 1000, "limit", 10.0), _ctx(quote={"price": 10.0, "pct": 6.2}))
        assert r["need_confirm"] and "chase" in codes(r)
        r = risk.check(c, acc, OrderRequest("600000", "buy", 3000, "limit", 10.0), _ctx())
        assert "single_cap" in codes(r) and "risk_per_trade" in codes(r)
        r = risk.check(c, acc, OrderRequest("600000", "buy", 100, "limit", 12.0), _ctx())
        assert "price_band" in codes(r)
        r = risk.check(c, acc, OrderRequest("600000", "buy", 1000, "limit", 10.0), _ctx(stage="distribution"))
        assert r["blocked"] and "stage" in codes(r)
        r = risk.check(c, live, OrderRequest("600000", "buy", 1000, "limit", 10.0), _ctx())
        assert "live_off" in codes(r)
        r = risk.check(c, acc, OrderRequest("600000", "sell", 100, "limit", 10.0), _ctx())
        assert "sell_qty" in codes(r)
        # 连亏 3 笔 → 冷静期
        for _ in range(3):
            c.execute("INSERT INTO journal(account_id, code, closed, pnl) VALUES (?,?,?,?)", (acc["id"], "600009", ledger.today().isoformat(), -100))
        r = risk.check(c, acc, OrderRequest("600000", "buy", 1000, "limit", 10.0), _ctx())
        assert "cooldown" in codes(r)
        # 当天亏超 3% → 禁止买入
        c.execute("DELETE FROM journal WHERE account_id=?", (acc["id"],))
        c.execute("INSERT INTO equity(account_id, date, cash, market_value, total) VALUES (?,?,?,?,?)",
                  (acc["id"], (ledger.today() - timedelta(days=1)).isoformat(), 100_000, 0, 104_000))
        r = risk.check(c, acc, OrderRequest("600000", "buy", 1000, "limit", 10.0), _ctx())
        assert "daily_loss" in codes(r)


# ---------------------------------------------------------------- 服务层：下单、手动实盘、交割单、日终、盘中、一键停止

@pytest.fixture()
def svc(ws: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    quotes = {"600000": {"code": "600000", "name": "浦发", "price": 10.0, "preclose": 9.9, "pct": 1.0, "limit_up": 10.89,
                         "limit_down": 8.91, "bid1": 9.99, "bid1_vol": 1000, "ask1": 10.0, "ask1_vol": 1000}}
    monkeypatch.setattr(service, "quote_map", lambda codes: {k: v for k, v in quotes.items() if k in codes})
    monkeypatch.setattr(service, "recent_levels", lambda code, days=60: {"ma20": 9.6, "ma10": 9.8, "atr": 0.2})
    monkeypatch.setattr(service, "regime_cap", lambda: 0.7)
    fake_diag = types.ModuleType("quant_web.analysis.diagnose")
    fake_diag.full = lambda code: {"stage": {"key": "markup"}, "risk": {"level": "green"}, "name": "浦发"}
    monkeypatch.setitem(sys.modules, "quant_web.analysis.diagnose", fake_diag)
    import quant_web.analysis
    monkeypatch.setattr(quant_web.analysis, "diagnose", fake_diag, raising=False)
    monkeypatch.setattr(tcal, "session", lambda now=None: "closed")
    return quotes


def test_submit_creates_plan_and_requires_confirmation(svc: dict) -> None:
    acc = ledger.create_account("模拟", "paper", "paper", 100_000)
    pv = service.preview(acc["id"], {"code": "600000", "side": "buy", "qty": 1000, "price": 10.0})
    assert pv["suggest"]["stop"] == 9.6 and pv["suggest"]["shares"] == 2000 and not pv["checks"]["blocked"]
    res = service.submit(acc["id"], {"code": "600000", "side": "buy", "qty": 1000, "price": 10.0}, {"target": 11.0, "reason": "测试"})
    assert res["plan"]["stop"] == 9.6 and res["order"]["status"] == "submitted"
    svc["600000"]["pct"] = 6.0
    with pytest.raises(ValueError, match="需要你确认"):
        service.submit(acc["id"], {"code": "600000", "side": "buy", "qty": 100, "price": 10.0})
    r2 = service.submit(acc["id"], {"code": "600000", "side": "buy", "qty": 100, "price": 10.0}, acknowledge=True)
    import json
    assert json.loads(r2["order"]["flags"])["chase"] is True


def test_manual_live_account_fill_and_statement(svc: dict) -> None:
    acc = ledger.create_account("实盘手动", "live", "manual", 100_000)
    data = settings_mod.load().model_dump()
    data["live"]["enabled"] = True
    settings_mod.save(settings_mod.validate(data))
    res = service.submit(acc["id"], {"code": "600000", "side": "buy", "qty": 1000, "price": 10.0})
    assert res["order"]["status"] == "pending_manual"
    service.confirm_fill(acc["id"], res["order"]["id"], 600, 9.98)
    with ledger.connect() as c:
        assert ledger.one(c, "SELECT qty FROM positions WHERE account_id=?", (acc["id"],))["qty"] == 600
        assert ledger.one(c, "SELECT status FROM orders WHERE id=?", (res["order"]["id"],))["status"] == "partial"
    csv_text = ("成交日期,证券代码,证券名称,操作,成交数量,成交均价,成交金额,成交编号\n"
                "20260922,600000,浦发银行,证券买入,400,9.99,3996,T001\n"
                "20260922,000001,平安银行,证券买入,100,11.2,1120,T002\n")
    r = service.import_statement(acc["id"], csv_text)
    assert r == {"rows": 2, "added": 2, "skipped": 0}
    assert service.import_statement(acc["id"], csv_text)["skipped"] == 2                   # 重复导入不重复记账
    with ledger.connect() as c:
        assert ledger.one(c, "SELECT status FROM orders WHERE id=?", (res["order"]["id"],))["status"] == "filled"
        assert {r["code"] for r in ledger.rows(c, "SELECT code FROM positions WHERE account_id=?", (acc["id"],))} == {"600000", "000001"}
    tdx = "证券代码\t证券名称\t买卖标志\t成交数量\t成交价格\t成交编号\n600036\t招商银行\t卖出\t100\t40.1\tX9\n"
    assert len(__import__("quant_web.trading.brokers.manual", fromlist=["x"]).parse_statement(tdx)) == 1
    with pytest.raises(ValueError, match="表头"):
        service.import_statement(acc["id"], "a,b,c\n1,2,3\n")


def test_eod_intraday_alert_and_kill_switch(svc: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    paper = ledger.create_account("模拟", "paper", "paper", 100_000)
    live = ledger.create_account("实盘", "live", "manual", 100_000)
    with ledger.connect() as c:
        p = plans.create(c, live["id"], "600000", "浦发", 10.0, 9.5, None, "none", 20)
        engine.place(c, live["id"], OrderRequest("600000", "buy", 1000, "limit", 10.0, plan_id=p["id"]), status="pending_manual")
        oid = ledger.one(c, "SELECT id FROM orders WHERE account_id=?", (live["id"],))["id"]
        engine.apply_fill(c, oid, live["id"], "600000", "buy", 1000, 10.0, DAYS[0], "浦发", "manual", p["id"])
        engine.place(c, paper["id"], OrderRequest("600000", "buy", 500, "limit", 10.0), trade_date=DAYS[1])
    # 日终：模拟盘按日线成交
    hist = types.ModuleType("quant_web.market.history")
    hist.last_date = lambda: DAYS[1]
    hist.load_panel = lambda codes=None, start=None, end=None, columns=None, **kw: pl.DataFrame({
        "date": [DAYS[1]], "code": ["600000"], "open": [9.95], "high": [10.1], "low": [9.9], "close": [10.0], "preclose": [10.0],
        "tradestatus": [1], "is_st": [False], "volume": [1e6]})
    monkeypatch.setitem(sys.modules, "quant_web.market.history", hist)
    monkeypatch.setattr(quant_web.market, "history", hist, raising=False)
    s = service.run_eod(DAYS[1])
    assert any(a["fills"] == 1 for a in s["accounts"] if a["id"] == paper["id"])
    # 盘中：实盘跌破止损 → 紧急提醒（同一天只一次）
    monkeypatch.setattr(tcal, "session", lambda now=None: "open")
    svc["600000"]["price"] = 9.4
    out = service.run_intraday(datetime(2026, 9, 23, 10, 0))
    out2 = service.run_intraday(datetime(2026, 9, 23, 10, 1))
    assert out["alerts"] >= 1 and out2["alerts"] == 0
    with ledger.connect() as c:
        al = ledger.rows(c, "SELECT * FROM alerts WHERE kind='stop_hit'")
    assert len(al) == 1 and al[0]["level"] == "urgent" and "跌破止损" in al[0]["title"] + al[0]["body"]
    # 一键停止
    data = settings_mod.load().model_dump()
    data["live"]["enabled"] = True
    settings_mod.save(settings_mod.validate(data))
    with ledger.connect() as c:
        engine.place(c, live["id"], OrderRequest("600000", "sell", 100, "limit", 11.0), status="pending_manual")
    k = service.kill_switch()
    assert k["live_enabled"] is False and k["cancelled"] >= 1 and settings_mod.load().live.enabled is False


# ---------------------------------------------------------------- 通知

def test_notify_levels_and_quiet_hours(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_web import notify
    sent: list = []
    monkeypatch.setattr(notify, "deliver", lambda cfg, ch, t, b: sent.append((ch, t)) or {"ok": True})
    cfg = {"channels": ["web", "pushplus"], "pushplus_token": "x", "min_level": "warn", "quiet_start": "22:30", "quiet_end": "07:30"}
    notify.send("一般消息", level="info", cfg=cfg, now=datetime(2026, 9, 23, 12, 0))
    assert sent == []
    notify.send("重要", level="warn", cfg=cfg, now=datetime(2026, 9, 23, 12, 0))
    assert sent == [("pushplus", "【量化助手】重要")]
    r = notify.send("夜里", level="warn", cfg=cfg, now=datetime(2026, 9, 23, 23, 30))
    assert r.get("quiet") and len(sent) == 1
    notify.send("止损", level="urgent", cfg=cfg, now=datetime(2026, 9, 23, 23, 30))          # 紧急不受免打扰限制
    assert len(sent) == 2
    with ledger.connect() as c:
        assert len(ledger.rows(c, "SELECT * FROM alerts")) == 4                              # 站内信都有


# ---------------------------------------------------------------- 网页接口

@pytest.fixture()
def client(svc: dict):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from quant_web.api import server
    with TestClient(server.create_app(background=False)) as c:
        yield c


def test_trading_api_accounts_orders_plans(client, svc: dict) -> None:
    b = client.get("/api/trading/brokers").json()
    names = {x["name"]: x for x in b["brokers"]}
    assert names["paper"]["available"] and not names["paper"]["is_live"] and "vnpy_setting" not in b["live"]
    assert "trail_pct8" in b["trail_rules"] and b["monitor"]["running"] is False
    acc = client.post("/api/trading/accounts", json={"name": "模拟", "initial_cash": 100_000}).json()
    assert acc["kind"] == "paper" and client.get("/api/trading/accounts").json()[0]["total"] == 100_000
    pv = client.post("/api/trading/preview", json={"account_id": acc["id"], "order": {"code": "600000", "side": "buy", "qty": 1000, "price": 10.0}}).json()
    assert pv["suggest"]["stop"] == 9.6 and not pv["checks"]["blocked"]
    res = client.post("/api/trading/orders", json={"account_id": acc["id"], "order": {"code": "600000", "side": "buy", "qty": 1000, "price": 10.0},
                                                   "plan": {"target": 11.0, "reason": "接口测试"}}).json()
    oid, pid = res["order"]["id"], res["plan"]["id"]
    d = client.get(f"/api/trading/accounts/{acc['id']}").json()
    assert d["frozen"] > 0 and d["orders"][0]["id"] == oid and d["plans"][0]["id"] == pid
    up = client.put(f"/api/trading/plans/{pid}", json={"account_id": acc["id"], "stop": 9.0})
    assert up.status_code == 400 and "止损" in up.json()["detail"]                       # 往下挪止损要明确允许
    assert client.put(f"/api/trading/plans/{pid}", json={"account_id": acc["id"], "stop": 9.7}).status_code == 200
    svc["600000"]["pct"] = 6.0
    r = client.post("/api/trading/orders", json={"account_id": acc["id"], "order": {"code": "600000", "side": "buy", "qty": 100, "price": 10.0}})
    assert r.status_code == 400 and "确认" in r.json()["detail"]
    c = client.post(f"/api/trading/orders/{oid}/cancel", json={"account_id": acc["id"]}).json()
    assert c["status"] == "cancelled"
    assert client.get("/api/trading/accounts/nope").status_code == 400
    assert client.post(f"/api/trading/accounts/{acc['id']}/reset").json()["cash"] == 100_000
    assert client.delete(f"/api/trading/accounts/{acc['id']}").json()["archived"] == acc["id"]
    assert client.get("/api/trading/accounts").json() == []


def test_trading_api_manual_import_review_nightly_alerts_kill(client, svc: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    acc = client.post("/api/trading/accounts", json={"name": "实盘手动", "kind": "live", "broker": "manual", "initial_cash": 50_000}).json()
    csv_text = ("成交日期,证券代码,证券名称,操作,成交数量,成交均价,成交金额,成交编号\n"
                "20260921,600000,浦发银行,证券买入,500,10.0,5000,T1\n"
                "20260922,600000,浦发银行,证券卖出,500,10.5,5250,T2\n")
    assert client.post("/api/trading/import", json={"account_id": acc["id"], "text": csv_text}).json()["added"] == 2
    rv = client.get(f"/api/trading/review/{acc['id']}").json()
    assert rv["stats"]["n"] == 1 and rv["trades"][0]["pnl"] > 0
    hist = types.ModuleType("quant_web.market.history")
    hist.last_date = lambda: DAYS[2]
    monkeypatch.setitem(sys.modules, "quant_web.market.history", hist)
    monkeypatch.setattr(quant_web.market, "history", hist, raising=False)
    n = client.get("/api/trading/nightly?rebuild=true").json()
    assert n["date"] == str(DAYS[2]) and n["accounts"][0]["id"] == acc["id"] and n["candidates"] == []
    assert client.get("/api/trading/nightly").json()["date"] == str(DAYS[2])              # 读最近一份
    from quant_web import notify
    notify.send("测试提醒", "内容", level="warn", cfg={"channels": ["web"], "min_level": "warn"})
    a = client.get("/api/alerts?unread=true").json()
    assert a["unread"] >= 1 and a["alerts"][0]["title"].endswith("测试提醒")
    assert client.post("/api/alerts/read", json={}).json()["ok"] and client.get("/api/alerts").json()["unread"] == 0
    assert client.post("/api/notify/test", json={"channel": "web"}).json()["ok"] is True
    k = client.post("/api/trading/kill").json()
    assert k["live_enabled"] is False and settings_mod.load().live.enabled is False
    m = client.get("/api/trading/monitor").json()
    assert m["running"] is False and "enabled" in m


def test_live_stop_and_queued_failures_still_alert(svc: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """实盘止损：已有卖单挂着 / 自动卖出报错 / 排队委托开盘提交失败 —— 都要发紧急提醒，不能因为出错把提醒吞掉"""
    acc = ledger.create_account("实盘自动", "live", "qmt", 100_000)
    data = settings_mod.load().model_dump()
    data["live"]["enabled"] = True
    settings_mod.save(settings_mod.validate(data))

    class FakeAuto:
        is_live, can_auto, mode = True, True, "ok"

        def __init__(self, account: dict, settings: dict | None = None) -> None:
            self.account = account

        def place(self, conn, req, ref_price=None):
            if FakeAuto.mode == "raise":
                raise RuntimeError("券商连接断开")
            return engine.place(conn, self.account["id"], req, ref_price=ref_price)

        def cancel(self, conn, order_id):
            return engine.cancel(conn, order_id)

        def sync(self, conn):
            raise RuntimeError("同步也坏了")

    monkeypatch.setattr(service, "get_broker", lambda account, settings=None: FakeAuto(account, settings))
    with ledger.connect() as c:
        p = plans.create(c, acc["id"], "600000", "浦发", 10.0, 9.5, None, "none", 20)
        engine.apply_fill(c, None, acc["id"], "600000", "buy", 1000, 10.0, DAYS[0], "浦发", "qmt", p["id"])
        c.execute("UPDATE positions SET available=qty WHERE account_id=?", (acc["id"],))
        tp = engine.place(c, acc["id"], OrderRequest("600000", "sell", 1000, "limit", 11.0), trade_date=DAYS[2])   # 已挂着的止盈卖单
    svc["600000"]["price"] = 9.4

    def stop_alerts() -> list[dict]:
        with ledger.connect() as c:
            return ledger.rows(c, "SELECT * FROM alerts WHERE kind='stop_hit' ORDER BY id")

    monkeypatch.setattr(tcal, "session", lambda now=None: "auction")
    assert service.run_intraday(datetime(2026, 9, 23, 9, 20))["alerts"] == 0             # 集合竞价的虚拟价不判断止损
    monkeypatch.setattr(tcal, "session", lambda now=None: "open")
    out = service.run_intraday(datetime(2026, 9, 23, 10, 0))                               # 同步报错也不影响
    assert out["alerts"] == 1 and "已经有卖出委托挂着" in stop_alerts()[-1]["body"]
    # 撤掉止盈单；第二天：自动卖出报错 + 排队的买单开盘提交失败
    service.cancel(acc["id"], tp["id"])
    with ledger.connect() as c:
        q = engine.place(c, acc["id"], OrderRequest("000001", "buy", 100, "limit", 11.0), status="queued")
    FakeAuto.mode = "raise"
    out = service.run_intraday(datetime(2026, 9, 24, 10, 0))
    assert out["alerts"] == 1 and "自动卖出失败" in stop_alerts()[-1]["body"] and stop_alerts()[-1]["level"] == "urgent"
    with ledger.connect() as c:
        qo = ledger.one(c, "SELECT * FROM orders WHERE id=?", (q["id"],))
        rej = ledger.rows(c, "SELECT * FROM alerts WHERE kind='order_rejected'")
    assert qo["status"] == "cancelled" and "开盘提交失败" in qo["message"] and len(rej) == 1
    # 第三天：正常自动卖出（数量不超过可卖）
    FakeAuto.mode = "ok"
    service.run_intraday(datetime(2026, 9, 25, 10, 0))
    assert "已按设置自动提交卖出 1000 股" in stop_alerts()[-1]["body"]
    with ledger.connect() as c:
        auto = ledger.rows(c, "SELECT * FROM orders WHERE account_id=? AND source='monitor'", (acc["id"],))
        stop_order = ledger.one(c, "SELECT * FROM orders WHERE account_id=? AND kind='stop'", (acc["id"],))
    assert len(auto) == 1 and auto[0]["qty"] == 1000 and auto[0]["kind"] == "market"
    assert service.cancel(acc["id"], stop_order["id"])["status"] == "cancelled"            # 程序自己盯的条件单：本地撤


def test_broker_sync_skips_unbookable_trade_once(ws: Path) -> None:
    """券商同步：卖出了账本里没有的股票（在别处下的单）→ 提醒一次，不卡住后面的成交"""
    from quant_web.trading.brokers.base import Broker
    acc = ledger.create_account("实盘", "live", "qmt", 100_000)
    b = Broker(acc)
    with ledger.connect() as c:
        assert b.book_trade(c, "qmt:T1", None, "600036", "sell", 100, 40.0) == 0
        assert b.book_trade(c, "qmt:T1", None, "600036", "sell", 100, 40.0) == 0
        assert b.book_trade(c, "qmt:T2", None, "600000", "buy", 200, 10.0) == 1
        al = ledger.rows(c, "SELECT * FROM alerts WHERE kind='sync_mismatch'")
        pos = ledger.one(c, "SELECT * FROM positions WHERE account_id=? AND code='600000'", (acc["id"],))
    assert len(al) == 1 and "没能记账" in al[0]["title"] and pos["qty"] == 200


def test_matching_skips_orders_cancelled_earlier_in_the_same_loop(ws: Path) -> None:
    """止损单先成交、平仓时撤掉了同一持仓的止盈单；撮合循环轮到止盈单时不能再撤一次（以前会让整个日终报错）"""
    acc = ledger.create_account("模拟", "paper", "paper", 100_000)
    with ledger.connect() as c:
        p = plans.create(c, acc["id"], "600000", "浦发", 10.0, 9.5, 12.0, "none", 20)
        engine.place(c, acc["id"], OrderRequest("600000", "buy", 1000, "limit", 10.0, plan_id=p["id"]), trade_date=DAYS[0])
        engine.match_with_bars(c, acc["id"], DAYS[0], {"600000": bar(DAYS[0], 10.0, 10.1, 9.9, 10.0, 10.0)})
        engine.settle_day(c, acc["id"], DAYS[0], {"600000": 10.0})
        engine.place(c, acc["id"], OrderRequest("600000", "sell", 1000, "take_profit", trigger=12.0, plan_id=p["id"]), trade_date=DAYS[1])
        fills = engine.match_with_bars(c, acc["id"], DAYS[1], {"600000": bar(DAYS[1], 9.4, 9.5, 9.3, 9.4, 10.0)})     # 跳空跌破止损
        assert len(fills) == 1 and fills[0]["side"] == "sell" and fills[0]["price"] == 9.4
        left = ledger.rows(c, "SELECT status FROM orders WHERE account_id=? AND kind='take_profit'", (acc["id"],))
        assert left[0]["status"] == "cancelled"
