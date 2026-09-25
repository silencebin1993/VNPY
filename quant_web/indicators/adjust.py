"""
前复权价格（以每只股票最新收盘价为锚）。

日线面板存的是不复权原始价格加 preclose（交易所除权参考价）。当天真实涨跌 = close / preclose - 1，所以：
    adj_t = Π_{s≤t} close_s / preclose_s          （累计"复权因子"）
    前复权收盘价 qclose_t = close_last × adj_t / adj_last
    同一天的开高低价乘同一个比例：qX_t = X_t × qclose_t / close_t

性质：
- 最新一天的前复权价等于真实价，可以直接和实时行情、下单价比较；
- 只截取一段数据来算（只要包含最新一天），结果和用全部历史算一样（adj 的比值只取决于中间的涨跌）；
- 成交量、成交额不调整（和通达信一致）。
"""
from __future__ import annotations

import polars as pl


PRICE_COLS: tuple[str, ...] = ("open", "high", "low", "close")


def add_qfq(df: pl.DataFrame, by: str | None = "code", prefix: str = "q") -> pl.DataFrame:
    """在按 (code, date) 排序的日线表上加 qopen/qhigh/qlow/qclose（prefix 可改）和 adj_factor（qclose/close）。

    需要 close、preclose 列；preclose 缺失或不大于 0 的行按"当天无除权"处理（比例取 close/前一天 close）。
    by=None 表示整张表是同一只股票。
    """
    if df.height == 0:
        return df.with_columns([pl.lit(None, dtype=pl.Float64).alias(prefix + c) for c in PRICE_COLS if c in df.columns]
                               + [pl.lit(None, dtype=pl.Float64).alias("adj_factor")])
    over = (lambda e: e.over(by)) if by else (lambda e: e)
    prev_close: pl.Expr = over(pl.col("close").shift(1))
    base: pl.Expr = (pl.when(pl.col("preclose") > 0).then(pl.col("preclose"))
                     .when(prev_close > 0).then(prev_close).otherwise(pl.col("close")))
    ratio: pl.Expr = (pl.col("close") / base).fill_null(1.0).fill_nan(1.0)
    out: pl.DataFrame = df.with_columns(over(ratio.cum_prod()).alias("_adj"))
    out = out.with_columns(
        (over(pl.col("close").last()) * pl.col("_adj") / over(pl.col("_adj").last()) / pl.col("close"))
        .alias("adj_factor")
    )
    out = out.with_columns([(pl.col(c) * pl.col("adj_factor")).alias(prefix + c) for c in PRICE_COLS if c in out.columns])
    return out.drop("_adj")


def adjust_price(price: float, factor_then: float, factor_now: float) -> float:
    """把某天记录的价格（当时的真实价）换算到今天的口径：用于除权后调整交易计划里的止损价/目标价。

    factor_then / factor_now 为 add_qfq 算出的同一只股票在"记录那天"和"今天"的 adj_factor（qclose/close，
    锚点相同即可）。例：10 送 10 前记录的止损价 9.00，除权后应变为 4.50 = 9.00 × 0.5 / 1。
    这样除权"跳空"不会被误判为跌破止损。
    """
    if not factor_then or not factor_now:
        return price
    return price * factor_then / factor_now
