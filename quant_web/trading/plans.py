"""
交易计划：每笔买入都要先定好"为什么买、跌到哪止损、目标在哪、怎么移动止盈、最多拿几天"，程序按计划盯着。

- 买入成交 → 计划生效（active），并自动挂一张"止损条件单"（模拟盘由程序撮合；实盘按"自动下单范围"处理）；
- 每天收盘：记录最高价，按移动止盈规则把止损往上提（只升不降）；跌破 10/20 日线的规则收盘确认后提醒/卖出；
  持有超过计划天数提醒；
- 手动调低止损要二次确认，并记为违规（"挪低止损"是新手亏大钱最常见的原因）；
- 除权除息：止损、目标、成本、最高价同比例调整（避免把除权跳空当成跌破止损）；
- 仓位全部卖出 → 计划结束，写复盘记录（盈亏、R 倍数、持有天数、违规）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from . import ledger

TRAIL_RULES: dict[str, str] = {
    "none": "固定止损（不移动）",
    "breakeven": "赚到 1 倍风险（涨幅 = 止损幅度）后，把止损提到买入价（保本）",
    "trail_pct8": "止损跟着最高价走：始终在最高收盘价下方 8%",
    "trail_atr": "止损跟着最高价走：最高收盘价下方 2 倍平均波幅",
    "ma10": "收盘跌破 10 日均线就卖",
    "ma20": "收盘跌破 20 日均线就卖",
}
PLAN_STATUSES: tuple[str, ...] = ("pending", "active", "closed", "cancelled")


def create(conn: sqlite3.Connection, account_id: str, code: str, name: str | None, entry: float | None, stop: float,
           target: float | None = None, trail: str = "breakeven", max_days: int | None = 20, reason: str = "",
           qty_plan: int | None = None) -> dict:
    if not stop or stop <= 0:
        raise ValueError("止损价必须填写")
    if entry and stop >= entry:
        raise ValueError("止损价必须低于计划买入价")
    if target is not None and entry and target <= entry:
        raise ValueError("目标价必须高于计划买入价")
    if trail not in TRAIL_RULES:
        raise ValueError(f"不认识的移动止盈规则「{trail}」")
    pid: str = ledger.new_id("plan")
    conn.execute("INSERT INTO plans(id, account_id, code, name, status, entry, stop, initial_stop, target, trail, max_days, reason, "
                 "created, highest, qty_plan, history) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, account_id, code, name, "pending", entry, stop, stop, target, trail, max_days, reason, ledger.now(),
                  None, qty_plan, json.dumps([{"time": ledger.now(), "action": "create", "stop": stop}], ensure_ascii=False)))
    return ledger.one(conn, "SELECT * FROM plans WHERE id=?", (pid,))            # type: ignore[return-value]


def get(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    return ledger.one(conn, "SELECT * FROM plans WHERE id=?", (plan_id,))


def active_for(conn: sqlite3.Connection, account_id: str, code: str) -> dict | None:
    return ledger.one(conn, "SELECT * FROM plans WHERE account_id=? AND code=? AND status IN ('active','pending') "
                            "ORDER BY created DESC LIMIT 1", (account_id, code))


def _history(p: dict, action: str, **kw: object) -> str:
    h: list = json.loads(p.get("history") or "[]")
    h.append({"time": ledger.now(), "action": action, **kw})
    return json.dumps(h[-100:], ensure_ascii=False, default=str)


def _stop_order(conn: sqlite3.Connection, plan: dict) -> dict | None:
    return ledger.one(conn, "SELECT * FROM orders WHERE plan_id=? AND kind='stop' AND status='waiting_trigger'", (plan["id"],))


def ensure_stop_order(conn: sqlite3.Connection, plan: dict) -> None:
    """给生效的计划挂（或更新）止损条件单：数量 = 当前持仓"""
    pos = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (plan["account_id"], plan["code"]))
    if not pos or pos["qty"] <= 0:
        return
    o = _stop_order(conn, plan)
    if o:
        conn.execute("UPDATE orders SET trigger=?, qty=?, updated=? WHERE id=?", (plan["stop"], pos["qty"], ledger.now(), o["id"]))
        return
    from .engine import OrderRequest, place
    place(conn, plan["account_id"], OrderRequest(code=plan["code"], side="sell", qty=pos["qty"], kind="stop", trigger=plan["stop"],
                                                 name=plan.get("name"), reason="计划止损", plan_id=plan["id"], source="plan"))


def on_buy_fill(conn: sqlite3.Connection, account_id: str, code: str, plan_id: str, price: float, day: date) -> None:
    p = get(conn, plan_id)
    if p is None:
        return
    conn.execute("UPDATE plans SET status='active', opened=COALESCE(opened, ?), entry=COALESCE(entry, ?), highest=MAX(COALESCE(highest, 0), ?), "
                 "history=? WHERE id=?", (day.isoformat(), price, price, _history(p, "filled", price=price), plan_id))
    conn.execute("UPDATE positions SET plan_id=? WHERE account_id=? AND code=?", (plan_id, account_id, code))
    ensure_stop_order(conn, get(conn, plan_id))                                   # type: ignore[arg-type]


def update_stop(conn: sqlite3.Connection, plan_id: str, new_stop: float, reason: str, allow_lower: bool = False) -> dict:
    p = get(conn, plan_id)
    if p is None:
        raise ValueError("没有这个交易计划")
    if p["status"] not in ("active", "pending"):
        raise ValueError("这个计划已经结束")
    if new_stop <= 0:
        raise ValueError("止损价要大于 0")
    lowered: bool = new_stop < p["stop"] - 1e-9
    if lowered and not allow_lower:
        raise ValueError("调低止损需要二次确认：挪低止损是新手亏大钱最常见的原因，确定要这样做吗？")
    conn.execute("UPDATE plans SET stop=?, history=? WHERE id=?",
                 (new_stop, _history(p, "lower_stop" if lowered else "raise_stop", stop=new_stop, reason=reason), plan_id))
    ledger.audit(conn, p["account_id"], "update_stop", {"plan_id": plan_id, "from": p["stop"], "to": new_stop, "reason": reason})
    p = get(conn, plan_id)
    if p and p["status"] == "active":
        ensure_stop_order(conn, p)
    return p                                                                       # type: ignore[return-value]


def adjust_for_ex_rights(conn: sqlite3.Connection, account_id: str, code: str, ratio: float) -> None:
    for p in ledger.rows(conn, "SELECT * FROM plans WHERE account_id=? AND code=? AND status IN ('active','pending')", (account_id, code)):
        vals = {k: (p[k] * ratio if p.get(k) else p.get(k)) for k in ("entry", "stop", "initial_stop", "target", "highest")}
        conn.execute("UPDATE plans SET entry=?, stop=?, initial_stop=?, target=?, highest=?, history=? WHERE id=?",
                     (vals["entry"], vals["stop"], vals["initial_stop"], vals["target"], vals["highest"],
                      _history(p, "ex_rights", ratio=ratio), p["id"]))
    conn.execute("UPDATE orders SET trigger = trigger * ?, price = CASE WHEN price IS NULL THEN NULL ELSE price * ? END, updated=? "
                 "WHERE account_id=? AND code=? AND status IN ('waiting_trigger','submitted','pending_manual') AND kind IN ('stop','take_profit')",
                 (ratio, ratio, ledger.now(), account_id, code))


def evaluate_close(conn: sqlite3.Connection, plan: dict, close: float, ma10: float | None, ma20: float | None, atr: float | None,
                   day: date) -> list[dict]:
    """收盘后按规则更新止损、给出提醒；返回动作列表 [{type: raise_stop/exit_signal/overdue/target, ...}]"""
    actions: list[dict] = []
    highest: float = max(plan.get("highest") or 0, close)
    new_stop: float = plan["stop"]
    entry: float = plan.get("entry") or close
    risk: float = entry - (plan.get("initial_stop") or plan["stop"])
    rule: str = plan.get("trail") or "none"
    if rule == "breakeven" and risk > 0 and highest >= entry + risk:
        new_stop = max(new_stop, entry)
    elif rule == "trail_pct8":
        new_stop = max(new_stop, highest * 0.92)
    elif rule == "trail_atr" and atr:
        new_stop = max(new_stop, highest - 2 * atr)
    elif rule in ("ma10", "ma20"):
        ma = ma10 if rule == "ma10" else ma20
        if ma and close < ma:
            actions.append({"type": "exit_signal", "text": f"收盘 {close:.2f} 跌破 {rule[2:]} 日均线 {ma:.2f}，按计划应卖出"})
    new_stop = round(new_stop, 2)
    conn.execute("UPDATE plans SET highest=? WHERE id=?", (highest, plan["id"]))
    if new_stop > plan["stop"] + 1e-9:
        update_stop(conn, plan["id"], new_stop, f"移动止盈（{TRAIL_RULES[rule]}）")
        actions.append({"type": "raise_stop", "stop": new_stop, "text": f"止损上移到 {new_stop:.2f}（{TRAIL_RULES[rule]}）"})
    if plan.get("target") and close >= plan["target"]:
        actions.append({"type": "target", "text": f"收盘 {close:.2f} 已到目标价 {plan['target']:.2f}，可以按计划卖出一部分或提高止损"})
    if plan.get("max_days") and plan.get("opened"):
        held: int = (day - date.fromisoformat(plan["opened"])).days
        if held > plan["max_days"] * 1.45:                            # 交易日 ≈ 日历日 × 0.69
            actions.append({"type": "overdue", "text": f"已经持有约 {held} 天，超过计划的 {plan['max_days']} 个交易日：按计划应该离场或重新评估"})
    return actions


def on_position_closed(conn: sqlite3.Connection, account_id: str, code: str, day: date) -> None:
    """持仓清零：写复盘记录、结束计划、撤掉残留的条件单"""
    pos = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (account_id, code))
    opened: str = pos["opened"] if pos and pos.get("opened") else day.isoformat()
    fills = ledger.rows(conn, "SELECT * FROM fills WHERE account_id=? AND code=? AND trade_date>=? ORDER BY id", (account_id, code, opened))
    buys = [f for f in fills if f["side"] == "buy"]
    sells = [f for f in fills if f["side"] == "sell"]
    bq: int = sum(f["qty"] for f in buys)
    sq: int = sum(f["qty"] for f in sells)
    entry: float | None = sum(f["price"] * f["qty"] for f in buys) / bq if bq else None
    exit_: float | None = sum(f["price"] * f["qty"] for f in sells) / sq if sq else None
    fee: float = sum(f["commission"] + f["stamp"] + f["transfer"] for f in fills)
    pnl: float = sum(f["amount"] for f in sells) - sum(f["amount"] for f in buys) - fee
    plan = ledger.one(conn, "SELECT * FROM plans WHERE id=?", (pos["plan_id"],)) if pos and pos.get("plan_id") else None
    r_mult: float | None = None
    violations: list[str] = []
    if plan and entry and plan.get("initial_stop") and entry > plan["initial_stop"] and exit_ is not None:
        r_mult = (exit_ - entry) / (entry - plan["initial_stop"])
        hist = json.loads(plan.get("history") or "[]")
        if any(h.get("action") == "lower_stop" for h in hist):
            violations.append("挪低过止损")
        if plan.get("max_days") and plan.get("opened") and (day - date.fromisoformat(plan["opened"])).days > plan["max_days"] * 1.45:
            violations.append("超过计划持有天数")
    elif not plan:
        violations.append("没有交易计划")
    orders = ledger.rows(conn, "SELECT flags, reason, side FROM orders WHERE account_id=? AND code=? AND created>=?", (account_id, code, opened))
    for o in orders:
        fl: dict = json.loads(o.get("flags") or "{}")
        if fl.get("chase"):
            violations.append("追高买入")
        if fl.get("average_down"):
            violations.append("亏损时补仓摊平")
        if fl.get("override"):
            violations.append("无视风控提醒下单")
    last_sell = next((o for o in reversed(orders) if o["side"] == "sell"), None)
    exit_reason: str = (last_sell or {}).get("reason") or ""
    days: int = (day - date.fromisoformat(opened)).days
    conn.execute("INSERT INTO journal(account_id, plan_id, code, name, opened, closed, qty, entry, exit, pnl, r_multiple, days, exit_reason, violations) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (account_id, plan["id"] if plan else None, code, (pos or {}).get("name"), opened, day.isoformat(), bq, entry, exit_,
                  round(pnl, 2), r_mult, days, exit_reason, json.dumps(sorted(set(violations)), ensure_ascii=False)))
    if plan:
        conn.execute("UPDATE plans SET status='closed', closed=?, history=? WHERE id=?", (day.isoformat(), _history(plan, "closed", pnl=pnl), plan["id"]))
    conn.execute("UPDATE orders SET status='cancelled', message='持仓已清空', updated=? WHERE account_id=? AND code=? "
                 "AND status='waiting_trigger'", (ledger.now(), account_id, code))
