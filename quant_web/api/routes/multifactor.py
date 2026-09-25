"""
量化选股（多因子 + LightGBM）接口：今日名单与调仓组合、完整回测报告、前向跟踪成绩、按资金算的下单清单。
打分和回测都是后台任务（mf_daily / mf_report），这里只读结果文件或启动任务。
"""
import math
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..common import CACHE, mod, ok, panel_sig


router = APIRouter()


def _svc():
    return mod("multifactor.service")


@router.get("/api/mf/today")
def mf_today() -> Any:
    svc = _svc()
    today = svc.load_today()
    rep = svc.load_report()
    try:                                                   # 下单前自检（数据是否最新 / 完整、名单是否按最新数据打分）
        chk = CACHE.get("mf_health", lambda: svc.health(today), ttl=60,
                        sig=(panel_sig(), today and today.get("generated_at")))
    except Exception as e:  # noqa: BLE001  自检出错不影响看名单，但要让用户知道没检查
        chk = {"ok": False, "items": [{"key": "error", "level": "warn", "text": f"自检没能完成：{e}"}]}
    return ok({
        "today": today,
        "health": chk,
        "report_at": rep.get("generated_at") if rep else None,
        "capital": svc.profile_capital(),
        "config": {"top_n": svc.TOP_N, "keep_rank": svc.KEEP_RANK, "industry_cap": svc.INDUSTRY_CAP,
                   "min_amount": svc.MIN_AMOUNT, "max_participation": svc.MAX_PARTICIPATION},
    })


@router.get("/api/mf/membership")
def mf_membership() -> Any:
    """诊断页用：最新一期量化选股组合里有哪些股票（和排名）"""
    today = _svc().load_today()
    if not today:
        return ok({"date": None, "target": [], "ranks": {}})
    ranks = {r["code"]: r["rank"] for r in today.get("rows") or []}
    return ok({"date": today["date"], "rebalance_day": today.get("rebalance_day"), "target": today.get("target") or [],
               "ranks": {c: ranks.get(c) for c in today.get("target") or []}})


@router.get("/api/mf/report")
def mf_report() -> Any:
    rep = _svc().load_report()
    return ok({"report": rep})


@router.post("/api/mf/run")
def mf_run() -> Any:
    return ok({"job_id": mod("tasks").submit("mf_daily", {})})


@router.post("/api/mf/rebuild")
def mf_rebuild() -> Any:
    return ok({"job_id": mod("tasks").submit("mf_report", {})})


@router.get("/api/mf/track")
def mf_track() -> Any:
    svc = _svc()
    return ok(CACHE.get("mf_track", svc.track_performance, ttl=600, sig=(panel_sig(), svc.TRACK_FILE.exists() and
                                                                        svc.TRACK_FILE.stat().st_mtime)))


@router.get("/api/mf/plan")
def mf_plan(capital: float | None = Query(None, ge=10_000, le=1e10)) -> Any:
    """按今天的调仓组合和资金算每只买多少股（等权，100 股一手向下取整）；和上一期组合比，列出要卖 / 要买 / 继续持有"""
    svc = _svc()
    today = svc.load_today()
    if not today:
        raise HTTPException(404, "还没有量化选股的结果，先点“重新打分”")
    cap = float(capital or svc.profile_capital())
    rows = {r["code"]: r for r in today["rows"]}
    target = today.get("target") or []
    prev = set(today.get("prev_target") or [])
    per = cap / max(len(target), 1)
    lim = float(svc.MAX_PARTICIPATION)
    items, used = [], 0.0
    for code in target:
        r = rows.get(code) or {"code": code, "name": "", "close": None, "industry": ""}
        px = r.get("close")
        shares = int(per / px / 100) * 100 if px and px > 0 else 0
        amt = shares * px if px else 0.0
        used += amt
        part = (per / r["amount20"]) if r.get("amount20") else None
        items.append({"code": code, "name": r.get("name"), "industry": r.get("industry"), "close": px, "shares": shares,
                      "amount": amt, "rank": r.get("rank"), "action": "继续持有" if code in prev else "买入",
                      "too_small": shares == 0, "participation": part, "too_big": bool(part and part > lim),
                      "limit_up": bool(r.get("limit_up"))})
    # 候补：排名紧跟在组合后面、同样守行业上限的股票（新买入的开盘涨停 / 停牌买不进时，按顺序换这些）
    ind_n: dict[str, int] = {}
    for code in target:
        k = (rows.get(code) or {}).get("industry") or "未知"
        ind_n[k] = ind_n.get(k, 0) + 1
    backups = []
    for r in sorted(today["rows"], key=lambda x: x.get("rank") or 10 ** 9):
        if len(backups) >= 8:
            break
        k = r.get("industry") or "未知"
        if r["code"] in set(target) or ind_n.get(k, 0) >= svc.INDUSTRY_CAP:
            continue
        px = r.get("close")
        backups.append({"code": r["code"], "name": r.get("name"), "rank": r.get("rank"), "close": px, "limit_up": bool(r.get("limit_up")),
                        "shares": int(per / px / 100) * 100 if px and px > 0 else 0})
    # 要卖出的：最新一天停牌的（没有成交）先别急，复牌后再卖——停牌时也卖不出去
    sell_codes = [c for c in prev if c not in set(target)]
    halted: set[str] = set()
    if sell_codes:
        try:
            from datetime import date as _date
            day = _date.fromisoformat(today["date"])
            got = mod("market.history").load_panel(start=day, end=day, codes=sell_codes, columns=["close"])
            halted = set(sell_codes) - set(got["code"].to_list())
        except Exception:  # noqa: BLE001  查不到就不标
            halted = set()
    sells = [{"code": c, "name": (rows.get(c) or {}).get("name", ""), "action": "卖出", "halted": c in halted} for c in sell_codes]
    too_small = sum(1 for x in items if x["too_small"])
    too_big = [x for x in items if x["too_big"]]
    notes = []
    built_cap = float(today.get("capital") or 0)
    if built_cap and cap > built_cap * 1.5:
        notes.append(f"名单是按 {built_cap / 1e4:.0f} 万元的资金筛的流动性，你现在按 {cap / 1e4:.0f} 万元下单：请先在“新手指南 / 我的情况”里把资金改成实际金额，"
                     "再点“重新打分”，名单会按你的资金重新筛掉成交太少的股票。")
    if too_big:
        notes.append(f"有 {len(too_big)} 只每只要买的金额超过它每天成交额的 {lim:.0%}（表里标了“量大”）：一次买这么多会把价格买上去、成本变高，"
                     "建议分 2~3 天买，或者减少资金。")
    if too_small:
        notes.append(f"资金按 {len(target)} 只平分后，有 {too_small} 只连一手（100 股）都买不起；资金较少时可以只买排名靠前的一部分，"
                     "但持股越少，结果越接近“碰运气”，和回测的差别越大。")
    if cap > 5e7:
        notes.append("资金超过 5000 万：回测显示这个方法的超额收益随资金增大明显变小（冲击成本和流动性），1 亿以上基本没有优势，见回测报告的容量表。")
    return ok({"date": today["date"], "next_trade_day": today.get("next_trade_day"), "capital": cap, "per_stock": per,
               "used": used, "cash_left": cap - used, "items": items, "sells": sells, "backups": backups, "notes": notes,
               "rebalance_day": today.get("rebalance_day"), "built_capital": built_cap or None,
               "lots_ok": not math.isnan(used)})
