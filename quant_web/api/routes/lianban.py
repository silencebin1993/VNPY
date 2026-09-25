"""
连板接口：天梯（某天按连板高度分层 + 次日复盘）、各高度晋级率、情绪周期。只读，不下单。
"""
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..common import CACHE, mod, ok, panel_sig


router = APIRouter()


@router.get("/api/lianban/ladder")
def lianban_ladder(day: str | None = Query(None, alias="date"), main_only: bool = False) -> Any:
    L = mod("market.lianban")
    if day is not None:
        try:
            from datetime import date
            date.fromisoformat(day)
        except ValueError as e:
            raise HTTPException(400, f"日期格式不对：{day}（例：2026-09-24）") from e
    data = CACHE.get(("lianban_ladder", day, main_only), lambda: L.ladder(day, main_only=main_only), ttl=120,
                     sig=panel_sig())
    sent = mod("market.sentiment")
    s = data.get("summary") or {}
    if s and not s.get("label"):
        s["label"] = sent.temperature_label(s.get("temperature"))
    return ok(data)


@router.get("/api/lianban/stats")
def lianban_stats(days: int = 60, main_only: bool = False) -> Any:
    if not 5 <= days <= 750:
        raise HTTPException(400, "统计天数要在 5 到 750 之间")
    L = mod("market.lianban")
    return ok({
        "promotion": CACHE.get(("lianban_promo", days, main_only), lambda: L.promotion(days, main_only=main_only),
                               ttl=600, sig=panel_sig()),
        "cycle": CACHE.get(("lianban_cycle", 250), lambda: L.cycle(250), ttl=600, sig=panel_sig()),
        "dates": L.trading_dates(250),
    })
