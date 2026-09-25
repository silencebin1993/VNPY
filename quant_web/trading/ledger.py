"""
交易账本（SQLite：workspace/trading/trading.db）：账户、委托、成交、持仓、交易计划、每日资产、复盘、审计、提醒。

- 模拟盘：账本就是全部真相；
- 实盘（手动 / QMT 等）：账本是券商状态的镜像（成交由你确认或由接口同步），用于风控、计划和复盘；
- 所有写操作在一个锁里、一个事务里完成（盘中监控线程和网页同时操作也不会写坏）；
- 审计表只追加不修改（实盘的每个动作都记在这里）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .. import config

_lock = threading.RLock()
_ready: set[str] = set()             # 已建表的数据库文件（每个文件只执行一次建表语句）

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, broker TEXT NOT NULL,
    initial_cash REAL NOT NULL, cash REAL NOT NULL, frozen REAL NOT NULL DEFAULT 0,
    created TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, note TEXT DEFAULT '', settings TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, side TEXT NOT NULL,
    kind TEXT NOT NULL, price REAL, trigger REAL, qty INTEGER NOT NULL, filled_qty INTEGER NOT NULL DEFAULT 0,
    avg_price REAL, status TEXT NOT NULL, valid TEXT NOT NULL DEFAULT 'day', trade_date TEXT, created TEXT NOT NULL,
    updated TEXT NOT NULL, reason TEXT DEFAULT '', plan_id TEXT, broker_order_id TEXT, message TEXT DEFAULT '',
    source TEXT DEFAULT 'user', flags TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_orders_acc ON orders(account_id, status);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    side TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL, amount REAL NOT NULL, commission REAL NOT NULL,
    stamp REAL NOT NULL, transfer REAL NOT NULL, trade_date TEXT NOT NULL, time TEXT NOT NULL, source TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_fills_acc ON fills(account_id, trade_date);
CREATE TABLE IF NOT EXISTS positions (
    account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, qty INTEGER NOT NULL, available INTEGER NOT NULL,
    cost REAL NOT NULL, opened TEXT, last_price REAL, last_close REAL, plan_id TEXT,
    PRIMARY KEY (account_id, code)
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT, status TEXT NOT NULL,
    entry REAL, stop REAL NOT NULL, initial_stop REAL NOT NULL, target REAL, trail TEXT DEFAULT 'none', max_days INTEGER,
    reason TEXT DEFAULT '', created TEXT NOT NULL, opened TEXT, closed TEXT, highest REAL, qty_plan INTEGER,
    history TEXT DEFAULT '[]', note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_plans_acc ON plans(account_id, status);
CREATE TABLE IF NOT EXISTS equity (
    account_id TEXT NOT NULL, date TEXT NOT NULL, cash REAL NOT NULL, market_value REAL NOT NULL, total REAL NOT NULL,
    PRIMARY KEY (account_id, date)
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, plan_id TEXT, code TEXT NOT NULL, name TEXT,
    opened TEXT, closed TEXT, qty INTEGER, entry REAL, exit REAL, pnl REAL, r_multiple REAL, days INTEGER,
    exit_reason TEXT, violations TEXT DEFAULT '[]', note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, account_id TEXT, action TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, account_id TEXT, code TEXT, level TEXT NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT DEFAULT '', read INTEGER NOT NULL DEFAULT 0, sent TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db_path() -> Path:
    return config.WORKSPACE.joinpath("trading", "trading.db")


def now() -> str:
    return datetime.now(config.CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today() -> date:
    return datetime.now(config.CHINA_TZ).date()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


_local = threading.local()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """带锁的连接：块内所有操作是一个事务，出错整体回滚。
    同一线程里嵌套调用（比如事务里又写站内信）复用外层连接和事务，避免自己等自己的写锁。"""
    outer: sqlite3.Connection | None = getattr(_local, "conn", None)
    if outer is not None:
        yield outer
        return
    path: Path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
        try:
            if str(path) not in _ready or not path.exists():
                conn.executescript(SCHEMA)
                _ready.add(str(path))
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _local.conn = None
            conn.close()


def rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def audit(conn: sqlite3.Connection, account_id: str | None, action: str, detail: Any) -> None:
    conn.execute("INSERT INTO audit(time, account_id, action, detail) VALUES (?,?,?,?)",
                 (now(), account_id, action, json.dumps(detail, ensure_ascii=False, default=str)))


def alert(conn: sqlite3.Connection, account_id: str | None, code: str | None, level: str, kind: str, title: str,
          body: str = "") -> int:
    cur = conn.execute("INSERT INTO alerts(time, account_id, code, level, kind, title, body) VALUES (?,?,?,?,?,?,?)",
                       (now(), account_id, code, level, kind, title, body))
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------- 账户

def create_account(name: str, kind: str, broker: str, initial_cash: float, note: str = "") -> dict:
    if kind not in ("paper", "live"):
        raise ValueError("账户类型只能是模拟盘（paper）或实盘（live）")
    if broker not in ("paper", "manual", "qmt", "easytrader", "vnpy_gateway"):
        raise ValueError(f"不认识的券商接口「{broker}」")
    if kind == "paper" and broker != "paper":
        raise ValueError("模拟盘账户的接口只能是 paper")
    if kind == "live" and broker == "paper":
        raise ValueError("实盘账户要选择手动、QMT 等券商接口")
    if not 1000 <= float(initial_cash) <= 1e10:
        raise ValueError("初始资金要在 1000 元到 100 亿元之间")
    aid: str = new_id("acc")
    with connect() as c:
        c.execute("INSERT INTO accounts(id, name, kind, broker, initial_cash, cash, created, note) VALUES (?,?,?,?,?,?,?,?)",
                  (aid, name.strip()[:40] or "我的账户", kind, broker, float(initial_cash), float(initial_cash), now(), note))
        audit(c, aid, "create_account", {"name": name, "kind": kind, "broker": broker, "initial_cash": initial_cash})
    return get_account(aid)


def get_account(aid: str) -> dict:
    with connect() as c:
        acc = one(c, "SELECT * FROM accounts WHERE id=?", (aid,))
    if acc is None:
        raise ValueError("没有这个账户（可能已被删除）")
    acc["settings"] = json.loads(acc.get("settings") or "{}")
    return acc


def list_accounts(include_archived: bool = False) -> list[dict]:
    with connect() as c:
        out = rows(c, "SELECT * FROM accounts" + ("" if include_archived else " WHERE archived=0") + " ORDER BY created")
    for a in out:
        a["settings"] = json.loads(a.get("settings") or "{}")
    return out


def reset_paper(aid: str) -> dict:
    acc = get_account(aid)
    if acc["kind"] != "paper":
        raise ValueError("只有模拟盘可以重置")
    with connect() as c:
        for t in ("orders", "fills", "positions", "plans", "equity", "journal"):
            c.execute(f"DELETE FROM {t} WHERE account_id=?", (aid,))           # noqa: S608  表名是固定的
        c.execute("UPDATE accounts SET cash=initial_cash, frozen=0 WHERE id=?", (aid,))
        audit(c, aid, "reset", {})
    return get_account(aid)


def archive_account(aid: str) -> None:
    with connect() as c:
        c.execute("UPDATE accounts SET archived=1 WHERE id=?", (aid,))
        audit(c, aid, "archive", {})


def get_meta(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        r = one(c, "SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


def set_meta(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
