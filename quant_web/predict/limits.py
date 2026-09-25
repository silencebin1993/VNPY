"""
涨跌停规则、涨停识别、连板数。

涨跌幅限制（已用本机下载的真实K线逐条核实，见 tests/test_quant_web_limits.py 与构建报告）：
- 主板 10%；主板 ST/*ST 5%，2026-07-06 起与非ST相同为 10%（约定文档写的 2025-07-07 与数据不符：
  当前 ST 股在 2026-07-03 前每天有数十只收在 ±5% 封板价，07-06 起消失、超过 5% 的涨跌幅骤增；
  腾讯快照给出的当日 ST 涨停价也是昨收×1.10）；
- 创业板 20%（2020-08-24 起，之前 10%，ST 5%）；科创板 20%；北交所 30%。
- 新股无涨跌幅限制期（no_limit）：注册制上市前5个交易日（科创板；创业板 2020-08-24 起；
  主板 2023-04-10 全面注册制起）；其余（旧规则主板/创业板、北交所）上市首日；另外，长期停牌后恢复上市首日、
  退市整理期首日也不设涨跌幅——只在收盘价确实超出正常涨跌停范围时才标记（由数据识别）。
涨停价 = 昨收 × (1 + 比例)，四舍五入到分（ROUND_HALF_UP）；北交所涨停价向下、跌停价向上取整到分；
名称以 S 开头的未股改股票（S佳通）5%。以上均与 2026-09-24 腾讯快照中交易所给出的全市场涨跌停价逐只核对。
价格比较容差 0.005 元。

ST 近似：面板 is_st 是"当前名称"应用于全部历史（名称含"退"的已退市股票也算 ST：它们退市前都是 *ST）。对 2026-07-06 前的主板（及改革前创业板），若该股当日或
此前20个交易日内出现过 5% 限制下不可能出现的收盘价，说明当时并非 ST，按 10% 处理（只用当日及以前的数据）。
"""
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

import polars as pl


CHINEXT_REFORM: date = date(2020, 8, 24)      # 创业板注册制：涨跌幅 10% → 20%
ST_MAIN_10PCT: date = date(2026, 7, 6)        # 主板 ST 涨跌幅 5% → 10%（数据核实，见模块说明）
MAIN_REGISTRATION: date = date(2023, 4, 10)   # 主板全面注册制首批上市，新股前5日不设涨跌幅
TOLERANCE: float = 0.005
ST_WINDOW: int = 20                           # ST 近似修正回看天数
RESUME_GAP_DAYS: int = 30                     # 停牌超过这么多自然日后复牌（恢复上市）首日可能不设涨跌幅
DELIST_WINDOW: int = 35                       # 已退市股票最后这么多根K线内（退市整理期）首日不设涨跌幅


def _board(code: str) -> str:
    from ..market.universe import board_of

    return board_of(code)


def limit_ratio(code: str, is_st: bool, day: date) -> float:
    """涨跌幅限制比例（0.1 表示 10%）"""
    board: str = _board(code)
    if board == "bj":
        return 0.30
    if board == "star":
        return 0.20
    if board == "chinext":
        if day >= CHINEXT_REFORM:
            return 0.20
        return 0.05 if is_st else 0.10
    if is_st and day < ST_MAIN_10PCT:
        return 0.05
    return 0.10


def limit_price(preclose: float, ratio: float, board: str = "main") -> float:
    """涨停价 = 昨收×(1+ratio)，四舍五入到分；跌停价传入负的 ratio。

    北交所例外：涨停价向下取整、跌停价向上取整到分（与交易所/腾讯快照给出的涨跌停价逐只核对一致）。
    """
    value: Decimal = Decimal(str(preclose)) * (Decimal(1) + Decimal(str(ratio)))
    if board == "bj":
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_FLOOR if ratio > 0 else ROUND_CEILING))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def no_limit_days(board: str, list_date: date | None) -> int:
    """上市后前几个交易日不设涨跌幅限制"""
    if list_date is None:
        return 1
    if board == "star":
        return 5
    if board == "chinext":
        return 5 if list_date >= CHINEXT_REFORM else 1
    if board == "main":
        return 5 if list_date >= MAIN_REGISTRATION else 1
    return 1


def _round2(expr: pl.Expr) -> pl.Expr:
    """向量化的四舍五入到分（ROUND_HALF_UP），加极小量抵消二进制浮点误差"""
    return ((expr * 100 + 0.5 + 1e-6).floor() / 100).round(2)


def _floor2(expr: pl.Expr) -> pl.Expr:
    return ((expr * 100 + 1e-6).floor() / 100).round(2)


def _ceil2(expr: pl.Expr) -> pl.Expr:
    return ((expr * 100 - 1e-6).ceil() / 100).round(2)


def _board_expr() -> pl.Expr:
    code = pl.col("code")
    return (
        pl.when(code.str.starts_with("4") | code.str.starts_with("8") | code.str.starts_with("92")).then(pl.lit("bj"))
        .when(code.str.starts_with("688") | code.str.starts_with("689")).then(pl.lit("star"))
        .when(code.str.starts_with("30")).then(pl.lit("chinext"))
        .otherwise(pl.lit("main"))
    )


def add_limit_columns(panel: pl.DataFrame, universe: pl.DataFrame) -> pl.DataFrame:
    """在日线面板上增加涨跌停相关列（见 ARCHITECTURE 3.4）。panel 需含 date, code, open, high, low, close, preclose"""
    df: pl.DataFrame = panel.sort(["code", "date"])
    if "is_st" not in df.columns:
        df = df.with_columns(pl.lit(False).alias("is_st"))
    if "board" in df.columns:
        df = df.drop("board")
    df = df.with_columns(_board_expr().alias("board"))

    # 上市日期与上市天数
    has_uni: bool = not universe.is_empty() and "list_date" in universe.columns
    uni_cols: pl.DataFrame = (
        universe.select([
            "code", pl.col("list_date").alias("_list_date"),
            (pl.col("status") == 0).fill_null(False).alias("_delisted") if "status" in universe.columns
            else pl.lit(False).alias("_delisted"),
            # 名称以 S 开头（未完成股改，如"S佳通"）：涨跌幅 5%
            (pl.col("name").str.starts_with("S") & ~pl.col("name").str.contains("ST")).fill_null(False)
            .alias("_s_share") if "name" in universe.columns else pl.lit(False).alias("_s_share"),
            pl.col("name").str.contains("退").fill_null(False).alias("_tui") if "name" in universe.columns
            else pl.lit(False).alias("_tui"),
        ])
        if has_uni
        else pl.DataFrame(schema={"code": pl.Utf8, "_list_date": pl.Date, "_delisted": pl.Boolean,
                                  "_s_share": pl.Boolean, "_tui": pl.Boolean})
    )
    df = df.join(uni_cols.unique("code"), on="code", how="left").with_columns(
        pl.col("_delisted").fill_null(False), pl.col("_s_share").fill_null(False),
        (pl.col("is_st").fill_null(False) | pl.col("_tui").fill_null(False)).alias("is_st"),
    )
    cal: pl.DataFrame = pl.DataFrame({"date": df["date"].unique().sort()}).with_row_index("_td", offset=1)
    cal_start: date | None = cal["date"][0] if cal.height else None
    df = df.join(cal.with_columns(pl.col("_td").cast(pl.Int64)), on="date", how="left")
    # list_date 对应的交易日序号：第一个 >= list_date 的交易日
    ld: pl.DataFrame = (
        df.select(["code", "_list_date"]).unique("code").drop_nulls("_list_date").sort("_list_date")
        .join_asof(cal.rename({"date": "_list_date", "_td": "_ld_td"}).with_columns(pl.col("_ld_td").cast(pl.Int64))
                   .sort("_list_date"), on="_list_date", strategy="forward")
    )
    df = df.join(ld.select(["code", "_ld_td"]), on="code", how="left")
    first_seen = pl.col("date").min().over("code")
    est_before_panel = (
        pl.when(pl.col("_list_date").is_not_null() & (pl.col("_list_date") < pl.lit(cal_start)))
        .then(((pl.lit(cal_start) - pl.col("_list_date")).dt.total_days() * 243 / 365).round(0).cast(pl.Int64))
        .otherwise(None)
    )
    df = df.with_columns(
        pl.when(pl.col("_ld_td").is_not_null() & pl.col("_list_date").is_not_null()
                & (pl.col("_list_date") >= pl.lit(cal_start)))
        .then(pl.col("_td") - pl.col("_ld_td") + 1)
        .when(est_before_panel.is_not_null())
        .then(pl.col("_td") + est_before_panel)
        # 不知道上市日期：按面板首次出现以来的日历天数估算，再加一年（多为 2018 年以前上市的老股票）
        .otherwise(((pl.col("date") - first_seen).dt.total_days() * 243 / 365).round(0).cast(pl.Int64) + 250)
        .clip(lower_bound=1)
        .cast(pl.Int32)
        .alias("list_days")
    )

    # 新股无涨跌幅限制期
    n_free = (
        pl.when(pl.col("board") == "star").then(5)
        .when((pl.col("board") == "chinext") & (pl.col("_list_date") >= pl.lit(CHINEXT_REFORM))).then(5)
        .when((pl.col("board") == "main") & (pl.col("_list_date") >= pl.lit(MAIN_REGISTRATION))).then(5)
        .otherwise(1)
    )
    # 其他不设涨跌幅的日子：长期停牌后恢复上市首日、退市整理期首日。只认"收盘超出正常涨跌停范围"的那一天
    normal = (
        pl.when(pl.col("board") == "bj").then(0.30)
        .when((pl.col("board") == "star") | ((pl.col("board") == "chinext") & (pl.col("date") >= pl.lit(CHINEXT_REFORM))))
        .then(0.20).otherwise(0.10)
    )
    beyond = (
        (pl.col("close") > _round2(pl.col("preclose") * (1 + normal)) + TOLERANCE)
        | (pl.col("close") < _round2(pl.col("preclose") * (1 - normal)) - TOLERANCE)
    ).fill_null(False)
    # 面板里的第一行若有昨收，说明上一根K线在面板起点之前（至少停牌到了面板起点）
    gap_days = (pl.col("date") - pl.coalesce(pl.col("date").shift(1).over("code"), pl.lit(cal_start))).dt.total_days()
    from_end = pl.len().over("code") - pl.int_range(pl.len()).over("code")
    special = beyond & (
        (gap_days > RESUME_GAP_DAYS).fill_null(False)
        | (pl.col("_delisted") & (from_end <= DELIST_WINDOW))
    )
    df = df.with_columns(
        (
            pl.col("preclose").is_null()
            | (pl.col("_list_date").is_not_null() & (pl.col("list_days") <= n_free))
            | special
        ).alias("no_limit"),
    )

    # ST 近似修正 + 涨跌幅比例
    pct = pl.col("close") / pl.col("preclose") - 1
    df = df.with_columns(pct.alias("pct"))
    # 当日收盘超出了 5% 限制所允许的价格范围 → 当时不可能是 5% 限制
    big_move = (
        (pl.col("close") > _round2(pl.col("preclose") * 1.05) + TOLERANCE)
        | (pl.col("close") < _round2(pl.col("preclose") * 0.95) - TOLERANCE)
    ) & ~pl.col("no_limit")
    df = df.with_columns(
        big_move.cast(pl.Int8).fill_null(0).rolling_max(window_size=ST_WINDOW + 1, min_samples=1).over("code")
        .cast(pl.Boolean).alias("_big")
    )
    st_eff = pl.col("is_st").fill_null(False) & ~pl.col("_big")
    ratio = (
        pl.when(pl.col("board") == "bj").then(0.30)
        .when(pl.col("board") == "star").then(0.20)
        .when(pl.col("board") == "chinext").then(
            pl.when(pl.col("date") >= pl.lit(CHINEXT_REFORM)).then(0.20)
            .when(st_eff).then(0.05).otherwise(0.10))
        .when(st_eff & (pl.col("date") < pl.lit(ST_MAIN_10PCT))).then(0.05)
        .when(pl.col("_s_share")).then(0.05)
        .otherwise(0.10)
    )
    df = df.with_columns(ratio.alias("_ratio"))
    pre = pl.col("preclose")
    up, down = pre * (1 + pl.col("_ratio")), pre * (1 - pl.col("_ratio"))
    is_bj = pl.col("board") == "bj"
    df = df.with_columns(
        pl.when(pl.col("no_limit")).then(None).when(is_bj).then(_floor2(up)).otherwise(_round2(up)).alias("limit_up"),
        pl.when(pl.col("no_limit")).then(None).when(is_bj).then(_ceil2(down)).otherwise(_round2(down))
        .alias("limit_down"),
    )
    lu, ldn = pl.col("limit_up"), pl.col("limit_down")
    df = df.with_columns(
        (pl.col("close") >= lu - TOLERANCE).fill_null(False).alias("is_limit_up"),
        (pl.col("high") >= lu - TOLERANCE).fill_null(False).alias("touched_up"),
        (pl.col("close") <= ldn + TOLERANCE).fill_null(False).alias("is_limit_down"),
    )
    df = df.with_columns(
        (pl.col("touched_up") & ~pl.col("is_limit_up")).alias("is_broken"),
        (pl.col("is_limit_up") & (pl.col("open") >= lu - TOLERANCE) & (pl.col("low") >= lu - TOLERANCE))
        .fill_null(False).alias("one_word"),
    )
    # 连板数：按该股自己的交易日（停牌不打断）
    grp = (~pl.col("is_limit_up")).cast(pl.Int32).cum_sum().over("code")
    df = df.with_columns(grp.alias("_grp"))
    df = df.with_columns(
        pl.when(pl.col("is_limit_up"))
        .then(pl.col("is_limit_up").cast(pl.Int32).cum_sum().over(["code", "_grp"]))
        .otherwise(0).cast(pl.Int16).alias("streak")
    )
    return df.drop(["_list_date", "_delisted", "_s_share", "_tui", "_td", "_ld_td", "_big", "_ratio", "_grp"])
