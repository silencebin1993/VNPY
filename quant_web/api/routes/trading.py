"""
交易接口：账户、下单（预览 → 确认）、撤单、手动成交确认、交割单导入、交易计划、明日计划、复盘、提醒、推送测试、一键停止。
"""
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query

from ..common import mod, ok


router = APIRouter()


def _svc() -> Any:
    return mod("trading.service")


def _ledger() -> Any:
    return mod("trading.ledger")


@router.get("/api/trading/brokers")
def trading_brokers() -> Any:
    cfg = _svc().settings_dict()
    live = {k: v for k, v in cfg["live"].items() if k != "vnpy_setting"}      # 网关登录信息不回传
    return ok({"brokers": mod("trading.brokers").listing(cfg), "trail_rules": mod("trading.plans").TRAIL_RULES,
               "live": live, "monitor": mod("trading.monitor").MONITOR.status()})


@router.get("/api/trading/accounts")
def trading_accounts() -> Any:
    lg = _ledger()
    eng = mod("trading.engine")
    out: list[dict] = []
    for a in lg.list_accounts():
        with lg.connect() as c:
            snap = eng.snapshot(c, a["id"])
        out.append({**a, "total": snap["total"], "return": snap["return"], "positions": len(snap["positions"]),
                    "market_value": snap["market_value"], "cash": snap["cash"]})
    return ok(out)


@router.post("/api/trading/accounts")
def trading_create_account(
    name: Annotated[str, Body(embed=True)],
    kind: Annotated[str, Body(embed=True)] = "paper",
    broker: Annotated[str, Body(embed=True)] = "paper",
    initial_cash: Annotated[float, Body(embed=True)] = 100_000,
) -> Any:
    return ok(_ledger().create_account(name, kind, broker, initial_cash))


@router.post("/api/trading/accounts/{account_id}/reset")
def trading_reset(account_id: str) -> Any:
    return ok(_ledger().reset_paper(account_id))


@router.delete("/api/trading/accounts/{account_id}")
def trading_archive(account_id: str) -> Any:
    _ledger().archive_account(account_id)
    return ok({"archived": account_id})


@router.get("/api/trading/accounts/{account_id}")
def trading_account(account_id: str, refresh: bool = True) -> Any:
    """账户详情：持仓（交易时段用实时价刷新市值）、未完成委托、最近成交、交易计划、资产曲线"""
    lg = _ledger()
    eng = mod("trading.engine")
    lg.get_account(account_id)
    with lg.connect() as c:
        codes = [r["code"] for r in lg.rows(c, "SELECT code FROM positions WHERE account_id=?", (account_id,))]
    quotes: dict = {}
    if refresh and codes:
        try:
            quotes = _svc().quote_map(codes)
        except Exception:  # noqa: BLE001  行情取不到时按最近价格显示
            quotes = {}
    with lg.connect() as c:
        for code, q in quotes.items():
            if q.get("price"):
                c.execute("UPDATE positions SET last_price=? WHERE account_id=? AND code=?", (q["price"], account_id, code))
        snap = eng.snapshot(c, account_id)
        orders = lg.rows(c, "SELECT * FROM orders WHERE account_id=? ORDER BY created DESC LIMIT 100", (account_id,))
        fills = lg.rows(c, "SELECT * FROM fills WHERE account_id=? ORDER BY id DESC LIMIT 100", (account_id,))
        plans = lg.rows(c, "SELECT * FROM plans WHERE account_id=? AND status IN ('active','pending') ORDER BY created DESC", (account_id,))
        equity = lg.rows(c, "SELECT date, total, cash, market_value FROM equity WHERE account_id=? ORDER BY date", (account_id,))
    for p in snap["positions"]:
        q = quotes.get(p["code"]) or {}
        p["pct_today"] = q.get("pct")
    return ok({**snap, "orders": orders, "fills": fills, "plans": plans, "equity": equity})


@router.post("/api/trading/preview")
def trading_preview(
    account_id: Annotated[str, Body(embed=True)],
    order: Annotated[dict, Body(embed=True)],
    plan: Annotated[dict | None, Body(embed=True)] = None,
) -> Any:
    return ok(_svc().preview(account_id, order, plan))


@router.post("/api/trading/orders")
def trading_submit(
    account_id: Annotated[str, Body(embed=True)],
    order: Annotated[dict, Body(embed=True)],
    plan: Annotated[dict | None, Body(embed=True)] = None,
    acknowledge: Annotated[bool, Body(embed=True)] = False,
) -> Any:
    return ok(_svc().submit(account_id, order, plan, acknowledge))


@router.post("/api/trading/orders/{order_id}/cancel")
def trading_cancel(order_id: str, account_id: Annotated[str, Body(embed=True)]) -> Any:
    return ok(_svc().cancel(account_id, order_id))


@router.post("/api/trading/orders/{order_id}/fill")
def trading_fill(
    order_id: str,
    account_id: Annotated[str, Body(embed=True)],
    qty: Annotated[int, Body(embed=True)],
    price: Annotated[float, Body(embed=True)],
) -> Any:
    return ok(_svc().confirm_fill(account_id, order_id, qty, price))


@router.post("/api/trading/import")
def trading_import(account_id: Annotated[str, Body(embed=True)], text: Annotated[str, Body(embed=True)]) -> Any:
    if len(text) > 5_000_000:
        raise HTTPException(400, "文件太大了")
    return ok(_svc().import_statement(account_id, text))


@router.put("/api/trading/plans/{plan_id}")
def trading_update_plan(
    plan_id: str,
    account_id: Annotated[str, Body(embed=True)],
    stop: Annotated[float | None, Body(embed=True)] = None,
    target: Annotated[float | None, Body(embed=True)] = None,
    trail: Annotated[str | None, Body(embed=True)] = None,
    allow_lower: Annotated[bool, Body(embed=True)] = False,
    note: Annotated[str | None, Body(embed=True)] = None,
) -> Any:
    return ok(_svc().update_plan(account_id, plan_id, stop, target, trail, allow_lower, note))


@router.get("/api/trading/nightly")
def trading_nightly(rebuild: bool = False) -> Any:
    nightly = mod("trading.nightly")
    res = nightly.build() if rebuild else (nightly.latest() or nightly.build())
    return ok(res)


@router.get("/api/trading/review/{account_id}")
def trading_review(account_id: str) -> Any:
    rv = mod("trading.review")
    return ok({"stats": rv.stats(account_id), "trades": rv.trades(account_id)})


@router.get("/api/alerts")
def alerts_list(unread: bool = False, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> Any:
    lg = _ledger()
    with lg.connect() as c:
        rows = lg.rows(c, "SELECT * FROM alerts" + (" WHERE read=0" if unread else "") + " ORDER BY id DESC LIMIT ?", (limit,))
        n_unread = lg.one(c, "SELECT COUNT(*) AS n FROM alerts WHERE read=0")["n"]
    return ok({"alerts": rows, "unread": n_unread})


@router.post("/api/alerts/read")
def alerts_read(ids: Annotated[list[int] | None, Body(embed=True)] = None) -> Any:
    lg = _ledger()
    with lg.connect() as c:
        if ids:
            c.executemany("UPDATE alerts SET read=1 WHERE id=?", [(int(i),) for i in ids])
        else:
            c.execute("UPDATE alerts SET read=1 WHERE read=0")
    return ok({"ok": True})


@router.post("/api/notify/test")
def notify_test(channel: Annotated[str, Body(embed=True)]) -> Any:
    return ok(mod("notify").test_channel(channel))


@router.post("/api/trading/kill")
def trading_kill() -> Any:
    """一键停止：关闭实盘总开关并撤掉实盘未成交的委托"""
    return ok(_svc().kill_switch())


@router.get("/api/trading/monitor")
def trading_monitor() -> Any:
    return ok(mod("trading.monitor").MONITOR.status())
