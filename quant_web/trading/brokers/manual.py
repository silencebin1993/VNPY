"""
实盘-手动：程序不碰你的钱。委托只生成"待你下单"的单子，你在券商 App 里下完单后：
- 回到这里点“已成交”，填成交价和数量（部分成交也可以）；或者
- 导入券商导出的交割单/成交记录（CSV），自动对上委托并记账。
条件单（止损）不会自动执行：每晚的“明日计划”里会列出需要在券商 App 设置的条件单价格。
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date, datetime

from .. import ledger
from ..engine import OrderRequest, apply_fill, place
from .base import Broker


class ManualBroker(Broker):
    name = "manual"
    label = "实盘-手动下单"
    description = "程序给出下单单子，你在自己的券商 App 下单，成交后回来确认或导入交割单。任何券商都能用。"
    is_live = True
    can_auto = False

    def place(self, conn: sqlite3.Connection, req: OrderRequest, ref_price: float | None = None) -> dict:
        status = "waiting_trigger" if req.kind in ("stop", "take_profit") else "pending_manual"
        return place(conn, self.account["id"], req, ref_price=ref_price, status=status)

    def confirm_fill(self, conn: sqlite3.Connection, order_id: str, qty: int, price: float, day: date | None = None) -> dict:
        o = ledger.one(conn, "SELECT * FROM orders WHERE id=? AND account_id=?", (order_id, self.account["id"]))
        if o is None:
            raise ValueError("没有这笔委托")
        if o["status"] not in ("pending_manual", "partial", "submitted", "waiting_trigger"):
            raise ValueError("这笔委托已经结束")
        left: int = o["qty"] - o["filled_qty"]
        if not 0 < qty <= left:
            raise ValueError(f"成交数量要在 1 到 {left} 之间")
        if price <= 0:
            raise ValueError("成交价要大于 0")
        return apply_fill(conn, order_id, self.account["id"], o["code"], o["side"], int(qty), float(price), day or ledger.today(),
                          o.get("name"), "manual", o.get("plan_id"))


# ---------------------------------------------------------------- 交割单导入

COLS: dict[str, tuple[str, ...]] = {
    "date": ("成交日期", "交割日期", "发生日期", "日期", "成交时间"),
    "code": ("证券代码", "股票代码", "代码"),
    "name": ("证券名称", "股票名称", "名称"),
    "side": ("操作", "买卖标志", "买卖方向", "业务名称", "摘要", "委托类别", "交易类别"),
    "qty": ("成交数量", "成交股数", "数量", "发生数量"),
    "price": ("成交均价", "成交价格", "成交价", "价格"),
    "trade_id": ("成交编号", "合同编号", "委托编号", "流水号"),
}


def _pick(header: list[str], keys: tuple[str, ...]) -> int | None:
    for k in keys:
        for i, h in enumerate(header):
            if h.strip().replace("﻿", "") == k:
                return i
    return None


def _side(text: str) -> str | None:
    t = text.strip()
    if any(w in t for w in ("买", "证券买入", "买入")) and "卖" not in t:
        return "buy"
    if any(w in t for w in ("卖", "证券卖出")):
        return "sell"
    return None


def _date(text: str) -> date | None:
    t = re.sub(r"[^0-9]", "", text.strip())[:8]
    try:
        return datetime.strptime(t, "%Y%m%d").date()
    except ValueError:
        return None


def parse_statement(text: str) -> list[dict]:
    """解析同花顺/通达信/东方财富等导出的成交记录（CSV 或制表符分隔），只保留股票的买入/卖出"""
    text = text.lstrip("﻿")
    sample: str = text[:2000]
    delim: str = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = list(csv.reader(io.StringIO(text), delimiter=delim))
    header_idx: int | None = next((i for i, r in enumerate(reader[:10]) if any("代码" in c for c in r)), None)
    if header_idx is None:
        raise ValueError("没有找到表头：请导出包含“证券代码、成交数量、成交价格”的成交记录/交割单")
    header: list[str] = [c.strip() for c in reader[header_idx]]
    idx = {k: _pick(header, v) for k, v in COLS.items()}
    missing = [k for k in ("code", "side", "qty", "price") if idx[k] is None]
    if missing:
        names = {"code": "证券代码", "side": "买卖方向", "qty": "成交数量", "price": "成交价格"}
        raise ValueError("交割单缺少这些列：" + "、".join(names[m] for m in missing))
    out: list[dict] = []
    for r in reader[header_idx + 1:]:
        if len(r) < len(header) // 2:
            continue
        cell = lambda k, r=r: r[idx[k]].strip().replace("=", "").strip('"') if idx[k] is not None and idx[k] < len(r) else ""   # noqa: E731
        code: str = re.sub(r"\D", "", cell("code"))[-6:]
        side = _side(cell("side"))
        if len(code) != 6 or side is None:
            continue
        try:
            qty = int(float(cell("qty")))
            price = float(cell("price"))
        except ValueError:
            continue
        if qty <= 0 or price <= 0:
            continue
        out.append({"date": _date(cell("date")) if idx["date"] is not None else None, "code": code, "name": cell("name") or None,
                    "side": side, "qty": abs(qty), "price": price, "trade_id": cell("trade_id") or None})
    return out


def import_statement(conn: sqlite3.Connection, account_id: str, text: str) -> dict:
    """导入交割单：已记过的成交（同一成交编号）跳过；能对上的待下单委托自动结清，对不上的按独立成交记账"""
    rows = parse_statement(text)
    added: int = 0
    skipped: int = 0
    for r in sorted(rows, key=lambda x: (x["date"] or date.min)):
        tag: str = f"import:{r['trade_id']}" if r["trade_id"] else f"import:{r['date']}:{r['code']}:{r['side']}:{r['qty']}:{r['price']}"
        if ledger.one(conn, "SELECT id FROM fills WHERE account_id=? AND source=?", (account_id, tag)):
            skipped += 1
            continue
        o = ledger.one(conn, "SELECT * FROM orders WHERE account_id=? AND code=? AND side=? AND status IN ('pending_manual','partial') "
                             "ORDER BY created LIMIT 1", (account_id, r["code"], r["side"]))
        qty: int = r["qty"]
        if o:
            qty = min(qty, o["qty"] - o["filled_qty"])
        apply_fill(conn, o["id"] if o else None, account_id, r["code"], r["side"], qty, r["price"], r["date"] or ledger.today(),
                   r["name"] or (o or {}).get("name"), tag, (o or {}).get("plan_id"))
        added += 1
    ledger.audit(conn, account_id, "import_statement", {"rows": len(rows), "added": added, "skipped": skipped})
    return {"rows": len(rows), "added": added, "skipped": skipped}
