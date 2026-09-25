"""
基于 vnpy.alpha AlphaStrategy 的稳健配置策略：在调仓日读取目标仓位信号，按整手调整持仓。
"""
from vnpy.trader.object import BarData, TradeData
from vnpy.alpha import AlphaStrategy

from .rebalance import plan_rebalance


class SteadyAllocationStrategy(AlphaStrategy):
    """稳健多资产趋势配置"""

    cash_symbol: str = "511880.SSE"
    rebalance_band: float = 0.03
    lot_size: int = 100
    cash_buffer: float = 0.01
    price_add: float = 0.02

    def on_init(self) -> None:
        self.last_prices: dict[str, float] = {}
        self.rebalance_count: int = 0

    def on_bars(self, bars: dict[str, BarData]) -> None:
        # 委托只在下一个交易日有效，未成交的撤掉
        self.cancel_all()

        for vt_symbol, bar in bars.items():
            if bar.close_price:
                self.last_prices[vt_symbol] = bar.close_price

        signal = self.get_signal()
        if signal.is_empty():
            return

        weights: dict[str, float] = dict(zip(signal["vt_symbol"].to_list(), signal["signal"].to_list(), strict=True))
        positions: dict[str, float] = {s: p for s, p in self.pos_data.items() if p}

        targets: dict[str, int] = plan_rebalance(
            weights=weights,
            positions=positions,
            prices=self.last_prices,
            cash=self.get_cash_available(),
            cash_symbol=self.cash_symbol,
            band=self.rebalance_band,
            lot_size=self.lot_size,
            cash_buffer=self.cash_buffer,
        )

        changed: bool = False
        for vt_symbol, target in targets.items():
            if target != positions.get(vt_symbol, 0):
                changed = True
            self.set_target(vt_symbol, target)

        if changed:
            self.rebalance_count += 1
            self.execute_trading({s: b for s, b in bars.items() if s in targets}, self.price_add)

    def on_trade(self, trade: TradeData) -> None:
        pass
