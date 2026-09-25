"""
实盘：miniQMT（券商极简交易终端，通过 `xtquant` 库下单）。

需要：券商开通 miniQMT 权限并安装客户端，设置里填好 `qmt_path`（miniQMT 的 userdata_mini 目录）和
`qmt_account`（资金账号）。`xtquant` 通常随 miniQMT 客户端安装在 `<安装目录>/bin.x64/Lib/site-packages`
下，不一定在 Python 的默认搜索路径里；如果直接 `import xtquant` 失败，会用 `qmt_path` 试着把
`<qmt_path>/../bin.x64/Lib/site-packages` 和 `<qmt_path>` 本身加入 `sys.path` 再试一次
（miniQMT 有时会把 `xtquant` 和 pip 装的旧版本混在一起，这里连同 sys.modules 里的旧缓存一起清掉重新导入，
避免用错版本）。

市价单/触发后的止损单不会真的按"市价"发给 QMT（避免极端行情下无限往下/往上成交），而是按参考价打折/加价后
的保护性限价单发送，参见 `_order_price`。
"""
from __future__ import annotations

import math
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import ledger
from ..engine import OrderRequest, cancel, place
from .base import Broker, exchange_suffix

_CLIENTS: dict[tuple, _Client] = {}
_LOCK = threading.Lock()


@dataclass
class _Client:
    trader: object          # XtQuantTrader 实例
    account: object          # StockAccount 实例
    xtconstant: object       # xtquant.xtconstant 模块（买卖方向、价格类型等常量）


def _import_xtquant(qmt_path: str = "") -> tuple:
    """导入 xtquant 的 XtQuantTrader / StockAccount / xtconstant；直接导入失败时，
    如果给了 qmt_path，把 miniQMT 安装目录下常见的两个位置插到 sys.path 最前面再试一次。"""
    try:
        from xtquant import xtconstant
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount
        return XtQuantTrader, StockAccount, xtconstant
    except ImportError:
        if not qmt_path:
            raise
        for mod in [m for m in list(sys.modules) if m == "xtquant" or m.startswith("xtquant.")]:
            del sys.modules[mod]           # 避免之前缓存了一个不完整/版本不对的 xtquant
        base = Path(qmt_path)
        for extra in (base.parent.joinpath("bin.x64", "Lib", "site-packages"), base):
            p = str(extra)
            if p not in sys.path:
                sys.path.insert(0, p)
        from xtquant import xtconstant             # noqa: F811  按 qmt_path 补上路径后重新导入
        from xtquant.xttrader import XtQuantTrader  # noqa: F811
        from xtquant.xttype import StockAccount     # noqa: F811
        return XtQuantTrader, StockAccount, xtconstant


def _connect(live: dict) -> _Client:
    qmt_path: str = str(live.get("qmt_path") or "")
    qmt_account: str = str(live.get("qmt_account") or "")
    if not qmt_path or not qmt_account:
        raise ConnectionError("请在设置里填写 QMT 目录和资金账号")
    try:
        xt_trader_cls, stock_account_cls, xtconstant = _import_xtquant(qmt_path)
    except ImportError as e:
        raise ConnectionError(f"没有找到 xtquant：{e}") from e
    session_id: int = int(time.time() * 1000) % 2_000_000_000       # 每次连接用不同的会话号
    trader = xt_trader_cls(qmt_path, session_id)
    trader.start()
    rc = trader.connect()
    if rc != 0:
        raise ConnectionError(f"miniQMT 连接失败（返回码 {rc}），请检查客户端是否已登录、目录是否正确")
    acc = stock_account_cls(qmt_account)
    trader.subscribe(acc)
    return _Client(trader=trader, account=acc, xtconstant=xtconstant)


def _order_price(req: OrderRequest, ref_price: float | None, settings: dict) -> float:
    """挂单价格：限价单用委托价；市价单（含止损/止盈触发后转的市价单）用保护性限价而不是真正的市价——
    卖出按参考价 98% 向下取整到分（不低于跌停价），买入按参考价 102% 向上取整到分（不高于涨停价）。"""
    if req.kind == "limit":
        if not req.price:
            raise ValueError("限价单要填价格")
        return float(req.price)
    if not ref_price:
        raise ValueError("没有可用的参考价，无法下市价单")
    quote: dict = settings.get("quote") or {}
    if req.side == "sell":
        px: float = math.floor(ref_price * 0.98 * 100 + 1e-6) / 100
        limit_down = quote.get("limit_down")
        if limit_down is not None:
            px = max(px, float(limit_down))
        return px
    px = math.ceil(ref_price * 1.02 * 100 - 1e-6) / 100
    limit_up = quote.get("limit_up")
    if limit_up is not None:
        px = min(px, float(limit_up))
    return px


class QmtBroker(Broker):
    name = "qmt"
    label = "QMT 实盘（miniQMT）"
    description = "通过券商的 miniQMT 极简交易终端下单，需要先在券商开通 miniQMT 权限并安装客户端。"
    is_live = True
    can_auto = True
    optional = True

    def available(self) -> tuple[bool, str]:
        live: dict = self.settings.get("live") or {}
        try:
            _import_xtquant(str(live.get("qmt_path") or ""))
        except ImportError:
            return False, "没有找到 xtquant（需要在券商开通 miniQMT，并在设置里填写 userdata_mini 目录）"
        if not live.get("qmt_path") or not live.get("qmt_account"):
            return False, "请在设置里填写 QMT 目录和资金账号"
        return True, ""

    def _client(self) -> _Client:
        live: dict = self.settings.get("live") or {}
        key = (self.account["id"], live.get("qmt_path"), live.get("qmt_account"))
        with _LOCK:
            c = _CLIENTS.get(key)
            if c is None:
                c = _connect(live)
                _CLIENTS[key] = c
            return c

    def place(self, conn, req: OrderRequest, ref_price: float | None = None) -> dict:
        if req.kind in ("stop", "take_profit"):
            return place(conn, self.account["id"], req, ref_price=ref_price)    # 条件单等触发，不发给券商
        order: dict = place(conn, self.account["id"], req, ref_price=ref_price, status="submitted")
        try:
            price: float = _order_price(req, ref_price, self.settings)
            c: _Client = self._client()
            side = c.xtconstant.STOCK_BUY if req.side == "buy" else c.xtconstant.STOCK_SELL
            result = c.trader.order_stock(c.account, f"{req.code}.{exchange_suffix(req.code)}", side, int(req.qty),
                                          c.xtconstant.FIX_PRICE, price, "quant_web", (req.reason or "")[:20])
            if result is None or result < 0:
                raise ConnectionError(f"miniQMT 下单被拒绝（返回码 {result}）")
        except Exception as e:                                       # noqa: BLE001  券商/网络异常种类不可控
            self.mark_rejected(conn, order["id"], f"券商下单失败：{e}")
            ledger.alert(conn, self.account["id"], req.code, "urgent", "broker", "QMT 下单失败", str(e))
            return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],))          # type: ignore[return-value]
        conn.execute("UPDATE orders SET broker_order_id=? WHERE id=?", (str(result), order["id"]))
        ledger.audit(conn, self.account["id"], "broker_place", {"order_id": order["id"], "broker_order_id": result})
        return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],))               # type: ignore[return-value]

    def cancel(self, conn, order_id: str) -> dict:
        o = ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if o is None:
            raise ValueError("没有这笔委托")
        if o.get("broker_order_id"):
            c: _Client = self._client()
            try:
                rc = c.trader.cancel_order_stock(c.account, int(o["broker_order_id"]))
            except Exception as e:
                raise ValueError(f"券商撤单失败：{e}") from e
            if rc != 0:
                raise ValueError(f"券商撤单失败（返回码 {rc}）")
        return cancel(conn, order_id)

    def sync(self, conn) -> dict:
        try:
            c: _Client = self._client()
            n = 0
            for t in c.trader.query_stock_trades(c.account) or []:
                tag = f"{self.name}:{t.traded_id}"
                if self.seen_trade(conn, self.account["id"], tag):
                    continue
                code = str(t.stock_code).split(".")[0]
                side = "buy" if t.order_type == c.xtconstant.STOCK_BUY else "sell"
                o = ledger.one(conn, "SELECT * FROM orders WHERE account_id=? AND broker_order_id=?",
                               (self.account["id"], str(t.order_id)))
                n += self.book_trade(conn, tag, o, code, side, int(t.traded_volume), float(t.traded_price))
            for od in c.trader.query_stock_orders(c.account, False) or []:
                row = ledger.one(conn, "SELECT * FROM orders WHERE account_id=? AND broker_order_id=?",
                                 (self.account["id"], str(od.order_id)))
                if row is None or row["status"] not in ("submitted", "partial"):
                    continue
                if od.order_status in (c.xtconstant.ORDER_CANCELED, c.xtconstant.ORDER_PART_CANCEL):
                    cancel(conn, row["id"], "cancelled", "券商显示已撤单")
                elif od.order_status == c.xtconstant.ORDER_JUNK:
                    cancel(conn, row["id"], "rejected", getattr(od, "status_msg", "") or "券商拒绝委托")
            return {"fills": n, "message": f"同步完成，新增 {n} 笔成交" if n else "没有新的成交"}
        except Exception as e:                                       # noqa: BLE001  同步失败不能抛出去
            return {"fills": 0, "error": f"同步失败：{e}"}

    def remote_snapshot(self) -> dict | None:
        try:
            c: _Client = self._client()
            asset = c.trader.query_stock_asset(c.account)
            positions = c.trader.query_stock_positions(c.account) or []
            return {
                "cash": float(asset.cash), "frozen": float(asset.frozen_cash),
                "market_value": float(asset.market_value), "total": float(asset.total_asset),
                "positions": [{"code": str(p.stock_code).split(".")[0], "qty": int(p.volume),
                              "available": int(p.can_use_volume), "cost": float(p.open_price),
                              "market_value": float(p.market_value)} for p in positions],
            }
        except Exception:                                            # noqa: BLE001  只用于页面对账，失败就不显示
            return None
