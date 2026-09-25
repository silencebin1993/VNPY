"""
量价与主力分析接口：个股诊断（主力阶段 + 排雷 + 参考信息）、大盘环境、板块强弱、主力阶段历史验证。
"""
from datetime import date
from typing import Any

import polars as pl
from fastapi import APIRouter, HTTPException

from ..common import CACHE, check_code, china_now, describe_error, mod, ok, panel_sig


router = APIRouter()


def _rps() -> pl.DataFrame:
    def build() -> pl.DataFrame:
        last: date | None = mod("market.history").last_date()
        if last is None:
            return pl.DataFrame(schema={"code": pl.Utf8, "date": pl.Date, "rps120": pl.Float64})
        return mod("analysis.diagnose").rps_recent(last)
    return CACHE.get("rps_recent", build, ttl=3600, sig=panel_sig())


def _fundamentals() -> pl.DataFrame:
    def build() -> pl.DataFrame:
        try:
            return mod("market.fundamentals").load_fundamentals()
        except Exception:  # noqa: BLE001  没有财报数据
            return pl.DataFrame()
    return CACHE.get("fundamentals", build, ttl=1800)


def _news_titles(name: str | None, code: str) -> list[tuple[str, str]] | None:
    """本地新闻库里提到这只股票的标题（最近 90 天）；新闻库不可用时返回 None（不当作没风险）"""
    try:
        news = mod("market.news")
        store: pl.DataFrame = news._load_store()                         # noqa: SLF001  只读本地缓存，不联网
    except Exception:  # noqa: BLE001
        return None
    if store.is_empty() or "title" not in store.columns:
        return []
    keys: list[str] = [k for k in (name, code) if k]
    text = pl.col("title").fill_null("") + pl.col("content").fill_null("") if "content" in store.columns else pl.col("title")
    hits = store.filter(pl.any_horizontal([text.str.contains(k, literal=True) for k in keys])) if keys else store.head(0)
    tcol: str = "time" if "time" in hits.columns else hits.columns[0]
    return [(str(r["title"]), str(r.get(tcol) or "")[:10]) for r in hits.head(50).to_dicts()]


def build_diagnosis(code: str) -> dict:
    diag_mod = mod("analysis.diagnose")
    flow: list[dict] | None = None
    try:
        res = mod("providers.base").registry().fetch("fund_flow", code=code)
        flow = res.data.to_dicts() if res.data.height else None
    except Exception:  # noqa: BLE001  资金流只是参考信息
        flow = None
    holders: pl.DataFrame | None = None
    try:
        hc: pl.DataFrame = mod("providers.store").load("holder_count")
        holders = hc.filter(pl.col("code") == code) if hc.height else None
    except Exception:  # noqa: BLE001
        holders = None
    d: dict = diag_mod.diagnose(code, rps=_rps(), flow=flow, holders=holders)
    info: dict = mod("api.server").stock_info(code)
    uni: pl.DataFrame = mod("market.universe").load_universe()
    row = uni.filter(pl.col("code") == code)
    list_date: date | None = row["list_date"][0] if row.height and "list_date" in row.columns else None
    float_cap = total_cap = None
    try:
        q = mod("market.realtime").quotes([code])
        q0 = q[0] if isinstance(q, list) and q else (q.row(0, named=True) if isinstance(q, pl.DataFrame) and q.height else {})
        float_cap, total_cap = q0.get("float_cap"), q0.get("total_cap")
    except Exception:  # noqa: BLE001  实时行情取不到时市值一项显示"没有数据"
        pass
    d["risk"] = mod("analysis.riskscan").scan_stock(
        code, info.get("name"), china_now().date(), d.get("last_bar"), list_date, _fundamentals(),
        d["stage"]["key"], float_cap=float_cap, total_cap=total_cap, news_titles=_news_titles(info.get("name"), code))
    d["name"] = info.get("name")
    return d


@router.get("/api/stock/{code}/diagnosis")
def stock_diagnosis(code: str) -> Any:
    """个股诊断：主力阶段（逐条证据、时间线、历史验证结论）+ 量价摘要 + 参考信息 + 排雷"""
    code = check_code(code)
    try:
        return ok(CACHE.get(("diagnosis", code), lambda: build_diagnosis(code), ttl=300, sig=panel_sig()))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None


@router.get("/api/market/regime")
def market_regime(refresh: bool = False) -> Any:
    """大盘环境（强势/震荡/弱势 + 建议总仓位上限 + 分项 + 历史统计）"""
    market = mod("analysis.market")
    try:
        return ok(CACHE.get("market_regime", lambda: market.current_regime(force=refresh), ttl=1800, sig=panel_sig()))
    except ValueError as e:
        last = market.last_regime()
        if last:
            return ok({**last, "stale": True, "warning": describe_error(e)[1]})
        raise


@router.get("/api/market/sectors")
def market_sectors() -> Any:
    return ok(CACHE.get("market_sectors", lambda: mod("analysis.market").current_sectors(), ttl=1800, sig=panel_sig()))


@router.get("/api/analysis/stage_stats")
def analysis_stage_stats() -> Any:
    res = mod("analysis.stage_stats").load()
    if res is None:
        raise HTTPException(404, "还没有做过主力阶段的历史验证（在个股诊断里点“开始验证”，大约需要几分钟）")
    return ok(res)
