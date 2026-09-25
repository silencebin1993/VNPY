"""quant_web 第三版：主力阶段识别（人工构造的吸筹/洗盘/拉升/出货/下跌 K 线）、证据缺数据的处理、阶段统计（全部离线）"""
from datetime import date, timedelta

import numpy as np
import polars as pl

from quant_web.analysis import features, stage, stage_stats
from quant_web.formula import engine


def make(closes: list[float], vols: list[float], shadows: list[float] | None = None, code: str = "600000",
         start: date = date(2024, 1, 1)) -> pl.DataFrame:
    """由收盘价和成交量造日线：开盘=昨收，高低 ±0.8%；shadows 给出上影线长度（比例）"""
    n = len(closes)
    c = np.array(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    sh = np.array(shadows if shadows is not None else [0.0] * n)
    high = np.maximum(o, c) * (1.008 + sh)
    low = np.minimum(o, c) * 0.992
    raw = pl.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)], "code": [code] * n, "open": o, "high": high, "low": low,
        "close": c, "preclose": o, "volume": np.array(vols, dtype=float), "amount": c * np.array(vols), "turn": np.full(n, 2.0),
        "tradestatus": [1] * n, "is_st": [False] * n,
    })
    return engine.prepare_frame(raw)


def last_stage(frame: pl.DataFrame) -> dict:
    cls = stage.classify(features.compute(frame, None, rps=False), keep_evidence=True)
    return cls.row(cls.height - 1, named=True)


def test_decline() -> None:
    rng = np.random.default_rng(1)
    n = 300
    c = list(20 * np.exp(np.linspace(0, np.log(0.4), n) + rng.normal(0, 0.004, n)))
    v = [1.6e6 if i and c[i] < c[i - 1] else 0.8e6 for i in range(n)]
    row = last_stage(make(c, v))
    assert row["stage"] == "decline", row["stage"]
    assert row["score_decline"] >= 0.75


def test_markup() -> None:
    rng = np.random.default_rng(2)
    flat = list(10 * (1 + rng.normal(0, 0.004, 250)))
    rise = list(np.linspace(10.2, 13.0, 40))
    c = flat + rise
    v = [1e6] * 250 + [2.6e6 if i % 4 else 0.9e6 for i in range(40)]
    for k in range(290 - 40, 290):
        if k % 4 == 0 and k >= 252:
            c[k] = c[k - 1] * 0.997                  # 少数小阴线，缩量
    row = last_stage(make(c, v))
    assert row["stage"] == "markup", (row["stage"], {k: row[k] for k in row if k.startswith("score_")})


def test_distribution() -> None:
    rng = np.random.default_rng(3)
    up = list(10 * np.exp(np.linspace(0, np.log(2.5), 250) + rng.normal(0, 0.003, 250)))
    top = [up[-1] * (1 + rng.normal(0, 0.003)) for _ in range(12)]
    down = [top[-1] * 0.97, top[-1] * 0.93, top[-1] * 0.90]
    c = up + top + down
    v = [1e6] * 250 + [3.2e6] * 12 + [2.5e6] * 3
    sh = [0.0] * 250 + [0.02] * 12 + [0.0] * 3
    row = last_stage(make(c, v, sh))
    assert row["stage"] == "distribution", (row["stage"], {k: row[k] for k in row if k.startswith("score_")})
    assert row["ev_dist_stall"] and row["ev_dist_break"]


def test_accumulation() -> None:
    rng = np.random.default_rng(4)
    fall = list(20 * np.exp(np.linspace(0, np.log(0.5), 200) + rng.normal(0, 0.02, 200)))
    flat = list(10 * (1 + rng.normal(0, 0.006, 80)))
    c = fall + flat
    v = [1e6] * 200 + [1.5e6 if i and flat[i] > flat[i - 1] else 0.7e6 for i in range(80)]
    row = last_stage(make(c, v))
    assert row["stage"] == "accumulation", (row["stage"], {k: row[k] for k in row if k.startswith("score_")})


def test_washout() -> None:
    rng = np.random.default_rng(5)
    base = list(10 * (1 + rng.normal(0, 0.004, 200)))
    rally = list(np.linspace(10.1, 13.0, 30))
    pull = list(np.linspace(12.9, 12.45, 8))
    c = base + rally + pull
    v = [1e6] * 200 + [2.0e6] * 30 + [0.6e6] * 8
    row = last_stage(make(c, v))
    assert row["stage"] == "washout", (row["stage"], {k: row[k] for k in row if k.startswith("score_")})


def test_explain_marks_missing_chip_evidence() -> None:
    rng = np.random.default_rng(6)
    c = list(10 * np.exp(np.cumsum(rng.normal(0, 0.02, 300))))
    row = last_stage(make(c, [1e6] * 300))
    ex = stage.explain_row(row)
    assert ex["label"] in {s["label"] for s in stage.STAGES.values()} and ex["advice"]
    chip_items = [e for items in ex["evidence"].values() for e in items if e["needs"] == "chips"]
    assert chip_items and all(e["missing"] and e["ok"] is None for e in chip_items)
    assert set(ex["prereq"]) == set(stage.PREREQ) and ex["threshold"] == stage.THRESHOLD


def test_stage_stats_summarize_structure() -> None:
    rng = np.random.default_rng(7)
    frames = []
    for k in range(6):
        c = list(10 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, 700))))
        frames.append(make(c, list(rng.integers(5, 30, 700) * 1e5), code=f"60000{k}", start=date(2023, 6, 1)))
    frame = pl.concat(frames)
    cls = stage_stats.classify_frame(frame, None, use_chips=False)
    assert cls.height == frame.height and set(cls["stage"].unique().to_list()) <= set(stage.STAGES)
    res = stage_stats.summarize(cls, holds=(5,))
    assert set(res["holds"]["5"]) == set(stage.STAGES)
    some = [v for v in res["holds"]["5"].values() if v["all"].get("n")]
    assert some and all("excess" in v["all"] for v in some)
    v = stage_stats.verdict("unclear", {"holds": {"10": res["holds"]["5"]}}, hold="10")
    assert v is None or "text" in v


# ---------------------------------------------------------------- 排雷、大盘环境、板块

def test_riskscan_stock_levels(tmp_path, monkeypatch) -> None:
    from quant_web import config
    from quant_web.analysis import riskscan
    from quant_web.providers import store
    monkeypatch.setattr(config, "STOCK_LAB", tmp_path / "stock_lab")
    today = date(2026, 9, 24)
    bar = {"raw_close": 1.05, "amt20": 5e6}
    r = riskscan.scan_stock("600001", "*ST 某某", today, bar, date(2020, 1, 1), None, "decline", total_cap=3e8, news_titles=[])
    lv = {i["key"]: i["level"] for i in r["items"]}
    assert r["level"] == "red" and lv["st"] == "red" and lv["par"] == "red" and lv["cap"] == "red" and lv["liq"] == "red"
    assert lv["forecast"] == "none" and lv["unlock"] == "none" and lv["pledge"] == "none" and lv["loss"] == "none"
    assert lv["stage"] == "yellow"
    # 有了扩展数据以后
    store.save("forecast", pl.DataFrame({"code": ["600002"], "notice_date": [today - timedelta(days=10)],
                                         "period": [date(2026, 6, 30)], "kind": ["预亏"], "summary": ["亏了"]}))
    store.save("unlock", pl.DataFrame({"code": ["600002"], "date": [today + timedelta(days=5)], "shares": [1e8],
                                       "float_ratio": [25.0], "kind": ["首发原股东"]}))
    store.save("pledge", pl.DataFrame({"code": ["600002"], "date": [today], "pledge_ratio": [35.0], "pledge_shares": [1e8]}))
    fund = pl.DataFrame({"code": ["600002", "600002"], "report_date": [date(2024, 12, 31), date(2025, 12, 31)],
                         "avail_date": [date(2025, 4, 1), date(2026, 4, 1)], "net_profit": [-1e8, -2e8],
                         "net_profit_ttm": [-1e8, -2e8], "bvps": [1.0, 0.5]})
    r2 = riskscan.scan_stock("600002", "某公司", today, {"raw_close": 12.0, "amt20": 2e8}, date(2010, 1, 1), fund,
                             "markup", total_cap=5e10, news_titles=[("某公司收到证监会立案调查通知书", "2026-09-01")])
    lv2 = {i["key"]: i["level"] for i in r2["items"]}
    assert lv2["forecast"] == "red" and lv2["unlock"] == "red" and lv2["pledge"] == "yellow" and lv2["loss"] == "red"
    assert lv2["reg"] == "red" and lv2["stage"] == "green" and lv2["st"] == "green" and r2["level"] == "red"


def test_riskscan_many_flags() -> None:
    from quant_web.analysis import riskscan
    df = pl.DataFrame({"code": ["600001", "600002", "600003"], "name": ["ST甲", "乙", "丙"],
                       "raw_close": [5.0, 1.5, 20.0], "amt20": [1e8, 1e8, 5e6], "stage": ["markup", "unclear", "distribution"]})
    out = {r["code"]: r for r in riskscan.scan_many(df, date(2026, 9, 24)).to_dicts()}
    assert out["600001"]["risk"] == "red" and "ST" in out["600001"]["risk_reasons"]
    assert out["600002"]["risk"] == "yellow" and "低价股" in out["600002"]["risk_reasons"]
    assert out["600003"]["risk"] == "red" and "疑似出货" in out["600003"]["risk_reasons"]


def _market_frame(n_codes: int = 40, n_days: int = 700) -> pl.DataFrame:
    rng = np.random.default_rng(11)
    frames = []
    mkt = np.cumsum(rng.normal(0.0003, 0.012, n_days))
    for k in range(n_codes):
        c = list(10 * np.exp(mkt + np.cumsum(rng.normal(0, 0.015, n_days))))
        frames.append(make(c, list(rng.integers(5, 30, n_days) * 1e5), code=f"600{k:03d}", start=date(2023, 6, 1)))
    return pl.concat(frames)


def test_regime_and_sectors() -> None:
    from quant_web.analysis import regime, sectors
    frame = _market_frame()
    res = regime.compute(frame)
    assert res["regime"] in regime.REGIMES and res["cap"] in (0.7, 0.4) and len(res["components"]) == 6
    assert res["history"] and set(res["stats"]) == set(regime.REGIMES)
    assert isinstance(res["insights"], list)
    uni = pl.DataFrame({"code": [f"600{k:03d}" for k in range(40)], "name": [f"股{k}" for k in range(40)],
                        "industry": ["甲行业"] * 20 + ["乙行业"] * 20})
    sec = sectors.compute(frame, uni)
    assert {r["industry"] for r in sec["rows"]} == {"甲行业", "乙行业"}
    assert all(len(r["leaders"]) <= 3 for r in sec["rows"]) and sec["date"]
