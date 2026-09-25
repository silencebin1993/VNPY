"""
逐日估值（市盈率TTM、市净率）：历史用 baostock（stock_lab/bs_hist，官方口径、按总股本），
之后每个交易日收盘后用腾讯全市场快照补一行（字段 39 = 市盈率TTM、46 = 市净率，已逐只核对与 baostock 一致），
存到 stock_lab/valuation_tx.parquet。某天没打开程序就缺那一天，打分时向前沿用最多 5 个交易日。
"""
from __future__ import annotations

import logging
from datetime import date

import polars as pl

from .. import config, net
from ..market import universe as uni_mod

log = logging.getLogger("quant_web.multifactor")
TX_FILE = config.STOCK_LAB.joinpath("valuation_tx.parquet")
QUOTE_URL = "https://qt.gtimg.cn/q="
BATCH = 500
SCHEMA = {"date": pl.Date, "code": pl.Utf8, "pe_ttm": pl.Float64, "pb": pl.Float64, "total_cap": pl.Float64,
          "float_cap": pl.Float64}


def _f(fields: list[str], i: int) -> float | None:
    try:
        v = float(fields[i])
    except (IndexError, ValueError):
        return None
    return v


def parse_line(line: str) -> dict | None:
    if "~" not in line or '="' not in line:
        return None
    f = line.split('="', 1)[1].rstrip('";\n ').split("~")
    if len(f) < 47 or not f[2] or len(f[30]) < 8:
        return None
    t = f[30]
    day = date(int(t[:4]), int(t[4:6]), int(t[6:8]))
    tot, flt = _f(f, 45), _f(f, 44)
    return {"date": day, "code": f[2], "pe_ttm": _f(f, 39), "pb": _f(f, 46),
            "total_cap": tot * 1e8 if tot is not None else None, "float_cap": flt * 1e8 if flt is not None else None}


def fetch_snapshot(codes: list[str]) -> pl.DataFrame:
    rows: list[dict] = []
    syms = []
    for c in codes:
        try:
            syms.append(uni_mod.market_symbol(c))
        except ValueError:
            continue
    for i in range(0, len(syms), BATCH):
        text = net.get_text(QUOTE_URL + ",".join(syms[i:i + BATCH]), encoding="gbk", timeout=15)
        for line in text.split(";"):
            r = parse_line(line.strip())
            if r:
                rows.append(r)
    return pl.DataFrame(rows, schema=SCHEMA, orient="row") if rows else pl.DataFrame(schema=SCHEMA)


def load_tx() -> pl.DataFrame:
    if not TX_FILE.exists():
        return pl.DataFrame(schema=SCHEMA)
    return pl.read_parquet(TX_FILE)


def refresh(day: date, codes: list[str]) -> dict:
    """收盘后调用：抓快照，只保留快照日期 == day 的行（停牌股报价日期是旧的，不要）"""
    snap = fetch_snapshot(codes)
    snap = snap.filter(pl.col("date") == day)
    if snap.is_empty():
        return {"rows": 0, "note": f"快照里没有 {day} 的估值（可能还没收盘或当天休市）"}
    old = load_tx()
    merged = pl.concat([old.filter(pl.col("date") != day), snap], how="vertical_relaxed").sort(["date", "code"])
    TX_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TX_FILE.with_suffix(".tmp")
    merged.write_parquet(tmp)
    tmp.replace(TX_FILE)
    return {"rows": snap.height, "date": str(day)}
