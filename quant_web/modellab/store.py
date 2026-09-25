"""
实验室的文件（workspace/modellab/）：
- runs/{id}/result.json（配置、评估、因子重要性、最新打分）、oos.parquet（样本外预测）、model.*（最终模型）、latest_{日期}.json；
- state.json：启用的模型（选股器"模型打分"用它）、完成的试验次数（试得越多，越容易碰巧试出好看的结果）；
- factor_tests/{key}.json：单因子检验结果。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from .. import config

_lock = threading.RLock()
RUN_ID = re.compile(r"^\d{8}_\d{6}_[a-z_]+_[0-9a-f]{4}$")
KEEP_TRIAL_LOG: int = 300


def base_dir() -> Path:
    return config.WORKSPACE.joinpath("modellab")


def _atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def new_run_id(model: str) -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^a-z_]', '', model.lower())}_{secrets.token_hex(2)}"


def run_dir(run_id: str) -> Path:
    if not RUN_ID.match(str(run_id)):
        raise ValueError("训练记录编号不对")
    return base_dir().joinpath("runs", run_id)


# ---------------------------------------------------------------- 状态

def state() -> dict:
    try:
        data = json.loads(base_dir().joinpath("state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    _atomic_json(base_dir().joinpath("state.json"), st)


def enabled() -> str | None:
    rid = state().get("enabled")
    return rid if rid and run_dir(rid).joinpath("result.json").exists() else None


def enable(run_id: str) -> dict:
    run = get_run(run_id)
    with _lock:
        st = state()
        st["enabled"] = run_id
        st["enabled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _save_state(st)
    return summary(run)


def disable() -> None:
    with _lock:
        st = state()
        st["enabled"] = None
        _save_state(st)


def trials() -> dict:
    st = state()
    return {"count": int(st.get("trials", 0)), "log": list(st.get("trial_log", []))[-30:]}


# ---------------------------------------------------------------- 训练记录

def save_run(run_id: str, meta: dict, oos: pl.DataFrame, spec: Any, fitted: Any) -> None:
    d = run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    oos.write_parquet(d.joinpath("oos.parquet"))
    spec.adapter.save(fitted, d.joinpath("model"))
    _atomic_json(d.joinpath("result.json"), meta)
    with _lock:
        st = state()
        st["trials"] = int(st.get("trials", 0)) + 1
        log: list = list(st.get("trial_log", []))
        log.append({"id": run_id, "time": meta.get("created"), "describe": meta.get("describe"),
                    "credible": bool(((meta.get("evaluation") or {}).get("verdict") or {}).get("credible"))})
        st["trial_log"] = log[-KEEP_TRIAL_LOG:]
        _save_state(st)


def get_run(run_id: str) -> dict:
    path = run_dir(run_id).joinpath("result.json")
    if not path.exists():
        raise ValueError("没有这条训练记录（可能已被删除）")
    return json.loads(path.read_text(encoding="utf-8"))


def summary(run: dict) -> dict:
    ev: dict = run.get("evaluation") or {}
    seg: dict = ev.get("segments") or {}

    def pick(s: str) -> dict:
        x: dict = seg.get(s) or {}
        ric: dict = x.get("rank_ic") or {}
        tk: dict = x.get("topk") or {}
        return {"rank_ic": ric.get("mean"), "rank_ic_t": ric.get("t"), "icir": ric.get("ir"), "excess": tk.get("excess"),
                "t": tk.get("t"), "periods": tk.get("periods"), "cagr": tk.get("cagr"), "base_cagr": tk.get("base_cagr"),
                "max_dd": tk.get("max_dd"), "base_max_dd": tk.get("base_max_dd")}

    return {"id": run["id"], "created": run.get("created"), "describe": run.get("describe"), "config": run.get("config"),
            "model": run.get("model"), "verdict": ev.get("verdict"), "selection": pick("selection"), "holdout": pick("holdout"),
            "data": run.get("data"), "seconds": run.get("seconds"), "enabled": enabled() == run["id"]}


def list_runs() -> list[dict]:
    d = base_dir().joinpath("runs")
    out: list[dict] = []
    if not d.exists():
        return out
    for p in sorted(d.iterdir(), reverse=True):
        if not RUN_ID.match(p.name) or not p.joinpath("result.json").exists():
            continue
        try:
            out.append(summary(json.loads(p.joinpath("result.json").read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError):
            continue
    return out


def delete_run(run_id: str) -> None:
    d = run_dir(run_id)
    if not d.exists():
        raise ValueError("没有这条训练记录")
    with _lock:
        if state().get("enabled") == run_id:
            disable()
        shutil.rmtree(d)


def load_model(run_id: str) -> tuple[Any, Any, dict]:
    from . import models
    run = get_run(run_id)
    spec = models.get(run["model"]["key"])
    return spec, spec.adapter.load(run_dir(run_id).joinpath("model")), run


def oos(run_id: str) -> pl.DataFrame:
    return pl.read_parquet(run_dir(run_id).joinpath("oos.parquet"))


# ---------------------------------------------------------------- 启用的模型：给最新一天打分

def save_latest(run_id: str, day: str, rows: list[dict]) -> None:
    """某一天全部股票的打分（按日期缓存，只留最近 5 天）"""
    d = run_dir(run_id)
    _atomic_json(d.joinpath(f"latest_{day}.json"), {"run_id": run_id, "date": day, "rows": rows})
    for old in sorted(d.glob("latest_*.json"))[:-5]:
        old.unlink(missing_ok=True)


def cached_latest(run_id: str | None = None) -> dict | None:
    """不重算：最新一天的打分已经算好就返回，否则 None"""
    from ..market import history
    rid = run_id or enabled()
    if not rid:
        return None
    path = run_dir(rid).joinpath(f"latest_{history.last_date()}.json")
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        return None


def latest_scores(run_id: str | None = None, *, force: bool = False, base: tuple | None = None, progress=None) -> dict:
    """启用的模型对"本地数据最新一天"全部股票的打分（日线更新后第一次调用时用最终模型重算，按日期缓存）"""
    from ..market import history
    from . import dataset, train

    rid = run_id or enabled()
    if not rid:
        raise ValueError("还没有启用的模型：请先在模型实验室训练一个，并点“启用到选股器”")
    last: date | None = history.last_date()
    cache = run_dir(rid).joinpath(f"latest_{last}.json")
    if cache.exists() and not force:
        try:
            return {**json.loads(cache.read_text(encoding="utf-8")), "fresh": False}
        except (OSError, ValueError):
            pass
    spec, fitted, run = load_model(rid)
    data = dataset.build(run["config"], progress=progress, base=base, scoring_only=True)
    feats: list[str] = run["features"]
    rows = data.ds
    for f in feats:
        if f not in rows.columns:
            rows = rows.with_columns(pl.lit(None, dtype=pl.Float32).alias(f))
    scored = train.score_rows(spec, fitted, rows.select(["date", "code", *feats]), feats, run["task"])
    day = str(rows["date"].max()) if rows.height else str(last)
    save_latest(rid, day, scored)
    return {"run_id": rid, "date": day, "rows": scored, "fresh": True}


def scores_on(day: date) -> pl.DataFrame:
    """选股器当天选股用：code, model_score。最新一天要先打过分（日线更新后投资助手会自动打；也可以在实验室点“给最新一天打分”）"""
    from ..market import history
    rid = enabled()
    if not rid:
        raise ValueError("还没有启用的模型：请先在模型实验室训练一个，并点“启用到选股器”，或换一种打分方式")
    if day == history.last_date():
        lat = cached_latest(rid)
        if lat is None:
            raise ValueError("启用的模型还没给最新一天打分：请在模型实验室点“给最新一天打分”（几分钟），完成后再运行选股")
        return pl.DataFrame({"code": [r["code"] for r in lat["rows"]], "model_score": [float(r["score"]) for r in lat["rows"]]},
                            schema={"code": pl.Utf8, "model_score": pl.Float64})
    hist = oos(rid).filter(pl.col("date") == day).select(pl.col("code"), pl.col("pred").alias("model_score"))
    if hist.is_empty():
        raise ValueError(f"启用的模型在 {day} 没有样本外打分")
    return hist


def model_scores(dates: list[date] | None = None, include_latest: bool = True) -> pl.DataFrame:
    """选股器用：启用的模型在指定日期的分数（历史日期用滚动训练的样本外预测，最新一天用最终模型）。列 date, code, model_score"""
    rid = enabled()
    if not rid:
        raise ValueError("还没有启用的模型：请先在模型实验室训练一个，并点“启用到选股器”")
    frames: list[pl.DataFrame] = []
    hist = oos(rid).select(pl.col("date"), pl.col("code"), pl.col("pred").alias("model_score"))
    if dates is not None:
        hist = hist.filter(pl.col("date").is_in(dates))
    frames.append(hist)
    if not include_latest:
        return hist
    lat = latest_scores(rid)
    if lat.get("rows") and lat.get("date"):
        d = date.fromisoformat(lat["date"])
        if dates is None or d in dates:
            frames.append(pl.DataFrame({"date": [d] * len(lat["rows"]), "code": [r["code"] for r in lat["rows"]],
                                        "model_score": [float(r["score"]) for r in lat["rows"]]},
                                       schema={"date": pl.Date, "code": pl.Utf8, "model_score": pl.Float64}))
    out = pl.concat(frames, how="vertical_relaxed")
    return out.unique(["date", "code"], keep="last")
