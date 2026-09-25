"""
交易费用（回测、模拟盘、波段标签共用同一套口径）。

- 佣金 COMMISSION 双边（按比例；组合回测里另有每笔最低 5 元）；
- 印花税只在卖出时收：2023-08-28 之前 0.1%，之后 0.05%（stamp_duty(day)）；
- 滑点 SLIPPAGE 双边：买价上浮、卖价下浮。

单笔净收益 = 卖价×(1-滑点)×(1-佣金-印花税) / (买价×(1+滑点)×(1+佣金)) - 1。
"""
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import polars as pl


COMMISSION: float = 0.00025
SLIPPAGE: float = 0.001
MIN_COMMISSION: float = 5.0
STAMP_CUT_DATE: date = date(2023, 8, 28)        # 印花税减半生效日（这一天起 0.05%）
STAMP_BEFORE: float = 0.001
STAMP_AFTER: float = 0.0005

_CUT64: np.datetime64 = np.datetime64(STAMP_CUT_DATE, "D")


def stamp_duty(day: date) -> float:
    """卖出印花税率：2023-08-28 之前 0.1%，之后 0.05%"""
    return STAMP_BEFORE if day < STAMP_CUT_DATE else STAMP_AFTER


def stamp_duty_expr(day: pl.Expr) -> pl.Expr:
    """按日期列给出印花税率（polars 表达式）"""
    return pl.when(day < pl.lit(STAMP_CUT_DATE)).then(STAMP_BEFORE).otherwise(STAMP_AFTER)


def stamp_duty_array(days: np.ndarray) -> np.ndarray:
    """按 datetime64 数组给出印花税率（NaT 按新税率）"""
    d = np.asarray(days).astype("datetime64[D]")
    return np.where(~np.isnat(d) & (d < _CUT64), STAMP_BEFORE, STAMP_AFTER)


@dataclass(frozen=True)
class Costs:
    """一套费用参数；stamp 为空时印花税按卖出日期（stamp_duty(day)）"""

    commission: float = COMMISSION
    slippage: float = SLIPPAGE
    stamp: float | None = None
    min_commission: float = MIN_COMMISSION

    def stamp_on(self, day: date) -> float:
        return stamp_duty(day) if self.stamp is None else float(self.stamp)

    def stamp_array(self, days: np.ndarray) -> np.ndarray:
        if self.stamp is None:
            return stamp_duty_array(days)
        return np.full(len(days), float(self.stamp))

    def buy_price(self, price: Any) -> Any:
        return price * (1 + self.slippage)

    def sell_price(self, price: Any) -> Any:
        return price * (1 - self.slippage)

    def net_return(self, entry_raw: Any, exit_raw: Any, exit_day: Any) -> Any:
        """原始买价（开盘价等）、原始卖价 → 扣除全部费用后的单笔收益（小数）。
        exit_day 可以是 date（标量）或 datetime64 数组。"""
        if isinstance(exit_day, date):
            stamp: Any = self.stamp_on(exit_day)
        else:
            stamp = self.stamp_array(np.asarray(exit_day))
        return (self.sell_price(exit_raw) * (1 - self.commission - stamp)
                / (self.buy_price(entry_raw) * (1 + self.commission)) - 1)


DEFAULT: Costs = Costs()


def from_trade(ts: dict | None) -> Costs:
    """由交易设置（dict）得到费用：fee_rate、slippage；stamp_by_date=False 时用固定的 stamp_duty"""
    ts = ts or {}
    by_date: bool = bool(ts.get("stamp_by_date", True))
    stamp: float | None = None if by_date or ts.get("stamp_duty") is None else float(ts["stamp_duty"])
    return Costs(
        commission=float(ts.get("fee_rate", COMMISSION) if ts.get("fee_rate") is not None else COMMISSION),
        slippage=float(ts.get("slippage", SLIPPAGE) if ts.get("slippage") is not None else SLIPPAGE),
        stamp=stamp,
    )


def net_return(entry_raw: Any, exit_raw: Any, exit_day: Any, costs: Costs | None = None) -> Any:
    """默认费用下的单笔净收益（见 Costs.net_return）"""
    return (costs or DEFAULT).net_return(entry_raw, exit_raw, exit_day)
