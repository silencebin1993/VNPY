"""
板块（行业）强弱：按证监会行业把全市场股票分组，看最近 5/20/60 天的中位涨幅、站上 20 日线的比例、成交额和龙头股。
只用于"看清哪里强、哪里弱"，不做预测；强势行业的历史表现以验证为准。
"""
from __future__ import annotations

import polars as pl

MIN_STOCKS: int = 5


def compute(frame: pl.DataFrame, universe: pl.DataFrame, leaders: int = 3, min_amount: float = 5e7) -> dict:
    """frame：公式表（前复权，按 code/date 排序）；返回 {date, rows: [...]}（按 20 日强度排序）"""
    if frame.is_empty():
        return {"date": None, "rows": []}
    df = frame.select("code", "date", "close", "amount").with_columns(
        (pl.col("close") / pl.col("close").shift(5).over("code") - 1).alias("r5"),
        (pl.col("close") / pl.col("close").shift(20).over("code") - 1).alias("r20"),
        (pl.col("close") / pl.col("close").shift(60).over("code") - 1).alias("r60"),
        (pl.col("close") > pl.col("close").rolling_mean(20, min_samples=20).over("code")).alias("above20"),
        pl.col("amount").rolling_mean(20, min_samples=5).over("code").alias("amt20"),
    )
    last_date = df["date"].max()
    today = df.filter(pl.col("date") == last_date)
    uni = universe.select("code", "name", "industry").filter(pl.col("industry").is_not_null() & (pl.col("industry") != ""))
    today = today.join(uni, on="code", how="inner")
    agg = today.group_by("industry").agg(
        pl.len().alias("n"), pl.col("r5").median().alias("r5"), pl.col("r20").median().alias("r20"),
        pl.col("r60").median().alias("r60"), pl.col("above20").mean().alias("above20"),
        pl.col("amount").sum().alias("amount"),
    ).filter(pl.col("n") >= MIN_STOCKS)
    agg = agg.with_columns(
        (pl.col("r20").rank("average") / pl.len() * 100).alias("rank20"),
        (pl.col("r60").rank("average") / pl.len() * 100).alias("rank60"),
    ).with_columns(((pl.col("rank20") + pl.col("rank60")) / 2).alias("strength")).sort("strength", descending=True)
    top = (today.filter(pl.col("amt20") >= min_amount).sort("r20", descending=True)
           .group_by("industry", maintain_order=True).head(leaders)
           .select("industry", "code", "name", "r20"))
    lead: dict[str, list[dict]] = {}
    for r in top.to_dicts():
        lead.setdefault(r["industry"], []).append({"code": r["code"], "name": r["name"], "r20": r["r20"]})
    rows = [{**r, "leaders": lead.get(r["industry"], [])} for r in agg.to_dicts()]
    return {"date": str(last_date), "rows": rows,
            "note": "按行业中位数计算（不受个别大涨股影响）；强度 = 20 日和 60 日涨幅排名的平均。强势行业不代表还会继续强。"}
