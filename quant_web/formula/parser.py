"""
公式解析（通达信风格的子集）→ 经过白名单校验的语法树。安全：绝不 eval 用户输入。

支持的写法：
    MA5:=MA(C,5);              中间变量（不输出）
    DIF:EMA(C,12)-EMA(C,26);   输出线（在图上画出来）
    XG:CROSS(MA5,MA10) AND V>MA(V,20)*1.5;     选股条件（名为 XG 的输出；没有 XG 时用最后一条）
    注释 {…} 或 // …；运算 + - * /；比较 > < >= <= = <>；逻辑 AND OR NOT（也可写 && ||）
    指标引用 MACD.DIF / KDJ.J / "MACD.DIF"；筹码 WINNER(C)、COST(50)
    行尾的画线属性（,COLORRED ,LINETHICK2 ,NODRAW 等）会被忽略。

校验：
- 只允许：数字常量、已知数据字段、已定义的变量、白名单函数、白名单指标引用、算术/比较/逻辑运算；
- 未来函数（ZIG、PEAK、BACKSET、REFX 等）直接报错并解释；
- 限制长度、语句数、语法树大小和嵌套深度，防止把程序拖死；
- 窗口参数（MA 的 N 等）必须是常数（可以是 5*2 这样的常数表达式）。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field


class FormulaError(ValueError):
    """公式有问题（消息为中文，尽量指出是哪一句）"""


MAX_TEXT: int = 6000
MAX_STATEMENTS: int = 80
MAX_NODES: int = 3000
MAX_DEPTH: int = 60

# 数据字段（前复权价格；VOL 单位"手"与通达信一致）
DATA_FIELDS: dict[str, str] = {
    "O": "open", "OPEN": "open", "H": "high", "HIGH": "high", "L": "low", "LOW": "low", "C": "close", "CLOSE": "close",
    "V": "vol", "VOL": "vol", "AMOUNT": "amount", "AMO": "amount", "TURN": "turn", "CAPITAL": "capital",
}
DATA_HELP: dict[str, str] = {
    "O/OPEN": "开盘价（前复权）", "H/HIGH": "最高价", "L/LOW": "最低价", "C/CLOSE": "收盘价",
    "V/VOL": "成交量（手，1 手 = 100 股）", "AMOUNT": "成交额（元）", "TURN": "换手率（%）", "CAPITAL": "流通股本（手）",
}

# 函数：名称 → (最少参数, 最多参数, 必须是常数的参数下标, 说明)
FUNCS: dict[str, tuple[int, int, tuple[int, ...], str]] = {
    "MA": (2, 2, (1,), "MA(X,N)：N 日简单平均"),
    "EMA": (2, 2, (1,), "EMA(X,N)：N 日指数平均"),
    "EXPMA": (2, 2, (1,), "EXPMA(X,N)：同 EMA"),
    "SMA": (3, 3, (1, 2), "SMA(X,N,M)：移动平均，Y=(M·X+(N-M)·Y')/N"),
    "WMA": (2, 2, (1,), "WMA(X,N)：加权平均，越近权重越大"),
    "DMA": (2, 2, (), "DMA(X,A)：动态平均，Y=A·X+(1-A)·Y'"),
    "REF": (2, 2, (), "REF(X,N)：N 天前的值（N 可以是变量）"),
    "HHV": (2, 2, (1,), "HHV(X,N)：N 日最高值（N=0 表示从第一天起）"),
    "LLV": (2, 2, (1,), "LLV(X,N)：N 日最低值"),
    "HHVBARS": (2, 2, (1,), "HHVBARS(X,N)：N 日内最高值到现在的天数"),
    "LLVBARS": (2, 2, (1,), "LLVBARS(X,N)：N 日内最低值到现在的天数"),
    "SUM": (2, 2, (1,), "SUM(X,N)：N 日累加（N=0 表示从第一天起）"),
    "COUNT": (2, 2, (1,), "COUNT(X,N)：N 日内条件成立的天数"),
    "EVERY": (2, 2, (1,), "EVERY(X,N)：N 日内每天都成立"),
    "EXIST": (2, 2, (1,), "EXIST(X,N)：N 日内至少有一天成立"),
    "STD": (2, 2, (1,), "STD(X,N)：N 日样本标准差"),
    "AVEDEV": (2, 2, (1,), "AVEDEV(X,N)：N 日平均绝对偏差"),
    "SLOPE": (2, 2, (1,), "SLOPE(X,N)：N 日线性回归斜率"),
    "CROSS": (2, 2, (), "CROSS(A,B)：A 上穿 B（今天 A>B 且昨天 A<=B）"),
    "BARSLAST": (1, 1, (), "BARSLAST(X)：上一次 X 成立到现在的天数"),
    "BARSCOUNT": (1, 1, (), "BARSCOUNT(X)：从第一天到现在的天数"),
    "FILTER": (2, 2, (1,), "FILTER(X,N)：X 成立后其后 N 天内的信号忽略"),
    "IF": (3, 3, (), "IF(条件,A,B)：条件成立取 A，否则取 B"),
    "IFF": (3, 3, (), "IFF(条件,A,B)：同 IF"),
    "MAX": (2, 2, (), "MAX(A,B)：两者较大值"),
    "MIN": (2, 2, (), "MIN(A,B)：两者较小值"),
    "ABS": (1, 1, (), "ABS(X)：绝对值"),
    "SQRT": (1, 1, (), "SQRT(X)：平方根"),
    "POW": (2, 2, (), "POW(X,Y)：X 的 Y 次方"),
    "LN": (1, 1, (), "LN(X)：自然对数"),
    "LOG": (1, 1, (), "LOG(X)：以 10 为底的对数"),
    "EXP": (1, 1, (), "EXP(X)：e 的 X 次方"),
    "SIGN": (1, 1, (), "SIGN(X)：正数 1、负数 -1、0 为 0"),
    "BETWEEN": (3, 3, (), "BETWEEN(A,B,C)：A 在 B 和 C 之间（含边界）"),
    "RANGE": (3, 3, (), "RANGE(A,B,C)：B<A<C"),
    "NOT": (1, 1, (), "NOT(X)：取反"),
    "WINNER": (1, 1, (), "WINNER(C)：获利比例（0~1，筹码分布估算，只支持 WINNER(C)）"),
    "COST": (1, 1, (0,), "COST(Q)：Q% 的筹码成本在这个价格以下（筹码分布估算，Q 为 1~99 的常数）"),
}

# 未来函数：用到了"当时还不知道的数据"，回测会神准但实际做不到
FUTURE_FUNCS: dict[str, str] = {
    "ZIG": "ZIG（之字转向）要等后面的走势走出来才能确定转折点，会“修改历史”",
    "PEAK": "PEAK（波峰）依赖 ZIG，要看到后面的走势才能确定",
    "PEAKBARS": "PEAKBARS 依赖 ZIG，要看到后面的走势才能确定",
    "TROUGH": "TROUGH（波谷）依赖 ZIG，要看到后面的走势才能确定",
    "TROUGHBARS": "TROUGHBARS 依赖 ZIG，要看到后面的走势才能确定",
    "BACKSET": "BACKSET 会把今天的信号往前改写到过去几天",
    "REFX": "REFX 引用的是未来 N 天的数据",
    "REFXV": "REFXV 引用的是未来 N 天的数据",
    "XMA": "XMA（中心移动平均）用到了后面半个窗口的数据",
    "BARSNEXT": "BARSNEXT 要看后面的数据",
    "FINDHIGH": "FINDHIGH 可引用未来区间",
    "FINDLOW": "FINDLOW 可引用未来区间",
    "CURRBARSCOUNT": "CURRBARSCOUNT 从最后一根往前数，随数据更新而改变历史信号",
    "DRAWNULL": "DRAWNULL 只用于画图",
}

# 指标引用：指标名 → {字段: (ta 指标 id, 线的 key)}
INDICATOR_REFS: dict[str, dict[str, tuple[str, str]]] = {
    "MACD": {"DIF": ("macd", "dif"), "DEA": ("macd", "dea"), "MACD": ("macd", "macd")},
    "KDJ": {"K": ("kdj", "k"), "D": ("kdj", "d"), "J": ("kdj", "j")},
    "RSI": {"RSI1": ("rsi", "rsi1"), "RSI2": ("rsi", "rsi2"), "RSI3": ("rsi", "rsi3")},
    "BOLL": {"BOLL": ("boll", "mid"), "MID": ("boll", "mid"), "UB": ("boll", "upper"), "UPPER": ("boll", "upper"),
             "LB": ("boll", "lower"), "LOWER": ("boll", "lower")},
    "DMI": {"PDI": ("dmi", "pdi"), "MDI": ("dmi", "mdi"), "ADX": ("dmi", "adx"), "ADXR": ("dmi", "adxr")},
    "CCI": {"CCI": ("cci", "cci")},
    "OBV": {"OBV": ("obv", "obv"), "MAOBV": ("obv", "maobv")},
    "BIAS": {"BIAS1": ("bias", "bias1"), "BIAS2": ("bias", "bias2"), "BIAS3": ("bias", "bias3")},
    "WR": {"WR1": ("wr", "wr1"), "WR2": ("wr", "wr2")},
    "ATR": {"ATR": ("atr", "atr")},
    "VR": {"VR": ("vr", "vr"), "MAVR": ("vr", "mavr")},
    "MFI": {"MFI": ("mfi", "mfi")},
    "TRIX": {"TRIX": ("trix", "trix"), "MATRIX": ("trix", "matrix")},
    "DMA": {"DIF": ("dma", "dif"), "DIFMA": ("dma", "difma")},
    "PSY": {"PSY": ("psy", "psy"), "PSYMA": ("psy", "psyma")},
    "BRAR": {"BR": ("brar", "br"), "AR": ("brar", "ar")},
    "CR": {"CR": ("cr", "cr")},
    "EXPMA": {"EXP1": ("expma", "exp1"), "EXP2": ("expma", "exp2")},
    "SAR": {"SAR": ("sar", "sar")},
}

_STYLE_WORDS = re.compile(
    r"^(COLOR\w*|RGBX?\w*|LINETHICK\d*|DOTLINE|DASHLINE|STICK|COLORSTICK|VOLSTICK|LINESTICK|CROSSDOT|CIRCLEDOT|"
    r"POINTDOT|NODRAW|NOTEXT|NOFRAME|NOTITLE|LAYER\d*|ALIGN\d*|VALIGN\d*|MOVE\w*|DRAWABOVE|NOKEY|NOSHOW)$", re.I)
_NAME = r"[A-Za-z_一-鿿][A-Za-z0-9_一-鿿]*"
_ASSIGN_RE = re.compile(rf"^\s*({_NAME})\s*:=\s*(.+)$", re.S)
_OUTPUT_RE = re.compile(rf"^\s*({_NAME})\s*:(?!=)\s*(.+)$", re.S)
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_CMPOPS = (ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq)


@dataclass
class Statement:
    kind: str               # "var"（:=）/ "out"（NAME:）/ "expr"（无名）
    name: str | None
    node: ast.expr
    text: str               # 原始语句（报错和显示用）
    index: int              # 第几句（从 1 开始）


@dataclass
class Program:
    statements: list[Statement]
    outputs: list[str] = field(default_factory=list)       # 输出线名称（按出现顺序）
    condition: str | None = None                           # 选股条件用哪一句：输出名，或 "#<index>"
    uses_chips: bool = False
    indicators: set[str] = field(default_factory=set)      # 用到的 ta 指标 id
    lookback: int = 60                                     # 估计需要多少天历史数据才能算准最后一天


def _strip_comments(text: str) -> str:
    text = re.sub(r"\{[^{}]*\}", " ", text)
    return re.sub(r"//[^\n]*", " ", text)


def _split_statements(text: str) -> list[str]:
    return [s.strip() for s in text.split(";") if s.strip()]


def _strip_style(expr: str) -> str:
    """去掉行尾的画线属性：MA(C,5),COLORRED,LINETHICK2 → MA(C,5)（只去掉括号外、属于样式词的逗号段）"""
    depth: int = 0
    cut: list[int] = []
    for i, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            cut.append(i)
    if not cut:
        return expr
    parts: list[str] = []
    last: int = 0
    for i in cut:
        parts.append(expr[last:i])
        last = i + 1
    parts.append(expr[last:])
    head, tail = parts[0], parts[1:]
    if all(_STYLE_WORDS.match(p.strip()) for p in tail):
        return head
    raise FormulaError(f"「{expr.strip()}」里有多余的逗号（函数参数要写在括号里）")


def _translate(expr: str) -> str:
    """通达信写法 → Python 表达式文本（不执行，只用于 ast.parse）"""
    expr = re.sub(r'"\s*([A-Za-z]+)\s*\.\s*([A-Za-z0-9]+)\s*"', r"\1.\2", expr)       # "MACD.DIF" → MACD.DIF
    if '"' in expr or "'" in expr:
        raise FormulaError("公式里不支持引号字符串（指标引用请写成 MACD.DIF）")
    if "#" in expr:
        raise FormulaError("不支持跨周期引用（如 #WEEK），请直接在对应周期上写公式")
    expr = expr.upper()
    expr = expr.replace("&&", " AND ").replace("||", " OR ")
    expr = expr.replace("<>", "!=")
    expr = re.sub(r"(?<![<>!=:])=(?!=)", "==", expr)
    expr = re.sub(r"\bAND\b", " and ", expr)
    expr = re.sub(r"\bOR\b", " or ", expr)
    expr = re.sub(r"\bNOT\b(?!\s*\()", " not ", expr)      # NOT X；NOT(X) 仍按函数处理
    return expr


def _const_value(node: ast.expr) -> float | None:
    """常数表达式求值（只支持数字和 + - * /），不是常数返回 None"""
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        v = _const_value(node.operand)
        return None if v is None else (-v if isinstance(node.op, ast.USub) else v)
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        a, b = _const_value(node.left), _const_value(node.right)
        if a is None or b is None:
            return None
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        return None if b == 0 else a / b
    return None


def _depth(node: ast.AST, d: int = 0) -> int:
    kids = list(ast.iter_child_nodes(node))
    return d if not kids else max(_depth(k, d + 1) for k in kids)


class _Checker:
    def __init__(self, known_vars: set[str], stmt: str, lookbacks: dict[str, int] | None = None) -> None:
        self.vars = known_vars
        self.stmt = stmt
        self.lookbacks: dict[str, int] = lookbacks or {}
        self.uses_chips = False
        self.indicators: set[str] = set()

    def fail(self, msg: str) -> None:
        raise FormulaError(f"{msg}（在「{self.stmt}」）")

    def check(self, node: ast.AST) -> int:
        """校验节点，返回估计需要的历史天数"""
        if isinstance(node, ast.Expression):
            return self.check(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int | float):
                self.fail("只支持数字常量")
            return 0
        if isinstance(node, ast.Name):
            n = node.id
            if n in self.vars:
                return self.lookbacks.get(n, 0)          # 变量本身需要的历史天数
            if n in DATA_FIELDS:
                return 0
            if n in FUTURE_FUNCS:
                self.fail(f"不能使用未来函数 {n}：{FUTURE_FUNCS[n]}，这种公式回测看起来很准，实际根本做不到")
            if n in FUNCS:
                self.fail(f"{n} 是函数，要写成 {n}(…)")
            if n in INDICATOR_REFS:
                self.fail(f"引用指标要写明是哪条线，例如 {n}.{next(iter(INDICATOR_REFS[n]))}")
            self.fail(f"不认识「{n}」（拼写错了，或者变量要先定义再使用）")
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or node.value.id not in INDICATOR_REFS:
                self.fail("只支持 指标.线名 这种引用，例如 MACD.DIF、KDJ.J")
            fields = INDICATOR_REFS[node.value.id]
            if node.attr not in fields:
                self.fail(f"{node.value.id} 没有「{node.attr}」这条线，可用：{'、'.join(fields)}")
            self.indicators.add(fields[node.attr][0])
            return 120
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, ast.USub | ast.UAdd | ast.Not):
                self.fail("不支持这种运算")
            return self.check(node.operand)
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                self.fail("只支持 + - * / 四则运算（乘方请用 POW(X,Y)）")
            return max(self.check(node.left), self.check(node.right))
        if isinstance(node, ast.BoolOp):
            return max(self.check(v) for v in node.values)
        if isinstance(node, ast.Compare):
            if not all(isinstance(op, _ALLOWED_CMPOPS) for op in node.ops):
                self.fail("只支持 > < >= <= = <> 比较")
            return max(self.check(x) for x in [node.left, *node.comparators])
        if isinstance(node, ast.Call):
            return self.check_call(node)
        self.fail("公式里有不支持的写法")
        return 0

    def check_call(self, node: ast.Call) -> int:
        if not isinstance(node.func, ast.Name):
            self.fail("函数名写法不对")
        name: str = node.func.id                                   # type: ignore[union-attr]
        if name in FUTURE_FUNCS:
            self.fail(f"不能使用未来函数 {name}：{FUTURE_FUNCS[name]}，这种公式回测看起来很准，实际根本做不到")
        if name not in FUNCS:
            self.fail(f"不支持函数 {name}（可以在“公式说明”里查看支持的函数）")
        if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
            self.fail(f"{name} 的参数写法不对")
        lo, hi, consts, help_text = FUNCS[name]
        if not lo <= len(node.args) <= hi:
            self.fail(f"{name} 的参数个数不对：{help_text}")
        const_vals: dict[int, float] = {}
        for i in consts:
            v = _const_value(node.args[i])
            if v is None:
                self.fail(f"{name} 的第 {i + 1} 个参数必须是固定的数字：{help_text}")
            const_vals[i] = v                                      # type: ignore[assignment]
        if name == "WINNER":
            arg = node.args[0]
            if not (isinstance(arg, ast.Name) and DATA_FIELDS.get(arg.id) == "close"):
                self.fail("WINNER 目前只支持 WINNER(C)（按收盘价计算获利比例）")
            self.uses_chips = True
            return 250
        if name == "COST":
            q = const_vals[0]
            if not 1 <= q <= 99:
                self.fail("COST(Q) 的 Q 要在 1 到 99 之间")
            self.uses_chips = True
            return 250
        if name == "SMA" and const_vals[2] > const_vals[1]:
            self.fail("SMA(X,N,M) 要求 M 不大于 N")
        for i, v in const_vals.items():
            if name != "COST" and (v < 0 or v != int(v) or v > 2000):
                self.fail(f"{name} 的第 {i + 1} 个参数要是 0~2000 之间的整数")
        inner: int = max((self.check(a) for i, a in enumerate(node.args) if i not in consts), default=0)
        n: int = int(const_vals.get(1, 0))
        if name in ("EMA", "EXPMA", "SMA"):
            return inner + 4 * max(n, 1)
        if name in ("BARSLAST", "BARSCOUNT", "DMA") or (name == "REF" and _const_value(node.args[1]) is None):
            return inner + 250
        if name == "REF":
            return inner + int(_const_value(node.args[1]) or 0)
        if name in ("HHV", "LLV", "SUM", "COUNT") and n == 0:
            return inner + 250
        return inner + n


def compile_formula(text: str) -> Program:
    """解析并校验公式；有问题抛 FormulaError（中文）"""
    if not text or not text.strip():
        raise FormulaError("公式是空的")
    if len(text) > MAX_TEXT:
        raise FormulaError(f"公式太长了（最多 {MAX_TEXT} 个字符）")
    raw_statements: list[str] = _split_statements(_strip_comments(text))
    if not raw_statements:
        raise FormulaError("公式里没有有效的语句")
    if len(raw_statements) > MAX_STATEMENTS:
        raise FormulaError(f"语句太多了（最多 {MAX_STATEMENTS} 句）")
    known: set[str] = set()
    lookback_of: dict[str, int] = {}
    stmts: list[Statement] = []
    outputs: list[str] = []
    uses_chips: bool = False
    indicators: set[str] = set()
    max_look: int = 0
    for idx, s in enumerate(raw_statements, start=1):
        m = _ASSIGN_RE.match(s)
        kind, name, body = ("var", m.group(1), m.group(2)) if m else ("expr", None, s)
        if not m:
            m2 = _OUTPUT_RE.match(s)
            if m2:
                kind, name, body = "out", m2.group(1), m2.group(2)
        if name is not None:
            name = name.upper()
            if name in DATA_FIELDS or name in FUNCS or name in FUTURE_FUNCS or name in INDICATOR_REFS:
                raise FormulaError(f"「{name}」是系统保留的名字，不能用作变量名（在第 {idx} 句）")
        body = _strip_style(body)
        py: str = _translate(body)
        try:
            node: ast.Expression = ast.parse(py.strip(), mode="eval")
        except SyntaxError:
            raise FormulaError(f"第 {idx} 句语法不对：「{s}」（检查括号是否配对、是否少了运算符）") from None
        count: int = sum(1 for _ in ast.walk(node))
        if count > MAX_NODES or _depth(node) > MAX_DEPTH:
            raise FormulaError(f"第 {idx} 句太复杂了，请拆成几句中间变量")
        chk = _Checker(known, s, lookback_of)
        look: int = chk.check(node)
        uses_chips |= chk.uses_chips
        indicators |= chk.indicators
        max_look = max(max_look, look)
        if name is not None:
            known.add(name)
            lookback_of[name] = look
        if kind == "out":
            if name in outputs:
                raise FormulaError(f"输出线「{name}」重复了（在第 {idx} 句）")
            outputs.append(name)                                  # type: ignore[arg-type]
        stmts.append(Statement(kind, name, node.body, s, idx))
    cond: str | None = None
    if "XG" in outputs:
        cond = "XG"
    else:
        last = stmts[-1]
        cond = last.name if last.kind == "out" else (f"#{last.index}" if last.kind == "expr" else None)
    return Program(stmts, outputs, cond, uses_chips, indicators, min(max(max_look + 20, 60), 1500))


def reference() -> dict:
    """给"公式说明"页面用：支持的数据、函数、指标引用、禁止的未来函数"""
    return {
        "data": DATA_HELP,
        "funcs": {k: v[3] for k, v in FUNCS.items()},
        "indicators": {k: list(v) for k, v in INDICATOR_REFS.items()},
        "future": FUTURE_FUNCS,
    }
