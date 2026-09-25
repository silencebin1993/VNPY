"""
历史回测：用 vnpy.alpha 的 BacktestingEngine 按真实规则（整手、手续费、次日开盘成交）回放策略。
"""
import contextlib
import io
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
import polars as pl

from vnpy.trader.constant import Interval
from vnpy.alpha import BacktestingEngine, logger

from .allocator import Allocation, compute_allocations
from .config import StrategyParams, TradingParams, Universe, vt
from .data import get_lab, load_prices, setup_contracts
from .strategy import SteadyAllocationStrategy


@dataclass
class BacktestResult:
    balance: pd.Series                  # 每日账户总资产
    benchmarks: dict[str, pd.Series]    # 基准净值（同样起始资金）
    metrics: dict[str, float]
    benchmark_metrics: dict[str, dict[str, float]]
    yearly: pd.DataFrame                # 年度收益：策略 & 基准
    class_weights: pd.DataFrame         # 各大类目标仓位（周频）
    allocations: list[Allocation]
    trade_count: int
    rebalance_count: int
    total_commission: float
    capital: float


def calc_metrics(balance: pd.Series, annual_days: int = 244, risk_free: float = 0.02) -> dict[str, float]:
    """根据每日资产序列计算常用指标"""
    balance = balance.dropna()
    ret: pd.Series = balance.pct_change().dropna()
    years: float = max(len(ret) / annual_days, 1e-9)
    total_return: float = balance.iloc[-1] / balance.iloc[0] - 1
    cagr: float = (1 + total_return) ** (1 / years) - 1
    vol: float = float(ret.std() * np.sqrt(annual_days))
    sharpe: float = (cagr - risk_free) / vol if vol else 0.0
    drawdown: pd.Series = balance / balance.cummax() - 1
    max_dd: float = float(drawdown.min())

    # 最长的"从创新高到重新创新高"天数
    peak_dates: pd.Series = balance.index.to_series().where(drawdown == 0).ffill()
    underwater_days: int = int((balance.index.to_series() - peak_dates).dt.days.max())

    yearly: pd.Series = _yearly_returns(balance)
    monthly: pd.Series = balance.resample("ME").last().pct_change().dropna()

    return {
        "total_return": total_return,
        "cagr": cagr,
        "vol": vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "calmar": cagr / -max_dd if max_dd < 0 else 0.0,
        "underwater_days": underwater_days,
        "positive_years": float((yearly > 0).mean()),
        "worst_year": float(yearly.min()),
        "best_year": float(yearly.max()),
        "monthly_win_rate": float((monthly > 0).mean()) if len(monthly) else 0.0,
    }


def _yearly_returns(balance: pd.Series) -> pd.Series:
    """按自然年计算收益，第一年从起点算起"""
    year_end: pd.Series = balance.groupby(balance.index.year).last()
    prev_end: pd.Series = year_end.shift(1)
    prev_end.iloc[0] = balance.iloc[0]
    return year_end / prev_end - 1


def run_backtest(
    start: str = "2014-06-01",
    end: str | None = None,
    capital: float = 200_000,
    params: StrategyParams | None = None,
    trading: TradingParams | None = None,
    universe: Universe | None = None,
    verbose: bool = False,
) -> BacktestResult:
    params = params or StrategyParams()
    trading = trading or TradingParams()
    universe = universe or Universe()

    if not verbose:
        logger.disable("vnpy.alpha")

    lab = get_lab()
    setup_contracts(lab, universe, trading)

    close: pd.DataFrame = load_prices(universe.vt_symbols, lab=lab)
    if close.empty:
        raise RuntimeError("本地没有行情数据，请先执行【更新行情数据】")

    start_dt: datetime = datetime.strptime(start, "%Y-%m-%d")
    end_dt: datetime = datetime.strptime(end, "%Y-%m-%d") if end else close.index[-1].to_pydatetime()

    allocations: list[Allocation] = [
        a for a in compute_allocations(close, universe, params) if start_dt <= a.date <= end_dt
    ]
    rows: list[dict] = [
        {"datetime": a.date.to_pydatetime(), "vt_symbol": s, "signal": w}
        for a in allocations for s, w in a.weights.items()
    ]
    signal_df: pl.DataFrame = pl.DataFrame(rows, schema={"datetime": pl.Datetime("us"), "vt_symbol": pl.Utf8, "signal": pl.Float64})

    engine = BacktestingEngine(lab)
    engine.set_parameters(
        vt_symbols=universe.vt_symbols,
        interval=Interval.DAILY,
        start=start_dt,
        end=end_dt,
        capital=int(capital),
        risk_free=0,
        annual_days=params.annual_days,
    )
    setting: dict = {
        "cash_symbol": universe.cash,
        "rebalance_band": params.rebalance_band,
        "lot_size": trading.lot_size,
        "cash_buffer": trading.cash_buffer,
        "price_add": trading.price_add,
    }
    engine.add_strategy(SteadyAllocationStrategy, setting, signal_df)
    with contextlib.nullcontext() if verbose else contextlib.redirect_stderr(io.StringIO()):
        engine.load_data()
    engine.run_backtesting()
    daily_df = engine.calculate_result()
    if daily_df is None:
        raise RuntimeError("回测期间没有产生任何交易，请检查数据区间")
    engine.calculate_statistics()

    daily: pd.DataFrame = engine.daily_df.to_pandas()
    balance: pd.Series = pd.Series(daily["balance"].to_numpy(), index=pd.DatetimeIndex(daily["date"]), name="策略")
    # 起点补上初始资金
    balance = pd.concat([pd.Series([capital], index=[balance.index[0] - pd.Timedelta(days=1)]), balance])

    benchmarks: dict[str, pd.Series] = {}
    for name, code in [("沪深300ETF（一直持有）", "510300"), ("货币基金（银华日利）", "511880")]:
        s: pd.Series = close[vt(code)].loc[balance.index[1]:balance.index[-1]].dropna()
        if len(s):
            s = s / s.iloc[0] * capital
            benchmarks[name] = s.reindex(balance.index[1:]).ffill()

    yearly: pd.DataFrame = pd.DataFrame({"策略": _yearly_returns(balance)})
    for name, s in benchmarks.items():
        yearly[name] = _yearly_returns(s.dropna())

    class_rows: list[dict] = []
    for a in allocations:
        row: dict = {"date": a.date}
        for cname, symbols in universe.classes.items():
            row[cname] = sum(a.weights.get(s, 0.0) for s in symbols)
        row["货币基金/现金"] = 1 - sum(v for k, v in row.items() if k != "date")
        class_rows.append(row)
    class_weights: pd.DataFrame = pd.DataFrame(class_rows).set_index("date")

    strategy: SteadyAllocationStrategy = engine.strategy     # type: ignore[assignment]

    return BacktestResult(
        balance=balance,
        benchmarks=benchmarks,
        metrics=calc_metrics(balance, params.annual_days),
        benchmark_metrics={k: calc_metrics(v.dropna(), params.annual_days) for k, v in benchmarks.items()},
        yearly=yearly,
        class_weights=class_weights,
        allocations=allocations,
        trade_count=len(engine.trades),
        rebalance_count=strategy.rebalance_count,
        total_commission=float(daily["commission"].sum()),
        capital=capital,
    )
