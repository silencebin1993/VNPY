"""quant_web 第二版：费用（印花税按日期）、逐笔成交模拟（模拟盘契约）、波段标签、净化、波段模型端到端、
回测新增字段（按日 t 值、同池随机基准、选择期/留出期）、"退"视同 ST、设置按模型给默认值。全部离线。"""
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from quant_web import settings as settings_mod
from quant_web.market import sentiment
from quant_web.market import universe as uni_mod
from quant_web.predict import costs, execution, features, labels, model, scoring, swing
from quant_web.predict.backtest import run_backtest, simulate_trades
from quant_web.predict.limits import add_limit_columns, limit_price, limit_ratio


def _bdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d: date = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- 费用

def test_stamp_duty_by_date() -> None:
    assert costs.stamp_duty(date(2023, 8, 25)) == 0.001
    assert costs.stamp_duty(date(2023, 8, 27)) == 0.001
    assert costs.stamp_duty(date(2023, 8, 28)) == 0.0005          # 减半当天生效
    assert costs.stamp_duty(date(2026, 1, 5)) == 0.0005
    arr = np.array(["2019-01-02", "2023-08-25", "2023-08-28", "NaT"], dtype="datetime64[D]")
    assert costs.stamp_duty_array(arr).tolist() == [0.001, 0.001, 0.0005, 0.0005]
    df = pl.DataFrame({"d": [date(2023, 8, 25), date(2023, 8, 28)]})
    assert df.select(costs.stamp_duty_expr(pl.col("d")))["literal"].to_list() == [0.001, 0.0005]
    # 净收益 = 卖价×(1-滑点)×(1-佣金-印花税) / (买价×(1+滑点)×(1+佣金)) - 1
    got = costs.net_return(10.0, 11.0, date(2022, 5, 6))
    assert got == pytest.approx(11 * 0.999 * (1 - 0.00025 - 0.001) / (10 * 1.001 * 1.00025) - 1)
    got2 = costs.net_return(10.0, 11.0, date(2024, 5, 6))
    assert got2 - got == pytest.approx(11 * 0.999 * 0.0005 / (10 * 1.001 * 1.00025))
    fixed = costs.from_trade({"stamp_by_date": False, "stamp_duty": 0.002, "fee_rate": 0.0003, "slippage": 0.0})
    assert fixed.stamp_on(date(2019, 1, 2)) == 0.002 and fixed.commission == 0.0003 and fixed.slippage == 0.0
    assert costs.from_trade({"stamp_duty": 0.002}).stamp is None               # 默认按日期


# ---------------------------------------------------------------- 逐笔成交模拟 / 波段标签

D: list[date] = _bdays(date(2024, 1, 2), 14)


def _stock(code: str, bars: list[tuple[float, float, float, float]], start: int = 0, pre: float = 10.0,
           ratio: float = 0.1) -> list[dict]:
    """bars: (open, high, low, close)，从 D[start] 起逐日；涨跌停价按昨收计算"""
    rows: list[dict] = []
    for i, (o, h, lo, c) in enumerate(bars):
        rows.append({"date": D[start + i], "code": code, "open": o, "high": h, "low": lo, "close": c,
                     "preclose": pre, "limit_up": limit_price(pre, ratio), "limit_down": limit_price(pre, -ratio)})
        pre = c
    return rows


def _lu_path(pre: float, n: int) -> list[tuple[float, float, float, float]]:
    """连续 n 天收盘涨停（开盘在昨收附近，不是一字）"""
    out: list[tuple[float, float, float, float]] = []
    for _ in range(n):
        up: float = limit_price(pre, 0.1)
        out.append((round(pre * 1.01, 2), up, round(pre * 0.99, 2), up))
        pre = up
    return out


def _ld_path(pre: float, n: int, one_word: bool = False) -> list[tuple[float, float, float, float]]:
    out: list[tuple[float, float, float, float]] = []
    for _ in range(n):
        dn: float = limit_price(pre, -0.1)
        out.append((dn, dn, dn, dn) if one_word else (round(pre * 0.99, 2), round(pre * 1.0, 2), dn, dn))
        pre = dn
    return out


def _filler(n: int = len(D)) -> list[dict]:
    return _stock("999999", [(10.0, 10.0, 10.0, 10.0)] * n)


def _panel(*stocks: list[dict], n_cal: int = len(D)) -> pl.DataFrame:
    rows: list[dict] = [r for s in stocks for r in s] + _filler(n_cal)
    return pl.DataFrame(rows)


def _labels(panel: pl.DataFrame, code: str, day: date = D[0], delisted: set[str] | None = None) -> dict:
    feat = pl.DataFrame({"date": [day], "code": [code]})
    return labels.swing_labels(feat, panel, delisted=delisted or set()).row(0, named=True)


def test_swing_label_first_non_limit_close() -> None:
    # D0 信号 → D1 开盘 10.2 买 → D2 收盘涨停（不卖）→ D3 收盘没涨停，按收盘价卖
    bars = [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _lu_path(10.5, 1) + [(11.6, 11.8, 11.2, 11.3)]
    panel = _panel(_stock("600001", bars))
    r = _labels(panel, "600001")
    assert r["status"] == "closed" and r["fill"] is True
    assert r["entry_date"] == D[1] and r["exit_date"] == D[3] and r["label_end"] == D[3] and r["hold_days"] == 2
    assert r["net"] == pytest.approx(costs.net_return(10.2, 11.3, D[3]))
    assert r["y"] == 1 and "收盘没有涨停" in r["exit_reason"]
    # 与模拟盘的逐笔模拟一致
    sim = simulate_trades(pl.DataFrame({"signal_date": [D[0]], "code": ["600001"], "kind": ["swing"],
                                        "exit_rule": ["until_break_close"]}), panel, delisted=set())
    assert sim["ret"][0] == pytest.approx(r["net"]) and sim["status"][0] == "closed"
    assert sim["entry"][0] == pytest.approx(10.2 * 1.001) and sim["exit"][0] == pytest.approx(11.3 * 0.999)


def test_swing_label_rolls_on_limit_down_and_forced_exit() -> None:
    # D2 收盘没涨停但收在跌停 → 卖不出，顺延到 D3 收盘
    bars = [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _ld_path(10.5, 1) + [(9.3, 9.8, 9.2, 9.6)]
    r = _labels(_panel(_stock("600002", bars)), "600002")
    assert r["exit_date"] == D[3] and r["net"] == pytest.approx(costs.net_return(10.2, 9.6, D[3]))
    assert "顺延" in r["exit_reason"] and r["hold_days"] == 2
    # 一直涨停：第 5 根（买入日为第 1 根）收盘强制卖
    bars2 = [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _lu_path(10.5, 6)
    r2 = _labels(_panel(_stock("600003", bars2)), "600003")
    assert r2["exit_date"] == D[5] and r2["hold_days"] == 4 and "第5个交易日" in r2["exit_reason"]
    assert r2["exit"] == pytest.approx(bars2[5][3] * 0.999)
    # 第 5 根收盘跌停 → 顺延，第 7 根仍跌停也按收盘价计
    bars3 = [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _lu_path(10.5, 3)
    bars3 += _ld_path(bars3[-1][3], 4)
    r3 = _labels(_panel(_stock("600004", bars3)), "600004")
    assert r3["exit_date"] == D[7] and "第7个交易日" in r3["exit_reason"] and r3["hold_days"] == 6


def test_swing_label_unfillable_suspended_delisted_and_panel_end() -> None:
    # 开盘涨停（一字）买不进
    lu = _stock("600005", [(10.0, 10.0, 10.0, 10.0), (11.0, 11.0, 11.0, 11.0), (11.5, 12.0, 11.2, 11.4)])
    r = _labels(_panel(lu), "600005")
    assert r["status"] == "unfilled" and r["fill"] is False and r["net"] is None and r["label_end"] is None
    # T+1 停牌 → 买不到
    sus = _stock("600006", [(10.0, 10.0, 10.0, 10.0)]) + _stock("600006", [(10.1, 10.3, 10.0, 10.2)], start=2)
    r = _labels(_panel(sus), "600006")
    assert r["status"] == "unfilled" and "停牌" in r["exit_reason"]
    # 买入后一直涨停，然后退市（之后没有行情）：在退市名单里 → 按最后收盘价计；不在 → 持有中（标签为空）
    dl = _stock("600007", [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _lu_path(10.5, 1))
    r = _labels(_panel(dl), "600007", delisted={"600007"})
    assert r["status"] == "closed" and r["exit_date"] == D[2] and "退市" in r["exit_reason"]
    assert r["net"] == pytest.approx(costs.net_return(10.2, limit_price(10.5, 0.1), D[2]))
    r = _labels(_panel(dl), "600007")
    assert r["status"] == "holding" and r["net"] is None and r["fill"] is True and r["label_end"] is None


def test_simulate_trades_statuses_panel_ends_mid_trade() -> None:
    n_cal: int = 5                                               # 面板只到 D[4]
    hold = _stock("600010", [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _lu_path(10.5, 3))
    done = _stock("600011", [(10.0, 10.0, 10.0, 10.0), (10.0, 10.6, 9.9, 10.5), (10.6, 10.9, 10.5, 10.8),
                             (10.8, 10.9, 10.5, 10.6), (10.6, 10.7, 10.4, 10.5)])
    lu = _stock("600012", [(10.0, 10.0, 10.0, 10.0), (11.0, 11.0, 11.0, 11.0), (11.5, 12.0, 11.2, 11.4),
                           (11.4, 11.5, 11.2, 11.3), (11.3, 11.4, 11.2, 11.3)])
    panel = _panel(hold, done, lu, n_cal=n_cal)
    sig = pl.DataFrame({
        "signal_date": [D[0], D[0], D[0], D[4], D[0]],
        "code": ["600010", "600011", "600012", "600011", "600011"],
        "kind": ["swing", "streak", "swing", "first", "first"],
        "exit_rule": ["until_break_close", "next_close", "until_break_close", "next_open", "until_break"],
        "stop_loss_pct": [0.0, 0.0, 0.0, 0.0, 0.05],
    })
    out = simulate_trades(sig, panel, {"max_gap_pct": 7.0}, delisted=set())
    assert out.columns[:5] == sig.columns
    assert out["status"].to_list() == ["holding", "closed", "unfilled", "pending", "closed"]
    h = out.row(0, named=True)
    last_close: float = hold[4]["close"]
    assert h["exit_date"] is None and h["exit"] is None and h["hold_days"] == 3 and h["entry_date"] == D[1]
    assert h["ret"] == pytest.approx(costs.net_return(10.2, last_close, D[4]))          # 按最新收盘价估算
    c = out.row(1, named=True)
    assert c["exit_date"] == D[2] and c["ret"] == pytest.approx(costs.net_return(10.0, 10.8, D[2]))
    assert out.row(2, named=True)["ret"] is None and out.row(3, named=True)["entry_date"] is None
    assert out["hold_days"].dtype == pl.Int32 and out["entry"].dtype == pl.Float64
    # until_break：D1（买入日）收盘没涨停 → D2 开盘卖出
    ub = out.row(4, named=True)
    assert ub["exit_date"] == D[2] and ub["ret"] == pytest.approx(costs.net_return(10.0, 10.6, D[2]))
    assert "断板" in ub["exit_reason"]
    # 只传一只股票的面板时，给全市场日历才能认出买入日停牌
    sus = _stock("600013", [(10.0, 10.0, 10.0, 10.0)]) + _stock("600013", [(10.1, 10.3, 10.0, 10.2)], start=2)
    one = pl.DataFrame(sus)
    s1 = pl.DataFrame({"signal_date": [D[0]], "code": ["600013"], "kind": ["swing"], "exit_rule": ["next_close"]})
    assert simulate_trades(s1, one, delisted=set())["entry_date"][0] == D[2]
    assert simulate_trades(s1, one, delisted=set(), calendar=D[:5])["status"][0] == "unfilled"
    # 空信号表
    empty = simulate_trades(sig.head(0), panel)
    assert empty.height == 0 and {"status", "ret", "hold_days"} <= set(empty.columns)


def test_simulate_trades_gap_and_stop() -> None:
    gap = _stock("600020", [(10.0, 10.0, 10.0, 10.0), (10.8, 10.9, 10.5, 10.6), (10.6, 10.7, 10.4, 10.5)])
    stop = _stock("600021", [(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.9, 10.0), (9.8, 9.9, 9.4, 9.6)])
    panel = _panel(gap, stop)
    sig = pl.DataFrame({"signal_date": [D[0], D[0]], "code": ["600020", "600021"], "kind": ["swing"] * 2,
                        "exit_rule": ["next_close", "next_close"], "stop_loss_pct": [0.0, 0.05]})
    out = simulate_trades(sig, panel, {"max_gap_pct": 7.0, "slippage": 0.0}, delisted=set())
    assert out["status"].to_list() == ["unfilled", "closed"] and "超过上限" in out["exit_reason"][0]
    assert out["exit"][1] == pytest.approx(9.5) and "止损" in out["exit_reason"][1]


def test_run_backtest_matches_per_trade_simulation() -> None:
    """资金充足、不触发最低佣金时，组合回测里每笔的收益与逐笔模拟一致（同一套规则与费用）"""
    stocks: list[list[dict]] = [
        _stock("600030", [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _lu_path(10.5, 2)
               + [(12.9, 13.0, 12.5, 12.6), (12.6, 12.7, 12.4, 12.5)]),
        _stock("600031", [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _ld_path(10.5, 1)
               + [(9.3, 9.8, 9.2, 9.6), (9.6, 9.7, 9.5, 9.6)]),
    ]
    panel = _panel(*stocks)
    oos = pl.DataFrame({
        "date": [D[0], D[0]], "code": ["600030", "600031"], "prob": [0.6, 0.5],
        "bias": pl.Series([None, None], dtype=pl.Float64), "y": pl.Series([1, 0], dtype=pl.Int8),
        "close": [10.0, 10.0], "float_cap": [50.0, 50.0], "board": ["main", "main"], "is_st": [False, False],
        "streak": pl.Series([0, 0], dtype=pl.Int16), "one_word": [False, False],
    })
    ps = {"kind": "streak", "exclude_st": False, "boards": ["main"], "top_n": 5, "threshold": 0.0,
          "min_price": 0, "max_price": 1e9, "min_float_cap": 0, "max_float_cap": 1e9, "streak_min": 0, "streak_max": 99}
    ts = {"capital": 1e8, "position_pct": 0.1, "max_positions": 5, "exit_rule": "until_break_close",
          "fee_rate": 0.00025, "slippage": 0.001, "max_gap_pct": 9.0}
    bt = run_backtest(oos, panel, ps, ts, delisted=set(), baseline_seeds=0)
    got = {t["code"]: t for t in bt["trades"]}
    sim = simulate_trades(oos.select(pl.col("date").alias("signal_date"), "code").with_columns(
        pl.lit("streak").alias("kind"), pl.lit("until_break_close").alias("exit_rule")), panel, ts, delisted=set())
    for r in sim.iter_rows(named=True):
        t = got[r["code"]]
        assert t["exit_date"] == r["exit_date"].isoformat() and t["hold_days"] == r["hold_days"]
        assert t["ret"] == pytest.approx(r["ret"], abs=2e-5)
    assert got["600030"]["exit_date"] == D[4].isoformat() and "顺延" in got["600031"]["reason"]


def test_backtest_limit_down_open_not_one_word_fills() -> None:
    # next_open：D2 开盘就在跌停价，但盘中打开过（最高价 > 跌停价）→ 按跌停价卖出，不再顺延
    dn: float = limit_price(9.8, -0.1)
    rows = _stock("600040", [(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.7, 9.8), (dn, 9.5, dn, 9.2),
                             (9.0, 9.3, 8.9, 9.1)])
    oos = pl.DataFrame({"date": [D[0]], "code": ["600040"], "prob": [0.5], "bias": pl.Series([None], dtype=pl.Float64),
                        "y": pl.Series([1], dtype=pl.Int8), "close": [10.0], "float_cap": [50.0], "board": ["main"],
                        "is_st": [False], "streak": pl.Series([0], dtype=pl.Int16), "one_word": [False]})
    ts = {"capital": 100_000, "position_pct": 0.1, "max_positions": 5, "exit_rule": "next_open", "slippage": 0.0}
    bt = run_backtest(oos, _panel(rows), {"kind": "streak", "exclude_st": False}, ts, delisted=set(), baseline_seeds=0)
    tr = bt["trades"][0]
    assert tr["exit_date"] == D[2].isoformat() and tr["exit"] == pytest.approx(dn) and "顺延" not in tr["reason"]


def test_backtest_new_fields_and_baseline_determinism() -> None:
    rng = np.random.default_rng(3)
    days: list[date] = _bdays(date(2025, 5, 1), 60)
    rows: list[dict] = []
    codes: list[str] = [f"600{i:03d}" for i in range(12)]
    for code in codes:
        pre: float = 10.0
        for d in days:
            close: float = round(pre * (1 + rng.normal(0, 0.02)), 2)
            o: float = round(pre * (1 + rng.normal(0, 0.005)), 2)
            rows.append({"date": d, "code": code, "open": o, "high": max(o, close) + 0.05, "low": min(o, close) - 0.05,
                         "close": close, "preclose": pre, "limit_up": limit_price(pre, 0.1),
                         "limit_down": limit_price(pre, -0.1)})
            pre = close
    panel = pl.DataFrame(rows)
    oos = panel.filter(pl.col("date") < days[-3]).select("date", "code", "close").with_columns(
        pl.Series("pred", rng.normal(0.005, 0.01, panel.filter(pl.col("date") < days[-3]).height)),
        pl.lit(None, pl.Float64).alias("bias"), pl.lit(0.01).alias("net"), pl.lit(1, pl.Int8).alias("y"),
        pl.lit(50.0).alias("float_cap"), pl.lit("main").alias("board"), pl.lit(False).alias("is_st"),
        pl.lit(0, pl.Int16).alias("streak"), pl.lit(False).alias("one_word"),
    )
    ps = {"kind": "swing", "exclude_st": True, "boards": ["main"], "top_n": 3, "threshold": 0.0}
    ts = {"capital": 1e6, "position_pct": 0.04, "max_positions": 25, "exit_rule": "until_break_close"}
    a = run_backtest(oos, panel, ps, ts, delisted=set())
    b = run_backtest(oos, panel, ps, ts, delisted=set())
    assert a["baseline"] == b["baseline"] and a["baseline"]["seeds"] == 20
    assert {"avg_return", "win_rate", "total_return", "cagr"} <= set(a["baseline"])
    assert a["metrics"]["daily_t"] is not None and a["metrics"]["trades"] > 20
    assert set(a["by_period"]) == {"selection", "holdout"}
    assert a["by_period"]["selection"]["start"] < "2025-07-01" <= a["by_period"]["holdout"]["start"]
    # 两段各用全新账户：跨分界时仍持有的股票在整段回测里会跳过（已持有），分段时不会，所以只差几笔
    both: int = a["by_period"]["selection"]["trades"] + a["by_period"]["holdout"]["trades"]
    assert a["metrics"]["trades"] <= both <= a["metrics"]["trades"] + 5
    # 分段的随机基准：和策略一样，每段用全新账户单独回测
    for part in ("selection", "holdout"):
        pb: dict = a["by_period"][part]["baseline"]
        assert {"avg_return", "win_rate", "trades", "daily_t", "total_return", "seeds"} <= set(pb)
        assert pb["seeds"] == 20
    assert "by_period" not in a["baseline"]
    # 门槛为 0（每天都挑满 top_n）：同日同数量基准和普通随机基准完全一样
    assert a["baseline_matched"]["same_as_baseline"] is True
    assert a["baseline_matched"]["avg_return"] == a["baseline"]["avg_return"]
    assert a["by_period"]["holdout"]["baseline_matched"]["trades"] == a["by_period"]["holdout"]["baseline"]["trades"]
    assert a["calibration"] and a["calibration"][0]["bucket"].endswith("%")          # 回归：按预期收益分桶
    # 门槛起作用：同日同数量基准只在出信号的日子、挑同样多只 → 笔数和策略相当，比“天天挑满”少
    g = run_backtest(oos, panel, {**ps, "threshold": 0.012}, ts, delisted=set())
    assert g["baseline_matched"]["same_as_baseline"] is False
    assert g["baseline_matched"]["trades"] < g["baseline"]["trades"]
    assert abs(g["baseline_matched"]["trades"] - g["metrics"]["trades"]) <= 0.2 * g["metrics"]["trades"] + 3
    assert g["by_period"]["selection"]["baseline_matched"]["seeds"] == 20
    # 波段门槛按预期收益：门槛 5% 时一只都不选
    c = run_backtest(oos, panel, {**ps, "threshold": 0.05}, ts, delisted=set(), baseline_seeds=0)
    assert c["metrics"]["trades"] == 0 and c["baseline_matched"] is None


def test_period_baseline_uses_fresh_account_when_full_random_account_goes_broke() -> None:
    """整段随机账户在选择期几乎亏光（后面连一手都买不起）时，留出期的随机基准必须用全新账户重算，
    不能拿整段账户在留出期剩下的寥寥几笔来比（会把"策略不如随机"颠倒成"策略好过随机"）"""
    sel_days: list[date] = _bdays(date(2025, 5, 1), 40)
    hold_days: list[date] = _bdays(date(2025, 7, 1), 30)
    rows: list[dict] = []
    losers: list[str] = [f"600{i:03d}" for i in range(100, 106)]
    winners: list[str] = [f"600{i:03d}" for i in range(200, 206)]
    for code in losers:              # 选择期每天开盘到收盘跌 7%（没跌停），留出期横盘
        pre: float = 10.0
        for d in sel_days + hold_days:
            o: float = pre
            c: float = round(o * 0.93, 2) if d < date(2025, 7, 1) else o
            rows.append({"date": d, "code": code, "open": o, "high": o, "low": c, "close": c, "preclose": pre,
                         "limit_up": limit_price(pre, 0.1), "limit_down": limit_price(pre, -0.1)})
            pre = c
    for code in winners:             # 留出期才上市交易，50 元一股、每天涨 1%（一手 5000 元）
        pre = 50.0
        for d in hold_days:
            o = pre
            c = round(o * 1.01, 2)
            rows.append({"date": d, "code": code, "open": o, "high": c, "low": o, "close": c, "preclose": pre,
                         "limit_up": limit_price(pre, 0.1), "limit_down": limit_price(pre, -0.1)})
            pre = c
    panel = pl.DataFrame(rows)
    cand = pl.concat([
        panel.filter(pl.col("code").is_in(losers) & (pl.col("date") < date(2025, 7, 1))),
        panel.filter(pl.col("code").is_in(winners) & (pl.col("date") < hold_days[-3])),
    ]).select("date", "code", "close")
    rng = np.random.default_rng(11)
    oos = cand.with_columns(
        pl.Series("pred", rng.normal(0.02, 0.01, cand.height)), pl.lit(None, pl.Float64).alias("bias"),
        pl.lit(0.01).alias("net"), pl.lit(1, pl.Int8).alias("y"), pl.lit(50.0).alias("float_cap"),
        pl.lit("main").alias("board"), pl.lit(False).alias("is_st"), pl.lit(0, pl.Int16).alias("streak"),
        pl.lit(False).alias("one_word"),
    )
    ps = {"kind": "swing", "exclude_st": True, "boards": ["main"], "top_n": 3, "threshold": 0.0,
          "max_float_cap": 100_000, "max_price": 10_000}
    ts = {"capital": 100_000, "position_pct": 0.3, "max_positions": 3, "exit_rule": "next_open", "slippage": 0.0}
    r = run_backtest(oos, panel, ps, ts, delisted=set(), baseline_seeds=5)
    # 整段账户：选择期亏到连一手都买不起，留出期几乎没有成交
    assert r["metrics"]["final_equity"] < 30_000 and r["metrics"]["skipped_lot"] > 0
    hold: dict = r["by_period"]["holdout"]
    pb: dict = hold["baseline"]
    # 留出期：策略和随机基准都是全新账户，笔数相当、都赚钱（每天涨 1%）
    assert hold["trades"] >= 30 and pb["trades"] >= 0.8 * hold["trades"]
    assert hold["avg_return"] > 0 and pb["avg_return"] > 0
    assert pb["avg_return"] == pytest.approx(hold["avg_return"], abs=0.005)


# ---------------------------------------------------------------- 净化与回归模型

def _toy_reg(n_days: int = 900, per_day: int = 25, seed: int = 5) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    days: list[date] = _bdays(date(2019, 1, 2), n_days)
    n: int = n_days * per_day
    data: dict = {"date": [d for d in days for _ in range(per_day)], "code": [f"{i % 5000:06d}" for i in range(n)]}
    for f in features.feature_columns("swing"):
        data[f] = rng.normal(0, 1, n).astype(np.float32)
    data["net"] = 0.01 * data["turnover"] - 0.005 * data["ret_5"] + rng.normal(0, 0.03, n)
    df = pl.DataFrame(data)
    hold = pl.Series(rng.integers(1, 8, n))
    return df.with_columns((pl.col("date") + pl.duration(days=hold)).alias("label_end"), pl.lit(True).alias("fill"),
                           pl.lit("main").alias("board"))


def test_purge_training_rows(monkeypatch) -> None:
    ds = _toy_reg()
    folds = model.make_folds(ds["date"].unique().to_list(), 2021)
    seen: list[tuple[date, date, date]] = []
    real = model.fit_regression

    def spy(df: pl.DataFrame, cols: list[str], label: str = "net", params=None, num_boost_round=None):
        seen.append((df["date"].max(), df["label_end"].max(), df["date"].min()))
        return real(df, cols, label, params, num_boost_round)

    monkeypatch.setattr(model, "fit_regression", spy)
    oos, info = model.train_walk_forward_reg(ds, features.feature_columns("swing"), folds[:2],
                                             train_filter=pl.col("fill"))
    for (dmax, lmax, _), f in zip(seen, folds, strict=False):
        assert lmax <= f.train_end and dmax <= f.train_end
    # 被净化掉的：date ≤ train_end 但标签在 train_end 之后揭晓的行
    f0 = folds[0]
    n_purged = ds.filter((pl.col("date") <= f0.train_end) & (pl.col("label_end") > f0.train_end)).height
    assert n_purged > 0 and info[0]["n_train"] == ds.filter(pl.col("date") <= f0.train_end).height - n_purged
    # 分类模型同样按 next_date/label_end 净化
    cls = pl.DataFrame({"date": [date(2021, 12, 30), date(2021, 12, 31)], "code": ["1", "2"],
                        "label_end": [date(2021, 12, 31), date(2022, 1, 3)]})
    assert cls.filter(model.purge_mask(cls, date(2021, 12, 31))).height == 1
    assert cls.drop("label_end").with_columns(pl.col("date").alias("next_date")).filter(
        model.purge_mask(cls.drop("label_end").with_columns(pl.col("date").alias("next_date")), date(2021, 12, 30))
    ).height == 1
    # 回归预测：pred = bias + Σ 贡献；学到 turnover 的正向作用
    assert np.allclose(oos["pred"].to_numpy(), oos["bias"].to_numpy() + sum(oos[c].to_numpy()
                                                                             for c in model.CONTRIB_COLUMNS))
    lab = oos.join(ds.select("date", "code", "net"), on=["date", "code"])
    assert model.day_ic(lab["pred"].to_numpy(), lab["net"].to_numpy(), lab["date"].to_numpy()) > 0.1


def test_trade_metrics_and_random_baseline() -> None:
    days: list[date] = _bdays(date(2024, 1, 2), 6)
    picks = pl.DataFrame({"date": [days[0], days[0], days[2]], "fill": [True, False, True],
                          "net": [0.05, None, -0.02], "pred": [0.02, 0.03, 0.02]})
    m = swing.trade_metrics(picks, days, n=5, sleeves=5)
    assert m["n"] == 2 and m["signals"] == 3 and m["fill_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["mean"] == pytest.approx(0.015) and m["win_rate"] == 0.5 and m["days"] == 6
    assert m["daily_mean"] == pytest.approx((0.05 / 5 - 0.02 / 5) / 6)
    # 5 份资金错开：第 1 天用第 1 份，第 3 天用第 3 份
    assert m["total_return"] == pytest.approx(round((1.01 + 0.996 + 3) / 5 - 1, 4))
    pool = pl.DataFrame({"date": [d for d in days for _ in range(8)], "code": [f"{i}" for i in range(48)],
                         "fill": [True] * 48, "net": list(np.linspace(-0.05, 0.05, 48)), "pred": [0.0] * 48})
    r1 = swing.random_baseline(pool, days, 3, seeds=5)
    r2 = swing.random_baseline(pool, days, 3, seeds=5)
    assert r1 == r2 and r1["n"] == 18 and r1["seeds"] == 5
    # 同日同数量：策略只在第 1、3 天各挑 1 只 → 随机也只在这两天各挑 1 只
    strat = pool.filter(pl.col("date").is_in([days[0], days[2]])).group_by("date").head(1)
    mb = swing.matched_baseline(pool, strat, days, 3, seeds=5)
    assert mb["n"] == 2 and mb["seeds"] == 5 and mb == swing.matched_baseline(pool, strat, days, 3, seeds=5)
    assert swing.matched_baseline(pool, strat.head(0), days, 3, seeds=5)["n"] is None


# ---------------------------------------------------------------- 退 = ST、设置

def test_tui_names_are_st(tmp_path, monkeypatch) -> None:
    assert uni_mod.name_is_st("国华退") and uni_mod.name_is_st("*ST 金 泰") and not uni_mod.name_is_st("平安银行")
    from quant_web import config

    lab = tmp_path.joinpath("stock_lab")
    lab.mkdir()
    monkeypatch.setattr(config, "STOCK_LAB", lab)
    pl.DataFrame({"code": ["000004", "000001"], "name": ["国华退", "平安银行"], "is_st": [False, False],
                  "status": pl.Series([0, 1], dtype=pl.Int8)}).write_parquet(lab.joinpath("universe.parquet"))
    uni = uni_mod.load_universe()
    assert dict(zip(uni["code"], uni["is_st"], strict=True)) == {"000004": True, "000001": False}
    # 涨跌停：名称带"退"的主板股 2026-07-06 之前按 5%
    days = _bdays(date(2024, 3, 4), 3)
    panel = pl.DataFrame({"date": days, "code": ["000004"] * 3, "open": [2.0, 2.1, 2.2], "high": [2.1, 2.1, 2.2],
                          "low": [2.0, 2.0, 2.1], "close": [2.0, 2.1, 2.2], "preclose": [2.0, 2.0, 2.1],
                          "is_st": [False] * 3})
    lim = add_limit_columns(panel, uni)
    assert lim["is_st"].to_list() == [True] * 3 and lim["limit_up"][1] == limit_price(2.0, 0.05)
    assert lim["is_limit_up"][1] and limit_ratio("000004", True, days[1]) == 0.05


def test_settings_defaults_per_kind_and_presets() -> None:
    s = settings_mod.Settings()
    assert s.predict.news_weight == 0.0 and s.trade.stamp_by_date is True
    d = settings_mod.defaults_for("swing")
    assert d["predict"]["kind"] == "swing" and d["predict"]["boards"] == ["main"]
    assert d["predict"]["threshold"] == 0.01 and d["predict"]["top_n"] == 5
    assert d["trade"]["position_pct"] == 0.04 and d["trade"]["max_positions"] == 25
    assert d["trade"]["exit_rule"] == "until_break_close" and d["predict"]["max_float_cap"] == 100_000
    assert settings_mod.defaults_for("streak")["trade"]["exit_rule"] == "next_close"
    assert settings_mod.defaults_for("first")["predict"]["max_float_cap"] == 500
    with pytest.raises(settings_mod.SettingsError):
        settings_mod.defaults_for("abc")
    custom = settings_mod.merge_update(s, {"predict": {"threshold": 0.3, "min_price": 5.0},
                                           "trade": {"stop_loss_pct": 0.05}})
    sw = settings_mod.for_kind(custom, "swing")
    assert sw.predict.kind == "swing" and sw.predict.threshold == 0.01 and sw.predict.min_price == 5.0
    assert sw.trade.stop_loss_pct == 0.05 and sw.trade.exit_rule == "until_break_close"
    assert settings_mod.for_kind(sw, "swing") is sw
    for name, preset in settings_mod.PRESETS.items():
        assert "swing" in preset["threshold_by_kind"], name
        p = settings_mod.apply_preset(settings_mod.for_kind(s, "swing"), name)
        assert p.predict.threshold == preset["threshold_by_kind"]["swing"] and p.predict.boards == ["main"]
        assert p.trade.exit_rule == "until_break_close"
    assert settings_mod.merge_update(s, {"trade": {"exit_rule": "until_break_close"}}).trade.exit_rule \
        == "until_break_close"


def test_news_weight_is_clipped() -> None:
    pred = pl.DataFrame({"date": [date(2024, 5, 6)] * 2, "code": ["600001", "600002"], "prob": [0.2, 0.2],
                         "bias": pl.Series([None, None], dtype=pl.Float64)})
    news = pl.DataFrame({"code": ["600001", "600002"], "news_count": [30, 1], "policy_count": [0, 0],
                         "news_z": [9.0, 1.0]})
    out = scoring.apply_weights(pred, None, news, news_weight=1.0)
    logit0 = float(np.log(0.2 / 0.8))
    assert out["score_logit"].to_list() == pytest.approx([logit0 + 2.0, logit0 + 1.0])
    reg = pl.DataFrame({"date": [date(2024, 5, 6)], "code": ["600001"], "pred": [0.012],
                        "bias": pl.Series([None], dtype=pl.Float64)})
    r = scoring.apply_weights(reg, None, news.head(1), news_weight=1.0)
    assert r["score"][0] == pytest.approx(0.012 + 2.0 * scoring.NEWS_RET_UNIT)
    assert scoring.reasons_text([{"feature": "chg", "value": 0.06, "contrib": 0.01}], regression=True)[0] \
        .endswith("↑提高预期收益")


# ---------------------------------------------------------------- 特征与端到端

CODES: list[str] = [f"600{i:03d}" for i in range(14)] + [f"000{i:03d}" for i in range(10)] + \
    [f"300{i:03d}" for i in range(6)] + ["830001"]
ST_CODE: str = "600001"
TUI_CODE: str = "000009"


def _swing_panel(n_days: int = 1150, seed: int = 11) -> pl.DataFrame:
    """2019-01 起的合成面板：波动大（经常出现 ≥5% 的强势股），且"今天换手高 → 之后几天偏强"可学"""
    rng = np.random.default_rng(seed)
    days: list[date] = _bdays(date(2019, 1, 2), n_days)
    rows: list[dict] = []
    for code in CODES:
        pre: float = round(float(rng.uniform(5, 30)), 2)
        st: bool = code in (ST_CODE, TUI_CODE)
        drift: float = 0.0
        for d in days:
            r: float = limit_ratio(code, st, d)
            up, dn = limit_price(pre, r), limit_price(pre, -r)
            turn: float = float(rng.uniform(1, 20))
            o: float = min(up, max(dn, round(pre * (1 + drift / 2 + rng.normal(0, 0.01)), 2)))
            close: float = min(up, max(dn, round(pre * (1 + drift + rng.normal(0.004, 0.035)), 2)))
            drift = 0.004 * (turn - 10.5) / 5.5                     # 明天的漂移由今天的换手决定
            vol: float = turn / 100 * 1e8
            rows.append({"date": d, "code": code, "open": o, "high": max(o, close), "low": min(o, close),
                         "close": close, "preclose": pre, "volume": vol, "amount": vol * close, "turn": round(turn, 2),
                         "tradestatus": 1, "is_st": st, "source": "tx"})
            pre = close
    return pl.DataFrame(rows).with_columns(pl.col("tradestatus").cast(pl.Int8))


def _swing_universe() -> pl.DataFrame:
    return pl.DataFrame({
        "code": CODES,
        "name": [("*ST测试" if c == ST_CODE else "测试退" if c == TUI_CODE else f"测试{c}") for c in CODES],
        "exchange": ["SSE" if c.startswith("6") else "BSE" if c.startswith("8") else "SZSE" for c in CODES],
        "board": [uni_mod.board_of(c) for c in CODES],
        "list_date": [date(2015, 1, 5)] * len(CODES),
        "delist_date": pl.Series([None] * len(CODES), dtype=pl.Date),
        "is_st": [c == ST_CODE for c in CODES],
        "industry": [["电子", "计算机", "医药", None][i % 4] for i in range(len(CODES))],
        "status": pl.Series([1] * len(CODES), dtype=pl.Int8),
    })


@pytest.fixture(scope="module")
def swing_panel() -> pl.DataFrame:
    return _swing_panel()


def test_swing_pool_and_chunked_features_match(swing_panel: pl.DataFrame) -> None:
    panel = swing_panel.filter(pl.col("date") < date(2019, 9, 1))
    uni = _swing_universe()
    lim = add_limit_columns(panel, uni)
    sent = sentiment.daily_sentiment(lim)
    empty = pl.DataFrame()
    full = features.build_features(lim, uni, sent, "swing", fund=empty, lhb=empty, zt_pool=empty)
    assert full.height > 100
    assert full["pct"].min() >= 0.05 and not full["is_st"].any() and "bj" not in full["board"].to_list()
    assert TUI_CODE not in full["code"].to_list() and ST_CODE not in full["code"].to_list()
    chunked = features.build_features_chunked(lim, uni, sent, "swing", n_chunks=3, fund=empty, lhb=empty,
                                              zt_pool=empty)
    assert chunked.shape == full.shape
    from polars.testing import assert_frame_equal

    assert_frame_equal(chunked.select(full.columns), full, check_exact=False, rel_tol=1e-5)
    assert features.feature_columns("swing") == features.feature_columns("first")


def test_swing_service_end_to_end(tmp_path, monkeypatch, swing_panel: pl.DataFrame) -> None:
    from quant_web import config
    from quant_web.predict import service

    lab = tmp_path.joinpath("stock_lab")
    daily_dir = lab.joinpath("daily")
    daily_dir.mkdir(parents=True)
    for (year,), part in swing_panel.with_columns(pl.col("date").dt.year().alias("_y")).partition_by(
            "_y", as_dict=True).items():
        part.drop("_y").write_parquet(daily_dir.joinpath(f"{year}.parquet"))
    _swing_universe().write_parquet(lab.joinpath("universe.parquet"))
    for name, value in {"WORKSPACE": tmp_path, "STOCK_LAB": lab, "PANEL_DIR": daily_dir,
                        "UNIVERSE_FILE": lab.joinpath("universe.parquet"), "MODEL_DIR": tmp_path.joinpath("models"),
                        "SETTINGS_FILE": tmp_path.joinpath("settings.json"), "POOLS_DIR": lab.joinpath("pools"),
                        "CACHE_DIR": tmp_path.joinpath("cache")}.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(service, "_news_heat", lambda codes: (None, None))
    service.invalidate()

    real_train_fn = service.train
    steps: list[tuple[float, str]] = []
    r = service.train("swing", lambda f, m: steps.append((f, m)))
    fracs = [f for f, _ in steps]
    assert fracs == sorted(fracs) and fracs[-1] == 1.0 and "训练完成" in steps[-1][1]
    assert r["folds"] >= 2 and r["trade"] is not None
    meta = service.models_meta("swing")
    for key in ("kind", "trained_at", "data_end", "feature_cols", "folds", "oos", "trade_oos", "importance",
                "calibration", "topn_hit", "notes"):
        assert key in meta
    assert meta["objective"] == "regression" and meta["feature_cols"] == features.feature_columns("swing")
    t = meta["trade_oos"]
    assert {"n", "mean", "median", "win_rate", "daily_t", "by_year", "selection", "holdout", "random"} <= set(t)
    assert t["config"]["boards"] == ["main"] and t["random"]["seeds"] == 20
    # 学到了"换手高 → 之后偏强"（玩具数据噪声大，只要求整体为正、至少一折明显为正）
    assert meta["oos"]["ic"] is not None and meta["oos"]["ic"] > 0
    assert max(f.get("ic") or 0 for f in meta["folds"]) > 0.05
    # 每折 3 个随机种子取平均，每个至少 50 棵树
    trained = [f for f in meta["folds"] if "best_iter" in f]
    assert trained and all(f["seeds"] == [7, 8, 9] and len(f["best_iters"]) == 3 and min(f["best_iters"]) >= 50
                           for f in trained)
    assert meta["n_rounds"] >= 50 and meta["seeds"] == [7, 8, 9] and meta["min_rounds"] == 50
    assert {"nw_t", "held_skipped"} <= set(t) and {"nw_t", "held_skipped"} <= set(t["holdout"])
    oos = model.load_oos("swing")
    assert {"pred", "net", "fill", "status", "label_end", "board"} <= set(oos.columns)
    assert oos["date"].min() >= date(2022, 1, 1)
    summary = service.dataset_status()["models"]["swing"]
    assert summary["trade"]["avg_return"] == t["mean"] and summary["stale"] is False

    # 今天的预测：保存的设置是连板（默认）→ 波段用它自己的默认值（主板、门槛 1%、每天 5 只）
    res = service.predict_latest("swing")
    assert res["signal_date"] == swing_panel["date"].max().isoformat()
    assert set(res["gate"]) >= {"trade", "reason"} and set(res["plan"]) == {"buy", "sell", "position"}
    assert res["model"]["trade"]["avg_return"] == t["mean"] and res["model"]["auc"] is None
    for row in res["rows"]:
        assert row["prob"] is None and row["board"] == "main" and row["exp_ret"] == pytest.approx(row["score"] * 100,
                                                                                                    abs=0.01)
        assert row["pick"] == (row["score"] >= 0.01 and res["rows"].index(row) < 5)
        assert row["reasons"] and ("预期收益" in row["reasons"][0])
    assert res["gate"]["trade"] == any(row["pick"] for row in res["rows"])
    # 门槛放到 100%：今天不操作
    strict = settings_mod.for_kind(settings_mod.Settings(), "swing").model_copy(deep=True)
    strict.predict.threshold = 1.0
    res2 = service.predict_latest("swing", strict)
    assert res2["gate"]["trade"] is False and not any(row["pick"] for row in res2["rows"])
    assert "不操作" in res2["gate"]["reason"] and "不买入" in res2["plan"]["buy"]

    bt = service.backtest("swing")
    assert bt["settings_used"]["trade"]["exit_rule"] == "until_break_close"
    assert bt["settings_used"]["predict"]["boards"] == ["main"]
    assert bt["metrics"]["signals"] > 0 and bt["baseline"] is not None and "daily_t" in bt["metrics"]
    assert {"selection", "holdout"} == set(bt["by_period"])
    assert "baseline_matched" in bt and bt["tuned"] == {"modified": False, "fields": []}
    # 推荐设置之外改了选股/买卖规则 → tuned；只改本金/费用不算
    s2 = settings_mod.for_kind(settings_mod.Settings(), "swing").model_copy(deep=True)
    s2.trade.capital = 300_000
    s2.trade.fee_rate = 0.0001
    assert service.tuned_fields("swing", s2.predict.model_dump(), s2.trade.model_dump())["modified"] is False
    s2.predict.threshold = 0.02
    s2.trade.stop_loss_pct = 0.05
    tf = service.tuned_fields("swing", s2.predict.model_dump(), s2.trade.model_dump())
    assert tf["modified"] is True and set(tf["fields"]) == {"门槛", "止损"}
    assert service.backtest("swing", s2)["tuned"]["modified"] is True
    # 按已保存的样本外预测重算逐笔评价：只重写 meta，旧口径的数字不变，新增同日同数量随机基准
    meta_before = service.models_meta("swing")
    summ = service.refresh_evaluation("swing")
    meta_after = service.models_meta("swing")
    assert meta_after["trade_oos"]["mean"] == meta_before["trade_oos"]["mean"]
    assert meta_after["trade_oos"]["holdout"]["daily_t"] == meta_before["trade_oos"]["holdout"]["daily_t"]
    assert meta_after["trade_oos"]["random_matched"]["seeds"] == 20 and "eval_refreshed_at" in meta_after
    assert summ["avg_return"] == meta_after["trade_oos"]["mean"] and "baseline_matched_avg_return" in summ
    with pytest.raises(ValueError):
        service.refresh_evaluation("streak")

    # 个股页：波段候选给出预期收益
    if res["rows"]:
        pf = service.prediction_for(res["rows"][0]["code"])
        assert pf is not None and pf["kind"] == "swing" and pf["exp_ret"] is not None and pf["prob"] is None

    # 每日流水线：三个模型都预测，并交给模拟盘记录（没有模拟盘模块时跳过）；设置按模型切换
    import sys
    import types

    import quant_web

    recorded: list[tuple[str, str, bool]] = []
    fake = types.ModuleType("quant_web.paper")
    fake.record_signals = lambda kind, result, s: recorded.append((kind, s.predict.kind, "gate" in result)) or 1
    monkeypatch.setitem(sys.modules, "quant_web.paper", fake)
    monkeypatch.setattr(quant_web, "paper", fake, raising=False)
    monkeypatch.setattr(service, "update_data", lambda progress=None: {"warnings": []})
    monkeypatch.setattr(service, "train", lambda kind, progress=None: {"kind": kind})
    daily = service.daily_pipeline()
    assert [k for k, _, _ in recorded] == ["streak", "first", "swing"]
    assert all(k == sk for k, sk, _ in recorded) and recorded[-1][2] is True
    assert daily["paper"] == {"streak": 1, "first": 1, "swing": 1} and "gate" in daily["predictions"]["swing"]
    monkeypatch.setattr(service, "train", real_train_fn)

    # train("all") 的顺序
    calls: list[str] = []
    monkeypatch.setattr(service, "_train_swing", lambda progress=None, first_test_year=2022: calls.append("swing")
                        or {"kind": "swing"})
    real_train = service.train

    def fake(kind, progress=None, neg_sample=None, first_test_year=2022):
        if kind in ("streak", "first"):
            calls.append(kind)
            return {"kind": kind}
        return real_train(kind, progress, neg_sample, first_test_year)

    monkeypatch.setattr(service, "train", fake)
    out = real_train("all")
    assert calls == ["streak", "first", "swing"] and set(out) == {"streak", "first", "swing"}
    service.invalidate()


def test_execution_rule_validation() -> None:
    panel = _panel(_stock("600050", [(10.0, 10.0, 10.0, 10.0)] * 3))
    with pytest.raises(ValueError):
        execution.simulate(pl.DataFrame({"signal_date": [D[0]], "code": ["600050"], "exit_rule": ["abc"]}), panel,
                           delisted=set())
    # 信号表里卖出规则为空 → 用交易设置里的
    sig = pl.DataFrame({"signal_date": [D[0]], "code": ["600050"], "kind": ["swing"],
                        "exit_rule": pl.Series([None], dtype=pl.Utf8)})
    out = simulate_trades(sig, panel, SimpleNamespace(exit_rule="next_open", max_gap_pct=7.0), delisted=set())
    assert out["status"][0] == "closed" and out["exit_reason"][0] == "第二天开盘卖出"


# ---------------------------------------------------------------- 第五轮：锁定卖出、买不进原因、回测新字段、NW t、去重、分种子训练

def _one_oos(codes: list[str], day: date = D[0]) -> pl.DataFrame:
    n: int = len(codes)
    return pl.DataFrame({
        "date": [day] * n, "code": codes, "prob": [0.6 - 0.01 * i for i in range(n)],
        "bias": pl.Series([None] * n, dtype=pl.Float64), "y": pl.Series([1] * n, dtype=pl.Int8),
        "close": [10.0] * n, "float_cap": [50.0] * n, "board": ["main"] * n, "is_st": [False] * n,
        "streak": pl.Series([0] * n, dtype=pl.Int16), "one_word": [False] * n,
    })


PS_ALL: dict = {"kind": "streak", "exclude_st": False, "boards": ["main"], "top_n": 5, "threshold": 0.0, "min_price": 0,
                "max_price": 1e9, "min_float_cap": 0, "max_float_cap": 1e9, "streak_min": 0, "streak_max": 99}


def test_until_break_close_sale_stays_committed_after_limit_down() -> None:
    """要卖的那根收盘跌停 → 顺延到下一根收盘；下一根即使收涨停也照卖（不重新判断），标签/逐笔/组合回测一致"""
    # D2 收盘没涨停但收在跌停（卖不出）→ D3 收盘涨停，仍按 D3 收盘价卖
    bars = [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _ld_path(10.5, 1)
    bars += _lu_path(bars[-1][3], 1) + [(10.5, 10.8, 10.3, 10.6), (10.6, 10.7, 10.5, 10.6)]
    # 连续涨停到第 5 根，第 5 根收跌停（没涨停 → 要卖，卖不出）→ 第 6 根收涨停也照卖
    bars2 = [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5)] + _lu_path(10.5, 3)
    bars2 += _ld_path(bars2[-1][3], 1)
    bars2 += _lu_path(bars2[-1][3], 1) + [(12.0, 12.2, 11.8, 12.0)]
    panel = _panel(_stock("600050", bars), _stock("600051", bars2))
    r = _labels(panel, "600050")
    assert r["exit_date"] == D[3] and r["exit"] == pytest.approx(bars[3][3] * 0.999) and r["hold_days"] == 2
    assert "收盘没有涨停" in r["exit_reason"] and "顺延" in r["exit_reason"]
    r2 = _labels(panel, "600051")
    assert r2["exit_date"] == D[6] and r2["hold_days"] == 5
    assert "收盘没有涨停" in r2["exit_reason"] and "顺延" in r2["exit_reason"]
    sig = pl.DataFrame({"signal_date": [D[0], D[0]], "code": ["600050", "600051"], "kind": ["swing"] * 2,
                        "exit_rule": ["until_break_close"] * 2})
    sim = simulate_trades(sig, panel, {"max_gap_pct": 30.0}, delisted=set())
    assert sim["exit_date"].to_list() == [D[3], D[6]]
    assert sim["ret"].to_list() == pytest.approx([r["net"], r2["net"]])
    ts = {"capital": 1e8, "position_pct": 0.1, "max_positions": 5, "exit_rule": "until_break_close",
          "fee_rate": 0.00025, "slippage": 0.001, "max_gap_pct": 30.0}
    bt = run_backtest(_one_oos(["600050", "600051"]), panel, PS_ALL, ts, delisted=set(), baseline_seeds=0)
    got = {t["code"]: t for t in bt["trades"]}
    assert got["600050"]["exit_date"] == D[3].isoformat() and got["600051"]["exit_date"] == D[6].isoformat()
    assert got["600050"]["ret"] == pytest.approx(r["net"], abs=2e-5) and "顺延" in got["600050"]["reason"]
    assert "顺延" in got["600051"]["reason"] and got["600051"]["ret"] == pytest.approx(r2["net"], abs=2e-5)


def test_unfilled_reason_one_word_vs_open_at_limit() -> None:
    up: float = limit_price(10.0, 0.1)
    one_word = _stock("600060", [(10.0, 10.0, 10.0, 10.0), (up, up, up, up), (11.2, 11.5, 11.0, 11.1)])
    opened = _stock("600061", [(10.0, 10.0, 10.0, 10.0), (up, up, 10.6, 10.8), (10.8, 11.0, 10.6, 10.7)])
    panel = _panel(one_word, opened)
    sig = pl.DataFrame({"signal_date": [D[0], D[0]], "code": ["600060", "600061"], "kind": ["swing"] * 2,
                        "exit_rule": ["until_break_close"] * 2})
    out = simulate_trades(sig, panel, delisted=set())
    assert out["status"].to_list() == ["unfilled", "unfilled"]
    assert out["exit_reason"].to_list() == [execution.ONE_WORD_TEXT, execution.LIMIT_UP_TEXT]
    assert execution.ONE_WORD_TEXT == "一字板，买不进" and execution.LIMIT_UP_TEXT == "开盘就涨停，买不进"


def test_backtest_fill_rate_nw_t_uncapped_and_skipped_note() -> None:
    up: float = limit_price(10.0, 0.1)
    stocks = [
        _stock("600070", [(10.0, 10.0, 10.0, 10.0), (10.2, 10.6, 10.1, 10.5), (10.6, 10.9, 10.4, 10.7),
                          (10.7, 10.8, 10.5, 10.6)]),
        _stock("600071", [(10.0, 10.0, 10.0, 10.0), (up, up, up, up), (11.2, 11.5, 11.0, 11.1)]),
        _stock("600072", [(10.0, 10.0, 10.0, 10.0), (10.9, 11.0, 10.7, 10.8), (10.8, 10.9, 10.6, 10.7)]),
        _stock("600073", [(10.0, 10.0, 10.0, 10.0), (10.1, 10.2, 9.9, 10.0), (10.0, 10.3, 9.9, 10.2)]),
    ]
    panel = _panel(*stocks)
    oos = _one_oos(["600070", "600071", "600072", "600073"])
    ts = {"capital": 1e8, "position_pct": 0.1, "max_positions": 5, "exit_rule": "next_close", "max_gap_pct": 7.0}
    bt = run_backtest(oos, panel, PS_ALL, ts, delisted=set(), baseline_seeds=0)
    m = bt["metrics"]
    # 买不进 = 一字涨停 1 只 + 开盘涨 9% 超过 7% 上限 1 只
    assert (m["signals"], m["unfilled"], m["skipped_gap"], m["trades"]) == (4, 1, 1, 2)
    assert m["fill_rate"] == pytest.approx(0.5) and "nw_t" in m and bt["notes"] == []
    unc = m["per_trade_uncapped"]
    sim = simulate_trades(oos.select(pl.col("date").alias("signal_date"), "code").with_columns(
        pl.lit("streak").alias("kind"), pl.lit("next_close").alias("exit_rule")), panel, ts, delisted=set())
    closed = sim.filter(pl.col("status") == "closed")
    assert unc["trades"] == closed.height == 2
    assert unc["avg_return"] == pytest.approx(float(closed["ret"].mean()), abs=1e-5)
    assert unc["win_rate"] == pytest.approx(float((closed["ret"] > 0).mean()))
    # 本金很小：每只预算 400 元，一手 1000 元买不起 → skipped_lot，给出中文提示；逐笔（不受资金限制）照样有成交
    small = run_backtest(oos, panel, PS_ALL, {**ts, "capital": 4_000}, delisted=set(), baseline_seeds=0)
    sm = small["metrics"]
    assert sm["trades"] == 0 and sm["skipped_lot"] == 2 and sm["per_trade_uncapped"]["trades"] == 2
    assert len(small["notes"]) == 1 and "资金不足/一手太贵跳过了 50% 的信号" in small["notes"][0]
    assert "整体结果主要代表前一段时间" in small["notes"][0]
    empty = run_backtest(oos.head(0), panel, PS_ALL, ts, delisted=set(), baseline_seeds=0)
    assert empty["notes"] == [] and empty["metrics"]["per_trade_uncapped"]["trades"] == 0


def test_newey_west_t() -> None:
    from quant_web.predict import stats

    x = np.array([0.01, -0.02, 0.03, 0.0, 0.015, -0.005, 0.02, 0.01, -0.01, 0.025])
    n, lag = len(x), 5
    d = x - x.mean()
    lrv = d @ d / n + sum(2 * (1 - k / (lag + 1)) * (d[k:] @ d[:-k]) / n for k in range(1, lag + 1))
    assert stats.nw_t(x) == pytest.approx(x.mean() / np.sqrt(lrv / n))
    assert stats.nw_t(x, lag=0) == pytest.approx(x.mean() / np.sqrt(d @ d / n / n))
    assert stats.nw_t(x[:5]) is None and stats.nw_t([0.01] * 20) is None
    assert stats.daily_t(x) == pytest.approx(x.mean() / x.std(ddof=1) * np.sqrt(n))
    # 持有 5 天的重叠收益（强正自相关）：NW t 明显小于普通 t
    rng = np.random.default_rng(0)
    e = rng.normal(0.002, 0.01, 2000)
    overlap = np.convolve(e, np.ones(5) / 5, mode="valid")
    assert stats.nw_t(overlap) < 0.75 * stats.daily_t(overlap)


def test_drop_held_and_trade_oos_fields() -> None:
    days = _bdays(date(2025, 6, 2), 12)
    picks = pl.DataFrame({
        "date": [days[0], days[1], days[2], days[3], days[4], days[5], days[5]],
        "code": ["A", "A", "A", "B", "B", "C", "A"],
        "fill": [True, True, True, False, True, True, True],
        "label_end": [days[2], days[3], days[4], None, None, days[6], days[7]],
        "net": [0.01, 0.02, 0.03, None, None, 0.01, 0.02],
    })
    kept, n = swing.drop_held(picks)
    # A@d1 还持有（d0 买、d2 卖）→ 跳过；A@d2：d0 那笔 d2 收盘已卖出 → 可以；B@d3 没买进 → 不算持有；
    # B@d4 买进后一直没卖出；A@d5：d2 那笔 d4 已卖出 → 可以
    assert n == 1 and kept["date"].to_list() == [days[0], days[2], days[3], days[4], days[5], days[5]]
    none, n0 = swing.drop_held(picks.head(0))
    assert none.height == 0 and n0 == 0
    oos = pl.DataFrame({
        "date": [d for d in days for _ in range(3)], "code": ["A", "B", "C"] * len(days),
        "pred": [0.03, 0.02, 0.0] * len(days), "fill": [True] * (3 * len(days)),
        "net": [0.01, -0.005, 0.002] * len(days), "board": ["main"] * (3 * len(days)),
    }).with_columns(pl.col("date").alias("label_end"))
    oos = oos.with_columns(pl.when(pl.col("code") == "A").then(pl.col("date") + pl.duration(days=3))
                           .otherwise(pl.col("label_end")).alias("label_end"))
    t = swing.trade_oos(oos, top_n=2, threshold=0.01, seeds=3)
    assert t["held_skipped"] > 0 and t["n"] + t["held_skipped"] == len(days) * 2
    assert "nw_t" in t and "nw_t" in t["random"] and "held_skipped" in t["random_matched"]


def test_fit_regression_seeds_min_rounds_and_average(monkeypatch) -> None:
    ds = _toy_reg()
    cols = features.feature_columns("swing")
    calls: list[tuple] = []
    real = model.fit_regression

    def spy(df, feature_cols, label="net", params=None, num_boost_round=None):
        calls.append(((params or {}).get("seed"), num_boost_round))
        booster, best = real(df, feature_cols, label, params, num_boost_round)
        return booster, (3 if num_boost_round is None else best)            # 假装早停只选了 3 棵树

    monkeypatch.setattr(model, "fit_regression", spy)
    tr = ds.filter(pl.col("date") <= date(2021, 12, 31))
    boosters, iters = model.fit_regression_seeds(tr, cols, seeds=(7, 8, 9), min_rounds=50)
    assert iters == [50, 50, 50] and [b.current_iteration() for b in boosters] == [50, 50, 50]
    assert calls == [(7, None), (7, 50), (8, None), (8, 50), (9, None), (9, 50)]
    te = ds.filter(pl.col("date") > date(2021, 12, 31)).head(200)
    avg = model.predict_reg_frame_avg(boosters, te, cols)
    single = [model.predict_reg_frame(b, te, cols)["pred"].to_numpy() for b in boosters]
    assert np.allclose(avg["pred"].to_numpy(), np.mean(single, axis=0))
    assert np.allclose(avg["pred"].to_numpy(), avg["bias"].to_numpy() + sum(avg[c].to_numpy()
                                                                            for c in model.CONTRIB_COLUMNS))
    assert not np.allclose(single[0], single[1])                 # 不同种子确实不同


def test_swing_defaults_no_gap_cap_and_computed_plan_text() -> None:
    d = settings_mod.defaults_for("swing")
    assert d["trade"]["max_gap_pct"] == 30.0 and "max_gap_pct" in settings_mod.KIND_FIELDS["trade"]
    assert settings_mod.defaults_for("streak")["trade"]["max_gap_pct"] == 7.0
    sw = settings_mod.for_kind(settings_mod.Settings(), "swing")
    assert sw.trade.max_gap_pct == 30.0 and settings_mod.for_kind(sw, "streak").trade.max_gap_pct == 7.0
    for name in settings_mod.PRESETS:
        assert settings_mod.apply_preset(sw, name).trade.max_gap_pct == 30.0, name
    meta = {"trade_oos": {"n": 500, "mean": 0.0123, "holdout": {"n": 200, "mean": 0.0045, "daily_t": 2.4, "nw_t": 1.6}}}
    gate, plan = swing.gate_and_plan([{"code": "600001", "name": "甲"}], d["predict"], d["trade"], "2026-09-24",
                                     meta=meta)
    rec: str = swing.record_text(meta)
    assert gate["trade"] is True and "+1.23%" in rec and "+0.45%" in rec
    assert "t 值 1.6" in rec and "还不够可信" in rec
    # 计划里的“仓位”只讲怎么分配资金，不夹带容易误读的历史成绩
    assert "4%" in plan["position"] and "没有证据证明它能赚钱" in plan["position"] and "+1.23%" not in plan["position"]
    assert "不重复买" in plan["buy"] and "超过" not in plan["buy"]            # 30% 上限等于不设，不提
    assert "下一个交易日收盘卖出（不管那天涨不涨停" in plan["sell"]
    _, plan7 = swing.gate_and_plan([{"code": "600001"}], d["predict"], {**d["trade"], "max_gap_pct": 7.0}, None)
    assert "开盘涨幅超过 7% 的也放弃" in plan7["buy"] and "历史成绩见预测页上方" in swing.record_text(None)


def test_swing_notes_computed_from_meta() -> None:
    from quant_web.predict import service

    meta = {"trade_oos": {"n": 594, "mean": 0.0135, "random": {"mean": -0.0031}, "random_matched": {"mean": 0.0086},
                          "holdout": {"n": 254, "mean": 0.0093, "daily_t": 1.455, "nw_t": 1.099},
                          "config": {"top_n": 5, "threshold": 0.01, "holdout_start": "2025-07-01"}}}
    text: str = "".join(service.notes_for("swing", meta))
    assert "594 笔平均每笔 +1.35%" in text and "254 笔平均 +0.93%" in text and "Newey-West 1.099" in text
    assert "只比挑股票多 0.49 个百分点" in text and "其余 1.17 个百分点" in text
    assert "1.9~2.7" not in text and "3 个随机种子" in text and "至少 50 棵树" in text
    assert "约 4,000 元" in text and "约多花 0.2%" in text and "约 40 元" in text and "默认 30%" in text
    assert "训练后显示" in "".join(service.notes_for("swing", {}))
    assert service.notes_for("streak", meta) == [*service.KIND_NOTES["streak"], *service.NOTES]
    # 留出期的说法不过头：没有“从没用来挑方案”，如实说方案是比较多种做法后选出的
    assert "从没" not in text and "没有参与训练；但方案是在研究中比较多种做法后选出的，结果可能偏乐观" in text
    # 留出期同日同数量随机：有就写出来，并按数据说谁好
    worse = {"trade_oos": {**meta["trade_oos"], "holdout": {**meta["trade_oos"]["holdout"], "mean": 0.0024,
                                                            "random_matched": {"mean": 0.0039}}}}
    t_worse: str = "".join(service.notes_for("swing", worse))
    assert "同日同数量随便挑 +0.39%（模型并不比随便挑好）" in t_worse
    better = {"trade_oos": {**meta["trade_oos"], "holdout": {**meta["trade_oos"]["holdout"],
                                                             "random_matched": {"mean": 0.0048}}}}
    assert "（模型只多 0.45 个百分点）" in "".join(service.notes_for("swing", better))


def test_swing_plan_text_compares_holdout_with_same_day_random() -> None:
    meta = {"trade_oos": {"n": 594, "mean": 0.0135,
                          "holdout": {"n": 254, "mean": 0.0024, "daily_t": 0.19, "nw_t": 0.2, "random_matched": {"mean": 0.0039}}}}
    rec: str = swing.record_text(meta)
    assert "同一天随便挑同样多只 +0.39%，模型并不比随便挑好；t 值 0.2，还不够可信" in rec
    meta["trade_oos"]["holdout"]["random_matched"] = {"mean": 0.001}
    rec = swing.record_text(meta)
    assert "同一天随便挑同样多只 +0.10%；t 值 0.2" in rec and "并不比" not in rec


def test_pages_have_no_stale_fixed_figures_or_overclaims() -> None:
    """页面上不写死会过时的成绩数字，不说“从没用来挑方案/调参数”，“默认规则”按名单算"""
    from pathlib import Path

    static = Path(__file__).resolve().parents[1].joinpath("quant_web", "static")
    texts: dict[str, str] = {p.name: p.read_text(encoding="utf-8") for p in [static.joinpath("app.js"), *static.joinpath("pages").glob("*.js")]}
    for name, text in texts.items():
        assert "约 +1% 一笔" not in text and "波段约 1~2 分钟" not in text, name
        assert "从没用来挑方案" not in text and "从没用来调参数" not in text.replace("“从没用来调参数”这类过头的话", ""), name
        assert "这是默认规则" not in text and "唯一在“选择期”和“留出期”都赚钱" not in text, name
    app: str = texts["app.js"]
    assert '"比较可信（仍需模拟盘验证）"' in app and '"偏亏，但不够确定"' in app and '"略好，但不够确定"' in app
    assert "最近一段时间，它并不比同一天随便挑更好——" in app and "略好于随便挑，但还不能确定是真本事" in app
    assert "最近表现较好，但仍需模拟盘验证" in app
    assert "fillPct(fillInfo.fill)" in texts["predict.js"] and "样本太少" in texts["model.js"]
    assert "模型检验（不受资金限制，偏乐观）" in texts["model.js"] and "everSaved" in texts["settings.js"]


def test_pages_have_no_stale_retrain_t_range() -> None:
    """页面上不写研究里单种子重训的留出期 t 值范围（和当前 3 种子平均的模型对不上，会显得比实际可信）"""
    from pathlib import Path

    pages = Path(__file__).resolve().parents[1].joinpath("quant_web", "static", "pages")
    for name in ("predict.js", "model.js"):
        text: str = pages.joinpath(name).read_text(encoding="utf-8")
        assert "1.9~2.7" not in text and "六成" not in text, name
    assert "meta.seeds" in pages.joinpath("model.js").read_text(encoding="utf-8")
