"""
A 股成交规则与费用（模拟盘、手动实盘记账、回测口径一致）。

费用（每笔成交）：
- 佣金：成交额 × 费率（默认万 2.5），不足 5 元按 5 元；
- 印花税：只在卖出时收，按成交日期（2023-08-28 前 0.1%，之后 0.05%），与 predict/costs 一致；
- 过户费：成交额 × 0.001%（沪深都收，买卖都收）。
数量：买入按整手（主板/创业板 100 股，科创板 200 股起）；卖出可以是零股，但只能在全部卖出时。
能否成交（日线口径，价格都是真实价格）：
- 一字涨停（开=高=低=涨停价）当天买不进；最低价就是涨停价（全天封死）也买不进；
- 一字跌停当天卖不出；最高价就是跌停价也卖不出；
- 限价买：开盘价 ≤ 限价 → 按开盘价成交；否则最低价 ≤ 限价 → 按限价成交；否则不成交；卖出反过来；
- 市价（开盘）单：按开盘价 ± 滑点成交；
- 止损（到价卖出）：最低价 ≤ 触发价 → 卖出；开盘就低于触发价时按开盘价（跳空低开的真实损失），否则按触发价 − 滑点；
- 止盈（到价卖出）：最高价 ≥ 触发价 → 卖出；开盘就高于触发价时按开盘价，否则按触发价。
盘中口径（实时行情）：限价买在卖一价 ≤ 限价且卖一有量时按卖一价成交；限价卖在买一价 ≥ 限价且买一有量时按买一价成交；
止损在最新价 ≤ 触发价时按买一价卖出（买一没有量 = 跌停封死，卖不出）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..predict import costs as costs_mod

COMMISSION: float = costs_mod.COMMISSION
MIN_COMMISSION: float = costs_mod.MIN_COMMISSION
TRANSFER_FEE: float = 0.00001
SLIPPAGE: float = costs_mod.SLIPPAGE
EPS: float = 0.0049


def lot_of(code: str) -> int:
    return 200 if code.startswith(("688", "689")) else 100


def fees(side: str, amount: float, day: date, commission: float = COMMISSION, min_commission: float = MIN_COMMISSION) -> dict:
    """一笔成交的费用：{commission, stamp, transfer, total}"""
    comm: float = max(amount * commission, min_commission) if amount > 0 else 0.0
    stamp: float = amount * costs_mod.stamp_duty(day) if side == "sell" else 0.0
    transfer: float = amount * TRANSFER_FEE
    return {"commission": round(comm, 2), "stamp": round(stamp, 2), "transfer": round(transfer, 2),
            "total": round(comm + stamp + transfer, 2)}


def check_qty(code: str, side: str, qty: int, position_qty: int = 0) -> str | None:
    """数量是否合规；不合规返回中文原因"""
    if qty <= 0:
        return "数量要大于 0"
    lot: int = lot_of(code)
    if side == "buy":
        if code.startswith(("688", "689")):
            if qty < 200:
                return "科创板买入至少 200 股"
        elif qty % lot:
            return f"买入要按整手：{lot} 股的整数倍"
    else:
        if qty > position_qty:
            return f"可卖数量只有 {position_qty} 股"
        if qty % 100 and qty != position_qty:
            return "卖出零股（不足 100 股的部分）只能在全部卖出时一起卖"
    return None


@dataclass
class Bar:
    """一天的真实价格（不复权）"""
    day: date
    open: float
    high: float
    low: float
    close: float
    preclose: float
    limit_up: float | None = None
    limit_down: float | None = None

    @property
    def one_price_up(self) -> bool:
        return self.limit_up is not None and self.low >= self.limit_up - EPS

    @property
    def one_price_down(self) -> bool:
        return self.limit_down is not None and self.high <= self.limit_down + EPS


def match_bar(side: str, kind: str, bar: Bar, price: float | None = None, trigger: float | None = None,
              slippage: float = SLIPPAGE) -> float | None:
    """日线口径的成交价（不能成交返回 None）。kind：limit / market / stop / take_profit"""
    if side == "buy" and bar.one_price_up:
        return None
    if side == "sell" and bar.one_price_down:
        return None
    if kind == "market":
        px = bar.open * (1 + slippage) if side == "buy" else bar.open * (1 - slippage)
        if bar.limit_up is not None:
            px = min(px, bar.limit_up)
        if bar.limit_down is not None:
            px = max(px, bar.limit_down)
        return round(px, 3)
    if kind == "limit":
        if price is None:
            return None
        if side == "buy":
            if bar.open <= price + 1e-9:
                return bar.open
            return price if bar.low <= price + 1e-9 else None
        if bar.open >= price - 1e-9:
            return bar.open
        return price if bar.high >= price - 1e-9 else None
    if kind == "stop" and side == "sell":
        if trigger is None or bar.low > trigger + 1e-9:
            return None
        px = bar.open if bar.open <= trigger else trigger * (1 - slippage)
        if bar.limit_down is not None:
            px = max(px, bar.limit_down)
        return round(px, 3)
    if kind == "take_profit" and side == "sell":
        if trigger is None or bar.high < trigger - 1e-9:
            return None
        return round(bar.open if bar.open >= trigger else trigger, 3)
    return None


def match_quote(side: str, kind: str, quote: dict, price: float | None = None, trigger: float | None = None) -> float | None:
    """盘中实时行情口径的成交价（不能成交返回 None）。quote 需要 price, bid1, ask1, bid1_vol, ask1_vol"""
    last = quote.get("price")
    bid, ask = quote.get("bid1") or 0, quote.get("ask1") or 0
    bid_v, ask_v = quote.get("bid1_vol") or 0, quote.get("ask1_vol") or 0
    if not last:
        return None
    if kind == "market":
        if side == "buy":
            return ask if ask > 0 and ask_v > 0 else None
        return bid if bid > 0 and bid_v > 0 else None
    if kind == "limit" and price is not None:
        if side == "buy":
            return ask if ask > 0 and ask_v > 0 and ask <= price + 1e-9 else None
        return bid if bid > 0 and bid_v > 0 and bid >= price - 1e-9 else None
    if kind == "stop" and side == "sell" and trigger is not None:
        if last > trigger + 1e-9:
            return None
        return bid if bid > 0 and bid_v > 0 else None          # 跌停封死时买一没有量：卖不出
    if kind == "take_profit" and side == "sell" and trigger is not None:
        if last < trigger - 1e-9:
            return None
        return bid if bid > 0 and bid_v > 0 else None
    return None


def ex_rights(prev_close: float, preclose: float) -> dict | None:
    """除权除息（昨收 ≠ 今天的参考价）的处理方式：
    参考价比昨收低 20% 以上多半是送转股 → 股数按比例增加、成本按比例降低；否则按现金分红处理（现金 += 股数 × 差价）"""
    if not prev_close or not preclose or abs(preclose / prev_close - 1) < 1e-4:
        return None
    ratio: float = preclose / prev_close
    if ratio < 0.8:
        return {"type": "shares", "multiplier": 1 / ratio, "ratio": ratio}
    return {"type": "cash", "per_share": prev_close - preclose, "ratio": ratio}
