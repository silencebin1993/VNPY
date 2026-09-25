"""
下单前的纪律与风控检查（模拟盘和实盘一样）。每条结果：{level, code, message}
- block（禁止）：不能下单；
- confirm（需确认）：页面弹窗说明风险，你勾选"我知道风险"后才能下单，并记入复盘（算违规）；
- warn（提醒）/ info（说明）：只提示。
上下文 ctx（由调用方准备，缺的项跳过对应检查）：
  quote {price, pct(%), limit_up, limit_down}、plan {stop}（买入）、stage（主力阶段 key）、risk_level（排雷 red/yellow/green）、
  regime_cap（大盘环境给的总仓位上限 0~1）、settings（profile/risk/live 字典）、now_session。
"""
from __future__ import annotations

import sqlite3

from . import ledger, rules
from .engine import OrderRequest, available_cash, snapshot


def _item(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def summarize(items: list[dict]) -> dict:
    lv = {x["level"] for x in items}
    return {"blocked": "block" in lv, "need_confirm": "confirm" in lv and "block" not in lv, "items": items}


def today_pnl(conn: sqlite3.Connection, account_id: str) -> float:
    """今天的盈亏 ≈ 当前总资产 − 上一个交易日的资产快照"""
    snap = snapshot(conn, account_id)
    last = ledger.one(conn, "SELECT total FROM equity WHERE account_id=? AND date<? ORDER BY date DESC LIMIT 1",
                      (account_id, ledger.today().isoformat()))
    return snap["total"] - (last["total"] if last else snap["account"]["initial_cash"])


def losing_streak(conn: sqlite3.Connection, account_id: str) -> tuple[int, str | None]:
    """最近连续亏损的笔数，以及最后一笔的平仓日期"""
    js = ledger.rows(conn, "SELECT pnl, closed FROM journal WHERE account_id=? ORDER BY id DESC LIMIT 20", (account_id,))
    n: int = 0
    for j in js:
        if (j["pnl"] or 0) < 0:
            n += 1
        else:
            break
    return n, js[0]["closed"] if js else None


def check(conn: sqlite3.Connection, account: dict, req: OrderRequest, ctx: dict) -> dict:
    s: dict = ctx.get("settings") or {}
    prof: dict = s.get("profile") or {}
    rk: dict = s.get("risk") or {}
    live: dict = s.get("live") or {}
    q: dict = ctx.get("quote") or {}
    items: list[dict] = []
    px: float | None = req.price or q.get("price")
    code: str = req.code
    pos = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (account["id"], code))
    snap: dict = snapshot(conn, account["id"])
    equity: float = snap["total"]

    # ---------------- 实盘总开关
    if account["kind"] == "live":
        if not live.get("enabled"):
            items.append(_item("block", "live_off", "实盘总开关是关着的（在“交易 → 设置”里打开后才能用真钱下单）"))
        cap = float(live.get("max_order_amount") or 0)
        if cap and px and px * req.qty > cap:
            items.append(_item("block", "live_amount", f"单笔金额 {px * req.qty:,.0f} 元超过了实盘单笔上限 {cap:,.0f} 元"))
    # ---------------- 价格
    if req.kind == "limit" and px:
        lu, ld = q.get("limit_up"), q.get("limit_down")
        if lu and px > lu + 1e-6:
            items.append(_item("block", "price_band", f"委托价 {px:.2f} 高于涨停价 {lu:.2f}，交易所不接受"))
        if ld and px < ld - 1e-6:
            items.append(_item("block", "price_band", f"委托价 {px:.2f} 低于跌停价 {ld:.2f}，交易所不接受"))
        last = q.get("price")
        if last and abs(px / last - 1) > 0.02 and ctx.get("now_session") == "open":
            items.append(_item("warn", "price_cage", f"委托价离现价 {abs(px / last - 1) * 100:.1f}%：连续竞价时超过 ±2% 的限价单可能被交易所拒绝（价格笼子）"))

    if req.side == "buy":
        boards: list[str] = list(prof.get("boards") or ["main"])
        board: str = ("star" if code.startswith(("688", "689")) else "chinext" if code.startswith("30")
                      else "bj" if code.startswith(("8", "4", "92")) else "main")
        if board not in boards:
            names = {"main": "沪深主板", "chinext": "创业板", "star": "科创板", "bj": "北交所"}
            items.append(_item("block", "board", f"这是{names[board]}股票，你在“我的情况”里没有这个板块的交易权限"))
        err = rules.check_qty(code, "buy", req.qty)
        if err:
            items.append(_item("block", "lot", err))
        plan: dict = ctx.get("plan") or {}
        stop = plan.get("stop")
        if rk.get("require_stop", True) and not stop:
            items.append(_item("block", "no_stop", "买入前必须定好止损价（交易计划）：不知道错了在哪认输，就不要买"))
        if px and req.qty:
            amount: float = px * req.qty
            fee = rules.fees("buy", amount, ledger.today())["total"]
            if amount + fee > available_cash(conn, account["id"]) + 1e-6:
                items.append(_item("block", "cash", f"可用资金不够：需要约 {amount + fee:,.0f} 元，可用 {available_cash(conn, account['id']):,.0f} 元"))
            if stop and stop < px:
                risk_amt: float = (px - stop) * req.qty
                budget: float = equity * float(prof.get("risk_per_trade") or 0.01)
                if risk_amt > budget * 1.1:
                    items.append(_item("confirm", "risk_per_trade", f"如果跌到止损价，这笔会亏约 {risk_amt:,.0f} 元，"
                                                                    f"超过了“一笔最多亏 {budget:,.0f} 元”的纪律（建议减少股数）"))
            single: float = float(rk.get("max_single_pct") or 0.2)
            held_v: float = (pos["qty"] * (pos["last_price"] or pos["cost"])) if pos else 0.0
            if equity and (held_v + amount) / equity > single + 1e-6:
                items.append(_item("block", "single_cap", f"买完后这只股票会占总资产 {(held_v + amount) / equity * 100:.0f}%，"
                                                          f"超过单只上限 {single * 100:.0f}%"))
            cap_total = ctx.get("regime_cap")
            if cap_total is not None and equity:
                after: float = (snap["market_value"] + amount) / equity
                if after > float(cap_total) + 1e-6:
                    items.append(_item("confirm", "regime_cap", f"买完后总仓位 {after * 100:.0f}%，超过了当前大盘环境建议的上限 {float(cap_total) * 100:.0f}%"))
        pct = q.get("pct")
        if pct is not None and pct >= float(rk.get("chase_warn_pct") or 5):
            items.append(_item("confirm", "chase", f"这只股票今天已经涨了 {pct:.1f}%：追高买入很容易买在短期高点"))
        if q.get("limit_up") and q.get("price") and q["price"] >= q["limit_up"] - 0.005:
            items.append(_item("warn", "at_limit_up", "现在是涨停价：排队也很可能买不到，第二天低开的风险也大"))
        stg = ctx.get("stage")
        if stg in ("distribution", "decline"):
            lvl = "block" if rk.get("block_distribution", True) else "confirm"
            items.append(_item(lvl, "stage", "主力阶段判断为“疑似出货”，不要买" if stg == "distribution" else "主力阶段判断为“下跌趋势”，不要抄底"))
        if ctx.get("risk_level") == "red":
            items.append(_item("block" if rk.get("block_risk_red", True) else "confirm", "risk_red", "排雷体检是红灯（有明显风险），不要买"))
        if pos and rk.get("warn_average_down", True) and q.get("price") and q["price"] < pos["cost"] * 0.97:
            items.append(_item("confirm", "average_down", f"你已经持有这只股票而且在亏（成本 {pos['cost']:.2f}），越跌越买是新手亏大钱最常见的方式"))
        limit = float(rk.get("daily_loss_limit") or 0)
        if limit and equity:
            pnl = today_pnl(conn, account["id"])
            if pnl <= -limit * equity:
                items.append(_item("block", "daily_loss", f"今天已经亏了 {abs(pnl):,.0f} 元（超过总资产的 {limit * 100:.0f}%），今天不能再买入：先停下来"))
        n_loss = int(rk.get("cooldown_losses") or 0)
        if n_loss:
            streak, last_closed = losing_streak(conn, account["id"])
            if streak >= n_loss and last_closed:
                from datetime import date as _d
                days_since = (ledger.today() - _d.fromisoformat(last_closed)).days
                cool = int(rk.get("cooldown_days") or 2)
                if days_since <= cool * 1.45:
                    items.append(_item("block", "cooldown", f"已经连续亏了 {streak} 笔，进入冷静期（{cool} 个交易日内不能买入）：先复盘，别急着扳本"))
        if account["kind"] == "live" and req.source != "user":
            items.append(_item("block", "auto_buy", "实盘买入必须由你本人确认，程序不会自动买入"))
    else:
        avail: int = int(pos["available"]) if pos else 0
        if req.kind in ("limit", "market"):
            err = rules.check_qty(code, "sell", req.qty, avail)
            if err:
                items.append(_item("block", "sell_qty", err + ("（今天买的要明天才能卖，T+1）" if pos and pos["qty"] > avail else "")))
        plan_s = ledger.one(conn, "SELECT * FROM plans WHERE account_id=? AND code=? AND status='active'", (account["id"], code))
        if plan_s and plan_s.get("target") and q.get("price") and q["price"] < plan_s["target"] and q["price"] > plan_s["stop"] and req.kind == "limit":
            items.append(_item("info", "before_target", f"还没到计划的目标价 {plan_s['target']:.2f}，也没到止损价 {plan_s['stop']:.2f}：确定要提前卖吗？"))
    return summarize(items)
