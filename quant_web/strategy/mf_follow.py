"""
"量化选股 每周调仓"策略的模拟跟踪：和量化选股页的回测同一套规则。
- 组合 = 最近一个调仓日（每周最后一个交易日）收盘后记下的那一期（workspace/multifactor/track.json，记下后不再改）；
- 下一个交易日开盘：先卖掉不在组合里的，再按"总资产 ÷ 组合只数"等权买新进组合的（卖出回笼的钱当天就能用来买）；
- 开盘涨停买不进 / 跌停卖不出的，之后每天收盘后自动补单；不设单只止损、不在周中换股。
每天收盘后由 follow.run_daily 调用（每日流水线：量化选股打分 → 交易日终 → 这里）。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ..trading import engine, ledger, rules
from ..trading.engine import OrderRequest

MIN_CAPITAL: float = 1_000_000          # 模拟账户起始资金下限：50 只等权，每只约 2 万，保证每只都能买到至少一手
SELL_HAIRCUT: float = 0.999             # 卖出回笼的钱扣掉印花税和佣金；开盘低开导致钱不够时，撮合时的现金核对会让最后几只当天不买、第二天补
FEE_RESERVE: float = 0.998              # 等权金额先留出 0.2% 给手续费，最后一只才不会因为差几块钱买不了
OPEN: tuple[str, ...] = ("submitted", "partial", "queued", "pending_manual")


def official_target() -> dict | None:
    """最近一期正式组合：{date, holdings, next_trade_day, ...}；还没有记录时返回 None"""
    from ..multifactor import service as mf
    recs: list[dict] = mf.load_track().get("records") or []
    return recs[-1] if recs else None


def latest_prices() -> tuple[str | None, dict[str, float], dict[str, str]]:
    """量化选股最近一次打分里的收盘价和名称（前 300 名；组合里的股票都在里面）"""
    from ..multifactor import service as mf
    t: dict = mf.load_today() or {}
    rows: list[dict] = t.get("rows") or []
    px = {r["code"]: float(r["close"]) for r in rows if r.get("close") and r["close"] == r["close"]}
    names = {r["code"]: r.get("name") or "" for r in rows}
    return t.get("date"), px, names


def mf_step(conn, account_id: str, day: date, next_day: date, price_of: Callable[[str], float | None],
            name_of: Callable[[str], str] | None = None, allow_buys: bool = True) -> dict:
    """在模拟账户里向最近一期组合靠拢：卖出组合外的持仓，等权买入组合内还没有的股票（都挂下一个交易日开盘）。
    allow_buys=False（价格太旧）时只卖不买"""
    rec: dict | None = official_target()
    out: dict = {"sells": 0, "buys": 0, "skipped": [], "target_date": rec["date"] if rec else None}
    if not rec or not rec.get("holdings") or rec["date"] > str(day):
        out["skipped"].append("还没有量化选股的组合记录（每周最后一个交易日收盘后才会记下一期）")
        return out
    target: list[str] = list(rec["holdings"])
    tset: set[str] = set(target)
    positions: list[dict] = ledger.rows(conn, "SELECT * FROM positions WHERE account_id=?", (account_id,))
    orders: list[dict] = ledger.rows(conn, "SELECT * FROM orders WHERE account_id=? AND status IN (" + ",".join("?" * len(OPEN)) + ")",
                                     (account_id, *OPEN))
    selling: set[str] = {o["code"] for o in orders if o["side"] == "sell"}
    buying: set[str] = {o["code"] for o in orders if o["side"] == "buy"}
    held: dict[str, dict] = {p["code"]: p for p in positions}

    proceeds: float = 0.0
    for code, pos in held.items():
        if code in tset or code in selling or int(pos["available"]) <= 0:
            continue
        engine.place(conn, account_id, OrderRequest(code, "sell", int(pos["available"]), "market", name=pos.get("name"),
                                                    reason=f"量化选股调仓：{rec['date']} 的组合里没有它", source="strategy"),
                     trade_date=next_day)
        proceeds += int(pos["available"]) * float(price_of(code) or pos.get("last_price") or pos["cost"])
        out["sells"] += 1

    snap: dict = engine.snapshot(conn, account_id)
    per: float = float(snap["total"]) * FEE_RESERVE / max(len(target), 1)
    budget: float = engine.available_cash(conn, account_id) + proceeds * SELL_HAIRCUT
    # 真正的资金约束是上面的 budget（按收盘价算）和撮合时的现金核对；市价买单按参考价上浮 10% 冻结，
    # 所以给下单检查的"可以先用的钱"放宽到 budget 的 1.1 倍，免得冻结口径把本来买得起的单挡掉
    credit: float = 1.1 * max(budget, 0.0)
    if not allow_buys:
        out["skipped"].append("量化选股的最新打分太旧（最近一天没打分成功）：今天只卖不买，等打分更新后再补买")
        return out
    for rank, code in enumerate(target, start=1):
        if code in held or code in buying:
            continue
        px: float | None = price_of(code)
        if not px or px <= 0:
            out["skipped"].append(f"{code}：没有最新价格")
            continue
        lot: int = rules.lot_of(code)
        qty: int = int(per / px / lot) * lot
        if qty <= 0:
            out["skipped"].append(f"{code}：每只约 {per:,.0f} 元，一手都买不起")
            continue
        cost: float = qty * px + rules.fees("buy", qty * px, day)["total"]
        if cost > budget + 1e-6:
            out["skipped"].append(f"{code}：钱不够（等卖出成交后再补）")
            continue
        try:
            engine.place(conn, account_id, OrderRequest(code, "buy", qty, "market", name=(name_of(code) if name_of else None),
                                                        reason=f"量化选股调仓：{rec['date']} 组合第 {rank} 名", source="strategy"),
                         ref_price=px, trade_date=next_day, credit=credit)
        except ValueError as e:
            out["skipped"].append(f"{code}：{e}")
            continue
        budget -= cost
        out["buys"] += 1
    return out
