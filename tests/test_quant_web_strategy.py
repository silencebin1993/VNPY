"""quant_web 第三版 P8：策略中心（合成数据；回测 = 真正的模拟盘代码逐日跑；和模拟盘一致性；模拟跟踪；接口）"""
import sys
import types
import warnings
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

import quant_web.market
from quant_web import config
from quant_web import settings as settings_mod
from quant_web.strategy import backtest as SB
from quant_web.strategy import follow, step, store
from quant_web.strategy import signals as SG
from quant_web.strategy import templates as T
from quant_web.trading import calendar as tcal
from quant_web.trading import ledger, nightly, service

START = date(2024, 1, 1)


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_table(n_stocks: int = 12, n_days: int = 480, seed: int = 11, vol: float = 0.025) -> pl.DataFrame:
    """合成的全历史字段表（真实价 = 前复权价，没有除权）；波动大一些，止损和止盈都会被触发"""
    rng = np.random.default_rng(seed)
    days = _weekdays(START, n_days)
    parts = []
    for i in range(n_stocks):
        code = f"{600000 + i}"
        ret = rng.normal(0.0004, vol, n_days)
        close = np.round(10 * np.cumprod(1 + ret), 2)
        pre = np.concatenate([[round(close[0] / (1 + ret[0]), 2)], close[:-1]])
        opn = np.round(pre * (1 + rng.normal(0, 0.006, n_days)), 2)
        hi = np.round(np.maximum(opn, close) * (1 + rng.random(n_days) * 0.02), 2)
        lo = np.round(np.minimum(opn, close) * (1 - rng.random(n_days) * 0.02), 2)
        lu, ld = np.round(pre * 1.1 + 1e-6, 2), np.round(pre * 0.9 + 1e-6, 2)
        hi, lo = np.minimum(hi, lu), np.maximum(lo, ld)
        opn, close = np.clip(opn, ld, lu), np.clip(close, ld, lu)
        stage = np.where(rng.random(n_days) < 0.03, "distribution", "markup")
        parts.append(pl.DataFrame({
            "code": [code] * n_days, "date": days, "open": opn, "high": hi, "low": lo, "close": close, "raw_open": opn, "raw_close": close,
            "preclose": pre, "limit_up": lu, "limit_down": ld, "is_st": [False] * n_days, "amount": [2e8] * n_days, "turn": [2.0] * n_days,
            "volume": [2e7] * n_days, "board": ["main"] * n_days, "pos": np.arange(n_days), "amt20": [2e8] * n_days,
            "adj_factor": [1.0] * n_days, "stage": stage, "rps120": rng.random(n_days) * 100, "ret20": rng.normal(0, 0.1, n_days),
            "pos250": rng.random(n_days), "ma_bull": rng.random(n_days) > 0.5, "vr20": rng.random(n_days), "bias20": rng.normal(0, 1, n_days),
            "close_raw": close, "tradestatus": [1] * n_days,
        }))
    return pl.concat(parts).sort(["code", "date"])


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "WATCHLIST_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(config, "STOCK_LAB", tmp_path / "stock_lab")
    monkeypatch.setattr(config, "PANEL_DIR", tmp_path / "stock_lab" / "daily")
    return tmp_path


@pytest.fixture(scope="module")
def base() -> tuple[pl.DataFrame, pl.DataFrame]:
    t = make_table()
    return t, t


def simple_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """策略信号换成一个不需要财报的简单方案（按相对强度打分）"""
    from quant_web.screener import schemes as S
    sch = S.validate_scheme({"name": "测试", "universe": {"boards": ["main"], "min_amount": 0, "price_min": 1}, "conditions": [],
                             "scoring": {"scheme": "momentum"}, "risk": {"exclude_red": False, "exclude_yellow": False}, "top_n": 10})
    monkeypatch.setattr(SG, "_scheme", lambda spec: sch)


# ---------------------------------------------------------------- 模板

def test_templates_resolve_and_validate() -> None:
    s = T.resolve("reversal_value")
    assert s["entry"] == "open" and s["max_positions"] == 5 and s["signal"]["scheme_id"] == "reversal_value"
    s2 = T.resolve("washout_dip", {"max_days": 7, "target_r": 0})
    assert s2["max_days"] == 7 and s2["target_r"] == 0.0 and s2["entry"] == "pullback"
    with pytest.raises(ValueError, match="最长持有天数"):
        T.resolve("reversal_value", {"max_days": 0})
    with pytest.raises(ValueError, match="模板"):
        T.resolve("moon")
    assert not any("推荐" in t["name"] for t in T.listing())                # 没有一个模板被标成"推荐"


# ---------------------------------------------------------------- 回测和模拟盘完全一致

def test_backtest_equals_live_paper_pipeline(ws: Path, base, monkeypatch: pytest.MonkeyPatch) -> None:
    table, frame = base
    days = sorted(table["date"].unique().to_list())[100:160]
    tcal.set_calendar(sorted(table["date"].unique().to_list()))
    try:
        book = SG.PriceBook(table, frame)
        spec = T.resolve("reversal_value", {"max_days": 5, "max_positions": 3, "target_r": 1.5, "trail": "breakeven", "regime": False})
        codes = sorted(table["code"].unique().to_list())
        cands = {d: [codes[(i + k) % len(codes)] for k in range(4)] for i, d in enumerate(days)}
        settings = SB.settings_view()
        idx = {d: i for i, d in enumerate(book.days)}
        held = lambda d: (lambda opened: idx[d] - idx[date.fromisoformat(opened)])          # noqa: E731
        # A：回测（内存账本）
        a = SB.simulate(spec, days, cands, book, {}, settings, 100_000)
        # B：真正的每日流水线：service.run_eod 从“日线”读行情（假的 history 模块，只能看到当天及以前），再走策略的每日一步
        clock = {"day": days[0]}
        raw = table.select("code", "date", "open", "high", "low", "close", "preclose", "volume", "tradestatus", "is_st")
        hist = types.ModuleType("quant_web.market.history")
        hist.last_date = lambda: clock["day"]

        def load_panel(codes=None, start=None, end=None, columns=None, **kw):
            out = raw.filter(pl.col("date") <= clock["day"])
            if codes:
                out = out.filter(pl.col("code").is_in(codes))
            if start is not None:
                out = out.filter(pl.col("date") >= start)
            if end is not None:
                out = out.filter(pl.col("date") <= end)
            return out
        hist.load_panel = load_panel
        monkeypatch.setitem(sys.modules, "quant_web.market.history", hist)
        monkeypatch.setattr(quant_web.market, "history", hist, raising=False)
        acc = ledger.create_account("跟踪", "paper", "paper", 100_000)
        for i, d in enumerate(days):
            clock["day"] = d
            service.run_eod(d)
            if i + 1 < len(days):
                with ledger.connect() as conn:
                    step.strategy_step(conn, acc["id"], spec, d, days[i + 1], cands[d], lambda c, d=d: book.info(d, c), settings, None, held(d))
        with ledger.connect() as conn:
            jb = ledger.rows(conn, "SELECT code, qty, entry, exit, pnl FROM journal WHERE account_id=? ORDER BY closed, code", (acc["id"],))
            eq_b = ledger.rows(conn, "SELECT date, total FROM equity WHERE account_id=? ORDER BY date", (acc["id"],))
        ja = sorted(({k: t[k] for k in ("code", "qty", "entry", "exit", "pnl")} for t in a["trades"]), key=lambda t: t["code"])
        assert len(ja) >= 5, "合成数据应该产生若干笔完整交易"
        key = lambda t: (t["code"], t["qty"], round(t["entry"], 4), round(t["exit"], 4), round(t["pnl"], 2))  # noqa: E731
        assert sorted(map(key, ja)) == sorted(map(key, jb))
        assert [round(e[1], 2) for e in a["equity"]] == [round(e["total"], 2) for e in eq_b]
    finally:
        tcal.set_calendar(None)


def test_step_rules_exits_takeprofit_and_caps(ws: Path, base) -> None:
    table, frame = base
    book = SG.PriceBook(table, frame)
    d0, d1 = book.days[200], book.days[201]
    spec = T.resolve("reversal_value", {"max_positions": 2, "target_r": 2.0})
    settings = SB.settings_view()
    codes = sorted(table["code"].unique().to_list())
    with ledger.isolated() as conn:
        acc = ledger.create_account("测试", "paper", "paper", 100_000)
        out = step.strategy_step(conn, acc["id"], spec, d0, d1, codes, lambda c: {**book.info(d0, c), "stage": "markup"}, settings, None,
                                 lambda o: 0)
        assert out["buys"] == 2                                            # 最多同时持有 2 只
        orders = ledger.rows(conn, "SELECT * FROM orders WHERE account_id=?", (acc["id"],))
        assert all(o["trade_date"] == str(d1) and o["side"] == "buy" and o["source"] == "strategy" for o in orders)
        pl_rows = ledger.rows(conn, "SELECT * FROM plans WHERE account_id=?", (acc["id"],))
        assert all(p["stop"] < p["entry"] < p["target"] for p in pl_rows)   # 有止损、有目标
        # 大盘弱势：仓位上限 0 → 不开新仓
        acc2 = ledger.create_account("弱势", "paper", "paper", 100_000)
        s2 = {**settings, "risk": {**settings["risk"], "regime_caps": {"weak": 0.0}}}
        spec2 = {**spec, "regime": True}
        out2 = step.strategy_step(conn, acc2["id"], spec2, d0, d1, codes, lambda c: book.info(d0, c), s2, "weak", lambda o: 0)
        assert out2["buys"] == 0 and any("仓位上限" in x for x in out2["skipped"])


def test_backtest_run_is_honest(ws: Path, base, monkeypatch: pytest.MonkeyPatch) -> None:
    simple_scheme(monkeypatch)
    res = SB.run("trend_swing", {"max_positions": 3, "max_days": 10}, base=base, seeds=2, capital=100_000)
    assert set(res["segments"]) == {"selection", "holdout"} and res["holdout_start"] == "2025-07-01"
    for seg in res["segments"].values():
        assert seg["random"]["n"] == 2 and "excess_cagr" in seg and {"t", "daily_t", "nw_t"} <= set(seg)
        assert seg["strategy"]["days"] > 20 and seg["strategy"]["trades"]["n"] > 0

    warm = str(sorted(base[0]["date"].unique().to_list())[SB.WARMUP_DAYS])
    assert res["segments"]["selection"]["curve"]["dates"][0] == warm              # 前 260 个交易日只用来热身
    assert res["segments"]["holdout"]["curve"]["dates"][0] >= "2025-07-01"    # 留出期从新账户开始
    assert res["verdict"]["key"] in ("good", "weak", "bad", "short") and res["rules"]
    assert ledger.list_accounts() == []                                       # 回测不碰真实账本


def test_follow_places_orders_and_live_suggestions(ws: Path, base, monkeypatch: pytest.MonkeyPatch) -> None:
    simple_scheme(monkeypatch)
    monkeypatch.setattr(follow, "_regime_key", lambda: "neutral")
    item = store.save({"template": "trend_swing", "params": {"max_positions": 3}, "name": "测试策略"})
    store.update(item["id"], live=True, follow={"enabled": True, "account_id": None})
    day = base[0]["date"].max() - timedelta(days=10)
    day = max(d for d in base[0]["date"].unique().to_list() if d <= day)
    out = follow.run_daily(day, base=base)
    res = out["items"][0]
    assert "error" not in res and res["follow"]["buys"] > 0 and res["live"] > 0
    aid = store.get(item["id"])["follow"]["account_id"]
    with ledger.connect() as conn:
        orders = ledger.rows(conn, "SELECT * FROM orders WHERE account_id=? AND side='buy'", (aid,))
    assert orders and all(o["trade_date"] > str(day) for o in orders)
    cands = follow.live_candidates(str(day))
    assert cands and cands[0]["scheme_name"] == "策略：测试策略" and cands[0]["shares"] % 100 == 0
    assert any(c.get("scheme_id") == item["id"] for c in nightly.candidates())


# ---------------------------------------------------------------- 接口

@pytest.fixture()
def client(ws: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from quant_web.api import server
    with TestClient(server.create_app(background=False)) as c:
        yield c


def test_strategy_api(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_web import tasks
    h = client.get("/api/strategy").json()
    assert {t["id"] for t in h["templates"]} >= {"reversal_value", "model_rotation"} and h["items"] == []
    it = client.post("/api/strategy/items", json={"template": "reversal_value", "params": {"max_days": 15}}).json()
    assert it["params"]["max_days"] == 15 and it["follow"]["enabled"] is False
    bad = client.post("/api/strategy/items", json={"template": "reversal_value", "params": {"max_positions": 99}})
    assert bad.status_code == 400
    submitted: list = []
    monkeypatch.setattr(tasks, "submit", lambda name, params=None, title=None: submitted.append((name, params)) or "job9")
    r = client.post("/api/strategy/backtest", json={"template": "reversal_value", "params": it["params"], "item_id": it["id"]}).json()
    assert r["job_id"] == "job9" and submitted[0][0] == "strategy_backtest"
    m = client.post("/api/strategy/backtest", json={"template": "model_rotation"})
    assert m.status_code == 400 and "模型实验室" in m.json()["detail"]
    f = client.post(f"/api/strategy/items/{it['id']}/follow", json={"enabled": True}).json()
    assert f["follow"]["enabled"] and f["account"]["total"] > 0
    assert client.get("/api/trading/accounts").json()[0]["name"].startswith("策略跟踪")
    lv = client.post(f"/api/strategy/items/{it['id']}/live", json={"enabled": True}).json()
    assert lv["live"] is True
    assert client.post("/api/strategy/run").json()["job_id"] == "job9"
    assert client.get("/api/strategy/backtest/0123456789abcdef").status_code == 404
    assert client.delete(f"/api/strategy/items/{it['id']}").json()["deleted"] == it["id"]
    assert client.get("/api/strategy").json()["items"] == []
    s = settings_mod.load()
    assert s.lab.max_mem_gb == 20
