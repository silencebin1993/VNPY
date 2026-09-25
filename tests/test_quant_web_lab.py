"""quant_web 第三版 P7：模型实验室（全部用合成数据：40 只股票、约两年，埋了一个真实有效的信号；不碰真实数据、不训练真实模型）"""
import json
import warnings
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from quant_web import config
from quant_web import settings as settings_mod
from quant_web.modellab import dataset as D
from quant_web.modellab import evaluate as E
from quant_web.modellab import factor_test, models, store, train
from quant_web.modellab import options as O
from quant_web.predict.model import purge_mask

START = date(2024, 1, 1)


def _weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_table(n_stocks: int = 40, n_days: int = 480, seed: int = 1) -> pl.DataFrame:
    """合成的"全历史字段表"：rps120 是一个持续的信号（AR(1)），决定之后几天的涨跌；其余主力列是噪声"""
    rng = np.random.default_rng(seed)
    days = _weekdays(START, n_days)
    parts: list[pl.DataFrame] = []
    stages = ["accumulation", "washout", "markup", "distribution", "decline", "unclear"]
    for i in range(n_stocks):
        code = f"{600000 + i}" if i % 2 == 0 else f"{i:06d}"
        z = np.zeros(n_days)
        for t in range(1, n_days):
            z[t] = 0.9 * z[t - 1] + rng.normal(0, 0.44)
        ret = 0.004 * np.concatenate([[0.0], z[:-1]]) + rng.normal(0, 0.02, n_days)
        close = 10 * np.cumprod(1 + ret)
        pre = np.concatenate([[close[0] / (1 + ret[0])], close[:-1]])
        opn = pre * (1 + rng.normal(0, 0.004, n_days))
        parts.append(pl.DataFrame({
            "code": [code] * n_days, "date": days, "open": opn, "close": close, "high": np.maximum(opn, close) * 1.01,
            "low": np.minimum(opn, close) * 0.99, "raw_open": opn, "raw_close": close, "preclose": pre, "is_st": [False] * n_days,
            "amount": [1e8] * n_days, "turn": [2.0] * n_days, "volume": [1e7] * n_days, "board": ["main"] * n_days,
            "pos": np.arange(n_days), "amt20": [1e8] * n_days, "adj_factor": [1.0] * n_days,
            "limit_up": np.round(pre * 1.1 + 1e-6, 2), "limit_down": np.round(pre * 0.9 + 1e-6, 2),
            "rps120": z, "pos250": rng.random(n_days), "bias20": rng.normal(0, 1, n_days), "vr20": rng.random(n_days),
            "udr20": rng.random(n_days), "obv_slope": rng.normal(0, 1, n_days), "atr_pct": rng.random(n_days),
            "rally60": rng.random(n_days), "vol_shrink": rng.random(n_days) > 0.5, "dist_ma20": rng.normal(0, 1, n_days),
            "ma_bull": rng.random(n_days) > 0.5, "turn20": rng.random(n_days), "rev20": rng.normal(0, 1, n_days),
            "stage_score": rng.random(n_days), "score_accumulation": rng.random(n_days), "score_washout": rng.random(n_days),
            "score_markup": rng.random(n_days), "score_distribution": rng.random(n_days), "score_decline": rng.random(n_days),
            "stage": rng.choice(stages, n_days), "float_cap": [5e9 + i * 1e8] * n_days,
        }))
    return pl.concat(parts).sort(["code", "date"])


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "WATCHLIST_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(config, "STOCK_LAB", tmp_path / "stock_lab")
    monkeypatch.setattr(config, "PANEL_DIR", tmp_path / "stock_lab" / "daily")
    data = settings_mod.load().model_dump()
    data["lab"]["threads"] = 2
    settings_mod.save(settings_mod.validate(data))
    return tmp_path


@pytest.fixture(scope="module")
def base() -> tuple[pl.DataFrame, pl.DataFrame]:
    t = make_table()
    return t, t


def cfg(**kw) -> dict:
    return O.validate_config({"universe": "main", "factor_sets": ["mainforce"], "label": "excess_10", "model": "lightgbm",
                              "preset": "fast", "start_year": 2024, "top_k": 5, **kw})


# ---------------------------------------------------------------- 配置

def test_config_defaults_and_errors() -> None:
    c = O.validate_config({})
    assert c["universe"] == "main" and c["label"] == "excess_10" and c["model"] == "lightgbm" and c["normalize"] == "rank"
    with pytest.raises(ValueError, match="股票范围"):
        O.validate_config({"universe": "moon"})
    with pytest.raises(ValueError, match="分类|数值"):
        O.validate_config({"model": "logistic", "label": "excess_10"})
    with pytest.raises(ValueError, match="标准化"):
        O.validate_config({"model": "ridge", "normalize": "none"})
    with pytest.raises(ValueError, match="因子组"):
        O.validate_config({"factor_sets": []})
    assert "LightGBM" in O.describe(c)


# ---------------------------------------------------------------- 数据：标签、标准化、净化

def test_labels_follow_trade_rules_and_excess_is_same_day_relative(ws: Path, base) -> None:
    t = base[0].filter(pl.col("code").is_in(["600000", "000001"]))
    # 600000 在第 100 天之后的第二天一字涨停开盘 → 那一天的信号买不进，没有标签
    d100 = t.filter(pl.col("code") == "600000")["date"][100]
    d101 = t.filter(pl.col("code") == "600000")["date"][101]
    t = t.with_columns(pl.when((pl.col("code") == "600000") & (pl.col("date") == d101)).then(pl.col("limit_up"))
                       .otherwise(pl.col("raw_open")).alias("raw_open"))
    lab = D.add_labels(t, cfg())
    row = lab.filter((pl.col("code") == "600000") & (pl.col("date") == d100)).row(0, named=True)
    assert row["filled"] is False and row["net"] is None
    ok = lab.filter(pl.col("code") == "000001").row(100, named=True)
    days = t.filter(pl.col("code") == "000001")["date"].to_list()
    assert ok["label_end"] == days[110]                                    # 持有 10 天后卖出
    rows = D.add_targets(lab.filter(pl.col("pos") >= 60).select("code", "date", "net", "filled", "label_end"), cfg())
    m = rows.filter(pl.col("net").is_not_null()).group_by("date").agg(pl.col("excess").mean())
    assert m["excess"].abs().max() < 1e-5                                  # 超额 = 相对同日平均


def test_normalize_rank_per_day_keeps_nulls() -> None:
    df = pl.DataFrame({"date": [date(2025, 1, 2)] * 4 + [date(2025, 1, 3)] * 3, "x": [1.0, 5.0, None, 3.0, 100.0, 50.0, 0.0]})
    out = D.normalize(df, ["x"], "rank")["x"].to_list()
    assert out[:4] == [-0.5, 0.5, None, 0.0] and out[4:] == [0.5, 0.0, -0.5]


def test_build_dataset_purge_and_split(ws: Path, base) -> None:
    data = D.build(cfg(), base=base)
    ds = data.ds
    assert data.info["stocks"] == 40 and set(data.features) >= {"m_rps120", "m_score_markup", "m_is_markup"}
    assert ds.filter(pl.col("date") < START + timedelta(days=80))["code"].n_unique() == 0          # 上市不满 60 天不在范围里
    days = sorted(ds["date"].unique().to_list())
    folds = train.folds_for(days, cfg())
    assert len(folds) >= 2 and all(f.train_end < f.test_start for f in folds)
    f = folds[0]
    tr = ds.filter(purge_mask(ds, f.train_end) & pl.col("target").is_not_null())
    assert tr["label_end"].max() <= f.train_end                            # 训练行的标签在测试开始前就已揭晓
    trn, val, _ = train._split(tr, models.get("lightgbm"), "fast")
    assert trn["label_end"].max() < val["date"].min()                      # 训练和验证之间也净化


# ---------------------------------------------------------------- 训练与评估

def test_lightgbm_finds_planted_signal_and_is_honest(ws: Path, base, monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_web.market import history
    s = train.run(cfg(), base=base)
    run = store.get_run(s["id"])
    ev = run["evaluation"]
    all_ = ev["segments"]["all"]
    assert all_["rank_ic"]["mean"] > 0.03 and all_["topk"]["excess"] > 0
    assert set(ev["segments"]) == {"selection", "holdout", "all"} and ev["holdout_start"] == "2025-07-01"
    for seg in ev["segments"].values():                                   # 每段都有 t 值、随机基准
        if seg.get("topk"):
            assert {"t", "daily_t", "nw_t", "base", "excess", "base_cagr"} <= set(seg["topk"])
    assert ev["verdict"]["key"] in ("good", "weak", "bad", "short") and isinstance(ev["verdict"]["credible"], bool)
    assert run["importance"]["top"][0]["name"] == "m_rps120"             # 真正有效的因子排第一
    assert run["latest"]["rows"] and run["latest"]["rows"][0]["why"]     # LightGBM 能拆解得分
    assert store.trials()["count"] == 1 and store.list_runs()[0]["id"] == s["id"]
    # 启用到选股器：历史日期用样本外预测，最新一天用最终模型
    store.enable(s["id"])
    last = base[0]["date"].max()
    monkeypatch.setattr(history, "last_date", lambda: last)
    sc = store.model_scores([last, date(2025, 3, 3)])
    assert set(sc["date"].unique().to_list()) <= {last, date(2025, 3, 3)} and sc.filter(pl.col("date") == last).height == 40
    assert store.cached_latest()["date"] == str(last)
    store.delete_run(s["id"])
    assert store.enabled() is None and store.list_runs() == []


@pytest.mark.parametrize("key", [k for k, m in models.MODELS.items() if m.available()[0]])
def test_every_model_trains_quickly(ws: Path, key: str) -> None:
    small = make_table(n_stocks=24, n_days=420, seed=3)
    label = "up5_10" if key == "logistic" else "rank_10"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        s = train.run(cfg(model=key, label=label), base=(small, small))
    run = store.get_run(s["id"])
    assert run["evaluation"]["segments"]["all"]["days"] > 50 and run["latest"]["rows"]
    spec, fitted, _ = store.load_model(s["id"])                            # 存下来的模型能读回来
    assert spec.key == key and fitted is not None


def test_equal_weight_baseline_signs_follow_training_ic() -> None:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(2000, 3)).astype(np.float32)
    g = np.repeat(np.arange(40), 50)
    y = (0.5 * X[:, 0] - 0.5 * X[:, 1] + rng.normal(size=2000)).astype(np.float32)
    m = models.get("equal_weight").adapter.fit(X, y, "reg", "fast", g=g)
    assert m.obj["w"][0] > 0 and m.obj["w"][1] < 0


def test_evaluate_quintiles_and_topk_against_random() -> None:
    days = _weekdays(date(2025, 1, 1), 60)
    rows = []
    rng = np.random.default_rng(5)
    for d in days:
        for i in range(30):
            pred = float(i)
            net = 0.001 * i + rng.normal(0, 0.01)
            rows.append({"date": d, "code": f"{i:06d}", "pred": pred, "net": net})
    oos = pl.DataFrame(rows).with_columns((pl.col("net") - pl.col("net").mean().over("date")).alias("excess"))
    ev = E.evaluate(oos, hold=5, top_k=5)
    seg = ev["segments"]["all"]
    assert seg["quintiles"][4] > seg["quintiles"][0] and seg["monotonic"] >= 3
    assert seg["topk"]["excess"] > 0 and seg["topk"]["periods"] == 12 and seg["rank_ic"]["mean"] > 0.3


# ---------------------------------------------------------------- 估算、单因子检验

def test_estimate_blocks_over_memory_cap(ws: Path) -> None:
    data = settings_mod.load().model_dump()
    data["lab"]["max_mem_gb"] = 2
    settings_mod.save(settings_mod.validate(data))
    est = D.estimate(O.validate_config({"universe": "all", "factor_sets": ["alpha158", "alpha101", "basic"]}))
    assert est["blocked"] and "内存" in est["reason"] and est["rows"] > 0
    small = D.estimate(O.validate_config({"universe": "main", "factor_sets": ["mainforce"], "start_year": 2024}))
    assert small["mem_gb"] < est["mem_gb"] and small["minutes"] >= 1


def test_factor_test_ranks_true_factor_first(ws: Path, base) -> None:
    r = factor_test.run(cfg(), base=base)
    res = factor_test.load(r["key"])
    top = res["rows"][0]
    assert top["name"] == "m_rps120" and top["all"]["mean"] > 0.03 and res["hold"] == 10


# ---------------------------------------------------------------- 接口

@pytest.fixture()
def client(ws: Path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from quant_web.api import server
    with TestClient(server.create_app(background=False)) as c:
        yield c


def test_lab_api(client, base, monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_web import tasks
    from quant_web.market import history
    o = client.get("/api/lab/options").json()
    assert {m["key"] for m in o["models"]} >= {"lightgbm", "xgboost", "catboost", "equal_weight"}
    assert o["defaults"]["universe"] == "main" and o["trials"]["count"] == 0 and o["holdout_start"] == "2025-07-01"
    est = client.post("/api/lab/estimate", json={"config": {"factor_sets": ["mainforce"]}}).json()
    assert est["config"]["factor_sets"] == ["mainforce"] and "mem_gb" in est
    bad = client.post("/api/lab/estimate", json={"config": {"universe": "moon"}})
    assert bad.status_code == 400
    submitted: list = []
    monkeypatch.setattr(tasks, "submit", lambda name, params=None, title=None: submitted.append((name, params)) or "job1")
    r = client.post("/api/lab/train", json={"config": {"factor_sets": ["mainforce"], "preset": "fast"}}).json()
    assert r["job_id"] == "job1" and submitted[0][0] == "lab_train" and submitted[0][1]["config"]["preset"] == "fast"
    s = train.run(cfg(), base=base)
    assert client.get("/api/lab/runs").json()["runs"][0]["id"] == s["id"]
    d = client.get(f"/api/lab/runs/{s['id']}").json()
    assert d["summary"]["id"] == s["id"] and d["evaluation"]["verdict"]["text"]
    assert client.get("/api/lab/runs/bad..id").status_code == 400
    assert client.post(f"/api/lab/runs/{s['id']}/enable").json()["enabled"] is True
    monkeypatch.setattr(history, "last_date", lambda: base[0]["date"].max())
    sc = client.get("/api/lab/scores").json()
    assert sc["enabled"] == s["id"] and not sc["stale"] and len(sc["rows"]) == 40
    assert client.post("/api/lab/score", json={}).json()["job_id"] == "job1"
    ft = client.post("/api/lab/factor_test", json={"config": {"factor_sets": ["mainforce"]}}).json()
    assert ft["key"] and submitted[-1][0] == "lab_factor_test"
    assert client.get("/api/lab/factor_test/abc123").status_code == 404
    assert client.delete(f"/api/lab/runs/{s['id']}").json()["deleted"] == s["id"]
    assert client.get("/api/lab/scores").json()["enabled"] is None
    assert json.loads(store.base_dir().joinpath("state.json").read_text(encoding="utf-8"))["trials"] == 1
