"""
按用户权重合成综合分、五维评分（0~100，50 为中性）、候选过滤、白话理由。

分类模型（连板/首板）：score_logit = bias + Σ_g weights[g]·contrib_g + news_weight·clip(news_z, -2, 2)；
score = sigmoid(score_logit)。权重全为 1、没有消息面时 score 就是模型原始概率。没有贡献分解的行
（首板样本外预测只给每天前 N 名算了贡献）用原始概率的对数几率代替。
回归模型（强势股波段，预测"扣费后净收益"）：score = bias + Σ_g weights[g]·contrib_g
+ news_weight·clip(news_z, -2, 2)·NEWS_RET_UNIT（小数收益，与门槛同单位）；没有贡献分解时用 pred。
消息热度只有实时数据，≠利好，默认权重 0（不参与排序）。
"""
import math
from collections.abc import Callable
from typing import Any

import numpy as np
import polars as pl

from .features import FEATURE_LABELS, GROUPS


LOGIT_CLIP: float = 1e-6
NEWS_CLIP: float = 2.0              # 消息面 z 分数限幅
NEWS_RET_UNIT: float = 0.005        # 回归模型：news_z 每 1 个单位折合的预期收益（0.5%）
RET_DIM_SCALE: float = 0.01         # 回归模型：贡献 1%（收益）≈ 对数几率 1 的维度分变化
REGRESSION_KINDS: set[str] = {"swing"}


def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


def is_regression(df: pl.DataFrame, kind: str | None = None) -> bool:
    """回归模型（波段，预测收益）的结果？kind 给定时按 kind 判断，否则看有没有 pred 列"""
    if kind:
        return kind in REGRESSION_KINDS
    return "pred" in df.columns


def apply_weights(
    pred: pl.DataFrame,
    weights: dict[str, float] | None,
    news_score: pl.DataFrame | None = None,
    news_weight: float = 0.0,
    regression: bool | None = None,
) -> pl.DataFrame:
    """增加 score_logit, score, dim_<group>, dim_news（有消息面时另加 news_count, policy_count, news_z）。
    regression（None 时按有没有 pred 列判断）：波段模型，score 为预测净收益（小数）"""
    if regression is None:
        regression = is_regression(pred)
    w: dict[str, float] = {g: float((weights or {}).get(g, 1.0)) for g in GROUPS}
    df: pl.DataFrame = pred
    for g in GROUPS:
        if f"contrib_{g}" not in df.columns:
            df = df.with_columns(pl.lit(None, pl.Float64).alias(f"contrib_{g}"))
    if "bias" not in df.columns:
        df = df.with_columns(pl.lit(None, pl.Float64).alias("bias"))
    drop: list[str] = [c for c in ("news_count", "policy_count", "news_z") if c in df.columns]
    if drop:
        df = df.drop(drop)
    if news_score is not None and not news_score.is_empty() and "code" in news_score.columns:
        ns: pl.DataFrame = news_score.select(
            pl.col("code").cast(pl.Utf8),
            *[(pl.col(c).cast(pl.Float64) if c in news_score.columns else pl.lit(None, pl.Float64)).alias(c)
              for c in ("news_count", "policy_count", "news_z")],
        ).unique("code")
        df = df.join(ns, on="code", how="left", maintain_order="left")
        has_news: bool = True
    else:
        df = df.with_columns(pl.lit(None, pl.Float64).alias("news_z"))
        has_news = False
    has_contrib = pl.col("bias").is_not_null() & pl.all_horizontal(
        [pl.col(f"contrib_{g}").is_not_null() for g in GROUPS]
    )
    weighted = pl.col("bias") + pl.sum_horizontal([pl.col(f"contrib_{g}") * w[g] for g in GROUPS])
    news_z = pl.col("news_z").fill_null(0.0).fill_nan(0.0)
    news_term = news_z.clip(-NEWS_CLIP, NEWS_CLIP) * float(news_weight)
    if regression:
        if "pred" not in df.columns:
            df = df.with_columns(pl.lit(None, pl.Float64).alias("pred"))
        df = df.with_columns(
            (pl.when(has_contrib).then(weighted).otherwise(pl.col("pred")) + news_term * NEWS_RET_UNIT)
            .alias("score"),
            *[(50 + 50 * (pl.col(f"contrib_{g}").fill_null(0.0) / RET_DIM_SCALE).tanh()).alias(f"dim_{g}")
              for g in GROUPS],
            (50 + 50 * (news_z / 2).tanh()).alias("dim_news"),
        ).with_columns(pl.col("score").alias("score_logit"))
    else:
        p = pl.col("prob").clip(LOGIT_CLIP, 1 - LOGIT_CLIP)
        raw_logit = (p / (1 - p)).log()
        df = df.with_columns(
            (pl.when(has_contrib).then(weighted).otherwise(raw_logit) + news_term).alias("score_logit"),
            *[(50 + 50 * pl.col(f"contrib_{g}").fill_null(0.0).tanh()).alias(f"dim_{g}") for g in GROUPS],
            (50 + 50 * (news_z / 2).tanh()).alias("dim_news"),          # news_z 是 z 分数，除以 2 免得一有消息就满分
        )
        df = df.with_columns((1 / (1 + (-pl.col("score_logit")).exp())).alias("score"))
    if not has_news:
        df = df.drop("news_z")
    return df


def filter_candidates(df: pl.DataFrame, filters: Any) -> pl.DataFrame:
    """按预测设置过滤：排除ST、板块、流通市值(亿)、股价、连板数（仅连板模型）、排除一字板、概率门槛。
    filters 可以是 dict 或 PredictSettings；缺少的数据（如流通市值为空）不因此被排除。"""
    f: dict = _as_dict(filters)
    cond: list[pl.Expr] = []
    if f.get("exclude_st", True) and "is_st" in df.columns:
        cond.append(~pl.col("is_st").fill_null(False))
    boards = f.get("boards")
    if boards and "board" in df.columns:
        cond.append(pl.col("board").is_in(list(boards)))
    if "float_cap" in df.columns:
        cap = pl.col("float_cap")
        if f.get("min_float_cap") is not None:
            cond.append(cap.is_null() | (cap >= float(f["min_float_cap"])))
        if f.get("max_float_cap") is not None:
            cond.append(cap.is_null() | (cap <= float(f["max_float_cap"])))
    if "close" in df.columns:
        if f.get("min_price") is not None:
            cond.append(pl.col("close") >= float(f["min_price"]))
        if f.get("max_price") is not None:
            cond.append(pl.col("close") <= float(f["max_price"]))
    kind: str | None = f.get("kind")
    if kind is None and "streak" in df.columns and df.height:
        kind = "streak" if (df["streak"].max() or 0) > 0 else "first"
    if kind == "streak" and "streak" in df.columns:
        if f.get("streak_min") is not None:
            cond.append(pl.col("streak") >= int(f["streak_min"]))
        if f.get("streak_max") is not None:
            cond.append(pl.col("streak") <= int(f["streak_max"]))
    if f.get("exclude_one_word") and "one_word" in df.columns:
        cond.append(~pl.col("one_word").fill_null(False))
    thr: float = float(f.get("threshold") or 0.0)
    if thr > 0:
        col: str = "score" if "score" in df.columns else "prob"
        cond.append(pl.col(col) >= thr)
    return df.filter(pl.all_horizontal(cond)) if cond else df


# ---------------------------------------------------------------- 理由白话

def _pct(v: float, digits: int = 1) -> str:
    return f"{v * 100:.{digits}f}%"


def _updown(v: float, up: str = "涨", down: str = "跌") -> str:
    return up if v >= 0 else down


def _temp_label(v: float) -> str:
    from ..market.sentiment import temperature_label

    return temperature_label(v)


def _streak(v: float) -> str:
    n: int = int(round(v))
    if n <= 0:
        return "今天没有涨停"
    if n == 1:
        return "今天是首板（第1个涨停）"
    return f"今天是第{n}个涨停（{n}连板）"


def _turnover(v: float) -> str:
    tail: str = ("换手很高，多空分歧大" if v > 25 else "筹码交换充分" if v > 10
                 else "换手适中" if v > 3 else "换手偏低，惜售或关注度低")
    return f"换手率 {v:.1f}%，{tail}"


def _vol_ratio(v: float) -> str:
    tail: str = "明显放量" if v > 2 else "温和放量" if v > 1.2 else "缩量" if v < 0.8 else "量能平稳"
    return f"量比 {v:.1f}，{tail}"


def _float_cap(v: float) -> str:
    cap: float = math.exp(v)
    tail: str = "小盘股，股性活跃" if cap < 50 else "中盘股" if cap < 200 else "大盘股，拉升需要更多资金"
    return f"流通市值约 {cap:.0f} 亿，{tail}"


def _m_limit_up(v: float) -> str:
    if v < 30:
        return f"市场情绪偏冷（涨停仅{v:.0f}家）"
    if v > 80:
        return f"市场情绪火热（涨停{v:.0f}家）"
    return f"市场情绪一般（涨停{v:.0f}家）"


def _ind_lu(v: float) -> str:
    if v >= 5:
        return f"所在行业今天有{v:.0f}只涨停，板块热度高"
    if v >= 2:
        return f"所在行业今天有{v:.0f}只涨停，板块有一定热度"
    return f"所在行业今天涨停{v:.0f}只，板块效应弱"


def _rank_text(v: float, what: str) -> str:
    return f"{what}排在前 {max(1.0, (1 - v) * 100):.0f}%" if v >= 0.5 else f"{what}排在后 {max(1.0, v * 100):.0f}%"


def _dist_high(v: float, n: int) -> str:
    return f"股价处于{n}日最高位" if v >= -0.005 else f"距{n}日最高价还差 {abs(v) * 100:.1f}%"


def _ret_fmt(n: int) -> Callable[[float], str]:
    return lambda v: f"近{n}日{_updown(v)} {abs(v) * 100:.1f}%"


def _bias_fmt(n: int) -> Callable[[float], str]:
    return lambda v: f"{'高于' if v >= 0 else '低于'}{n}日均线 {abs(v) * 100:.1f}%"


def _flag(yes: str, no: str) -> Callable[[float], str]:
    return lambda v: yes if v >= 0.5 else no


_FMT: dict[str, Callable[[float], str]] = {
    "lb_streak": _streak,
    "turnover": _turnover,
    "turn_ma5": lambda v: f"近5日平均换手率 {v:.1f}%",
    "turn_ratio20": lambda v: f"换手率是前20日平均的 {v:.1f} 倍",
    "vol_ratio": _vol_ratio,
    "log_amount": lambda v: f"成交额约 {math.exp(v) / 1e8:.1f} 亿",
    "log_float_cap": _float_cap,
    "word_lu": _flag("今天一字涨停，封板极强（但次日可能买不进）", "今天不是一字板"),
    "t_word": _flag("今天是T字板（开盘涨停、盘中打开又封回）", "今天不是T字板"),
    "close_at_high": _flag("收盘在全天最高价", "收盘没在最高价"),
    "amplitude": lambda v: f"振幅 {_pct(v)}",
    "gap_open": lambda v: f"开盘{_updown(v, '高开', '低开')} {abs(v) * 100:.1f}%",
    "lower_shadow": lambda v: f"下影线 {_pct(v)}" + ("，盘中有资金承接" if v > 0.03 else ""),
    "upper_shadow": lambda v: f"上影线 {_pct(v)}" + ("，上方抛压较重" if v > 0.03 else ""),
    "lhb_on": _flag("今天上了龙虎榜", "今天没上龙虎榜"),
    "lhb_net_ratio": lambda v: f"龙虎榜净{'买入' if v >= 0 else '卖出'}占成交额 {abs(v) * 100:.1f}%",
    "lhb_cnt5": lambda v: f"近5日上龙虎榜 {v:.0f} 次",
    "zt_first_min": lambda v: "开盘即封涨停" if v <= 0 else
    f"开盘后约 {v:.0f} 分钟封板" + ("，封板早" if v < 30 else "，封板偏晚" if v > 180 else ""),
    "zt_open_times": lambda v: "封板后没有打开过" if v < 0.5 else f"盘中炸板 {v:.0f} 次",
    "zt_seal_ratio": lambda v: f"封板资金约占流通市值 {v * 100:.2f}%",
    "m_limit_up": _m_limit_up,
    "m_limit_down": lambda v: f"今天跌停 {v:.0f} 家" + ("，亏钱效应明显" if v > 20 else ""),
    "m_break_rate": lambda v: f"炸板率 {v * 100:.0f}%，"
    + ("封板难度大" if v > 0.4 else "封板较容易" if v < 0.25 else "封板难度一般"),
    "m_max_streak": lambda v: f"市场最高 {v:.0f} 连板" + ("，空间打开" if v >= 5 else "，高度受限" if v <= 3 else ""),
    "m_streak2": lambda v: f"连板股 {v:.0f} 家",
    "m_prev_lu_premium": lambda v: f"昨天涨停的股票今天平均{_updown(v)} {abs(v):.1f}%，"
    + ("接力赚钱效应好" if v > 2 else "接力亏钱" if v < 0 else "接力效应一般"),
    "m_prev_lu_promote": lambda v: f"昨天涨停的股票今天晋级率 {v * 100:.0f}%",
    "m_prev_streak_premium": lambda v: f"昨天连板股今天平均{_updown(v)} {abs(v):.1f}%",
    "m_up_ratio": lambda v: f"今天上涨股票占 {v * 100:.0f}%",
    "m_median_pct": lambda v: f"全市场涨幅中位数 {v:.2f}%",
    "m_amount_ratio20": lambda v: f"两市成交额是20日均值的 {v:.2f} 倍",
    "m_temp": lambda v: f"情绪温度 {v:.0f}（{_temp_label(v)}）",
    "m_temp_chg5": lambda v: f"情绪温度较5天前{'升高' if v >= 0 else '下降'} {abs(v):.0f}",
    "m_lu_chg5": lambda v: f"涨停家数较5天前{'增加' if v >= 0 else '减少'} {abs(v):.0f} 家",
    "lvl_promote": lambda v: f"今天同高度股票晋级率 {v * 100:.0f}%",
    "lu_cnt20": lambda v: f"近20个交易日涨停 {v:.0f} 次",
    "lu_cnt60": lambda v: f"近60个交易日涨停 {v:.0f} 次",
    "brk_cnt20": lambda v: f"近20个交易日炸板 {v:.0f} 次",
    "days_since_lu": lambda v: "近60个交易日没有涨停过" if v >= 61 else f"距上次涨停 {v:.0f} 个交易日",
    "prev_broken": _flag("昨天冲涨停没封住（炸板）", "昨天没有炸板"),
    "pe_ttm": lambda v: f"市盈率 {v:.0f} 倍" + ("，估值偏高" if v > 80 else "，估值较低" if v < 15 else ""),
    "loss": _flag("最近12个月亏损", "最近12个月盈利"),
    "pb": lambda v: f"市净率 {v:.1f} 倍",
    "roe_ann": lambda v: f"净资产收益率（年化）{v:.1f}%",
    "rev_yoy": lambda v: f"营收同比{'增长' if v >= 0 else '下降'} {abs(v):.0f}%",
    "np_yoy": lambda v: f"净利润同比{'增长' if v >= 0 else '下降'} {abs(v):.0f}%",
    "debt_ratio": lambda v: f"资产负债率 {v:.0f}%",
    "gross_margin": lambda v: f"毛利率 {v:.0f}%",
    "st_flag": _flag("ST股（有退市风险警示）", "非ST股"),
    "limit_pct": lambda v: f"涨跌幅限制 {v:.0f}%",
    "log_list_days": lambda v: f"上市约 {math.exp(v):.0f} 个交易日" + ("，次新股" if math.exp(v) < 250 else ""),
    "price": lambda v: f"股价 {v:.2f} 元" + ("，低价股" if v < 5 else "，高价股" if v > 100 else ""),
    "ind_lu": _ind_lu,
    "ind_lu_ratio": lambda v: f"所在行业 {v * 100:.1f}% 的股票涨停",
    "ind_pct": lambda v: f"所在行业今天平均{_updown(v)} {abs(v) * 100:.1f}%",
    "ind_ret5_rank": lambda v: _rank_text(v, "所在行业近5日涨幅在全市场"),
    "ind_rank": lambda v: _rank_text(v, "今天涨幅在行业内"),
    "ind_lu_chg": lambda v: "所在行业涨停家数和昨天持平" if abs(v) < 0.5 else
    f"所在行业涨停家数较昨天{'增加' if v > 0 else '减少'} {abs(v):.0f} 家",
    "ind_max_streak": lambda v: f"所在行业最高 {v:.0f} 连板",
    "chg": lambda v: f"今天{_updown(v)} {abs(v) * 100:.1f}%",
    **{f"ret_{n}": _ret_fmt(n) for n in (3, 5, 10, 20, 60)},
    "dist_high20": lambda v: _dist_high(v, 20),
    "dist_high60": lambda v: _dist_high(v, 60),
    "pos60": lambda v: f"股价处在60日区间的 {v * 100:.0f}% 位置"
    + ("（高位）" if v > 0.8 else "（低位）" if v < 0.2 else ""),
    **{f"bias{n}": _bias_fmt(n) for n in (5, 10, 20, 60)},
    "vol20": lambda v: f"近20日日均波动 {v * 100:.1f}%",
}


def describe(feature: str, value: float | None) -> str:
    """单个特征的白话描述（不含方向箭头）"""
    label: str = FEATURE_LABELS.get(feature, feature)
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return f"{label}：暂无数据"
    fmt: Callable[[float], str] | None = _FMT.get(feature)
    try:
        return fmt(float(value)) if fmt else f"{label} {float(value):.2f}"
    except (ValueError, OverflowError):
        return f"{label} {value}"


def reasons_text(reasons: list[dict] | None, regression: bool = False) -> list[str]:
    """["换手率 18.5%，筹码交换充分 ↑提高概率", "市场情绪偏冷（涨停仅23家） ↓降低概率", ...]；
    regression=True（波段模型）时箭头文字为「↑提高预期收益 / ↓降低预期收益」"""
    out: list[str] = []
    up, down = ("↑提高预期收益", "↓降低预期收益") if regression else ("↑提高概率", "↓降低概率")
    for r in reasons or []:
        if not r:
            continue
        c: float = float(r.get("contrib") or 0.0)
        arrow: str = up if c > 0 else down
        out.append(f"{describe(str(r.get('feature')), r.get('value'))} {arrow}")
    return out
