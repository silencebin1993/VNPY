"""
主力阶段的历史验证：对全市场历史逐日判断阶段（只用当天已知的数据），统计"处在某个阶段的股票"之后 5/10/20 天
按真实规则交易（次日开盘买、持有 N 天收盘卖、扣成本）的平均表现，和同一天所有股票的平均比。

- 按日汇总：每天处在某阶段的股票的平均超额 → 日序列 → t = min(按日 t, Newey-West t(滞后 N))；
- 分段：2025-07-01 之前（选择期）/ 之后（留出期，样本外）；
- 为控制内存，按股票分批计算特征和阶段（RPS 的截面排名先对全市场算好再并进来）；
- 结果保存在 workspace/analysis/stage_stats.json，页面上每个阶段旁边显示"历史上出现后平均跑赢/跑输多少、是否可信"。
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import polars as pl

from .. import config
from ..formula import engine, validate
from ..predict import costs as costs_mod
from ..predict import stats
from ..predict.backtest import HOLDOUT_START
from . import features, stage

HOLDS: tuple[int, ...] = (5, 10, 20)
CHUNK_CODES: int = 700
BOARDS: tuple[str, ...] = ("main", "chinext", "star")


def result_path() -> Path:
    return config.WORKSPACE.joinpath("analysis", "stage_stats.json")


def load() -> dict | None:
    try:
        return json.loads(result_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(res: dict) -> None:
    path: Path = result_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def rps_table(frame: pl.DataFrame) -> pl.DataFrame:
    """全市场每天的 RPS120（120 日涨幅的截面百分位）"""
    df = frame.select("code", "date", "close").with_columns(
        (pl.col("close") / pl.col("close").shift(120).over("code") - 1).alias("ret120"))
    return df.with_columns(
        (pl.col("ret120").rank("average").over("date") / pl.col("ret120").count().over("date") * 100).alias("rps120")
    ).select("code", "date", "rps120")


def classify_frame(frame: pl.DataFrame, raw: pl.DataFrame | None, use_chips: bool,
                   progress: Callable[[float, str], None] | None = None) -> pl.DataFrame:
    """分批（按股票）计算特征和阶段，返回 code, date, stage, stage_score, score_*（以及交易需要的列）"""
    say = progress or (lambda f, m: None)
    rps: pl.DataFrame = rps_table(frame)
    codes: list[str] = frame["code"].unique(maintain_order=True).to_list()
    keep: list[str] = ["code", "date", "open", "close", "raw_open", "raw_close", "preclose", "is_st", "amount",
                       "stage", "stage_score", *[f"score_{s}" for s in stage.PRIORITY]]
    parts: list[pl.DataFrame] = []
    for i in range(0, len(codes), CHUNK_CODES):
        batch: list[str] = codes[i:i + CHUNK_CODES]
        sub: pl.DataFrame = frame.filter(pl.col("code").is_in(batch))
        chips = None
        if use_chips and raw is not None:
            chips = engine.chips_for_frame(sub, raw.filter(pl.col("code").is_in(batch)))
        feat: pl.DataFrame = features.compute(sub, chips, rps=False).drop("rps120").join(rps, on=["code", "date"], how="left")
        cl: pl.DataFrame = stage.classify(feat)
        parts.append(cl.select([c for c in keep if c in cl.columns]))
        say(min(0.95, (i + len(batch)) / len(codes)), f"主力阶段：已判断 {min(i + len(batch), len(codes))}/{len(codes)} 只股票")
    return pl.concat(parts).sort(["code", "date"])


def summarize(cls: pl.DataFrame, holds: tuple[int, ...] = HOLDS, boards: tuple[str, ...] = BOARDS,
              cost: costs_mod.Costs | None = None) -> dict:
    """各阶段之后的表现（按日汇总超额）"""
    cost = cost or costs_mod.DEFAULT
    df: pl.DataFrame = validate.add_trade_columns(cls)
    df = df.with_columns(validate.eligible_expr(boards, True, 0.0).alias("eligible"))
    out: dict = {}
    counts: dict = {r["stage"]: r["len"] for r in df.filter(pl.col("eligible")).group_by("stage").len().to_dicts()}
    for h in holds:
        fr: pl.DataFrame = validate.forward_returns(df.select(["code", "date", "open", "close", "raw_open", "raw_close",
                                                               "limit_up", "limit_down", "eligible", "stage"]), h, cost)
        fr = fr.filter(pl.col("eligible") & pl.col("net").is_not_null())
        base: pl.DataFrame = fr.group_by("date").agg(pl.col("net").mean().alias("base"))
        fr = fr.join(base, on="date", how="left").with_columns((pl.col("net") - pl.col("base")).alias("excess"))
        per: dict = {}
        for st in list(stage.STAGES):
            sub: pl.DataFrame = fr.filter(pl.col("stage") == st)
            per[st] = {seg: _seg_stats(part, h) for seg, part in (
                ("all", sub), ("selection", sub.filter(pl.col("date") < HOLDOUT_START)),
                ("holdout", sub.filter(pl.col("date") >= HOLDOUT_START)))}
        out[str(h)] = per
    return {"holds": out, "counts": counts, "boards": list(boards)}


def _seg_stats(sub: pl.DataFrame, hold: int) -> dict:
    if sub.height == 0:
        return {"n": 0}
    daily: pl.DataFrame = sub.group_by("date").agg(pl.col("excess").mean(), pl.col("net").mean()).sort("date")
    x = daily["excess"].to_numpy()
    dt, nwt = stats.daily_t(x), stats.nw_t(x, lag=max(hold, 1))
    t = min(dt, nwt) if dt is not None and nwt is not None else (dt if nwt is None else nwt)
    # 超额用"按天平均"（每天处在该阶段的股票平均，再对天平均），和 t 值用的是同一个序列，正负号不会对不上
    return {"n": sub.height, "days": daily.height, "mean": float(daily["net"].mean()), "excess": float(daily["excess"].mean()),
            "excess_pooled": float(sub["excess"].mean()), "win": float((sub["net"] > 0).mean()), "t": stats.rounded(t)}


def run(use_chips: bool = True, progress: Callable[[float, str], None] | None = None) -> dict:
    """后台任务：读全部日线 → 逐日判断阶段 → 统计 → 保存"""
    from ..market import history

    say = progress or (lambda f, m: None)
    say(0.01, "正在读取本地日线……")
    raw: pl.DataFrame = history.load_panel(columns=engine.FRAME_COLS)
    if raw.is_empty():
        raise ValueError("本地还没有日线数据，请先点“一键更新”")
    frame: pl.DataFrame = engine.prepare_frame(raw)
    cls: pl.DataFrame = classify_frame(frame, raw if use_chips else None, use_chips,
                                       progress=lambda f, m: say(0.02 + 0.78 * f, m))
    del frame
    say(0.82, "正在统计各阶段之后的表现……")
    res: dict = summarize(cls)
    res.update({"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "use_chips": use_chips,
                "data_start": str(cls["date"].min()), "data_end": str(cls["date"].max()),
                "holdout_start": str(HOLDOUT_START), "threshold": stage.THRESHOLD})
    _save(res)
    say(1.0, "主力阶段历史验证完成")
    return {"generated_at": res["generated_at"], "counts": res["counts"]}


def verdict(st: str, res: dict | None, hold: str = "10") -> dict | None:
    """某个阶段的一句话历史结论（以留出期为准），没有验证结果时返回 None"""
    if not res or hold not in res.get("holds", {}) or st not in res["holds"][hold]:
        return None
    seg: dict = res["holds"][hold][st]
    ho: dict = seg.get("holdout") or {}
    al: dict = seg.get("all") or {}
    if not al.get("n"):
        return {"text": "历史上几乎没出现过，无法统计", "credible": False, "hold": hold}
    ex: float = ho.get("excess") if ho.get("n") else al.get("excess")
    t = ho.get("t") if ho.get("n") else al.get("t")
    credible: bool = t is not None and abs(t) >= 2
    word: str = "跑赢" if ex >= 0 else "跑输"
    return {"hold": hold, "credible": credible, "excess": ex, "t": t, "n": al.get("n"),
            "text": f"历史上处在这个阶段的股票，之后 {hold} 天平均比同一天的所有股票{word} {abs(ex) * 100:.2f}%"
                    f"（{'留出期' if ho.get('n') else '全部样本'}，t 值 {t if t is not None else '—'}，"
                    f"{'统计上可信' if credible else '还不够可信，可能只是运气'}）"}
