"""
市场情绪指标：历史每日一行（由带涨停列的日线面板计算）+ 盘中实时一行（腾讯全市场快照）。

口径：
- 只统计当日 no_limit == False 的股票（新股无涨跌幅限制期不算），停牌股不在面板里；
- 涨幅类字段（prev_lu_premium、prev_streak_premium、median_pct）单位是 %（2.5 表示 2.5%）；
  比率类字段（break_rate、prev_lu_promote）是 0~1 的小数；amount_total 单位 元；
- "昨日涨停股"指上一个交易日涨停、且今天有行情的股票（停牌跳过的不算）；
- temperature：n_limit_up、-n_limit_down、-break_rate、prev_lu_premium、max_streak、n_up/n_stocks
  在过去 250 个交易日（含当日）中的分位数平均 ×100，只用当日及以前的数据。
"""
from datetime import date, datetime

import numpy as np
import polars as pl


TEMP_WINDOW: int = 250
TEMP_MIN_SAMPLES: int = 20
TOLERANCE: float = 0.005

SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date,
    "n_stocks": pl.Int32,
    "n_up": pl.Int32,
    "n_down": pl.Int32,
    "n_limit_up": pl.Int32,
    "n_limit_down": pl.Int32,
    "n_broken": pl.Int32,
    "break_rate": pl.Float64,
    "max_streak": pl.Int32,
    "n_streak2": pl.Int32,
    "prev_lu_premium": pl.Float64,
    "prev_lu_promote": pl.Float64,
    "prev_streak_premium": pl.Float64,
    "median_pct": pl.Float64,
    "amount_total": pl.Float64,
    "amount_ratio20": pl.Float64,
    "temperature": pl.Float64,
}

# 情绪温度的组成：(列表达式, 名称)；方向为负的指标取相反数
_TEMP_PARTS: list[tuple[str, int]] = [
    ("n_limit_up", 1), ("n_limit_down", -1), ("break_rate", -1),
    ("prev_lu_premium", 1), ("max_streak", 1), ("up_ratio", 1),
]


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMA)


def _with_prev(panel_lim: pl.DataFrame) -> pl.DataFrame:
    """加上"上一个交易日"该股的涨停/连板状态（上一行必须恰好是市场上一个交易日）"""
    df: pl.DataFrame = panel_lim.select(
        ["date", "code", "pct", "is_limit_up", "is_limit_down", "is_broken", "streak", "amount", "no_limit"]
    ).sort(["code", "date"])
    cal: pl.DataFrame = pl.DataFrame({"date": df["date"].unique().sort()})
    cal = cal.with_columns(pl.col("date").shift(1).alias("_cal_prev"))
    df = df.with_columns(
        pl.col("date").shift(1).over("code").alias("_prev_date"),
        pl.col("is_limit_up").shift(1).over("code").alias("prev_lu"),
        pl.col("streak").shift(1).over("code").alias("prev_streak"),
    ).join(cal, on="date", how="left")
    valid = (pl.col("_prev_date") == pl.col("_cal_prev")).fill_null(False)
    return df.with_columns(
        (valid & pl.col("prev_lu").fill_null(False)).alias("prev_lu"),
        pl.when(valid).then(pl.col("prev_streak")).otherwise(0).fill_null(0).alias("prev_streak"),
    ).drop(["_prev_date", "_cal_prev"])


def _aggregate(df: pl.DataFrame) -> pl.DataFrame:
    """按日汇总；df 需含 date, pct, is_limit_up, is_limit_down, is_broken, streak, amount, no_limit, prev_lu, prev_streak"""
    live = df.filter(~pl.col("no_limit").fill_null(False))
    pct = pl.col("pct")
    out: pl.DataFrame = live.group_by("date").agg(
        pl.len().cast(pl.Int32).alias("n_stocks"),
        (pct > 0).sum().cast(pl.Int32).alias("n_up"),
        (pct < 0).sum().cast(pl.Int32).alias("n_down"),
        pl.col("is_limit_up").sum().cast(pl.Int32).alias("n_limit_up"),
        pl.col("is_limit_down").sum().cast(pl.Int32).alias("n_limit_down"),
        pl.col("is_broken").sum().cast(pl.Int32).alias("n_broken"),
        pl.col("streak").max().fill_null(0).cast(pl.Int32).alias("max_streak"),
        (pl.col("streak") >= 2).sum().cast(pl.Int32).alias("n_streak2"),
        (pct.filter(pl.col("prev_lu")).mean() * 100).alias("prev_lu_premium"),
        pl.col("is_limit_up").filter(pl.col("prev_lu")).cast(pl.Float64).mean().alias("prev_lu_promote"),
        (pct.filter(pl.col("prev_streak") >= 2).mean() * 100).alias("prev_streak_premium"),
        (pct.median() * 100).alias("median_pct"),
        pl.col("amount").sum().cast(pl.Float64).alias("amount_total"),
    )
    touched = pl.col("n_broken") + pl.col("n_limit_up")
    return out.with_columns(
        pl.when(touched > 0).then(pl.col("n_broken") / touched).otherwise(None).alias("break_rate"),
    ).sort("date")


def _rolling_rank(values: np.ndarray, window: int = TEMP_WINDOW, min_samples: int = TEMP_MIN_SAMPLES) -> np.ndarray:
    """每个位置的值在过去 window 个值（含自身）中的分位（0~1，并列取中间）"""
    n: int = len(values)
    out: np.ndarray = np.full(n, np.nan)
    for i in range(n):
        x: float = values[i]
        if np.isnan(x):
            continue
        w: np.ndarray = values[max(0, i - window + 1): i + 1]
        w = w[~np.isnan(w)]
        if len(w) < min_samples:
            continue
        out[i] = ((w < x).sum() + 0.5 * (w == x).sum()) / len(w)
    return out


def _finish(stats: pl.DataFrame) -> pl.DataFrame:
    """在按日汇总表上补 amount_ratio20 与 temperature（只用过去的数据）"""
    stats = stats.sort("date").with_columns(
        (pl.col("amount_total") / pl.col("amount_total").shift(1).rolling_mean(20, min_samples=5))
        .alias("amount_ratio20"),
        (pl.col("n_up") / pl.col("n_stocks")).alias("up_ratio"),
    )
    ranks: list[np.ndarray] = []
    for name, sign in _TEMP_PARTS:
        arr: np.ndarray = stats[name].cast(pl.Float64).fill_null(np.nan).to_numpy() * sign
        ranks.append(_rolling_rank(arr))
    mat: np.ndarray = np.vstack(ranks)
    valid: np.ndarray = ~np.isnan(mat)
    cnt: np.ndarray = valid.sum(axis=0)
    temp: np.ndarray = np.where(cnt > 0, np.nansum(mat, axis=0) / np.maximum(cnt, 1) * 100, np.nan)
    stats = stats.with_columns(pl.Series("temperature", temp).fill_nan(None).round(1))
    return stats.select([pl.col(c).cast(t) for c, t in SCHEMA.items()])


def daily_sentiment(panel_lim: pl.DataFrame) -> pl.DataFrame:
    """每个交易日一行的市场情绪（字段见 SCHEMA）。panel_lim 为 add_limit_columns 的结果"""
    if panel_lim.is_empty():
        return _empty()
    return _finish(_aggregate(_with_prev(panel_lim)))


def temperature_label(t: float | None) -> str:
    if t is None or (isinstance(t, float) and np.isnan(t)):
        return "未知"
    if t < 20:
        return "冰点"
    if t < 40:
        return "低迷"
    if t < 60:
        return "正常"
    if t < 80:
        return "活跃"
    return "过热"


def _row_dict(row: dict) -> dict:
    out: dict = {}
    for k, v in row.items():
        if isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, float):
            out[k] = None if np.isnan(v) else round(v, 4)
        else:
            out[k] = v
    out["label"] = temperature_label(row.get("temperature"))
    return out


def recent(days: int = 60) -> list[dict]:
    """最近 days 个交易日的情绪（给网页"近60日"图表），每行带 label"""
    from ..predict import service

    sent: pl.DataFrame = service.context().sentiment
    return [_row_dict(r) for r in sent.tail(days).iter_rows(named=True)]


def _snapshot_day(snap: pl.DataFrame) -> date | None:
    if "time" not in snap.columns or snap.is_empty():
        return None
    days = snap["time"].cast(pl.Utf8).str.slice(0, 10).drop_nulls()
    if days.is_empty():
        return None
    try:
        return datetime.strptime(days.mode().sort()[-1], "%Y-%m-%d").date()
    except ValueError:
        return None


def snapshot_rows(snap: pl.DataFrame, day: date, panel_lim: pl.DataFrame) -> pl.DataFrame:
    """把全市场实时快照换算成与 panel_lim 同口径的当日行（涨跌停价直接用交易所给的）。

    上一交易日状态取 panel_lim 最后一个交易日；面板里没有、或上市不满5个交易日的股票视为无涨跌幅限制。
    """
    last_day: date = panel_lim["date"].max()
    prev: pl.DataFrame = panel_lim.filter(pl.col("date") == last_day).select(
        "code", pl.col("is_limit_up").alias("prev_lu"), pl.col("streak").alias("_prev_streak"),
        pl.col("list_days").alias("_list_days"), pl.col("is_st"), pl.col("board"),
    )
    df: pl.DataFrame = snap.filter(
        (pl.col("price") > 0) & (pl.col("prev_close") > 0) & (pl.col("volume") > 0)
    ).select(
        pl.lit(day).alias("date"), pl.col("code").cast(pl.Utf8),
        pl.col("open").cast(pl.Float64), pl.col("high").cast(pl.Float64), pl.col("low").cast(pl.Float64),
        pl.col("price").cast(pl.Float64).alias("close"), pl.col("prev_close").cast(pl.Float64).alias("preclose"),
        pl.col("volume").cast(pl.Float64), pl.col("amount").cast(pl.Float64),
        pl.col("turnover").cast(pl.Float64).alias("turn") if "turnover" in snap.columns else pl.lit(None, pl.Float64)
        .alias("turn"),
        pl.col("limit_up").cast(pl.Float64), pl.col("limit_down").cast(pl.Float64),
        # 快照里的当前名称带 ST/退 → ST（面板里没有的新股也能认出来）
        (pl.col("name").cast(pl.Utf8).str.replace_all(r"\s+", "").str.to_uppercase().str.contains("ST|退")
         .fill_null(False) if "name" in snap.columns else pl.lit(False)).alias("_name_st"),
    ).join(prev, on="code", how="left")
    lu, ldn = pl.col("limit_up"), pl.col("limit_down")
    no_limit = (lu.is_null() | (lu <= 0) | pl.col("_list_days").is_null() | (pl.col("_list_days") < 5))
    df = df.with_columns(
        no_limit.alias("no_limit"),
        (pl.col("close") / pl.col("preclose") - 1).alias("pct"),
        pl.col("prev_lu").fill_null(False),
        pl.col("_prev_streak").fill_null(0).cast(pl.Int16).alias("prev_streak"),
        (pl.col("is_st").fill_null(False) | pl.col("_name_st")).alias("is_st"),
        (pl.col("_list_days").fill_null(0) + 1).cast(pl.Int32).alias("list_days"),
    )
    df = df.with_columns(
        (~pl.col("no_limit") & (pl.col("close") >= lu - TOLERANCE)).fill_null(False).alias("is_limit_up"),
        (~pl.col("no_limit") & (pl.col("high") >= lu - TOLERANCE)).fill_null(False).alias("touched_up"),
        (~pl.col("no_limit") & (pl.col("close") <= ldn + TOLERANCE)).fill_null(False).alias("is_limit_down"),
    )
    df = df.with_columns(
        (pl.col("touched_up") & ~pl.col("is_limit_up")).alias("is_broken"),
        (pl.col("is_limit_up") & (pl.col("open") >= lu - TOLERANCE) & (pl.col("low") >= lu - TOLERANCE))
        .fill_null(False).alias("one_word"),
        pl.when(pl.col("is_limit_up")).then(pl.col("prev_streak") + 1).otherwise(0).cast(pl.Int16).alias("streak"),
    )
    return df.drop(["_prev_streak", "_list_days", "_name_st"])


def live_sentiment() -> dict:
    """盘中/收盘后的当日情绪（腾讯全市场快照 + 历史面板）。字段同 daily_sentiment 一行，另加
    as_of（快照时间）、label、live（是否来自实时快照）、pool_limit_up（东财涨停池家数，拿不到为 None）。
    """
    from ..predict import service
    from . import universe as uni_mod

    ctx = service.context()
    hist: pl.DataFrame = ctx.sentiment
    if hist.is_empty():
        return {}
    snap: pl.DataFrame = pl.DataFrame()
    try:
        from . import realtime

        uni: pl.DataFrame = uni_mod.load_universe()
        codes: list[str] = uni.filter(pl.col("status") == 1)["code"].to_list()
        snap = realtime.snapshot_all(codes)
    except Exception:  # noqa: BLE001  快照失败时退回历史最后一行
        snap = pl.DataFrame()
    day: date | None = _snapshot_day(snap)
    last_day: date = hist["date"][-1]
    if day is None or day <= last_day:
        pick: pl.DataFrame = hist.filter(pl.col("date") == day) if day is not None else hist.tail(1)
        if pick.is_empty():
            pick = hist.tail(1)
        row: dict = _row_dict(pick.row(0, named=True))
        row["as_of"] = str(snap["time"].max()) if day is not None else row["date"]
        row["live"] = False
    else:
        today: pl.DataFrame = snapshot_rows(snap, day, ctx.panel_lim)
        stats: pl.DataFrame = _aggregate(today)
        base: pl.DataFrame = hist.tail(TEMP_WINDOW + 30).select(
            [c for c in stats.columns if c in hist.columns]
        )
        merged: pl.DataFrame = _finish(pl.concat([base, stats.select(base.columns)], how="vertical_relaxed"))
        row = _row_dict(merged.tail(1).row(0, named=True))
        row["as_of"] = str(snap["time"].max())
        row["live"] = True
    row["pool_limit_up"] = None
    try:
        from . import pools

        pool: pl.DataFrame = pools.fetch_pool("zt", datetime.strptime(row["date"], "%Y-%m-%d").date())
        row["pool_limit_up"] = pool.height if not pool.is_empty() else None
    except Exception:  # noqa: BLE001
        pass
    return row


def history_frame() -> pl.DataFrame:
    """供其他模块直接取历史情绪表（进程内缓存）"""
    from ..predict import service

    return service.context().sentiment

