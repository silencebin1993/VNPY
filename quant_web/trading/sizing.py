"""
止损价与仓位计算（选股器给建议、下单前风控、交易计划共用同一套口径）。

止损价（建议）：
- 两个"技术止损"取更近（更高）的那个：买入价下方 2 倍平均波幅（ATR），20 日均线下方 2%（均线在买入价下方时才用）；
- 但不能比"买入价 × (1 − 默认止损比例)"更远（默认 8%），也至少留 2% 的空间（太近会被正常波动打掉）。
仓位（按风险）：
- 这笔最多亏 = 总资金 × 单笔风险比例（默认 1%）；每股风险 = 买入价 − 止损价；
- 股数 = 最多亏 ÷ 每股风险，按一手（100 股）向下取整；再受"单只最多占总资金的比例"和可用资金限制；
- 一手都买不起（或一手的风险就超标）时给出说明，不硬凑。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

LOT: int = 100


def lot_size(code: str) -> int:
    """买入最小单位：沪深主板/创业板 100 股；科创板 200 股起（之后 1 股递增，这里按 200 处理）；北交所 100 股"""
    return 200 if code.startswith(("688", "689")) else LOT


def suggest_stop(entry: float, atr: float | None = None, ma20: float | None = None, max_pct: float = 0.08,
                 min_pct: float = 0.02) -> dict:
    """建议止损价：{stop, basis, pct}；basis 说明依据（白话）"""
    floor_price: float = entry * (1 - max_pct)                   # 最远不超过这里
    cands: list[tuple[float, str]] = []
    if atr and atr > 0:
        cands.append((entry - 2 * atr, "买入价下方 2 倍平均波幅"))
    if ma20 and 0 < ma20 < entry:
        cands.append((ma20 * 0.98, "20 日均线下方 2%"))
    if cands:
        stop, basis = max(cands, key=lambda x: x[0])             # 取更近的那个
        if stop < floor_price:
            stop, basis = floor_price, f"买入价下方 {max_pct * 100:.0f}%（技术止损太远，按上限）"
    else:
        stop, basis = floor_price, f"买入价下方 {max_pct * 100:.0f}%"
    if stop > entry * (1 - min_pct):
        stop, basis = entry * (1 - min_pct), f"买入价下方 {min_pct * 100:.0f}%（留出正常波动的空间）"
    stop = math.floor(stop * 100) / 100
    return {"stop": stop, "basis": basis, "pct": 1 - stop / entry}


@dataclass
class Sizing:
    shares: int
    amount: float
    risk_amount: float            # 按止损卖出时大约亏多少（未含手续费）
    risk_pct: float               # 占总资金的比例
    position_pct: float           # 占总资金的比例
    limited_by: str               # risk / single_cap / cash / none
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def position_size(capital: float, entry: float, stop: float, risk_per_trade: float = 0.01,
                  max_single_pct: float = 0.2, cash: float | None = None, lot: int = LOT) -> Sizing:
    """按风险算股数（整手）"""
    if entry <= 0 or stop <= 0 or stop >= entry:
        return Sizing(0, 0.0, 0.0, 0.0, 0.0, "none", "止损价必须低于买入价")
    per_share: float = entry - stop
    budget_risk: float = capital * risk_per_trade
    by_risk: int = int(budget_risk / per_share // lot * lot)
    by_cap: int = int(capital * max_single_pct / entry // lot * lot)
    by_cash: int = int((cash if cash is not None else capital) / entry // lot * lot)
    shares: int = min(by_risk, by_cap, by_cash)
    limited: str = "risk" if shares == by_risk else "single_cap" if shares == by_cap else "cash"
    if shares <= 0:
        one_lot_risk: float = per_share * lot
        if by_cash <= 0 or by_cap <= 0:
            note = f"一手（{lot} 股）要 {entry * lot:,.0f} 元，超过了可用资金或单只上限，买不了"
        else:
            note = (f"一手的风险约 {one_lot_risk:,.0f} 元，超过了单笔最多亏 {budget_risk:,.0f} 元，"
                    "按规则不买（可以换止损更近的买点，或换便宜些的股票）")
        return Sizing(0, 0.0, 0.0, 0.0, 0.0, limited, note)
    amount: float = shares * entry
    risk_amount: float = shares * per_share
    notes = {"risk": f"按“一笔最多亏总资金 {risk_per_trade * 100:.1f}%”算出",
             "single_cap": f"受“单只最多占总资金 {max_single_pct * 100:.0f}%”限制",
             "cash": "受可用资金限制"}
    return Sizing(shares, round(amount, 2), round(risk_amount, 2), risk_amount / capital, amount / capital, limited,
                  notes[limited])
