"""
账本上的交易动作：下单（冻结资金）、成交记账（费用、成本、T+1）、撤单/过期、日终结算（T+1 解锁、除权、资产快照）、
按日线或实时行情撮合（模拟盘）。模拟盘、手动实盘（你回填成交）和券商接口同步都走这里，口径一致。

订单种类 kind：limit（限价）/ market（市价，开盘前下的就是"开盘价买/卖"）/ stop（止损：到价卖出）/ take_profit（止盈：到价卖出）。
状态 status：submitted（待成交）/ waiting_trigger（条件单等触发）/ pending_manual（等你在券商下单并回填）/
queued（实盘自动接口在非交易时段先排队，开盘时提交）/ partial / filled /
cancelled / expired / rejected。
有效期 valid：day（当天有效，收盘后未成交就过期）/ gtc（一直有效，条件单用）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import calendar as tcal
from . import ledger, rules

OPEN_STATUSES: tuple[str, ...] = ("submitted", "waiting_trigger", "pending_manual", "partial", "queued")


@dataclass
class OrderRequest:
    code: str
    side: str                         # buy / sell
    qty: int
    kind: str = "limit"               # limit / market / stop / take_profit
    price: float | None = None        # 限价
    trigger: float | None = None      # 条件单触发价
    valid: str = "day"
    name: str | None = None
    reason: str = ""
    plan_id: str | None = None
    source: str = "user"              # user / strategy / monitor / plan
    flags: dict = field(default_factory=dict)


def _freeze_amount(req: OrderRequest, ref_price: float | None) -> float:
    if req.side != "buy":
        return 0.0
    px: float = req.price or ref_price or 0.0
    if req.kind == "market":
        px = (ref_price or px) * 1.1                       # 市价买按参考价上浮 10% 预留（涨停价附近）
    amount: float = px * req.qty
    return round(amount + max(amount * rules.COMMISSION, rules.MIN_COMMISSION) + amount * rules.TRANSFER_FEE, 2)


def available_cash(conn: sqlite3.Connection, account_id: str) -> float:
    acc = ledger.one(conn, "SELECT cash, frozen FROM accounts WHERE id=?", (account_id,))
    return float(acc["cash"] - acc["frozen"]) if acc else 0.0


def place(conn: sqlite3.Connection, account_id: str, req: OrderRequest, *, status: str | None = None,
          ref_price: float | None = None, trade_date: date | None = None, credit: float = 0.0) -> dict:
    """写入一笔委托（风控检查由调用方先做）；买单冻结资金。
    credit：同一个开盘先卖后买的换仓（量化选股组合调仓）——允许买单按"卖出后预计回笼的资金"下单；
    这种买单撮合时再核对一次现金（卖出没成交、钱不够就不买，当天过期），账户现金不会变成负数"""
    acc = ledger.one(conn, "SELECT * FROM accounts WHERE id=?", (account_id,))
    if acc is None:
        raise ValueError("没有这个账户")
    if req.side not in ("buy", "sell"):
        raise ValueError("方向只能是买入或卖出")
    if req.kind not in ("limit", "market", "stop", "take_profit"):
        raise ValueError("不认识的委托类型")
    if req.kind in ("stop", "take_profit") and (req.side != "sell" or not req.trigger):
        raise ValueError("止损/止盈条件单只能是卖出，并且要有触发价")
    if req.kind == "limit" and not req.price:
        raise ValueError("限价单要填价格")
    pos = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (account_id, req.code))
    if req.side == "sell":
        sellable: int = int(pos["available"]) if pos else 0
        committed: int = int(ledger.one(conn, "SELECT COALESCE(SUM(qty-filled_qty),0) AS n FROM orders WHERE account_id=? AND code=? "
                                             "AND side='sell' AND kind IN ('limit','market') AND status IN ('submitted','partial','pending_manual')",
                                        (account_id, req.code))["n"])
        if req.kind in ("limit", "market"):
            err = rules.check_qty(req.code, "sell", req.qty, max(sellable - committed, 0))
            if err:
                raise ValueError(err)
    else:
        err = rules.check_qty(req.code, "buy", req.qty)
        if err:
            raise ValueError(err)
    freeze: float = _freeze_amount(req, ref_price)
    if req.side == "buy" and freeze > available_cash(conn, account_id) + max(credit, 0.0) + 1e-6:
        raise ValueError(f"可用资金不够：需要约 {freeze:,.0f} 元，可用 {available_cash(conn, account_id):,.0f} 元")
    if req.side == "buy" and credit > 0:
        freeze = round(min(freeze, max(available_cash(conn, account_id), 0.0)), 2)   # 超出可用资金的部分等卖出回笼，不冻结
    flags: dict = {**req.flags, "freeze": freeze, **({"credit": True} if req.side == "buy" and credit > 0 else {})}
    oid: str = ledger.new_id("ord")
    st: str = status or ("waiting_trigger" if req.kind in ("stop", "take_profit") else "submitted")
    td: date = trade_date or tcal.order_trade_date()
    conn.execute(
        "INSERT INTO orders(id, account_id, code, name, side, kind, price, trigger, qty, status, valid, trade_date, created, updated,"
        " reason, plan_id, source, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, account_id, req.code, req.name, req.side, req.kind, req.price, req.trigger, int(req.qty), st,
         req.valid if req.kind in ("limit", "market") else "gtc", td.isoformat(), ledger.now(), ledger.now(), req.reason,
         req.plan_id, req.source, json.dumps(flags, ensure_ascii=False)))
    if freeze:
        conn.execute("UPDATE accounts SET frozen = frozen + ? WHERE id=?", (freeze, account_id))
    ledger.audit(conn, account_id, "place", {"order_id": oid, **req.__dict__})
    return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (oid,))            # type: ignore[return-value]


def _release(conn: sqlite3.Connection, order: dict, fraction: float = 1.0) -> None:
    flags: dict = json.loads(order.get("flags") or "{}")
    freeze: float = float(flags.get("freeze") or 0) * fraction
    if freeze:
        conn.execute("UPDATE accounts SET frozen = MAX(frozen - ?, 0) WHERE id=?", (freeze, order["account_id"]))


def cancel(conn: sqlite3.Connection, order_id: str, status: str = "cancelled", message: str = "") -> dict:
    o = ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if o is None:
        raise ValueError("没有这笔委托")
    if o["status"] not in OPEN_STATUSES:
        raise ValueError("这笔委托已经结束，不能再撤")
    left: float = (o["qty"] - o["filled_qty"]) / o["qty"] if o["qty"] else 0
    _release(conn, o, left)
    conn.execute("UPDATE orders SET status=?, updated=?, message=? WHERE id=?", (status, ledger.now(), message, order_id))
    ledger.audit(conn, o["account_id"], status, {"order_id": order_id, "message": message})
    return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))            # type: ignore[return-value]


def apply_fill(conn: sqlite3.Connection, order_id: str | None, account_id: str, code: str, side: str, qty: int, price: float,
               day: date, name: str | None = None, source: str = "paper", plan_id: str | None = None) -> dict:
    """记一笔成交：扣费、改现金和持仓（买入当天不能卖，T+1）、更新委托；全部卖完时写复盘记录并结束计划"""
    amount: float = round(qty * price, 2)
    fee: dict = rules.fees(side, amount, day)
    pos = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (account_id, code))
    if side == "buy":
        total_cost: float = amount + fee["total"]
        if pos:
            new_qty: int = pos["qty"] + qty
            new_cost: float = (pos["cost"] * pos["qty"] + total_cost) / new_qty
            conn.execute("UPDATE positions SET qty=?, cost=?, name=COALESCE(?, name), last_price=?, plan_id=COALESCE(plan_id, ?) "
                         "WHERE account_id=? AND code=?", (new_qty, new_cost, name, price, plan_id, account_id, code))
        else:
            conn.execute("INSERT INTO positions(account_id, code, name, qty, available, cost, opened, last_price, plan_id) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (account_id, code, name, qty, 0, total_cost / qty, day.isoformat(), price, plan_id))
        conn.execute("UPDATE accounts SET cash = cash - ? WHERE id=?", (total_cost, account_id))
    else:
        if pos is None or pos["qty"] < qty:
            raise ValueError("卖出数量超过持仓")
        conn.execute("UPDATE positions SET qty = qty - ?, available = MAX(available - ?, 0), last_price=? WHERE account_id=? AND code=?",
                     (qty, qty, price, account_id, code))
        conn.execute("UPDATE accounts SET cash = cash + ? WHERE id=?", (amount - fee["total"], account_id))
    conn.execute("INSERT INTO fills(order_id, account_id, code, name, side, qty, price, amount, commission, stamp, transfer, "
                 "trade_date, time, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (order_id, account_id, code, name, side, qty, price, amount, fee["commission"], fee["stamp"], fee["transfer"],
                  day.isoformat(), ledger.now(), source))
    if order_id:
        o = ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if o:
            filled: int = o["filled_qty"] + qty
            avg: float = ((o["avg_price"] or 0) * o["filled_qty"] + price * qty) / filled
            st: str = "filled" if filled >= o["qty"] else "partial"
            if side == "buy":
                _release(conn, o, qty / o["qty"])
            conn.execute("UPDATE orders SET filled_qty=?, avg_price=?, status=?, updated=? WHERE id=?",
                         (filled, avg, st, ledger.now(), order_id))
            plan_id = plan_id or o.get("plan_id")
    ledger.audit(conn, account_id, "fill", {"order_id": order_id, "code": code, "side": side, "qty": qty, "price": price, **fee})
    from . import plans
    if side == "buy" and plan_id:
        plans.on_buy_fill(conn, account_id, code, plan_id, price, day)
    after = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (account_id, code))
    if after and after["qty"] == 0:
        plans.on_position_closed(conn, account_id, code, day)
        conn.execute("DELETE FROM positions WHERE account_id=? AND code=?", (account_id, code))
    return {"amount": amount, **fee}


def expire_day_orders(conn: sqlite3.Connection, account_id: str, day: date) -> int:
    """当天有效的委托，收盘后没成交完就过期（释放冻结资金）"""
    olds = ledger.rows(conn, "SELECT * FROM orders WHERE account_id=? AND valid='day' AND status IN ('submitted','partial') "
                             "AND trade_date<=?", (account_id, day.isoformat()))
    for o in olds:
        cancel(conn, o["id"], "expired", "当天没有成交，已过期")
    return len(olds)


def _credit_ok(conn: sqlite3.Connection, o: dict, qty: int, px: float, day: date) -> bool:
    """按回笼资金下的买单（flags.credit）：现金（扣掉别的买单冻结的部分）够付这笔才成交"""
    flags: dict = json.loads(o.get("flags") or "{}")
    if not flags.get("credit"):
        return True
    acc = ledger.one(conn, "SELECT cash, frozen FROM accounts WHERE id=?", (o["account_id"],))
    mine: float = float(flags.get("freeze") or 0) * (qty / o["qty"])
    cost: float = qty * px + rules.fees("buy", qty * px, day)["total"]
    return cost <= float(acc["cash"]) - max(float(acc["frozen"]) - mine, 0.0) + 1e-6


def _still_open(conn: sqlite3.Connection, o: dict) -> dict | None:
    """撮合循环开始时读到的委托，轮到它时再看一眼最新状态（已成交 / 已撤的跳过）"""
    cur = ledger.one(conn, "SELECT * FROM orders WHERE id=?", (o["id"],))
    return cur if cur is not None and cur["status"] in ("submitted", "partial", "waiting_trigger") else None


def match_with_bars(conn: sqlite3.Connection, account_id: str, day: date, bars: dict[str, rules.Bar]) -> list[dict]:
    """模拟盘日终撮合：当天有效的委托 + 条件单，按当天日线判断能否成交"""
    done: list[dict] = []
    # 条件单（gtc）从创建起一直有效，不受委托日期限制（当天买的股票当天不能卖，由可卖数量保证）
    orders = ledger.rows(conn, "SELECT * FROM orders WHERE account_id=? AND status IN ('submitted','partial','waiting_trigger') "
                               "AND (trade_date<=? OR valid='gtc') ORDER BY created, rowid", (account_id, day.isoformat()))
    for o in orders:
        o = _still_open(conn, o)                                        # 前面的成交可能已经把它撤掉了（例如平仓后撤掉其余条件单）
        if o is None:
            continue
        bar: rules.Bar | None = bars.get(o["code"])
        if bar is None:
            continue                                                    # 停牌：不成交，条件单继续等
        left: int = o["qty"] - o["filled_qty"]
        if o["side"] == "sell":
            pos = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (account_id, o["code"]))
            left = min(left, int(pos["available"]) if pos else 0)
            if left <= 0:
                if o["kind"] in ("stop", "take_profit") and not pos:
                    cancel(conn, o["id"], "cancelled", "持仓已经没有了")
                continue
        px = rules.match_bar(o["side"], o["kind"], bar, o["price"], o["trigger"])
        if px is None:
            continue
        if o["side"] == "buy" and not _credit_ok(conn, o, left, px, day):
            continue                                                    # 先卖后买的换仓：卖出没回笼够钱，这笔不买（当天过期）
        info = apply_fill(conn, o["id"], account_id, o["code"], o["side"], left, px, day, o.get("name"), "paper_eod", o.get("plan_id"))
        done.append({"order_id": o["id"], "code": o["code"], "side": o["side"], "qty": left, "price": px, **info})
    return done


def match_with_quotes(conn: sqlite3.Connection, account_id: str, day: date, quotes: dict[str, dict]) -> list[dict]:
    """模拟盘盘中撮合（实时行情）"""
    done: list[dict] = []
    # 条件单（gtc）从创建起一直有效，不受委托日期限制（当天买的股票当天不能卖，由可卖数量保证）
    orders = ledger.rows(conn, "SELECT * FROM orders WHERE account_id=? AND status IN ('submitted','partial','waiting_trigger') "
                               "AND (trade_date<=? OR valid='gtc') ORDER BY created, rowid", (account_id, day.isoformat()))
    for o in orders:
        o = _still_open(conn, o)
        if o is None:
            continue
        q = quotes.get(o["code"])
        if not q:
            continue
        left: int = o["qty"] - o["filled_qty"]
        if o["side"] == "sell":
            pos = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (account_id, o["code"]))
            left = min(left, int(pos["available"]) if pos else 0)
            if left <= 0:
                continue
        px = rules.match_quote(o["side"], o["kind"], q, o["price"], o["trigger"])
        if px is None:
            continue
        info = apply_fill(conn, o["id"], account_id, o["code"], o["side"], left, px, day, o.get("name"), "paper_live", o.get("plan_id"))
        done.append({"order_id": o["id"], "code": o["code"], "side": o["side"], "qty": left, "price": px, **info})
    return done


def apply_ex_rights(conn: sqlite3.Connection, account_id: str, preclose: dict[str, float]) -> list[dict]:
    """除权除息（在撮合之前做）：preclose[code] = 今天的昨收参考价。和持仓记录的昨收不一致时：
    送转 → 股数按比例增加、成本降低；分红 → 现金入账；交易计划的止损/目标价和条件单的触发价同比例下调。
    处理完把"昨收"改成参考价，同一天不会重复处理。"""
    from . import plans

    done: list[dict] = []
    for p in ledger.rows(conn, "SELECT * FROM positions WHERE account_id=?", (account_id,)):
        pc = preclose.get(p["code"])
        if not pc or not p.get("last_close"):
            continue
        ex = rules.ex_rights(p["last_close"], pc)
        if ex is None:
            continue
        name = p.get("name") or p["code"]
        if ex["type"] == "shares":
            mult: float = ex["multiplier"]
            conn.execute("UPDATE positions SET qty=?, available=?, cost=? WHERE account_id=? AND code=?",
                         (int(round(p["qty"] * mult)), int(round(p["available"] * mult)), p["cost"] / mult, account_id, p["code"]))
            ledger.alert(conn, account_id, p["code"], "info", "ex_rights", f"{name} 送转股",
                         f"持股从 {p['qty']} 股变为 {int(round(p['qty'] * mult))} 股，成本价相应降低，止损价同比例下调")
        else:
            cash_in: float = round(p["qty"] * ex["per_share"], 2)
            if cash_in > 0:
                conn.execute("UPDATE accounts SET cash = cash + ? WHERE id=?", (cash_in, account_id))
                ledger.alert(conn, account_id, p["code"], "info", "ex_rights", f"{name} 分红到账",
                             f"约 {cash_in:,.2f} 元（按除息价差估算），止损价同比例下调")
        plans.adjust_for_ex_rights(conn, account_id, p["code"], ex["ratio"])
        conn.execute("UPDATE positions SET last_close=? WHERE account_id=? AND code=?", (pc, account_id, p["code"]))
        done.append({"code": p["code"], **ex})
    return done


def settle_day(conn: sqlite3.Connection, account_id: str, day: date, closes: dict[str, float]) -> dict:
    """日终结算（在撮合之后做）：更新收盘价和市值、写资产快照、未成交的当日委托过期、明天起 T+1 解锁"""
    for code, close in closes.items():
        conn.execute("UPDATE positions SET last_price=?, last_close=? WHERE account_id=? AND code=?", (close, close, account_id, code))
    acc = ledger.one(conn, "SELECT cash FROM accounts WHERE id=?", (account_id,))
    mv: float = float(ledger.one(conn, "SELECT COALESCE(SUM(qty*COALESCE(last_price,cost)),0) AS v FROM positions WHERE account_id=?",
                                 (account_id,))["v"])
    total: float = float(acc["cash"]) + mv if acc else mv
    conn.execute("INSERT INTO equity(account_id, date, cash, market_value, total) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(account_id, date) DO UPDATE SET cash=excluded.cash, market_value=excluded.market_value, total=excluded.total",
                 (account_id, day.isoformat(), acc["cash"] if acc else 0, mv, total))
    conn.execute("UPDATE positions SET available = qty WHERE account_id=?", (account_id,))       # T+1：明天起可以卖
    expire_day_orders(conn, account_id, day)
    return {"date": day.isoformat(), "cash": acc["cash"] if acc else 0, "market_value": mv, "total": total}


def snapshot(conn: sqlite3.Connection, account_id: str) -> dict:
    """账户当前状态：现金、冻结、持仓市值（按最新价）、总资产、浮动盈亏"""
    acc = ledger.one(conn, "SELECT * FROM accounts WHERE id=?", (account_id,))
    if acc is None:
        raise ValueError("没有这个账户")
    pos = ledger.rows(conn, "SELECT * FROM positions WHERE account_id=? ORDER BY opened", (account_id,))
    mv: float = sum(p["qty"] * (p["last_price"] or p["cost"]) for p in pos)
    for p in pos:
        px = p["last_price"] or p["cost"]
        p["market_value"] = p["qty"] * px
        p["pnl"] = (px - p["cost"]) * p["qty"]
        p["pnl_pct"] = px / p["cost"] - 1 if p["cost"] else None
    total: float = acc["cash"] + mv
    return {"account": {**acc, "settings": json.loads(acc.get("settings") or "{}")}, "cash": acc["cash"],
            "frozen": acc["frozen"], "available": acc["cash"] - acc["frozen"], "market_value": mv, "total": total,
            "pnl_total": total - acc["initial_cash"], "return": total / acc["initial_cash"] - 1, "positions": pos}
