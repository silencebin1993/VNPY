"""
实盘：接入任意已安装的 vnpy 交易网关（高级/实验性，面向熟悉 vnpy 的用户）。

设置里 `vnpy_gateway` 是网关类的完整路径（例如 `"vnpy_xtp.XtpGateway"`），`vnpy_setting` 是该网关
`connect()` 要的参数字典（账号、密码、行情/交易服务器地址等，每个网关不一样，具体字段以该网关自己的
`default_setting` 为准）。网关本身要另外安装（例如 `pip install vnpy_xtp`），这里只是动态加载。

注意 `MainEngine.connect()` 是异步的：它把连接请求交给网关后立刻返回，真正登录成功还是失败要看网关自己
写的日志/事件，这层桥接不会等待也不会因为登录失败立刻报错——下单时如果网关其实没连上，`send_order` 通常
会返回空委托号，我们按下单失败处理；但也可能出现"已经发出但网关那边连接还没建好"的中间状态，接真实网关
时要注意观察。
"""
from __future__ import annotations

import importlib
import json
import math
import threading
from dataclasses import dataclass

from vnpy.event import EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import OrderRequest as VnOrderRequest

from .. import ledger
from ..engine import OrderRequest, cancel, place
from .base import Broker, exchange_suffix

_CLIENTS: dict[tuple, _Client] = {}
_LOCK = threading.Lock()

_EXCHANGE_MAP: dict[str, Exchange] = {"SH": Exchange.SSE, "SZ": Exchange.SZSE, "BJ": Exchange.BSE}


@dataclass
class _Client:
    engine: object          # MainEngine 实例
    gateway_name: str


def _load_gateway_class(path: str) -> type:
    """"package.module.ClassName" -> 网关类；路径不对或者网关包没装都会抛异常"""
    module_name, sep, cls_name = path.rpartition(".")
    if not sep:
        raise ImportError(f"网关路径格式不对，应为「包名.类名」，例如 vnpy_xtp.XtpGateway：{path}")
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)


def _cache_key(account_id: str, live: dict) -> tuple:
    setting: dict = live.get("vnpy_setting") or {}
    return account_id, live.get("vnpy_gateway"), json.dumps(setting, sort_keys=True, ensure_ascii=False, default=str)


def _connect(live: dict) -> _Client:
    gw_path: str = str(live.get("vnpy_gateway") or "")
    setting: dict = live.get("vnpy_setting") or {}
    if not gw_path or not setting:
        raise ConnectionError("请在设置里填写 vnpy 网关和连接参数")
    try:
        cls = _load_gateway_class(gw_path)
    except Exception as e:
        raise ConnectionError(f"没有找到 vnpy 网关「{gw_path}」：{e}") from e
    gateway_name: str = getattr(cls, "default_name", "") or cls.__name__
    ee = EventEngine()
    me = MainEngine(ee)
    try:
        me.add_gateway(cls, gateway_name)
        me.connect(dict(setting), gateway_name)
    except Exception as e:
        raise ConnectionError(f"vnpy 网关连接失败：{e}") from e
    return _Client(engine=me, gateway_name=gateway_name)


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


class VnpyGatewayBroker(Broker):
    """接入任意已安装的 vnpy 网关下单；高级/实验性功能，见模块文档。"""

    name = "vnpy_gateway"
    label = "vnpy 网关（高级/实验性）"
    description = "高级/实验性功能：接入任意已安装的 vnpy 交易网关（如期货/股票的 CTP、XTP 等），需要自己安装对应网关包并填写连接参数。"
    is_live = True
    can_auto = True
    optional = True

    def available(self) -> tuple[bool, str]:
        live: dict = self.settings.get("live") or {}
        gw_path = str(live.get("vnpy_gateway") or "")
        if not gw_path or not live.get("vnpy_setting"):
            return False, "请在设置里填写 vnpy 网关（例如 vnpy_xtp.XtpGateway）和连接参数"
        try:
            _load_gateway_class(gw_path)
        except Exception as e:
            return False, f"没有找到 vnpy 网关「{gw_path}」（需要先安装对应的网关包，例如 pip install vnpy_xtp）：{e}"
        return True, ""

    def _client(self) -> _Client:
        live: dict = self.settings.get("live") or {}
        key = _cache_key(self.account["id"], live)
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
        try:                                                         # 发出去之前出错：确定没有下单
            price: float = _order_price(req, ref_price, self.settings)
            c: _Client = self._client()
            vt_req = VnOrderRequest(
                symbol=req.code, exchange=_EXCHANGE_MAP[exchange_suffix(req.code)],
                direction=Direction.LONG if req.side == "buy" else Direction.SHORT, type=OrderType.LIMIT,
                volume=float(req.qty), price=float(price), offset=Offset.NONE, reference="quant_web",
            )
        except Exception as e:                                       # noqa: BLE001  网关异常种类不可控
            self.mark_rejected(conn, order["id"], f"券商下单失败：{e}")
            ledger.alert(conn, self.account["id"], req.code, "urgent", "broker", "vnpy 网关下单失败", str(e))
            return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],))          # type: ignore[return-value]
        try:
            vt_orderid = c.engine.send_order(vt_req, c.gateway_name)
        except Exception as e:                                       # noqa: BLE001  发送时出错：网关可能已经发出
            return self.mark_uncertain(conn, self.account["id"], order["id"], req.code, f"vnpy 网关下单时出错（{e}）")
        if not vt_orderid:                                           # 网关明确没有接受（没连上等）
            self.mark_rejected(conn, order["id"], "券商下单失败：vnpy 网关下单失败（send_order 返回空委托号）")
            ledger.alert(conn, self.account["id"], req.code, "urgent", "broker", "vnpy 网关下单失败", "send_order 返回空委托号")
            return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],))          # type: ignore[return-value]
        conn.execute("UPDATE orders SET broker_order_id=? WHERE id=?", (vt_orderid, order["id"]))
        ledger.audit(conn, self.account["id"], "broker_place", {"order_id": order["id"], "broker_order_id": vt_orderid})
        return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],))               # type: ignore[return-value]

    def cancel(self, conn, order_id: str) -> dict:
        o = ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if o is None:
            raise ValueError("没有这笔委托")
        if o.get("broker_order_id"):
            c: _Client = self._client()
            vt_order = c.engine.get_order(o["broker_order_id"])
            if vt_order is None:
                raise ValueError("在券商网关里找不到这笔委托，可能已经成交或已经撤销")
            try:
                c.engine.cancel_order(vt_order.create_cancel_request(), c.gateway_name)
            except Exception as e:
                raise ValueError(f"券商撤单失败：{e}") from e
        return cancel(conn, order_id)

    def sync(self, conn) -> dict:
        try:
            c: _Client = self._client()
            n = 0
            for t in c.engine.get_all_trades():
                tag = f"{self.name}:{t.vt_tradeid}"
                if self.seen_trade(conn, self.account["id"], tag):
                    continue
                side = "buy" if t.direction == Direction.LONG else "sell"
                o = ledger.one(conn, "SELECT * FROM orders WHERE account_id=? AND broker_order_id=?",
                               (self.account["id"], t.vt_orderid))
                n += self.book_trade(conn, tag, o, t.symbol, side, int(t.volume), float(t.price))
            for od in c.engine.get_all_orders():
                row = ledger.one(conn, "SELECT * FROM orders WHERE account_id=? AND broker_order_id=?",
                                 (self.account["id"], od.vt_orderid))
                if row is None or row["status"] not in ("submitted", "partial"):
                    continue
                if od.status == Status.CANCELLED:
                    cancel(conn, row["id"], "cancelled", "券商显示已撤单")
                elif od.status == Status.REJECTED:
                    cancel(conn, row["id"], "rejected", "券商拒绝委托")
            return {"fills": n, "message": f"同步完成，新增 {n} 笔成交" if n else "没有新的成交"}
        except Exception as e:                                       # noqa: BLE001  同步失败不能抛出去
            return {"fills": 0, "error": f"同步失败：{e}"}

    def remote_snapshot(self) -> dict | None:
        try:
            c: _Client = self._client()
            accounts = c.engine.get_all_accounts()
            acc = accounts[0] if accounts else None
            cash = float(acc.balance) if acc else 0.0
            frozen = float(acc.frozen) if acc else 0.0
            positions = []
            mv_total = 0.0
            for p in c.engine.get_all_positions():
                # PositionData 没有现价字段，市值只能用持仓量 x 成本价近似，不是真实市值
                mv = float(p.volume) * float(p.price)
                mv_total += mv
                positions.append({"code": p.symbol, "qty": int(p.volume), "available": int(p.volume - p.frozen),
                                  "cost": float(p.price), "market_value": mv})
            return {"cash": cash, "frozen": frozen, "market_value": mv_total, "total": cash + mv_total,
                    "positions": positions}
        except Exception:                                            # noqa: BLE001  只用于页面对账，失败就不显示
            return None
