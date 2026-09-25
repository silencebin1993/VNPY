"""
模型实验室接口：选项、训练前估算、开始训练（后台任务）、训练记录（详情 / 删除 / 启用到选股器）、最新打分、单因子检验。
训练只在用户点按钮后运行；估算超过内存上限时直接拒绝。
"""
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from ..common import mod, ok


router = APIRouter()


@router.get("/api/lab/options")
def lab_options() -> Any:
    opts = mod("modellab.options")
    S = mod("modellab.store")
    s = mod("settings").load()
    return ok({
        "universes": [{"key": k, "label": v["label"], "desc": v["desc"]} for k, v in opts.UNIVERSES.items()],
        "labels": [{"key": k, "label": v["label"], "desc": v["desc"], "task": v["task"], "hold": v["hold"]} for k, v in opts.LABELS.items()],
        "presets": [{"key": k, "label": v["label"], "desc": v["desc"]} for k, v in opts.PRESETS.items()],
        "normalize": [{"key": k, "label": v} for k, v in opts.NORMALIZE.items()],
        "factor_sets": mod("modellab.factors").listing(), "models": mod("modellab.models").listing(),
        "defaults": opts.DEFAULT_CONFIG, "trials": S.trials(), "enabled": S.enabled(),
        "settings": s.lab.model_dump(), "holdout_start": str(mod("predict.backtest").HOLDOUT_START),
    })


@router.post("/api/lab/estimate")
def lab_estimate(config: Annotated[dict, Body(embed=True)]) -> Any:
    cfg = mod("modellab.options").validate_config(config)
    return ok({"config": cfg, "describe": mod("modellab.options").describe(cfg), **mod("modellab.dataset").estimate(cfg)})


@router.post("/api/lab/train")
def lab_train(config: Annotated[dict, Body(embed=True)]) -> Any:
    opts = mod("modellab.options")
    cfg = opts.validate_config(config)
    est = mod("modellab.dataset").estimate(cfg)
    if est["blocked"]:
        raise HTTPException(400, est["reason"])
    job_id = mod("tasks").submit("lab_train", {"config": cfg}, title=f"模型训练：{opts.describe(cfg)}")
    return ok({"job_id": job_id, "estimate": est})


@router.get("/api/lab/runs")
def lab_runs() -> Any:
    S = mod("modellab.store")
    return ok({"runs": S.list_runs(), "enabled": S.enabled(), "trials": S.trials()})


def _names() -> dict[str, str]:
    try:
        u = mod("market.universe").load_universe()
        return dict(zip(u["code"].to_list(), u["name"].to_list(), strict=True)) if not u.is_empty() else {}
    except Exception:  # noqa: BLE001  没有股票列表时只显示代码
        return {}


@router.get("/api/lab/runs/{run_id}")
def lab_run(run_id: str) -> Any:
    S = mod("modellab.store")
    run = S.get_run(run_id)
    names = _names()
    latest = run.get("latest") or {}
    rows = [{**r, "name": names.get(r["code"])} for r in latest.get("rows", [])]
    return ok({**run, "latest": {**latest, "rows": rows}, "summary": S.summary(run)})


@router.delete("/api/lab/runs/{run_id}")
def lab_delete(run_id: str) -> Any:
    mod("modellab.store").delete_run(run_id)
    return ok({"deleted": run_id})


@router.post("/api/lab/runs/{run_id}/enable")
def lab_enable(run_id: str, ack: Annotated[bool, Body(embed=True)] = False) -> Any:
    """启用到选股器。留出期（样本外）没有显著优势的模型，必须明确确认（ack=true）才启用"""
    st = mod("modellab.store")
    run = st.get_run(run_id)
    credible = bool(((run.get("evaluation") or {}).get("verdict") or {}).get("credible"))
    if not credible and not ack:
        raise HTTPException(400, "这个模型在留出期（样本外）没有显著优势：启用后选股器的“模型打分”可能和随便挑差不多。确定要启用，请在页面上确认")
    return ok(st.enable(run_id))


@router.post("/api/lab/disable")
def lab_disable() -> Any:
    mod("modellab.store").disable()
    return ok({"enabled": None})


@router.get("/api/lab/scores")
def lab_scores() -> Any:
    """启用的模型对最新一天的打分（只读缓存；没有时 stale=True，页面提示点“重新打分”）"""
    S = mod("modellab.store")
    rid = S.enabled()
    if not rid:
        return ok({"enabled": None, "rows": [], "stale": False})
    res = S.cached_latest(rid)
    if res is None:
        return ok({"enabled": rid, "rows": [], "stale": True})
    return ok({**res, "enabled": rid, "stale": False})


@router.post("/api/lab/score")
def lab_score(force: Annotated[bool, Body(embed=True)] = False) -> Any:
    rid = mod("modellab.store").enabled()
    if not rid:
        raise HTTPException(400, "还没有启用的模型")
    return ok({"job_id": mod("tasks").submit("lab_score", {"run_id": rid, "force": force})})


@router.post("/api/lab/factor_test")
def lab_factor_test(config: Annotated[dict, Body(embed=True)]) -> Any:
    opts = mod("modellab.options")
    cfg = opts.validate_config({**config, "model": "lightgbm", "normalize": "none"})
    key = mod("modellab.factor_test").key_of(cfg)
    job_id = mod("tasks").submit("lab_factor_test", {"config": cfg}, title=f"单因子检验：{opts.describe(cfg)}")
    return ok({"job_id": job_id, "key": key})


@router.get("/api/lab/factor_test/{key}")
def lab_factor_test_result(key: str) -> Any:
    res = mod("modellab.factor_test").load(key)
    if res is None:
        raise HTTPException(404, "还没有这个检验的结果")
    return ok(res)
