"""
量价与主力分析接口：个股诊断（主力阶段 + 排雷 + 参考信息）、大盘环境、板块强弱、主力阶段历史验证。
"""
from typing import Any

from fastapi import APIRouter, HTTPException

from ..common import CACHE, check_code, describe_error, mod, ok, panel_sig


router = APIRouter()


@router.get("/api/stock/{code}/diagnosis")
def stock_diagnosis(code: str) -> Any:
    """个股诊断：主力阶段（逐条证据、时间线、历史验证结论）+ 量价摘要 + 参考信息 + 排雷"""
    code = check_code(code)
    try:
        return ok(mod("analysis.diagnose").full(code))
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
