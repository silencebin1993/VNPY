"""quant_web 第三版：新设置分组、白名单后台任务、数据源接口（全部离线，用假数据源）"""
import json
import sys
import types
import warnings
from pathlib import Path

import polars as pl
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from quant_web import config, jobs, tasks
from quant_web import settings as settings_mod
from quant_web.api import server
from quant_web.providers import base as pbase
from quant_web.providers import store


# ================================================================ 夹具
def install_fake(monkeypatch: pytest.MonkeyPatch, name: str, module: types.ModuleType) -> None:
    """用假模块替换 quant_web.market.<name>：sys.modules 和父包属性都要换（from ..market import x 先看父包属性）"""
    import quant_web.market
    monkeypatch.setitem(sys.modules, f"quant_web.market.{name}", module)
    monkeypatch.setattr(quant_web.market, name, module, raising=False)



@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 workspace（绝不碰真实目录）"""
    stock_lab: Path = tmp_path.joinpath("stock_lab")
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "STOCK_LAB", stock_lab)
    monkeypatch.setattr(config, "PANEL_DIR", stock_lab.joinpath("daily"))
    monkeypatch.setattr(config, "UNIVERSE_FILE", stock_lab.joinpath("universe.parquet"))
    monkeypatch.setattr(config, "POOLS_DIR", stock_lab.joinpath("pools"))
    monkeypatch.setattr(config, "MODEL_DIR", tmp_path.joinpath("models"))
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path.joinpath("cache"))
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path.joinpath("settings.json"))
    monkeypatch.setattr(config, "WATCHLIST_FILE", tmp_path.joinpath("watchlist.json"))
    monkeypatch.setenv("QUANT_WEB_NO_SCHEDULER", "1")
    config.ensure_dirs()
    server.CACHE.clear()
    return tmp_path


class FakeDaily(pbase.DataProvider):
    name = "fake_a"
    label = "假源A"
    capabilities = ("daily_bars",)

    def fetch_daily_bars(self, code: str, start=None, end=None) -> pl.DataFrame:
        return pl.DataFrame({"date": ["2026-09-23", "2026-09-24"], "code": [code, code], "close": [10.0, 10.5]})


class FakeBroken(pbase.DataProvider):
    name = "fake_b"
    label = "假源B"
    capabilities = ("daily_bars", "margin")

    def fetch_daily_bars(self, code: str, start=None, end=None) -> pl.DataFrame:
        raise ConnectionError("连不上")

    def fetch_margin(self, day=None) -> pl.DataFrame:
        return pl.DataFrame()


class FakeOptional(pbase.DataProvider):
    name = "fake_c"
    label = "假源C"
    optional = True
    capabilities = ("daily_bars",)

    def available(self) -> tuple[bool, str]:
        return False, "没有填写 token"

    def fetch_daily_bars(self, **kwargs) -> pl.DataFrame:
        raise AssertionError("不可用的数据源不应被调用")


@pytest.fixture()
def reg(ws: Path, monkeypatch: pytest.MonkeyPatch) -> pbase.ProviderRegistry:
    r = pbase.ProviderRegistry()
    for p in (FakeDaily(), FakeBroken(), FakeOptional()):
        r.register(p)
    monkeypatch.setattr(pbase, "_REGISTRY", r)
    monkeypatch.setattr(pbase, "DEFAULT_CHAINS", {"daily_bars": ["fake_b", "fake_a"]})
    return r


@pytest.fixture()
def client(ws: Path) -> TestClient:
    with TestClient(server.create_app(background=False)) as c:
        yield c


# ================================================================ 设置

def test_new_settings_defaults_follow_user_answers() -> None:
    s = settings_mod.Settings()
    assert s.profile.boards == ["main"] and s.profile.horizon == "swing" and s.profile.watch_time == "evening"
    assert s.profile.risk_per_trade == 0.01 and s.profile.onboarded is False
    assert s.live.enabled is False and s.live.auto_policy == "stop_only" and s.live.broker == "manual"
    assert s.risk.max_single_pct == 0.2 and s.risk.require_stop is True


def test_settings_put_profile_and_validation(client: TestClient) -> None:
    r = client.put("/api/settings", json={"profile": {"capital": 50000, "boards": ["chinext", "main"], "onboarded": True}})
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["capital"] == 50000 and body["profile"]["boards"] == ["main", "chinext"]
    assert body["predict"]["kind"] == "streak"          # 旧分组不受影响
    bad = client.put("/api/settings", json={"profile": {"boards": []}})
    assert bad.status_code == 400 and "至少要选一个板块" in bad.json()["detail"]
    bad2 = client.put("/api/settings", json={"notify": {"quiet_start": "25:99"}})
    assert bad2.status_code == 400 and "时:分" in bad2.json()["detail"]


def test_load_keeps_valid_new_sections_when_other_part_is_broken(ws: Path) -> None:
    raw = {"profile": {"capital": 88888, "onboarded": True}, "risk": {"max_single_pct": 99}, "live": {"broker": "qmt"}}
    config.SETTINGS_FILE.write_text(json.dumps(raw), encoding="utf-8")
    s = settings_mod.load()
    assert s.profile.capital == 88888 and s.profile.onboarded is True     # 好的部分保留
    assert s.risk.max_single_pct == 0.2                                  # 坏的部分恢复默认
    assert s.live.broker == "qmt"


# ================================================================ 后台任务白名单

def test_tasks_listing_and_unknown_task(client: TestClient) -> None:
    names = [t["name"] for t in client.get("/api/tasks").json()["tasks"]]
    assert "ext_update" in names
    r = client.post("/api/jobs/run", json={"name": "rm_rf"})
    assert r.status_code == 400 and "没有「rm_rf」这个后台任务" in r.json()["detail"]


def test_jobs_run_ext_update_uses_updates_module(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    fake = types.ModuleType("quant_web.providers.updates")

    def update_all(progress=None, flow_codes=None) -> dict:
        calls.append(flow_codes)
        if progress:
            progress(0.5, "一半")
        return {"margin": {"ok": True, "rows": 3}}

    fake.update_all = update_all
    monkeypatch.setitem(sys.modules, "quant_web.providers.updates", fake)
    import quant_web.providers as pkg
    monkeypatch.setattr(pkg, "updates", fake, raising=False)
    r = client.post("/api/jobs/run", json={"name": "ext_update", "params": {"codes": ["600519"]}})
    assert r.status_code == 200
    job = jobs.JOBS.wait(r.json()["job_id"], timeout=10)
    assert job and job["status"] == "done", job
    assert calls == [["600519"]]


def test_job_name_distinguishes_params() -> None:
    assert tasks.job_name("x", {}) == "x"
    assert tasks.job_name("x", {"b": 1, "a": 2}) == tasks.job_name("x", {"a": 2, "b": 1})
    assert tasks.job_name("x", {"a": 1}) != tasks.job_name("x", {"a": 2})


# ================================================================ 数据源接口

def test_registry_fallback_and_sources(reg: pbase.ProviderRegistry) -> None:
    res = reg.fetch("daily_bars", code="600519")
    assert res.source == "fake_a" and res.data.height == 2
    assert res.data.schema["date"] == pl.Date and res.data.schema["close"] == pl.Float64
    assert set(res.data.columns) == set(pbase.SCHEMAS["daily_bars"])    # 统一格式：缺的列补空
    assert [n for n, _ in res.tried] == ["假源B"]                        # 不可用的 C 没有排进默认顺序
    empty = reg.fetch("margin", day=None)                                # 所有源都只给空数据：不报错
    assert empty.data.height == 0
    with pytest.raises(pbase.ProviderError, match="所有数据源都取不到"):
        reg.fetch("daily_bars", chain=["fake_b"], code="600519")


def test_providers_api_status_chain_probe(client: TestClient, reg: pbase.ProviderRegistry) -> None:
    body = client.get("/api/providers").json()
    daily = next(c for c in body["capabilities"] if c["capability"] == "daily_bars")
    assert daily["chain"] == ["fake_b", "fake_a", "fake_c"] and daily["customized"] is False
    c_src = next(s for s in daily["sources"] if s["name"] == "fake_c")
    assert c_src["available"] is False and "token" in c_src["reason"]
    assert any(p["name"] == "fake_c" and p["optional"] for p in body["providers"])
    assert {a["name"] for a in body["archives"]} >= {"margin", "fund_flow", "holder_count"}

    r = client.put("/api/providers/chain", json={"capability": "daily_bars", "chain": ["fake_a", "fake_b"]})
    assert r.status_code == 200
    daily = next(c for c in r.json()["capabilities"] if c["capability"] == "daily_bars")
    assert daily["chain"] == ["fake_a", "fake_b"] and daily["customized"] is True
    assert settings_mod.load().providers.chains == {"daily_bars": ["fake_a", "fake_b"]}
    assert reg.fetch("daily_bars", code="000001").tried == []           # 用户顺序生效：A 第一个就成功

    bad = client.put("/api/providers/chain", json={"capability": "daily_bars", "chain": ["nope"]})
    assert bad.status_code == 400
    bad_cap = client.put("/api/providers/chain", json={"capability": "xx", "chain": []})
    assert bad_cap.status_code == 400
    reset = client.put("/api/providers/chain", json={"capability": "daily_bars", "chain": []})
    daily = next(c for c in reset.json()["capabilities"] if c["capability"] == "daily_bars")
    assert daily["customized"] is False and settings_mod.load().providers.chains == {}

    p = client.post("/api/providers/probe", json={"capability": "daily_bars", "provider": "fake_a"}).json()
    assert p["ok"] is True and p["rows"] == 2 and p["sample"][0]["code"] == "600519"
    p2 = client.post("/api/providers/probe", json={"capability": "daily_bars", "provider": "fake_b"}).json()
    assert p2["ok"] is False and "连不上" in p2["error"]


def test_archive_preview(client: TestClient, reg: pbase.ProviderRegistry) -> None:
    assert client.get("/api/providers/archive/nope").status_code == 404
    store.save("margin", pl.DataFrame({"date": ["2026-09-23", "2026-09-24"], "code": ["600519", "000001"],
                                       "rzye": [1.0, 2.0]}).with_columns(pl.col("date").str.to_date()))
    body = client.get("/api/providers/archive/margin", params={"code": "600519"}).json()
    assert body["rows"] == 1 and body["data"][0]["code"] == "600519"


def test_store_merge_and_replace(ws: Path) -> None:
    df1 = pl.DataFrame({"index": ["hs300", "hs300"], "code": ["600519", "000001"], "name": ["茅台", "平安"]})
    assert store.save("index_members", df1) == 2
    df2 = pl.DataFrame({"index": ["hs300"], "code": ["600036"], "name": ["招行"]})
    assert store.save("index_members", df2, replace_where=pl.col("index") == "hs300") == 1
    df3 = pl.DataFrame({"index": ["zz500"], "code": ["000001"], "name": ["平安"]})
    assert store.save("index_members", df3) == 2
    info = {i["name"]: i for i in store.info()}
    assert info["index_members"]["rows"] == 2 and info["index_members"]["codes"] == 2


# ================================================================ 图表与指标接口

def _fake_bars(n: int = 80) -> list[dict]:
    import math
    out = []
    for i in range(n):
        c = 10 + 2 * math.sin(i / 7) + i * 0.02
        out.append({"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": c * 0.99, "close": c, "high": c * 1.02,
                    "low": c * 0.98, "volume": 1e6 + i * 1e4, "amount": c * 1e6, "turnover": 2.0})
    return out


def _fake_panel(n: int = 80) -> pl.DataFrame:
    bars = _fake_bars(n)
    df = pl.DataFrame(bars).with_columns(pl.col("date").str.to_date(), pl.lit("600519").alias("code"),
                                         pl.col("turnover").alias("turn"))
    return df.with_columns(pl.col("close").shift(1).fill_null(pl.col("close")).alias("preclose")).drop("turnover")


@pytest.fixture()
def chart_env(ws: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"fail": False, "calls": 0}

    def kline(code, period="day", adjust="qfq", count=600):
        state["calls"] += 1
        if state["fail"]:
            raise ConnectionError("腾讯连不上")
        return _fake_bars()[-count:]

    rt = types.ModuleType("quant_web.market.realtime")
    rt.kline = kline
    rt.market_phase = lambda: "休市"
    install_fake(monkeypatch, "realtime", rt)
    hist = types.ModuleType("quant_web.market.history")
    hist.load_panel = lambda codes=None, columns=None, **kw: _fake_panel()
    hist.last_date = lambda: None
    install_fake(monkeypatch, "history", hist)
    return state


def test_indicator_catalog_api(client: TestClient) -> None:
    body = client.get("/api/indicators/catalog").json()
    ids = {x["id"] for x in body["items"]}
    assert {"ma", "macd", "kdj", "vol", "updown"} <= ids and body["cats"]


def test_chart_api_indicators_and_chips(client: TestClient, chart_env: dict) -> None:
    r = client.get("/api/stock/600519/chart", params={"ind": "ma,vol,macd,kdj", "params": '{"ma":{"n1":3}}'})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["source"] == "realtime" and b["adjust"] == "qfq" and len(b["bars"]) == 80
    assert set(b["indicators"]) == {"ma", "vol", "macd", "kdj"}
    assert b["indicators"]["ma"]["lines"][0]["label"] == "MA3"
    assert all(len(ln["data"]) == 80 for ln in b["indicators"]["macd"]["lines"])
    ch = b["chips"]
    assert ch["source"] == "panel" and len(ch["series"]["winner"]) == 80 and ch["dist"]["prices"]
    assert 0 <= ch["last"]["winner"] <= 1 and ch["note"]


def test_chart_api_falls_back_to_local_panel(client: TestClient, chart_env: dict) -> None:
    chart_env["fail"] = True
    b = client.get("/api/stock/600519/chart", params={"ind": "ma", "chips": "false"}).json()
    assert b["source"] == "panel" and b["chips"] is None
    assert any("本地日线" in w for w in b["warnings"])
    assert b["bars"][-1]["close"] == pytest.approx(_fake_bars()[-1]["close"])      # 前复权以最新价为锚


def test_chart_api_rejects_bad_input(client: TestClient, chart_env: dict) -> None:
    assert client.get("/api/stock/600519/chart", params={"ind": "nope"}).status_code == 400
    assert client.get("/api/stock/600519/chart", params={"params": "[1,2]"}).status_code == 400
    assert client.get("/api/stock/600519/chart", params={"period": "hour"}).status_code == 400
    assert client.get("/api/stock/12345/chart").status_code == 400


# ================================================================ 公式接口

def _formula_panel() -> pl.DataFrame:
    import numpy as np
    from datetime import date, timedelta
    rng = np.random.default_rng(5)
    frames = []
    for k in range(4):
        n = 320
        c = 10 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
        frames.append(pl.DataFrame({
            "date": [date(2025, 1, 1) + timedelta(days=i) for i in range(n)], "code": [f"60000{k}"] * n,
            "open": c * 0.995, "high": c * 1.01, "low": c * 0.99, "close": c, "preclose": np.concatenate([[c[0]], c[:-1]]),
            "volume": np.full(n, 1e6), "amount": c * 1e6, "turn": np.full(n, 2.0), "tradestatus": [1] * n,
            "is_st": [False] * n,
        }))
    return pl.concat(frames)


@pytest.fixture()
def formula_env(ws: Path, monkeypatch: pytest.MonkeyPatch) -> pl.DataFrame:
    df = _formula_panel()
    hist = types.ModuleType("quant_web.market.history")

    def load_panel(codes=None, start=None, end=None, columns=None, **kw):
        out = df
        if codes:
            out = out.filter(pl.col("code").is_in(codes))
        if start is not None:
            out = out.filter(pl.col("date") >= start)
        return out

    hist.load_panel = load_panel
    hist.last_date = lambda: df["date"].max()
    install_fake(monkeypatch, "history", hist)
    uni = types.ModuleType("quant_web.market.universe")
    uni.load_universe = lambda: pl.DataFrame({"code": [f"60000{k}" for k in range(4)], "name": ["甲", "乙", "丙", "丁"]})
    uni.is_a_share = lambda code: True
    install_fake(monkeypatch, "universe", uni)
    return df


def test_formula_library_check_and_mine(client: TestClient, formula_env: pl.DataFrame) -> None:
    lib = client.get("/api/formula/library").json()
    assert len(lib["items"]) >= 24 and "funcs" in lib["reference"] and "ZIG" in lib["reference"]["future"]
    bad = client.post("/api/formula/check", json={"text": "XG:ZIG(C,5)>C;"}).json()
    assert bad["ok"] is False and "未来函数" in bad["error"]
    good = client.post("/api/formula/check", json={"text": "A:MA(C,5);XG:C>A;"}).json()
    assert good["ok"] and good["outputs"] == ["A", "XG"] and good["condition"] == "XG"
    saved = client.post("/api/formula/mine", json={"name": "我的", "text": "XG:C>MA(C,10);"}).json()
    assert saved["id"].startswith("my_")
    assert client.post("/api/formula/mine", json={"name": "坏的", "text": "XG:ZIG(C,3);"}).status_code == 400
    assert [m["name"] for m in client.get("/api/formula/library").json()["mine"]] == ["我的"]
    assert client.delete(f"/api/formula/mine/{saved['id']}").status_code == 200
    assert client.delete(f"/api/formula/mine/{saved['id']}").status_code == 404


def test_formula_preview_scan_and_validate(client: TestClient, formula_env: pl.DataFrame) -> None:
    p = client.post("/api/formula/preview", json={"code": "600000", "text": "M:MA(C,5);XG:C>M;"}).json()
    assert isinstance(p["series"], dict) and len(p["series"]["M"]) == len(p["dates"]) == 320
    assert p["outputs"] == ["M", "XG"] and p["signals"]
    s = client.post("/api/formula/scan", json={"text": "XG:C>0;", "boards": ["main"]}).json()
    assert s["total"] == 4 and {r["name"] for r in s["rows"]} == {"甲", "乙", "丙", "丁"}
    r = client.post("/api/formula/validate", json={"text": "XG:C>REF(C,1);", "holds": [5]}).json()
    job = jobs.JOBS.wait(r["job_id"], timeout=60)
    assert job and job["status"] == "done", job
    res = client.get(f"/api/formula/result/{r['key']}").json()
    assert "5" in res["holds"] and res["params"]["holds"] == [5]
    again = client.post("/api/formula/validate", json={"text": "XG:C>REF(C,1);", "holds": [5]}).json()
    assert again["result"]["key"] == r["key"]                           # 相同公式+参数直接返回缓存结果
    assert client.get("/api/formula/result/zzzz").status_code == 404
