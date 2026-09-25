"""
数据源接口：查看每种数据的数据源顺序和健康状况、调整顺序、测试连接、查看扩展数据存档。
"""
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query

from ..common import mod, ok


router = APIRouter()


def _providers_view() -> dict:
    base = mod("providers.base")
    reg = base.registry()
    providers: list[dict] = []
    for p in reg.providers():
        available, reason = p.available()
        providers.append({
            "name": p.name, "label": p.label, "description": p.description, "optional": p.optional,
            "available": available, "reason": reason,
            "capabilities": [c for c in p.capabilities if p.supports(c)],
        })
    return {"capabilities": reg.status(), "providers": providers, "archives": mod("providers.store").info()}


@router.get("/api/providers")
def providers_status() -> Any:
    return ok(_providers_view())


@router.put("/api/providers/chain")
def providers_set_chain(
    capability: Annotated[str, Body(embed=True)],
    chain: Annotated[list[str], Body(embed=True)],
) -> Any:
    """保存某种数据的数据源顺序；chain 为空表示恢复默认顺序"""
    base = mod("providers.base")
    settings_mod = mod("settings")
    if capability not in base.CAPABILITIES:
        raise HTTPException(400, f"不认识的数据类型「{capability}」")
    reg = base.registry()
    capable: list[str] = reg.capable(capability)
    unknown: list[str] = [n for n in chain if n not in capable]
    if unknown:
        raise HTTPException(400, f"这些数据源不提供「{base.CAPABILITIES[capability][0]}」：{'、'.join(unknown)}")
    s = settings_mod.load()
    chains: dict[str, list[str]] = dict(s.providers.chains)
    if chain:
        chains[capability] = list(dict.fromkeys(chain))
    else:
        chains.pop(capability, None)
    data: dict = s.model_dump()
    data["providers"]["chains"] = chains        # 整体替换（merge_update 的深度合并删不掉某一项）
    settings_mod.save(settings_mod.validate(data))
    return ok(_providers_view())


@router.post("/api/providers/probe")
def providers_probe(
    capability: Annotated[str, Body(embed=True)],
    provider: Annotated[str, Body(embed=True)],
) -> Any:
    """用一组小参数试取一次数据（不保存）"""
    base = mod("providers.base")
    if capability not in base.CAPABILITIES:
        raise HTTPException(400, f"不认识的数据类型「{capability}」")
    return ok(base.registry().probe(capability, provider))


@router.get("/api/providers/archive/{name}")
def providers_archive(
    name: str,
    code: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> Any:
    """预览某个扩展数据存档（最新的 limit 行；可按股票代码筛选）"""
    store = mod("providers.store")
    if name not in store.ARCHIVES:
        raise HTTPException(404, f"没有「{name}」这个扩展数据存档")
    df = store.load(name)
    if df.height and code and "code" in df.columns:
        df = df.filter(df["code"] == code)
    return ok({"name": name, "label": store.ARCHIVES[name]["label"], "rows": df.height, "data": df.tail(limit)})
