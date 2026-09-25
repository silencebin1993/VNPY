"""
实盘：同花顺客户端下单（通过 `easytrader` 库做 GUI 自动化）。

**实验性功能，不要依赖它做止损这类必须可靠执行的委托**：easytrader 本质是控制同花顺下单程序（xiadan.exe）
的窗口和控件，同花顺弹窗（公告、验证码、强制升级、网络断线重连）、窗口被切走、下单程序卡死或崩溃，都会让
下单静默失败或者点错东西；没有交易所直连接口那种确定性。设置里的自动下单范围（`auto_policy`）如果选了
同花顺接口，止损自动卖出的可靠性完全取决于这个下单程序当时是不是正常。

需要：本机装同花顺下单程序，设置里填 `easytrader_client`（xiadan.exe 的路径）。
"""
from __future__ import annotations

import math
import re
import threading

from .. import ledger
from ..engine import OrderRequest, cancel, place
from .base import Broker

_CLIENTS: dict[tuple, object] = {}
_LOCK = threading.Lock()


def _import_easytrader() -> object:
    import easytrader
    return easytrader


def _connect(client_path: str) -> object:
    if not client_path:
        raise ConnectionError("请在设置里填写同花顺下单程序 xiadan.exe 的路径")
    et = _import_easytrader()
    try:
        try:
            user = et.use("universal_client")
        except Exception:                                            # noqa: BLE001  旧版 easytrader 没有这个类型
            user = et.use("ths")
        user.connect(client_path)
    except Exception as e:
        raise ConnectionError(f"连接同花顺下单程序失败：{e}") from e
    return user


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


def _pick(d: object, keys: tuple[str, ...]) -> str | None:
    """从 easytrader 返回的字典里按候选键名取值（不同版本/账户类型键名不一样）"""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _code6(text: str) -> str | None:
    code = re.sub(r"\D", "", text)[-6:]
    return code if len(code) == 6 else None


class EasytraderBroker(Broker):
    """通过同花顺下单程序下单；GUI 自动化，实验性功能，见模块文档。"""

    name = "easytrader"
    label = "同花顺客户端下单（实验性）"
    description = "实验性功能：通过 GUI 自动化控制同花顺下单程序下单，不稳定，止损等关键委托不要依赖它。"
    is_live = True
    can_auto = True
    optional = True

    def available(self) -> tuple[bool, str]:
        try:
            _import_easytrader()
        except ImportError:
            return False, "没有安装 easytrader（可选：pip install easytrader，并且需要同花顺下单程序）"
        live: dict = self.settings.get("live") or {}
        if not live.get("easytrader_client"):
            return False, "请在设置里填写同花顺下单程序 xiadan.exe 的路径"
        return True, ""

    def _client(self) -> object:
        live: dict = self.settings.get("live") or {}
        client_path = str(live.get("easytrader_client") or "")
        key = (self.account["id"], client_path)
        with _LOCK:
            user = _CLIENTS.get(key)
            if user is None:
                user = _connect(client_path)
                _CLIENTS[key] = user
            return user

    def place(self, conn, req: OrderRequest, ref_price: float | None = None) -> dict:
        if req.kind in ("stop", "take_profit"):
            return place(conn, self.account["id"], req, ref_price=ref_price)    # 条件单等触发，不发给券商
        order: dict = place(conn, self.account["id"], req, ref_price=ref_price, status="submitted")
        try:
            price: float = _order_price(req, ref_price, self.settings)
            user = self._client()
            fn = user.buy if req.side == "buy" else user.sell
            result = fn(req.code, price=price, amount=int(req.qty))
            entrust_no = _pick(result, ("entrust_no", "合同编号", "委托编号"))
            if not entrust_no:
                raise ConnectionError(f"下单没有返回委托编号：{result}")
        except Exception as e:                                       # noqa: BLE001  券商/GUI 异常种类不可控
            self.mark_rejected(conn, order["id"], f"券商下单失败：{e}")
            ledger.alert(conn, self.account["id"], req.code, "urgent", "broker", "同花顺下单失败", str(e))
            return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],))          # type: ignore[return-value]
        conn.execute("UPDATE orders SET broker_order_id=? WHERE id=?", (entrust_no, order["id"]))
        ledger.audit(conn, self.account["id"], "broker_place", {"order_id": order["id"], "broker_order_id": entrust_no})
        return ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order["id"],))               # type: ignore[return-value]

    def cancel(self, conn, order_id: str) -> dict:
        o = ledger.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if o is None:
            raise ValueError("没有这笔委托")
        if o.get("broker_order_id"):
            try:
                self._client().cancel_entrust(o["broker_order_id"])
            except Exception as e:
                raise ValueError(f"券商撤单失败：{e}") from e
        return cancel(conn, order_id)

    def sync(self, conn) -> dict:
        try:
            user = self._client()
            n = 0
            for t in user.today_trades or []:
                trade_id = _pick(t, ("成交编号",))
                if not trade_id:
                    continue                                          # 没有成交编号就没法去重，宁可漏记也不能重复记
                tag = f"{self.name}:{trade_id}"
                if self.seen_trade(conn, self.account["id"], tag):
                    continue
                code = _code6(_pick(t, ("证券代码",)) or "")
                side_text = _pick(t, ("买卖标志", "操作")) or ""
                side = "buy" if "买" in side_text else ("sell" if "卖" in side_text else None)
                if code is None or side is None:
                    continue
                qty = int(float(_pick(t, ("成交数量",)) or 0))
                price = float(_pick(t, ("成交均价", "成交价格")) or 0)
                if qty <= 0 or price <= 0:
                    continue
                order_no = _pick(t, ("合同编号", "委托编号"))
                o = ledger.one(conn, "SELECT * FROM orders WHERE account_id=? AND broker_order_id=?",
                               (self.account["id"], order_no)) if order_no else None
                n += self.book_trade(conn, tag, o, code, side, qty, price)
            # 同花顺没有稳定可查的委托状态接口，撤单/拒单不会自动同步回账本，只能靠这里补录到的成交和你在
            # 界面上手动核对；这是 GUI 自动化的固有局限，不是遗漏。
            return {"fills": n, "message": f"同步完成，新增 {n} 笔成交" if n else "没有新的成交"}
        except Exception as e:                                       # noqa: BLE001  同步失败不能抛出去
            return {"fills": 0, "error": f"同步失败：{e}"}

    def remote_snapshot(self) -> dict | None:
        try:
            user = self._client()
            bal = user.balance
            b = bal[0] if isinstance(bal, list) and bal else (bal if isinstance(bal, dict) else {})
            cash = float(_pick(b, ("可用金额", "资金余额")) or 0)
            total = float(_pick(b, ("总资产",)) or 0)
            mv = float(_pick(b, ("股票市值",)) or 0)
            positions = []
            for p in user.position or []:
                code = _code6(_pick(p, ("证券代码",)) or "")
                if code is None:
                    continue
                positions.append({
                    "code": code, "qty": int(float(_pick(p, ("股票余额",)) or 0)),
                    "available": int(float(_pick(p, ("可用余额",)) or 0)),
                    "cost": float(_pick(p, ("成本价",)) or 0), "market_value": float(_pick(p, ("市值",)) or 0),
                })
            # easytrader 的资金字典不单独给"冻结"字段，这里估算不出来，统一按 0 处理（页面对账时以账本为准）
            return {"cash": cash, "frozen": 0.0, "market_value": mv, "total": total, "positions": positions}
        except Exception:                                            # noqa: BLE001  只用于页面对账，失败就不显示
            return None
