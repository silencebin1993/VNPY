"""
策略的"每天收盘后一步"（回测和模拟跟踪调用的是同一个函数，所以两边规则完全一样）。在撮合、结算之后调用：
1. 策略自己的离场：持有满 max_days 个交易日、或出现出货迹象（exit_distribution）→ 下一个交易日开盘市价卖；
2. 有目标价的持仓挂止盈条件单（到价全部卖出；止损单先挂，同一天两个都碰到时按止损算，偏保守）；
3. 新开仓：空位 = 最多持有数 − 持仓 − 挂着的买单；按候选顺序，跳过已持有 / 已挂单 / 出货和下跌阶段的股票；
   止损按技术位置（sizing.suggest_stop），股数按"一笔最多亏总资金的 x%"，受单只上限、现金、大盘仓位上限限制；
   先建交易计划（止损、目标、移动止盈、最长持有），再挂下一个交易日的买单（开盘市价，或低 2% 的限价）。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ..trading import engine, ledger, plans, rules, sizing
from ..trading.engine import OrderRequest

PULLBACK: float = 0.02
OPEN: tuple[str, ...] = ("submitted", "partial", "queued", "pending_manual")


def strategy_step(conn, account_id: str, spec: dict, day: date, next_day: date, cands: list[str],
                  info: Callable[[str], dict | None], settings: dict, regime: str | None,
                  held_days: Callable[[str], int]) -> dict:
    """cands：今天的候选代码（按分数从高到低）；info(code) -> {close, ma20, atr, stage, name}（真实价格）；
    settings：{"profile": ..., "risk": ...}；held_days(opened) -> 从买入到今天经过了几个交易日"""
    risk: dict = settings["risk"]
    prof: dict = settings["profile"]
    out: dict = {"sells": 0, "buys": 0, "tp": 0, "skipped": []}
    positions: list[dict] = ledger.rows(conn, "SELECT * FROM positions WHERE account_id=?", (account_id,))
    orders: list[dict] = ledger.rows(conn, "SELECT * FROM orders WHERE account_id=? AND status IN ('submitted','partial','queued',"
                                           "'pending_manual','waiting_trigger')", (account_id,))
    selling: set[str] = {o["code"] for o in orders if o["side"] == "sell" and o["kind"] in ("limit", "market")}
    buying: set[str] = {o["code"] for o in orders if o["side"] == "buy"}
    tp_codes: set[str] = {o["code"] for o in orders if o["kind"] == "take_profit"}
    held: set[str] = {p["code"] for p in positions}

    # 1. 策略离场 + 2. 止盈单
    for pos in positions:
        code: str = pos["code"]
        plan: dict | None = plans.get(conn, pos["plan_id"]) if pos.get("plan_id") else None
        if pos["available"] <= 0 or code in selling:
            continue
        reason: str | None = None
        if pos.get("opened") and held_days(pos["opened"]) >= int(spec["max_days"]):
            reason = f"持有满 {spec['max_days']} 个交易日"
        elif spec.get("exit_distribution") and ((info(code) or {}).get("stage") == "distribution"):
            reason = "出现出货迹象"
        if reason:
            engine.place(conn, account_id, OrderRequest(code, "sell", int(pos["available"]), "market", name=pos.get("name"),
                                                        reason=f"策略离场：{reason}", plan_id=pos.get("plan_id"), source="strategy"),
                         trade_date=next_day)
            out["sells"] += 1
            continue
        if plan and plan.get("target") and code not in tp_codes:
            engine.place(conn, account_id, OrderRequest(code, "sell", int(pos["qty"]), "take_profit", trigger=float(plan["target"]),
                                                        name=pos.get("name"), reason="计划：到目标价止盈", plan_id=plan["id"],
                                                        source="strategy"), trade_date=next_day)
            out["tp"] += 1

    # 3. 新开仓
    slots: int = int(spec["max_positions"]) - len(positions) - len(buying)
    if slots <= 0 or not cands:
        return out
    snap: dict = engine.snapshot(conn, account_id)
    total: float = float(snap["total"])
    caps: dict = risk.get("regime_caps") or {}
    cap: float = float(caps.get(regime, 1.0)) if spec.get("regime") and regime else 1.0
    committed: float = float(snap["market_value"]) + float(snap["frozen"])
    for code in cands:
        if slots <= 0:
            break
        if code in held or code in buying:
            continue
        x: dict | None = info(code)
        if not x or not x.get("close"):
            continue
        if risk.get("block_distribution", True) and x.get("stage") in ("distribution", "decline"):
            out["skipped"].append(f"{code}：出货/下跌阶段")
            continue
        entry: float = float(x["close"])
        st: dict = sizing.suggest_stop(entry, x.get("atr"), x.get("ma20"), max_pct=float(risk.get("default_stop_pct", 0.08)))
        sz = sizing.position_size(total, entry, st["stop"], float(prof.get("risk_per_trade", 0.01)), float(risk.get("max_single_pct", 0.2)),
                                  cash=engine.available_cash(conn, account_id), lot=rules.lot_of(code))
        if sz.shares <= 0:
            continue
        amount: float = sz.shares * entry
        if committed + amount > cap * total + 1e-6:
            out["skipped"].append(f"{code}：超过大盘环境的仓位上限 {cap:.0%}")
            break
        target: float | None = round(entry + float(spec["target_r"]) * (entry - st["stop"]), 2) if spec.get("target_r") else None
        kind, price = ("limit", round(entry * (1 - PULLBACK), 2)) if spec["entry"] == "pullback" else ("market", None)
        plan = plans.create(conn, account_id, code, x.get("name"), entry, st["stop"], target, spec["trail"], int(spec["max_days"]),
                            f"策略：{spec['name']}", sz.shares)
        try:
            engine.place(conn, account_id, OrderRequest(code, "buy", sz.shares, kind, price, name=x.get("name"),
                                                        reason=f"策略：{spec['name']}", plan_id=plan["id"], source="strategy"),
                         ref_price=entry, trade_date=next_day)
        except ValueError as e:                                     # 资金不够等：这只跳过
            conn.execute("UPDATE plans SET status='cancelled' WHERE id=?", (plan["id"],))
            out["skipped"].append(f"{code}：{e}")
            continue
        committed += amount
        slots -= 1
        out["buys"] += 1
    return out
