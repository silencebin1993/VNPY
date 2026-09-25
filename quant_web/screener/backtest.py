"""
选股方案的历史回测：每隔 rebalance 个交易日，用当天（收盘后）已知的数据按方案选出前 N 只，
第二天开盘买入、持有 hold 个交易日收盘卖出（规则与公式验证、模拟盘一致：一字涨停买不进、跌停卖不掉顺延、扣成本）。

对比：
- 同日随机：当天所有"符合范围"的股票按同样规则的平均（选股有没有用，看能不能跑赢它）；
- 统计分选择期（2025-07-01 前）/ 留出期（之后），t 值按调仓期序列计算（rebalance = hold 时各期不重叠）；
- 组合曲线：每期把资金平均分给选出的股票（简化：不考虑一手限制和资金零头，只用于看趋势和回撤）。
历史回测里：主力阶段逐日重算（含筹码证据）；财报按"公布日期"对齐；排雷只用历史上可知的项目
（ST、低价、成交额、主力阶段、亏损/净资产）——解禁、质押、业绩预告等没有完整历史的项目不参与，页面会注明。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

import numpy as np
import polars as pl

from ..analysis import features, stage, stage_stats
from ..formula import engine as fengine
from ..formula import validate as fvalidate
from ..formula.parser import compile_formula
from ..predict import costs as costs_mod
from ..predict import stats
from ..predict.backtest import HOLDOUT_START
from . import engine as sengine
from . import schemes as S

MIN_LISTED: int = 60
CACHE_VERSION: int = 2          # 历史字段表的列有变化时加 1，旧缓存自动失效


def _score_by_date(df: pl.DataFrame, scheme: dict) -> pl.DataFrame:
    sc: dict = scheme["scoring"]
    weights: dict = dict(S.SCORING[sc["scheme"]]["weights"]) if sc["scheme"] in S.SCORING and sc["scheme"] != "custom" else dict(sc.get("weights") or {})
    weights = {k: w for k, w in weights.items() if k in S.TERMS and k in df.columns and abs(w) > 0}
    if not weights:
        return df.with_columns(pl.lit(50.0).alias("score"))
    parts: list[pl.Expr] = []
    for k, w in weights.items():
        col = pl.col(k).cast(pl.Float64)
        if k == "pe_ttm":
            col = pl.when(col > 0).then(col).otherwise(None)
        cnt = col.count().over("date")
        rk = col.rank("average").over("date")
        p = (rk / cnt * 100) if S.TERMS[k][1] > 0 else ((1 - (rk - 1) / cnt) * 100)
        parts.append(p.fill_null(50.0) * abs(w))
    total: float = sum(abs(w) for w in weights.values())
    return df.with_columns((pl.sum_horizontal(parts) / total).alias("score"))


def _fund_asof(df: pl.DataFrame, fund: pl.DataFrame | None) -> pl.DataFrame:
    if fund is None or fund.is_empty():
        return df.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in ("pe_ttm", "pb", "roe", "profit_yoy",
                                                                                  "revenue_yoy", "debt_ratio", "net_profit_ttm")])
    cols = [c for c in ("eps_ttm", "bvps", "roe", "net_profit_yoy", "revenue_yoy", "debt_ratio", "net_profit_ttm") if c in fund.columns]
    f = fund.select(["code", "avail_date", *cols]).drop_nulls("avail_date").sort(["code", "avail_date"])
    out = df.sort(["code", "date"]).join_asof(f.rename({"avail_date": "date"}), on="date", by="code", strategy="backward",
                                                   check_sortedness=False)          # 已按 (code, date) 排序
    return out.with_columns(
        pl.when(pl.col("eps_ttm").is_not_null() & (pl.col("eps_ttm") != 0)).then(pl.col("raw_close") / pl.col("eps_ttm")).alias("pe_ttm"),
        pl.when(pl.col("bvps") > 0).then(pl.col("raw_close") / pl.col("bvps")).alias("pb"),
        pl.col("net_profit_yoy").alias("profit_yoy"),
    )


def build_history(use_chips: bool = True, progress: Callable[[float, str], None] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """全历史的"每只股票每天"的字段表（特征 + 主力阶段）和公式表。分批计算控制内存"""
    from ..market import history

    say = progress or (lambda f, m: None)
    raw: pl.DataFrame = history.load_panel(columns=fengine.FRAME_COLS)
    if raw.is_empty():
        raise ValueError("本地还没有日线数据，请先点“一键更新”")
    frame: pl.DataFrame = fvalidate.add_trade_columns(fengine.prepare_frame(raw))
    rps: pl.DataFrame = stage_stats.rps_table(frame)
    codes: list[str] = frame["code"].unique(maintain_order=True).to_list()
    keep_feat: list[str] = ["ma20", "atr14", "rps120", "ret5", "ret20", "ret60", "rev20", "turn20", "pos250", "bias20", "vr20", "udr20", "obv_slope",
                            "atr_pct", "winner", "conc90", "rally60", "vol_shrink", "dist_ma20", "ma_bull",
                            *[f"score_{s}" for s in stage.PRIORITY], "stage", "stage_score"]
    parts: list[pl.DataFrame] = []
    for i in range(0, len(codes), stage_stats.CHUNK_CODES):
        batch: list[str] = codes[i:i + stage_stats.CHUNK_CODES]
        sub: pl.DataFrame = frame.filter(pl.col("code").is_in(batch))
        chips = fengine.chips_for_frame(sub, raw.filter(pl.col("code").is_in(batch))) if use_chips else None
        feat = features.compute(sub, chips, rps=False).drop("rps120").join(rps, on=["code", "date"], how="left")
        cl = stage.classify(feat)
        parts.append(cl.select(["code", "date", *[c for c in keep_feat if c in cl.columns]]))
        say(min(0.9, (i + len(batch)) / len(codes) * 0.9), f"逐日计算特征和主力阶段：{min(i + len(batch), len(codes))}/{len(codes)}")
    table: pl.DataFrame = pl.concat(parts)
    base_cols = ["code", "date", "open", "close", "raw_open", "raw_close", "preclose", "is_st", "amount", "turn", "volume",
                 "board", "pos", "amt20", "limit_up", "limit_down", "adj_factor"]
    table = frame.select([c for c in base_cols if c in frame.columns]).join(table, on=["code", "date"], how="left")
    table = table.with_columns((pl.col("raw_close") / pl.col("preclose") - 1).alias("pct"),
                               pl.col("raw_close").alias("close_raw"),
                               (pl.col("raw_close") * pl.col("volume") / (pl.col("turn") / 100)).alias("float_cap"))
    return table, frame


def _cache_paths(use_chips: bool) -> tuple:
    from .. import config
    d = config.WORKSPACE.joinpath("screener", "history")
    tag: str = "chips" if use_chips else "nochips"
    return d.joinpath(f"table_{tag}.parquet"), d.joinpath(f"table_{tag}.stamp")


def load_or_build_history(use_chips: bool = True, progress: Callable[[float, str], None] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """全历史字段表按日线文件签名缓存在 workspace/screener/history/（约几百 MB）；日线没变时直接读，几秒钟"""
    import os

    from ..analysis.market import _panel_stamp
    from ..market import history

    say = progress or (lambda f, m: None)
    path, stamp_path = _cache_paths(use_chips)
    stamp: str = f"v{CACHE_VERSION}|{_panel_stamp()}"
    if path.exists() and stamp_path.exists() and stamp_path.read_text(encoding="utf-8") == stamp:
        say(0.3, "读取已缓存的历史特征……")
        table: pl.DataFrame = pl.read_parquet(path)
        raw: pl.DataFrame = history.load_panel(columns=fengine.FRAME_COLS)
        frame: pl.DataFrame = fvalidate.add_trade_columns(fengine.prepare_frame(raw))
        return table, frame
    table, frame = build_history(use_chips, progress)
    small: pl.DataFrame = table.with_columns([pl.col(c).cast(pl.Float32) for c, t in table.schema.items()
                                              if t == pl.Float64 and c not in ("open", "close", "raw_open", "raw_close", "preclose",
                                                                               "limit_up", "limit_down", "adj_factor", "close_raw")])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    small.write_parquet(tmp)
    os.replace(tmp, path)
    stamp_path.write_text(stamp, encoding="utf-8")
    return table, frame


def run(scheme: dict, *, hold: int = 10, rebalance: int | None = None, profile: dict | None = None,
        use_chips: bool = True, cost: costs_mod.Costs | None = None, history_cache: dict | None = None,
        progress: Callable[[float, str], None] | None = None) -> dict:
    say = progress or (lambda f, m: None)
    scheme = S.validate_scheme(scheme)
    rebalance = rebalance or hold
    cost = cost or costs_mod.DEFAULT
    profile = profile or {}
    boards: list[str] = scheme["universe"]["boards"] or list(profile.get("boards") or ["main"])
    hc = history_cache if history_cache is not None else {}
    if hc.get("use_chips") != use_chips or "table" not in hc:
        table, frame = load_or_build_history(use_chips, progress=lambda f, m: say(f * 0.8, m))
        hc.update({"table": table, "frame": frame, "use_chips": use_chips})
    table, frame = hc["table"], hc["frame"]
    say(0.82, "正在按方案筛选每个调仓日……")
    try:
        from ..market import fundamentals
        fund = fundamentals.load_fundamentals()
    except Exception:  # noqa: BLE001
        fund = None
    df: pl.DataFrame = _fund_asof(table, fund)
    u: dict = scheme["universe"]
    elig = (pl.col("board").is_in(boards) & (pl.col("pos") >= MIN_LISTED) & (pl.col("close_raw") >= u["price_min"])
            & (pl.col("close_raw") <= u["price_max"]))
    if u["exclude_st"]:
        elig = elig & ~pl.col("is_st").fill_null(False)
    if u["min_amount"] > 0:
        elig = elig & (pl.col("amt20").fill_null(0) >= u["min_amount"])
    df = df.with_columns(elig.fill_null(False).alias("eligible"))
    days: list[date] = sorted(df["date"].unique().to_list())
    start_idx: int = 260                                      # 前面约一年用来"热身"（年线、相对强度等需要历史）
    reb_days: set = set(days[start_idx::rebalance]) if len(days) > start_idx else set()
    # 条件
    masks: list[pl.Series] = []
    for c in scheme["conditions"]:
        if c["type"] == "formula":
            prog = compile_formula(sengine._formula_text(c))
            chips = None
            if prog.uses_chips:
                raise ValueError("用到筹码函数的公式暂时不能做方案回测（计算量太大），请先在公式库里单独验证这个公式")
            res = fengine.evaluate(prog, frame, chips)
            hit = frame.select("code", "date").with_columns(res.condition.alias("_f"))
            df = df.join(hit, on=["code", "date"], how="left")
            masks.append(df["_f"].fill_null(False))
            df = df.drop("_f")
        else:
            masks.append(sengine._cond_mask(df, c, {}))
    cond: pl.Series = pl.Series([True] * df.height)
    if masks:
        cond = masks[0]
        for m in masks[1:]:
            cond = (cond & m) if scheme["match"] == "all" else (cond | m)
    # 历史上可知的排雷项
    red = (pl.col("close_raw") < 1.2) | (pl.col("amt20").fill_null(0) < 1e7) | (pl.col("stage") == "distribution")
    if "net_profit_ttm" in df.columns:
        red = red | (pl.col("bvps").fill_null(1) < 0) if "bvps" in df.columns else red
    yellow = (pl.col("close_raw") < 2) | (pl.col("amt20").fill_null(0) < 3e7) | (pl.col("stage") == "decline")
    if "net_profit_ttm" in df.columns:
        yellow = yellow | (pl.col("net_profit_ttm").fill_null(1) < 0)
    df = df.with_columns(cond.alias("cond"), red.fill_null(False).alias("red"), yellow.fill_null(False).alias("yellow"))
    pick = pl.col("eligible") & pl.col("cond")
    if scheme["risk"]["exclude_red"]:
        pick = pick & ~pl.col("red")
    if scheme["risk"]["exclude_yellow"]:
        pick = pick & ~pl.col("yellow")
    cand: pl.DataFrame = df.filter(pl.col("date").is_in(list(reb_days)) & pick)
    cand = _score_by_date(cand, scheme)
    picks: pl.DataFrame = (cand.sort(["date", "score"], descending=[False, True])
                           .group_by("date", maintain_order=True).head(scheme["top_n"]).select("code", "date"))
    say(0.9, "正在按真实规则计算收益……")
    fr: pl.DataFrame = fvalidate.forward_returns(df.select(["code", "date", "open", "close", "raw_open", "raw_close", "limit_up",
                                                            "limit_down", "eligible"]), hold, cost)
    base: pl.DataFrame = (fr.filter(pl.col("eligible") & pl.col("net").is_not_null() & pl.col("date").is_in(list(reb_days)))
                          .group_by("date").agg(pl.col("net").mean().alias("base"), pl.len().alias("base_n")))
    trades: pl.DataFrame = picks.join(fr.select("code", "date", "filled", "net", "exit_date"), on=["code", "date"], how="left") \
        .join(base, on="date", how="left").with_columns((pl.col("net") - pl.col("base")).alias("excess"))
    per: pl.DataFrame = (trades.filter(pl.col("net").is_not_null()).group_by("date")
                         .agg(pl.col("net").mean().alias("port"), pl.col("base").first(), pl.len().alias("n")).sort("date"))
    out: dict = {"hold": hold, "rebalance": rebalance, "top_n": scheme["top_n"], "boards": boards, "scheme": scheme,
                 "holdout_start": str(HOLDOUT_START), "use_chips": use_chips, "n_periods": per.height,
                 "n_trades": int(trades.filter(pl.col("net").is_not_null()).height),
                 "unfilled": int((~trades["filled"].fill_null(False)).sum()) if trades.height else 0,
                 "empty_periods": len(reb_days) - per.height,
                 "data_start": str(days[0]) if days else None, "data_end": str(days[-1]) if days else None}
    segs: dict = {}
    for name, part in (("selection", per.filter(pl.col("date") < HOLDOUT_START)), ("holdout", per.filter(pl.col("date") >= HOLDOUT_START)),
                       ("all", per)):
        segs[name] = _curve_stats(part, hold, trades.filter(pl.col("date").is_in(part["date"].to_list())) if part.height else trades.head(0))
    out["stats"] = segs
    out["curve"] = [{"date": str(r["date"]), "port": r["port"], "base": r["base"], "n": r["n"]} for r in per.to_dicts()]
    out["verdict"] = _verdict(segs)
    out["recent_picks"] = trades.sort("date", descending=True).head(60).to_dicts()
    say(1.0, "回测完成")
    return out


def _curve_stats(per: pl.DataFrame, hold: int, trades: pl.DataFrame) -> dict:
    if per.height == 0:
        return {"n": 0}
    port = per["port"].to_numpy()
    base = per["base"].to_numpy()
    ex = port - base
    eq_p, eq_b = np.cumprod(1 + port), np.cumprod(1 + base)
    years: float = max(per.height * hold / 244, 1e-9)
    dd = lambda eq: float((eq / np.maximum.accumulate(eq) - 1).min())          # noqa: E731
    t = stats.daily_t(ex)
    done = trades.filter(pl.col("net").is_not_null())
    return {
        "n": per.height, "trades": done.height, "start": str(per["date"].min()), "end": str(per["date"].max()),
        "period_mean": float(port.mean()), "base_mean": float(base.mean()), "excess": float(ex.mean()),
        "win_periods": float((ex > 0).mean()), "trade_win": float((done["net"] > 0).mean()) if done.height else None,
        "total": float(eq_p[-1] - 1), "base_total": float(eq_b[-1] - 1),
        "cagr": float(eq_p[-1] ** (1 / years) - 1), "base_cagr": float(eq_b[-1] ** (1 / years) - 1),
        "max_dd": dd(eq_p), "base_max_dd": dd(eq_b), "t": stats.rounded(t),
    }


def _verdict(segs: dict) -> dict:
    """一句话结论：以留出期（样本外）的超额为准，再补一句回撤对比（全部时段）"""
    ho, sel, al = segs.get("holdout") or {}, segs.get("selection") or {}, segs.get("all") or {}
    if not ho.get("n"):
        return {"credible": False, "text": "最近一段（样本外）没有选出股票，无法判断"}
    t, ex = ho.get("t"), ho.get("excess") or 0.0
    dd_note: str = ""
    if al.get("max_dd") is not None and al.get("base_max_dd") is not None:
        a, b = al["max_dd"], al["base_max_dd"]
        if a > b * 0.75:                                        # 回撤明显比随机小（负数，越接近 0 越小）
            dd_note = f"；它的最大回撤 {a * 100:.0f}%，明显小于同日随机的 {b * 100:.0f}%（更稳）"
        elif a < b * 1.25:
            dd_note = f"；它的最大回撤 {a * 100:.0f}%，比同日随机的 {b * 100:.0f}% 更大（更容易大亏）"
    if t is not None and t >= 2 and ex > 0 and (sel.get("excess") or 0) > 0:
        return {"credible": True, "text": f"留出期平均每期比同日随机多赚 {ex * 100:.2f}%（t {t:.1f}），有一定可信度，但不保证以后有效" + dd_note}
    if ex > 0:
        return {"credible": False, "text": f"留出期平均每期比同日随机多赚 {ex * 100:.2f}%，但 t 值 {t if t is not None else '—'} 不到 2，"
                                           "收益上还不能说比随便买强" + dd_note}
    return {"credible": False, "text": f"留出期平均每期比同日随机少赚 {abs(ex) * 100:.2f}%：这个方案在样本外没有优势" + dd_note}
