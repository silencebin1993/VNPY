"""
第三版后台任务白名单：POST /api/jobs/run {"name": ..., "params": {...}} 只能启动这里登记过的任务。

- 每个任务：名称 → TaskSpec(中文标题, 运行函数 run(params, progress), 任务组, 说明)；
- 任务组相同的任务排队依次运行（HEAVY = 会读写日线面板/模型文件的重任务，和原来的更新/训练共用一把锁）；
- 同名同参数的任务正在运行时，返回已有任务的 id（不会重复启动）；
- 运行函数一律在函数内部延迟导入，某个功能模块出错只影响它自己的任务。
各功能模块用 @task(...) 在这里登记（集中在本文件，便于一眼看清程序会在后台做哪些事）。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .jobs import HEAVY, JOBS, Progress


EXT: str = "ext"            # 扩展数据下载（网络为主，不碰日线面板）


@dataclass
class TaskSpec:
    name: str
    title: str
    run: Callable[[dict, Progress], Any]
    group: str | None = None
    description: str = ""


TASKS: dict[str, TaskSpec] = {}


def task(name: str, title: str, group: str | None = None, description: str = "") -> Callable:
    def deco(fn: Callable[[dict, Progress], Any]) -> Callable[[dict, Progress], Any]:
        TASKS[name] = TaskSpec(name, title, fn, group, description)
        return fn
    return deco


def job_name(name: str, params: dict) -> str:
    """同名同参数视为同一个任务（正在运行时不重复启动）"""
    if not params:
        return name
    text: str = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    if len(text) > 100:                         # 参数很长（例如公式全文）时用哈希，避免只比较开头导致误判为同一个任务
        text = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return f"{name}:{text}"


def submit(name: str, params: dict | None = None, title: str | None = None) -> str:
    spec: TaskSpec | None = TASKS.get(name)
    if spec is None:
        raise ValueError(f"没有「{name}」这个后台任务")
    if params is not None and not isinstance(params, dict):
        raise ValueError("任务参数格式不对（应为对象）")
    p: dict = dict(params or {})
    return JOBS.submit(job_name(name, p), title or spec.title, lambda progress: spec.run(p, progress), group=spec.group)


def listing() -> list[dict]:
    return [{"name": s.name, "title": s.title, "group": s.group, "description": s.description} for s in TASKS.values()]


# ================================================================ 任务登记

@task("ext_update", "更新扩展数据", group=EXT,
      description="下载资金流、融资融券、股东户数、限售解禁、股权质押、业绩预告、股东增减持、指数日线和成分股")
def _ext_update(params: dict, progress: Progress) -> Any:
    from .providers import updates

    codes: Any = params.get("codes")
    return updates.update_all(progress=progress, flow_codes=list(codes) if codes else None)


def formula_params(params: dict) -> dict:
    """验证参数整理（也用于结果缓存的 key）：持有天数、板块、是否排除 ST、成交额门槛"""
    holds: list[int] = sorted({int(h) for h in (params.get("holds") or [5, 10, 20]) if 1 <= int(h) <= 60})[:4] or [5, 10, 20]
    boards: list[str] = [b for b in (params.get("boards") or ["main"]) if b in ("main", "chinext", "star", "bj")] or ["main"]
    return {"holds": holds, "boards": boards, "exclude_st": bool(params.get("exclude_st", True)),
            "min_amount": float(params.get("min_amount") or 0)}


@task("formula_validate", "公式历史验证", group=HEAVY,
      description="在全部历史日线上按真实规则检验一个公式：次日开盘买、持有 N 天，扣成本，和同日随机比，分样本内/样本外")
def _formula_validate(params: dict, progress: Progress) -> Any:
    from .formula import store, validate

    text: str = str(params.get("text") or "")
    p: dict = formula_params(params)
    result: dict = validate.run(text, holds=tuple(p["holds"]), boards=tuple(p["boards"]), exclude_st=p["exclude_st"],
                                min_amount=p["min_amount"], progress=progress)
    key: str = store.result_key(text, p)
    result["params"] = p
    store.save_result(key, result)
    return {"key": key, "verdicts": {h: r["verdict"] for h, r in result["holds"].items()}}


@task("stage_stats", "主力阶段历史验证", group=HEAVY,
      description="对全市场历史逐日判断主力阶段，统计每个阶段之后 5/10/20 天的真实表现（和同日所有股票比，分样本内/样本外）")
def _stage_stats(params: dict, progress: Progress) -> Any:
    from .analysis import stage_stats

    return stage_stats.run(use_chips=bool(params.get("use_chips", True)), progress=progress)


@task("regime_refresh", "更新大盘环境", group=HEAVY, description="重新计算全市场宽度表和大盘环境")
def _regime_refresh(params: dict, progress: Progress) -> Any:
    from .analysis import market

    progress(0.1, "正在计算全市场宽度……")
    res: dict = market.current_regime(force=True)
    return {"date": res["date"], "regime": res["label"]}


@task("screener_backtest", "选股方案回测", group=HEAVY,
      description="在全部历史上按方案定期选股（次日开盘买、持有 N 天），扣成本，和同日随机比，分样本内/样本外")
def _screener_backtest(params: dict, progress: Progress) -> Any:
    from . import settings as settings_mod
    from .screener import backtest, schemes, store

    sch: dict = schemes.validate_scheme(params["scheme"])
    prof: dict = settings_mod.load().profile.model_dump()
    p: dict = {"hold": int(params.get("hold") or 10), "rebalance": int(params.get("rebalance") or params.get("hold") or 10),
               "use_chips": bool(params.get("use_chips", True)), "boards": params.get("boards") or sch["universe"]["boards"] or prof["boards"]}
    res: dict = backtest.run(sch, hold=p["hold"], rebalance=p["rebalance"], profile=prof, use_chips=p["use_chips"], progress=progress)
    key: str = store.backtest_key(sch, p)
    res["params"] = p
    store.save_backtest(key, res)
    return {"key": key, "verdict": res["verdict"]}


@task("assistant_daily", "投资助手每日流水线", group=HEAVY,
      description="大盘环境、扩展数据、每天的选股方案、交易日终、明日计划（交易日收盘更新数据后会自动跑）")
def _assistant_daily(params: dict, progress: Progress) -> Any:
    from .assistant import daily
    return daily.run(progress=progress)


@task("trading_eod", "交易日终处理", group=HEAVY, description="模拟盘按日线撮合、除权、移动止盈、资产快照；实盘同步成交")
def _trading_eod(params: dict, progress: Progress) -> Any:
    from .trading import service
    return service.run_eod(progress=progress)


@task("nightly_plan", "生成明日计划", description="持仓怎么做、条件单清单、候选买入")
def _nightly(params: dict, progress: Progress) -> Any:
    from .trading import nightly
    n = nightly.build()
    return {"date": n["date"], "accounts": len(n["accounts"]), "candidates": len(n["candidates"])}


@task("lab_train", "模型实验室：训练", group=HEAVY,
      description="按选的范围、因子、目标、模型滚动训练（每段只用之前的数据），样本外评估并和同日随机比；耗时几分钟到一小时以上")
def _lab_train(params: dict, progress: Progress) -> Any:
    from .modellab import train
    return train.run(params.get("config") or params, progress=progress)


@task("lab_factor_test", "模型实验室：单因子检验", group=HEAVY,
      description="不训练模型，逐个检验因子和之后超额收益的关系（RankIC、t 值），分样本内 / 样本外")
def _lab_factor_test(params: dict, progress: Progress) -> Any:
    from .modellab import factor_test
    return factor_test.run(params.get("config") or params, progress=progress)


@task("lab_score", "模型实验室：给最新一天打分", group=HEAVY, description="用启用的模型给本地数据最新一天的股票打分（选股器“模型打分”用）")
def _lab_score(params: dict, progress: Progress) -> Any:
    from .modellab import store
    res = store.latest_scores(params.get("run_id"), force=bool(params.get("force")), progress=progress)
    return {"run_id": res["run_id"], "date": res["date"], "rows": len(res["rows"])}


@task("strategy_backtest", "策略回测", group=HEAVY,
      description="在内存账本里逐日跑真正的模拟盘规则（T+1、手续费、止损、移动止盈），和同样规则的随机选股比，分选择期 / 留出期")
def _strategy_backtest(params: dict, progress: Progress) -> Any:
    from .strategy import backtest, store
    res = backtest.run(params["template"], params.get("params"), progress=progress)
    store.save_backtest(res)
    if params.get("item_id"):
        store.update(params["item_id"], backtest_key=res["key"])
    return {"key": res["key"], "verdict": res["verdict"]}


@task("strategy_follow", "策略模拟跟踪", group=HEAVY, description="开启了模拟跟踪 / 实盘建议的策略，按今天收盘的信号挂明天的单或给出建议")
def _strategy_follow(params: dict, progress: Progress) -> Any:
    from .strategy import follow
    return follow.run_daily(progress=progress)


__all__ = ["EXT", "HEAVY", "TASKS", "TaskSpec", "formula_params", "job_name", "listing", "submit", "task"]
