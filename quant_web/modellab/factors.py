"""
因子组注册表：每组因子的中文说明、计算成本、来源和计算函数。
- frame：在公式表（前复权日线）上按股票分块计算：基础量价、技术指标、Alpha158（实现在 factors_builtin.py）；
- cross：需要同一天全部股票的截面（Alpha101），按日期分块、带回看期；
- table：直接取选股器缓存的全历史字段表：主力行为、基本面与市值；
- formula：你在公式库里保存的公式（选股条件 0/1 + 输出线，用到筹码函数的公式跳过）。
所有因子只用当天及以前的数据；基本面按财报公布日对齐。
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import polars as pl

FACTOR_SETS: dict[str, dict[str, Any]] = {
    "basic": {"label": "基础量价", "short": "量价", "source": "frame", "cost": 1.0, "min_per_m": 0.03,
              "desc": "涨跌幅（动量/反转）、波动、换手、成交额、均线偏离、离高低点的距离等 30 多个"},
    "ta": {"label": "技术指标", "short": "指标", "source": "frame", "cost": 1.5, "min_per_m": 0.03,
           "desc": "MACD、KDJ、RSI、布林、DMI、OBV 等常用指标（价格类的已按股价标准化，不同股票可以比较）"},
    "mainforce": {"label": "主力行为", "short": "主力", "source": "table", "cost": 0.2, "min_per_m": 0.02,
                  "desc": "主力阶段打分（吸筹/洗盘/拉升/出货/下跌）、相对强度 RPS、量能收缩、OBV 斜率、年内位置等"},
    "fundamental": {"label": "基本面与市值", "short": "基本面", "source": "table", "cost": 0.2, "min_per_m": 0.05,
                    "desc": "盈利收益率（市盈率倒数）、净资产收益率、利润和营收增长、负债率、市值（按财报公布日对齐，不偷看未来）"},
    "alpha158": {"label": "Alpha158（vnpy/qlib 常用）", "short": "A158", "source": "frame", "cost": 6.0, "min_per_m": 0.6,
                 "desc": "158 个时间序列量价因子，机构常用的基础因子库。因子多、占内存多（全主板约 3 分钟）"},
    "alpha101": {"label": "Alpha101（WorldQuant 公开）", "short": "A101", "source": "cross", "cost": 8.0, "min_per_m": 2.0,
                 "desc": "WorldQuant 公开的 101 个量价因子（vnpy 里能用的 82 个），很多要做截面排名，计算最慢（全主板约 10 分钟）"},
    "formula": {"label": "我的公式", "short": "公式", "source": "formula", "cost": 2.0, "min_per_m": 0.3,
                "desc": "你在公式库里保存的公式：选股条件（满足=1）和输出线。用到筹码函数的公式会跳过（太慢）"},
}

# 主力行为：选股器全历史字段表里的列 → 因子名（m_ 前缀）
MAINFORCE_COLS: list[str] = ["rps120", "pos250", "bias20", "vr20", "udr20", "obv_slope", "atr_pct", "rally60", "vol_shrink",
                             "dist_ma20", "ma_bull", "turn20", "rev20", "stage_score"]
STAGES: list[str] = ["accumulation", "washout", "markup", "distribution", "decline"]
MAINFORCE_LABELS: dict[str, str] = {
    "rps120": "120 日相对强度", "pos250": "一年区间里的位置", "bias20": "20 日乖离", "vr20": "20 日量比（涨/跌日成交量）",
    "udr20": "20 日上涨天数占比", "obv_slope": "OBV 斜率", "atr_pct": "平均波幅占股价", "rally60": "60 日最大涨幅",
    "vol_shrink": "量能收缩", "dist_ma20": "离 20 日线的距离", "ma_bull": "均线多头排列", "turn20": "20 日平均换手",
    "rev20": "20 日反转", "stage_score": "主力阶段置信度",
    **{f"score_{s}": f"{lab}阶段打分" for s, lab in zip(STAGES, ["吸筹", "洗盘", "拉升", "出货", "下跌"], strict=True)},
    **{f"is_{s}": f"当前阶段是{lab}" for s, lab in zip(STAGES, ["吸筹", "洗盘", "拉升", "出货", "下跌"], strict=True)},
}
FUND_LABELS: dict[str, str] = {
    "f_ep": "盈利收益率（市盈率倒数）", "f_bp": "净资产收益率（市净率倒数）", "f_roe": "ROE", "f_profit_yoy": "净利润同比增长",
    "f_revenue_yoy": "营收同比增长", "f_debt_ratio": "资产负债率", "f_log_cap": "流通市值（对数）",
}


def _mainforce_names() -> list[str]:
    return ([f"m_{c}" for c in MAINFORCE_COLS] + [f"m_score_{s}" for s in STAGES] + [f"m_is_{s}" for s in STAGES])


def _formula_items() -> list[dict]:
    """可以当因子用的“我的公式”（不含筹码函数）：[{id, name, prog, outputs}]"""
    from ..formula import store
    from ..formula.parser import compile_formula

    out: list[dict] = []
    for item in store.list_mine():
        try:
            prog = compile_formula(item["text"])
        except Exception:  # noqa: BLE001  编译不过的公式跳过
            continue
        if prog.uses_chips:
            continue
        out.append({"id": str(item["id"]), "name": item.get("name") or str(item["id"]), "prog": prog})
    return out


def _formula_names(items: list[dict] | None = None) -> list[str]:
    names: list[str] = []
    for it in items if items is not None else _formula_items():
        names.append(f"u_{it['id']}_xg")
        outs = [o for o in getattr(it["prog"], "outputs", [])][:3]
        names += [f"u_{it['id']}_{_safe(o)}" for o in outs]
    return names


def _safe(name: Any) -> str:
    text: str = str(getattr(name, "name", name))
    return "".join(ch if ch.isalnum() else "_" for ch in text)[:24] or "out"


def feature_names(set_key: str) -> list[str]:
    """某个因子组的因子名（不计算，用于估算和页面显示）"""
    if set_key == "mainforce":
        return _mainforce_names()
    if set_key == "fundamental":
        return list(FUND_LABELS)
    if set_key == "formula":
        return _formula_names()
    from . import factors_builtin
    return factors_builtin.feature_names(set_key)


def feature_label(name: str) -> str:
    """因子的中文名（页面上显示重要性时用）"""
    if name.startswith("m_"):
        return MAINFORCE_LABELS.get(name[2:], name)
    if name in FUND_LABELS:
        return FUND_LABELS[name]
    if name.startswith("u_"):
        return "我的公式 " + name[2:]
    try:
        from . import factors_builtin
        info = factors_builtin.FEATURE_INFO.get(name)
        return info["label"] if info else name
    except Exception:  # noqa: BLE001
        return name


def set_of(name: str) -> str:
    return {"b": "basic", "t": "ta", "m": "mainforce", "f": "fundamental", "u": "formula"}.get(name.split("_", 1)[0],
                                                                                            "alpha158" if name.startswith("a158_") else "alpha101")


# ---------------------------------------------------------------- 计算

def table_features(table: pl.DataFrame, sets: list[str]) -> pl.DataFrame:
    """主力行为 / 基本面：从全历史字段表取列（table 已按股票范围过滤）。返回 code, date, 因子..."""
    cols: list[pl.Expr] = []
    if "mainforce" in sets:
        for c in MAINFORCE_COLS:
            cols.append((pl.col(c).cast(pl.Float32) if c in table.columns else pl.lit(None, dtype=pl.Float32)).alias(f"m_{c}"))
        for s in STAGES:
            c = f"score_{s}"
            cols.append((pl.col(c).cast(pl.Float32) if c in table.columns else pl.lit(None, dtype=pl.Float32)).alias(f"m_score_{s}"))
            cols.append(((pl.col("stage") == s).cast(pl.Float32) if "stage" in table.columns else pl.lit(None, dtype=pl.Float32))
                        .alias(f"m_is_{s}"))
    out: pl.DataFrame = table.select(["code", "date", *cols])
    if "fundamental" in sets:
        out = out.join(fundamental_features(table), on=["code", "date"], how="left")
    return out


def fundamental_features(table: pl.DataFrame) -> pl.DataFrame:
    """基本面：按财报公布日（avail_date）对齐到每个交易日；盈利收益率 = 每股收益 ÷ 股价（亏损为负，比市盈率好用）"""
    from ..screener.backtest import _fund_asof          # 和选股器回测同一套对齐口径

    try:
        from ..market import fundamentals
        fund: pl.DataFrame | None = fundamentals.load_fundamentals()
    except Exception:  # noqa: BLE001  没有财报数据
        fund = None
    base: pl.DataFrame = table.select(["code", "date", "raw_close", *([c] if (c := "float_cap") in table.columns else [])])
    df: pl.DataFrame = _fund_asof(base, fund)
    px = pl.col("raw_close")
    ep = (pl.col("eps_ttm") / px) if "eps_ttm" in df.columns else (1 / pl.col("pe_ttm"))
    bp = (pl.col("bvps") / px) if "bvps" in df.columns else (1 / pl.col("pb"))
    cap = pl.col("float_cap") if "float_cap" in df.columns else pl.lit(None, dtype=pl.Float64)
    num = lambda c: pl.col(c).cast(pl.Float64) if c in df.columns else pl.lit(None, dtype=pl.Float64)       # noqa: E731
    return df.select(
        "code", "date",
        ep.cast(pl.Float32).alias("f_ep"), bp.cast(pl.Float32).alias("f_bp"), num("roe").cast(pl.Float32).alias("f_roe"),
        num("profit_yoy").cast(pl.Float32).alias("f_profit_yoy"), num("revenue_yoy").cast(pl.Float32).alias("f_revenue_yoy"),
        num("debt_ratio").cast(pl.Float32).alias("f_debt_ratio"),
        pl.when(cap > 0).then(cap.log()).otherwise(None).cast(pl.Float32).alias("f_log_cap"),
    ).with_columns([pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in FUND_LABELS])


def formula_features(frame: pl.DataFrame, items: list[dict] | None = None) -> pl.DataFrame:
    """我的公式：每个公式的选股条件（满足=1，不满足=0）+ 前 3 条输出线（和收盘价的比值，价格类才可比）"""
    from ..formula import engine

    items = items if items is not None else _formula_items()
    cols: dict[str, pl.Series] = {}
    close: pl.Series = frame["close"].cast(pl.Float64)
    for it in items:
        try:
            res = engine.evaluate(it["prog"], frame)
        except Exception:  # noqa: BLE001  某个公式算不出来就跳过（因子为空）
            res = None
        xg = f"u_{it['id']}_xg"
        cols[xg] = (res.condition.cast(pl.Float32) if res is not None and res.condition is not None
                    else pl.Series(xg, [None] * frame.height, dtype=pl.Float32))
        outs = list(getattr(it["prog"], "outputs", []))[:3]
        for o in outs:
            name = f"u_{it['id']}_{_safe(o)}"
            s = res.outputs.get(getattr(o, "name", o)) if res is not None else None
            if s is None:
                cols[name] = pl.Series(name, [None] * frame.height, dtype=pl.Float32)
                continue
            s = s.cast(pl.Float64)
            med = s.drop_nulls().abs().median()
            cmed = close.drop_nulls().median()
            # 数值量级和股价接近的（均线、布林等价格线）除以收盘价，其余（指标、比例）原样
            if med is not None and cmed and 0.2 < float(med) / float(cmed) < 5:
                s = s / close
            cols[name] = s.cast(pl.Float32)
    out = frame.select("code", "date")
    if cols:
        out = out.with_columns([s.alias(k) for k, s in cols.items()])
    return out


def frame_builder(set_key: str) -> Callable[[pl.DataFrame], pl.DataFrame]:
    from . import factors_builtin
    return {"basic": factors_builtin.basic_features, "ta": factors_builtin.ta_features,
            "alpha158": factors_builtin.alpha158_features}[set_key]


def cross_builder(set_key: str) -> Callable[..., pl.DataFrame]:
    from . import factors_builtin
    return {"alpha101": factors_builtin.alpha101_features}[set_key]


def cost_of(sets: list[str]) -> float:
    return float(sum(FACTOR_SETS[s]["cost"] for s in sets))


def minutes_per_million(sets: list[str]) -> float:
    """算因子的大概耗时（分钟 / 百万行，本机实测）"""
    return float(sum(FACTOR_SETS[s].get("min_per_m", 0.5) for s in sets))


def count_of(sets: list[str]) -> int:
    n: int = 0
    for s in sets:
        try:
            n += len(feature_names(s))
        except Exception:  # noqa: BLE001  因子库加载失败时按经验值估
            n += {"basic": 36, "ta": 40, "alpha158": 158, "alpha101": 80, "formula": 8}.get(s, 20)
    return n


def listing() -> list[dict]:
    """给页面的因子组清单（含因子数量）"""
    out: list[dict] = []
    for k, v in FACTOR_SETS.items():
        try:
            n: int = len(feature_names(k))
            err: str = ""
        except Exception as e:  # noqa: BLE001
            n, err = 0, str(e)
        out.append({"key": k, "label": v["label"], "desc": v["desc"], "cost": v["cost"], "n": n,
                    "available": n > 0, "reason": err or ("你还没有保存可用的公式" if k == "formula" and n == 0 else "")})
    return out


def finite(x: float | None) -> float | None:
    return x if x is not None and math.isfinite(x) else None
