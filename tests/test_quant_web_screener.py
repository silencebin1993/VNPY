"""quant_web 第三版：止损与仓位、选股方案校验、选股引擎、方案回测（全部离线，用假日线）"""
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

import quant_web.market
from quant_web import config
from quant_web.screener import backtest, engine, schemes, store
from quant_web.trading import sizing


# ---------------------------------------------------------------- 止损与仓位

def test_suggest_stop_rules() -> None:
    s = sizing.suggest_stop(10.0, atr=0.2, ma20=9.7)            # 2ATR → 9.6；MA20×0.98 → 9.506；取更近的 9.6
    assert s["stop"] == 9.6 and "波幅" in s["basis"]
    s2 = sizing.suggest_stop(10.0, atr=1.0, ma20=None)            # 2ATR → 8.0，比 8% 还远 → 按上限 9.2
    assert s2["stop"] == 9.2 and "上限" in s2["basis"]
    s3 = sizing.suggest_stop(10.0, atr=0.01, ma20=9.99)           # 太近 → 至少留 2%
    assert s3["stop"] == 9.8 and "空间" in s3["basis"]
    s4 = sizing.suggest_stop(10.0, atr=None, ma20=11.0)           # 均线在上方不用
    assert s4["stop"] == 9.2


def test_position_size_limits() -> None:
    z = sizing.position_size(100_000, 10.0, 9.5)                  # 最多亏 1000 → 2000 股，但单只 20% = 2000 股
    assert z.shares == 2000 and z.amount == 20_000
    z2 = sizing.position_size(100_000, 10.0, 9.0)                 # 每股风险 1 元 → 1000 股（风险限制）
    assert z2.shares == 1000 and z2.limited_by == "risk" and abs(z2.risk_pct - 0.01) < 1e-9
    z3 = sizing.position_size(100_000, 10.0, 9.9, cash=5_000)     # 现金只够 500 股
    assert z3.shares == 500 and z3.limited_by == "cash"
    z4 = sizing.position_size(10_000, 200.0, 180.0)               # 一手 2 万元，超过单只上限
    assert z4.shares == 0 and "买不了" in z4.note
    z5 = sizing.position_size(100_000, 50.0, 30.0)                # 一手风险 2000 > 1000
    assert z5.shares == 0 and "风险" in z5.note
    assert sizing.position_size(100_000, 10.0, 10.5).shares == 0
    assert sizing.lot_size("688001") == 200 and sizing.lot_size("600000") == 100


# ---------------------------------------------------------------- 方案校验

def test_presets_valid_and_errors() -> None:
    for p in schemes.PRESETS:
        v = schemes.validate_scheme(p)
        assert v["top_n"] >= 1 and v["scoring"]["scheme"] in schemes.SCORING
    with pytest.raises(ValueError, match="不认识的字段"):
        schemes.validate_scheme({"conditions": [{"type": "field", "field": "xx", "op": ">", "value": 1}]})
    with pytest.raises(ValueError, match="两个数"):
        schemes.validate_scheme({"conditions": [{"type": "field", "field": "pe_ttm", "op": "between", "value": 5}]})
    with pytest.raises(ValueError, match="至少要给一个因子"):
        schemes.validate_scheme({"scoring": {"scheme": "custom", "weights": {}}})
    with pytest.raises(ValueError, match="至少要选一个板块"):
        schemes.validate_scheme({"universe": {"boards": ["zz"]}})


# ---------------------------------------------------------------- 引擎（假日线）

def fake_panel(n_codes: int = 30, n_days: int = 520, seed: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    frames = []
    start = date(2024, 6, 3)
    codes = [f"600{k:03d}" for k in range(n_codes - 3)] + ["300001", "688001", "000001"]
    for i, code in enumerate(codes):
        drift = 0.0015 if i % 3 == 0 else -0.0005
        c = 10 * np.exp(np.cumsum(rng.normal(drift, 0.02, n_days)))
        o = np.concatenate([[c[0]], c[:-1]]) * np.exp(rng.normal(0, 0.003, n_days))
        v = rng.integers(20, 200, n_days) * 1e5
        frames.append(pl.DataFrame({
            "date": [start + timedelta(days=d) for d in range(n_days)], "code": [code] * n_days, "open": o,
            "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99, "close": c,
            "preclose": np.concatenate([[c[0]], c[:-1]]), "volume": v, "amount": v * c, "turn": np.full(n_days, 3.0),
            "tradestatus": [1] * n_days, "is_st": [False] * n_days,
        }))
    return pl.concat(frames)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pl.DataFrame:
    df = fake_panel()
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "STOCK_LAB", tmp_path / "stock_lab")
    monkeypatch.setattr(config, "PANEL_DIR", tmp_path / "stock_lab" / "daily")
    hist = types.ModuleType("quant_web.market.history")

    def load_panel(codes=None, start=None, end=None, columns=None, **kw):
        out = df
        if codes:
            out = out.filter(pl.col("code").is_in(codes))
        if start is not None:
            out = out.filter(pl.col("date") >= start)
        if end is not None:
            out = out.filter(pl.col("date") <= end)
        return out

    hist.load_panel = load_panel
    hist.last_date = lambda: df["date"].max()
    uni = types.ModuleType("quant_web.market.universe")
    uni.load_universe = lambda: pl.DataFrame({"code": df["code"].unique(maintain_order=True),
                                              "name": [f"股{i}" for i in range(df["code"].n_unique())],
                                              "industry": ["行业甲"] * df["code"].n_unique(),
                                              "list_date": [date(2010, 1, 1)] * df["code"].n_unique()})
    fund = types.ModuleType("quant_web.market.fundamentals")
    fund.load_fundamentals = lambda: pl.DataFrame({
        "code": df["code"].unique(maintain_order=True), "report_date": [date(2025, 6, 30)] * df["code"].n_unique(),
        "avail_date": [date(2025, 8, 30)] * df["code"].n_unique(), "eps_ttm": [0.8] * df["code"].n_unique(),
        "bvps": [5.0] * df["code"].n_unique(), "roe": [12.0] * df["code"].n_unique(),
        "net_profit_yoy": [20.0] * df["code"].n_unique(), "revenue_yoy": [10.0] * df["code"].n_unique(),
        "debt_ratio": [40.0] * df["code"].n_unique(), "net_profit_ttm": [1e8] * df["code"].n_unique()})
    for name, m in (("history", hist), ("universe", uni), ("fundamentals", fund)):
        monkeypatch.setitem(sys.modules, f"quant_web.market.{name}", m)
        monkeypatch.setattr(quant_web.market, name, m, raising=False)
    return df


def test_engine_run_main_board_only_and_sizing(env: pl.DataFrame) -> None:
    sch = {"name": "测试", "universe": {"boards": None, "min_amount": 0}, "conditions": [{"type": "field", "field": "pos250", "op": ">=", "value": 0}],
           "scoring": {"scheme": "momentum"}, "top_n": 10}
    res = engine.run(sch, profile={"boards": ["main"], "capital": 100_000, "risk_per_trade": 0.01}, risk={"max_single_pct": 0.2})
    assert res["rows"] and all(r["board"] == "main" for r in res["rows"])            # 创业板/科创板不出现
    scores = [r["score"] for r in res["rows"]]
    assert scores == sorted(scores, reverse=True)
    for r in res["rows"]:
        assert r["stop"] < r["close"] and r["shares"] % 100 == 0 and r["amount"] <= 20_000 + 1e-6
    res2 = engine.run(sch, profile={"boards": ["main", "chinext", "star"]}, risk={})
    assert {r["board"] for r in res2["rows"]} >= {"main"} and res2["universe_n"] > res["universe_n"]
    res3 = engine.run({**sch, "conditions": [{"type": "formula", "text": "XG:C>REF(C,1)*1.5;"}]}, profile={"boards": ["main"]})
    assert res3["matched_n"] == 0 and res3["rows"] == []


def test_store_schemes_and_results(env: pl.DataFrame) -> None:
    s = store.save_mine({"name": "我的", "conditions": [], "scoring": {"scheme": "value"}})
    assert s["id"].startswith("my_") and store.get(s["id"])["name"] == "我的"
    assert len(store.all_schemes()) == len(schemes.PRESETS) + 1
    store.save_result(s["id"], {"date": "2026-09-24", "rows": []})
    assert store.latest_result(s["id"])["date"] == "2026-09-24"
    assert store.delete_mine(s["id"]) and not store.delete_mine(s["id"])
    with pytest.raises(ValueError):
        store.get("nope")


def test_backtest_runs_and_everything_equals_baseline(env: pl.DataFrame) -> None:
    sch_all = {"name": "全买", "universe": {"boards": ["main"], "min_amount": 0}, "conditions": [], "scoring": {"scheme": "momentum"},
               "top_n": 200, "risk": {"exclude_red": False}}
    res = backtest.run(sch_all, hold=5, profile={"boards": ["main"]}, use_chips=False)
    assert res["n_periods"] > 10 and res["curve"]
    assert abs(res["stats"]["all"]["excess"]) < 1e-9                              # 全部买入 = 基准本身
    sch = {**sch_all, "top_n": 3}
    res2 = backtest.run(sch, hold=5, profile={"boards": ["main"]}, use_chips=False)
    assert res2["verdict"]["text"] and set(res2["stats"]) == {"selection", "holdout", "all"}
    assert (config.WORKSPACE / "screener" / "history" / "table_nochips.parquet").exists()     # 历史特征已缓存


# ---------------------------------------------------------------- 接口

def test_screener_api(env: pl.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from quant_web import jobs
    from quant_web.api import server
    from quant_web.api.routes import screener as sroute
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("QUANT_WEB_NO_SCHEDULER", "1")
    sroute._RUN_CACHE.clear()
    with TestClient(server.create_app(background=False)) as c:
        meta = c.get("/api/screener/schemes").json()
        assert {s["id"] for s in meta["schemes"]} >= {p["id"] for p in schemes.PRESETS}
        assert "rps120" in meta["fields"] and "momentum" in meta["scoring"] and meta["profile_boards"] == ["main"]
        r = c.post("/api/screener/run", json={"scheme": {"name": "临时", "universe": {"min_amount": 0}, "conditions": [],
                                                           "scoring": {"scheme": "momentum"}, "top_n": 5}})
        assert r.status_code == 200, r.text
        assert len(r.json()["rows"]) == 5
        bad = c.post("/api/screener/run", json={"scheme": {"conditions": [{"type": "field", "field": "nope", "op": ">", "value": 1}]}})
        assert bad.status_code == 400
        saved = c.post("/api/screener/schemes", json={"scheme": {"name": "我的方案", "conditions": [], "scoring": {"scheme": "value"}}}).json()
        assert saved["id"].startswith("my_")
        r2 = c.post("/api/screener/run", json={"scheme_id": saved["id"]}).json()
        assert c.get(f"/api/screener/latest/{saved['id']}").json()["date"] == r2["date"]
        b = c.post("/api/screener/backtest", json={"scheme_id": saved["id"], "hold": 5}).json()
        job = jobs.JOBS.wait(b["job_id"], timeout=120)
        assert job and job["status"] == "done", job
        assert c.get(f"/api/screener/backtest/{b['key']}").json()["hold"] == 5
        again = c.post("/api/screener/backtest", json={"scheme_id": saved["id"], "hold": 5}).json()
        assert again["result"]["key"] == b["key"]
        assert c.delete(f"/api/screener/schemes/{saved['id']}").status_code == 200
        assert c.delete("/api/screener/schemes/trend_swing").status_code == 404
