"""
策略中心接口：模板、我的策略（新建 / 修改 / 删除）、诚实回测（后台任务）、开启模拟跟踪、开启实盘建议、最近的建议。
"""
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from ..common import mod, ok


router = APIRouter()


def _item_view(item: dict) -> dict:
    st = mod("strategy.store")
    bt = st.load_backtest(item.get("backtest_key"))
    acc = None
    aid = (item.get("follow") or {}).get("account_id")
    if aid:
        try:
            lg = mod("trading.ledger")
            eng = mod("trading.engine")
            with lg.connect() as c:
                snap = eng.snapshot(c, aid)
            acc = {"id": aid, "total": snap["total"], "return": snap["return"], "positions": len(snap["positions"])}
        except Exception:  # noqa: BLE001  账户被删了
            acc = None
    return {**item, "backtest": {"key": bt["key"], "verdict": bt["verdict"], "saved_at": bt.get("saved_at"),
                                 "holdout": (bt["segments"].get("holdout") or {}).get("excess_cagr")} if bt else None,
            "account": acc, "latest": st.load_latest(item["id"])}


@router.get("/api/strategy")
def strategy_home() -> Any:
    T = mod("strategy.templates")
    st = mod("strategy.store")
    lab = mod("modellab.store")
    return ok({"templates": T.listing(), "entry": T.ENTRY, "trail": T.TRAIL, "param_names": T.PARAM_NAMES,
               "items": [_item_view(x) for x in st.list_items()], "model_enabled": lab.enabled(),
               "holdout_start": str(mod("predict.backtest").HOLDOUT_START)})


@router.post("/api/strategy/items")
def strategy_create(
    template: Annotated[str, Body(embed=True)],
    params: Annotated[dict | None, Body(embed=True)] = None,
    name: Annotated[str | None, Body(embed=True)] = None,
    item_id: Annotated[str | None, Body(embed=True)] = None,
) -> Any:
    return ok(_item_view(mod("strategy.store").save({"id": item_id, "template": template, "params": params, "name": name})))


@router.delete("/api/strategy/items/{item_id}")
def strategy_delete(item_id: str) -> Any:
    mod("strategy.store").delete(item_id)
    return ok({"deleted": item_id})


@router.post("/api/strategy/backtest")
def strategy_backtest(
    template: Annotated[str, Body(embed=True)],
    params: Annotated[dict | None, Body(embed=True)] = None,
    item_id: Annotated[str | None, Body(embed=True)] = None,
    force: Annotated[bool, Body(embed=True)] = False,
) -> Any:
    T = mod("strategy.templates")
    spec = T.resolve(template, params)                       # 参数不对直接 400
    if spec["signal"]["type"] == "model" and not mod("modellab.store").enabled():
        raise HTTPException(400, "这个策略要用实验室的模型：请先在模型实验室训练一个模型，并点“启用到选股器”")
    st = mod("strategy.store")
    if item_id and not force:
        item = st.get(item_id)
        bt = st.load_backtest(item.get("backtest_key"))
        if bt and bt.get("spec") == spec:
            return ok({"result": bt})
    job_id = mod("tasks").submit("strategy_backtest", {"template": template, "params": params or {}, "item_id": item_id},
                                 title=f"策略回测：{spec['name']}")
    return ok({"job_id": job_id})


@router.get("/api/strategy/backtest/{key}")
def strategy_backtest_result(key: str) -> Any:
    res = mod("strategy.store").load_backtest(key)
    if res is None:
        raise HTTPException(404, "还没有这个回测的结果")
    return ok(res)


@router.post("/api/strategy/items/{item_id}/follow")
def strategy_follow(item_id: str, enabled: Annotated[bool, Body(embed=True)]) -> Any:
    st = mod("strategy.store")
    item = st.get(item_id)
    if enabled:
        s = mod("settings").load()
        aid = mod("strategy.follow").ensure_account(item, float(s.profile.capital or 100_000))
        item = st.update(item_id, follow={"enabled": True, "account_id": aid})
    else:
        item = st.update(item_id, follow={**(item.get("follow") or {}), "enabled": False})
    return ok(_item_view(item))


@router.post("/api/strategy/items/{item_id}/live")
def strategy_live(item_id: str, enabled: Annotated[bool, Body(embed=True)]) -> Any:
    return ok(_item_view(mod("strategy.store").update(item_id, live=bool(enabled))))


@router.post("/api/strategy/run")
def strategy_run_now() -> Any:
    """立即按最近一天收盘跑一次模拟跟踪 / 实盘建议（平时每日流水线会自动跑）"""
    return ok({"job_id": mod("tasks").submit("strategy_follow", {})})
