"""
把目标仓位比例换算成具体持有份额（整手、偏离阈值、现金缓冲）。回测与实盘建议共用。
"""
import math


def plan_rebalance(
    weights: dict[str, float],
    positions: dict[str, float],
    prices: dict[str, float],
    cash: float,
    cash_symbol: str,
    band: float,
    lot_size: int = 100,
    cash_buffer: float = 0.01,
) -> dict[str, int]:
    """
    返回每只ETF调仓后的目标持有份额。

    - 新买入 / 清仓 / 仓位偏离超过 band 的ETF才调整，其余保持不动；
    - 买入数量向下取整到整手，保证不会超出可用资金；
    - 调整后的剩余资金（留出 cash_buffer）买入货币ETF。
    """
    held: dict[str, float] = {s: p for s, p in positions.items() if p}
    total: float = cash + sum(p * prices[s] for s, p in held.items() if prices.get(s))
    if total <= 0:
        return {}

    targets: dict[str, int] = {}
    for symbol in sorted(set(weights) | set(held)):
        if symbol == cash_symbol:
            continue
        pos: float = held.get(symbol, 0)
        price: float | None = prices.get(symbol)
        if not price:
            targets[symbol] = int(pos)     # 没有价格（停牌等）不动
            continue

        w_target: float = weights.get(symbol, 0.0)
        w_current: float = pos * price / total
        entering: bool = w_target > 0 and pos == 0
        exiting: bool = w_target == 0 and pos > 0
        if entering or exiting or abs(w_target - w_current) > band:
            targets[symbol] = math.floor(total * w_target / price / lot_size) * lot_size
        else:
            targets[symbol] = int(pos)

    invested: float = sum(v * prices[s] for s, v in targets.items() if prices.get(s))
    spare: float = total * (1 - cash_buffer) - invested

    # 保持不动的ETF合计超配、导致资金不够时，改为全部按目标调整
    if spare < 0 and band > 0:
        return plan_rebalance(weights, positions, prices, cash, cash_symbol, 0, lot_size, cash_buffer)

    cash_price: float | None = prices.get(cash_symbol)
    if cash_price:
        current: int = int(held.get(cash_symbol, 0))
        if weights.get(cash_symbol, 0.0) > 0:
            ideal: int = math.floor(max(spare, 0) / cash_price / lot_size) * lot_size
            # 货币ETF变化不大时不动，避免每周都有零碎交易
            if current and current * cash_price <= max(spare, 0) and abs(ideal - current) * cash_price / total <= band:
                ideal = current
            targets[cash_symbol] = ideal
        elif current:
            targets[cash_symbol] = 0

    return targets
