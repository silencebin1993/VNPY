"""
实盘操作建议：根据最新行情和我的持仓，算出本周需要买卖哪些ETF、各多少份。
"""
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

import pandas as pd

from .allocator import Allocation, compute_allocations, weekly_rebalance_dates
from .config import ADVICE_FILE, CODE_MAP, ETF_MAP, StrategyParams, TradingParams, Universe
from .data import china_now, fetch_latest_prices, load_prices
from .portfolio import Holdings, append_nav, load_holdings, save_holdings
from .rebalance import plan_rebalance


@dataclass
class Order:
    code: str
    name: str
    side: str               # 买入 / 卖出
    volume: int
    price: float            # 参考价（最新收盘价）
    amount: float
    commission: float


@dataclass
class PositionRow:
    code: str
    name: str
    asset_class: str
    target_weight: float
    current_volume: int
    target_volume: int
    price: float

    @property
    def current_value(self) -> float:
        return self.current_volume * self.price

    @property
    def target_value(self) -> float:
        return self.target_volume * self.price


@dataclass
class Advice:
    signal_date: date                   # 信号依据的收盘日（上一个完整周的最后交易日）
    price_date: date                    # 参考价日期
    total_value: float
    cash: float
    allocation: Allocation
    rows: list[PositionRow]
    orders: list[Order]
    warnings: list[str] = field(default_factory=list)
    ignored: dict[str, int] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)     # 代码 -> 参考价

    @property
    def cash_after(self) -> float:
        cash: float = self.cash
        for o in self.orders:
            cash += (-o.amount if o.side == "买入" else o.amount) - o.commission
        return cash


def estimate_commission(amount: float, code: str, trading: TradingParams, universe: Universe) -> float:
    if CODE_MAP[code].vt_symbol == universe.cash:
        return 0.0
    return max(amount * trading.commission_rate, trading.min_commission)


def class_of(vt_symbol: str, universe: Universe) -> str:
    for cname, symbols in universe.classes.items():
        if vt_symbol in symbols:
            return cname
    return "现金管理" if vt_symbol == universe.cash else ""


def make_advice(
    holdings: Holdings | None = None,
    params: StrategyParams | None = None,
    trading: TradingParams | None = None,
    universe: Universe | None = None,
) -> Advice:
    params = params or StrategyParams()
    trading = trading or TradingParams()
    universe = universe or Universe()
    holdings = holdings or load_holdings()

    close: pd.DataFrame = load_prices(universe.vt_symbols)
    if close.empty:
        raise RuntimeError("本地没有行情数据，请先执行【更新行情数据】")

    today: date = china_now().date()
    dates: list[pd.Timestamp] = weekly_rebalance_dates(close.index, today=today)
    allocation: Allocation = compute_allocations(close, universe, params, dates)[-1]

    warnings: list[str] = []
    if (today - allocation.date.date()).days > 10:
        warnings.append(f"行情数据停留在 {allocation.date:%Y-%m-%d}，已经很久没更新，请先更新行情数据。")

    quotes: dict[str, tuple[date, float]] = fetch_latest_prices([ETF_MAP[s] for s in universe.vt_symbols])
    prices: dict[str, float] = {s: p for s, (_, p) in quotes.items()}
    price_date: date = max((d for d, _ in quotes.values()), default=allocation.date.date())

    positions: dict[str, float] = {}
    ignored: dict[str, int] = {}
    for code, volume in holdings.positions.items():
        etf = CODE_MAP.get(code)
        if etf:
            positions[etf.vt_symbol] = volume
        else:
            ignored[code] = volume

    needed: set[str] = set(allocation.weights) | set(positions)
    missing: list[str] = [ETF_MAP[s].name for s in needed if s not in prices]
    if missing:
        warnings.append(f"以下ETF没取到最新价格，本次不对它们做调整：{'、'.join(missing)}。可稍后重试。")

    targets: dict[str, int] = plan_rebalance(
        weights=allocation.weights,
        positions=positions,
        prices=prices,
        cash=holdings.cash,
        cash_symbol=universe.cash,
        band=params.rebalance_band,
        lot_size=trading.lot_size,
        cash_buffer=trading.cash_buffer,
    )

    rows: list[PositionRow] = []
    orders: list[Order] = []
    for vt_symbol in universe.vt_symbols:
        etf = ETF_MAP[vt_symbol]
        current: int = int(positions.get(vt_symbol, 0))
        target: int = targets.get(vt_symbol, current)
        weight: float = allocation.weights.get(vt_symbol, 0.0)
        price: float = prices.get(vt_symbol, 0.0)
        if current or target or weight:
            rows.append(PositionRow(etf.code, etf.name, class_of(vt_symbol, universe), weight, current, target, price))

        if weight > 0 and target == 0 and price and vt_symbol != universe.cash:
            warnings.append(
                f"{etf.name} 目标仓位 {weight:.0%}，但资金不够买一手（100份约 {price * 100:,.0f} 元），暂不买入。"
            )

        diff: int = target - current
        if diff and price:
            amount: float = abs(diff) * price
            orders.append(Order(
                code=etf.code,
                name=etf.name,
                side="买入" if diff > 0 else "卖出",
                volume=abs(diff),
                price=price,
                amount=amount,
                commission=estimate_commission(amount, etf.code, trading, universe),
            ))

    # 先卖后买，卖出的钱当天就能用来买入
    orders.sort(key=lambda o: (o.side != "卖出", -o.amount))

    total: float = holdings.cash + sum(v * prices.get(s, 0.0) for s, v in positions.items())
    if total < 50_000:
        warnings.append("资金少于5万元时，国债ETF（一手约1.4万元）等可能买不了整手，实际效果会偏离回测。建议投入10万元以上。")

    advice = Advice(
        signal_date=allocation.date.date(),
        price_date=price_date,
        total_value=total,
        cash=holdings.cash,
        allocation=allocation,
        rows=rows,
        orders=orders,
        warnings=warnings,
        ignored=ignored,
        prices={ETF_MAP[s].code: p for s, p in prices.items()},
    )
    save_advice(advice)
    return advice


def save_advice(advice: Advice) -> None:
    ADVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "signal_date": advice.signal_date.isoformat(),
        "price_date": advice.price_date.isoformat(),
        "applied": False,
        "orders": [asdict(o) for o in advice.orders],
        "prices": advice.prices,
    }
    ADVICE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_saved_advice() -> dict | None:
    if not ADVICE_FILE.exists():
        return None
    return json.loads(ADVICE_FILE.read_text(encoding="utf-8"))


def apply_saved_advice() -> tuple[Holdings, list[Order], float]:
    """按最近一次建议（参考价）更新持仓。返回 (新持仓, 已执行的委托, 调整后总资产)"""
    data: dict | None = load_saved_advice()
    if not data:
        raise RuntimeError("还没有生成过操作建议")
    if data.get("applied"):
        raise RuntimeError("最近一次建议已经更新过持仓了，不能重复更新")

    orders: list[Order] = [Order(**o) for o in data["orders"]]
    holdings: Holdings = load_holdings()
    for o in orders:
        sign: int = 1 if o.side == "买入" else -1
        holdings.positions[o.code] = holdings.positions.get(o.code, 0) + sign * o.volume
        holdings.cash += -sign * o.amount - o.commission
    holdings.positions = {k: v for k, v in holdings.positions.items() if v}
    save_holdings(holdings)

    prices: dict[str, float] = data.get("prices", {})
    total: float = holdings.cash + sum(v * prices.get(code, 0.0) for code, v in holdings.positions.items())
    if all(code in prices for code in holdings.positions):
        append_nav(total, holdings.cash, "按建议调仓", date.fromisoformat(data["price_date"]))

    data["applied"] = True
    ADVICE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return holdings, orders, total
