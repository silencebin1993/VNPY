"""quant_web 第三版：公式验证（事件研究）的交易规则与统计口径（全部离线）"""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from quant_web.formula import engine, validate
from quant_web.formula.parser import compile_formula
from quant_web.predict import costs as costs_mod


def frame_from(closes: list[float], opens: list[float] | None = None, code: str = "600000",
               start: date = date(2024, 1, 1)) -> pl.DataFrame:
    n = len(closes)
    c = np.array(closes, dtype=float)
    o = np.array(opens if opens is not None else closes, dtype=float)
    pre = np.concatenate([[c[0]], c[:-1]])
    days = [start + timedelta(days=i) for i in range(n)]
    raw = pl.DataFrame({"date": days, "code": [code] * n, "open": o, "high": np.maximum(o, c) * 1.001,
                        "low": np.minimum(o, c) * 0.999, "close": c, "preclose": pre, "volume": np.full(n, 1e6),
                        "amount": c * 1e6, "turn": np.full(n, 2.0), "tradestatus": [1] * n, "is_st": [False] * n})
    return engine.prepare_frame(raw)


def test_forward_returns_costs_limit_up_and_delayed_exit() -> None:
    closes = [10.0] * 70 + [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 12.5, 12.5]
    fr = validate.add_trade_columns(frame_from(closes))
    out = validate.forward_returns(fr, 3, costs_mod.DEFAULT)
    i = 70                                                     # 信号日：次日开盘 10.5 买，持有 3 天 → 第 3 天收盘 11.5 卖
    row = out.row(i, named=True)
    f = (1 - 0.001) * (1 - 0.00025 - 0.0005) / ((1 + 0.001) * (1 + 0.00025))
    assert row["filled"] and row["net"] == pytest.approx(11.5 / 10.5 * f - 1)
    # 次日开盘就涨停：买不进
    closes2 = [10.0] * 70 + [10.0, 11.0, 11.0, 11.0, 11.0, 11.0]
    opens2 = [10.0] * 70 + [10.0, 11.0, 11.0, 11.0, 11.0, 11.0]
    out2 = validate.forward_returns(validate.add_trade_columns(frame_from(closes2, opens2)), 2, costs_mod.DEFAULT)
    assert out2.row(70, named=True)["filled"] is False and out2.row(70, named=True)["net"] is None
    # 卖出那天跌停封死：顺延到能卖的那天
    closes3 = [10.0] * 70 + [10.0, 10.0, 9.0, 9.5, 9.6, 9.7]
    fr3 = validate.add_trade_columns(frame_from(closes3))
    out3 = validate.forward_returns(fr3, 2, costs_mod.DEFAULT)
    r3 = out3.row(70, named=True)
    assert r3["exit_date"] == fr3["date"][73] and r3["net"] == pytest.approx(9.5 / 10.0 * f - 1)


def test_forward_returns_books_delisting_at_last_close() -> None:
    """持有期间退市（之后没有行情）：按最后收盘价算亏损，不能当成"还没结束"丢掉"""
    a = frame_from([10.0] * 72 + [10.0, 8.0, 6.0], code="600999")            # 第 74 天之后就没有行情了（退市）
    b = frame_from([10.0] * 80, code="600888")                             # 另一只正常交易到第 79 天
    fr = validate.add_trade_columns(pl.concat([a, b]).sort(["code", "date"]))
    out = validate.forward_returns(fr, 5, costs_mod.DEFAULT, delisted={"600999"})
    r = out.filter((pl.col("code") == "600999") & (pl.col("date") == a["date"][72])).row(0, named=True)
    f = (1 - 0.001) * (1 - 0.00025 - 0.0005) / ((1 + 0.001) * (1 + 0.00025))
    assert r["filled"] and r["exit_date"] == a["date"][74] and r["net"] == pytest.approx(6.0 / 8.0 * f - 1)
    # 没退市、只是数据到头了（还在持有期）：仍然是"没结束"
    out2 = validate.forward_returns(fr, 5, costs_mod.DEFAULT, delisted=set())
    assert out2.filter((pl.col("code") == "600999") & (pl.col("date") == a["date"][72]))["net"][0] is None


def planted(n_codes: int = 30, n_days: int = 700, seed: int = 1, edge: float = 0.01) -> pl.DataFrame:
    """在随机游走里埋一个规律：某天大涨超过 6% 后，接下来 5 天每天多涨 edge"""
    rng = np.random.default_rng(seed)
    frames = []
    start = date(2024, 1, 1)                   # 跨过 2025-07-01，才有留出期
    for k in range(n_codes):
        r = rng.normal(0, 0.025, n_days)
        boost = np.zeros(n_days)
        for t in range(1, n_days):
            if r[t - 1] > 0.06:
                boost[t:t + 5] += edge
        c = 10 * np.exp(np.cumsum(r + boost))
        o = np.concatenate([[c[0]], c[:-1]]) * np.exp(rng.normal(0, 0.002, n_days))
        frames.append(pl.DataFrame({
            "date": [start + timedelta(days=i) for i in range(n_days)], "code": [f"600{k:03d}"] * n_days,
            "open": o, "high": np.maximum(o, c) * 1.005, "low": np.minimum(o, c) * 0.995, "close": c,
            "preclose": np.concatenate([[c[0]], c[:-1]]), "volume": np.full(n_days, 1e6), "amount": c * 1e6,
            "turn": np.full(n_days, 2.0), "tradestatus": [1] * n_days, "is_st": [False] * n_days,
        }))
    return engine.prepare_frame(pl.concat(frames))


@pytest.fixture(scope="module")
def planted_frame() -> pl.DataFrame:
    return planted()


def test_event_study_finds_planted_edge(planted_frame: pl.DataFrame) -> None:
    res = validate.event_study(compile_formula("XG:C>REF(C,1)*1.06;"), planted_frame, holds=(5,), boards=("main",))
    h = res["holds"]["5"]
    assert h["signals"] > 100
    assert h["all"]["excess"] > 0.02 and h["all"]["t"] is not None and h["all"]["t"] > 2
    assert h["holdout"]["n"] > 0 and h["selection"]["n"] > 0              # 按 2025-07-01 分段
    assert h["verdict"]["credible"] is True
    assert res["recent"] and {"code", "date", "net", "excess"} <= set(res["recent"][0])


def test_event_study_no_edge_for_random_formula(planted_frame: pl.DataFrame) -> None:
    res = validate.event_study(compile_formula("XG:REF(C,3)>REF(C,4) AND REF(C,7)<REF(C,8) AND C<REF(C,1);"),
                               planted_frame, holds=(10,))
    h = res["holds"]["10"]
    assert abs(h["all"]["excess"]) < 0.01
    assert h["verdict"]["credible"] is False


def test_dedupe_counts_overlapping_signals_once(planted_frame: pl.DataFrame) -> None:
    prog = compile_formula("XG:C>0;")                                   # 天天都有信号
    a = validate.event_study(prog, planted_frame, holds=(5,), dedupe=True)["holds"]["5"]["signals"]
    b = validate.event_study(prog, planted_frame, holds=(5,), dedupe=False)["holds"]["5"]["signals"]
    assert b > 4 * a                                                    # 去重后大约每 5 天一次
    res = validate.event_study(prog, planted_frame, holds=(5,), dedupe=False)["holds"]["5"]["all"]
    assert abs(res["excess"]) < 1e-9                                    # 全买 = 基准本身，超额为 0
