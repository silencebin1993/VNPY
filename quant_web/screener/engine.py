"""
选股器：在最近一个交易日（或指定日期）上，按方案筛选、排雷、打分、排序，并给出每只股票的建议止损价和股数。

流程：范围（板块/ST/上市天数/成交额/股价）→ 条件（公式、字段门槛、主力阶段；全部满足或任一满足）
→ 排雷（去掉红灯，可选去掉黄灯）→ 打分（百分位加权）→ 前 N 名 → 建议止损与仓位（trading.sizing，
按"我的情况"里的资金和单笔风险）→ 白话理由。

数据：本地日线最近约 420 个日历日（公式需要更长历史时自动加长）；筹码只算最后 30 天的统计（主力阶段的筹码证据用得到）。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import polars as pl

from ..analysis import features, riskscan, stage
from ..formula import engine as fengine
from ..formula import library as flib
from ..formula import validate as fvalidate
from ..formula.parser import compile_formula
from ..trading import sizing
from . import schemes as S

LOOKBACK_DAYS: int = 420
CHIP_DAYS: int = 30
MIN_LISTED: int = 60


def _formula_text(cond: dict) -> str:
    if cond.get("text"):
        return cond["text"]
    fid: str = cond.get("fid") or ""
    if fid.startswith("my_"):
        from ..formula import store
        item = next((x for x in store.list_mine() if x["id"] == fid), None)
        if item is None:
            raise ValueError("方案里用到的“我的公式”已被删除")
        return item["text"]
    return flib.get(fid)["text"]


def formula_lookback(scheme: dict) -> int:
    look: int = 0
    for c in scheme.get("conditions", []):
        if c["type"] == "formula":
            look = max(look, compile_formula(_formula_text(c)).lookback)
    return look


def fundamentals_asof(fund: pl.DataFrame | None, day: date) -> pl.DataFrame:
    """每只股票在 day 当天已经公布的最新一期财报（avail_date ≤ day，避免用到当时还不知道的数据）"""
    if fund is None or fund.is_empty():
        return pl.DataFrame(schema={"code": pl.Utf8})
    f = fund.filter(pl.col("avail_date") <= day) if "avail_date" in fund.columns else fund
    cols = [c for c in ("eps_ttm", "bvps", "roe", "net_profit_yoy", "revenue_yoy", "debt_ratio", "net_profit_ttm") if c in f.columns]
    return f.sort("report_date").group_by("code").last().select(["code", *cols])


def build_frame(last: date, lookback_days: int, raw: pl.DataFrame | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(原始日线, 公式表 + 交易列)"""
    if raw is None:
        from ..market import history
        raw = history.load_panel(start=last - timedelta(days=lookback_days), end=last, columns=fengine.FRAME_COLS)
    frame: pl.DataFrame = fvalidate.add_trade_columns(fengine.prepare_frame(raw))
    return raw, frame


def day_table(raw: pl.DataFrame, frame: pl.DataFrame, last: date, universe: pl.DataFrame | None,
              fund: pl.DataFrame | None, use_chips: bool = True) -> pl.DataFrame:
    """最后一天每只股票的全部字段：特征、主力阶段、财报、名称行业"""
    chips = None
    if use_chips:
        from ..indicators import chips as chips_mod
        days: list = sorted(frame["date"].unique().to_list())[-CHIP_DAYS:]
        stats, _ = chips_mod.compute_panel(raw.filter(pl.col("code").is_in(frame["code"].unique().to_list())), want=set(days))
        chips = frame.select("code", "date").join(stats, on=["code", "date"], how="left")
    feat: pl.DataFrame = features.compute(frame, chips, rps=True)
    cls: pl.DataFrame = stage.classify(feat)
    today: pl.DataFrame = cls.filter(pl.col("date") == last)
    today = today.with_columns(
        (pl.col("raw_close") / pl.col("preclose") - 1).alias("pct"),
        pl.col("raw_close").alias("close_raw"),
        (pl.col("raw_close") * pl.col("volume") / (pl.col("turn") / 100)).alias("float_cap"),
    )
    if universe is not None and universe.height:
        cols = [c for c in ("code", "name", "industry", "list_date") if c in universe.columns]
        today = today.join(universe.select(cols), on="code", how="left")
    fa = fundamentals_asof(fund, last)
    if fa.width > 1:
        today = today.join(fa, on="code", how="left").with_columns(
            (pl.col("close_raw") / pl.col("eps_ttm")).alias("pe_ttm") if "eps_ttm" in fa.columns else pl.lit(None).alias("pe_ttm"),
            pl.when(pl.col("bvps") > 0).then(pl.col("close_raw") / pl.col("bvps")).otherwise(None).alias("pb") if "bvps" in fa.columns else pl.lit(None).alias("pb"),
            pl.col("net_profit_yoy").alias("profit_yoy") if "net_profit_yoy" in fa.columns else pl.lit(None).alias("profit_yoy"),
        )
    else:
        today = today.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in ("pe_ttm", "pb", "roe", "profit_yoy", "revenue_yoy", "debt_ratio")])
    return today


def _field_expr(field: str) -> pl.Expr:
    return pl.col("close_raw") if field == "close" else pl.col(field)


def _cond_mask(df: pl.DataFrame, cond: dict, formula_hits: dict[str, set]) -> pl.Series:
    t: str = cond["type"]
    if t == "field":
        e = _field_expr(cond["field"])
        v = cond["value"]
        op = cond["op"]
        expr = (e > v) if op == ">" else (e >= v) if op == ">=" else (e < v) if op == "<" else (e <= v) if op == "<=" else \
            ((e >= min(v)) & (e <= max(v)))
        return df.select(expr.fill_null(False)).to_series()
    if t == "stage":
        m = pl.lit(True)
        if cond.get("include"):
            m = m & pl.col("stage").is_in(cond["include"])
        if cond.get("exclude"):
            m = m & ~pl.col("stage").is_in(cond["exclude"])
        return df.select(m.fill_null(False)).to_series()
    key: str = cond.get("fid") or cond.get("text") or ""
    return df["code"].is_in(list(formula_hits.get(key, set())))


def describe_condition(cond: dict) -> str:
    t = cond["type"]
    if t == "field":
        name, unit = S.FIELDS[cond["field"]]
        sc = S.DISPLAY_SCALE.get(cond["field"], 1)
        fmt = lambda x: f"{x * sc:g}"                          # noqa: E731
        if cond["op"] == "between":
            return f"{name} 介于 {fmt(cond['value'][0])} ~ {fmt(cond['value'][1])}"
        return f"{name} {S.OPS[cond['op']]} {fmt(cond['value'])}"
    if t == "stage":
        parts = []
        if cond.get("include"):
            parts.append("主力阶段是" + "/".join(S.STAGES[x] for x in cond["include"]))
        if cond.get("exclude"):
            parts.append("排除" + "/".join(S.STAGES[x] for x in cond["exclude"]))
        return "，".join(parts) or "主力阶段不限"
    if cond.get("fid") and not cond["fid"].startswith("my_"):
        return "公式：" + flib.get(cond["fid"])["name"]
    return "公式：" + ("我的公式" if cond.get("fid") else "自定义")


def score(df: pl.DataFrame, scheme: dict) -> pl.DataFrame:
    """按打分方案给每只股票打分（0~100），并记下贡献最大的理由"""
    sc: dict = scheme["scoring"]
    weights: dict = dict(S.SCORING[sc["scheme"]]["weights"]) if sc["scheme"] in S.SCORING and sc["scheme"] != "custom" else dict(sc.get("weights") or {})
    weights = {k: w for k, w in weights.items() if k in S.TERMS and k in df.columns and abs(w) > 0}
    if not weights or df.is_empty():
        return df.with_columns(pl.lit(50.0).alias("score"), pl.lit("").alias("score_reasons"))
    pct_cols: list[pl.Expr] = []
    for k in weights:
        direction: int = S.TERMS[k][1]
        col = pl.col(k).cast(pl.Float64)
        if k == "pe_ttm":                                    # 亏损（PE 为负）当作最差
            col = pl.when(col > 0).then(col).otherwise(None)
        ranked = col.rank("average") / col.count() * 100 if direction > 0 else (1 - (col.rank("average") - 1) / col.count()) * 100
        pct_cols.append(ranked.fill_null(50.0).alias(f"_p_{k}"))
    out = df.with_columns(pct_cols)
    total: float = sum(abs(w) for w in weights.values())
    out = out.with_columns((pl.sum_horizontal([pl.col(f"_p_{k}") * abs(w) for k, w in weights.items()]) / total).alias("score"))
    # 理由：百分位最高的 3 个因子
    reasons: list[str] = []
    rows = out.select([f"_p_{k}" for k in weights]).to_dicts()
    for r in rows:
        top = sorted(((r[f"_p_{k}"], k) for k in weights), reverse=True)[:3]
        reasons.append("；".join(S.TERMS[k][2].replace("{top}", f"{max(1, round(100 - p))}") for p, k in top if p >= 60))
    return out.with_columns(pl.Series("score_reasons", reasons)).drop([f"_p_{k}" for k in weights])


def run(scheme: dict, *, as_of: date | None = None, profile: dict | None = None, risk: dict | None = None,
        cache: dict | None = None, progress: Callable[[float, str], None] | None = None) -> dict:
    """运行一个选股方案。cache 可传入同一天已算好的 {raw, frame, table}（选股器页面连续调整条件时复用）"""
    from ..market import history, universe as uni_mod

    say = progress or (lambda f, m: None)
    scheme = S.validate_scheme(scheme)
    last: date | None = as_of or history.last_date()
    if last is None:
        raise ValueError("本地还没有日线数据，请先点“一键更新”")
    profile = profile or {}
    risk = risk or {}
    boards: list[str] = scheme["universe"]["boards"] or list(profile.get("boards") or ["main"])
    look: int = max(LOOKBACK_DAYS, int(formula_lookback(scheme) * 1.5) + 30)
    cache = cache if cache is not None else {}
    if cache.get("last") != last or cache.get("look", 0) < look:
        say(0.1, "正在读取日线、计算特征和主力阶段……")
        raw, frame = build_frame(last, look)
        try:
            from ..market import fundamentals
            fund = fundamentals.load_fundamentals()
        except Exception:  # noqa: BLE001  没有财报时基本面字段为空
            fund = None
        uni = uni_mod.load_universe()
        table = day_table(raw, frame, last, uni, fund)
        cache.update({"last": last, "look": look, "raw": raw, "frame": frame, "table": table, "fund": fund})
    frame, table = cache["frame"], cache["table"]
    say(0.7, "正在按条件筛选……")
    # 范围
    u: dict = scheme["universe"]
    df: pl.DataFrame = table.filter(pl.col("board").is_in(boards) & (pl.col("pos") >= MIN_LISTED)
                                    & (pl.col("close_raw") >= u["price_min"]) & (pl.col("close_raw") <= u["price_max"]))
    if u["exclude_st"]:
        df = df.filter(~pl.col("is_st").fill_null(False))
    if u["min_amount"] > 0:
        df = df.filter(pl.col("amt20").fill_null(0) >= u["min_amount"])
    universe_n: int = df.height
    # 条件
    formula_hits: dict[str, set] = {}
    for c in scheme["conditions"]:
        if c["type"] == "formula":
            key: str = c.get("fid") or c.get("text") or ""
            prog = compile_formula(_formula_text(c))
            chips = None
            if prog.uses_chips:
                from ..indicators import chips as chips_mod
                days: list = sorted(frame["date"].unique().to_list())[-CHIP_DAYS:]
                stats, _ = chips_mod.compute_panel(cache["raw"], want=set(days))
                chips = frame.select("code", "date").join(stats, on=["code", "date"], how="left")
            res = fengine.evaluate(prog, frame, chips)
            sig = frame.select("code", "date").with_columns(res.condition.alias("s")).filter((pl.col("date") == last) & pl.col("s"))
            formula_hits[key] = set(sig["code"].to_list())
    masks: list[pl.Series] = [_cond_mask(df, c, formula_hits) for c in scheme["conditions"]]
    if masks:
        m = masks[0]
        for x in masks[1:]:
            m = (m & x) if scheme["match"] == "all" else (m | x)
        df = df.filter(m)
    matched_n: int = df.height
    # 排雷
    rs: pl.DataFrame = riskscan.scan_many(df.select([c for c in ("code", "name", "raw_close", "amt20", "stage") if c in df.columns]),
                                          last, cache.get("fund"))
    df = df.join(rs, on="code", how="left")
    removed_red: int = int((df["risk"] == "red").sum()) if scheme["risk"]["exclude_red"] else 0
    if scheme["risk"]["exclude_red"]:
        df = df.filter(pl.col("risk") != "red")
    if scheme["risk"]["exclude_yellow"]:
        df = df.filter(pl.col("risk") != "yellow")
    # 打分排序
    say(0.85, "正在打分排序……")
    if S.uses_model(scheme):
        from ..modellab import store as lab_store
        df = df.join(lab_store.scores_on(last), on="code", how="left")
    df = score(df, scheme).sort("score", descending=True)
    top: pl.DataFrame = df.head(scheme["top_n"])
    capital: float = float(profile.get("capital") or 100_000)
    rpt: float = float(profile.get("risk_per_trade") or 0.01)
    single: float = float(risk.get("max_single_pct") or 0.2)
    max_stop: float = float(risk.get("default_stop_pct") or 0.08)
    rows: list[dict] = []
    for r in top.to_dicts():
        entry: float = float(r["close_raw"])
        fac: float = float(r.get("adj_factor") or 1.0)
        atr_raw = (r.get("atr14") or 0) / fac if fac else None          # 前复权波幅换算回真实价格
        ma20_raw = (r.get("ma20") or 0) / fac if fac and r.get("ma20") else None
        st = sizing.suggest_stop(entry, atr_raw, ma20_raw, max_pct=max_stop)
        sz = sizing.position_size(capital, entry, st["stop"], rpt, single, lot=sizing.lot_size(r["code"]))
        rows.append({
            "code": r["code"], "name": r.get("name"), "industry": r.get("industry"), "board": r.get("board"),
            "close": entry, "pct": r.get("pct"), "score": r.get("score"), "stage": r.get("stage"),
            "stage_label": S.STAGES.get(r.get("stage") or "unclear"), "stage_score": r.get("stage_score"),
            "risk": r.get("risk"), "risk_reasons": r.get("risk_reasons") or "", "reasons": r.get("score_reasons") or "",
            "rps120": r.get("rps120"), "ret20": r.get("ret20"), "bias20": r.get("bias20"), "amt20": r.get("amt20"),
            "turn": r.get("turn"), "pe_ttm": r.get("pe_ttm"), "roe": r.get("roe"),
            "stop": st["stop"], "stop_basis": st["basis"], "stop_pct": st["pct"], "target": round(entry + 2 * (entry - st["stop"]), 2),
            "shares": sz.shares, "amount": sz.amount, "risk_amount": sz.risk_amount, "size_note": sz.note,
        })
    say(1.0, "选股完成")
    return {
        "date": str(last), "scheme": scheme, "boards": boards, "universe_n": universe_n, "matched_n": matched_n,
        "removed_red": removed_red, "after_risk_n": df.height, "rows": rows,
        "conditions_text": [describe_condition(c) for c in scheme["conditions"]],
        "capital": capital, "risk_per_trade": rpt,
        "note": "选出来的是“符合条件、按打分排在前面”的股票，不是买入保证。先看方案的历史回测是否跑赢随机，再逐只看诊断；"
                f"建议股数按“一笔最多亏总资金 {rpt * 100:.1f}%”和止损价算出，买之前再确认。",
    }
