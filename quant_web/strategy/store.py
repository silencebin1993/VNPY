"""
策略中心的文件（workspace/strategy/）：strategies.json（我的策略：模板 + 参数 + 是否模拟跟踪 / 实盘建议）、
backtests/{key}.json（回测结果）、latest/{策略编号}.json（最近一天给出的实盘建议）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config

_lock = threading.RLock()
ITEM_ID = re.compile(r"^st_[0-9a-f]{8}$")


def base_dir() -> Path:
    return config.WORKSPACE.joinpath("strategy")


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def list_items() -> list[dict]:
    data = _read(base_dir().joinpath("strategies.json"), [])
    return [x for x in data if isinstance(x, dict) and ITEM_ID.match(str(x.get("id", "")))] if isinstance(data, list) else []


def get(item_id: str) -> dict:
    for x in list_items():
        if x["id"] == item_id:
            return x
    raise ValueError("没有这个策略（可能已被删除）")


def save(item: dict) -> dict:
    """新建（没有 id）或更新；模板和参数先校验"""
    from . import templates
    spec = templates.resolve(item["template"], item.get("params"))
    with _lock:
        items = list_items()
        old = next((x for x in items if x["id"] == item.get("id")), None)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        merged: dict = {
            "id": old["id"] if old else f"st_{secrets.token_hex(4)}", "template": spec["template"],
            "params": {k: spec[k] for k in templates.TEMPLATES[spec["template"]]["defaults"]},
            "name": str(item.get("name") or (old or {}).get("name") or spec["name"])[:40],
            "created": (old or {}).get("created") or now, "updated": now,
            "follow": (old or {}).get("follow") or {"enabled": False, "account_id": None},
            "live": bool((old or {}).get("live", False)), "backtest_key": (old or {}).get("backtest_key"),
        }
        items = [x for x in items if x["id"] != merged["id"]] + [merged]
        _write(base_dir().joinpath("strategies.json"), items)
    return merged


def update(item_id: str, **fields: Any) -> dict:
    with _lock:
        items = list_items()
        for x in items:
            if x["id"] == item_id:
                x.update(fields)
                x["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                _write(base_dir().joinpath("strategies.json"), items)
                return x
    raise ValueError("没有这个策略")


def delete(item_id: str) -> None:
    with _lock:
        items = list_items()
        if not any(x["id"] == item_id for x in items):
            raise ValueError("没有这个策略")
        _write(base_dir().joinpath("strategies.json"), [x for x in items if x["id"] != item_id])
        base_dir().joinpath("latest", f"{item_id}.json").unlink(missing_ok=True)


def save_backtest(res: dict) -> None:
    _write(base_dir().joinpath("backtests", f"{res['key']}.json"), {**res, "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M")})


def load_backtest(key: str | None) -> dict | None:
    if not key or not re.fullmatch(r"[0-9a-f]{16}", key):
        return None
    return _read(base_dir().joinpath("backtests", f"{key}.json"), None)


def save_latest(item_id: str, data: dict) -> None:
    _write(base_dir().joinpath("latest", f"{item_id}.json"), data)


def load_latest(item_id: str) -> dict | None:
    if not ITEM_ID.match(item_id):
        return None
    return _read(base_dir().joinpath("latest", f"{item_id}.json"), None)
