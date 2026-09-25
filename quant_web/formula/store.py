"""
我的公式与验证结果的本地保存：
- workspace/formula/mine.json：用户自己写的公式 [{id, name, text, note, created, updated}]；保存前必须通过语法检查；
- workspace/formula/results/{key}.json：验证结果（key = 公式文本 + 参数的哈希；公式或参数一变就是新结果）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .. import config
from .parser import compile_formula

_lock = threading.Lock()
MAX_MINE: int = 200


def base_dir() -> Path:
    return config.WORKSPACE.joinpath("formula")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def list_mine() -> list[dict]:
    path: Path = base_dir().joinpath("mine.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict) and x.get("id") and x.get("text")] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_mine(item: dict) -> dict:
    name: str = str(item.get("name") or "").strip()[:40]
    text: str = str(item.get("text") or "").strip()
    note: str = str(item.get("note") or "").strip()[:300]
    if not name:
        raise ValueError("请给公式起个名字")
    compile_formula(text)                       # 语法不对直接报错（FormulaError 是 ValueError）
    now: str = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _lock:
        items: list[dict] = list_mine()
        fid: str = str(item.get("id") or "")
        old: dict | None = next((x for x in items if x["id"] == fid), None) if fid else None
        if old is None:
            if len(items) >= MAX_MINE:
                raise ValueError(f"最多保存 {MAX_MINE} 个公式，请先删掉一些")
            old = {"id": "my_" + uuid.uuid4().hex[:10], "created": now}
            items.append(old)
        old.update({"name": name, "text": text, "note": note, "updated": now})
        _atomic_write(base_dir().joinpath("mine.json"), json.dumps(items, ensure_ascii=False, indent=2))
        return dict(old)


def delete_mine(fid: str) -> bool:
    with _lock:
        items: list[dict] = list_mine()
        left: list[dict] = [x for x in items if x["id"] != fid]
        if len(left) == len(items):
            return False
        _atomic_write(base_dir().joinpath("mine.json"), json.dumps(left, ensure_ascii=False, indent=2))
        return True


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "").upper()


def result_key(text: str, params: dict) -> str:
    raw: str = normalize(text) + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def save_result(key: str, result: dict) -> None:
    result = {**result, "key": key, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "saved_ts": time.time()}
    _atomic_write(base_dir().joinpath("results", f"{key}.json"), json.dumps(result, ensure_ascii=False, default=str))


def load_result(key: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{16}", key or ""):
        return None
    try:
        return json.loads(base_dir().joinpath("results", f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
