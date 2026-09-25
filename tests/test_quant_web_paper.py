"""quant_web 模拟盘（paper.py）：记录去重/闸门/时间窗、随数据增长的 待买入→持有中→已卖出、收益曲线、空文件、每日任务与调度（全部离线）"""
import json
import sys
import types
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

import quant_web.predict
from quant_web import config, jobs, paper, scheduler
from quant_web import settings as settings_mod
from quant_web.market.history import PANEL_SCHEMA
from quant_web.market.universe import SCHEMA as UNI_SCHEMA
from quant_web.predict import backtest
from quant_web.predict.limits import limit_price


DAYS: list[date] = [date(2026, 9, d) for d in (14, 15, 16, 17, 18, 21, 22, 23, 24)]
T: int = 4                      # 信号日 2026-09-18（周五）
SD: date = DAYS[T]
TOL: float = 0.005
FEE, STAMP, SLIP = 0.00025, 0.0005, 0.001


# ================================================================ 测试用的逐笔成交（真实实现不可用时的替身）

def stub_simulate(signals: pl.DataFrame, panel_lim: pl.DataFrame, trade: object = None) -> pl.DataFrame:
    """简化版 simulate_trades：T+1 开盘买（开盘涨停/停牌买不到），按 exit_rule 卖，费用佣金+印花税+滑点"""
    cal: list[date] = sorted(panel_lim["date"].unique().to_list())
    by_code: dict[str, list[dict]] = {
        (k[0] if isinstance(k, tuple) else k): part.sort("date").to_dicts()
        for k, part in panel_lim.partition_by("code", as_dict=True).items()
    }
    out: list[dict] = []
    for s in signals.to_dicts():
        sd: date = s["signal_date"]
        res: dict = {"status": "pending", "entry_date": None, "entry": None, "exit_date": None, "exit": None,
                     "ret": None, "hold_days": None, "exit_reason": "等待买入"}
        nxt: list[date] = [d for d in cal if d > sd]
        rows: list[dict] = [r for r in by_code.get(s["code"], []) if r["date"] > sd]
        if nxt and (not rows or rows[0]["date"] != nxt[0]):
            res.update(status="unfilled", exit_reason="停牌")
        elif rows and rows[0]["open"] >= rows[0]["limit_up"] - TOL:
            res.update(status="unfilled", exit_reason="开盘涨停买不进")
        elif rows:
            entry: float = rows[0]["open"] * (1 + SLIP)
            cost: float = entry * (1 + FEE)
            exit_i: int | None = None
            px: float | None = None
            rule: str = s["exit_rule"]
            if rule in ("next_open", "next_close") and len(rows) > 1:
                exit_i, px = 1, rows[1]["open" if rule == "next_open" else "close"]
            elif rule == "until_break_close":
                for k in range(1, min(len(rows), 5)):
                    if rows[k]["close"] < rows[k]["limit_up"] - TOL or k == 4:
                        exit_i, px = k, rows[k]["close"]
                        break
            elif rule == "until_break":
                for k in range(min(len(rows) - 1, 10)):
                    if rows[k]["close"] < rows[k]["limit_up"] - TOL or k == 9:
                        exit_i, px = k + 1, rows[k + 1]["open"]
                        break
            res.update(entry_date=rows[0]["date"], entry=entry)
            if exit_i is None:
                res.update(status="holding", hold_days=len(rows) - 1, exit_reason="持有中",
                           ret=rows[-1]["close"] * (1 - SLIP) * (1 - FEE - STAMP) / cost - 1)
            else:
                fill: float = px * (1 - SLIP)
                res.update(status="closed", exit_date=rows[exit_i]["date"], exit=fill, hold_days=exit_i,
                           ret=fill * (1 - FEE - STAMP) / cost - 1, exit_reason="卖出")
        out.append({**s, **res})
    schema: dict = {**dict(signals.schema), "status": pl.Utf8, "entry_date": pl.Date, "entry": pl.Float64,
                    "exit_date": pl.Date, "exit": pl.Float64, "ret": pl.Float64, "hold_days": pl.Int32,
                    "exit_reason": pl.Utf8}
    return pl.DataFrame(out, schema=schema)


# ================================================================ 手工面板

def _bars() -> dict[str, list[tuple[float, float, float, float] | None]]:
    """每只股票 9 天的 (开, 高, 低, 收)；None = 停牌"""
    flat = (10.0, 10.0, 10.0, 10.0)
    lu11 = limit_price(10.6, 0.1)
    return {
        # swing：信号日 +6%；T+1 没涨停收盘继续拿；T+2 收盘没涨停 → 收盘卖
        "600001": [flat] * 4 + [(10.1, 10.7, 10.0, 10.6), (10.8, 11.6, 10.7, 11.5), (11.4, 11.5, 11.1, 11.2),
                                (11.2, 11.2, 11.2, 11.2), (11.2, 11.2, 11.2, 11.2)],
        # swing：T+1 一字涨停 → 买不进
        "600002": [flat] * 4 + [(10.1, 10.7, 10.0, 10.6), (lu11, lu11, lu11, lu11)] + [(lu11,) * 4] * 3,
        # streak：信号日涨停；T+1 开盘买；T+2 收盘卖
        "000003": [flat] * 4 + [(10.2, 11.0, 10.2, 11.0), (11.3, 11.8, 11.2, 11.6), (11.5, 11.6, 11.3, 11.4),
                                (11.4, 11.4, 11.4, 11.4), (11.4, 11.4, 11.4, 11.4)],
        # first：T+1 开盘买；T+2 开盘卖
        "600004": [flat] * 4 + [(10.0, 10.3, 10.0, 10.3), (10.4, 10.6, 10.3, 10.5), (10.6, 10.8, 10.5, 10.7),
                                (10.7, 10.7, 10.7, 10.7), (10.7, 10.7, 10.7, 10.7)],
        # first：T+1、T+2 停牌 → 买不进
        "000005": [flat] * 4 + [(10.0, 10.2, 10.0, 10.2), None, None, (10.2, 10.3, 10.1, 10.2), (10.2,) * 4],
    }


def make_panel(n_days: int) -> pl.DataFrame:
    rows: list[dict] = []
    for code, bars in _bars().items():
        pre: float = 10.0
        for d, bar in zip(DAYS[:n_days], bars, strict=False):
            if bar is None:
                continue
            o, h, low, c = bar
            rows.append({"date": d, "code": code, "open": o, "high": h, "low": low, "close": c, "preclose": pre,
                         "volume": 1e6, "amount": 1e6 * c, "turn": 3.0, "tradestatus": 1, "is_st": False, "source": "tx"})
            pre = c
    return pl.DataFrame(rows, schema=PANEL_SCHEMA)


def make_universe() -> pl.DataFrame:
    rows: list[dict] = []
    for code in _bars():
        rows.append({"code": code, "name": f"测试{code}", "exchange": "SSE" if code[0] == "6" else "SZSE",
                     "board": "main", "list_date": date(2010, 1, 4), "delist_date": None, "is_st": False,
                     "industry": "制造业", "status": 1})
    return pl.DataFrame(rows, schema=UNI_SCHEMA)


def write_panel(n_days: int) -> None:
    make_panel(n_days).write_parquet(config.PANEL_DIR.joinpath("2026.parquet"))


def result(kind: str, day: date | str, codes: list[str], picks: int | None = None, **extra: object) -> dict:
    """假的 predict_latest 结果：rows 按顺序，前 picks 个 pick=True"""
    n: int = len(codes) if picks is None else picks
    rows: list[dict] = [
        {"code": c, "name": f"测试{c}", "score": 0.5 - i * 0.01, "prob": None if kind == "swing" else 0.3,
         "exp_ret": 1.5 - i * 0.1 if kind == "swing" else None, "pick": i < n}
        for i, c in enumerate(codes)
    ]
    return {"kind": kind, "signal_date": day.isoformat() if isinstance(day, date) else day,
            "model": {"trained_at": "2026-09-01"}, "count": len(rows), "rows": rows, "warnings": [], **extra}


def trade_settings(exit_rule: str, stop: float = 0.0) -> dict:
    return {"trade": {"exit_rule": exit_rule, "stop_loss_pct": stop}}


def cn(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=config.CHINA_TZ)


# ================================================================ 夹具

@pytest.fixture()
def pws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 workspace（绝不碰真实目录）；交易日历 = 工作日（不联网）"""
    stock_lab: Path = tmp_path.joinpath("stock_lab")
    for name, value in {
        "WORKSPACE": tmp_path, "STOCK_LAB": stock_lab, "PANEL_DIR": stock_lab.joinpath("daily"),
        "UNIVERSE_FILE": stock_lab.joinpath("universe.parquet"), "POOLS_DIR": stock_lab.joinpath("pools"),
        "MODEL_DIR": tmp_path.joinpath("models"), "CACHE_DIR": tmp_path.joinpath("cache"),
        "SETTINGS_FILE": tmp_path.joinpath("settings.json"), "WATCHLIST_FILE": tmp_path.joinpath("watchlist.json"),
    }.items():
        monkeypatch.setattr(config, name, value)
    config.ensure_dirs()
    make_universe().write_parquet(config.UNIVERSE_FILE)
    monkeypatch.setattr(paper, "_is_trading_day", lambda day: day.weekday() < 5)
    assert paper.paper_dir() == tmp_path.joinpath("paper")
    return tmp_path


@pytest.fixture(params=["stub", "real"])
def sim(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """stub：用本文件的替身；real：用 predict.backtest.simulate_trades（还没实现时跳过）"""
    if request.param == "real":
        if not hasattr(backtest, "simulate_trades"):
            pytest.skip("predict.backtest.simulate_trades 还没有实现")
    else:
        monkeypatch.setattr(backtest, "simulate_trades", stub_simulate, raising=False)
    return request.param


@pytest.fixture()
def stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backtest, "simulate_trades", stub_simulate, raising=False)


def record_all(day: date = SD) -> None:
    assert paper.record_signals("swing", result("swing", day, ["600001", "600002", "600009"], picks=2),
                                trade_settings("until_break_close"), strict=False) == 2
    assert paper.record_signals("streak", result("streak", day, ["000003"]), trade_settings("next_close"),
                                strict=False) == 1
    assert paper.record_signals("first", result("first", day, ["600004", "000005"]), trade_settings("next_open"),
                                strict=False) == 2


# ================================================================ 记录

def test_record_dedup_gate_and_file(pws: Path) -> None:
    record_all()
    sig: pl.DataFrame = paper.load_signals()
    assert sig.height == 5 and list(sig.columns) == list(paper.SIGNAL_SCHEMA)
    swing: list[dict] = sig.filter(pl.col("kind") == "swing").sort("rank").to_dicts()
    assert [r["code"] for r in swing] == ["600001", "600002"] and [r["rank"] for r in swing] == [1, 2]
    assert swing[0]["exp_ret"] == 1.5 and swing[0]["prob"] is None and swing[0]["exit_rule"] == "until_break_close"
    assert sig.filter(pl.col("kind") == "first")["exit_rule"].to_list() == ["next_open", "next_open"]

    # 同一天同一策略只记第一次（改了设置、选出别的票也不再记）
    assert paper.record_signals("swing", result("swing", SD, ["600001", "600002"]), None, strict=False) == 0
    assert paper.record_signals("swing", result("swing", SD, ["600004"]), None, strict=False) == 0
    detail: dict = paper._record("swing", result("swing", SD, ["600001"]), None, strict=False)
    assert detail["status"] == "already" and "已经记录过" in detail["message"]
    assert paper.load_signals().height == 5

    # swing 闸门说今天不操作：一只都不记，但这一天锁定
    nxt: date = DAYS[T + 1]
    gated = result("swing", nxt, ["600001"], gate={"trade": False, "reason": "预期收益都没到 1%"})
    assert paper.record_signals("swing", gated, None, strict=False) == 0
    days: pl.DataFrame = paper.load_days()
    row: dict = days.filter((pl.col("kind") == "swing") & (pl.col("signal_date") == nxt)).to_dicts()[0]
    assert row["picks"] == 0 and row["trade"] is False and "1%" in row["note"]
    assert paper.record_signals("swing", result("swing", nxt, ["600001"]), None, strict=False) == 0

    # 没有模型 / 没有数据 / 策略对不上
    assert paper._record("first", {"kind": "first", "signal_date": None, "rows": [], "model": None,
                                   "warnings": ["还没有日线数据"]}, None)["status"] == "no_data"
    with pytest.raises(ValueError):
        paper.record_signals("streak", result("first", SD, ["600004"]), None, strict=False)
    with pytest.raises(ValueError):
        paper.record_signals("abc", result("abc", SD, ["600004"]), None, strict=False)
    # 默认卖出规则：swing 没给 exit_rule 时用 until_break_close
    paper.record_signals("swing", result("swing", DAYS[T + 2], ["000005"]), {"trade": {}}, strict=False)
    new: pl.DataFrame = paper.load_signals().filter((pl.col("code") == "000005") & (pl.col("kind") == "swing"))
    assert new["exit_rule"].to_list() == ["until_break_close"]


def test_record_time_window(pws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    thu: date = date(2026, 9, 24)
    # 交易日 15:05 前拒绝
    assert "15:05" in paper.record_block_reason(cn(2026, 9, 24, 14, 59))
    assert paper.record_signals("swing", result("swing", date(2026, 9, 23), ["600001"]), None,
                                now=cn(2026, 9, 24, 14, 59)) == 0
    with pytest.raises(RuntimeError, match="15:05"):
        paper.record_today(predict_fn=lambda k, s: result(k, thu, ["600001"]), settings_fn=lambda k: None,
                           now=cn(2026, 9, 24, 10))
    assert paper.expected_signal_day(cn(2026, 9, 24, 15, 5)) == thu
    assert paper.expected_signal_day(cn(2026, 9, 24, 9)) == date(2026, 9, 23)
    assert paper.expected_signal_day(cn(2026, 9, 27, 12)) == date(2026, 9, 25)     # 周日 → 周五
    monkeypatch.setattr(paper, "_is_trading_day", lambda d: d.weekday() < 5 and d != date(2026, 9, 25))
    assert paper.record_block_reason(cn(2026, 9, 25, 10)) is None           # 节假日随时可以记
    assert paper.expected_signal_day(cn(2026, 9, 25, 10)) == thu

    # 收盘后但数据还没更新：三个策略都是旧数据 → 拒绝并说明
    stale = lambda k, s: result(k, date(2026, 9, 23), ["600001"])           # noqa: E731
    with pytest.raises(RuntimeError, match="更新数据"):
        paper.record_today(predict_fn=stale, settings_fn=lambda k: None, now=cn(2026, 9, 24, 16))
    assert paper.load_signals().is_empty()

    # 正常：三个策略各自记录；一个策略出错不影响其他
    def predict(kind: str, s: object) -> dict:
        if kind == "first":
            raise RuntimeError("模型文件坏了")
        return result(kind, thu, ["600001", "000003"], picks=1)

    out: dict = paper.record_today(predict_fn=predict, settings_fn=lambda k: trade_settings("next_close"),
                                   now=cn(2026, 9, 24, 16))
    assert out["signal_date"] == "2026-09-24" and out["recorded"] == 2
    assert out["kinds"]["swing"]["status"] == "recorded" and out["kinds"]["first"]["status"] == "error"
    assert "模型文件坏了" in out["kinds"]["first"]["message"] and "记录了 1 只" in out["message"]
    again: dict = paper.record_today(predict_fn=predict, settings_fn=lambda k: None, now=cn(2026, 9, 24, 17))
    assert again["recorded"] == 0 and again["kinds"]["streak"]["status"] == "already"

    # 调度器用：只有模型已训练、这天还没记录的策略才算待记录
    config.MODEL_DIR.joinpath("first_meta.json").write_text("{}", encoding="utf-8")
    config.MODEL_DIR.joinpath("swing_meta.json").write_text("{}", encoding="utf-8")
    assert paper.pending_kinds(thu, now=cn(2026, 9, 24, 16)) == ["first"]
    assert paper.pending_kinds(thu, now=cn(2026, 9, 24, 14)) == []           # 收盘前
    assert paper.pending_kinds(date(2026, 9, 23), now=cn(2026, 9, 24, 16)) == []


# ================================================================ 计算结果

def test_transitions_as_data_grows(pws: Path, sim: str) -> None:
    record_all()
    status = lambda: dict(zip(                                               # noqa: E731
        (f"{r['kind']}:{r['code']}" for r in paper.trades()), (r["status"] for r in paper.trades()), strict=False))

    write_panel(T + 1)                  # 面板只到信号日：全部待买入
    assert set(status().values()) == {"pending"}
    s0: dict = paper.summary()
    assert s0["kinds"]["swing"]["pending"] == 2 and s0["kinds"]["swing"]["equity"] == []

    write_panel(T + 2)                  # 有了 T+1
    st: dict = status()
    assert st == {"swing:600001": "holding", "swing:600002": "unfilled", "streak:000003": "holding",
                  "first:600004": "holding", "first:000005": "unfilled"}
    a: dict = next(r for r in paper.trades(kind="swing", status="holding"))
    cost: float = 10.8 * (1 + SLIP) * (1 + FEE)
    assert a["entry_date"] == DAYS[T + 1].isoformat() and a["exit_date"] is None
    assert abs(a["ret"] - (11.5 / cost - 1)) < 0.006                     # 按最新收盘价的浮动收益
    assert a["status_label"] == "持有中" and a["kind_label"] == "强势股波段" and a["name"] == "测试600001"

    write_panel(T + 3)                  # 有了 T+2：三笔都卖出
    st = status()
    assert st["swing:600001"] == "closed" and st["streak:000003"] == "closed" and st["first:600004"] == "closed"
    assert st["swing:600002"] == "unfilled" and st["first:000005"] == "unfilled"
    rows: dict[str, dict] = {r["code"]: r for r in paper.trades(status="closed")}
    assert set(rows) == {"600001", "000003", "600004"}
    for code, px in (("600001", 11.2), ("000003", 11.4), ("600004", 10.6)):
        assert rows[code]["exit_date"] == DAYS[T + 2].isoformat(), code
        assert abs(rows[code]["exit"] - px) < 0.05, code
    exp_a: float = 11.2 * (1 - SLIP) * (1 - FEE - STAMP) / cost - 1
    assert abs(rows["600001"]["ret"] - exp_a) < 0.004 and rows["600001"]["exit_reason"]

    summ: dict = paper.summary()
    sw: dict = summ["kinds"]["swing"]
    assert (sw["signals"], sw["filled"], sw["unfilled"], sw["open"], sw["closed"]) == (2, 1, 1, 0, 1)
    assert sw["win_rate"] == 1.0 and abs(sw["avg_return"] - rows["600001"]["ret"]) < 1e-4
    assert sw["equity"][0] == {"date": SD.isoformat(), "value": 1.0}
    # 账户式曲线：这一笔只占 4%（波段默认单只仓位），其余资金不产生收益 → 终点约 1 + 4%×ret
    assert sw["position_pct"] == 0.04
    assert abs(sw["equity"][-1]["value"] - (1 + 0.04 * rows["600001"]["ret"])) < 2e-4
    assert abs(sw["total_return"] - (sw["equity"][-1]["value"] - 1)) < 1e-5
    assert summ["since"] == SD.isoformat() and summ["data_end"] == DAYS[T + 2].isoformat()
    assert summ["kinds"]["first"]["closed"] == 1 and summ["kinds"]["first"]["unfilled"] == 1
    assert summ["equity_method"] and summ["warnings"] == []
    json.dumps(summ, allow_nan=False)
    # 过滤与排序
    assert [r["kind"] for r in paper.trades()][:2] == ["swing", "swing"]
    assert len(paper.trades(limit=2)) == 2 and paper.trades(kind="streak", status="pending") == []
    with pytest.raises(ValueError, match="状态"):
        paper.trades(status="sold")
    with pytest.raises(ValueError, match="策略"):
        paper.trades(kind="abc")


def test_equity_curve_math() -> None:
    """两笔重叠的交易：每天等权平均当日收益，逐日复利；各笔各天系数乘积 = 1+ret"""
    d = [date(2026, 9, x) for x in (21, 22, 23, 24)]
    frame = pl.DataFrame({
        "code": ["A", "B"], "status": ["closed", "holding"], "entry_date": [d[1], d[2]], "entry": [10.0, 20.0],
        "exit_date": [d[3], None], "exit": [12.0, None], "ret": [0.18, 0.05],
    }, schema={"code": pl.Utf8, "status": pl.Utf8, "entry_date": pl.Date, "entry": pl.Float64,
               "exit_date": pl.Date, "exit": pl.Float64, "ret": pl.Float64})
    closes = pl.DataFrame({
        "code": ["A"] * 4 + ["B"] * 4, "date": d * 2,
        "close": [9.0, 11.0, 11.5, 12.5, 19.0, 19.5, 20.0, 21.0],
    })
    curve: list[dict] = paper.equity_curve(frame, closes, start=d[0])
    assert [c["date"] for c in curve] == [x.isoformat() for x in d]
    # A：9-22 11/10，9-23 11.5/11，9-24 卖出 12/11.5 再乘费用调整 1.18/1.2
    # B：9-23 20/20，9-24 21/20 再乘 1.05/1.05
    a = [11 / 10, 11.5 / 11, 12 / 11.5 * 1.18 / 1.2]
    b = [1.0, 21 / 20 * 1.05 / 1.05]
    expect: list[float] = [1.0, a[0], a[0] * (1 + ((a[1] - 1) + (b[0] - 1)) / 2)]
    expect.append(expect[-1] * (1 + ((a[2] - 1) + (b[1] - 1)) / 2))
    for got, want in zip(curve, expect, strict=True):
        assert abs(got["value"] - want) < 1e-6
    assert paper.equity_curve(frame.filter(pl.col("code") == "Z"), closes) == []


def test_empty_and_broken_files(pws: Path, stub: None) -> None:
    s: dict = paper.summary()
    assert set(s["kinds"]) == {"swing", "streak", "first"} and s["since"] is None and s["warnings"] == []
    for k in s["kinds"].values():
        assert k["signals"] == 0 and k["equity"] == [] and k["win_rate"] is None and k["total_return"] is None
    assert paper.trades() == [] and paper.evaluate().is_empty()

    paper.paper_dir().mkdir(parents=True, exist_ok=True)
    paper.signals_path().write_bytes(b"")                      # 0 字节文件当作空
    paper.days_path().write_bytes(b"")
    assert paper.trades() == [] and paper.summary()["kinds"]["swing"]["signals"] == 0

    record_all()                                                 # 面板还不存在：全部待买入
    assert {r["status"] for r in paper.trades()} == {"pending"}

    paper.signals_path().write_bytes(b"not a parquet file")
    s = paper.summary()
    assert s["warnings"] and "损坏" in s["warnings"][0] and s["kinds"]["swing"]["signals"] == 0
    with pytest.raises(RuntimeError, match="损坏"):
        paper.record_signals("swing", result("swing", DAYS[T + 1], ["600001"]), None, strict=False)
    assert paper.signals_path().read_bytes() == b"not a parquet file"       # 不覆盖坏文件


def test_summary_without_simulator(pws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """逐笔成交模块还没准备好：summary 仍显示记录了哪些信号，并给出中文提示"""
    monkeypatch.delattr(backtest, "simulate_trades", raising=False)
    write_panel(T + 3)
    record_all()
    s: dict = paper.summary()
    assert s["kinds"]["swing"]["signals"] == 2 and s["kinds"]["swing"]["pending"] == 2
    assert s["warnings"] and "算不出" in s["warnings"][0]
    with pytest.raises(ImportError):
        paper.trades()


# ================================================================ 每个策略用的设置

def test_settings_for(pws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_mod.save(settings_mod.Settings.model_validate(
        {"predict": {"top_n": 7, "threshold": 0.2, "weights": {"technical": 1.5}}, "trade": {"capital": 50_000}}))
    first = paper.settings_for("first")
    assert first.predict.kind == "first" and first.predict.top_n == 7 and first.predict.threshold == 0.2

    def defaults_for(kind: str) -> dict:
        return {"predict": {"kind": kind, "threshold": 0.01, "top_n": 5, "boards": ["main"]},
                "trade": {"position_pct": 0.04, "max_positions": 25}}

    monkeypatch.setattr(settings_mod, "defaults_for", defaults_for, raising=False)
    swing = paper.settings_for("swing")
    assert swing.predict.kind == "swing" and swing.predict.threshold == 0.01 and swing.predict.boards == ["main"]
    assert swing.trade.position_pct == 0.04 and swing.trade.max_positions == 25
    # 只换随模型切换的字段（settings.KIND_FIELDS），权重、资金等保留（与 settings.for_kind 一致）
    assert swing.predict.weights["technical"] == 1.5 and swing.trade.capital == 50_000
    assert paper.settings_for("streak").predict.top_n == 7          # 同一类：沿用保存的设置


def test_calendar_kw(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟盘算结果时把全市场交易日历传给 simulate_trades（只含信号股的面板认不出买入日停牌）"""
    from quant_web.market import history

    cal: list[date] = [date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21)]
    monkeypatch.setattr(history, "trade_dates", lambda start=None, end=None: [d for d in cal if d >= start])
    assert paper._calendar_kw(stub_simulate, date(2026, 9, 18)) == {}       # 替身不接受 calendar
    assert paper._calendar_kw(backtest.simulate_trades, date(2026, 9, 18)) == {"calendar": cal}


# ================================================================ 每日任务与调度

def _install_service(monkeypatch: pytest.MonkeyPatch, service: types.ModuleType) -> None:
    monkeypatch.setitem(sys.modules, "quant_web.predict.service", service)
    monkeypatch.setattr(quant_web.predict, "service", service, raising=False)


def test_run_daily_records_paper(pws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    thu: date = date(2026, 9, 24)
    monkeypatch.setattr(paper, "china_now", lambda: cn(2026, 9, 24, 16))
    calls: list[str] = []

    def predict_latest(kind: str, settings: object = None) -> dict:
        calls.append(kind)
        return result(kind, thu, ["600001", "000003"], picks=1)

    service = types.ModuleType("quant_web.predict.service")
    service.daily_pipeline = lambda progress=None: {"trained": [], "predictions": {}}
    service.predict_latest = predict_latest
    _install_service(monkeypatch, service)
    out: dict = jobs.run_daily(None)
    assert out["paper"]["recorded"] == 3 and sorted(calls) == ["first", "streak", "swing"]
    assert jobs.run_daily(None)["paper"]["recorded"] == 0              # 再跑一次：不重复记录
    assert paper.load_signals().height == 3

    # 流水线自己记录过（结果里有 paper）：不再记录
    service.daily_pipeline = lambda progress=None: {"paper": {"recorded": 3}}
    calls.clear()
    assert jobs.run_daily(None)["paper"] == {"recorded": 3} and calls == []

    # 收盘前手动一键更新：不记录，给出原因，任务本身不失败
    monkeypatch.setattr(paper, "china_now", lambda: cn(2026, 9, 24, 11))
    service.daily_pipeline = lambda progress=None: {"trained": []}
    assert "15:05" in jobs.run_daily(None)["paper"]["error"]
    with pytest.raises(RuntimeError, match="15:05"):
        jobs.run_paper(None)


def test_scheduler_records_paper_when_data_is_current(pws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mono: dict = {"t": 1000.0}
    monkeypatch.setattr(scheduler._time, "monotonic", lambda: mono["t"])
    manager = jobs.JobManager(capture_output=False)
    ran: list[str] = []
    pending: dict[str, list[str]] = {"kinds": ["swing"]}
    asked: list[date] = []

    def pending_fn(day: date) -> list[str]:
        asked.append(day)
        return pending["kinds"]

    sch = scheduler.Scheduler(
        jobs=manager, runner=lambda p: ran.append("daily"), now_fn=lambda: cn(2026, 9, 24, 16),
        calendar_fn=lambda: DAYS, last_date_fn=lambda: date(2026, 9, 24),
        paper_fn=lambda p: ran.append("paper"), paper_pending_fn=pending_fn,
    )
    job_id: str | None = sch.check()
    assert job_id is not None and manager.wait(job_id, 5)["status"] == "done" and ran == ["paper"]
    assert asked == [date(2026, 9, 24)] and "模拟盘" in sch.message and manager.get(job_id)["name"] == "paper"
    assert sch.check() is None and ran == ["paper"]                      # 30 分钟内不重复
    mono["t"] += scheduler.RETRY_SECONDS + 1
    pending["kinds"] = []                                               # 已经记录好了
    assert sch.check() is None and ran == ["paper"] and "最新" in sch.message

    # 没配置模拟盘钩子（测试里的其他调度器）：行为不变
    plain = scheduler.Scheduler(jobs=manager, runner=lambda p: None, now_fn=lambda: cn(2026, 9, 24, 16),
                                calendar_fn=lambda: DAYS, last_date_fn=lambda: date(2026, 9, 24))
    assert plain.check() is None
    assert scheduler.SCHEDULER.paper_fn is jobs.run_paper and scheduler.SCHEDULER.paper_pending_fn is not None


# ================================================================ 第五轮：记录时的设置、不重复买、账户式曲线、原因文字

def test_record_stores_trade_settings_and_evaluate_uses_them(pws: Path) -> None:
    from quant_web.predict import execution

    no_cost = {"trade": {"exit_rule": "until_break_close", "slippage": 0.0, "fee_rate": 0.0, "stamp_by_date": False,
                         "stamp_duty": 0.0, "max_gap_pct": 30.0, "position_pct": 0.1}}
    assert paper.record_signals("swing", result("swing", SD, ["600001", "600002"]), no_cost, strict=False) == 2
    assert paper.record_signals("first", result("first", SD, ["600004", "000005"]), trade_settings("next_open"),
                                strict=False) == 2
    sig: pl.DataFrame = paper.load_signals()
    sw: dict = sig.filter(pl.col("code") == "600001").row(0, named=True)
    assert (sw["slippage"], sw["fee_rate"], sw["stamp_by_date"], sw["stamp_duty"], sw["max_gap_pct"],
            sw["position_pct"]) == (0.0, 0.0, False, 0.0, 30.0, 0.1)
    fi: dict = sig.filter(pl.col("code") == "600004").row(0, named=True)
    assert fi["slippage"] is None and fi["position_pct"] is None          # 没给的留空，算的时候用当前设置补
    write_panel(T + 3)
    rows: dict[str, dict] = {f"{r['kind']}:{r['code']}": r for r in paper.trades()}
    # 记录时不收费用、没有滑点：收益就是 卖价/买价 - 1（当前设置有佣金和滑点，也不改写历史）
    assert rows["swing:600001"]["ret"] == pytest.approx(11.2 / 10.8 - 1, abs=1e-5)
    assert rows["swing:600001"]["position_pct"] == 0.1
    exp_first: float = 10.6 * (1 - SLIP) * (1 - FEE - STAMP) / (10.4 * (1 + SLIP) * (1 + FEE)) - 1
    assert rows["first:600004"]["ret"] == pytest.approx(exp_first, abs=1e-5)
    # 买不进的原因：一字板 / 买入日停牌（与逐笔成交模块同一句话）
    assert rows["swing:600002"]["exit_reason"] == execution.ONE_WORD_TEXT == "一字板，买不进"
    assert rows["first:000005"]["exit_reason"] == execution.SUSPENDED_TEXT
    s: dict = paper.summary()
    assert s["kinds"]["swing"]["position_pct"] == 0.1 and s["kinds"]["first"]["position_pct"] == 0.2
    assert s["kinds"]["streak"]["position_pct"] == 0.2 and "账户" in s["equity_method"]
    # 早期记录（文件里没有这些列）：照样能读，费用按当前设置
    old: pl.DataFrame = sig.drop(["max_gap_pct", "fee_rate", "slippage", "stamp_by_date", "stamp_duty", "position_pct"])
    old.write_parquet(paper.signals_path())
    rows = {f"{r['kind']}:{r['code']}": r for r in paper.trades()}
    exp_sw: float = 11.2 * (1 - SLIP) * (1 - FEE - STAMP) / (10.8 * (1 + SLIP) * (1 + FEE)) - 1
    assert rows["swing:600001"]["ret"] == pytest.approx(exp_sw, abs=1e-5)
    assert paper.summary()["kinds"]["swing"]["position_pct"] == 0.04            # 波段默认 4%


def test_record_skips_stocks_still_held(pws: Path) -> None:
    write_panel(T + 2)                              # 600001：T+1 买入，还没卖出
    assert paper.record_signals("swing", result("swing", SD, ["600001"]), trade_settings("until_break_close"),
                                strict=False) == 1
    nxt: date = DAYS[T + 1]
    detail: dict = paper._record("swing", result("swing", nxt, ["600001", "600004"]),
                                 trade_settings("until_break_close"), strict=False)
    assert detail["status"] == "recorded" and detail["recorded"] == 1 and detail["held_skipped"] == 1
    assert "还在持有中" in detail["message"]
    assert paper.load_signals().filter(pl.col("signal_date") == nxt)["code"].to_list() == ["600004"]
    day_row: dict = paper.load_days().filter(pl.col("signal_date") == nxt).row(0, named=True)
    assert day_row["picks"] == 1 and "不重复买" in day_row["note"]
    # 选中的全都还拿着：今天没有新买入（这一天照样锁定）
    only: dict = paper._record("swing", result("swing", DAYS[T + 2], ["600001", "600004"]),
                               trade_settings("until_break_close"), strict=False)
    assert only["status"] == "no_trade" and only["recorded"] == 0 and only["held_skipped"] == 2
    # 别的策略不受影响
    assert paper.record_signals("first", result("first", nxt, ["600001"]), trade_settings("next_open"),
                                strict=False) == 1
    # 卖出之后可以再买
    write_panel(T + 4)
    again: dict = paper._record("swing", result("swing", DAYS[T + 4], ["600001"]),
                                trade_settings("until_break_close"), strict=False)
    assert again["status"] == "recorded" and again["held_skipped"] == 0


def test_equity_curve_account_like_weights_and_cap() -> None:
    d = [date(2026, 9, x) for x in (21, 22, 23, 24)]
    schema = {"code": pl.Utf8, "status": pl.Utf8, "entry_date": pl.Date, "entry": pl.Float64,
              "exit_date": pl.Date, "exit": pl.Float64, "ret": pl.Float64, "position_pct": pl.Float64}
    closes = pl.DataFrame({"code": ["A"] * 4 + ["B"] * 4, "date": d * 2,
                           "close": [9.0, 11.0, 11.5, 12.5, 19.0, 19.5, 20.0, 21.0]})
    a = [11 / 10, 11.5 / 11, 12 / 11.5 * 1.18 / 1.2]
    b = [1.0, 21 / 20]

    def frame(wa: float, wb: float) -> pl.DataFrame:
        return pl.DataFrame({"code": ["A", "B"], "status": ["closed", "holding"], "entry_date": [d[1], d[2]],
                             "entry": [10.0, 20.0], "exit_date": [d[3], None], "exit": [12.0, None],
                             "ret": [0.18, 0.05], "position_pct": [wa, wb]}, schema=schema)

    # 每笔占 4%、其余资金不产生收益：当天账户收益 = Σ 4% × 当天收益
    curve = paper.equity_curve(frame(0.04, 0.04), closes, start=d[0])
    v1 = 1 + 0.04 * (a[0] - 1)
    v2 = v1 * (1 + 0.04 * (a[1] - 1) + 0.04 * (b[0] - 1))
    v3 = v2 * (1 + 0.04 * (a[2] - 1) + 0.04 * (b[1] - 1))
    assert [c["value"] for c in curve] == pytest.approx([1.0, v1, v2, v3], abs=1e-6)
    # 仓位合计超过 100%（钱不够全买）：按比例缩小到 100% → 两笔各 50%
    capped = paper.equity_curve(frame(0.8, 0.8), closes, start=d[0])
    w2 = v = 1 + 0.8 * (a[0] - 1)
    w2 = v * (1 + 0.5 * (a[1] - 1) + 0.5 * (b[0] - 1))
    assert capped[1]["value"] == pytest.approx(v, abs=1e-6) and capped[2]["value"] == pytest.approx(w2, abs=1e-6)


def test_calibrate_weekly_in_daily_job(pws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一键更新最后每周一次核对除权日昨收：限时、不到一周不重复、出错不影响任务"""
    from datetime import timedelta

    from quant_web.market import history

    calls: list[float] = []
    state: dict = {"last": date(2026, 9, 24), "fail": False}

    def fake_calibrate(progress=None, start=None, timeout=3600.0, batch=1000) -> dict:
        calls.append(timeout)
        if state["fail"]:
            raise ConnectionError("baostock 连不上")
        return {"events": 3, "checked": 3, "fixed": 1, "changed": 1, "reapplied": 0, "remaining": 0, "seconds": 0.1}

    monkeypatch.setattr(history, "calibrate_ex_rights", fake_calibrate)
    monkeypatch.setattr(history, "last_date", lambda: state["last"])
    now = cn(2026, 9, 24, 16)
    assert jobs.calibrate_weekly(now=now)["changed"] == 1 and calls == [jobs.CALIBRATE_TIMEOUT]
    stamp: dict = json.loads(config.STOCK_LAB.joinpath(jobs.CALIBRATE_STAMP).read_text(encoding="utf-8"))
    assert stamp["last_run"].startswith("2026-09-24T16:00") and stamp["result"]["checked"] == 3
    assert jobs.calibrate_weekly(now=now + timedelta(days=6)) is None and len(calls) == 1      # 不到一周
    state["fail"] = True
    assert "baostock" in jobs.calibrate_weekly(now=now + timedelta(days=7, minutes=1))["error"]
    state["fail"], state["last"] = False, None
    config.STOCK_LAB.joinpath(jobs.CALIBRATE_STAMP).unlink()
    assert jobs.calibrate_weekly(now=now) is None and len(calls) == 2                          # 还没有日线

    # 每日任务：到期时结果里有 calibrate；核对失败也不影响任务本身
    state["last"] = date(2026, 9, 24)
    service = types.ModuleType("quant_web.predict.service")
    service.daily_pipeline = lambda progress=None: {"paper": {"recorded": 0}}
    _install_service(monkeypatch, service)
    out: dict = jobs.run_daily(None)
    assert out["calibrate"]["changed"] == 1 and len(calls) == 3
    assert "calibrate" not in jobs.run_daily(None)                                              # 同一周不再跑
