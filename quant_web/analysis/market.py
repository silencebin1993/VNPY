"""
大盘数据的本地缓存：全市场每日宽度表（market_daily.parquet）和最近一次的大盘环境结果（regime.json）。
日线面板变化（文件签名变了）才重新计算，平时直接读文件，首页打开很快。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import polars as pl

from .. import config
from ..formula import engine
from . import regime, sectors

_lock = threading.Lock()
INDEX_CODE: str = "sh000001"
INDEX_NAME: str = "上证指数"


def _dir() -> Path:
    return config.WORKSPACE.joinpath("analysis")


def _panel_stamp() -> str:
    items: list[str] = []
    try:
        for p in sorted(config.PANEL_DIR.glob("*.parquet")):
            st = p.stat()
            items.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
    except OSError:
        pass
    return "|".join(items)


def daily_table(force: bool = False) -> pl.DataFrame:
    """全市场每日宽度表；面板没变时读缓存文件"""
    path: Path = _dir().joinpath("market_daily.parquet")
    stamp_path: Path = _dir().joinpath("market_daily.stamp")
    stamp: str = _panel_stamp()
    with _lock:
        if not force and path.exists() and stamp_path.exists() and stamp_path.read_text(encoding="utf-8") == stamp:
            return pl.read_parquet(path)
        from ..market import history

        raw: pl.DataFrame = history.load_panel(columns=["open", "high", "low", "close", "preclose", "volume", "amount",
                                                        "tradestatus"])
        if raw.is_empty():
            raise ValueError("本地还没有日线数据，请先点“一键更新”")
        frame: pl.DataFrame = engine.prepare_frame(raw)
        del raw
        daily: pl.DataFrame = regime.market_table(frame)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path = path.with_name(path.name + f".{os.getpid()}.tmp")
        daily.write_parquet(tmp)
        os.replace(tmp, path)
        stamp_path.write_text(stamp, encoding="utf-8")
        return daily


def index_bars(code: str = INDEX_CODE) -> pl.DataFrame | None:
    try:
        from ..providers import store
        df: pl.DataFrame = store.load("index_bars")
    except Exception:  # noqa: BLE001
        return None
    if df.is_empty() or "index" not in df.columns:
        return None
    sub = df.filter(pl.col("index") == code).sort("date")
    return sub if sub.height >= 60 else None


def current_regime(force: bool = False) -> dict:
    """最新的大盘环境（面板变了才重算宽度表；结果另存 regime.json 方便首页秒开）"""
    from .. import settings as settings_mod

    caps: dict = dict(settings_mod.load().risk.regime_caps)
    daily: pl.DataFrame = daily_table(force)
    idx: pl.DataFrame | None = index_bars()
    res: dict = regime.summarize(regime.score_table(daily, idx), caps, INDEX_NAME if idx is not None else None)
    path: Path = _dir().joinpath("regime.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res, ensure_ascii=False, default=str), encoding="utf-8")
    return res


def last_regime() -> dict | None:
    """上次保存的大盘环境（不重新计算）"""
    try:
        return json.loads(_dir().joinpath("regime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def current_sectors() -> dict:
    from ..market import history, universe

    last = history.last_date()
    if last is None:
        raise ValueError("本地还没有日线数据")
    raw: pl.DataFrame = history.load_panel(start=last - timedelta(days=130),
                                           columns=["open", "high", "low", "close", "preclose", "volume", "amount", "tradestatus"])
    frame: pl.DataFrame = engine.prepare_frame(raw)
    return sectors.compute(frame, universe.load_universe())
