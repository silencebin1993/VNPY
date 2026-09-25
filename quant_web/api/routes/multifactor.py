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
    return ok({
        "today": today,
        "report_at": rep.get("generated_at") if rep else None,
        "capital": svc.profile_capital(),
        "config": {"top_n": svc.TOP_N, "keep_rank": svc.KEEP_RANK, "industry_cap": svc.INDUSTRY_CAP,
                   "min_amount": svc.MIN_AMOUNT, "max_participation": svc.MAX_PARTICIPATION},
    })


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
    items, used = [], 0.0
    for code in target:
        r = rows.get(code) or {"code": code, "name": "", "close": None, "industry": ""}
        px = r.get("close")
        shares = int(per / px / 100) * 100 if px and px > 0 else 0
        amt = shares * px if px else 0.0
        used += amt
        items.append({"code": code, "name": r.get("name"), "industry": r.get("industry"), "close": px, "shares": shares,
                      "amount": amt, "rank": r.get("rank"), "action": "继续持有" if code in prev else "买入",
                      "too_small": shares == 0,
                      "participation": (per / r["amount20"]) if r.get("amount20") else None})
    sells = [{"code": c, "name": (rows.get(c) or {}).get("name", ""), "action": "卖出"} for c in prev if c not in set(target)]
    too_small = sum(1 for x in items if x["too_small"])
    notes = []
    if too_small:
        notes.append(f"资金按 {len(target)} 只平分后，有 {too_small} 只连一手（100 股）都买不起；资金较少时可以只买排名靠前的一部分，"
                     "但持股越少，结果越接近“碰运气”，和回测的差别越大。")
    if cap > 5e7:
        notes.append("资金超过 5000 万：回测显示这个方法的超额收益随资金增大明显变小（冲击成本和流动性），1 亿以上基本没有优势，见回测报告的容量表。")
    return ok({"date": today["date"], "next_trade_day": today.get("next_trade_day"), "capital": cap, "per_stock": per,
               "used": used, "cash_left": cap - used, "items": items, "sells": sells, "notes": notes,
               "rebalance_day": today.get("rebalance_day"),
               "lots_ok": not math.isnan(used)})
