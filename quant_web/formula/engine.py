"""
公式计算：在"多只股票 × 多天"的表上逐句计算已编译的公式（parser.compile_formula 的结果）。

输入表（load_frame 生成）：按 (code, date) 排序；open/high/low/close 为前复权价；volume 为股、amount 为元、
turn 为换手率 %、adj_factor（前复权系数，筹码价格换算用）；停牌日（没有成交）不在表里，和通达信的 K 线一致。
数据字段 V/VOL 按"手"（股数÷100）提供，和通达信一致。

结果：FormulaResult(outputs={输出名: Series}, condition=布尔 Series 或 None)，与输入表逐行对齐。
条件里的空值一律当作"不成立"。
"""
from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl

from ..indicators import funcs as F
from ..indicators import ta
from ..indicators.funcs import Ctx
from .parser import DATA_FIELDS, INDICATOR_REFS, FormulaError, Program

Val = pl.Series | float


@dataclass
class FormulaResult:
    outputs: dict[str, pl.Series] = field(default_factory=dict)
    condition: pl.Series | None = None


def _num(v: Val, n: int) -> pl.Series:
    """布尔/常数 → Float64 Series"""
    if isinstance(v, pl.Series):
        return v.cast(pl.Float64, strict=False)
    return pl.Series(np.full(n, float(v)), dtype=pl.Float64)


def _clean(s: pl.Series) -> pl.Series:
    return s.fill_nan(None) if s.dtype in (pl.Float64, pl.Float32) else s


class Evaluator:
    def __init__(self, frame: pl.DataFrame, chips: pl.DataFrame | None = None) -> None:
        self.df: pl.DataFrame = frame
        self.n: int = frame.height
        self.ctx: Ctx = Ctx(frame["code"]) if "code" in frame.columns else Ctx(None, frame.height)
        vol: pl.Series = frame["volume"].cast(pl.Float64)
        turn: pl.Series = frame["turn"].cast(pl.Float64) if "turn" in frame.columns else pl.Series([None] * self.n, dtype=pl.Float64)
        self.data: dict[str, pl.Series] = {
            "open": frame["open"].cast(pl.Float64), "high": frame["high"].cast(pl.Float64),
            "low": frame["low"].cast(pl.Float64), "close": frame["close"].cast(pl.Float64),
            "vol": vol / 100, "amount": frame["amount"].cast(pl.Float64) if "amount" in frame.columns else vol * 0,
            "turn": turn, "capital": F.safe_div(vol / 100, turn / 100),
        }
        self.chips: pl.DataFrame | None = chips
        self._ind: dict[str, dict[str, pl.Series]] = {}

    # ------------------------------------------------ 运行
    def run(self, prog: Program) -> FormulaResult:
        env: dict[str, Val] = {}
        res = FormulaResult()
        anon: dict[str, Val] = {}
        for st in prog.statements:
            try:
                val: Val = self.ev(st.node, env)
            except FormulaError:
                raise
            except Exception as e:  # noqa: BLE001  计算出错时指出是哪一句
                raise FormulaError(f"第 {st.index} 句算不出来：{e}（「{st.text}」）") from None
            if st.name:
                env[st.name] = val
            if st.kind == "out":
                is_bool: bool = isinstance(val, pl.Series) and val.dtype == pl.Boolean
                res.outputs[st.name] = val if is_bool else _clean(_num(val, self.n))      # type: ignore[index,assignment]
            elif st.kind == "expr":
                anon[f"#{st.index}"] = val
        if prog.condition:
            v: Val | None = res.outputs.get(prog.condition, anon.get(prog.condition))
            if v is None and prog.condition in env:
                v = env[prog.condition]
            if v is not None:
                res.condition = F._bool(_num(v, self.n) if not isinstance(v, pl.Series) else v).alias("signal")
        return res

    # ------------------------------------------------ 求值
    def ev(self, node: ast.expr, env: dict[str, Val]) -> Val:
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            return self.data[DATA_FIELDS[node.id]]
        if isinstance(node, ast.Attribute):
            ind_id, key = INDICATOR_REFS[node.value.id][node.attr]         # type: ignore[union-attr]
            return self._indicator(ind_id)[key]
        if isinstance(node, ast.UnaryOp):
            v = self.ev(node.operand, env)
            if isinstance(node.op, ast.Not):
                return ~F._bool(_num(v, self.n) if not isinstance(v, pl.Series) else v)
            if isinstance(node.op, ast.USub):
                return -v if not isinstance(v, pl.Series) else -_num(v, self.n)
            return v
        if isinstance(node, ast.BinOp):
            a, b = self.ev(node.left, env), self.ev(node.right, env)
            if not isinstance(a, pl.Series) and not isinstance(b, pl.Series):
                return self._scalar_binop(node.op, a, b)
            a, b = _num(a, self.n), _num(b, self.n)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            return F.safe_div(a, b)
        if isinstance(node, ast.BoolOp):
            parts = [F._bool(_num(v, self.n) if not isinstance(v, pl.Series) else v) for v in (self.ev(x, env) for x in node.values)]
            out: pl.Series = parts[0]
            for p in parts[1:]:
                out = (out & p) if isinstance(node.op, ast.And) else (out | p)
            return out
        if isinstance(node, ast.Compare):
            left: Val = self.ev(node.left, env)
            result: pl.Series | None = None
            for op, comp in zip(node.ops, node.comparators, strict=True):
                right: Val = self.ev(comp, env)
                cmp: pl.Series = self._compare(op, _num(left, self.n), _num(right, self.n))
                result = cmp if result is None else (result & cmp)
                left = right
            return result                                                  # type: ignore[return-value]
        if isinstance(node, ast.Call):
            return self.call(node.func.id, node.args, env)                  # type: ignore[union-attr]
        raise FormulaError("公式里有不支持的写法")

    @staticmethod
    def _scalar_binop(op: ast.operator, a: float, b: float) -> float:
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        return a / b if b else math.nan

    @staticmethod
    def _compare(op: ast.cmpop, a: pl.Series, b: pl.Series) -> pl.Series:
        if isinstance(op, ast.Gt):
            r = a > b
        elif isinstance(op, ast.GtE):
            r = a >= b
        elif isinstance(op, ast.Lt):
            r = a < b
        elif isinstance(op, ast.LtE):
            r = a <= b
        elif isinstance(op, ast.Eq):
            r = (a - b).abs() < 1e-9
        else:
            r = (a - b).abs() >= 1e-9
        return r.fill_null(False)

    def _indicator(self, ind_id: str) -> dict[str, pl.Series]:
        if ind_id not in self._ind:
            d = {"open": self.data["open"], "high": self.data["high"], "low": self.data["low"],
                 "close": self.data["close"], "volume": self.data["vol"] * 100}
            self._ind[ind_id] = {key: s for key, _, s, _ in ta.COMPUTE[ind_id](d, self.ctx)}
        return self._ind[ind_id]

    # ------------------------------------------------ 函数
    def call(self, name: str, args: list[ast.expr], env: dict[str, Val]) -> Val:
        ctx, n = self.ctx, self.n
        const = lambda i: float(self.ev(args[i], env))       # noqa: E731  解析时已保证是常数（或常数表达式）
        s = lambda i: _num(self.ev(args[i], env), n)                                             # noqa: E731
        raw = lambda i: self.ev(args[i], env)                                                     # noqa: E731
        if name == "MA":
            return F.ma(s(0), int(const(1)), ctx)
        if name in ("EMA", "EXPMA"):
            return F.ema(s(0), int(const(1)), ctx)
        if name == "SMA":
            return F.sma(s(0), int(const(1)), int(const(2)), ctx)
        if name == "WMA":
            return F.wma(s(0), int(const(1)), ctx)
        if name == "DMA":
            return F.dma(s(0), raw(1), ctx)
        if name == "REF":
            k = raw(1)
            return F.ref(s(0), int(k), ctx) if not isinstance(k, pl.Series) else F.ref_dynamic(s(0), k, ctx)
        if name == "HHV":
            return F.hhv(s(0), int(const(1)), ctx)
        if name == "LLV":
            return F.llv(s(0), int(const(1)), ctx)
        if name == "HHVBARS":
            return F.hhvbars(s(0), int(const(1)), ctx)
        if name == "LLVBARS":
            return F.llvbars(s(0), int(const(1)), ctx)
        if name == "SUM":
            return F.sum_(s(0), int(const(1)), ctx)
        if name == "COUNT":
            return F.count(self._bool_arg(args[0], env), int(const(1)), ctx)
        if name == "EVERY":
            return F.every(self._bool_arg(args[0], env), int(const(1)), ctx)
        if name == "EXIST":
            return F.exist(self._bool_arg(args[0], env), int(const(1)), ctx)
        if name == "STD":
            return F.std(s(0), int(const(1)), ctx)
        if name == "AVEDEV":
            return F.avedev(s(0), int(const(1)), ctx)
        if name == "SLOPE":
            return F.slope(s(0), int(const(1)), ctx)
        if name == "CROSS":
            return F.cross(s(0), s(1), ctx)
        if name == "BARSLAST":
            return F.barslast(self._bool_arg(args[0], env), ctx)
        if name == "BARSCOUNT":
            return F.barscount(ctx)
        if name == "FILTER":
            return F.filter_(self._bool_arg(args[0], env), int(const(1)), ctx)
        if name in ("IF", "IFF"):
            return F.if_(self._bool_arg(args[0], env), raw(1), raw(2), ctx)
        if name == "MAX":
            return F.max_(raw(0), raw(1), ctx)
        if name == "MIN":
            return F.min_(raw(0), raw(1), ctx)
        if name == "ABS":
            return s(0).abs()
        if name == "SQRT":
            return _clean(s(0).sqrt())
        if name == "POW":
            return _clean(s(0).pow(s(1)))
        if name == "LN":
            x = s(0)
            return _clean(pl.select(pl.when(x > 0).then(x.log()).otherwise(None)).to_series())
        if name == "LOG":
            x = s(0)
            return _clean(pl.select(pl.when(x > 0).then(x.log10()).otherwise(None)).to_series())
        if name == "EXP":
            return _clean(s(0).exp())
        if name == "SIGN":
            return s(0).sign().cast(pl.Float64)
        if name == "BETWEEN":
            a, b, c = s(0), s(1), s(2)
            lo, hi = F.min_(b, c, ctx), F.max_(b, c, ctx)
            return ((a >= lo) & (a <= hi)).fill_null(False)
        if name == "RANGE":
            a, b, c = s(0), s(1), s(2)
            return ((a > b) & (a < c)).fill_null(False)
        if name == "NOT":
            return ~self._bool_arg(args[0], env)
        if name == "WINNER":
            return self._chip("winner")
        if name == "COST":
            return self._cost(const(0))
        raise FormulaError(f"不支持函数 {name}")

    def _bool_arg(self, node: ast.expr, env: dict[str, Val]) -> pl.Series:
        v = self.ev(node, env)
        return F._bool(v if isinstance(v, pl.Series) else _num(v, self.n))

    # ------------------------------------------------ 筹码
    def _need_chips(self) -> pl.DataFrame:
        if self.chips is None or self.chips.height != self.n:
            raise FormulaError("这个公式用到了筹码函数（WINNER/COST），但当前没有筹码数据")
        return self.chips

    def _chip(self, col: str) -> pl.Series:
        return self._need_chips()[col].cast(pl.Float64)

    def _cost(self, q: float) -> pl.Series:
        """COST(Q)：由 5/15/50/85/95 分位线性插值（两端外推用端点），再换算成前复权价"""
        ch = self._need_chips()
        pts: list[tuple[float, str]] = [(5, "p5"), (15, "p15"), (50, "p50"), (85, "p85"), (95, "p95")]
        if q <= 5:
            raw = ch["p5"]
        elif q >= 95:
            raw = ch["p95"]
        else:
            for (q0, c0), (q1, c1) in zip(pts, pts[1:], strict=False):
                if q0 <= q <= q1:
                    w = (q - q0) / (q1 - q0)
                    raw = ch[c0] * (1 - w) + ch[c1] * w
                    break
        fac: pl.Series = self.df["adj_factor"] if "adj_factor" in self.df.columns else pl.Series(np.ones(self.n))
        return (raw.cast(pl.Float64) * fac).alias("cost")


def evaluate(prog: Program, frame: pl.DataFrame, chips: pl.DataFrame | None = None) -> FormulaResult:
    return Evaluator(frame, chips).run(prog)


# ---------------------------------------------------------------- 数据准备

FRAME_COLS: list[str] = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "tradestatus", "is_st"]


def prepare_frame(panel: pl.DataFrame) -> pl.DataFrame:
    """原始日线面板 → 公式用的表：前复权、去掉停牌日、按 (code, date) 排序。保留 raw_close（真实收盘价）"""
    from ..indicators.adjust import add_qfq

    df: pl.DataFrame = panel.sort(["code", "date"])
    df = add_qfq(df)
    if "tradestatus" in df.columns:
        df = df.filter(pl.col("tradestatus").fill_null(1) != 0)
    df = df.filter(pl.col("volume").fill_null(0) > 0)
    return df.with_columns(
        pl.col("close").alias("raw_close"), pl.col("open").alias("raw_open"),
        pl.col("qopen").alias("open"), pl.col("qhigh").alias("high"), pl.col("qlow").alias("low"),
        pl.col("qclose").alias("close"),
    ).drop(["qopen", "qhigh", "qlow", "qclose"])


def load_frame(codes: list[str] | None = None, start: date | None = None, end: date | None = None) -> pl.DataFrame:
    """从本地日线面板读数据并整理成公式用的表"""
    from ..market import history

    panel: pl.DataFrame = history.load_panel(codes=codes, start=start, end=end, columns=FRAME_COLS)
    if panel.is_empty():
        return panel
    return prepare_frame(panel)


def chips_for_frame(frame: pl.DataFrame, raw_panel: pl.DataFrame, progress=None) -> pl.DataFrame:
    """按 frame 的行顺序给出筹码统计（winner/p5..p95，真实价口径）；raw_panel 为同一批股票的原始日线（含更早的预热期）"""
    from ..indicators import chips

    want: set = set(frame["date"].unique().to_list())
    stats, _ = chips.compute_panel(raw_panel, want=want, progress=progress)
    return frame.select("code", "date").join(stats, on=["code", "date"], how="left")


def lookback_start(prog: Program, end: date) -> date:
    """算准 end 这一天需要从哪天开始读数据（交易日 → 日历日大约 ×1.5）"""
    return end - timedelta(days=int(prog.lookback * 1.5) + 10)


__all__ = ["Evaluator", "FormulaResult", "chips_for_frame", "evaluate", "load_frame", "lookback_start", "prepare_frame"]
