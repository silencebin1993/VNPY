"""
每日"投资助手"流水线（交易日收盘、日线更新之后，由 jobs.run_daily 调用；也可以单独在数据页点）：
1. 大盘环境（日线变了才重算）；
2. 扩展数据：持仓 + 自选 + 候选股的资金流、指数日线（每天）；其余扩展数据每周一次；
3. 模型实验室启用的模型给最新一天打分（选股器"模型打分"用）；每天自动运行的选股方案（settings.assistant.screeners，默认"反转 + 低估值 + 低换手"）；
   量化选股（多因子 + LightGBM）给最新一天打分，本周最后一个交易日记下调仓组合（前向跟踪）；
4. 交易日终：模拟盘撮合、除权、移动止盈、资产快照；实盘同步成交、检查计划；
5. 明日计划（持仓怎么做 + 条件单清单 + 候选）并推送摘要。
每一步单独 try/except：某一步出错只记在结果里，不影响后面的步骤，也不影响原来的数据更新和预测。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime

log = logging.getLogger("quant_web.assistant")
WEEKLY_DAYS: int = 7


def _step(out: dict, name: str, fn: Callable[[], object]) -> None:
    try:
        out[name] = fn()
    except Exception as e:  # noqa: BLE001  单步失败只记录
        log.exception("投资助手：%s 失败", name)
        out.setdefault("errors", []).append(f"{name}：{type(e).__name__}: {e}")


def _flow_codes(limit: int) -> list[str]:
    import json

    from .. import config
    from ..trading import ledger, nightly

    codes: list[str] = []
    with ledger.connect() as c:
        codes += [r["code"] for r in ledger.rows(c, "SELECT DISTINCT code FROM positions")]
    try:
        data = json.loads(config.WATCHLIST_FILE.read_text(encoding="utf-8")) if config.WATCHLIST_FILE.exists() else []
        codes += [x["code"] if isinstance(x, dict) else str(x) for x in data]
    except (OSError, ValueError):
        pass
    codes += [r["code"] for r in nightly.candidates(limit=20)]
    return list(dict.fromkeys(codes))[:limit]


def run(progress: Callable[[float, str], None] | None = None, day: date | None = None) -> dict:
    from .. import config
    from .. import settings as settings_mod
    from ..market import history

    say = progress or (lambda f, m: None)
    s = settings_mod.load()
    out: dict = {"started": datetime.now(config.CHINA_TZ).strftime("%Y-%m-%d %H:%M")}
    if not s.assistant.enabled:
        return {**out, "skipped": "投资助手每日流水线已在设置里关闭"}
    day = day or history.last_date()
    if day is None:
        return {**out, "skipped": "还没有日线数据"}
    out["date"] = str(day)

    say(0.02, "投资助手：更新大盘环境……")

    def regime() -> dict:
        from ..analysis import market
        r = market.current_regime()
        return {"label": r["label"], "cap": r["cap"]}
    _step(out, "regime", regime)

    say(0.15, "投资助手：更新资金流和指数……")

    def ext() -> dict:
        from ..providers import updates
        from ..trading import ledger
        res: dict = {"fund_flow": updates.update_fund_flow(_flow_codes(s.assistant.fund_flow_limit), s.assistant.fund_flow_limit),
                     "index_bars": updates.update_index_bars()}
        last = ledger.get_meta("ext_full_update")
        if not last or (day - date.fromisoformat(last)).days >= WEEKLY_DAYS:
            res["weekly"] = updates.update_all()
            ledger.set_meta("ext_full_update", str(day))
        return {k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk in ("ok", "rows", "error", "source")})
                for k, v in res.items()}
    _step(out, "ext", ext)

    say(0.35, "投资助手：启用的模型给最新一天打分……")

    def lab_scores() -> dict:
        from ..modellab import store as lab
        if not lab.enabled():
            return {"skipped": "没有启用的模型"}
        r = lab.latest_scores()
        return {"run_id": r["run_id"], "date": r["date"], "rows": len(r["rows"])}
    _step(out, "model_scores", lab_scores)

    say(0.4, "投资助手：运行每天的选股方案……")

    def screens() -> dict:
        from ..screener import engine, store
        prof, risk = s.profile.model_dump(), s.risk.model_dump()
        cache: dict = {}
        res: dict = {}
        for sid in list(s.assistant.screeners) or ["reversal_value"]:
            try:
                r = engine.run(store.get(sid), profile=prof, risk=risk, cache=cache)
                store.save_result(sid, r)
                res[sid] = {"rows": len(r["rows"]), "matched": r["matched_n"]}
            except Exception as e:  # noqa: BLE001  某个方案出错不影响其他方案
                res[sid] = {"error": str(e)}
        return res
    _step(out, "screeners", screens)

    say(0.55, "投资助手：量化选股打分……")

    def multifactor() -> dict:
        from ..multifactor import service as mf
        return mf.run_daily()
    _step(out, "multifactor", multifactor)

    say(0.7, "投资助手：交易日终处理……")
    def eod() -> dict:
        from ..trading import service
        return service.run_eod(day)
    _step(out, "eod", eod)

    say(0.8, "投资助手：策略模拟跟踪和实盘建议……")

    def strategies() -> dict:
        from ..strategy import follow
        return follow.run_daily(day)
    _step(out, "strategies", strategies)

    say(0.85, "投资助手：生成明日计划……")

    def plan() -> dict:
        from .. import notify
        from ..trading import nightly
        n = nightly.build(day)
        acts = [a for acc in n["accounts"] for a in acc["positions"] if a["action"] != "继续持有"]
        lines: list[str] = []
        # 先说数据和名单能不能用：前面哪一步失败了、日线是否最新完整、量化选股名单是否按最新数据打的分
        try:
            from ..multifactor import service as mf
            hc = mf.health()
        except Exception as e:  # noqa: BLE001
            hc = {"ok": False, "items": [{"level": "bad", "text": f"自检没能完成：{e}"}]}
        bad = [i["text"] for i in hc.get("items", []) if i["level"] == "bad"]
        errs = list(out.get("errors") or [])
        if bad or errs:
            lines.append("⚠ 今晚的数据或名单有问题，先别按清单下单：" + "；".join(bad + [f"这一步没跑成功：{x}" for x in errs]))
        mfr = n.get("mf_rebalance")
        if mfr and not bad:
            lines.append(f"量化选股调仓日：明天开盘卖出 {len(mfr['sells'])} 只、买入 {len(mfr['buys'])} 只（股数在量化选股页的下单清单）")
        if n.get("regime"):
            lines.append(f"大盘环境：{n['regime']['label']}（你设置的仓位上限 {int((n['regime']['cap'] or 0) * 100)}%；这是控制波动的经验规则，不是涨跌预测）")
        lines += [f"{a['name'] or a['code']}：{a['action']}（{'；'.join(a['reasons'])}）" for a in acts]
        if n["candidates"]:
            lines.append("选股器候选（历史回测没有跑赢随机，只供参考）：" + "、".join(f"{c['name']}" for c in n["candidates"][:5]))
        level = "urgent" if (bad or errs) else "warn" if acts or mfr else "info"
        notify.send(f"明日计划（{n['date']}）", "\n".join(lines) or "没有需要处理的持仓。", level=level, kind="nightly")
        return {"actions": len(acts), "candidates": len(n["candidates"]), "health_ok": not bad, "errors": len(errs)}
    _step(out, "nightly", plan)
    say(1.0, "投资助手：完成")
    return out
