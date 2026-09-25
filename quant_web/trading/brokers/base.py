"""
统一的券商接口：模拟盘、手动实盘、miniQMT、同花顺客户端（easytrader）、vnpy 网关都实现同一组方法，
以后换券商只需要在设置里换接口，页面、风控、交易计划、复盘都不用改。

Broker(account, settings)：
- available() -> (能否使用, 中文原因)          检查安装和配置，不下单
- can_auto                                      能不能由程序自动下单（手动实盘不能）
- place(conn, req, ref_price) -> 委托记录       先写账本，再（实盘）发给券商；失败时委托标记为 rejected 并写原因
- cancel(conn, order_id) -> 委托记录
- sync(conn) -> {fills: n, message}             实盘：从券商拉取成交写进账本（按券商成交编号去重）；模拟/手动：不做事
- status() -> {connected, message}
"""
from __future__ import annotations

import sqlite3

from .. import ledger
from ..engine import OrderRequest, apply_fill, cancel, place


class Broker:
    name: str = ""
    label: str = ""
    description: str = ""
    is_live: bool = False
    can_auto: bool = False
    optional: bool = False

    def __init__(self, account: dict, settings: dict | None = None) -> None:
        self.account = account
        self.settings = settings or {}

    def available(self) -> tuple[bool, str]:
        return True, ""

    def status(self) -> dict:
        ok, why = self.available()
        return {"connected": ok, "message": why or "可以使用"}

    def place(self, conn: sqlite3.Connection, req: OrderRequest, ref_price: float | None = None) -> dict:
        return place(conn, self.account["id"], req, ref_price=ref_price)

    def cancel(self, conn: sqlite3.Connection, order_id: str) -> dict:
        return cancel(conn, order_id)

    def sync(self, conn: sqlite3.Connection) -> dict:
        return {"fills": 0, "message": ""}

    # ---------------------------------------------------------------- 实盘接口共用的小工具
    @staticmethod
    def seen_trade(conn: sqlite3.Connection, account_id: str, tag: str) -> bool:
        """这笔券商成交是否已经记过账（fills.source 里存 "<接口>:<成交编号>"）"""
        return ledger.one(conn, "SELECT id FROM fills WHERE account_id=? AND source=?", (account_id, tag)) is not None

    def book_trade(self, conn: sqlite3.Connection, tag: str, order: dict | None, code: str, side: str, qty: int, price: float) -> int:
        """把券商的一笔成交记进账本，返回 1；记不进去（例如卖出了账本里没有的股票——在别处下的单）时只提醒一次、返回 0，
        不影响后面其他成交的同步"""
        try:
            apply_fill(conn, order["id"] if order else None, self.account["id"], code, side, qty, price, ledger.today(),
                       (order or {}).get("name"), tag, (order or {}).get("plan_id"))
            return 1
        except ValueError as e:
            key = f"sync_skip:{self.account['id']}:{tag}"
            if ledger.one(conn, "SELECT value FROM meta WHERE key=?", (key,)) is None:
                conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, ledger.now()))
                ledger.alert(conn, self.account["id"], code, "warn", "sync_mismatch", "有一笔券商成交没能记账",
                             f"{code} {'买入' if side == 'buy' else '卖出'} {qty} 股 @ {price:.2f}：{e}。"
                             "可能是在别的地方下的单，请核对持仓（可以导入交割单补记）")
            return 0

    @staticmethod
    def mark_uncertain(conn: sqlite3.Connection, account_id: str, order_id: str, code: str, message: str) -> dict:
        """发给券商时出错（超时、断线、界面没反应）：券商那边可能已经收到了这笔委托，也可能没有。
        不能当成"被拒绝"——那样你或程序再下一次就可能变成两笔。保持"已提交"，等同步成交核对（收盘后仍没成交会过期），并紧急提醒去券商 App 核对"""
        conn.execute("UPDATE orders SET message=?, updated=? WHERE id=?", (f"提交结果不确定：{message}", ledger.now(), order_id))
        ledger.alert(conn, account_id, code, "urgent", "broker", "下单结果不确定，先别重复下单",
                     f"{message}。券商那边可能已经收到这笔委托：请马上在券商 App 的“当日委托”里核对；确认没有这笔之前，不要重新下单。")
        return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))            # type: ignore[return-value]

    @staticmethod
    def mark_rejected(conn: sqlite3.Connection, order_id: str, message: str) -> dict:
        o = ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if o and o["status"] in ("submitted", "pending_manual", "partial", "waiting_trigger"):
            cancel(conn, order_id, "rejected", message)
        return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))            # type: ignore[return-value]


def exchange_suffix(code: str) -> str:
    """6 位代码 → 交易所后缀（QMT 等接口用）：60/68/90 开头 SH，8/4/92 开头 BJ，其余 SZ"""
    if code.startswith(("6", "9")) and not code.startswith("92"):
        return "SH"
    if code.startswith(("8", "4", "92")):
        return "BJ"
    return "SZ"
