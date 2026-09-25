"""
选股方案与结果的本地保存：
- workspace/screener/schemes.json：我的方案（内置方案在 schemes.PRESETS，不保存）；
- workspace/screener/results/{方案id}/{日期}.json：每天自动运行的结果（每个方案保留最近 60 天）；
- workspace/screener/backtests/{key}.json：回测结果（key = 方案内容 + 回测参数的哈希）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .. import config
from . import schemes as S

_lock = threading.Lock()
KEEP_RESULTS: int = 60


def base() -> Path:
    return config.WORKSPACE.joinpath("screener")


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def list_mine() -> list[dict]:
    try:
        data = json.loads(base().joinpath("schemes.json").read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict) and x.get("id")] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def all_schemes() -> list[dict]:
    return [dict(p) for p in S.PRESETS] + list_mine()


def get(scheme_id: str) -> dict:
    if scheme_id in S.PRESET_BY_ID:
        return dict(S.PRESET_BY_ID[scheme_id])
    item = next((x for x in list_mine() if x["id"] == scheme_id), None)
    if item is None:
        raise ValueError(f"没有找到选股方案「{scheme_id}」")
    return item


def save_mine(scheme: dict) -> dict:
    s: dict = S.validate_scheme(scheme)
    with _lock:
        items: list[dict] = list_mine()
        sid: str = str(scheme.get("id") or "")
        if sid in S.PRESET_BY_ID or not sid.startswith("my_"):
            sid = ""
        old = next((x for x in items if x["id"] == sid), None) if sid else None
        now: str = datetime.now().strftime("%Y-%m-%d %H:%M")
        if old is None:
            s["id"] = "my_" + uuid.uuid4().hex[:10]
            s["created"] = now
            items.append(s)
        else:
            s["id"] = sid
            s["created"] = old.get("created", now)
            items[items.index(old)] = s
        s["updated"] = now
        s["builtin"] = False
        _write(base().joinpath("schemes.json"), items)
        return s


def delete_mine(scheme_id: str) -> bool:
    with _lock:
        items = list_mine()
        left = [x for x in items if x["id"] != scheme_id]
        if len(left) == len(items):
            return False
        _write(base().joinpath("schemes.json"), left)
        return True


def save_result(scheme_id: str, result: dict) -> None:
    d: Path = base().joinpath("results", scheme_id)
    _write(d.joinpath(f"{result['date']}.json"), {**result, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
    files = sorted(d.glob("*.json"))
    for f in files[:-KEEP_RESULTS]:
        try:
            f.unlink()
        except OSError:
            pass


def latest_result(scheme_id: str) -> dict | None:
    d: Path = base().joinpath("results", scheme_id)
    files = sorted(d.glob("*.json")) if d.exists() else []
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def backtest_key(scheme: dict, params: dict) -> str:
    core = {k: scheme.get(k) for k in ("universe", "conditions", "match", "risk", "scoring", "top_n")}
    raw = json.dumps({"s": core, "p": params}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def save_backtest(key: str, result: dict) -> None:
    _write(base().joinpath("backtests", f"{key}.json"), {**result, "key": key, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M")})


def load_backtest(key: str) -> dict | None:
    if not key or not all(c in "0123456789abcdef" for c in key):
        return None
    try:
        return json.loads(base().joinpath("backtests", f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
