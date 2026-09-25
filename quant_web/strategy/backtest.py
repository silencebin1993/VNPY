"""
策略的诚实回测：在内存账本里逐日跑真正的模拟盘代码（trading.service.paper_eod → engine.settle_day → strategy.step），
和模拟跟踪完全同一套规则（T+1、一手、手续费、印花税按日期、一字板买卖不了、除权、止损条件单、移动止盈）。
对照：同样的买卖规则和仓位，但每天在同一范围里随机挑股票（默认 5 次取平均）——看"选股"本身有没有用。
选择期（2025-07 之前）和留出期（之后）各用全新账户算一遍；结论以留出期为准。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import date

import numpy as np
import polars as pl

from ..predict import stats
from ..predict.backtest import HOLDOUT_START
from ..trading import engine, ledger
from ..trading import service as tservice
from . import signals as SG
from . import templates as T
from .step import strategy_step

Progress = Callable[[float, str], None]
WARMUP_DAYS: int = 260                  # 前面约一年只用来"热身"（年线、相对强度等需要历史）
SEEDS: int = 5
YEAR_DAYS: int = 243


def settings_view() -> dict:
    s = tservice.settings_dict()
    return {"profile": s["profile"], "risk": s["risk"]}


def simulate(spec: dict, days: list[date], cands: dict, book: SG.PriceBook, regimes: dict, settings: dict,
             capital: float) -> dict:
    """在内存账本里从 days[0] 跑到 days[-1]（全新账户）。返回每日资产、成交复盘"""
    index: dict = {d: i for i, d in enumerate(book.days)}
    equity: list[tuple[date, float, float]] = []
    with ledger.isolated() as conn:
        acc = ledger.create_account("回测", "paper", "paper", capital)
        aid: str = acc["id"]
        for i, d in enumerate(days):
            codes = [r["code"] for r in ledger.rows(conn, "SELECT code FROM positions WHERE account_id=? UNION SELECT DISTINCT code FROM orders "
                                                          "WHERE account_id=? AND status IN ('submitted','partial','waiting_trigger')", (aid, aid))]
            bars = {c: b for c in codes if (b := book.bar(d, c)) is not None}
            tservice.paper_eod(conn, aid, d, bars, book.levels(d), alerts=False)
            snap = engine.settle_day(conn, aid, d, {c: b.close for c, b in bars.items()})
            equity.append((d, float(snap["total"]), float(snap["market_value"])))
            if i + 1 < len(days):
                strategy_step(conn, aid, spec, d, days[i + 1], cands.get(d, []), lambda c, d=d: book.info(d, c), settings,
                              regimes.get(d), lambda opened, d=d: index.get(d, 0) - index.get(date.fromisoformat(opened), index.get(d, 0)))
        trades: list[dict] = ledger.rows(conn, "SELECT * FROM journal WHERE account_id=? ORDER BY closed", (aid,))
        n_fills: int = int(ledger.one(conn, "SELECT COUNT(*) AS n FROM fills WHERE account_id=?", (aid,))["n"])
    return {"equity": equity, "trades": trades, "fills": n_fills}


def _curve(equity: list[tuple[date, float, float]], capital: float) -> dict:
    if not equity:
        return {"days": 0}
    tot = np.array([e[1] for e in equity])
    mv = np.array([e[2] for e in equity])
    ret = np.diff(np.concatenate([[capital], tot])) / np.concatenate([[capital], tot[:-1]])
    years = max(len(tot) / YEAR_DAYS, 1e-9)
    peak = np.maximum.accumulate(np.concatenate([[capital], tot]))[1:]
    sd = float(np.std(ret, ddof=1)) if len(ret) > 1 else 0.0
    return {"days": len(tot), "total": float(tot[-1] / capital - 1), "cagr": float((tot[-1] / capital) ** (1 / years) - 1),
            "max_dd": float(np.min(tot / peak - 1)), "sharpe": float(np.mean(ret) / sd * np.sqrt(YEAR_DAYS)) if sd > 0 else None,
            "exposure": float(np.mean(mv / tot)), "start": str(equity[0][0]), "end": str(equity[-1][0])}


def _trades(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    pnl = np.array([t["pnl"] or 0 for t in trades], dtype=float)
    rs = np.array([t["r_multiple"] for t in trades if t.get("r_multiple") is not None], dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    return {"n": n, "win": float((pnl > 0).mean()), "avg_r": float(rs.mean()) if len(rs) else None,
            "profit_factor": float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else None,
            "avg_days": float(np.mean([t["days"] or 0 for t in trades]))}


def _segment(spec: dict, days: list[date], strat: dict, rand: list[dict], book, regimes, settings, capital: float) -> dict:
    s = simulate(spec, days, strat, book, regimes, settings, capital)
    rs = [simulate(spec, days, r, book, regimes, settings, capital) for r in rand]
    sc, tc = _curve(s["equity"], capital), _trades(s["trades"])
    rcs = [_curve(r["equity"], capital) for r in rs]
    # 每天的超额收益（策略 − 随机平均）→ t 值
    tot = np.array([e[1] for e in s["equity"]])
    rt = np.mean([[e[1] for e in r["equity"]] for r in rs], axis=0) if rs else tot
    ret_s = np.diff(np.concatenate([[capital], tot])) / np.concatenate([[capital], tot[:-1]])
    ret_r = np.diff(np.concatenate([[capital], rt])) / np.concatenate([[capital], rt[:-1]])
    ex = ret_s - ret_r
    dt, nwt = (stats.daily_t(ex), stats.nw_t(ex, lag=10)) if len(ex) > 20 else (None, None)
    t = min(dt, nwt) if dt is not None and nwt is not None else (dt if nwt is None else nwt)
    rc = [c.get("cagr") for c in rcs if c.get("cagr") is not None]
    return {
        "strategy": {**sc, "trades": tc, "fills": s["fills"]},
        "random": {"cagr": float(np.mean(rc)) if rc else None, "cagr_min": float(np.min(rc)) if rc else None,
                   "cagr_max": float(np.max(rc)) if rc else None,
                   "max_dd": float(np.mean([c["max_dd"] for c in rcs])) if rcs else None,
                   "beat": int(sum(1 for c in rc if sc.get("cagr") is not None and sc["cagr"] > c)), "n": len(rc)},
        "excess_cagr": (sc["cagr"] - float(np.mean(rc))) if rc and sc.get("cagr") is not None else None,
        "t": stats.rounded(t), "daily_t": stats.rounded(dt), "nw_t": stats.rounded(nwt),
        "curve": {"dates": [str(e[0]) for e in s["equity"]], "strategy": [round(e[1] / capital - 1, 4) for e in s["equity"]],
                  "random": [round(float(v) / capital - 1, 4) for v in rt]},
        "recent_trades": s["trades"][-30:],
    }


def verdict(segs: dict) -> dict:
    ho: dict = segs.get("holdout") or {}
    sel: dict = segs.get("selection") or {}
    if not ho or (ho.get("strategy") or {}).get("days", 0) < 60:
        return {"key": "short", "credible": False, "text": "留出期（2025-07 之后）太短，还不能下结论。"}
    ex, t = ho.get("excess_cagr"), ho.get("t")
    sel_ex = sel.get("excess_cagr")
    if ex is not None and ex > 0 and t is not None and t >= 2 and (sel_ex is None or sel_ex > 0):
        return {"key": "good", "credible": True,
                "text": f"留出期（样本外）年化比同样规则的随机选股高 {ex * 100:.1f}%，t 值 {t:.1f}，有一定可信度（仍不保证以后有效）。"}
    if ex is not None and ex > 0:
        return {"key": "weak", "credible": False,
                "text": f"留出期年化比随机选股高 {ex * 100:.1f}%，但 t 值 {t if t is not None else '—'}，还不够可信，可能是运气。"}
    return {"key": "bad", "credible": False,
            "text": f"留出期年化比同样规则的随机选股低 {abs(ex or 0) * 100:.1f}%：选股这一步没有帮上忙。" if ex is not None else "没有足够的数据"}


def key_of(spec: dict, boards: list[str], extra: str = "") -> str:
    raw = json.dumps({"spec": spec, "boards": boards, "extra": extra}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def run(template_id: str, params: dict | None = None, progress: Progress | None = None, *, seeds: int = SEEDS,
        base: tuple | None = None, capital: float | None = None) -> dict:
    say: Progress = progress or (lambda f, m: None)
    t0: float = time.time()
    spec: dict = T.resolve(template_id, params)
    settings: dict = settings_view()
    boards: list[str] = list(settings["profile"].get("boards") or ["main"])
    capital = float(capital or settings["profile"].get("capital") or 100_000)
    say(0.02, "读取全历史数据……")
    if base is None:
        from ..screener import backtest as sbt
        base = sbt.load_or_build_history(use_chips=False, progress=lambda f, m: say(0.02 + 0.2 * f, m))
    table, frame = base
    say(0.24, "整理每天的真实价格和指标……")
    uni: pl.DataFrame = table.filter(pl.col("board").is_in(boards))
    book = SG.PriceBook(uni, frame.filter(pl.col("code").is_in(uni["code"].unique().to_list())))
    if len(book.days) <= WARMUP_DAYS + 20:
        raise ValueError("本地历史数据太短（不到一年半），没法做策略回测")
    days: list[date] = book.days[WARMUP_DAYS:]
    say(0.3, "计算每天的选股信号……")
    scheme, sel_df = SG.selection_table(spec, table, frame, boards)
    strat = SG.top_by_day(SG.scores_from(spec, scheme, sel_df))
    rand = [SG.top_by_day(SG.scores_from(spec, scheme, sel_df, random_seed=k)) for k in range(seeds)]
    del sel_df
    if not any(strat.get(d) for d in days):
        raise ValueError("这个策略在历史上一只股票都没选出来（如果是模型策略，请先在实验室训练并启用模型）")
    regimes: dict = SG.regime_by_day() if spec.get("regime") else {}
    segs: dict = {}
    parts = (("selection", [d for d in days if d < HOLDOUT_START]), ("holdout", [d for d in days if d >= HOLDOUT_START]))
    for k, (name, seg_days) in enumerate(parts):
        if len(seg_days) < 20:
            continue
        say(0.4 + 0.28 * k, f"逐日模拟{'选择期' if name == 'selection' else '留出期'}（策略 + {seeds} 次随机对照）……")
        segs[name] = _segment(spec, seg_days, strat, rand, book, regimes, settings, capital)
    extra = ""
    if spec["signal"]["type"] == "model":
        from ..modellab import store as lab
        extra = lab.enabled() or ""
    res: dict = {
        "key": key_of(spec, boards, extra), "spec": spec, "boards": boards, "capital": capital, "seeds": seeds,
        "holdout_start": str(HOLDOUT_START), "segments": segs, "verdict": verdict(segs),
        "data_start": str(days[0]), "data_end": str(days[-1]), "seconds": round(time.time() - t0, 1),
        "rules": "每天收盘后按信号挂第二天的单（开盘买，一字涨停买不进；或回踩 2% 的限价单），按“一笔最多亏总资金的比例”和技术止损算股数，"
                 "单只不超过上限、总仓位不超过大盘环境上限；止损条件单、移动止盈、到期离场都按交易计划执行；扣佣金（最低 5 元）、印花税、过户费、滑点。"
                 "随机对照：同样的范围、排雷和买卖规则，只是每天随机挑股票。",
    }
    say(1.0, f"回测完成：{res['verdict']['text']}")
    return res
