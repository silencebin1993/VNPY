"""
五维特征工程：情绪面、资金面、基本面、题材政策面、技术面。

严禁未来函数：T 日特征只用 T 日及以前的行情（按股票 date 排序后的回看窗口）；
市场/行业统计只用 T 日截面；基本面按 avail_date ≤ T 做 as-of 连接；龙虎榜 T 日收盘后公布，可用。
已知的轻微偏差：行业用当前归属；ST 用当前名称近似全部历史（见 limits 模块说明）。

技术面收益率用"除权调整后"的价格指数：adj_t = ∏ close/preclose（preclose 已按除权除息调整），
所以跨除权日的涨幅不会出现假的大跌。

唯一用到"未来"的地方：kind="first" 训练时的负样本抽样需要知道次日是否涨停（正样本全留），
只决定哪些行进入训练集，不进入任何特征。
"""
from collections.abc import Callable
from datetime import date

import numpy as np
import polars as pl


GROUPS: list[str] = ["sentiment", "capital", "fundamental", "theme", "technical"]
GROUP_LABELS: dict[str, str] = {
    "sentiment": "情绪面", "capital": "资金面", "fundamental": "基本面", "theme": "题材政策面", "technical": "技术面",
}

FEATURE_GROUPS: dict[str, list[str]] = {
    "sentiment": [
        "m_limit_up", "m_limit_down", "m_break_rate", "m_max_streak", "m_streak2", "m_prev_lu_premium",
        "m_prev_lu_promote", "m_prev_streak_premium", "m_up_ratio", "m_median_pct", "m_amount_ratio20",
        "m_temp", "m_temp_chg5", "m_lu_chg5", "lvl_promote",
        "lb_streak", "lu_cnt20", "lu_cnt60", "brk_cnt20", "days_since_lu", "prev_broken",
    ],
    "capital": [
        "turnover", "turn_ma5", "turn_ratio20", "vol_ratio", "log_amount", "log_float_cap",
        "word_lu", "t_word", "close_at_high", "amplitude", "gap_open", "lower_shadow", "upper_shadow",
        "lhb_on", "lhb_net_ratio", "lhb_cnt5", "zt_first_min", "zt_open_times", "zt_seal_ratio",
    ],
    "fundamental": [
        "pe_ttm", "loss", "pb", "roe_ann", "rev_yoy", "np_yoy", "debt_ratio", "gross_margin",
        "st_flag", "limit_pct", "log_list_days", "price",
    ],
    "theme": ["ind_lu", "ind_lu_ratio", "ind_pct", "ind_ret5_rank", "ind_rank", "ind_lu_chg", "ind_max_streak"],
    "technical": [
        "chg", "ret_3", "ret_5", "ret_10", "ret_20", "ret_60", "dist_high20", "dist_high60", "pos60",
        "bias5", "bias10", "bias20", "bias60", "vol20",
    ],
}

FEATURE_LABELS: dict[str, str] = {
    "m_limit_up": "今天全市场涨停家数",
    "m_limit_down": "今天全市场跌停家数",
    "m_break_rate": "今天炸板率（冲上涨停又打开的比例）",
    "m_max_streak": "市场最高连板数",
    "m_streak2": "连板（2板及以上）股票家数",
    "m_prev_lu_premium": "昨天涨停的股票今天平均涨幅",
    "m_prev_lu_promote": "昨天涨停的股票今天继续涨停的比例",
    "m_prev_streak_premium": "昨天连板的股票今天平均涨幅",
    "m_up_ratio": "今天上涨股票占比",
    "m_median_pct": "今天全市场涨幅中位数",
    "m_amount_ratio20": "今天两市成交额与20日均值之比",
    "m_temp": "市场情绪温度",
    "m_temp_chg5": "情绪温度较5天前的变化",
    "m_lu_chg5": "涨停家数较5天前的变化",
    "lvl_promote": "今天同高度股票的晋级率",
    "lb_streak": "连板数",
    "lu_cnt20": "近20个交易日涨停次数",
    "lu_cnt60": "近60个交易日涨停次数",
    "brk_cnt20": "近20个交易日炸板次数",
    "days_since_lu": "距上一次涨停的交易日数",
    "prev_broken": "昨天是否炸板",
    "turnover": "换手率",
    "turn_ma5": "5日平均换手率",
    "turn_ratio20": "换手率与前20日均值之比",
    "vol_ratio": "量比（今天成交量/前5日均量）",
    "log_amount": "成交额",
    "log_float_cap": "流通市值",
    "word_lu": "一字涨停",
    "t_word": "T字板（开盘涨停、盘中打开又封回）",
    "close_at_high": "收盘在全天最高价",
    "amplitude": "振幅",
    "gap_open": "开盘跳空幅度",
    "lower_shadow": "下影线长度",
    "upper_shadow": "上影线长度",
    "lhb_on": "今天上了龙虎榜",
    "lhb_net_ratio": "龙虎榜净买入占成交额",
    "lhb_cnt5": "近5日上龙虎榜次数",
    "zt_first_min": "首次封板时间（开盘后第几分钟）",
    "zt_open_times": "盘中炸板次数",
    "zt_seal_ratio": "封板资金占流通市值",
    "pe_ttm": "市盈率(TTM)",
    "loss": "最近12个月亏损",
    "pb": "市净率",
    "roe_ann": "净资产收益率（年化）",
    "rev_yoy": "营收同比增长",
    "np_yoy": "净利润同比增长",
    "debt_ratio": "资产负债率",
    "gross_margin": "毛利率",
    "st_flag": "ST股",
    "limit_pct": "涨跌幅限制",
    "log_list_days": "上市时间",
    "price": "股价",
    "ind_lu": "所在行业今天涨停家数",
    "ind_lu_ratio": "所在行业涨停股占比",
    "ind_pct": "所在行业今天平均涨幅",
    "ind_ret5_rank": "所在行业近5日涨幅在全市场的排名",
    "ind_rank": "个股今天涨幅在行业内的排名",
    "ind_lu_chg": "所在行业涨停家数较昨天的变化",
    "ind_max_streak": "所在行业最高连板数",
    "chg": "今天涨幅",
    "ret_3": "近3日涨幅",
    "ret_5": "近5日涨幅",
    "ret_10": "近10日涨幅",
    "ret_20": "近20日涨幅",
    "ret_60": "近60日涨幅",
    "dist_high20": "距20日最高价",
    "dist_high60": "距60日最高价",
    "pos60": "股价在60日区间的位置",
    "bias5": "5日均线乖离",
    "bias10": "10日均线乖离",
    "bias20": "20日均线乖离",
    "bias60": "60日均线乖离",
    "vol20": "20日波动率",
}

ALL_FEATURES: list[str] = [f for g in GROUPS for f in FEATURE_GROUPS[g]]
FEATURE_TO_GROUP: dict[str, str] = {f: g for g in GROUPS for f in FEATURE_GROUPS[g]}
# 首板模型里恒为常数或恒为空的特征
STREAK_ONLY: set[str] = {"lb_streak", "word_lu", "t_word", "zt_first_min", "zt_open_times", "zt_seal_ratio"}
DISPLAY_COLUMNS: list[str] = ["close", "pct", "streak", "turn", "float_cap", "is_st", "board", "one_word", "industry"]
KINDS: tuple[str, ...] = ("streak", "first", "swing")
LABEL_GAP_DAYS: int = 10            # 下一根K线距今超过这么多自然日视为没有"次日"
TOLERANCE: float = 0.005
F32 = pl.Float32
SWING_MIN_PCT: float = 0.05         # 波段候选：当天涨幅下限（且收盘没涨停）
SWING_MIN_PRICE: float = 2.0        # 波段候选：收盘价下限
# 截面统计（行业、晋级率、行业内排名）需要的逐行列（分块计算时只保留这些）
SLIM_COLUMNS: list[str] = ["date", "code", "industry", "no_limit", "is_limit_up", "pct", "ret_5", "streak",
                           "_prev_level"]


def feature_columns(kind: str) -> list[str]:
    """该模型使用的特征列（按五维顺序）；波段模型与首板模型相同（不用只对涨停股有意义的特征）"""
    if kind in ("first", "swing"):
        return [f for f in ALL_FEATURES if f not in STREAK_ONLY]
    return list(ALL_FEATURES)


# ---------------------------------------------------------------- 可选数据源

def _load_lhb() -> pl.DataFrame:
    try:
        from ..market import pools

        return pools.load_lhb()
    except Exception:  # noqa: BLE001  龙虎榜模块缺失或数据损坏时不用
        return pl.DataFrame()


def _load_zt_pool() -> pl.DataFrame:
    try:
        from ..market import pools

        return pools.load_archive("zt")
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def _load_fund() -> pl.DataFrame:
    try:
        from ..market import fundamentals

        return fundamentals.load_fundamentals()
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def _prep_lhb(lhb: pl.DataFrame | None) -> pl.DataFrame | None:
    """龙虎榜按 (date, code) 汇总：net_buy 取平均（同一天多个上榜原因时数据重复）"""
    if lhb is None or lhb.is_empty() or not {"date", "code"}.issubset(lhb.columns):
        return None
    net = pl.col("net_buy").cast(pl.Float64) if "net_buy" in lhb.columns else pl.lit(None, pl.Float64)
    return (
        lhb.select(pl.col("date").cast(pl.Date), pl.col("code").cast(pl.Utf8).str.zfill(6), net.alias("_lhb_net"))
        .drop_nulls(["date", "code"])
        .group_by(["date", "code"]).agg(pl.col("_lhb_net").mean())
        .with_columns(pl.lit(1.0).alias("_lhb_on"))
    )


# ---------------------------------------------------------------- 逐行特征（全市场）

def compute_base(panel_lim: pl.DataFrame, universe: pl.DataFrame, lhb: pl.DataFrame | None = None,
                 calendar: list[date] | None = None) -> pl.DataFrame:
    """逐行（每只股票每天）计算只依赖该股历史的特征，以及后面做截面统计要用的列。

    lhb 为 None 时读取本地龙虎榜；传入空表表示不用龙虎榜。calendar：市场交易日（按股票分块计算时传入全市场日历；
    为空时用 panel_lim 里出现的日期）。注意 ind_rank 是行业内截面排名，分块计算时要在全市场上重算。
    """
    need: list[str] = [
        "date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "turn", "is_st", "board",
        "list_days", "no_limit", "pct", "limit_up", "is_limit_up", "is_broken", "one_word", "streak",
    ]
    df: pl.DataFrame = panel_lim.select([c for c in need if c in panel_lim.columns]).sort(["code", "date"])
    if "turn" not in df.columns:
        df = df.with_columns(pl.lit(None, pl.Float64).alias("turn"))
    if not universe.is_empty() and "industry" in universe.columns:
        ind: pl.DataFrame = universe.select("code", "industry").unique("code")
        df = df.join(ind, on="code", how="left", maintain_order="left")
    else:
        df = df.with_columns(pl.lit(None, pl.Utf8).alias("industry"))

    # 市场日历：上一交易日
    cal: pl.DataFrame = pl.DataFrame({"date": pl.Series(sorted(calendar), dtype=pl.Date) if calendar is not None
                                      else df["date"].unique().sort()})
    cal = cal.with_columns(pl.col("date").shift(1).alias("_cal_prev"))
    df = df.join(cal, on="date", how="left", maintain_order="left")

    lhb_t: pl.DataFrame | None = _prep_lhb(_load_lhb() if lhb is None else lhb)
    if lhb_t is not None:
        lhb_start: date = lhb_t["date"].min()
        df = df.join(lhb_t, on=["date", "code"], how="left", maintain_order="left").with_columns(
            pl.when(pl.col("date") >= lhb_start).then(pl.col("_lhb_on").fill_null(0.0)).otherwise(None)
            .alias("_lhb_on")
        )
    else:
        df = df.with_columns(pl.lit(None, pl.Float64).alias("_lhb_on"), pl.lit(None, pl.Float64).alias("_lhb_net"))

    code = "code"
    ratio = pl.when(pl.col("preclose") > 0).then(pl.col("close") / pl.col("preclose")).otherwise(1.0)
    turn_ok = pl.when(pl.col("turn") > 0).then(pl.col("turn")).otherwise(None)
    df = df.with_columns(
        ratio.cum_prod().over(code).alias("_adj"),
        (pl.when(pl.col("preclose") > 0).then(ratio - 1).otherwise(None)).alias("_ret"),
        pl.int_range(pl.len()).over(code).alias("_i"),
        (pl.col("volume") / (turn_ok / 100)).alias("_fs"),
        pl.col("is_limit_up").cast(pl.Int32).alias("_lu"),
        pl.col("date").shift(1).over(code).alias("_prev_date"),
    )
    df = df.with_columns(
        (pl.col("high") * pl.col("_adj") / pl.col("close")).alias("_ah"),
        (pl.col("low") * pl.col("_adj") / pl.col("close")).alias("_al"),
        pl.col("_fs").rolling_median(10, min_samples=1).over(code).alias("_fs_med"),
    )
    adj = pl.col("_adj")
    pre = pl.col("preclose")
    rolling: list[pl.Expr] = [
        *[(adj / adj.shift(n).over(code) - 1).alias(f"ret_{n}") for n in (3, 5, 10, 20, 60)],
        (adj / pl.col("_ah").rolling_max(20, min_samples=1).over(code) - 1).alias("dist_high20"),
        (adj / pl.col("_ah").rolling_max(60, min_samples=1).over(code) - 1).alias("dist_high60"),
        pl.col("_ah").rolling_max(60, min_samples=1).over(code).alias("_hh60"),
        pl.col("_al").rolling_min(60, min_samples=1).over(code).alias("_ll60"),
        *[(adj / adj.rolling_mean(n, min_samples=max(2, n // 2)).over(code) - 1).alias(f"bias{n}")
          for n in (5, 10, 20, 60)],
        pl.col("_ret").rolling_std(20, min_samples=10).over(code).alias("vol20"),
        pl.col("_lu").rolling_sum(5, min_samples=1).over(code).alias("_lu_cnt5"),
        pl.col("_lu").rolling_sum(20, min_samples=1).over(code).alias("lu_cnt20"),
        pl.col("_lu").rolling_sum(60, min_samples=1).over(code).alias("lu_cnt60"),
        pl.col("is_broken").cast(pl.Int32).rolling_sum(20, min_samples=1).over(code).alias("brk_cnt20"),
        pl.when(pl.col("is_limit_up")).then(pl.col("_i")).shift(1).forward_fill().over(code).alias("_last_lu"),
        pl.col("is_broken").shift(1).over(code).cast(pl.Float64).alias("prev_broken"),
        pl.col("streak").shift(1).over(code).alias("_prev_streak"),
        pl.col("turn").rolling_mean(5, min_samples=1).over(code).alias("turn_ma5"),
        (pl.col("turn") / turn_ok.shift(1).rolling_mean(20, min_samples=5).over(code)).alias("turn_ratio20"),
        (pl.col("volume") / pl.col("volume").shift(1).rolling_mean(5, min_samples=3).over(code)).alias("vol_ratio"),
        pl.col("_lhb_on").rolling_sum(5, min_samples=1).over(code).alias("lhb_cnt5"),
        # 仅用于首板模型负样本抽样（不是特征）
        (pl.col("is_limit_up").shift(-1).over(code)
         & ((pl.col("date").shift(-1).over(code) - pl.col("date")).dt.total_days() <= LABEL_GAP_DAYS))
        .fill_null(False).alias("_next_lu"),
    ]
    df = df.with_columns(rolling)
    lu_price = pl.col("limit_up")
    float_cap = pl.col("close") * pl.col("_fs_med") / 1e8           # 亿
    prev_ok = (pl.col("_prev_date") == pl.col("_cal_prev")).fill_null(False)
    df = df.with_columns(
        pl.col("pct").alias("chg"),
        ((adj - pl.col("_ll60")) / (pl.col("_hh60") - pl.col("_ll60"))).fill_nan(None).alias("pos60"),
        (pl.col("_i") - pl.col("_last_lu")).clip(upper_bound=61).fill_null(61).alias("days_since_lu"),
        pl.col("streak").cast(pl.Float64).alias("lb_streak"),
        pl.col("turn").alias("turnover"),
        (pl.col("amount") + 1).log().alias("log_amount"),
        float_cap.alias("float_cap"),
        float_cap.log().alias("log_float_cap"),
        pl.col("one_word").cast(pl.Float64).alias("word_lu"),
        (pl.col("is_limit_up") & (pl.col("open") >= lu_price - TOLERANCE) & (pl.col("low") < lu_price - TOLERANCE))
        .fill_null(False).cast(pl.Float64).alias("t_word"),
        (pl.col("close") >= pl.col("high") - TOLERANCE).cast(pl.Float64).alias("close_at_high"),
        ((pl.col("high") - pl.col("low")) / pre).alias("amplitude"),
        (pl.col("open") / pre - 1).alias("gap_open"),
        ((pl.min_horizontal("open", "close") - pl.col("low")) / pre).alias("lower_shadow"),
        ((pl.col("high") - pl.max_horizontal("open", "close")) / pre).alias("upper_shadow"),
        pl.col("_lhb_on").alias("lhb_on"),
        (pl.col("_lhb_net") / pl.col("amount")).alias("lhb_net_ratio"),
        pl.col("is_st").cast(pl.Float64).alias("st_flag"),
        ((pl.col("limit_up") / pre - 1) * 100).round(0).alias("limit_pct"),
        pl.col("list_days").cast(pl.Float64).log().alias("log_list_days"),
        pl.col("close").alias("price"),
        pl.when(pl.col("industry").is_not_null())
        .then(pl.col("pct").rank("average").over(["date", "industry"]) / pl.len().over(["date", "industry"]))
        .otherwise(None).alias("ind_rank"),
        pl.when(prev_ok).then(pl.col("_prev_streak").clip(upper_bound=5)).otherwise(None).alias("_prev_level"),
    )
    drop: list[str] = [
        "_adj", "_ret", "_i", "_fs", "_lu", "_prev_date", "_ah", "_al", "_fs_med", "_hh60", "_ll60", "_last_lu",
        "_prev_streak", "_cal_prev", "_lhb_on", "_lhb_net", "open", "high", "low", "preclose", "volume",
        "amount", "limit_up", "is_broken", "list_days",
    ]
    df = df.drop([c for c in drop if c in df.columns])
    # 特征列统一 Float32，节省内存
    return df.with_columns([pl.col(c).cast(F32) for c in df.columns if c in FEATURE_TO_GROUP or c == "ret_5"])


# ---------------------------------------------------------------- 截面统计

def _sentiment_features(sentiment: pl.DataFrame) -> pl.DataFrame:
    s: pl.DataFrame = sentiment.sort("date")
    return s.select(
        "date",
        pl.col("n_limit_up").alias("m_limit_up"),
        pl.col("n_limit_down").alias("m_limit_down"),
        pl.col("break_rate").alias("m_break_rate"),
        pl.col("max_streak").alias("m_max_streak"),
        pl.col("n_streak2").alias("m_streak2"),
        pl.col("prev_lu_premium").alias("m_prev_lu_premium"),
        pl.col("prev_lu_promote").alias("m_prev_lu_promote"),
        pl.col("prev_streak_premium").alias("m_prev_streak_premium"),
        (pl.col("n_up") / pl.col("n_stocks")).alias("m_up_ratio"),
        pl.col("median_pct").alias("m_median_pct"),
        pl.col("amount_ratio20").alias("m_amount_ratio20"),
        pl.col("temperature").alias("m_temp"),
        (pl.col("temperature") - pl.col("temperature").shift(5)).alias("m_temp_chg5"),
        (pl.col("n_limit_up") - pl.col("n_limit_up").shift(5)).cast(pl.Float64).alias("m_lu_chg5"),
    )


def _industry_features(base: pl.DataFrame) -> pl.DataFrame:
    live: pl.DataFrame = base.filter(~pl.col("no_limit") & pl.col("industry").is_not_null())
    t: pl.DataFrame = live.group_by(["date", "industry"]).agg(
        pl.len().alias("_n"),
        pl.col("is_limit_up").sum().cast(pl.Float64).alias("ind_lu"),
        pl.col("pct").mean().alias("ind_pct"),
        pl.col("ret_5").mean().alias("_ind_ret5"),
        pl.col("streak").max().cast(pl.Float64).alias("ind_max_streak"),
    ).sort(["industry", "date"])
    return t.with_columns(
        (pl.col("ind_lu") / pl.col("_n")).alias("ind_lu_ratio"),
        (pl.col("ind_lu") - pl.col("ind_lu").shift(1).over("industry")).alias("ind_lu_chg"),
        (pl.col("_ind_ret5").rank("average").over("date") / pl.col("_ind_ret5").count().over("date"))
        .alias("ind_ret5_rank"),
    ).drop(["_n", "_ind_ret5"])


def _level_features(base: pl.DataFrame) -> pl.DataFrame:
    """今天各高度的晋级率：昨天 s 板（0 表示昨天没涨停，5 表示 5 板及以上）的股票今天涨停的比例"""
    live: pl.DataFrame = base.filter(~pl.col("no_limit") & pl.col("_prev_level").is_not_null())
    return live.group_by(["date", "_prev_level"]).agg(
        pl.col("is_limit_up").cast(pl.Float64).mean().alias("lvl_promote")
    ).rename({"_prev_level": "_level"}).with_columns(pl.col("_level").cast(F32))


def _fundamental_features(cand: pl.DataFrame, fund: pl.DataFrame | None) -> pl.DataFrame:
    names: list[str] = FEATURE_GROUPS["fundamental"][:8]
    if fund is None or fund.is_empty():
        return pl.DataFrame({n: pl.Series(n, [None] * cand.height, dtype=pl.Float64) for n in names})
    from ..market.fundamentals import asof_join

    keep: list[str] = [c for c in ["code", "report_date", "avail_date", "eps_ttm", "bvps", "roe", "revenue_yoy",
                                   "net_profit_yoy", "debt_ratio", "gross_margin"] if c in fund.columns]
    j: pl.DataFrame = asof_join(cand.select("date", "code", "close"), fund.select(keep))
    for c in ["eps_ttm", "bvps", "roe", "revenue_yoy", "net_profit_yoy", "debt_ratio", "gross_margin"]:
        if c not in j.columns:
            j = j.with_columns(pl.lit(None, pl.Float64).alias(c))
    if "report_date" not in j.columns:
        j = j.with_columns(pl.lit(None, pl.Date).alias("report_date"))
    eps = pl.col("eps_ttm")
    return j.select(
        pl.when(eps > 0).then(pl.col("close") / eps).otherwise(None).alias("pe_ttm"),
        pl.when(eps.is_null()).then(None).otherwise((eps <= 0).cast(pl.Float64)).alias("loss"),
        pl.when(pl.col("bvps") > 0).then(pl.col("close") / pl.col("bvps")).otherwise(None).alias("pb"),
        (pl.col("roe") * 12 / pl.col("report_date").dt.month()).alias("roe_ann"),
        pl.col("revenue_yoy").alias("rev_yoy"),
        pl.col("net_profit_yoy").alias("np_yoy"),
        pl.col("debt_ratio"),
        pl.col("gross_margin"),
    )


def _minutes_after_open(expr: pl.Expr) -> pl.Expr:
    """"09:35:12"/"093512"/93512 → 开盘后分钟数（午休不计，集合竞价封板记 0）"""
    s = expr.cast(pl.Utf8).str.replace_all(":", "").str.zfill(6)
    t = s.str.slice(0, 2).cast(pl.Int32, strict=False) * 60 + s.str.slice(2, 2).cast(pl.Int32, strict=False)
    return pl.when(t <= 11 * 60 + 30).then(t - (9 * 60 + 30)).otherwise(120 + t - 13 * 60).clip(lower_bound=0)


def _prep_zt(zt: pl.DataFrame | None) -> pl.DataFrame | None:
    if zt is None or zt.is_empty() or not {"date", "code"}.issubset(zt.columns):
        return None
    cols: list[pl.Expr] = [pl.col("date").cast(pl.Date), pl.col("code").cast(pl.Utf8).str.zfill(6)]
    cols.append(_minutes_after_open(pl.col("first_time")).cast(pl.Float64).alias("zt_first_min")
                if "first_time" in zt.columns else pl.lit(None, pl.Float64).alias("zt_first_min"))
    cols.append(pl.col("open_times").cast(pl.Float64, strict=False).alias("zt_open_times")
                if "open_times" in zt.columns else pl.lit(None, pl.Float64).alias("zt_open_times"))
    cols.append(pl.col("seal_amount").cast(pl.Float64, strict=False).alias("_seal")
                if "seal_amount" in zt.columns else pl.lit(None, pl.Float64).alias("_seal"))
    return zt.select(cols).drop_nulls(["date", "code"]).unique(["date", "code"], keep="last")


# ---------------------------------------------------------------- 候选与组装

def candidate_mask(kind: str) -> pl.Expr:
    """候选股票条件（在 compute_base 的结果上）。

    streak：当日收盘涨停；first：当日未涨停且近5日没涨停过；swing：当日涨幅 ≥5% 但收盘没涨停、非 ST（含名称带"退"）、
    非北交所、收盘价 ≥2 元、有成交。三者都排除新股无涨跌幅限制期。"""
    if kind == "streak":
        return pl.col("is_limit_up") & ~pl.col("no_limit")
    if kind == "first":
        return ~pl.col("is_limit_up") & ~pl.col("no_limit") & (pl.col("_lu_cnt5") == 0)
    if kind == "swing":
        return (
            (pl.col("pct") >= SWING_MIN_PCT) & ~pl.col("is_limit_up") & ~pl.col("no_limit")
            & ~pl.col("is_st").fill_null(False) & (pl.col("board") != "bj")
            & (pl.col("close") >= SWING_MIN_PRICE) & (pl.col("log_amount") > 0)
        ).fill_null(False)
    raise ValueError(f"未知的预测类型：{kind}")


def build_features(
    panel_lim: pl.DataFrame,
    universe: pl.DataFrame,
    sentiment: pl.DataFrame,
    kind: str,
    dates: list[date] | None = None,
    neg_sample: float | None = None,
    seed: int = 7,
    base: pl.DataFrame | None = None,
    fund: pl.DataFrame | None = None,
    lhb: pl.DataFrame | None = None,
    zt_pool: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """候选股票的特征表：date, code + 全部特征(Float32) + 展示列 close, pct, streak, turn, float_cap(亿),
    is_st, board, one_word, industry。按 date, code 排序。

    - kind="streak"：当日涨停（非无涨跌幅限制期）的股票；kind="first"：当日未涨停且近5日没涨停过的股票；
      kind="swing"：当日涨 ≥5% 未涨停的非 ST、非北交所股票（见 candidate_mask）；
    - neg_sample（仅 first 训练用）：负样本按此比例确定性抽样（按 date+code 哈希），正样本全留；
    - base：compute_base 的结果（可复用以免重复计算）；fund/lhb/zt_pool 为 None 时读本地文件，传空表表示不用。
    """
    if base is None:
        base = compute_base(panel_lim, universe, lhb)
    cand: pl.DataFrame = base.filter(candidate_mask(kind))
    if dates is not None:
        cand = cand.filter(pl.col("date").is_in(list(dates)))
    if neg_sample is not None and neg_sample < 1:
        u = (pl.struct(["date", "code"]).hash(seed) % 1_000_003).cast(pl.Float64) / 1_000_003
        cand = cand.filter(pl.col("_next_lu") | (u < neg_sample))
    return _assemble(cand.sort(["date", "code"]), base, sentiment, fund, zt_pool)


def build_features_chunked(
    panel_lim: pl.DataFrame,
    universe: pl.DataFrame,
    sentiment: pl.DataFrame,
    kind: str,
    start: date | None = None,
    n_chunks: int = 4,
    fund: pl.DataFrame | None = None,
    lhb: pl.DataFrame | None = None,
    zt_pool: pl.DataFrame | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> pl.DataFrame:
    """与 build_features(kind) 结果相同（不抽样），但按股票分 n_chunks 块计算逐行特征，峰值内存约为全量的几分之一。
    start：只保留这一天及以后的候选。截面统计（行业、晋级率、行业内排名）在全市场的精简列上计算。"""
    lhb_df: pl.DataFrame = _load_lhb() if lhb is None else lhb
    calendar: list[date] = panel_lim["date"].unique().sort().to_list()
    codes: list[str] = panel_lim["code"].unique().sort().to_list()
    n_chunks = max(1, min(n_chunks, len(codes) or 1))
    parts: list[pl.DataFrame] = []
    slims: list[pl.DataFrame] = []
    for i in range(n_chunks):
        if progress:
            progress(i / n_chunks, f"正在计算逐行特征（第 {i + 1}/{n_chunks} 块）…")
        chunk: list[str] = codes[i::n_chunks]
        base: pl.DataFrame = compute_base(panel_lim.filter(pl.col("code").is_in(chunk)), universe, lhb_df, calendar)
        slims.append(base.select(SLIM_COLUMNS))
        cand: pl.DataFrame = base.filter(candidate_mask(kind))
        if start is not None:
            cand = cand.filter(pl.col("date") >= start)
        parts.append(cand)
        del base
    slim: pl.DataFrame = pl.concat(slims)
    del slims
    cand = pl.concat(parts, how="vertical_relaxed").sort(["date", "code"])
    del parts
    # 行业内涨幅排名要在全市场截面上算
    ir: pl.DataFrame = slim.select(
        "date", "code",
        pl.when(pl.col("industry").is_not_null())
        .then(pl.col("pct").rank("average").over(["date", "industry"]) / pl.len().over(["date", "industry"]))
        .otherwise(None).cast(F32).alias("ind_rank"),
    )
    cand = cand.drop("ind_rank").join(ir, on=["date", "code"], how="left", maintain_order="left")
    del ir
    if progress:
        progress(1.0, f"候选 {cand.height:,} 条，正在组装五维特征…")
    return _assemble(cand, slim, sentiment, fund, zt_pool)


def _assemble(cand: pl.DataFrame, base: pl.DataFrame, sentiment: pl.DataFrame, fund: pl.DataFrame | None,
              zt_pool: pl.DataFrame | None) -> pl.DataFrame:
    """候选行（compute_base 的行）+ 截面统计 → 输出特征表。base 只需要 SLIM_COLUMNS"""
    # 情绪面（市场）、晋级率
    cand = cand.join(_sentiment_features(sentiment), on="date", how="left", maintain_order="left")
    level: pl.DataFrame = _level_features(base)
    cand = cand.with_columns(pl.col("streak").cast(F32).clip(upper_bound=5).alias("_level")).join(
        level, on=["date", "_level"], how="left", maintain_order="left"
    )
    # 题材（行业）
    cand = cand.join(_industry_features(base), on=["date", "industry"], how="left", maintain_order="left")
    # 基本面（as-of）
    fund_df: pl.DataFrame | None = _load_fund() if fund is None else fund
    cand = cand.hstack(_fundamental_features(cand, fund_df))
    # 涨停池存档（只有存档开始后才有）
    zt: pl.DataFrame | None = _prep_zt(_load_zt_pool() if zt_pool is None else zt_pool)
    if zt is not None:
        cand = cand.join(zt, on=["date", "code"], how="left", maintain_order="left").with_columns(
            (pl.col("_seal") / (pl.col("float_cap") * 1e8)).alias("zt_seal_ratio")
        ).drop("_seal")
    for name in ALL_FEATURES:
        if name not in cand.columns:
            cand = cand.with_columns(pl.lit(None, F32).alias(name))

    out: pl.DataFrame = cand.select(
        "date", "code",
        *[pl.col(f).cast(F32).fill_nan(None) for f in ALL_FEATURES],
        pl.col("close").cast(pl.Float64),
        pl.col("pct").cast(pl.Float64),
        pl.col("streak").cast(pl.Int16),
        pl.col("turn").cast(pl.Float64),
        pl.col("float_cap").cast(pl.Float64),
        pl.col("is_st").cast(pl.Boolean),
        pl.col("board").cast(pl.Utf8),
        pl.col("one_word").cast(pl.Boolean),
        pl.col("industry").cast(pl.Utf8),
    )
    return out


def feature_matrix(df: pl.DataFrame, feature_cols: list[str]) -> np.ndarray:
    """特征矩阵（float32，空值为 NaN）"""
    for c in feature_cols:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, F32).alias(c))
    return df.select([pl.col(c).cast(F32) for c in feature_cols]).to_numpy()
