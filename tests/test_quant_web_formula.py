"""quant_web 第三版：公式系统（解析安全、未来函数、语义、与逐行实现一致、不偷看未来、内置公式全部可运行；全部离线）"""
import re
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from quant_web.formula import engine, library
from quant_web.formula.parser import FormulaError, compile_formula
from quant_web.indicators import ta
from quant_web.indicators.funcs import Ctx


def panel(n_codes: int = 3, n_days: int = 420, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    frames = []
    start = date(2024, 1, 1)
    for k in range(n_codes):
        ret = rng.normal(0.0005, 0.022, n_days)
        close = 10 * (k + 1) * np.exp(np.cumsum(ret))
        open_ = close * np.exp(rng.normal(0, 0.01, n_days))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.012, n_days)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.012, n_days)))
        vol = rng.integers(5_000, 200_000, n_days).astype(float) * 100
        frames.append(pl.DataFrame({
            "date": [start + timedelta(days=i) for i in range(n_days)], "code": [f"60000{k}"] * n_days,
            "open": open_, "high": high, "low": low, "close": close, "preclose": np.concatenate([[close[0]], close[:-1]]),
            "volume": vol, "amount": vol * close, "turn": rng.uniform(0.5, 6, n_days), "tradestatus": [1] * n_days,
            "is_st": [False] * n_days,
        }))
    return pl.concat(frames)


@pytest.fixture(scope="module")
def frame() -> pl.DataFrame:
    return engine.prepare_frame(panel())


def run(text: str, fr: pl.DataFrame, chips: pl.DataFrame | None = None) -> engine.FormulaResult:
    return engine.evaluate(compile_formula(text), fr, chips)


# ---------------------------------------------------------------- 安全与报错

@pytest.mark.parametrize("text, msg", [
    ("XG:__import__('os');", "引号"),
    ("XG:C.__class__;", "指标.线名"),
    ("XG:C[0]>1;", "不支持的写法"),
    ("XG:(lambda: 1)();", "语法不对"),              # 转大写后 LAMBDA 不是关键字：直接语法错误
    ("XG:ZIG(C,5)>C;", "未来函数 ZIG"),
    ("XG:BACKSET(C>O,3);", "未来函数 BACKSET"),
    ("XG:REFX(C,1)>C;", "未来函数 REFX"),
    ("XG:FOO(C,1);", "不支持函数 FOO"),
    ("XG:C>X1;", "不认识「X1」"),
    ("XG:MA(C,C)>1;", "必须是固定的数字"),
    ("XG:MA(C)>1;", "参数个数不对"),
    ("XG:C**2>1;", "四则运算"),
    ("XG:MACD.XYZ>0;", "没有「XYZ」这条线"),
    ("XG:WINNER(O)>0.5;", "只支持 WINNER(C)"),
    ("C:=MA(C,5);", "保留的名字"),
    ("XG:(C>O;", "语法不对"),
    ("", "空的"),
])
def test_rejects_unsafe_or_wrong(text: str, msg: str) -> None:
    with pytest.raises(FormulaError, match=re.escape(msg)):
        compile_formula(text)


def test_rejects_too_long_and_too_many() -> None:
    with pytest.raises(FormulaError, match="太长"):
        compile_formula("XG:" + "+".join(["C"] * 3000) + ";")
    with pytest.raises(FormulaError, match="语句太多"):
        compile_formula("".join(f"A{i}:=C;" for i in range(100)))


# ---------------------------------------------------------------- 语义

def test_syntax_features(frame: pl.DataFrame) -> None:
    c, o = frame["close"], frame["open"]
    v = frame["volume"] / 100
    r = run("{注释} X:=MA(C,5); // 行注释\n 均线:X,COLORRED,LINETHICK2; XG:C>X;", frame)
    assert "均线" in r.outputs
    ma5 = ta.ma({"close": c}, Ctx(frame["code"]), n1=5, n2=0, n3=0, n4=0)[0][2]
    assert r.condition.to_list() == (c > ma5).fill_null(False).to_list()
    r2 = run("XG:C>O AND V>0 OR NOT C>O;", frame)                      # 优先级：(C>O AND V>0) OR (NOT C>O)
    exp = (((c > o) & (v > 0)) | ~(c > o)).to_list()
    assert r2.condition.to_list() == exp
    r3 = run("XG:C=C AND C<>O;", frame)
    assert r3.condition.to_list() == (c != o).to_list()
    r4 = run("XG:0<C-O<1;", frame)                                       # 连写比较按"同时成立"
    assert r4.condition.to_list() == (((c - o) > 0) & ((c - o) < 1)).to_list()
    r5 = run('XG:"MACD.DIF">MACD.DEA;', frame)
    lines = {k: s for k, _, s, _ in ta.macd({"close": c}, Ctx(frame["code"]))}
    assert r5.condition.to_list() == (lines["dif"] > lines["dea"]).fill_null(False).to_list()
    r6 = run("xg:c>ref(c,1) && v>0;", frame)                            # 大小写不敏感，&& 等价 AND
    assert r6.condition.sum() > 0


def test_matches_row_by_row_reference(frame: pl.DataFrame) -> None:
    """XG:CROSS(MA(C,5),MA(C,20)) AND V>MA(V,10)*1.2 与逐行循环实现一致（逐只股票）"""
    r = run("XG:CROSS(MA(C,5),MA(C,20)) AND V>MA(V,10)*1.2;", frame)
    got = r.condition.to_list()
    exp: list[bool] = []
    for code in frame["code"].unique(maintain_order=True).to_list():
        sub = frame.filter(pl.col("code") == code)
        c = sub["close"].to_list()
        v = (sub["volume"] / 100).to_list()
        ma = lambda x, n, i: None if i + 1 < n else sum(x[i - n + 1:i + 1]) / n   # noqa: E731
        for i in range(len(c)):
            a5, a20, p5, p20, mv = ma(c, 5, i), ma(c, 20, i), ma(c, 5, i - 1) if i else None, ma(c, 20, i - 1) if i else None, ma(v, 10, i)
            cross = a5 is not None and a20 is not None and p5 is not None and p20 is not None and a5 > a20 and p5 <= p20
            exp.append(bool(cross and mv is not None and v[i] > mv * 1.2))
    assert got == exp


def test_dynamic_ref_barslast_and_counts(frame: pl.DataFrame) -> None:
    r = run("ZT:=C>REF(C,1)*1.03;N:=BARSLAST(ZT);P:REF(C,N+1);XG:COUNT(ZT,10)>=2;", frame)
    sub = frame.with_columns(r.outputs["P"].alias("P"), r.condition.alias("XG"))
    one = sub.filter(pl.col("code") == "600000")
    closes = one["close"].to_list()
    zt = [i > 0 and closes[i] > closes[i - 1] * 1.03 for i in range(len(closes))]
    for i in range(30, 120):
        last = max((j for j in range(i + 1) if zt[j]), default=None)
        exp = None if last is None or last - 1 < 0 else closes[last - 1]
        got = one["P"][i]
        assert (got is None and exp is None) or abs(got - exp) < 1e-9, (i, got, exp)
    assert sub["XG"].dtype == pl.Boolean


def test_no_lookahead_for_library(frame: pl.DataFrame) -> None:
    """把第 300 天以后的数据全部改掉，前 300 天的信号必须完全不变"""
    cut = frame["date"].unique().sort()[300]
    changed = frame.with_columns([
        pl.when(pl.col("date") > cut).then(pl.col(c) * 3).otherwise(pl.col(c)).alias(c)
        for c in ("open", "high", "low", "close", "volume")
    ])
    for f in library.LIBRARY:
        if "WINNER" in f["text"] or "COST" in f["text"]:
            continue
        a = run(f["text"], frame).condition
        b = run(f["text"], changed).condition
        mask = (frame["date"] <= cut)
        assert a.filter(mask).to_list() == b.filter(mask).to_list(), f["id"]


def test_library_compiles_and_runs_with_chips(frame: pl.DataFrame) -> None:
    raw = panel()
    chips = engine.chips_for_frame(frame, raw)
    assert chips.height == frame.height
    ids = set()
    for f in library.LIBRARY:
        prog = compile_formula(f["text"])
        assert f["kind"] in ("select", "warn") and f["group"] in library.GROUPS and f["explain"] and f["trap"]
        res = engine.evaluate(prog, frame, chips if prog.uses_chips else None)
        assert res.condition is not None and res.condition.dtype == pl.Boolean and len(res.condition) == frame.height, f["id"]
        ids.add(f["id"])
    assert len(ids) == len(library.LIBRARY) >= 24


def test_lookback_and_chip_flags() -> None:
    assert compile_formula("XG:C>MA(C,250);").lookback >= 250
    assert compile_formula("A:=EMA(C,30);XG:C>MA(A,20);").lookback >= 140          # 变量的历史长度会累加
    p = compile_formula("XG:WINNER(C)>0.5 AND C>COST(50);")
    assert p.uses_chips and p.condition == "XG"
    assert compile_formula("C>O;").condition == "#1"
    with pytest.raises(FormulaError, match="筹码"):
        engine.evaluate(p, engine.prepare_frame(panel(1, 60)), None)
