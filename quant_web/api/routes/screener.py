"""
选股器接口：方案列表、运行选股、保存/删除我的方案、方案历史回测、最近一次自动选股结果。
"""
import threading
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from ..common import mod, ok


router = APIRouter()
_RUN_CACHE: dict = {}                 # 同一天的日线特征：连续调整条件时复用（换一天自动重算）
_RUN_LOCK = threading.Lock()
DEFAULT_BT: dict = {"hold": 10, "rebalance": 10, "use_chips": True}


def _profile_risk() -> tuple[dict, dict]:
    s = mod("settings").load()
    return s.profile.model_dump(), s.risk.model_dump()


def _bt_params(scheme: dict, profile: dict, hold: int | None = None, rebalance: int | None = None) -> dict:
    h: int = int(hold or DEFAULT_BT["hold"])
    return {"hold": h, "rebalance": int(rebalance or h), "use_chips": True,
            "boards": scheme["universe"]["boards"] or list(profile.get("boards") or ["main"])}


@router.get("/api/screener/schemes")
def screener_schemes() -> Any:
    S = mod("screener.schemes")
    store = mod("screener.store")
    lib = mod("formula.library")
    fstore = mod("formula.store")
    profile, _ = _profile_risk()
    items: list[dict] = []
    for sc in store.all_schemes():
        try:
            v = S.validate_scheme(sc)
        except ValueError as e:
            items.append({**sc, "error": str(e)})
            continue
        bt = store.load_backtest(store.backtest_key(v, _bt_params(v, profile)))
        latest = store.latest_result(sc["id"])
        items.append({**sc, "backtest": {"verdict": bt["verdict"], "saved_at": bt.get("saved_at"), "key": bt.get("key")} if bt else None,
                      "latest_date": latest.get("date") if latest else None})
    return ok({
        "schemes": items, "fields": {k: {"label": v[0], "unit": v[1], "scale": S.DISPLAY_SCALE.get(k, 1)} for k, v in S.FIELDS.items()},
        "ops": S.OPS, "scoring": {k: {"name": v["name"], "desc": v["desc"], "weights": v["weights"]} for k, v in S.SCORING.items()},
        "terms": {k: {"label": v[0], "direction": v[1]} for k, v in S.TERMS.items()}, "stages": S.STAGES,
        "formulas": [{"id": f["id"], "name": f["name"], "group": f["group"], "kind": f["kind"]} for f in lib.LIBRARY]
                    + [{"id": f["id"], "name": f["name"], "group": "我的公式", "kind": "select"} for f in fstore.list_mine()],
        "profile_boards": profile.get("boards"),
    })


@router.post("/api/screener/run")
def screener_run(
    scheme_id: Annotated[str | None, Body(embed=True)] = None,
    scheme: Annotated[dict | None, Body(embed=True)] = None,
) -> Any:
    store = mod("screener.store")
    if scheme is None:
        if not scheme_id:
            raise HTTPException(400, "请选择一个选股方案")
        try:
            scheme = store.get(scheme_id)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
    profile, risk = _profile_risk()
    with _RUN_LOCK:
        res: dict = mod("screener.engine").run(scheme, profile=profile, risk=risk, cache=_RUN_CACHE)
    if scheme_id:
        store.save_result(scheme_id, res)
    return ok(res)


@router.post("/api/screener/schemes")
def screener_save(scheme: Annotated[dict, Body(embed=True)]) -> Any:
    return ok(mod("screener.store").save_mine(scheme))


@router.delete("/api/screener/schemes/{scheme_id}")
def screener_delete(scheme_id: str) -> Any:
    if not mod("screener.store").delete_mine(scheme_id):
        raise HTTPException(404, "没有找到这个方案（内置方案不能删除）")
    return ok({"deleted": scheme_id})


@router.post("/api/screener/backtest")
def screener_backtest(
    scheme_id: Annotated[str | None, Body(embed=True)] = None,
    scheme: Annotated[dict | None, Body(embed=True)] = None,
    hold: Annotated[int | None, Body(embed=True)] = None,
    force: Annotated[bool, Body(embed=True)] = False,
) -> Any:
    """已有相同方案 + 参数的回测结果时直接返回；否则启动后台任务 → {key, job_id}"""
    store = mod("screener.store")
    S = mod("screener.schemes")
    if scheme is None:
        if not scheme_id:
            raise HTTPException(400, "请选择一个选股方案")
        scheme = store.get(scheme_id)
    v: dict = S.validate_scheme(scheme)
    profile, _ = _profile_risk()
    if hold is not None and not 2 <= int(hold) <= 60:
        raise HTTPException(400, "持有天数要在 2 到 60 之间")
    params: dict = _bt_params(v, profile, hold)
    key: str = store.backtest_key(v, params)
    if not force:
        cached = store.load_backtest(key)
        if cached:
            return ok({"key": key, "result": cached})
    job_id: str = mod("tasks").submit("screener_backtest", {"scheme": v, **params})
    return ok({"key": key, "job_id": job_id})


@router.get("/api/screener/backtest/{key}")
def screener_backtest_result(key: str) -> Any:
    res = mod("screener.store").load_backtest(key)
    if res is None:
        raise HTTPException(404, "还没有这个回测结果")
    return ok(res)


@router.get("/api/screener/latest/{scheme_id}")
def screener_latest(scheme_id: str) -> Any:
    res = mod("screener.store").latest_result(scheme_id)
    if res is None:
        raise HTTPException(404, "这个方案还没有运行过")
    return ok(res)
