"""
明日计划（每天收盘后生成，晚上看一眼就够）：
- 持仓：每只股票明天怎么做（跌破止损 → 开盘卖；疑似出货 → 减仓/上移止损；超期 → 考虑离场；到目标 → 部分止盈；其余继续拿）；
- 条件单清单：需要在券商 App 里设置的止损（和止盈）条件单价格——电脑关着也能止损；
- 候选买入：每天自动运行的选股方案今天的前几名（带建议止损和股数），需要你确认才会下单；
- 大盘环境与总仓位提醒。结果存 workspace/trading/nightly/{日期}.json。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from pathlib import Path

from .. import config
from . import engine, ledger


def _dir() -> Path:
    return config.WORKSPACE.joinpath("trading", "nightly")


def _stage_of(code: str) -> tuple[str | None, str | None]:
    try:
        from ..analysis import diagnose
        d = diagnose.full(code)
        return d["stage"]["key"], d["stage"]["label"]
    except Exception:  # noqa: BLE001
        return None, None


def position_actions(conn, account_id: str, day: date, with_stage: bool = True, mf_follow: bool = False) -> list[dict]:
    """mf_follow：跟随量化选股组合的模拟账户——按组合调仓，不看主力阶段、不要求止损（和回测一致），只标出要调出的"""
    out: list[dict] = []
    target: set[str] = set()
    if mf_follow:
        from ..strategy import mf_follow as mff
        rec = mff.official_target()
        target = set((rec or {}).get("holdings") or [])
    for p in ledger.rows(conn, "SELECT * FROM positions WHERE account_id=? ORDER BY opened", (account_id,)):
        plan = ledger.one(conn, "SELECT * FROM plans WHERE id=?", (p["plan_id"],)) if p.get("plan_id") else None
        close = p.get("last_close") or p.get("last_price") or p["cost"]
        pnl_pct = close / p["cost"] - 1 if p["cost"] else None
        held = (day - date.fromisoformat(p["opened"])).days if p.get("opened") else None
        stage_key, stage_label = _stage_of(p["code"]) if with_stage and not mf_follow else (None, None)
        action, tone = "继续持有", "neutral"
        reasons: list[str] = []
        if mf_follow:
            if target and p["code"] not in target:
                action, tone = "调仓卖出", "watch"
                reasons.append("已不在最新一期量化选股组合里：模拟账户会在下一个交易日开盘自动卖出")
            else:
                reasons.append("量化选股组合持仓：调仓日按组合换股，平时不动，不设单只止损")
        elif plan and close <= plan["stop"]:
            action, tone = "明天开盘卖出", "bad"
            reasons.append(f"收盘 {close:.2f} 已跌破止损价 {plan['stop']:.2f}")
        elif stage_key == "distribution":
            action, tone = "留意：疑似出货", "watch"
            reasons.append("主力阶段判断为疑似出货（历史上之后一周平均偏弱，但不是一定会跌）：按你的计划处理，止损价别往下挪")
        elif plan and plan.get("target") and close >= plan["target"]:
            action, tone = "部分止盈", "good"
            reasons.append(f"已到目标价 {plan['target']:.2f}")
        elif plan and plan.get("max_days") and held is not None and held > plan["max_days"] * 1.45:
            action, tone = "考虑离场", "watch"
            reasons.append(f"已经持有约 {held} 天，超过计划的 {plan['max_days']} 个交易日")
        if not plan and not mf_follow:
            reasons.append("没有交易计划：请补一个止损价")
        out.append({"code": p["code"], "name": p.get("name"), "qty": p["qty"], "available": p["available"], "cost": p["cost"],
                    "close": close, "pnl_pct": pnl_pct, "held_days": held, "stop": plan["stop"] if plan else None,
                    "target": plan.get("target") if plan else None, "trail": plan.get("trail") if plan else None,
                    "stage": stage_key, "stage_label": stage_label, "action": action, "tone": tone, "reasons": reasons})
    return out


def conditional_orders(actions: list[dict]) -> list[dict]:
    """需要在券商 App 设置的条件单"""
    out: list[dict] = []
    for a in actions:
        if a.get("stop"):
            out.append({"code": a["code"], "name": a["name"], "type": "止损卖出", "trigger": round(a["stop"], 2), "qty": a["qty"],
                        "text": f"{a['name'] or a['code']}（{a['code']}）：价格 ≤ {a['stop']:.2f} 时卖出 {a['qty']} 股"})
        if a.get("target"):
            half = (a["qty"] // 200) * 100 or a["qty"]
            out.append({"code": a["code"], "name": a["name"], "type": "止盈卖出（可选）", "trigger": round(a["target"], 2), "qty": half,
                        "text": f"{a['name'] or a['code']}（{a['code']}）：价格 ≥ {a['target']:.2f} 时卖出 {half} 股（先卖一半）"})
    return out


def candidates(limit: int = 5, day: date | None = None) -> list[dict]:
    """day 给了就只用那一天的结果：当天的选股没跑成功时，不拿以前的名单冒充今天的"""
    from .. import settings as settings_mod
    from ..screener import store as sstore

    ids: list[str] = list(settings_mod.load().assistant.screeners) or ["reversal_value"]
    out: list[dict] = []
    for sid in ids:
        res = sstore.latest_result(sid)
        if not res or (day is not None and res.get("date") != str(day)):
            continue
        for r in res.get("rows", [])[:limit]:
            if r.get("shares"):
                out.append({**r, "scheme_id": sid, "scheme_name": res.get("scheme", {}).get("name"), "result_date": res.get("date")})
    try:                                                    # 策略中心里开启了"实盘建议"的策略
        from ..strategy import follow
        out += follow.live_candidates(str(day) if day is not None else None)
    except Exception:  # noqa: BLE001
        pass
    return out


def _mf_follow_accounts() -> set[str]:
    """策略中心里"量化选股 每周调仓"的模拟账户"""
    try:
        from ..strategy import follow, store
        return {(x.get("follow") or {}).get("account_id") for x in store.list_items() if follow.is_mf(x)} - {None}
    except Exception:  # noqa: BLE001
        return set()


def mf_rebalance(day: date) -> dict | None:
    """今天是量化选股的调仓日（每周最后一个交易日）：明天开盘要卖哪些、买哪些（下单股数按资金在量化选股页算）"""
    try:
        from ..multifactor import service as mf
        t = mf.load_today()
    except Exception:  # noqa: BLE001
        return None
    if not t or t.get("date") != str(day) or not t.get("rebalance_day"):
        return None
    names = {r["code"]: r.get("name") or "" for r in t.get("rows") or []}
    target, prev = list(t.get("target") or []), list(t.get("prev_target") or [])
    buys = [c for c in target if c not in set(prev)]
    sells = [c for c in prev if c not in set(target)]
    return {"date": t["date"], "next_trade_day": t.get("next_trade_day"), "n": len(target), "keep": len(target) - len(buys),
            "buys": [{"code": c, "name": names.get(c, "")} for c in buys], "sells": [{"code": c, "name": names.get(c, "")} for c in sells]}


def build(day: date | None = None, with_stage: bool = True) -> dict:
    from ..analysis import market
    from ..market import history

    day = day or history.last_date() or ledger.today()
    reg = market.last_regime()
    accounts_out: list[dict] = []
    mf_accounts: set[str] = _mf_follow_accounts()
    for acc in ledger.list_accounts():
        with ledger.connect() as c:
            acts = position_actions(c, acc["id"], day, with_stage, mf_follow=acc["id"] in mf_accounts)
            snap = engine.snapshot(c, acc["id"])
        accounts_out.append({"id": acc["id"], "name": acc["name"], "kind": acc["kind"], "broker": acc["broker"],
                             "total": snap["total"], "cash": snap["cash"], "market_value": snap["market_value"],
                             "exposure": snap["market_value"] / snap["total"] if snap["total"] else 0,
                             "positions": acts, "conditional": conditional_orders(acts) if acc["kind"] == "live" else []})
    res: dict = {
        "date": str(day), "generated_at": datetime.now(config.CHINA_TZ).strftime("%Y-%m-%d %H:%M"),
        "regime": {k: reg.get(k) for k in ("label", "cap", "advice", "tone", "date")} if reg else None,
        "accounts": accounts_out, "candidates": candidates(day=day), "mf_rebalance": mf_rebalance(day),
        "note": "候选买入只是“符合方案、排名靠前”的股票，需要你逐只看诊断后在“交易”页确认才会下单；"
                "这些候选来自选股器方案，它们的历史回测都没有显著跑赢随机，仓位宁小勿大；"
                "样本外显著跑赢随机的是“量化选股”页面的每周组合。",
    }
    path = _dir().joinpath(f"{day}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return res


def latest() -> dict | None:
    d = _dir()
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
