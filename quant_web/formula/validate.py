"""
公式验证（事件研究）：历史上每次出现信号后，按真实规则买入、持有 N 天，扣成本后到底赚不赚、比"同一天随便买"强不强。

规则（与回测、模拟盘同一套口径）：
- 信号当天收盘后才知道 → 下一个交易日开盘买入；开盘就涨停（一字/秒板）视为买不进；
- 持有 N 个交易日（买入当天算第 1 天），第 N 天收盘卖出；那天跌停封死卖不掉时顺延（最多 5 天）；
- 收益按前复权价计算（含分红送股），再扣佣金、印花税（按卖出日期）、双边滑点（predict/costs.py）；
- 同一只股票在持有期内重复出现的信号只算第一次（避免同一笔持仓被重复计数）；
- 随机基准：同一天、同一范围（同样的板块/非 ST/上市满 60 天/成交额门槛）里所有能买进的股票，按同样规则的平均收益。
  超额收益 = 信号收益 − 同日基准；
- 分段：2025-07-01（backtest.HOLDOUT_START）之前为"选择期"，之后为"留出期"（样本外），分开统计；
- t 值 = min(按日 t, Newey-West t（滞后 = 持有天数）)，按信号日汇总后计算；小于 2 标"还不够可信"。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from ..indicators import funcs as F
from ..predict import costs as costs_mod
from ..predict import stats
from ..predict.backtest import HOLDOUT_START
from . import engine
from .parser import Program

HOLDS: tuple[int, ...] = (5, 10, 20)
MAX_EXIT_DELAY: int = 5
MIN_LISTED_DAYS: int = 60
MAX_ENTRY_GAP_DAYS: int = 10          # 信号日到下一个交易日超过这么多天（长期停牌）视为买不进


def _board_expr() -> pl.Expr:
    code = pl.col("code")
    return (pl.when(code.str.starts_with("688") | code.str.starts_with("689")).then(pl.lit("star"))
            .when(code.str.starts_with("30")).then(pl.lit("chinext"))
            .when(code.str.starts_with("8") | code.str.starts_with("4") | code.str.starts_with("92")).then(pl.lit("bj"))
            .otherwise(pl.lit("main")))


def _limit_ratio_expr() -> pl.Expr:
    """近似涨跌停比例（与 limits 的规则一致：主板 10%，创业板/科创板 20%（创业板 2020-08-24 前 10%），北交所 30%，ST 5%）"""
    b = pl.col("board")
    cyb_old = (b == "chinext") & (pl.col("date") < pl.lit(date(2020, 8, 24)))
    return (pl.when(pl.col("is_st").fill_null(False) & (b == "main")).then(0.05)
            .when(b == "bj").then(0.30)
            .when(cyb_old).then(0.10)
            .when(b.is_in(["chinext", "star"])).then(0.20)
            .otherwise(0.10))


def add_trade_columns(frame: pl.DataFrame) -> pl.DataFrame:
    """加上板块、真实价格的涨跌停价、组内序号、20 日平均成交额（按 (code, date) 排序的公式表）"""
    df: pl.DataFrame = frame.with_columns(_board_expr().alias("board"))
    ratio = _limit_ratio_expr()
    df = df.with_columns(
        ((pl.col("preclose") * (1 + ratio) + 1e-6).round(2)).alias("limit_up"),
        ((pl.col("preclose") * (1 - ratio) + 1e-6).round(2)).alias("limit_down"),
        pl.int_range(pl.len()).over("code").alias("pos"),
        pl.col("amount").rolling_mean(20, min_samples=5).over("code").alias("amt20"),
    )
    return df


def eligible_expr(boards: tuple[str, ...], exclude_st: bool, min_amount: float) -> pl.Expr:
    e = pl.col("board").is_in(list(boards)) & (pl.col("pos") >= MIN_LISTED_DAYS)
    if exclude_st:
        e = e & ~pl.col("is_st").fill_null(False)
    if min_amount > 0:
        e = e & (pl.col("amt20").fill_null(0) >= min_amount)
    return e


def forward_returns(df: pl.DataFrame, hold: int, cost: costs_mod.Costs, delisted: set[str] | None = None) -> pl.DataFrame:
    """每一行"明天开盘买、持有 hold 天收盘卖"的净收益：列 filled（能否买进）、net（扣成本净收益）、exit_date。
    持有期间退市（之后再也没有行情）的，按最后一个收盘价算（和 predict/execution 的 R_DELISTED 同一口径）——
    不能把它们当成"还没结束"丢掉，否则爱买暴跌股的方法会显得比实际好"""
    if delisted is None:
        from ..predict.execution import load_delisted
        delisted = load_delisted()
    over = lambda e: e.over("code")                                            # noqa: E731
    nxt = lambda c, k: over(pl.col(c).shift(-k))                               # noqa: E731
    entry_ok: pl.Expr = (
        nxt("date", 1).is_not_null()
        & ((nxt("date", 1) - pl.col("date")).dt.total_days() <= MAX_ENTRY_GAP_DAYS)
        & (nxt("raw_open", 1) < nxt("limit_up", 1) - 0.005)
    )
    exit_q: list[pl.Expr] = []
    exit_d: list[pl.Expr] = []
    for k in range(hold, hold + MAX_EXIT_DELAY + 1):
        can_sell: pl.Expr = nxt("raw_close", k) > nxt("limit_down", k) + 0.005
        last: bool = k == hold + MAX_EXIT_DELAY
        cond: pl.Expr = nxt("close", k).is_not_null() & (can_sell if not last else pl.lit(True))
        exit_q.append(pl.when(cond).then(nxt("close", k)))
        exit_d.append(pl.when(cond).then(nxt("date", k)))
    out: pl.DataFrame = df.with_columns(
        entry_ok.fill_null(False).alias("filled"),
        nxt("open", 1).alias("_entry_q"),
        pl.coalesce(exit_q).alias("_exit_q"),
        pl.coalesce(exit_d).alias("exit_date"),
    )
    if delisted and out.height:
        last_d = pl.col("date").last().over("code")
        miss = (pl.col("_exit_q").is_null() & pl.col("filled") & pl.col("code").is_in(list(delisted))
                & (last_d > pl.col("date")) & (last_d < out["date"].max()))           # 在数据结束之前就没有行情了 = 退市
        out = out.with_columns(pl.when(miss).then(pl.col("close").drop_nulls().last().over("code")).otherwise(pl.col("_exit_q")).alias("_exit_q"),
                               pl.when(miss).then(last_d).otherwise(pl.col("exit_date")).alias("exit_date"))
    stamp: pl.Expr = costs_mod.stamp_duty_expr(pl.col("exit_date")) if cost.stamp is None else pl.lit(float(cost.stamp))
    factor: pl.Expr = ((1 - cost.slippage) * (1 - cost.commission - stamp)) / ((1 + cost.slippage) * (1 + cost.commission))
    out = out.with_columns(
        pl.when(pl.col("filled") & pl.col("_exit_q").is_not_null() & (pl.col("_entry_q") > 0))
        .then(pl.col("_exit_q") / pl.col("_entry_q") * factor - 1).otherwise(None).alias("net")
    )
    return out.drop(["_entry_q", "_exit_q"])


def _period_stats(sig: pl.DataFrame, hold: int) -> dict:
    """一段时间里的信号统计（sig 含 date, net, base, excess）"""
    done: pl.DataFrame = sig.filter(pl.col("net").is_not_null())
    n: int = done.height
    if n == 0:
        return {"n": 0}
    daily: pl.DataFrame = done.group_by("date").agg(pl.col("excess").mean(), pl.col("net").mean()).sort("date")
    dt: float | None = stats.daily_t(daily["excess"].to_numpy())
    nwt: float | None = stats.nw_t(daily["excess"].to_numpy(), lag=max(hold, 1))
    t: float | None = min(dt, nwt) if dt is not None and nwt is not None else (dt if nwt is None else nwt)
    q = lambda c: float(done[c].mean())                                         # noqa: E731
    return {
        "n": n, "days": daily.height, "start": str(done["date"].min()), "end": str(done["date"].max()),
        "mean": q("net"), "median": float(done["net"].median()), "win": float((done["net"] > 0).mean()),
        "base": q("base"), "excess": q("excess"), "excess_daily": float(daily["excess"].mean()),
        "excess_win": float((done["excess"] > 0).mean()),
        "t": stats.rounded(t), "daily_t": stats.rounded(dt), "nw_t": stats.rounded(nwt),
        "best": float(done["net"].max()), "worst": float(done["net"].min()),
    }


def _verdict(hold_res: dict) -> dict:
    """一句话结论（以留出期为准）"""
    ho: dict = hold_res.get("holdout") or {}
    sel: dict = hold_res.get("selection") or {}
    if not ho.get("n"):
        return {"key": "no_data", "text": "留出期（最近）没有足够的信号，无法判断", "credible": False}
    t: float | None = ho.get("t")
    ex: float = ho.get("excess") or 0.0
    # 可信要同时满足：t ≥ 2、按笔平均和按天平均的超额都为正、选择期也为正（避免少数几天的大量信号造成假象）
    if t is not None and t >= 2 and ex > 0 and (ho.get("excess_daily") or 0) > 0 and (sel.get("excess") or 0) > 0:
        return {"key": "good", "credible": True,
                "text": f"最近一段（样本外）平均每笔比同日随便买多赚 {ex * 100:.2f}%，t 值 {t:.1f}，有一定可信度（仍不保证以后有效）"}
    if ex > 0:
        return {"key": "weak", "credible": False,
                "text": f"最近一段平均比同日随便买多赚 {ex * 100:.2f}%，但 t 值 {t if t is not None else '—'} 不到 2，还不够可信，可能只是运气"}
    return {"key": "bad", "credible": False,
            "text": f"最近一段平均比同日随便买少赚 {abs(ex) * 100:.2f}%——这个公式在样本外没有优势"}


def event_study(prog: Program, frame: pl.DataFrame, *, chips: pl.DataFrame | None = None,
                holds: tuple[int, ...] = HOLDS, boards: tuple[str, ...] = ("main",), exclude_st: bool = True,
                min_amount: float = 0.0, start: date | None = None, end: date | None = None,
                cost: costs_mod.Costs | None = None, dedupe: bool = True,
                progress: Callable[[float, str], None] | None = None) -> dict:
    """在已准备好的公式表（engine.prepare_frame 的结果，含 preclose/is_st）上做事件研究，返回可直接给页面的结果"""
    cost = cost or costs_mod.DEFAULT
    say = progress or (lambda f, m: None)
    say(0.05, "正在计算公式……")
    df: pl.DataFrame = add_trade_columns(frame)
    res = engine.evaluate(prog, df, chips)
    if res.condition is None:
        raise ValueError("这个公式没有选股条件（请写一句 XG:…）")
    df = df.with_columns(res.condition.alias("signal"), eligible_expr(boards, exclude_st, min_amount).alias("eligible"))
    if start is not None:
        df = df.with_columns(pl.when(pl.col("date") < start).then(False).otherwise(pl.col("eligible")).alias("eligible"))
    if end is not None:
        df = df.with_columns(pl.when(pl.col("date") > end).then(False).otherwise(pl.col("eligible")).alias("eligible"))
    ctx = F.Ctx(df["code"])
    out: dict = {"holds": {}, "boards": list(boards), "exclude_st": exclude_st, "min_amount": min_amount,
                 "holdout_start": str(HOLDOUT_START), "uses_chips": prog.uses_chips}
    raw_signals: int = int((df["signal"] & df["eligible"]).sum())
    out["raw_signals"] = raw_signals
    for i, h in enumerate(holds):
        say(0.15 + 0.8 * i / len(holds), f"正在统计持有 {h} 天的结果……")
        sig_col: pl.Series = df["signal"] & df["eligible"]
        if dedupe:
            sig_col = F.filter_(sig_col, h - 1, ctx) & df["eligible"]
        fr: pl.DataFrame = forward_returns(df.select(["code", "date", "open", "close", "raw_open", "raw_close",
                                                      "limit_up", "limit_down", "eligible"]), h, cost)
        fr = fr.with_columns(sig_col.alias("sig"))
        base: pl.DataFrame = (fr.filter(pl.col("eligible") & pl.col("net").is_not_null())
                              .group_by("date").agg(pl.col("net").mean().alias("base"), pl.len().alias("base_n")))
        sig: pl.DataFrame = fr.filter(pl.col("sig")).join(base, on="date", how="left").with_columns(
            (pl.col("net") - pl.col("base")).alias("excess"))
        pending: int = int(sig.filter(pl.col("filled") & pl.col("net").is_null()).height)
        unfilled: int = int((~sig["filled"]).sum()) if sig.height else 0
        sel: pl.DataFrame = sig.filter(pl.col("date") < HOLDOUT_START)
        ho: pl.DataFrame = sig.filter(pl.col("date") >= HOLDOUT_START)
        years: list[dict] = []
        if sig.height:
            for (y,), g in sig.filter(pl.col("net").is_not_null()).group_by(pl.col("date").dt.year(), maintain_order=True):
                years.append({"year": int(y), "n": g.height, "mean": float(g["net"].mean()),
                              "excess": float(g["excess"].mean()), "win": float((g["net"] > 0).mean())})
        hr: dict = {
            "signals": sig.height, "unfilled": unfilled, "pending": pending,
            "all": _period_stats(sig, h), "selection": _period_stats(sel, h), "holdout": _period_stats(ho, h),
            "years": sorted(years, key=lambda x: x["year"]),
        }
        hr["verdict"] = _verdict(hr)
        out["holds"][str(h)] = hr
        if h == holds[0]:
            recent: pl.DataFrame = sig.sort("date", descending=True).head(60)
            out["recent"] = recent.select(["code", "date", "filled", "net", "base", "excess", "exit_date"]).to_dicts()
    say(1.0, "验证完成")
    return out


def run(text: str, *, holds: tuple[int, ...] = HOLDS, boards: tuple[str, ...] = ("main",), exclude_st: bool = True,
        min_amount: float = 0.0, start: date | None = None, end: date | None = None,
        progress: Callable[[float, str], None] | None = None) -> dict:
    """从本地日线读数据并验证一个公式（后台任务调用）"""
    from ..market import history
    from .parser import compile_formula

    prog: Program = compile_formula(text)
    say = progress or (lambda f, m: None)
    say(0.01, "正在读取本地日线……")
    panel: pl.DataFrame = history.load_panel(columns=engine.FRAME_COLS)
    if panel.is_empty():
        raise ValueError("本地还没有日线数据，请先点“一键更新”下载数据")
    frame: pl.DataFrame = engine.prepare_frame(panel)
    chips: pl.DataFrame | None = None
    if prog.uses_chips:
        say(0.03, "公式用到了筹码函数：正在逐日估算全市场筹码分布（较慢，约几分钟）……")
        chips = engine.chips_for_frame(frame, panel, progress=lambda f, m: say(0.03 + 0.1 * f, m))
    del panel
    result: dict = event_study(prog, frame, chips=chips, holds=holds, boards=boards, exclude_st=exclude_st,
                               min_amount=min_amount, start=start, end=end,
                               progress=lambda f, m: say(0.13 + 0.86 * f, m))
    result["formula"] = text
    result["data_start"] = str(frame["date"].min())
    result["data_end"] = str(frame["date"].max())
    return result
