"""
标签与前向价格：同一只股票的"下一根K线"（停牌日不在面板里，所以就是下一个有行情的交易日）。

- 连板/首板：y = 下一交易日收盘是否涨停（Int8）；下一根K线距今超过 10 个自然日（长期停牌）或没有下一根时为空。
  label_end = 标签揭晓日（下一根K线的日期），滚动训练时训练样本的 label_end 必须 ≤ 训练截止日（净化）。
- 强势股波段（swing_labels）：按"可实现收益"打标签——T+1 开盘买（开盘涨停买不进 → fill=False，不参与训练），
  从第 2 根K线起第一个收盘没涨停的那根按收盘价卖，第 5 根强制卖，卖出日收盘跌停顺延到下一根收盘——决定卖出后就锁定，
  下一根不管是否涨停都卖（仍跌停再顺延，最多到第 7 根）；不设开盘涨幅上限；
  扣除佣金、按日期的印花税、滑点后的净收益 net；label_end = 实际卖出日。股票中途退市按最后收盘价计。
  规则与 execution.simulate(exit_rule="until_break_close") 完全一致。
"""
from collections.abc import Iterable

import numpy as np
import polars as pl

from .costs import DEFAULT, Costs
from .execution import simulate
from .features import LABEL_GAP_DAYS


NEXT_COLUMNS: list[str] = [
    "next_date", "next_open", "next_high", "next_low", "next_close", "next_limit_up", "next_limit_down",
    "next_one_word", "next_pct",
]
SWING_EXIT_RULE: str = "until_break_close"
SWING_LABEL_COLUMNS: list[str] = ["status", "fill", "entry_date", "entry", "exit_date", "exit", "net", "y",
                                  "label_end", "hold_days", "exit_reason"]


def next_rows(panel_lim: pl.DataFrame) -> pl.DataFrame:
    """每行附上同一股票下一根K线的价格：date, code, next_*, y"""
    df: pl.DataFrame = panel_lim.select(
        ["date", "code", "open", "high", "low", "close", "limit_up", "limit_down", "one_word", "pct", "is_limit_up"]
    ).sort(["code", "date"])
    nxt = {
        "next_date": "date", "next_open": "open", "next_high": "high", "next_low": "low", "next_close": "close",
        "next_limit_up": "limit_up", "next_limit_down": "limit_down", "next_one_word": "one_word",
        "next_pct": "pct", "_next_lu": "is_limit_up",
    }
    df = df.select(["date", "code", *[pl.col(src).shift(-1).over("code").alias(dst) for dst, src in nxt.items()]])
    gap_ok = (pl.col("next_date") - pl.col("date")).dt.total_days() <= LABEL_GAP_DAYS
    return df.with_columns(
        pl.when(pl.col("next_date").is_not_null() & gap_ok).then(pl.col("_next_lu").cast(pl.Int8))
        .otherwise(None).alias("y")
    ).drop("_next_lu")


def add_labels(feat: pl.DataFrame, panel_lim: pl.DataFrame) -> pl.DataFrame:
    """在特征表上加 y、label_end 与 next_date, next_open, next_limit_up(价格), next_one_word, next_close, next_pct 等列"""
    drop: list[str] = [c for c in [*NEXT_COLUMNS, "y", "label_end"] if c in feat.columns]
    base: pl.DataFrame = feat.drop(drop) if drop else feat
    codes: pl.DataFrame = base.select("code").unique()
    nr: pl.DataFrame = next_rows(panel_lim.join(codes, on="code", how="semi"))
    return base.join(nr, on=["date", "code"], how="left", maintain_order="left").with_columns(
        pl.when(pl.col("y").is_not_null()).then(pl.col("next_date")).otherwise(None).alias("label_end")
    )


def swing_labels(
    feat: pl.DataFrame,
    panel_lim: pl.DataFrame,
    delisted: Iterable[str] | None = None,
    costs: Costs = DEFAULT,
    calendar: np.ndarray | None = None,
) -> pl.DataFrame:
    """在波段候选表（date, code, ...）上加：status(pending/unfilled/holding/closed)、fill（T+1 开盘买得进）、
    entry_date、entry、exit_date、exit（含滑点成交价）、net（扣费净收益，只有 closed 有值）、y（net>0，Int8）、
    label_end（= exit_date）、hold_days、exit_reason。delisted：已退市代码（None 时读股票列表）。"""
    drop: list[str] = [c for c in SWING_LABEL_COLUMNS if c in feat.columns]
    base: pl.DataFrame = feat.drop(drop) if drop else feat
    sig: pl.DataFrame = base.select(pl.col("date").alias("signal_date"), "code")
    sim: pl.DataFrame = simulate(sig, panel_lim, costs=costs, exit_rule=SWING_EXIT_RULE, max_gap_pct=None,
                                 delisted=delisted, calendar=calendar)
    closed = pl.col("status") == "closed"
    lab: pl.DataFrame = sim.select(
        "status",
        pl.col("status").is_in(["holding", "closed"]).alias("fill"),
        "entry_date", "entry", "exit_date", "exit",
        pl.when(closed).then(pl.col("ret")).otherwise(None).alias("net"),
        pl.when(closed).then((pl.col("ret") > 0).cast(pl.Int8)).otherwise(None).alias("y"),
        pl.when(closed).then(pl.col("exit_date")).otherwise(None).alias("label_end"),
        "hold_days", "exit_reason",
    )
    return base.hstack(lab)
