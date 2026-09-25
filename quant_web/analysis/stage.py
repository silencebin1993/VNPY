"""
主力阶段识别：吸筹 / 洗盘 / 拉升 / 出货 / 下跌 / 不明确。

- 每个阶段有一组"证据"（量价、均线、筹码的经验规则），每条有权重；满足的证据权重之和 ÷ 有数据的证据权重之和 = 该阶段得分；
  缺数据的证据（比如没有筹码数据）不计入分母，也不算满足；
- 每个阶段还有"前提"（吸筹必须在低位、出货必须在高位、拉升必须在均线上方……），不满足前提的阶段不参与；
- 得分最高且不低于 THRESHOLD 的阶段胜出；分数相同时按 PRIORITY 先报风险（出货、下跌）；都不够时为"不明确"；
- 这些规则是事先定好的经验规则，没有用历史数据调参；是否有用以 stage_stats 的历史验证为准（样本外 + 和同日随机比）。
- 资金流（只有实时数据）和股东户数只作为"参考信息"展示，不参与打分，保证历史验证和实时判断用的是同一套规则。
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl


THRESHOLD: float = 0.6
PRIORITY: list[str] = ["distribution", "decline", "markup", "washout", "accumulation"]

STAGES: dict[str, dict] = {
    "accumulation": {"label": "吸筹", "tone": "watch",
                     "desc": "主力可能在低位悄悄买入：股价低位横盘、波动变小、上涨放量下跌缩量。",
                     "advice": "可以放进观察名单，等放量突破时再考虑买；吸筹可能持续很久，不要急着进场。"},
    "washout": {"label": "洗盘", "tone": "watch",
                "desc": "涨了一段后缩量回调、关键均线不破，像是在把不坚定的人吓出去。",
                "advice": "持有的可以继续拿，但要设好止损（放量跌破 20 日线就走）；没持有的等缩量企稳、再次放量时再考虑。"},
    "markup": {"label": "拉升", "tone": "good",
               "desc": "放量突破、均线多头、量价齐升，趋势向上。",
               "advice": "趋势向上，持有为主，用移动止盈保护利润；不要在大涨当天追高。"},
    "distribution": {"label": "出货", "tone": "bad",
                     "desc": "高位放量滞涨、量价背离或放量破位，像是大资金在卖给后来者。",
                     "advice": "高风险：不要买；持有的建议减仓或把止损提到最近的支撑下方。"},
    "decline": {"label": "下跌", "tone": "bad",
                "desc": "均线空头、低点不断下移、反弹无量，处在下跌趋势中。",
                "advice": "不要抄底，也不要补仓摊平；等股价重新站上 60 日线再说。"},
    "unclear": {"label": "不明确", "tone": "neutral",
                "desc": "各阶段的证据都不充分，看不出明显的主力行为。",
                "advice": "信号不清楚时，不操作也是一种操作。"},
}


def _c(name: str) -> pl.Expr:
    return pl.col(name)


PREREQ: dict[str, pl.Expr] = {
    "accumulation": _c("pos250") < 0.4,
    "washout": (_c("rally60") >= 0.15) & (_c("close") > _c("ma60")) & (_c("dd20") <= -0.03),
    "markup": (_c("close") > _c("ma20")) & (_c("ma20") > _c("ma60")),
    "distribution": (_c("pos250") > 0.6) | (_c("rise_low120") > 0.5),
    "decline": _c("close") < _c("ma60"),
}
PREREQ_TEXT: dict[str, str] = {
    "accumulation": "股价在一年区间的下 40%", "washout": "前期涨过 15% 以上、在 60 日线上方，并且正在回调（离 20 日高点 3% 以上）",
    "markup": "股价在 20 日线上方、20 日线在 60 日线上方", "distribution": "股价处在高位（一年区间上 40% 或较半年低点涨 50% 以上）",
    "decline": "股价在 60 日线下方",
}


@dataclass(frozen=True)
class Evidence:
    key: str
    stage: str
    weight: float
    label: str
    expr: pl.Expr
    needs: str | None = None          # "chips"：需要筹码数据


EVIDENCE: list[Evidence] = [
    # 吸筹
    Evidence("acc_low", "accumulation", 1.0, "股价在一年区间的低位（下 35%）", _c("pos250") < 0.35),
    Evidence("acc_flat", "accumulation", 1.0, "最近 30 天横着走（振幅不到 20%）", _c("range30") < 0.20),
    Evidence("acc_squeeze", "accumulation", 0.5, "波动在收缩（平均波幅比 50 天前小 15% 以上）", _c("atr_ratio50") < 0.85),
    Evidence("acc_upvol", "accumulation", 1.0, "上涨日的成交量明显多于下跌日（阳量 ÷ 阴量 > 1.2）", _c("udr20") > 1.2),
    Evidence("acc_obv", "accumulation", 1.0, "OBV 往上走、股价却没怎么涨（资金可能在悄悄进场）",
             (_c("obv_slope") > 0) & (_c("price_slope").abs() < 0.15)),
    Evidence("acc_nodump", "accumulation", 0.5, "最近 20 天没有放量大跌", _c("big_drop20") == 0),
    Evidence("acc_chips", "accumulation", 1.0, "筹码在集中（集中度比 20 天前明显变好，或已经很集中）",
             (_c("conc_chg20") < -0.1) | (_c("conc90") < 0.15), needs="chips"),
    # 洗盘
    Evidence("wash_rally", "washout", 1.0, "前期有过拉升（最近 60 天的涨幅超过 15%）", _c("rally60") >= 0.15),
    Evidence("wash_shrink", "washout", 1.0, "回调时明显缩量（5 日均量不到 20 日均量的 75%）",
             (_c("mav5") < _c("mav20") * 0.75) & (_c("dd20") < -0.03)),
    Evidence("wash_support", "washout", 1.0, "没有跌破关键均线（收在 20 日线附近或以上）", _c("close") >= _c("ma20") * 0.98),
    Evidence("wash_half", "washout", 1.0, "回撤没有超过前期涨幅的一半", _c("dd20").abs() <= _c("rally60") * 0.5),
    Evidence("wash_noblack", "washout", 1.0, "最近 5 天没有放量长阴", ~_c("black_vol5")),
    Evidence("wash_shadow", "washout", 0.5, "回调中出现长下影线（下方有人承接）", _c("low_shadow5")),
    Evidence("wash_cost", "washout", 0.5, "平均成本没有下移（筹码没有松动）", _c("cost_chg5") >= -0.01, needs="chips"),
    # 拉升
    Evidence("up_break", "markup", 1.0, "20 天内出现过放量突破 60 日高点", _c("breakout20")),
    Evidence("up_newhigh", "markup", 1.0, "最近 5 天收盘创过 60 日新高（趋势在延续）", _c("newhigh5")),
    Evidence("up_bull", "markup", 1.0, "均线多头排列，20 日线向上", _c("ma_bull")),
    Evidence("up_volprice", "markup", 1.0, "量价齐升（上涨日平均量是下跌日的 1.3 倍以上）", _c("udr10_avg") > 1.3),
    Evidence("up_rps", "markup", 1.0, "相对强度高（半年涨幅超过全市场 80% 的股票）", _c("rps120") > 80),
    Evidence("up_nostall", "markup", 0.5, "没有出现高位放量滞涨", ~_c("stall10")),
    Evidence("up_notover", "markup", 0.5, "没有涨得过热（离 20 日线不到 15%）", _c("bias20") < 0.15),
    # 出货
    Evidence("dist_high", "distribution", 1.0, "最近到过高位（20 天内到过一年区间的上 15%，或从半年低点涨了 50% 以上）",
             (_c("pos_max20") > 0.85) | (_c("rise_low120") > 0.5)),
    Evidence("dist_stall", "distribution", 1.0, "15 天内出现高位放量滞涨、长上影线", _c("stall15")),
    Evidence("dist_div", "distribution", 1.0, "价格创新高，但成交量和 MACD 没跟上（顶背离）", _c("div10")),
    Evidence("dist_break", "distribution", 1.0, "10 天内带量跌破 20 日线", _c("brk10")),
    Evidence("dist_top", "distribution", 0.5, "10 天内出现半年来的天量阴线", _c("top10")),
    Evidence("dist_chips", "distribution", 0.5, "筹码在向上转移、变分散（平均成本明显上移，集中度变差）",
             (_c("conc_chg20") > 0.3) & (_c("cost_chg20") > 0.05), needs="chips"),
    # 下跌
    Evidence("down_ma60", "decline", 1.0, "股价在 60 日线下方，而且 60 日线在往下", (_c("close") < _c("ma60")) & (_c("ma60_slope5") < 0)),
    Evidence("down_bear", "decline", 1.0, "均线空头排列", _c("bear_ma")),
    Evidence("down_lows", "decline", 1.0, "低点不断下移", _c("lower_lows")),
    Evidence("down_weak", "decline", 1.0, "反弹没有量（阳量不到阴量的 80%）", _c("udr20") < 0.8),
]


def classify(feat: pl.DataFrame, keep_evidence: bool = False) -> pl.DataFrame:
    """在特征表上逐行判断阶段：加 stage、stage_score、score_<阶段> 列（keep_evidence=True 时保留 ev_<证据> 列）"""
    df: pl.DataFrame = feat.with_columns([e.expr.alias(f"ev_{e.key}") for e in EVIDENCE])
    score_exprs: list[pl.Expr] = []
    for st in STAGES:
        evs: list[Evidence] = [e for e in EVIDENCE if e.stage == st]
        if not evs:
            continue
        total: float = sum(e.weight for e in evs)
        num = pl.sum_horizontal([pl.col(f"ev_{e.key}").fill_null(False).cast(pl.Float64) * e.weight for e in evs])
        den = pl.sum_horizontal([pl.col(f"ev_{e.key}").is_not_null().cast(pl.Float64) * e.weight for e in evs])
        # 有数据的证据不到总权重的一半：信息太少，不打分
        score_exprs.append(pl.when(den >= total / 2).then(num / den).otherwise(None).alias(f"score_{st}"))
    df = df.with_columns(score_exprs)
    masked: dict[str, pl.Expr] = {
        st: pl.when(PREREQ[st].fill_null(False) & (pl.col(f"score_{st}") >= THRESHOLD)).then(pl.col(f"score_{st}")).otherwise(-1.0)
        for st in PRIORITY
    }
    df = df.with_columns([m.alias(f"_m_{st}") for st, m in masked.items()])
    best = pl.max_horizontal([pl.col(f"_m_{st}") for st in PRIORITY])
    chain = None
    for st in PRIORITY:
        cond = (pl.col(f"_m_{st}") >= THRESHOLD) & (pl.col(f"_m_{st}") >= best - 1e-9)
        chain = pl.when(cond).then(pl.lit(st)) if chain is None else chain.when(cond).then(pl.lit(st))
    df = df.with_columns(chain.otherwise(pl.lit("unclear")).alias("stage"),                    # type: ignore[union-attr]
                         pl.when(best >= THRESHOLD).then(best).otherwise(None).alias("stage_score"))
    drop: list[str] = [f"_m_{st}" for st in PRIORITY]
    if keep_evidence:
        df = df.with_columns([PREREQ[st].fill_null(False).alias(f"pre_{st}") for st in PREREQ])
    else:
        drop += [f"ev_{e.key}" for e in EVIDENCE]
    return df.drop(drop)


def confidence(score: float | None) -> str:
    if score is None:
        return "—"
    return "高" if score >= 0.85 else "中" if score >= 0.7 else "低"


def explain_row(row: dict) -> dict:
    """一行（含 ev_*/score_*/stage）→ 页面要的结构：当前阶段、各阶段得分、每个阶段的证据逐条打勾"""
    stage: str = row.get("stage") or "unclear"
    info: dict = STAGES[stage]
    scores: dict = {st: row.get(f"score_{st}") for st in STAGES if st != "unclear"}
    evidence: dict[str, list[dict]] = {}
    for e in EVIDENCE:
        v = row.get(f"ev_{e.key}")
        evidence.setdefault(e.stage, []).append({
            "key": e.key, "label": e.label, "weight": e.weight, "ok": None if v is None else bool(v),
            "needs": e.needs, "missing": v is None,
        })
    prereq: dict = {st: row.get(f"pre_{st}") for st in PREREQ}
    return {
        "key": stage, "label": info["label"], "tone": info["tone"], "desc": info["desc"], "advice": info["advice"],
        "score": row.get("stage_score"), "confidence": confidence(row.get("stage_score")),
        "scores": scores, "evidence": evidence, "prereq_text": PREREQ_TEXT, "prereq": prereq,
        "threshold": THRESHOLD,
    }
