"""
单因子检验（不训练模型）：每个因子本身和"之后的真实净收益"有没有关系。
每天算因子和之后净收益的排名相关系数（RankIC），看平均、稳定性（ICIR）、t 值，分选择期 / 留出期，
并看两段的方向是否一致（方向反了的因子，多半是碰巧）。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable

import numpy as np
import polars as pl

from ..predict.backtest import HOLDOUT_START
from . import dataset as D
from . import evaluate as E
from . import factors as F
from . import options as O
from . import store

Progress = Callable[[float, str], None]


def key_of(cfg: dict) -> str:
    sub = {k: cfg[k] for k in ("universe", "factor_sets", "label", "start_year", "end", "min_amount")}
    return hashlib.sha1(json.dumps(sub, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _stats(x: np.ndarray, hold: int) -> dict:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return {"mean": None, "ir": None, "t": None, "days": int(len(x))}
    sd = float(np.std(x, ddof=1))
    return {"mean": float(np.mean(x)), "ir": float(np.mean(x) / sd) if sd else None, "t": E._t(x, hold)["t"], "days": int(len(x))}


def run(cfg: dict, progress: Progress | None = None, *, base: tuple | None = None) -> dict:
    say: Progress = progress or (lambda f, m: None)
    t0: float = time.time()
    cfg = O.validate_config({**cfg, "model": "lightgbm", "normalize": "none"})
    hold: int = O.hold_of(cfg)
    data = D.build(cfg, progress=lambda f, m: say(0.9 * f, m), base=base)
    ds = data.ds.filter(pl.col("net").is_not_null())
    say(0.92, f"逐个因子计算每天的排名相关系数（{len(data.features)} 个）……")
    daily = (ds.group_by("date").agg([pl.corr(f, "net", method="spearman").alias(f) for f in data.features] + [pl.len().alias("_n")])
             .filter(pl.col("_n") >= E.MIN_PER_DAY).sort("date"))
    sel = daily.filter(pl.col("date") < HOLDOUT_START)
    ho = daily.filter(pl.col("date") >= HOLDOUT_START)
    rows: list[dict] = []
    for f in data.features:
        a = _stats(daily[f].to_numpy().astype(np.float64), hold)
        s = _stats(sel[f].to_numpy().astype(np.float64), hold)
        h = _stats(ho[f].to_numpy().astype(np.float64), hold)
        consistent = s["mean"] is not None and h["mean"] is not None and np.sign(s["mean"]) == np.sign(h["mean"])
        cov = float(1 - ds[f].null_count() / max(ds.height, 1))
        credible = bool(consistent and h["t"] is not None and abs(h["t"]) >= 2 and s["t"] is not None and abs(s["t"]) >= 2)
        rows.append({"name": f, "label": F.feature_label(f), "set": F.set_of(f), "all": a, "selection": s, "holdout": h,
                     "consistent": bool(consistent), "credible": credible, "coverage": cov})
    rows.sort(key=lambda r: -abs(r["holdout"]["t"] or 0))
    res: dict = {
        "key": key_of(cfg), "config": cfg, "describe": O.describe(cfg), "hold": hold, "rows": rows,
        "data": data.info, "holdout_start": str(HOLDOUT_START), "seconds": round(time.time() - t0, 1),
        "note": "RankIC 为正：因子越大，之后越容易涨；为负：越大越容易跌（反过来用也行）。两段方向一致、t 值都超过 2 才算比较可信；"
                "单个因子的 RankIC 通常只有 0.02~0.05，看起来小但长期有用。",
    }
    store._atomic_json(store.base_dir().joinpath("factor_tests", f"{res['key']}.json"), res)
    say(1.0, "单因子检验完成")
    return {"key": res["key"], "n": len(rows), "credible": sum(1 for r in rows if r["credible"])}


def load(key: str) -> dict | None:
    if not key.isalnum() or len(key) > 32:
        raise ValueError("检验编号不对")
    path = store.base_dir().joinpath("factor_tests", f"{key}.json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
