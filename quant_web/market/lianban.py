"""
连板天梯与短线情绪（复盘 / 观察用，不是买入信号）。

- ladder(day)：某天的涨停股按连板高度分层。优先用东方财富涨停池存档（有封板资金、首次/最后封板时间、炸板次数、
  几天几板），没有存档的日子用日线面板识别（收盘涨停；连板数按该股自己的交易日连续计算，停牌不打断）。
  过去的日子附带"次日结果"：次日开盘/收盘涨幅、是否继续涨停（晋级），便于复盘；
- promotion(days)：最近 N 个交易日里，各高度（首板、2板……）第二天继续涨停的比例，以及次日平均开盘溢价、收盘涨幅、
  红盘率——这是短线情绪最直接的温度计；
- cycle(days)：每日情绪序列（涨停/跌停家数、炸板率、最高板、昨日涨停今日晋级率与溢价、情绪温度）。

口径：涨停/跌停价按交易所规则（见 predict/limits.py）；"晋级"= 次日收盘仍涨停；一字板 = 开盘、最低都在涨停价。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from ..predict import service


MAX_TIER: int = 7                     # 晋级率统计时 7 板及以上合并


def _ctx() -> service.Context:
    return service.context()


def trading_dates(limit: int = 250) -> list[str]:
    ctx = _ctx()
    return [d.isoformat() for d in ctx.dates[-limit:]][::-1]


def _next_day_map(lim: pl.DataFrame, day: date, dates: list[date]) -> pl.DataFrame | None:
    """day 的下一个交易日每只股票的表现（次日停牌的股票没有行）"""
    if day not in dates:
        return None
    i = dates.index(day)
    if i + 1 >= len(dates):
        return None
    nxt = dates[i + 1]
    return lim.filter(pl.col("date") == nxt).select(
        "code",
        pl.lit(nxt).alias("next_date"),
        (pl.col("open") / pl.col("preclose") - 1).alias("next_open_pct"),
        (pl.col("close") / pl.col("preclose") - 1).alias("next_pct"),
        pl.col("is_limit_up").alias("next_limit_up"),
        pl.col("one_word").alias("next_one_word"),
        pl.col("is_limit_down").alias("next_limit_down"),
    )


def _pool_rows(kind: str, day: date) -> pl.DataFrame | None:
    from . import pools

    try:
        df = pools.load_archive(kind, start=day, end=day)
    except Exception:  # noqa: BLE001  存档读不到就用面板
        return None
    if df is None or df.is_empty():
        return None
    return df


def _panel_day(ctx: service.Context, day: date) -> pl.DataFrame:
    uni = ctx.universe.select("code", "name", "industry")
    return (
        ctx.panel_lim.filter(pl.col("date") == day)
        .join(uni, on="code", how="left")
        .with_columns((pl.col("close") / pl.col("preclose") - 1).alias("pct_d"))
    )


def ladder(day_text: str | None = None, main_only: bool = False) -> dict:
    ctx = _ctx()
    dates: list[date] = ctx.dates
    if not dates:
        return {"date": None, "tiers": [], "broken": [], "limit_down": [], "dates": [], "summary": {}}
    day: date = date.fromisoformat(day_text) if day_text else dates[-1]
    if day not in dates:
        earlier = [d for d in dates if d <= day]
        day = earlier[-1] if earlier else dates[-1]
    lim = ctx.panel_lim
    pan = _panel_day(ctx, day)
    nxt = _next_day_map(lim, day, dates)

    base_cols = ["code", "name", "industry", "board", "close", "pct_d", "amount", "turn", "streak", "one_word"]
    zt = pan.filter(pl.col("is_limit_up")).select(base_cols)
    broken = pan.filter(pl.col("is_broken")).select(base_cols + ["high"])
    ldown = pan.filter(pl.col("is_limit_down")).select(base_cols)
    source = "panel"

    pool = _pool_rows("zt", day)
    if pool is not None:
        source = "pool"
        extra = pool.select("code", "seal_amount", "first_time", "last_time", "open_times", "stat", "float_cap",
                            pl.col("industry").alias("pool_industry"), pl.col("streak").alias("pool_streak"))
        zt = zt.join(extra, on="code", how="left").with_columns(
            pl.coalesce("pool_industry", "industry").alias("industry"),
        ).drop("pool_industry")
    zb_pool = _pool_rows("zb", day)
    if zb_pool is not None:
        broken = broken.join(zb_pool.select("code", "first_time", "open_times", "stat"), on="code", how="left")
    dt_pool = _pool_rows("dt", day)
    if dt_pool is not None:
        ldown = ldown.join(dt_pool.select("code", "seal_amount", "open_times", pl.col("streak").alias("dt_days")),
                           on="code", how="left")

    if nxt is not None:
        zt = zt.join(nxt, on="code", how="left")
        broken = broken.join(nxt, on="code", how="left")
        ldown = ldown.join(nxt, on="code", how="left")
    if main_only:
        zt, broken, ldown = (df.filter(pl.col("board") == "main") for df in (zt, broken, ldown))

    # 模型晋级概率：只有最新一天有（模型只给最新收盘打分）
    probs: dict[str, float] = {}
    if day == dates[-1]:
        try:
            pred = service.predict_latest("streak")
            probs = {r["code"]: r["prob"] for r in pred.get("rows") or [] if r.get("prob") is not None}
        except Exception:  # noqa: BLE001  没训练模型时不显示概率
            probs = {}

    def rows(df: pl.DataFrame) -> list[dict]:
        out = []
        for r in df.sort(["streak", "amount"], descending=[True, True]).iter_rows(named=True):
            r["pct"] = r.pop("pct_d", None)
            if r["code"] in probs:
                r["prob"] = probs[r["code"]]
            for k in ("next_date",):
                if r.get(k) is not None:
                    r[k] = r[k].isoformat()
            out.append(r)
        return out

    zt_rows = rows(zt)
    tiers: dict[int, list[dict]] = {}
    for r in zt_rows:
        tiers.setdefault(int(r.get("pool_streak") or r["streak"] or 1), []).append(r)
    tier_list = [{"streak": k, "count": len(v), "stocks": v,
                  "promoted": sum(1 for x in v if x.get("next_limit_up")) if nxt is not None else None}
                 for k, v in sorted(tiers.items(), reverse=True)]

    # 当日汇总（与首页"市场情绪"同口径，另加昨日涨停今日表现）
    sent = ctx.sentiment.filter(pl.col("date") == day)
    summary: dict = sent.row(0, named=True) if sent.height else {}
    summary = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in summary.items()}
    if main_only:
        summary["note"] = "汇总数字是全市场口径；下面的天梯只列主板"
    return {
        "date": day.isoformat(), "source": source, "latest": day == dates[-1],
        "next_date": dates[dates.index(day) + 1].isoformat() if day != dates[-1] else None,
        "summary": summary, "tiers": tier_list, "broken": rows(broken), "limit_down": rows(ldown),
        "has_probs": bool(probs),
    }


def promotion(days: int = 60, main_only: bool = False) -> dict:
    """最近 days 个交易日（有次日结果的）各连板高度的次日表现"""
    ctx = _ctx()
    dates = ctx.dates
    if len(dates) < 3:
        return {"days": 0, "rows": []}
    use = dates[-(days + 1):-1]            # 最后一天还没有次日
    lim = ctx.panel_lim.filter(pl.col("date") >= use[0])
    if main_only:
        lim = lim.filter(pl.col("board") == "main")
    lim = lim.sort(["code", "date"]).with_columns(
        pl.col("date").shift(-1).over("code").alias("_nd"),
        (pl.col("open").shift(-1).over("code") / pl.col("close") - 1).alias("n_open"),
        (pl.col("close").shift(-1).over("code") / pl.col("close") - 1).alias("n_close"),
        pl.col("is_limit_up").shift(-1).over("code").alias("n_lu"),
        pl.col("one_word").shift(-1).over("code").alias("n_ow"),
    )
    nd = {d: dates[i + 1] for i, d in enumerate(dates[:-1])}
    lim = lim.filter(pl.col("date").is_in(use)).with_columns(
        pl.col("date").replace_strict(nd, default=None).alias("_expect")
    ).filter(pl.col("_nd") == pl.col("_expect"))             # 次日停牌的不算
    tier = pl.when(pl.col("is_limit_up")).then(pl.min_horizontal(pl.col("streak").cast(pl.Int32), MAX_TIER)) \
        .when(pl.col("is_broken")).then(0).otherwise(None)
    g = (
        lim.with_columns(tier.alias("tier")).filter(pl.col("tier").is_not_null())
        .group_by("tier").agg(
            pl.len().alias("n"),
            pl.col("n_lu").mean().alias("promote"),
            pl.col("n_open").mean().alias("open_prem"),
            pl.col("n_close").mean().alias("close_ret"),
            (pl.col("n_close") > 0).mean().alias("red"),
            pl.col("n_ow").mean().alias("one_word_next"),
        ).sort("tier")
    )
    out = []
    for r in g.iter_rows(named=True):
        t = int(r["tier"])
        r["label"] = "炸板" if t == 0 else ("首板" if t == 1 else (f"{t}板" if t < MAX_TIER else f"{MAX_TIER}板及以上"))
        out.append(r)
    return {"days": len(use), "start": use[0].isoformat(), "end": use[-1].isoformat(), "rows": out,
            "note": "晋级 = 次日收盘仍涨停；溢价 = 次日开盘价相对当日收盘；次日停牌的不计入。一字板买不进，实际能拿到的收益比平均值差。"}


def cycle(days: int = 120) -> list[dict]:
    from . import sentiment

    return sentiment.recent(days)
