"""
交易服务：网页接口、盘中监控、每日流水线都通过这里操作账户（风控检查 → 交易计划 → 券商接口 → 提醒）。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import polars as pl

from .. import settings as settings_mod
from . import calendar as tcal
from . import engine, ledger, plans, risk, rules, sizing
from .brokers import get_broker
from .engine import OrderRequest

log = logging.getLogger("quant_web.trading")


def settings_dict() -> dict:
    s = settings_mod.load()
    return {"profile": s.profile.model_dump(), "risk": s.risk.model_dump(), "live": s.live.model_dump(),
            "monitor": s.monitor.model_dump()}


def quote_map(codes: list[str]) -> dict[str, dict]:
    """实时报价（整理成成交规则要用的字段）"""
    if not codes:
        return {}
    from ..market import realtime
    out: dict[str, dict] = {}
    for q in realtime.quotes(list(dict.fromkeys(codes))):
        bids, asks = q.get("bids") or [], q.get("asks") or []
        out[q["code"]] = {
            "code": q["code"], "name": q.get("name"), "price": q.get("price"), "preclose": q.get("prev_close"),
            "pct": q.get("pct"), "limit_up": q.get("limit_up"), "limit_down": q.get("limit_down"),
            "open": q.get("open"), "high": q.get("high"), "low": q.get("low"), "time": q.get("time"),
            "bid1": bids[0][0] if bids else None, "bid1_vol": bids[0][1] if bids else 0,
            "ask1": asks[0][0] if asks else None, "ask1_vol": asks[0][1] if asks else 0,
        }
    return out


def regime_cap() -> float | None:
    try:
        from ..analysis import market
        r = market.last_regime()
        return float(r["cap"]) if r and r.get("cap") is not None else None
    except Exception:  # noqa: BLE001
        return None


def recent_levels(code: str, days: int = 60) -> dict:
    """最近的 20 日均线、10 日均线、14 日平均波幅（真实价格口径，用于建议止损和移动止盈）"""
    from ..market import history
    last = history.last_date()
    if last is None:
        return {}
    df = history.load_panel(codes=[code], start=last - timedelta(days=days * 2), columns=["high", "low", "close", "preclose"])
    if df.height < 15:
        return {}
    df = df.sort("date")
    tr = pl.max_horizontal(pl.col("high") - pl.col("low"), (pl.col("high") - pl.col("preclose")).abs(), (pl.col("preclose") - pl.col("low")).abs())
    df = df.with_columns(tr.alias("tr"))
    tail = df.tail(20)
    return {"ma20": float(df.tail(20)["close"].mean()), "ma10": float(df.tail(10)["close"].mean()),
            "atr": float(df.tail(14)["tr"].mean()), "close": float(tail["close"][-1]), "date": str(tail["date"][-1])}


def _ctx(account: dict, code: str, with_diag: bool = True) -> dict:
    ctx: dict = {"settings": settings_dict(), "regime_cap": regime_cap(), "now_session": tcal.session()}
    try:
        ctx["quote"] = quote_map([code]).get(code) or {}
    except Exception as e:  # noqa: BLE001  行情取不到时价格类检查跳过
        ctx["quote"] = {}
        ctx["quote_error"] = str(e)
    if with_diag:
        try:
            from ..analysis import diagnose
            d = diagnose.full(code)
            ctx["stage"] = d["stage"]["key"]
            ctx["risk_level"] = d["risk"]["level"]
            ctx["diag_name"] = d.get("name")
        except Exception as e:  # noqa: BLE001  诊断失败时相关检查跳过（页面会说明）
            ctx["diag_error"] = str(e)
    return ctx


def _req(order: dict) -> OrderRequest:
    try:
        return OrderRequest(code=str(order["code"]), side=str(order["side"]), qty=int(order["qty"]),
                            kind=str(order.get("kind") or "limit"), price=float(order["price"]) if order.get("price") else None,
                            trigger=float(order["trigger"]) if order.get("trigger") else None, valid=str(order.get("valid") or "day"),
                            name=order.get("name"), reason=str(order.get("reason") or "")[:100])
    except (KeyError, TypeError, ValueError):
        raise ValueError("委托内容不完整：要有股票代码、方向、数量") from None


def preview(account_id: str, order: dict, plan: dict | None = None) -> dict:
    """下单前预览：风控检查 + 建议止损与股数"""
    account = ledger.get_account(account_id)
    req = _req(order)
    ctx = _ctx(account, req.code, with_diag=req.side == "buy")
    q: dict = ctx.get("quote") or {}
    suggest: dict | None = None
    if req.side == "buy":
        entry = req.price or q.get("price")
        lv = recent_levels(req.code)
        if entry:
            st = sizing.suggest_stop(entry, lv.get("atr"), lv.get("ma20"), max_pct=ctx["settings"]["risk"]["default_stop_pct"])
            stop = (plan or {}).get("stop") or st["stop"]
            with ledger.connect() as c:
                snap = engine.snapshot(c, account_id)
            sz = sizing.position_size(snap["total"], entry, stop, ctx["settings"]["profile"]["risk_per_trade"],
                                      ctx["settings"]["risk"]["max_single_pct"], cash=snap["available"], lot=rules.lot_of(req.code))
            suggest = {"stop": st["stop"], "stop_basis": st["basis"], "shares": sz.shares, "amount": sz.amount,
                       "risk_amount": sz.risk_amount, "note": sz.note, "target": round(entry + 2 * (entry - stop), 2), "levels": lv}
    # 没有自己填止损时，按建议止损检查（正式下单时也会用这个止损建交易计划）
    eff_plan: dict = dict(plan or {})
    if req.side == "buy" and not eff_plan.get("stop") and suggest:
        eff_plan["stop"] = suggest["stop"]
    with ledger.connect() as c:
        checks = risk.check(c, account, req, {**ctx, "plan": eff_plan})
    return {"checks": checks, "quote": q, "stage": ctx.get("stage"), "risk_level": ctx.get("risk_level"),
            "suggest": suggest, "regime_cap": ctx.get("regime_cap"), "session": ctx.get("now_session"),
            "warnings": [w for w in (ctx.get("quote_error"), ctx.get("diag_error")) if w]}


def submit(account_id: str, order: dict, plan: dict | None = None, acknowledge: bool = False, source: str = "user") -> dict:
    """正式下单：风控不过 → 报错；需要确认而没确认 → 报错；买入先建交易计划；实盘自动接口在非交易时段先排队到开盘"""
    account = ledger.get_account(account_id)
    req = _req(order)
    req.source = source
    pv = preview(account_id, order, plan)
    chk = pv["checks"]
    if chk["blocked"]:
        raise ValueError("；".join(x["message"] for x in chk["items"] if x["level"] == "block"))
    confirms = [x for x in chk["items"] if x["level"] == "confirm"]
    if confirms and not acknowledge:
        raise ValueError("需要你确认这些风险：" + "；".join(x["message"] for x in confirms))
    codes = {x["code"] for x in confirms}
    req.flags = {"chase": "chase" in codes, "average_down": "average_down" in codes, "override": bool(confirms),
                 "confirmed": [x["message"] for x in confirms]}
    s = settings_dict()
    q: dict = pv["quote"] or {}
    with ledger.connect() as c:
        plan_row = None
        if req.side == "buy":
            p = plan or {}
            if not p.get("stop") and pv.get("suggest"):
                p = {**p, "stop": pv["suggest"]["stop"]}
            plan_row = plans.create(c, account_id, req.code, req.name or q.get("name"), req.price or q.get("price"), float(p["stop"]),
                                    float(p["target"]) if p.get("target") else None, p.get("trail") or "breakeven",
                                    int(p["max_days"]) if p.get("max_days") else 20, str(p.get("reason") or req.reason)[:200], req.qty)
            req.plan_id = plan_row["id"]
        broker = get_broker(account, {"live": s["live"], "quote": q})
        if broker.is_live and broker.can_auto and tcal.session() not in ("auction", "open"):
            o = engine.place(c, account_id, req, ref_price=q.get("price"), status="queued")
            ledger.alert(c, account_id, req.code, "info", "queued", f"{req.name or req.code} 委托已排队",
                         "现在不是交易时间：程序会在下一个交易日 9:15 集合竞价开始后自动提交（电脑和券商软件要开着）")
        else:
            o = broker.place(c, req, ref_price=q.get("price"))
        ledger.audit(c, account_id, "submit", {"order": order, "plan": plan, "acknowledged": [x["message"] for x in confirms]})
    return {"order": o, "plan": plan_row, "checks": chk}


def cancel(account_id: str, order_id: str) -> dict:
    account = ledger.get_account(account_id)
    with ledger.connect() as c:
        o = ledger.one(c, "SELECT * FROM orders WHERE id=? AND account_id=?", (order_id, account_id))
        if o is None:
            raise ValueError("没有这笔委托")
        if o["status"] in ("queued", "waiting_trigger"):         # 排队中 / 程序自己盯的条件单：券商那边没有这笔委托
            return engine.cancel(c, order_id)
        return get_broker(account, {"live": settings_dict()["live"]}).cancel(c, order_id)


def confirm_fill(account_id: str, order_id: str, qty: int, price: float, day: date | None = None) -> dict:
    account = ledger.get_account(account_id)
    if account["broker"] != "manual":
        raise ValueError("只有“实盘-手动”账户需要手动确认成交")
    from .brokers.manual import ManualBroker
    with ledger.connect() as c:
        return ManualBroker(account).confirm_fill(c, order_id, int(qty), float(price), day)


def import_statement(account_id: str, text: str) -> dict:
    account = ledger.get_account(account_id)
    if account["broker"] != "manual":
        raise ValueError("交割单导入只用于“实盘-手动”账户")
    from .brokers.manual import import_statement as _imp
    with ledger.connect() as c:
        return _imp(c, account_id, text)


def update_plan(account_id: str, plan_id: str, stop: float | None = None, target: float | None = None,
                trail: str | None = None, allow_lower: bool = False, note: str | None = None) -> dict:
    with ledger.connect() as c:
        p = plans.get(c, plan_id)
        if p is None or p["account_id"] != account_id:
            raise ValueError("没有这个交易计划")
        if stop is not None:
            plans.update_stop(c, plan_id, float(stop), "手动修改", allow_lower=allow_lower)
        if target is not None or trail is not None or note is not None:
            if trail is not None and trail not in plans.TRAIL_RULES:
                raise ValueError("不认识的移动止盈规则")
            c.execute("UPDATE plans SET target=COALESCE(?, target), trail=COALESCE(?, trail), note=COALESCE(?, note) WHERE id=?",
                      (target, trail, note, plan_id))
        return plans.get(c, plan_id)                                   # type: ignore[return-value]


# ---------------------------------------------------------------- 日终处理

def _bars_for(day: date, codes: list[str]) -> dict[str, rules.Bar]:
    from ..market import history
    if not codes:
        return {}
    df = history.load_panel(codes=codes, start=day, end=day, columns=["open", "high", "low", "close", "preclose", "tradestatus", "is_st", "volume"])
    out: dict[str, rules.Bar] = {}
    for r in df.to_dicts():
        if not r.get("volume") or (r.get("tradestatus") is not None and r["tradestatus"] == 0):
            continue                                                    # 停牌：不撮合
        ratio = rules_limit_ratio(r["code"], bool(r.get("is_st")))
        pc = r["preclose"]
        out[r["code"]] = rules.Bar(day, r["open"], r["high"], r["low"], r["close"], pc,
                                   round(pc * (1 + ratio) + 1e-6, 2) if pc else None, round(pc * (1 - ratio) + 1e-6, 2) if pc else None)
    return out


def rules_limit_ratio(code: str, is_st: bool) -> float:
    if code.startswith(("688", "689", "30")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.05 if is_st else 0.10


def _codes_in_play(conn, account_id: str) -> list[str]:
    a = [r["code"] for r in ledger.rows(conn, "SELECT code FROM positions WHERE account_id=?", (account_id,))]
    b = [r["code"] for r in ledger.rows(conn, "SELECT DISTINCT code FROM orders WHERE account_id=? AND status IN "
                                               "('submitted','partial','waiting_trigger','pending_manual','queued')", (account_id,))]
    return list(dict.fromkeys(a + b))


def run_eod(day: date | None = None, progress=None) -> dict:
    """收盘后（日线更新之后）：模拟盘按日线撮合、除权、移动止盈、资产快照；实盘同步成交、检查计划；最后推送摘要"""
    from .. import notify
    from ..market import history

    day = day or history.last_date()
    if day is None:
        return {"error": "还没有日线数据"}
    say = progress or (lambda f, m: None)
    s = settings_dict()
    summary: dict = {"date": str(day), "accounts": []}
    accounts = ledger.list_accounts()
    for i, acc in enumerate(accounts):
        say(i / max(len(accounts), 1), f"日终处理：{acc['name']}")
        with ledger.connect() as c:
            codes = _codes_in_play(c, acc["id"])
        bars = _bars_for(day, codes)
        fills: list[dict] = []
        notes: list[str] = []
        with ledger.connect() as c:
            if acc["kind"] == "paper":
                engine.apply_ex_rights(c, acc["id"], {k: b.preclose for k, b in bars.items()})
                fills = engine.match_with_bars(c, acc["id"], day, bars)
            else:
                broker = get_broker(acc, {"live": s["live"]})
                if broker.can_auto:
                    try:
                        res = broker.sync(c)
                    except Exception as e:  # noqa: BLE001  同步失败不影响日终结算
                        res = {"error": str(e)}
                    if res.get("error"):
                        notes.append(f"同步券商成交失败：{res['error']}")
            for p in ledger.rows(c, "SELECT * FROM plans WHERE account_id=? AND status='active'", (acc["id"],)):
                b = bars.get(p["code"])
                if b is None:
                    continue
                lv = recent_levels(p["code"])
                for a in plans.evaluate_close(c, p, b.close, lv.get("ma10"), lv.get("ma20"), lv.get("atr"), day):
                    level = "warn" if a["type"] in ("exit_signal", "overdue", "target") else "info"
                    ledger.alert(c, acc["id"], p["code"], level, f"plan_{a['type']}", f"{p.get('name') or p['code']}：{a['text']}")
                    notes.append(f"{p.get('name') or p['code']}：{a['text']}")
                    if a["type"] == "exit_signal" and acc["kind"] == "paper":
                        pos = ledger.one(c, "SELECT * FROM positions WHERE account_id=? AND code=?", (acc["id"], p["code"]))
                        qty = int(pos["available"]) - _open_sell_qty(c, acc["id"], p["code"]) if pos else 0
                        if qty > 0:
                            try:
                                engine.place(c, acc["id"], OrderRequest(p["code"], "sell", qty, "market", name=p.get("name"),
                                                                        reason="计划：收盘跌破均线", plan_id=p["id"], source="plan"),
                                             trade_date=tcal.next_trading_day(day))
                            except ValueError as e:
                                notes.append(f"{p.get('name') or p['code']}：离场卖单没有下成（{e}）")
            snap = engine.settle_day(c, acc["id"], day, {k: b.close for k, b in bars.items()})
        summary["accounts"].append({"id": acc["id"], "name": acc["name"], "fills": len(fills), "notes": notes, **snap})
        if fills or notes:
            body = "\n".join([f"{'买入' if f['side'] == 'buy' else '卖出'} {f['code']} {f['qty']} 股 @ {f['price']:.2f}" for f in fills] + notes)
            try:
                notify.send(f"{acc['name']}：今天 {len(fills)} 笔成交" if fills else f"{acc['name']}：交易计划提醒", body,
                            level="warn" if notes else "info", kind="eod", account_id=acc["id"])
            except Exception:  # noqa: BLE001  推送失败不影响结算
                log.exception("日终推送失败")
    ledger.set_meta("last_eod", str(day))
    return summary


# ---------------------------------------------------------------- 盘中

_last_px: dict[str, list[tuple[float, float]]] = {}


def _open_sell_qty(conn, account_id: str, code: str) -> int:
    """已经挂着、还没成交的普通卖单股数（和 engine.place 检查可卖数量的口径一致；条件单不算）"""
    r = ledger.one(conn, "SELECT COALESCE(SUM(qty-filled_qty),0) AS n FROM orders WHERE account_id=? AND code=? AND side='sell' "
                         "AND kind IN ('limit','market') AND status IN ('submitted','partial','pending_manual')", (account_id, code))
    return int(r["n"]) if r else 0


def _urgent(conn, acc: dict, code: str | None, kind: str, title: str, msg: str) -> None:
    """紧急提醒：站内信 + 推送；推送出错时至少写站内信"""
    from .. import notify
    try:
        notify.send(title, msg, level="urgent", kind=kind, code=code, account_id=acc["id"])
    except Exception:  # noqa: BLE001
        log.exception("紧急提醒推送失败")
        ledger.alert(conn, acc["id"], code, "urgent", kind, title, msg)


def _stop_action(conn, acc: dict, broker, plan: dict, q: dict, s: dict) -> str:
    """实盘跌破止损：能自动卖且设置允许 → 提交保护性卖单；否则（或自动卖失败）→ 文字说明请手动卖。返回提醒内容"""
    name: str = plan.get("name") or plan["code"]
    head: str = f"{name} 跌破止损价 {plan['stop']:.2f}（现价 {q['price']:.2f}）"
    live_on: bool = bool(s["live"].get("enabled"))
    if not (live_on and broker.can_auto and s["live"].get("auto_policy") in ("stop_only", "full")):
        return head + "：按计划应立即卖出。如果在券商 App 设了条件单，请确认是否已经触发"
    pos = ledger.one(conn, "SELECT * FROM positions WHERE account_id=? AND code=?", (acc["id"], plan["code"]))
    if not pos or pos["available"] <= 0:
        return head + "：现在没有可卖的股数（今天买的要下一个交易日才能卖），请下一个交易日开盘处理"
    qty: int = int(pos["available"]) - _open_sell_qty(conn, acc["id"], plan["code"])
    if qty <= 0:
        return head + "：已经有卖出委托挂着但还没成交，请检查委托价格，必要时撤单后按现价卖出"
    req = OrderRequest(plan["code"], "sell", qty, "market", name=name, reason="跌破止损自动卖出", plan_id=plan["id"], source="monitor")
    try:
        o = get_broker(acc, {"live": s["live"], "quote": q}).place(conn, req, ref_price=q["price"])
    except Exception as e:  # noqa: BLE001  自动卖失败绝不能吞掉提醒
        log.exception("止损自动卖出失败")
        return head + f"：自动卖出失败（{e}），请立即手动卖出"
    if o and o.get("status") == "rejected":
        return head + f"：自动卖出被拒绝（{o.get('message') or '原因不明'}），请立即手动卖出"
    return head + f"，已按设置自动提交卖出 {qty} 股，请留意是否成交"


def _live_intraday(conn, acc: dict, s: dict, quotes: dict[str, dict], today: date, ses: str, out: dict) -> None:
    """实盘账户的一次盘中检查。顺序：止损（最重要）→ 开盘提交排队委托 → 同步券商成交；每一步单独兜底，互不影响"""
    broker = get_broker(acc, {"live": s["live"]})
    live_on: bool = bool(s["live"].get("enabled"))
    if ses == "open":                  # 集合竞价的虚拟价格会被撤单操纵，不用来判断止损
        for p in ledger.rows(conn, "SELECT * FROM plans WHERE account_id=? AND status='active'", (acc["id"],)):
            q = quotes.get(p["code"])
            if not q or not q.get("price") or q["price"] > p["stop"]:
                continue
            key = f"stop_hit:{p['id']}:{today}"
            if ledger.one(conn, "SELECT value FROM meta WHERE key=?", (key,)):
                continue
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, ledger.now()))
            try:
                msg = _stop_action(conn, acc, broker, p, q, s)
            except Exception as e:  # noqa: BLE001
                log.exception("止损处理出错")
                msg = f"{p.get('name') or p['code']} 跌破止损价 {p['stop']:.2f}：处理出错（{e}），请立即手动卖出"
            out["alerts"] += 1
            _urgent(conn, acc, p["code"], "stop_hit", "跌破止损", msg)
    if live_on and broker.can_auto:
        for o in ledger.rows(conn, "SELECT * FROM orders WHERE account_id=? AND status='queued'", (acc["id"],)):
            q = quotes.get(o["code"]) or {}
            req = OrderRequest(o["code"], o["side"], o["qty"], o["kind"], o["price"], o["trigger"], o["valid"], o.get("name"),
                               o.get("reason") or "", o.get("plan_id"), "queued")
            engine.cancel(conn, o["id"], "cancelled", "开盘后已转成正式委托")
            try:
                placed = get_broker(acc, {"live": s["live"], "quote": q}).place(conn, req, ref_price=q.get("price"))
                out["sent"] += 1
                if placed and placed.get("status") == "rejected":
                    _urgent(conn, acc, o["code"], "order_rejected", "委托被拒绝",
                            f"{o.get('name') or o['code']} 排队的委托开盘提交后被拒绝：{placed.get('message') or '原因不明'}")
            except Exception as e:  # noqa: BLE001
                log.exception("排队委托提交失败")
                conn.execute("UPDATE orders SET message=? WHERE id=?", (f"开盘提交失败：{e}", o["id"]))
                _urgent(conn, acc, o["code"], "order_rejected", "委托没有提交成功",
                        f"{o.get('name') or o['code']} 排队的委托开盘提交失败：{e}")
        try:
            broker.sync(conn)
        except Exception:  # noqa: BLE001  同步失败下次再试
            log.exception("同步券商成交失败")


def run_intraday(now: datetime | None = None) -> dict:
    """盘中一次检查（监控线程每隔几十秒调用）：模拟盘撮合；实盘止损触发（按设置自动卖或紧急提醒）；排队的委托在开盘时提交；异动提醒"""
    from .. import notify

    now = now or tcal.china_now()
    ses = tcal.session(now)
    if ses not in ("auction", "open"):
        return {"session": ses}
    s = settings_dict()
    accounts = ledger.list_accounts()
    codes: list[str] = []
    with ledger.connect() as c:
        for a in accounts:
            codes += _codes_in_play(c, a["id"])
    watch: list[str] = []
    if s["monitor"].get("watch_watchlist"):
        try:
            from .. import config
            data = json.loads(config.WATCHLIST_FILE.read_text(encoding="utf-8")) if config.WATCHLIST_FILE.exists() else []
            watch = [x["code"] if isinstance(x, dict) else str(x) for x in data][:200]
        except Exception:  # noqa: BLE001
            watch = []
    quotes = quote_map(list(dict.fromkeys(codes + watch)))
    out: dict = {"session": ses, "fills": 0, "alerts": 0, "sent": 0}
    today: date = now.date()
    for acc in accounts:
        with ledger.connect() as c:
            if acc["kind"] == "paper":
                if ses == "open":
                    engine.apply_ex_rights(c, acc["id"], {k: q["preclose"] for k, q in quotes.items() if q.get("preclose")})
                    out["fills"] += len(engine.match_with_quotes(c, acc["id"], today, quotes))
                continue
            _live_intraday(c, acc, s, quotes, today, ses, out)
    # 异动提醒（5 分钟内涨跌超过设定幅度，同一只 30 分钟内只提醒一次）
    thr: float = float(s["monitor"].get("move_alert_pct") or 3.0)
    t_now = now.timestamp()
    for code, q in quotes.items():
        px = q.get("price")
        if not px:
            continue
        hist = [x for x in _last_px.get(code, []) if t_now - x[0] <= 330]
        hist.append((t_now, px))
        _last_px[code] = hist
        base = hist[0][1]
        if len(hist) >= 2 and base and abs(px / base - 1) * 100 >= thr:
            key = f"move:{code}:{int(t_now // 1800)}"
            with ledger.connect() as c:
                if ledger.one(c, "SELECT value FROM meta WHERE key=?", (key,)):
                    continue
                c.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, ledger.now()))
            chg = (px / base - 1) * 100
            try:
                notify.send(f"{q.get('name') or code} 异动", f"5 分钟内{'上涨' if chg > 0 else '下跌'} {abs(chg):.1f}%，现价 {px:.2f}",
                            level="warn", kind="move", code=code)
                out["alerts"] += 1
            except Exception:  # noqa: BLE001
                log.exception("异动推送失败")
    return out


def kill_switch() -> dict:
    """一键停止：关闭实盘总开关，撤掉所有实盘账户未成交的委托（能撤的都撤）"""
    s = settings_mod.load()
    data = s.model_dump()
    data["live"]["enabled"] = False
    settings_mod.save(settings_mod.validate(data))
    cancelled, failed = 0, []
    for acc in ledger.list_accounts():
        if acc["kind"] != "live":
            continue
        broker = get_broker(acc, {"live": data["live"]})
        with ledger.connect() as c:
            for o in ledger.rows(c, "SELECT * FROM orders WHERE account_id=? AND status IN ('submitted','partial','queued','pending_manual')", (acc["id"],)):
                try:
                    engine.cancel(c, o["id"]) if o["status"] in ("queued", "pending_manual") else broker.cancel(c, o["id"])
                    cancelled += 1
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{o['code']}：{e}")
            ledger.audit(c, acc["id"], "kill_switch", {"cancelled": cancelled, "failed": failed})
    return {"live_enabled": False, "cancelled": cancelled, "failed": failed}
