"""quant_web 后端：API 路由返回结构、设置校验、后台任务、自动更新调度、稳健ETF接口（全部离线）"""
import json
import shutil
import sys
import threading
import time
import types
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

import quant_web.market
import quant_web.predict
from quant_web import config, jobs, paper, scheduler
from quant_web import settings as settings_mod
from quant_web.api import etf as etf_api
from quant_web.api import server
from quant_web.market.history import PANEL_SCHEMA
from quant_web.market.universe import SCHEMA as UNI_SCHEMA
from quant_web.predict.limits import limit_price


ETF_LAB: Path = Path(__file__).resolve().parents[1].joinpath("etf_quant", "workspace", "lab")
LAST: date = date(2026, 9, 24)


# ================================================================ 公共夹具

def install(monkeypatch: pytest.MonkeyPatch, name: str, module: types.ModuleType | None) -> None:
    """用假模块替换 quant_web.<name>（sys.modules 和父包属性都要换）"""
    full: str = f"quant_web.{name}"
    monkeypatch.setitem(sys.modules, full, module)
    parent: types.ModuleType = quant_web.market if name.startswith("market.") else quant_web.predict
    monkeypatch.setattr(parent, name.split(".")[1], module, raising=False)


def fake_module(name: str, **attrs: object) -> types.ModuleType:
    m = types.ModuleType(f"quant_web.{name}")
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def trading_days(n: int, end: date = LAST) -> list[date]:
    days: list[date] = []
    d: date = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def make_panel(days: list[date]) -> pl.DataFrame:
    """三只股票：600001 最后两天连板，300003 最后一天 20cm 涨停，000002 横盘"""
    rows: list[dict] = []
    for code, base in (("600001", 10.0), ("000002", 5.0), ("300003", 20.0)):
        close: float = base
        for i, d in enumerate(days):
            pre: float = close
            if code == "600001" and i >= len(days) - 2:
                close = limit_price(pre, 0.1)
            elif code == "300003" and i == len(days) - 1:
                close = limit_price(pre, 0.2)
            else:
                close = round(pre * (1.01 if i % 2 else 0.99), 2)
            rows.append({
                "date": d, "code": code, "open": pre, "high": max(pre, close), "low": min(pre, close),
                "close": close, "preclose": pre, "volume": 1e6, "amount": 1e6 * close, "turn": 2.5,
                "tradestatus": 1, "is_st": False, "source": "tx",
            })
    return pl.DataFrame(rows, schema=PANEL_SCHEMA)


def make_universe() -> pl.DataFrame:
    return pl.DataFrame([
        {"code": "600001", "name": "测试一号", "exchange": "SSE", "board": "main", "list_date": date(2010, 1, 4),
         "delist_date": None, "is_st": False, "industry": "制造业", "status": 1},
        {"code": "000002", "name": "测试二号", "exchange": "SZSE", "board": "main", "list_date": date(2010, 1, 4),
         "delist_date": None, "is_st": False, "industry": "金融业", "status": 1},
        {"code": "300003", "name": "创业三号", "exchange": "SZSE", "board": "chinext", "list_date": date(2015, 1, 5),
         "delist_date": None, "is_st": False, "industry": "制造业", "status": 1},
    ], schema=UNI_SCHEMA)


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 workspace（绝不碰真实目录）+ 临时静态文件；假的情绪/服务模块默认不可用"""
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
    static: Path = tmp_path.joinpath("static")
    static.joinpath("vendor").mkdir(parents=True)
    static.joinpath("index.html").write_text("<html>量化助手首页</html>", encoding="utf-8")
    static.joinpath("app.js").write_text("console.log('app')", encoding="utf-8")
    static.joinpath("vendor", "lib.js").write_text("var lib = 1;", encoding="utf-8")
    monkeypatch.setattr(config, "STATIC_DIR", static)
    monkeypatch.setenv("QUANT_WEB_NO_SCHEDULER", "1")
    config.ensure_dirs()
    server.CACHE.clear()
    phase = fake_module("market.realtime", market_phase=lambda: "休市", index_quotes=lambda: [])
    install(monkeypatch, "market.realtime", phase)
    return tmp_path


@pytest.fixture()
def panel_ws(ws: Path) -> Path:
    make_panel(trading_days(30)).write_parquet(config.PANEL_DIR.joinpath("2026.parquet"))
    make_universe().write_parquet(config.UNIVERSE_FILE)
    server.CACHE.clear()
    return ws


@pytest.fixture()
def client(ws: Path) -> TestClient:
    with TestClient(server.create_app(background=False)) as c:
        yield c


def write_meta(kind: str = "streak") -> dict:
    meta: dict = {
        "kind": kind, "trained_at": "2026-09-20T18:00:00", "data_start": "2019-01-02", "data_end": "2026-09-19",
        "n_samples": 12345, "base_rate": 0.25, "feature_cols": ["a", "b"],
        "folds": [{"auc": 0.6, "top5_hit": 0.4}, {"auc": 0.7, "top5_hit": 0.5}],
        "oos": {"auc": 0.65, "top5_hit": 0.45, "top1_hit": 0.5},
        "calibration": [{"bucket": "0-10%", "pred": 0.05, "actual": 0.04, "n": 100}],
        "importance": {"sentiment": 0.3, "capital": 0.7},
    }
    config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    config.MODEL_DIR.joinpath(f"{kind}_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def fake_sentiment_module() -> types.ModuleType:
    def daily_sentiment(lim: pl.DataFrame) -> pl.DataFrame:
        stats = lim.group_by("date").agg(
            pl.len().alias("n_stocks"), pl.col("is_limit_up").sum().alias("n_limit_up"),
        ).sort("date")
        return stats.with_columns(pl.lit(55.0).alias("temperature"), pl.lit(float("nan")).alias("break_rate"))

    return fake_module(
        "market.sentiment",
        daily_sentiment=daily_sentiment,
        temperature_label=lambda t: "正常" if 40 <= t < 60 else "其他",
        live_sentiment=lambda: {"date": "2026-09-25", "n_limit_up": 60, "temperature": 70.0, "as_of": "2026-09-25 10:30:00"},
    )


# ================================================================ JSON 转换与错误

def test_jsonable_handles_common_types() -> None:
    @dataclass
    class Row:
        day: date
        value: float

    df = pl.DataFrame({"date": [date(2026, 9, 24)], "x": [float("nan")], "n": [3]})
    obj = {
        "df": df, "series": pl.Series([1.5, None]), "np_int": np.int64(7), "np_nan": np.float64("nan"),
        "arr": np.array([1, 2]), "dt": datetime(2026, 9, 24, 15, 0, 1), "dec": Decimal("1.25"),
        "path": Path("a/b"), "row": Row(date(2026, 1, 2), float("inf")), "tuple": (1, np.bool_(True)),
        "settings": settings_mod.Settings(),
    }
    out = server.jsonable(obj)
    text: str = json.dumps(out, allow_nan=False, ensure_ascii=False)
    assert out["df"] == [{"date": "2026-09-24", "x": None, "n": 3}]
    assert out["np_int"] == 7 and out["np_nan"] is None and out["arr"] == [1, 2]
    assert out["dt"] == "2026-09-24 15:00:01" and out["dec"] == 1.25
    assert out["row"] == {"day": "2026-01-02", "value": None}
    assert out["tuple"] == [1, True] and out["settings"]["predict"]["top_n"] == 5
    assert "NaN" not in text


def test_describe_error_is_chinese() -> None:
    assert server.describe_error(ValueError("参数不对")) == (400, "参数不对")
    assert server.describe_error(RuntimeError("状态不对"))[0] == 409
    assert server.describe_error(ConnectionError("x"))[0] == 502
    status, detail = server.describe_error(ModuleNotFoundError("no", name="quant_web.market.info"))
    assert status == 503 and "模块" in detail
    status, detail = server.describe_error(KeyError("boom"))
    assert status == 500 and detail.startswith("服务器内部出错")
    assert server.describe_error(settings_mod.SettingsError("坏了")) == (400, "坏了")


# ================================================================ 状态 / 静态文件

def test_status_shape(client: TestClient, panel_ws: Path) -> None:
    write_meta("streak")
    r = client.get("/api/status")
    assert r.status_code == 200
    data: dict = r.json()
    for key in ("now", "phase", "panel", "models", "jobs", "settings_risk_ack"):
        assert key in data
    assert data["phase"] == "休市"
    assert data["panel"]["stocks"] == 3 and data["panel"]["end"] == LAST.isoformat()
    assert data["panel"]["rows"] == 90 and data["first_run"] is False
    assert data["models"]["first"] is None
    streak: dict = data["models"]["streak"]
    assert streak["trained_at"] and streak["auc"] == 0.65 and streak["top5_hit"] == 0.45
    assert streak["label"] == "连板晋级" and "feature_cols" not in streak
    assert isinstance(data["jobs"], list) and data["settings_risk_ack"] is False
    assert "swing" in data["models"] and data["models"]["swing"] is None

    meta: dict = write_meta("swing")
    meta["trade_oos"] = {"n": 1200, "mean": 0.012, "win_rate": 0.53, "daily_t": 2.1,
                         "holdout": {"mean": 0.01}, "by_year": [{"year": 2025, "mean": 0.01}]}
    config.MODEL_DIR.joinpath("swing_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    swing: dict = client.get("/api/status").json()["models"]["swing"]
    assert swing["label"] == "强势股波段" and swing["trade_oos"]["mean"] == 0.012
    assert swing["trade_oos"]["holdout"] == {"mean": 0.01} and "by_year" not in swing["trade_oos"]


def test_status_empty_panel(client: TestClient) -> None:
    data: dict = client.get("/api/status").json()
    assert data["panel"]["empty"] is True and data["first_run"] is True and data["panel"]["stocks"] == 0


def test_static_files_and_404(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "量化助手首页" in r.text
    assert r.headers["cache-control"] == "no-cache"
    r = client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    r = client.get("/static/vendor/lib.js")
    assert r.status_code == 200 and "max-age" in r.headers["cache-control"]
    r = client.get("/app.js")           # index.html 用相对路径引用时
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    r = client.get("/api/not-exist")
    assert r.status_code == 404 and r.json()["detail"] == "没有这个接口"
    r = client.get("/../secret.txt")
    assert r.status_code == 404


def test_gzip(client: TestClient) -> None:
    settings_mod.save(settings_mod.Settings())
    r = client.get("/api/settings/presets", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"


# ================================================================ 设置

def test_settings_get_put_validation(client: TestClient) -> None:
    data: dict = client.get("/api/settings").json()
    assert data["predict"]["kind"] == "streak" and data["trade"]["capital"] == 100000
    assert set(data["predict"]["weights"]) == set(settings_mod.GROUPS)
    assert data["saved"] is False and not config.SETTINGS_FILE.exists()     # 没保存过：返回的是默认值

    r = client.put("/api/settings", json={"predict": {"top_n": 3, "weights": {"capital": 1.5}}})
    assert r.status_code == 200
    assert r.json()["predict"]["top_n"] == 3 and r.json()["predict"]["weights"]["capital"] == 1.5
    assert r.json()["predict"]["weights"]["sentiment"] == 1.0
    saved: dict = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert saved["predict"]["top_n"] == 3 and "saved" not in saved             # saved 只是接口字段，不写进文件
    assert r.json()["saved"] is True and client.get("/api/settings").json()["saved"] is True

    r = client.put("/api/settings", json={"risk_ack": True})
    assert r.status_code == 200 and r.json()["risk_ack"] is True and r.json()["predict"]["top_n"] == 3
    assert client.get("/api/status").json()["settings_risk_ack"] is True

    bad_cases: list[tuple[dict, str]] = [
        ({"predict": {"weights": {"sentiment": 3}}}, "情绪面"),
        ({"trade": {"capital": 10}}, "初始资金"),
        ({"predict": {"min_price": 50, "max_price": 10}}, "股价上限"),
        ({"predict": {"boards": ["xx"]}}, "板块"),
        ({"predict": {"kind": "abc"}}, "预测类型"),
        ({"trade": {"stop_loss_pct": 0.9}}, "止损"),
        ({"predict": {"top_n": "很多"}}, "整数"),
    ]
    for body, word in bad_cases:
        r = client.put("/api/settings", json=body)
        assert r.status_code == 400, body
        assert word in r.json()["detail"], (body, r.json())
    assert client.get("/api/settings").json()["predict"]["top_n"] == 3      # 失败的修改不保存

    r = client.put("/api/settings", json=[1, 2])
    assert r.status_code == 422 and "请求参数不正确" in r.json()["detail"]


def test_settings_presets(client: TestClient) -> None:
    presets: dict = client.get("/api/settings/presets").json()
    assert set(presets) == {"稳健", "均衡", "激进"}
    assert presets["稳健"]["predict"]["top_n"] == 3 and presets["稳健"]["predict"]["exclude_one_word"] is True
    assert presets["稳健"]["trade"]["stop_loss_pct"] == 0.05 and presets["稳健"]["trade"]["position_pct"] == 0.1
    assert presets["激进"]["predict"]["top_n"] == 8 and presets["激进"]["trade"]["position_pct"] == 0.2
    assert presets["稳健"]["predict"]["threshold"] > presets["均衡"]["predict"]["threshold"] > presets["激进"]["predict"]["threshold"]
    for name in presets:         # 每套风格本身都能通过校验
        s = settings_mod.apply_preset(settings_mod.Settings(), name)
        assert s.predict.top_n == presets[name]["predict"]["top_n"]

    r = client.put("/api/settings", json={"preset": "稳健"})
    assert r.status_code == 200 and r.json()["predict"]["threshold"] == 0.30
    r = client.put("/api/settings", json={"preset": "稳健", "predict": {"kind": "first"}})
    assert r.json()["predict"]["kind"] == "first" and r.json()["predict"]["threshold"] == 0.08
    r = client.put("/api/settings", json={"preset": "不存在"})
    assert r.status_code == 400 and "风格" in r.json()["detail"]


def test_settings_load_recovers_from_bad_file(ws: Path) -> None:
    config.SETTINGS_FILE.write_text("{坏掉的json", encoding="utf-8")
    assert settings_mod.load() == settings_mod.Settings()
    config.SETTINGS_FILE.write_text(json.dumps({
        "predict": {"top_n": 999}, "trade": {"capital": 50000}, "risk_ack": True,
    }), encoding="utf-8")
    s = settings_mod.load()
    assert s.predict.top_n == 5 and s.trade.capital == 50000 and s.risk_ack is True
    settings_mod.save(s)
    assert settings_mod.load() == s
    assert not list(config.WORKSPACE.glob("*.tmp"))


# ================================================================ 后台任务

def test_job_manager_lifecycle() -> None:
    manager = jobs.JobManager()

    def work(progress: jobs.Progress) -> dict:
        progress(0.5, "处理到一半")
        print("打印的一行")
        jobs_logger.warning("日志里的警告")
        progress(0.9, "快完成了")
        return {"n": np.int64(3)}

    import logging
    jobs_logger = logging.getLogger("quant_web.test")
    job_id: str = manager.submit("work", "测试任务", work)
    job: dict = manager.wait(job_id, 10)
    assert job["status"] == "done" and job["progress"] == 1.0 and job["result"]["n"] == 3
    assert job["started_at"] and job["finished_at"] and job["error"] is None
    text: str = "\n".join(job["logs"])
    assert "处理到一半" in text and "打印的一行" in text and "日志里的警告" in text
    assert manager.list()[0]["id"] == job_id

    def boom(progress: jobs.Progress) -> None:
        progress(0.2, "准备出错")
        raise ZeroDivisionError("除数为零")

    job = manager.wait(manager.submit("boom", "会失败的任务", boom), 10)
    assert job["status"] == "failed" and job["error"] == "除数为零"
    assert any("ZeroDivisionError" in line for line in job["logs"])
    assert any("Traceback" in line or "line" in line for line in job["logs"])


def test_job_single_flight_and_group_queue() -> None:
    manager = jobs.JobManager(capture_output=False)
    gate = threading.Event()

    def slow(progress: jobs.Progress) -> str:
        gate.wait(10)
        return "ok"

    first: str = manager.submit("slow", "慢任务", slow, group="heavy")
    assert manager.submit("slow", "慢任务", slow, group="heavy") == first
    second: str = manager.submit("other", "另一个", lambda p: "done", group="heavy")
    time.sleep(0.2)
    assert manager.get(second)["queued"] is True and manager.get(second)["status"] == "running"
    assert manager.is_busy("heavy") and len(manager.running()) == 2
    gate.set()
    assert manager.wait(first, 10)["status"] == "done"
    assert manager.wait(second, 10)["result"] == "done"
    assert manager.get("nope") is None


def test_update_data_pipeline(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    today: date = datetime.now(config.CHINA_TZ).date()

    def refresh_universe(progress: object = None) -> pl.DataFrame:
        calls.append("universe")
        raise ConnectionError("列表接口挂了")

    fake_uni = fake_module("market.universe", refresh_universe=refresh_universe, load_universe=make_universe)
    fake_hist = fake_module("market.history", update_history=lambda progress=None: (
        calls.append("history") or {"last_date": today.isoformat(), "failed": ["000001"]}))
    fake_pools = fake_module(
        "market.pools", archive=lambda day: calls.append("archive") or {"zt": 10},
        update_lhb=lambda progress=None: calls.append("lhb") or {"rows": 5},
    )
    fake_fund = fake_module("market.fundamentals", update_fundamentals=lambda progress=None: calls.append("fund") or {})
    for name, m in (("market.universe", fake_uni), ("market.history", fake_hist),
                    ("market.pools", fake_pools), ("market.fundamentals", fake_fund)):
        install(monkeypatch, name, m)
    messages: list[str] = []
    out: dict = jobs.update_data(lambda f, m="": messages.append(m))
    assert calls == ["universe", "history", "archive", "lhb", "fund"]
    assert any("股票列表更新失败" in w for w in out["warnings"])
    assert "1 只股票下载失败" in messages[-1]


def test_job_routes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def train(kind: str, progress: object = None) -> dict:
        calls.append(kind)
        return {"kind": kind, "auc": 0.6}

    service = fake_module(
        "predict.service",
        update_data=lambda progress=None: {"updated": 1},
        daily_pipeline=lambda progress=None: {"trained": [], "predictions": {}},
        train=train,
    )
    install(monkeypatch, "predict.service", service)

    job_id: str = client.post("/api/jobs/update").json()["job_id"]
    assert jobs.JOBS.wait(job_id, 10)["status"] == "done"
    job: dict = client.get(f"/api/jobs/{job_id}").json()
    for key in ("id", "name", "title", "status", "progress", "message", "logs", "result", "error",
                "started_at", "finished_at"):
        assert key in job
    assert job["result"] == {"updated": 1} and job["name"] == "update"

    job_id = client.post("/api/jobs/train", json={"kind": "all"}).json()["job_id"]
    assert jobs.JOBS.wait(job_id, 10)["result"]["first"]["kind"] == "first"
    assert calls == ["streak", "first", "swing"]
    job_id = client.post("/api/jobs/train", json={"kind": "streak"}).json()["job_id"]
    assert jobs.JOBS.wait(job_id, 10)["result"]["kind"] == "streak"
    job_id = client.post("/api/jobs/train", json={"kind": "swing"}).json()["job_id"]
    assert jobs.JOBS.wait(job_id, 10)["result"]["kind"] == "swing"
    assert "强势股波段" in jobs.JOBS.get(job_id)["title"]
    service.KIND_LABELS = {"streak": "连板晋级", "swing": "强势股波段"}        # "all" 以服务模块的类型为准
    calls.clear()
    job_id = client.post("/api/jobs/train", json={"kind": "all"}).json()["job_id"]
    assert jobs.JOBS.wait(job_id, 10)["status"] == "done" and calls == ["streak", "swing"]
    r = client.post("/api/jobs/train", json={"kind": "xx"})
    assert r.status_code == 400 and "模型类型" in r.json()["detail"]

    job_id = client.post("/api/jobs/daily").json()["job_id"]
    assert jobs.JOBS.wait(job_id, 10)["status"] == "done"
    assert "paper" in jobs.JOBS.get(job_id)["result"]           # 预测后尝试记录模拟盘（这里记不了，只带原因）
    listed: list = client.get("/api/jobs").json()
    assert listed and listed[0]["id"] == job_id
    r = client.get("/api/jobs/doesnotexist")
    assert r.status_code == 404 and "任务" in r.json()["detail"]


def test_failed_job_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def daily(progress: object = None) -> None:
        raise ConnectionError("连不上腾讯")

    install(monkeypatch, "predict.service", fake_module("predict.service", daily_pipeline=daily))
    job_id: str = client.post("/api/jobs/daily").json()["job_id"]
    job: dict = jobs.JOBS.wait(job_id, 10)
    assert job["status"] == "failed" and "网络" in job["error"]
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "failed"


# ================================================================ 市场

def test_overview_from_panel(client: TestClient, panel_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, "market.sentiment", fake_sentiment_module())
    install(monkeypatch, "predict.service", fake_module("predict.service"))      # 没有 context：走本地计算
    r = client.get("/api/market/overview")
    assert r.status_code == 200, r.text
    data: dict = r.json()
    for key in ("as_of", "phase", "indexes", "sentiment", "ladder", "industries", "history"):
        assert key in data
    assert data["source"] == "panel" and data["as_of"].startswith(LAST.isoformat())
    assert [g["streak"] for g in data["ladder"]] == [2, 1]
    top: dict = data["ladder"][0]["stocks"][0]
    assert top["code"] == "600001" and top["name"] == "测试一号" and top["industry"] == "制造业"
    assert 9.9 < top["pct"] < 10.1 and top["one_word"] is False
    assert data["ladder"][1]["stocks"][0]["code"] == "300003"
    assert data["industries"] == [{"industry": "制造业", "count": 2}]
    assert len(data["history"]) == 30 and data["history"][-1]["label"] == "正常"
    assert data["history"][-1]["break_rate"] is None           # NaN → null
    assert data["sentiment"]["label"] == "正常" and data["sentiment"]["n_limit_up"] == 2


def test_overview_uses_service_context_and_live(client: TestClient, panel_ws: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_web.predict import limits

    sent_mod = fake_sentiment_module()
    install(monkeypatch, "market.sentiment", sent_mod)
    lim: pl.DataFrame = limits.add_limit_columns(make_panel(trading_days(30)), make_universe())
    ctx = types.SimpleNamespace(last_date=LAST, panel_lim=lim, universe=make_universe(),
                                sentiment=sent_mod.daily_sentiment(lim))
    install(monkeypatch, "predict.service", fake_module("predict.service", context=lambda: ctx))
    data: dict = client.get("/api/market/overview").json()
    assert data["ladder"][0]["streak"] == 2 and len(data["history"]) == 30

    # 盘中：实时情绪 + 东财涨停池
    pool = pl.DataFrame({
        "code": ["600009", "000010"], "name": ["实时甲", "实时乙"], "pct": [10.01, 9.98], "price": [11.0, 5.5],
        "first_time": ["09:25:00", "10:31:02"], "open_times": [0, 2], "streak": [3, 1], "industry": ["汽车", "汽车"],
    })
    realtime = fake_module("market.realtime", market_phase=lambda: "交易中",
                           index_quotes=lambda: [{"code": "sh000001", "name": "上证指数", "price": 3000.0}])
    install(monkeypatch, "market.realtime", realtime)
    install(monkeypatch, "market.pools", fake_module("market.pools", fetch_pool=lambda kind, day: pool))
    server.CACHE.clear()
    data = client.get("/api/market/overview").json()
    assert data["phase"] == "交易中" and data["source"] == "live"
    assert data["sentiment"]["n_limit_up"] == 60 and data["as_of"] == "2026-09-25 10:30:00"
    assert data["ladder"][0]["streak"] == 3 and data["ladder"][0]["stocks"][0]["one_word"] is True
    assert data["industries"] == [{"industry": "汽车", "count": 2}]
    assert data["indexes"][0]["name"] == "上证指数"


def test_overview_survives_missing_modules(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, "market.realtime", None)
    install(monkeypatch, "market.sentiment", None)
    install(monkeypatch, "predict.service", None)
    install(monkeypatch, "market.pools", None)          # 不能去网上取真实涨停池
    server.CACHE.clear()
    r = client.get("/api/market/overview")
    assert r.status_code == 200
    data: dict = r.json()
    assert data["ladder"] == [] and data["indexes"] == [] and data["warnings"]
    assert data["phase"] in ("盘前", "交易中", "午间休市", "已收盘", "休市")


def test_market_pool(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    archived = pl.DataFrame({"date": [date(2026, 9, 23)], "code": ["600001"], "name": ["测试一号"], "pct": [10.0]})
    pools = fake_module(
        "market.pools", KINDS={"zt": "涨停", "zb": "炸板", "dt": "跌停"},
        fetch_pool=lambda kind, day: pl.DataFrame({"code": ["000002"], "pct": [9.99]}),
        load_archive=lambda kind, start=None, end=None: archived if start == date(2026, 9, 23) else pl.DataFrame(),
    )
    install(monkeypatch, "market.pools", pools)
    data: dict = client.get("/api/market/pool?kind=zt&date=20260923").json()
    assert data["source"] == "archive" and data["count"] == 1 and data["rows"][0]["date"] == "2026-09-23"
    assert data["label"] == "涨停"
    data = client.get("/api/market/pool?kind=zb").json()
    assert data["source"] == "live" and data["rows"][0]["code"] == "000002"
    r = client.get("/api/market/pool?kind=xx")
    assert r.status_code == 400 and "股票池" in r.json()["detail"]
    r = client.get("/api/market/pool?kind=zt&date=2026年")
    assert r.status_code == 400 and "日期" in r.json()["detail"]


# ================================================================ 个股

def test_stock_search(client: TestClient, panel_ws: Path) -> None:
    rows: list = client.get("/api/stock/search", params={"q": "测试"}).json()
    assert {r["code"] for r in rows} == {"600001", "000002"}
    assert set(rows[0]) >= {"code", "name", "board", "industry"}
    assert client.get("/api/stock/search?q=").json() == []


def test_stock_quote_kline_minute(client: TestClient, panel_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bars: list[dict] = [{"date": "2026-09-24", "open": 1.0, "close": 1.1, "high": 1.1, "low": 1.0,
                         "volume": 100.0, "amount": 110.0}]

    def quotes(codes: list[str]) -> list[dict]:
        return [{"code": c, "name": "测试一号", "price": 12.1, "pct": 10.0} for c in codes]

    realtime = fake_module(
        "market.realtime", market_phase=lambda: "已收盘", quotes=quotes,
        kline=lambda code, period="day", adjust="qfq", count=600: bars,
        minute=lambda code, days=1: {"prev_close": 11.0, "points": [{"time": "09:31", "price": 11.1}], "days": days},
    )
    install(monkeypatch, "market.realtime", realtime)
    assert client.get("/api/stock/600001/quote").json()["price"] == 12.1
    data: dict = client.get("/api/stock/600001/kline?period=day&adjust=qfq&count=600").json()
    assert data["code"] == "600001" and data["name"] == "测试一号" and data["bars"] == bars
    assert data["limit_days"] == [d.isoformat() for d in trading_days(2)]
    assert client.get("/api/stock/600001/minute").json()["points"][0]["time"] == "09:31"
    assert client.get("/api/stock/600001/minute?days=5").json()["days"] == 5

    r = client.get("/api/stock/12345/quote")
    assert r.status_code == 400 and "6位数字" in r.json()["detail"]
    r = client.get("/api/stock/900901/kline")
    assert r.status_code == 400 and "A股" in r.json()["detail"]
    r = client.get("/api/stock/600001/kline?period=year")
    assert r.status_code == 400


def test_kline_falls_back_to_panel(client: TestClient, panel_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def kline(*args: object, **kw: object) -> list:
        raise ConnectionError("腾讯K线连不上")

    install(monkeypatch, "market.realtime", fake_module("market.realtime", market_phase=lambda: "休市", kline=kline))
    data: dict = client.get("/api/stock/600001/kline?count=10").json()
    assert data["source"] == "panel" and data["adjust"] == "" and len(data["bars"]) == 10
    assert data["bars"][-1]["date"] == LAST.isoformat() and data["warnings"]
    week: dict = client.get("/api/stock/600001/kline?period=week").json()
    assert 5 <= len(week["bars"]) <= 7 and week["bars"][-1]["date"] == LAST.isoformat()
    r = client.get("/api/stock/000009/kline")          # 面板里也没有：返回原始网络错误
    assert r.status_code == 502 and "网络" in r.json()["detail"]


def test_missing_module_and_crash_give_clean_json(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, "market.info", None)
    r = client.get("/api/stock/600001/profile")
    assert r.status_code == 503 and "模块" in r.json()["detail"]

    def quotes(codes: list[str]) -> list:
        raise KeyError("boom")

    install(monkeypatch, "market.realtime", fake_module("market.realtime", market_phase=lambda: "休市", quotes=quotes))
    r = client.get("/api/stock/600001/quote")
    assert r.status_code == 500 and r.json()["detail"].startswith("服务器内部出错")


def test_stock_profile_with_prediction(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    write_meta("streak")
    info = fake_module("market.info", profile=lambda code: {
        "code": code, "name": "测试一号", "concepts": [], "errors": {}, "list_date": date(2010, 1, 4)})
    service = fake_module("predict.service", prediction_for=lambda code, settings=None: {"kind": "streak", "prob": 0.4}
                          if code == "600001" else None)
    install(monkeypatch, "market.info", info)
    install(monkeypatch, "predict.service", service)
    data: dict = client.get("/api/stock/600001/profile").json()
    assert data["name"] == "测试一号" and data["list_date"] == "2010-01-04"
    assert data["prediction"] == {"kind": "streak", "prob": 0.4}
    assert client.get("/api/stock/000002/profile").json()["prediction"] is None


def test_profile_prediction_matches_today_list(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """看盘页的排名/综合概率要和「连板预测」名单一致（名单含消息面，prediction_for 不含）"""
    write_meta("first")
    info = fake_module("market.info", profile=lambda code: {"code": code, "name": code, "concepts": [], "errors": {}})

    def prediction_for(code: str, settings: object = None) -> dict:
        base: dict = {"kind": "first", "prob": 0.06, "score": 0.06, "dims": {"news": 50.0}, "in_list": True, "rank": 21, "pick": False}
        if code == "600003":
            base.update(in_list=False, rank=None)
        return base

    rows: list[dict] = [
        {"code": "600001", "prob": 0.06, "score": 0.23, "dims": {"news": 99.0}, "pick": True, "news_count": 4, "policy_count": 12},
        {"code": "600009", "prob": 0.08, "score": 0.10, "dims": {"news": 60.0}, "pick": False},
    ]
    service = fake_module(
        "predict.service", prediction_for=prediction_for,
        predict_latest=lambda kind, settings: {"kind": kind, "count": 3, "rows": rows, "model": {}},
    )
    install(monkeypatch, "market.info", info)
    install(monkeypatch, "predict.service", service)
    p1: dict = client.get("/api/stock/600001/profile").json()["prediction"]
    assert p1["rank"] == 1 and p1["pick"] is True and p1["in_list"] is True
    assert p1["score"] == 0.23 and p1["score_no_news"] == 0.06 and p1["news_count"] == 4 and p1["dims"]["news"] == 99.0
    p2: dict = client.get("/api/stock/600002/profile").json()["prediction"]     # 在名单里但不在已显示的行中
    assert p2["in_list"] is True and p2["rank"] is None and p2["beyond_rows"] == 2 and p2["pick"] is False
    p3: dict = client.get("/api/stock/600003/profile").json()["prediction"]     # 被筛选条件挡掉
    assert p3["in_list"] is False and p3["rank"] is None


# ================================================================ 预测 / 模型

def test_predict_routes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def predict_latest(kind: str, settings: settings_mod.Settings) -> dict:
        seen["kind"], seen["settings"] = kind, settings
        return {
            "kind": kind, "signal_date": "2026-09-24", "generated_at": "2026-09-24T18:00:00",
            "model": {"trained_at": "2026-09-20", "data_end": "2026-09-19", "oos_topn_hit": 0.4, "base_rate": 0.25},
            "count": 1, "filtered_out": 0, "warnings": [],
            "rows": [{"code": "600001", "name": "测试一号", "prob": np.float64(0.42), "score": 0.45,
                      "dims": {"sentiment": 60.0, "news": 50.0}, "reasons": ["换手率高"], "pick": True}],
        }

    def backtest(kind: str, settings: settings_mod.Settings) -> dict:
        seen["bt_kind"] = kind
        if settings.trade.capital == 12345:
            raise RuntimeError("还没有「连板晋级」模型的样本外预测，请先到模型中心训练")
        return {"metrics": {"trades": 10, "win_rate": 0.5}, "equity": [], "trades": []}

    install(monkeypatch, "predict.service", fake_module(
        "predict.service", predict_latest=predict_latest, backtest=backtest,
        models_meta=lambda kind: json.loads(config.MODEL_DIR.joinpath(f"{kind}_meta.json").read_text("utf-8"))
        if config.MODEL_DIR.joinpath(f"{kind}_meta.json").exists() else None,
    ))
    client.put("/api/settings", json={"predict": {"top_n": 7}})
    data: dict = client.get("/api/predict/today?kind=first").json()
    assert data["kind"] == "first" and data["rows"][0]["prob"] == 0.42 and data["rows"][0]["pick"] is True
    assert seen["kind"] == "first" and seen["settings"].predict.kind == "first" and seen["settings"].predict.top_n == 7
    r = client.get("/api/predict/today?kind=abc")
    assert r.status_code == 400 and "swing" in r.json()["detail"]
    data = client.get("/api/predict/today?kind=swing").json()
    assert data["kind"] == "swing" and seen["kind"] == "swing" and seen["settings"].predict.kind == "swing"

    r = client.post("/api/predict/backtest", json={"trade": {"capital": 50000}, "predict": {"threshold": 0.2}})
    assert r.status_code == 200
    data = r.json()
    assert data["metrics"]["trades"] == 10
    assert data["settings_used"]["trade"]["capital"] == 50000 and data["settings_used"]["predict"]["threshold"] == 0.2
    assert data["settings_used"]["predict"]["top_n"] == 7            # 未传的部分沿用已保存设置
    assert client.get("/api/settings").json()["trade"]["capital"] == 100000     # 回测不改保存的设置
    r = client.post("/api/predict/backtest", json={"trade": {"position_pct": 5}})
    assert r.status_code == 400 and "单只仓位" in r.json()["detail"]
    r = client.post("/api/predict/backtest", json={"trade": {"capital": 12345}})
    assert r.status_code == 409 and "样本外" in r.json()["detail"]
    r = client.post("/api/predict/backtest", json={"predict": {"kind": "swing", "top_n": 4}, "trade": {"capital": 60000}})
    assert r.status_code == 200, r.text
    assert seen["bt_kind"] == "swing" and r.json()["settings_used"]["predict"]["kind"] == "swing"
    assert r.json()["settings_used"]["predict"]["top_n"] == 4 and r.json()["settings_used"]["trade"]["capital"] == 60000
    r = client.post("/api/predict/backtest", json={"kind": "swing"})
    assert r.status_code == 200 and seen["bt_kind"] == "swing"
    r = client.post("/api/predict/backtest", json={"predict": {"kind": "xx"}})
    assert r.status_code == 400
    assert client.get("/api/settings").json()["predict"]["kind"] == "streak"    # 回测不改保存的设置

    r = client.get("/api/model/report?kind=streak")
    assert r.status_code == 404 and "训练" in r.json()["detail"]
    write_meta("streak")
    data = client.get("/api/model/report?kind=streak").json()
    assert data["folds"] and data["calibration"] and data["importance"] and data["label"] == "连板晋级"
    r = client.get("/api/model/report?kind=swing")
    assert r.status_code == 404 and "强势股波段" in r.json()["detail"]
    write_meta("swing")
    assert client.get("/api/model/report?kind=swing").json()["label"] == "强势股波段"


def test_news_flash(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def flash(limit: int = 100) -> list[dict]:
        seen.append(limit)
        return [{"time": datetime(2026, 9, 24, 15, 1), "title": "国务院发文", "content": "", "tags": ["国家政策"],
                 "stocks": []}]

    install(monkeypatch, "market.news", fake_module("market.news", flash=flash))
    rows: list = client.get("/api/news/flash?limit=20").json()
    assert rows[0]["time"] == "2026-09-24 15:01:00" and seen == [20]
    client.get("/api/news/flash?limit=20")
    assert seen == [20]                     # 60 秒内走缓存


def test_settings_defaults_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(settings_mod, "defaults_for", raising=False)
    data: dict = client.get("/api/settings/defaults?kind=first").json()
    assert data["predict"]["kind"] == "first" and data["predict"]["top_n"] == 5 and "trade" in data
    assert client.get("/api/settings/defaults").json()["predict"]["kind"] == "streak"     # 缺省 = 已保存的类型
    assert client.get("/api/settings/defaults?kind=swing").json()["predict"]["kind"] == "swing"
    r = client.get("/api/settings/defaults?kind=abc")
    assert r.status_code == 400 and "模型类型" in r.json()["detail"]

    asked: list[str] = []

    def defaults_for(kind: str) -> dict:
        asked.append(kind)
        return {"predict": {"kind": kind, "threshold": 0.01, "boards": ["main"]}, "trade": {"position_pct": 0.04}}

    monkeypatch.setattr(settings_mod, "defaults_for", defaults_for, raising=False)
    data = client.get("/api/settings/defaults?kind=swing").json()
    assert asked == ["swing"] and data["predict"]["threshold"] == 0.01 and data["trade"]["position_pct"] == 0.04


# ================================================================ 模拟盘

def _paper_service(monkeypatch: pytest.MonkeyPatch, day: str = "2026-09-24") -> dict:
    """假的预测服务：每个类型两行候选，第一行 pick"""
    state: dict = {"day": day, "calls": []}

    def predict_latest(kind: str, settings: settings_mod.Settings) -> dict:
        state["calls"].append((kind, settings.predict.kind))
        codes: list[str] = {"swing": ["600001", "000002"], "streak": ["600001", "300003"],
                            "first": ["000002", "300003"]}[kind]
        rows: list[dict] = [
            {"code": c, "name": f"股{c}", "score": 0.5 - i / 10, "prob": None if kind == "swing" else 0.3,
             "exp_ret": 1.8 if kind == "swing" else None, "pick": i == 0}
            for i, c in enumerate(codes)
        ]
        out: dict = {"kind": kind, "signal_date": state["day"], "model": {"trained_at": "2026-09-20"},
                     "count": 2, "rows": rows, "warnings": []}
        if kind == "swing":
            out["gate"] = {"trade": True, "reason": ""}
        return out

    install(monkeypatch, "predict.service", fake_module("predict.service", predict_latest=predict_latest))
    return state


def test_paper_routes(client: TestClient, panel_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict = _paper_service(monkeypatch)
    clock: dict = {"now": datetime(2026, 9, 24, 14, 30, tzinfo=config.CHINA_TZ)}
    monkeypatch.setattr(paper, "china_now", lambda: clock["now"])

    empty: dict = client.get("/api/paper/summary").json()
    assert set(empty["kinds"]) == {"swing", "streak", "first"} and empty["since"] is None
    assert empty["kinds"]["swing"]["signals"] == 0 and client.get("/api/paper/trades").json() == []

    # 交易日收盘前：拒绝并说明原因
    r = client.post("/api/paper/record")
    assert r.status_code == 409 and "15:05" in r.json()["detail"] and not state["calls"]

    # 收盘后、数据是最新的：三个类型各记 1 只（各自按自己的类型取设置）
    clock["now"] = datetime(2026, 9, 24, 16, 0, tzinfo=config.CHINA_TZ)
    r = client.post("/api/paper/record")
    assert r.status_code == 200, r.text
    data: dict = r.json()
    assert data["signal_date"] == "2026-09-24" and data["recorded"] == 3
    assert {k: v["status"] for k, v in data["kinds"].items()} == {
        "swing": "recorded", "streak": "recorded", "first": "recorded"}
    assert sorted(state["calls"]) == [("first", "first"), ("streak", "streak"), ("swing", "swing")]
    assert "记录了 1 只" in data["message"]
    again: dict = client.post("/api/paper/record").json()          # 再点一次：不重复
    assert again["recorded"] == 0 and {v["status"] for v in again["kinds"].values()} == {"already"}

    summ: dict = client.get("/api/paper/summary").json()
    assert summ["since"] == "2026-09-24" and summ["data_end"] == "2026-09-24" and summ["equity_method"]
    for kind in ("swing", "streak", "first"):
        k: dict = summ["kinds"][kind]
        assert k["signals"] == 1 and k["pending"] == 1 and k["days"] == 1 and k["equity"] == [], kind
    rows: list[dict] = client.get("/api/paper/trades?kind=swing").json()
    assert len(rows) == 1 and rows[0]["code"] == "600001" and rows[0]["exp_ret"] == 1.8
    assert rows[0]["status"] == "pending" and rows[0]["status_label"] == "待买入"
    assert rows[0]["signal_date"] == "2026-09-24"
    assert len(client.get("/api/paper/trades?status=pending&limit=2").json()) == 2
    r = client.get("/api/paper/trades?status=sold")
    assert r.status_code == 400 and "状态" in r.json()["detail"]
    r = client.get("/api/paper/trades?kind=abc")
    assert r.status_code == 400 and "策略" in r.json()["detail"]

    # 第二天收盘后数据还没更新（预测仍是 9-24 的）：拒绝，提示先更新数据
    clock["now"] = datetime(2026, 9, 25, 16, 0, tzinfo=config.CHINA_TZ)
    r = client.post("/api/paper/record")
    assert r.status_code == 409 and "更新数据" in r.json()["detail"]
    assert paper.load_signals().height == 3


# ================================================================ 自选股

def test_watchlist(client: TestClient, panel_ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def quotes(codes: list[str]) -> list[dict]:
        return [{"code": c, "name": "名称" + c, "price": 10.0, "pct": 1.0} for c in codes if c != "000002"]

    install(monkeypatch, "market.realtime", fake_module("market.realtime", market_phase=lambda: "休市", quotes=quotes))
    assert client.get("/api/watchlist").json() == []
    rows: list = client.post("/api/watchlist", json={"code": "600001", "note": "观察"}).json()
    assert rows[0]["code"] == "600001" and rows[0]["note"] == "观察" and rows[0]["price"] == 10.0
    server.CACHE.clear()
    rows = client.post("/api/watchlist", json={"code": "000002"}).json()
    assert [r["code"] for r in rows] == ["600001", "000002"]
    assert rows[1]["price"] is None and rows[1]["name"] == "测试二号" and rows[1]["error"]
    rows = client.post("/api/watchlist", json={"code": "600001"}).json()         # 重复添加不重复
    assert len(rows) == 2 and rows[0]["note"] == "观察"
    r = client.post("/api/watchlist", json={"code": "abc"})
    assert r.status_code == 400
    rows = client.request("DELETE", "/api/watchlist", json={"code": "600001"}).json()
    assert [r["code"] for r in rows] == ["000002"]
    r = client.request("DELETE", "/api/watchlist", json={"code": "600001"})
    assert r.status_code == 404
    assert client.delete("/api/watchlist/000002").json() == []
    assert json.loads(config.WATCHLIST_FILE.read_text(encoding="utf-8")) == []


# ================================================================ 自动更新调度

class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def cn(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=config.CHINA_TZ)


def test_target_day() -> None:
    cal: list[date] = [date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
    assert scheduler.target_day(cn(2026, 9, 24, 16), cal) == date(2026, 9, 24)
    assert scheduler.target_day(cn(2026, 9, 24, 15, 34), cal) == date(2026, 9, 23)
    assert scheduler.target_day(cn(2026, 9, 24, 10), cal) == date(2026, 9, 23)
    assert scheduler.target_day(cn(2026, 9, 27, 10), cal) == date(2026, 9, 24)     # 周日


def test_scheduler_rules(ws: Path) -> None:
    manager = jobs.JobManager(capture_output=False)
    ran: list[str] = []
    state: dict = {"last": None, "calendar_calls": 0}
    calendar: list[date] = trading_days(40)

    def calendar_fn() -> list[date]:
        state["calendar_calls"] += 1
        return calendar

    clock = _Clock(cn(2026, 9, 24, 16))
    sch = scheduler.Scheduler(jobs=manager, runner=lambda p: ran.append("run") or "ok", now_fn=clock,
                              calendar_fn=calendar_fn, last_date_fn=lambda: state["last"])

    # 从没下载过数据：绝不自动开始
    assert sch.check() is None and "点击" in sch.message and state["calendar_calls"] == 0

    # 关闭自动更新
    state["last"] = date(2026, 9, 23)
    settings_mod.save(settings_mod.Settings(auto_update=False))
    assert sch.check() is None and "关闭" in sch.message
    settings_mod.save(settings_mod.Settings(auto_update=True))

    # 收盘前：前一交易日已有 → 不跑，且不联网查日历
    clock.now = cn(2026, 9, 24, 14)
    assert sch.check() is None and state["calendar_calls"] == 0

    # 15:35 以后当天缺数据 → 自动提交一次
    clock.now = cn(2026, 9, 24, 15, 40)
    job_id: str | None = sch.check()
    assert job_id is not None and manager.wait(job_id, 5)["status"] == "done" and ran == ["run"]
    assert sch.status()["target_day"] == "2026-09-24"
    # 数据仍没更新上：30 分钟内不重试
    assert sch.check() is None and ran == ["run"]

    # 数据已更新 → 不再跑
    state["last"] = date(2026, 9, 24)
    clock.now = cn(2026, 9, 24, 23)
    assert sch.check() is None and "最新" in sch.message

    # 落后太多（>30 个交易日）→ 留给用户手动
    state["last"] = date(2026, 7, 1)
    assert sch.check() is None and "手动" in sch.message


def test_scheduler_retry_limit_and_busy(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """假时钟（北京时间 + monotonic 都是假的）：失败后 30 分钟才重试，同一交易日最多 3 次；有大任务在跑时不提交"""
    mono: dict = {"t": 1000.0}
    monkeypatch.setattr(scheduler._time, "monotonic", lambda: mono["t"])
    manager = jobs.JobManager(capture_output=False)
    runs: list[str] = []

    def failing(progress) -> None:
        runs.append("run")
        raise ConnectionError("网络断了")

    clock = _Clock(cn(2026, 9, 24, 15, 40))
    calendar: list[date] = trading_days(40)
    sch = scheduler.Scheduler(jobs=manager, runner=failing, now_fn=clock, calendar_fn=lambda: calendar,
                              last_date_fn=lambda: date(2026, 9, 23))
    for attempt in range(1, scheduler.MAX_ATTEMPTS + 1):
        job_id: str | None = sch.check()
        assert job_id is not None, attempt
        assert manager.wait(job_id, 5)["status"] == "failed" and len(runs) == attempt
        mono["t"] += scheduler.RETRY_SECONDS - 60              # 30 分钟内：不重试
        assert sch.check() is None
        assert ("稍后重试" if attempt < scheduler.MAX_ATTEMPTS else "请手动更新") in sch.message
        mono["t"] += 120
    assert sch.check() is None and "手动" in sch.message and len(runs) == scheduler.MAX_ATTEMPTS

    # 第二天：重新计数；但有大任务（如训练）在跑时先等
    clock.now = cn(2026, 9, 25, 16)
    calendar.append(date(2026, 9, 25))
    gate = threading.Event()
    busy_id: str = manager.submit("train", "训练", lambda p: gate.wait(5), group=jobs.HEAVY)
    assert sch.check() is None and "其他任务" in sch.message
    gate.set()
    manager.wait(busy_id, 5)
    assert sch.check() is not None


def test_scheduler_thread_and_status(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """后台线程：启动后先等 FIRST_DELAY 再检查（这里改成 0，不真的等），stop() 能及时退出"""
    monkeypatch.setattr(scheduler, "FIRST_DELAY", 0.0)
    monkeypatch.setattr(scheduler, "CHECK_SECONDS", 0.01)
    manager = jobs.JobManager(capture_output=False)
    ran = threading.Event()
    sch = scheduler.Scheduler(jobs=manager, runner=lambda p: ran.set(), now_fn=_Clock(cn(2026, 9, 24, 16)),
                              calendar_fn=lambda: trading_days(10), last_date_fn=lambda: date(2026, 9, 23))
    assert not sch.status()["running"] and "--no-scheduler" in sch.status()["message"]
    sch.start()
    try:
        assert ran.wait(5) and sch.status()["running"] and sch.status()["last_job_id"]
    finally:
        sch.stop()
    assert not sch.status()["running"] and "--no-scheduler" not in sch.status()["message"]


def test_create_app_background_flags(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """QUANT_WEB_NO_SCHEDULER=1 只关自动更新，仍然后台预热；background=False 两者都关"""
    started: list[str] = []
    monkeypatch.setattr(scheduler, "start", lambda: started.append("scheduler"))
    monkeypatch.setattr(scheduler, "stop", lambda: None)
    monkeypatch.setattr(server, "_warmup", lambda: started.append("warmup"))
    monkeypatch.setenv("QUANT_WEB_NO_SCHEDULER", "1")
    with TestClient(server.create_app()):
        time.sleep(0.05)
    assert started == ["warmup"]
    started.clear()
    with TestClient(server.create_app(background=False)):
        pass
    assert started == []
    monkeypatch.delenv("QUANT_WEB_NO_SCHEDULER")
    with TestClient(server.create_app()):
        time.sleep(0.05)
    assert sorted(started) == ["scheduler", "warmup"]


def test_scheduler_holiday(ws: Path) -> None:
    manager = jobs.JobManager(capture_output=False)
    calendar: list[date] = [date(2026, 9, 29), date(2026, 9, 30)]       # 国庆节休市
    sch = scheduler.Scheduler(jobs=manager, runner=lambda p: None, now_fn=_Clock(cn(2026, 10, 2, 16)),
                              calendar_fn=lambda: calendar, last_date_fn=lambda: date(2026, 9, 30))
    assert sch.check() is None and "最新" in sch.message


# ================================================================ 稳健ETF

ETF_PRICES: dict[str, float] = {
    "510300": 4.5, "510880": 3.1, "512890": 1.4, "510500": 6.2, "159915": 3.3, "512100": 2.6, "588000": 1.2,
    "513100": 1.8, "513500": 2.1, "518880": 7.2, "511010": 140.0, "511260": 120.0, "159985": 2.2, "511880": 100.8,
}


@pytest.fixture()
def etf_ws(ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 ETF_QUANT_WORKSPACE：复制仓库里的行情 lab，价格用固定值（完全离线）"""
    if not ETF_LAB.joinpath("daily").exists():
        pytest.skip("etf_quant/workspace/lab 没有行情数据")
    from etf_quant import advisor, data, portfolio
    from etf_quant import config as etf_config

    root: Path = tmp_path.joinpath("etf_ws")
    shutil.copytree(ETF_LAB, root.joinpath("lab"))
    monkeypatch.setenv("ETF_QUANT_WORKSPACE", str(root))
    paths: dict[str, Path] = {
        "WORKSPACE": root, "LAB_PATH": root.joinpath("lab"), "REPORT_PATH": root.joinpath("reports"),
        "HOLDINGS_FILE": root.joinpath("持仓.csv"), "NAV_FILE": root.joinpath("净值记录.csv"),
        "ADVICE_FILE": root.joinpath("last_advice.json"),
    }
    for k, v in paths.items():
        monkeypatch.setattr(etf_config, k, v)
    monkeypatch.setattr(data, "LAB_PATH", paths["LAB_PATH"])
    monkeypatch.setattr(advisor, "ADVICE_FILE", paths["ADVICE_FILE"])
    monkeypatch.setattr(portfolio, "WORKSPACE", root)

    def fake_quotes(etfs: list) -> dict:
        return {e.vt_symbol: (LAST, ETF_PRICES[e.code]) for e in etfs}

    monkeypatch.setattr(etf_api, "fetch_quotes", fake_quotes)
    monkeypatch.setattr(data, "fetch_latest_prices", lambda etfs: {})
    for cache in (etf_api._advice_cache, etf_api._quote_cache, etf_api._bt_cache):
        cache.clear()
    return root


def test_etf_holdings_flow(client: TestClient, etf_ws: Path) -> None:
    data: dict = client.get("/api/etf/advice?update=false").json()
    assert data["needs_init"] is True and data["message"]
    assert data["allocation"] and data["signal_date"]          # 未初始化也给出目标配置预览
    assert client.get("/api/etf/holdings").json()["exists"] is False

    r = client.post("/api/etf/init", json={"capital": -1})
    assert r.status_code == 400 and "大于0" in r.json()["detail"]
    data = client.post("/api/etf/init", json={"capital": 200000}).json()
    assert data["exists"] is True and data["cash"] == 200000 and data["positions"] == [] and data["replaced"] is False

    advice: dict = client.get("/api/etf/advice?update=false").json()
    assert advice["needs_init"] is False and advice["orders"] and advice["applied"] is False
    for key in ("signal_date", "price_date", "total_value", "cash", "cash_after", "positions", "allocation",
                "warnings", "data"):
        assert key in advice
    order: dict = advice["orders"][0]
    assert set(order) >= {"code", "name", "side", "volume", "price", "amount", "commission"}
    assert order["price"] == ETF_PRICES[order["code"]]
    assert abs(advice["total_value"] - 200000) < 1e-6

    applied: dict = client.post("/api/etf/apply").json()
    assert applied["ok"] is True and applied["positions"] and applied["cash"] < 200000
    r = client.post("/api/etf/apply")
    assert r.status_code == 409 and "已经更新过" in r.json()["detail"]

    held: dict = client.get("/api/etf/holdings").json()
    assert held["exists"] and held["positions"][0]["value"] > 0 and held["total_value"] > 190000
    assert held["nav"][0]["note"] == "初始资金" and held["price_source"] == "realtime"

    r = client.put("/api/etf/holdings", json={"cash": 1000, "positions": {"510300": 1000}})
    assert r.status_code == 200
    held = r.json()
    assert held["cash"] == 1000 and [p["code"] for p in held["positions"]] == ["510300"]
    assert held["positions"][0]["value"] == 4500
    r = client.put("/api/etf/holdings", json={"positions": {"510300": -5}})
    assert r.status_code == 400 and "整数" in r.json()["detail"]
    r = client.put("/api/etf/holdings", json={"positions": {"abc": 100}})
    assert r.status_code == 400
    assert client.post("/api/etf/init", json={"capital": 100000}).json()["replaced"] is True


def test_etf_backtest(client: TestClient, etf_ws: Path) -> None:
    t0: float = time.time()
    r = client.get("/api/etf/backtest?start=2020-01-01&capital=200000")
    assert r.status_code == 200, r.text
    data: dict = r.json()
    for key in ("metrics", "benchmark_metrics", "equity", "yearly", "class_weights"):
        assert key in data
    assert data["cached"] is False and data["start"] >= "2020-01-01"
    assert set(data["metrics"]) >= {"total_return", "cagr", "max_drawdown", "sharpe"}
    assert data["equity"][0]["strategy"] == 200000 and {"date", "strategy", "benchmark"} <= set(data["equity"][-1])
    assert data["yearly"][0]["year"] == 2020 and "benchmark" in data["yearly"][0]
    assert data["class_weights"] and set(data["classes"]) <= set(data["class_weights"][-1])
    # 参数是参考同一段历史选的：必须标明是样本内
    assert data["in_sample"] is True and "样本内" in data["in_sample_note"]
    first_seconds: float = time.time() - t0

    t0 = time.time()
    again: dict = client.get("/api/etf/backtest?start=2020-01-01&capital=200000").json()
    assert again["cached"] is True and time.time() - t0 < max(first_seconds / 3, 1.0)

    r = client.get("/api/etf/backtest?start=2020/01/01")
    assert r.status_code == 400 and "YYYY-MM-DD" in r.json()["detail"]
    r = client.get("/api/etf/backtest?capital=100")
    assert r.status_code == 400


# ================================================================ 启动脚本

def test_main_port_helpers() -> None:
    import socket

    from quant_web import __main__ as entry

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port: int = sock.getsockname()[1]
        assert entry.port_free("127.0.0.1", port) is False
        assert entry.already_running("127.0.0.1", port) is False
