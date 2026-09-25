"""
扩展数据的本地存档：stock_lab/ext/{名字}.parquet（资金流、融资融券、股东户数、解禁、质押、业绩预告、增减持、
指数日线、指数成分）。

- save(name, df, keys) 与已有文件合并：按 keys 去重，新数据覆盖旧数据；先写临时文件再替换（断电不留半个文件）；
- load(name) 读全部（没有文件返回空表）；info() 给数据页面看每个存档的行数、日期范围、更新时间。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

import polars as pl

from .. import config


_lock = threading.Lock()

# 每个存档的去重主键和"日期列"（用于显示日期范围）
ARCHIVES: dict[str, dict] = {
    "fund_flow": {"keys": ["code", "date"], "date": "date", "label": "个股资金流向"},
    "margin": {"keys": ["code", "date"], "date": "date", "label": "融资融券"},
    "holder_count": {"keys": ["code", "end_date"], "date": "end_date", "label": "股东户数"},
    "unlock": {"keys": ["code", "date", "kind"], "date": "date", "label": "限售解禁"},
    "pledge": {"keys": ["code", "date"], "date": "date", "label": "股权质押"},
    "forecast": {"keys": ["code", "period", "notice_date"], "date": "notice_date", "label": "业绩预告"},
    "holder_trades": {"keys": ["code", "notice_date", "holder", "direction", "shares"], "date": "notice_date",
                      "label": "股东增减持"},
    "index_bars": {"keys": ["index", "date"], "date": "date", "label": "指数日线"},
    "index_members": {"keys": ["index", "code"], "date": None, "label": "指数成分股"},
}


def ext_dir() -> Path:
    return config.STOCK_LAB.joinpath("ext")


def path_of(name: str) -> Path:
    if name not in ARCHIVES:
        raise ValueError(f"没有「{name}」这个扩展数据存档")
    return ext_dir().joinpath(f"{name}.parquet")


def load(name: str) -> pl.DataFrame:
    path: Path = path_of(name)
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception:  # noqa: BLE001  文件损坏时当作没有（下次保存会重写）
        return pl.DataFrame()


def save(name: str, df: pl.DataFrame, *, replace_where: pl.Expr | None = None) -> int:
    """合并保存，返回保存后的总行数。replace_where：先删掉旧数据中满足条件的行（比如"这个指数的成分"整体替换）"""
    if df is None or df.height == 0:
        return load(name).height
    spec: dict = ARCHIVES[name]
    keys: list[str] = [k for k in spec["keys"] if k in df.columns]
    path: Path = path_of(name)
    with _lock:
        old: pl.DataFrame = load(name)
        if old.height and replace_where is not None:
            old = old.filter(~replace_where)
        if old.height:
            common: list[str] = [c for c in df.columns if c in old.columns]
            # 新旧列不一致时以新数据的列为准（旧存档里多的列丢掉，少的列补空）
            old = old.select([pl.col(c).cast(df.schema[c], strict=False) if c in common
                              else pl.lit(None, dtype=df.schema[c]).alias(c) for c in df.columns])
            merged: pl.DataFrame = pl.concat([old, df], how="vertical_relaxed")
        else:
            merged = df
        if keys:
            merged = merged.unique(subset=keys, keep="last", maintain_order=True)
        date_col: str | None = spec.get("date")
        sort_cols: list[str] = [c for c in ([date_col] if date_col else []) + keys if c and c in merged.columns]
        if sort_cols:
            merged = merged.sort(list(dict.fromkeys(sort_cols)))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        merged.write_parquet(tmp)
        os.replace(tmp, path)
        return merged.height


def info() -> list[dict]:
    """每个存档：行数、股票数、日期范围、文件更新时间"""
    out: list[dict] = []
    for name, spec in ARCHIVES.items():
        path: Path = path_of(name)
        item: dict = {"name": name, "label": spec["label"], "exists": path.exists(), "rows": 0, "codes": 0,
                      "start": None, "end": None, "updated": None, "size": 0}
        if path.exists():
            st = path.stat()
            item["updated"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            item["size"] = st.st_size
            df: pl.DataFrame = load(name)
            item["rows"] = df.height
            if "code" in df.columns:
                item["codes"] = df.get_column("code").n_unique()
            date_col: str | None = spec.get("date")
            if date_col and date_col in df.columns and df.height:
                col = df.get_column(date_col).drop_nulls()
                if col.len():
                    item["start"], item["end"] = col.min(), col.max()
        out.append(item)
    return out
