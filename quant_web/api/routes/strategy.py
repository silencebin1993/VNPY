"""
策略中心接口：模板、我的策略（新建 / 修改 / 删除）、诚实回测（后台任务）、开启模拟跟踪、开启实盘建议、最近的建议。
"量化选股 每周调仓"模板（信号类型 mf）的回测就是量化选股页的回测报告（这里只取摘要），不另外跑。
"""
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from ..common import mod, ok


router = APIRouter()


def _mf_summary() -> dict | None:
    """量化选股回测报告的摘要（默认资金规模、全部样本外）+ 最近一期组合的日期"""
    svc = mod("multifactor.service")
    rep = svc.load_report()
    if not rep:
        return None
    default = rep["spec"]["default_capital"]
    cap = next((c for c in rep["capacity"] if c["capital"] == default), None)
    seg = (cap or {}).get("segments", {}).get("全部样本外")
    if not seg:
        return None
    good = seg["excess_ann"] > 0 and seg["excess_t"] >= 2
    key = "good" if good else "weak" if seg["excess_ann"] > 0 else "bad"
    recs = svc.load_track().get("records") or []
    today = svc.load_today() or {}
    return {
        "oos_start": rep["oos_start"], "data_end": rep["data_end"], "generated_at": rep["generated_at"], "capital": default,
        **{k: seg.get(k) for k in ("cagr", "bench_cagr", "excess_ann", "excess_t", "maxdd", "bench_maxdd", "random_pct")},
        "verdict": {"key": key, "credible": good,
                    "text": f"样本外（{rep['oos_start'][:7]} 起，{default / 1e4:.0f} 万资金口径，扣费后）年化 {seg['cagr'] * 100:.1f}%，"
                            f"同池随机 {seg['bench_cagr'] * 100:.1f}%，每年超额 {seg['excess_ann'] * 100:+.1f}%，t 值 {seg['excess_t']:.1f}。"},
        "last_record": recs[-1]["date"] if recs else None, "records": len(recs),
        "today": {k: today.get(k) for k in ("date", "rebalance_day", "next_trade_day")} if today else None,
    }


def _cached_backtest(spec: dict) -> dict | None:
    """同样的模板 + 参数以前回测过（不管是不是存成了"我的策略"）就直接用"""
    bt = mod("strategy.backtest")
    sv = bt.settings_view()
    boards = list(sv["profile"].get("boards") or ["main"])
    capital = float(sv["profile"].get("capital") or 100_000)
    extra = (mod("modellab.store").enabled() or "") if spec["signal"]["type"] == "model" else ""
    return mod("strategy.store").load_backtest(bt.key_of(spec, boards, extra, capital, sv))


def _item_view(item: dict, mf: dict | None = None) -> dict:
    st = mod("strategy.store")
    T = mod("strategy.templates")
    is_mf = T.TEMPLATES.get(item.get("template"), {}).get("signal", {}).get("type") == "mf"
    bt = None if is_mf else st.load_backtest(item.get("backtest_key"))
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
    if is_mf:
        mf = mf if mf is not None else _mf_summary()
        backtest = {"key": None, "source": "mf", "verdict": mf["verdict"], "saved_at": mf["generated_at"]} if mf else None
    else:
        backtest = {"key": bt["key"], "verdict": bt["verdict"], "saved_at": bt.get("saved_at"),
                    "holdout": (bt["segments"].get("holdout") or {}).get("excess_cagr")} if bt else None
    return {**item, "backtest": backtest, "account": acc, "latest": None if is_mf else st.load_latest(item["id"]), "is_mf": is_mf}


@router.get("/api/strategy")
def strategy_home() -> Any:
    T = mod("strategy.templates")
    st = mod("strategy.store")
    lab = mod("modellab.store")
    mf = _mf_summary()
    return ok({"templates": T.listing(), "entry": T.ENTRY, "trail": T.TRAIL, "param_names": T.PARAM_NAMES,
               "items": [_item_view(x, mf) for x in st.list_items()], "model_enabled": lab.enabled(), "mf": mf,
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
    peek: Annotated[bool, Body(embed=True)] = False,
) -> Any:
    """已经回测过同样的模板 + 参数就直接返回结果；peek=true 只查缓存（没有就返回 result=None，不启动任务）"""
    T = mod("strategy.templates")
    spec = T.resolve(template, params)                       # 参数不对直接 400
    if spec["signal"]["type"] == "mf":
        raise HTTPException(400, "“量化选股 每周调仓”的回测就是量化选股页的回测报告，请在量化选股页查看或重新回测")
    if spec["signal"]["type"] == "model" and not mod("modellab.store").enabled():
        if peek:
            return ok({"result": None})
        raise HTTPException(400, "这个策略要用实验室的模型：请先在模型实验室训练一个模型，并点“启用到选股器”")
    st = mod("strategy.store")
    if not force:
        bt = None
        if item_id:
            bt = st.load_backtest(st.get(item_id).get("backtest_key"))
            cap_now = float(mod("strategy.backtest").settings_view()["profile"].get("capital") or 100_000)
            if bt and (bt.get("spec") != spec or float(bt.get("capital") or 0) != cap_now):
                bt = None
        bt = bt or _cached_backtest(spec)
        if bt:
            if item_id and st.get(item_id).get("backtest_key") != bt["key"]:
                st.update(item_id, backtest_key=bt["key"])
            return ok({"result": bt})
        if peek:
            return ok({"result": None})
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
    st = mod("strategy.store")
    if enabled and mod("strategy.follow").is_mf(st.get(item_id)):
        raise HTTPException(400, "量化选股的调仓清单每个调仓日会自动出现在“交易 → 明日计划”，量化选股页也有按你的资金算好的下单清单，不需要再开实盘建议")
    return ok(_item_view(st.update(item_id, live=bool(enabled))))


@router.post("/api/strategy/run")
def strategy_run_now() -> Any:
    """立即按最近一天收盘跑一次模拟跟踪 / 实盘建议（平时每日流水线会自动跑）"""
    return ok({"job_id": mod("tasks").submit("strategy_follow", {})})
