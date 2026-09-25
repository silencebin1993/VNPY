"""
每天收盘后（每日流水线在交易日终之后调用）：
- 开启了"模拟跟踪"的策略：在它自己的模拟账户里走一步（和回测是同一个函数 step.strategy_step）：
  到期 / 出货的持仓挂明天开盘卖，有目标价的挂止盈单，空位按信号挂明天的买单（都会带交易计划和止损）；
- 开启了"实盘建议"的策略：把今天的前几名（带建议止损和股数）存下来，明日计划的"候选买入"会列出来——买入仍要你确认。
- "量化选股 每周调仓"（信号类型 mf）：不用选股方案，直接跟随量化选股的每周组合（mf_follow.mf_step），不需要读全市场历史表。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import polars as pl

from ..trading import calendar as tcal
from ..trading import ledger, rules, sizing
from . import signals as SG
from . import store
from . import templates as T
from .backtest import settings_view
from .step import strategy_step

Progress = Callable[[float, str], None]
LIVE_TOP: int = 5
LOOKBACK_DAYS: int = 90                 # 行情指标（20 日线、平均波幅）往前取多少天
SELECT_DAYS: int = 500                  # 选股条件（公式可能要年线）往前取多少天


def held_days_between(opened: str, day: date) -> int:
    """从买入日到 day 经过了几个交易日（按交易日历）"""
    d0 = date.fromisoformat(opened)
    n, x = 0, d0
    while x < day and n < 400:
        x = tcal.next_trading_day(x)
        if x <= day:
            n += 1
    return n


def is_mf(item: dict) -> bool:
    return T.TEMPLATES.get(item.get("template"), {}).get("signal", {}).get("type") == "mf"


def ensure_account(item: dict, capital: float) -> str:
    if is_mf(item):
        from .mf_follow import MIN_CAPITAL
        capital = max(capital, MIN_CAPITAL)
    acc_id = (item.get("follow") or {}).get("account_id")
    if acc_id:
        try:
            if not ledger.get_account(acc_id).get("archived"):
                return acc_id
        except ValueError:
            pass
    acc = ledger.create_account(f"策略跟踪：{item['name']}", "paper", "paper", capital, note=f"strategy:{item['id']}")
    store.update(item["id"], follow={"enabled": True, "account_id": acc["id"]})
    return acc["id"]


def _regime_key() -> str | None:
    try:
        from ..analysis import market
        r = market.last_regime()
        return r.get("regime") if r else None
    except Exception:  # noqa: BLE001
        return None


def run_daily(day: date | None = None, progress: Progress | None = None, *, base: tuple | None = None) -> dict:
    say: Progress = progress or (lambda f, m: None)
    items: list[dict] = [x for x in store.list_items() if (x.get("follow") or {}).get("enabled") or x.get("live")]
    if not items:
        return {"skipped": "没有开启模拟跟踪或实盘建议的策略"}
    from ..market import history
    day = day or history.last_date()
    if day is None:
        return {"skipped": "还没有日线数据"}
    settings: dict = settings_view()
    boards: list[str] = list(settings["profile"].get("boards") or ["main"])
    capital: float = float(settings["profile"].get("capital") or 100_000)
    next_day: date = tcal.next_trading_day(day)
    out: dict = {"date": str(day), "items": []}
    mf_items: list[dict] = [x for x in items if is_mf(x)]
    items = [x for x in items if not is_mf(x)]
    if mf_items:
        from . import mf_follow
        px_date, px, names = mf_follow.latest_prices()
        for item in mf_items:
            res: dict = {"id": item["id"], "name": item["name"]}
            try:
                if (item.get("follow") or {}).get("enabled"):
                    if px_date != str(day):
                        res["note"] = f"量化选股最近一次打分是 {px_date}，不是 {day}：用那天的收盘价算股数"
                    aid = ensure_account(item, capital)
                    with ledger.connect() as conn:
                        res["follow"] = mf_follow.mf_step(conn, aid, day, next_day, px.get, names.get)
                    store.update(item["id"], follow={**(store.get(item["id"]).get("follow") or {}), "enabled": True, "account_id": aid,
                                                     "target_date": res["follow"].get("target_date"), "last_run": str(day)})
            except Exception as e:  # noqa: BLE001
                res["error"] = f"{type(e).__name__}: {e}"
            out["items"].append(res)
    if not items:
        return out
    if base is None:
        from ..screener import backtest as sbt
        base = sbt.load_or_build_history(use_chips=False, progress=lambda f, m: say(0.3 * f, m))
    table, frame = base
    recent_t = table.filter(pl.col("board").is_in(boards) & (pl.col("date") >= day - timedelta(days=LOOKBACK_DAYS)))
    recent_f = frame.filter(pl.col("code").is_in(recent_t["code"].unique().to_list()) & (pl.col("date") >= day - timedelta(days=LOOKBACK_DAYS)))
    book = SG.PriceBook(recent_t, recent_f)
    sel_t = table.filter(pl.col("date") >= day - timedelta(days=SELECT_DAYS))
    sel_f = frame.filter(pl.col("date") >= day - timedelta(days=SELECT_DAYS))
    regime: str | None = _regime_key()
    for k, item in enumerate(items):
        say(0.3 + 0.7 * k / len(items), f"策略：{item['name']}")
        res: dict = {"id": item["id"], "name": item["name"]}
        try:
            spec: dict = T.resolve(item["template"], item.get("params"))
            scheme, df = SG.selection_table(spec, sel_t, sel_f, boards)
            cands: list[str] = SG.top_by_day(SG.scores_from(spec, scheme, df.filter(pl.col("date") == day), include_latest=True)).get(day, [])
            if (item.get("follow") or {}).get("enabled"):
                aid = ensure_account(item, capital)
                with ledger.connect() as conn:
                    res["follow"] = strategy_step(conn, aid, spec, day, next_day, cands, lambda c: book.info(day, c), settings, regime,
                                                  lambda opened: held_days_between(opened, day))
            if item.get("live"):
                picks: list[dict] = []
                for c in cands:
                    x = book.info(day, c)
                    if not x or not x.get("close") or x.get("stage") in ("distribution", "decline"):
                        continue
                    st = sizing.suggest_stop(x["close"], x.get("atr"), x.get("ma20"), max_pct=float(settings["risk"].get("default_stop_pct", 0.08)))
                    sz = sizing.position_size(capital, x["close"], st["stop"], float(settings["profile"].get("risk_per_trade", 0.01)),
                                              float(settings["risk"].get("max_single_pct", 0.2)), lot=rules.lot_of(c))
                    if sz.shares <= 0:
                        continue
                    target = round(x["close"] + spec["target_r"] * (x["close"] - st["stop"]), 2) if spec.get("target_r") else None
                    picks.append({"code": c, "name": x.get("name"), "close": x["close"], "stop": st["stop"], "stop_basis": st["basis"],
                                  "shares": sz.shares, "amount": sz.amount, "target": target, "rank": len(picks) + 1,
                                  "reasons": f"策略“{item['name']}”今天排第 {len(picks) + 1}", "entry": spec["entry"]})
                    if len(picks) >= LIVE_TOP:
                        break
                store.save_latest(item["id"], {"date": str(day), "id": item["id"], "name": item["name"], "picks": picks})
                res["live"] = len(picks)
        except Exception as e:  # noqa: BLE001  某个策略出错不影响其他策略
            res["error"] = f"{type(e).__name__}: {e}"
        out["items"].append(res)
    return out


def live_candidates(day: str | None = None) -> list[dict]:
    """明日计划用：开启了实盘建议的策略最近一次给出的候选（日期要是最新的）"""
    out: list[dict] = []
    for item in store.list_items():
        if not item.get("live") or is_mf(item):             # 量化选股的调仓清单单独出现在明日计划里（nightly.mf_rebalance）
            continue
        lat = store.load_latest(item["id"])
        if not lat or (day and lat.get("date") != day):
            continue
        for p in lat.get("picks", []):
            out.append({**p, "scheme_id": item["id"], "scheme_name": f"策略：{item['name']}", "result_date": lat["date"]})
    return out
