"""quant_web 实盘券商接口（qmt / easytrader / vnpy 网关）：可用性检测、保护性限价下单、条件单不发给券商、
券商失败时拒单+提醒、成交同步去重、券商撤单/拒单回写账本、撤单、对账快照；全部离线，用注入 sys.modules /
monkeypatch 的假对象代替真实券商接口（不连接任何真实券商）。"""
import sys
import types
from pathlib import Path

import pytest
from vnpy.trader.constant import Direction, Exchange, OrderType, Status

from quant_web import config
from quant_web.trading import brokers, ledger
from quant_web.trading.brokers import easytrader_broker, qmt, vnpy_bridge
from quant_web.trading.engine import OrderRequest


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 workspace（账本落在 tmp）；顺便清空三个接口的连接缓存，测试之间不互相影响"""
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    qmt._CLIENTS.clear()
    easytrader_broker._CLIENTS.clear()
    vnpy_bridge._CLIENTS.clear()
    return tmp_path


def make_account(broker: str, cash: float = 100_000) -> dict:
    return ledger.create_account("实盘", "live", broker, cash)


def _seed_position(conn, account_id: str, code: str, qty: int, cost: float) -> None:
    conn.execute("INSERT INTO positions(account_id, code, name, qty, available, cost, opened, last_price) "
                 "VALUES (?,?,?,?,?,?,?,?)", (account_id, code, f"测试{code}", qty, qty, cost, ledger.today().isoformat(), cost))


# ================================================================ QMT（xtquant）

class FakeXtQuantTrader:
    """记录调用参数的假 XtQuantTrader；order_stock_result/cancel_result 留空表示成功，测试里可以改成失败值"""

    def __init__(self, path: str, session_id: int) -> None:
        self.path = path
        self.session_id = session_id
        self.started = False
        self.connect_rc = 0
        self.subscribed = None
        self.orders: list[dict] = []
        self.cancel_calls: list[int] = []
        self.trades: list[object] = []
        self.query_orders: list[object] = []
        self.asset: object = None
        self.positions: list[object] = []
        self.order_stock_result: int | None = None
        self.cancel_result: int = 0
        self._next_order_id = 1_000_001

    def start(self) -> None:
        self.started = True

    def connect(self) -> int:
        return self.connect_rc

    def subscribe(self, acc: object) -> None:
        self.subscribed = acc

    def order_stock(self, acc, code, order_type, qty, price_type, price, strategy, remark):
        self.orders.append({"acc": acc, "code": code, "order_type": order_type, "qty": qty,
                            "price_type": price_type, "price": price, "strategy": strategy, "remark": remark})
        if self.order_stock_result is not None:
            return self.order_stock_result
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    def cancel_order_stock(self, acc, order_id) -> int:
        self.cancel_calls.append(order_id)
        return self.cancel_result

    def query_stock_trades(self, acc):
        return self.trades

    def query_stock_orders(self, acc, cancelable_only):
        return self.query_orders

    def query_stock_asset(self, acc):
        return self.asset

    def query_stock_positions(self, acc):
        return self.positions


class FakeXtTrade:
    def __init__(self, traded_id, order_id, stock_code, traded_volume, traded_price, order_type) -> None:
        self.traded_id, self.order_id, self.stock_code = traded_id, order_id, stock_code
        self.traded_volume, self.traded_price, self.order_type = traded_volume, traded_price, order_type


class FakeXtOrder:
    def __init__(self, order_id, order_status, status_msg: str = "") -> None:
        self.order_id, self.order_status, self.status_msg = order_id, order_status, status_msg


def install_fake_xtquant(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    xtquant = types.ModuleType("xtquant")
    xttrader = types.ModuleType("xtquant.xttrader")
    xttype = types.ModuleType("xtquant.xttype")
    xtconstant = types.ModuleType("xtquant.xtconstant")
    xtconstant.STOCK_BUY, xtconstant.STOCK_SELL, xtconstant.FIX_PRICE = 23, 24, 11
    xtconstant.ORDER_CANCELED, xtconstant.ORDER_PART_CANCEL, xtconstant.ORDER_JUNK = 54, 53, 57

    class FakeStockAccount:
        def __init__(self, account_id: str) -> None:
            self.account_id = account_id

    xttrader.XtQuantTrader = FakeXtQuantTrader
    xttype.StockAccount = FakeStockAccount
    for name, mod in (("xtquant", xtquant), ("xtquant.xttrader", xttrader), ("xtquant.xttype", xttype),
                      ("xtquant.xtconstant", xtconstant)):
        monkeypatch.setitem(sys.modules, name, mod)
    return xtconstant


def qmt_broker(monkeypatch: pytest.MonkeyPatch, live: dict | None = None, quote: dict | None = None,
              cash: float = 100_000) -> tuple[qmt.QmtBroker, types.ModuleType]:
    xtconstant = install_fake_xtquant(monkeypatch)
    acc = make_account("qmt", cash)
    settings = {"live": {"qmt_path": "C:/qmt/userdata_mini", "qmt_account": "8888", **(live or {})}}
    if quote is not None:
        settings["quote"] = quote
    return qmt.QmtBroker(acc, settings), xtconstant


def test_qmt_available_messages(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = qmt.QmtBroker({"id": "x", "broker": "qmt"}, {"live": {}})
    ok, why = b.available()
    assert not ok and "xtquant" in why
    install_fake_xtquant(monkeypatch)
    b2 = qmt.QmtBroker({"id": "x", "broker": "qmt"}, {"live": {}})
    ok2, why2 = b2.available()
    assert not ok2 and "QMT 目录" in why2
    b3 = qmt.QmtBroker({"id": "x", "broker": "qmt"}, {"live": {"qmt_path": "C:/qmt", "qmt_account": "8888"}})
    assert b3.available() == (True, "")


def test_qmt_place_limit_buy(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, xtconstant = qmt_broker(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.5), ref_price=10.4)
        assert order["status"] == "submitted" and order["broker_order_id"]
        call = b._client().trader.orders[-1]
        assert (call["code"], call["qty"], call["price"]) == ("600000.SH", 1000, 10.5)
        assert call["order_type"] == xtconstant.STOCK_BUY and call["price_type"] == xtconstant.FIX_PRICE
        assert call["strategy"] == "quant_web"
        audit = ledger.rows(c, "SELECT * FROM audit WHERE account_id=? AND action='broker_place'", (b.account["id"],))
        assert len(audit) == 1


def test_qmt_market_sell_uses_protective_limit_and_limit_down(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xtconstant = install_fake_xtquant(monkeypatch)
    acc = make_account("qmt")
    with ledger.connect() as c:
        _seed_position(c, acc["id"], "600000", 1000, 9.0)
        _seed_position(c, acc["id"], "600001", 1000, 9.0)
        b1 = qmt.QmtBroker(acc, {"live": {"qmt_path": "C:/qmt", "qmt_account": "8888"}})
        o1 = b1.place(c, OrderRequest("600000", "sell", 300, "market"), ref_price=10.0)
        assert o1["status"] == "submitted"
        trader = b1._client().trader
        assert trader.orders[-1]["price"] == 9.8 and trader.orders[-1]["order_type"] == xtconstant.STOCK_SELL
        # 同一账户同一配置：复用同一个连接（缓存生效），quote 只影响这一笔的保护价
        b2 = qmt.QmtBroker(acc, {"live": {"qmt_path": "C:/qmt", "qmt_account": "8888"}, "quote": {"limit_down": 9.9}})
        b2.place(c, OrderRequest("600001", "sell", 300, "market"), ref_price=10.0)
        assert trader.orders[-1]["price"] == 9.9 and b2._client() is b1._client()


def test_qmt_stop_order_not_sent_to_broker(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, _ = qmt_broker(monkeypatch)
    with ledger.connect() as c:
        _seed_position(c, b.account["id"], "600000", 1000, 9.0)
        order = b.place(c, OrderRequest("600000", "sell", 500, "stop", trigger=9.0))
        assert order["status"] == "waiting_trigger" and order["broker_order_id"] is None
    assert qmt._CLIENTS == {}                              # 没有连过券商


def test_qmt_failure_rejects_order_and_alerts(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, _ = qmt_broker(monkeypatch)
    with ledger.connect() as c:
        b._client().trader.order_stock_result = -1          # 券商拒绝
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        assert order["status"] == "rejected" and "券商下单失败" in order["message"]
        acc_row = ledger.one(c, "SELECT frozen FROM accounts WHERE id=?", (b.account["id"],))
        assert acc_row["frozen"] == 0                        # 冻结资金已释放
        alerts = ledger.rows(c, "SELECT * FROM alerts WHERE account_id=?", (b.account["id"],))
        assert len(alerts) == 1 and alerts[0]["level"] == "urgent" and alerts[0]["kind"] == "broker"

    # 配置缺失导致连接失败也一样：拒单而不是抛异常
    bad = qmt.QmtBroker(make_account("qmt"), {"live": {}})
    with ledger.connect() as c:
        order2 = bad.place(c, OrderRequest("600000", "buy", 100, "limit", 10.0), ref_price=10.0)
        assert order2["status"] == "rejected"


def test_qmt_sync_dedups_fills_and_marks_broker_cancelled(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, xtconstant = qmt_broker(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        trader = b._client().trader
        trader.trades = [FakeXtTrade("T1", int(order["broker_order_id"]), "600000.SH", 1000, 9.98, xtconstant.STOCK_BUY)]
        res = b.sync(c)
        assert res == {"fills": 1, "message": "同步完成，新增 1 笔成交"}
        assert ledger.one(c, "SELECT qty FROM positions WHERE account_id=?", (b.account["id"],))["qty"] == 1000
        assert b.sync(c)["fills"] == 0                       # 再同步一次不重复记账
        assert ledger.one(c, "SELECT qty FROM positions WHERE account_id=?", (b.account["id"],))["qty"] == 1000

        order2 = b.place(c, OrderRequest("600001", "buy", 500, "limit", 10.0), ref_price=10.0)
        trader.query_orders = [FakeXtOrder(int(order2["broker_order_id"]), xtconstant.ORDER_CANCELED)]
        b.sync(c)
        assert ledger.one(c, "SELECT status FROM orders WHERE id=?", (order2["id"],))["status"] == "cancelled"

    broken = qmt.QmtBroker(make_account("qmt"), {"live": {}})
    result = broken.sync(None)                                          # 失败不抛异常，返回中文原因（conn 都用不上）
    assert result["fills"] == 0 and "失败" in result["error"]


def test_qmt_cancel_calls_broker_then_ledger(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, _ = qmt_broker(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        result = b.cancel(c, order["id"])
        assert result["status"] == "cancelled"
        assert b._client().trader.cancel_calls == [int(order["broker_order_id"])]

        b._client().trader.cancel_result = -1
        order2 = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        with pytest.raises(ValueError, match="撤单失败"):
            b.cancel(c, order2["id"])


def test_qmt_remote_snapshot(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, _ = qmt_broker(monkeypatch)
    trader = b._client().trader

    class FakeAsset:
        cash, frozen_cash, market_value, total_asset = 50_000.0, 1_000.0, 20_000.0, 71_000.0

    class FakePosition:
        stock_code, volume, can_use_volume, open_price, market_value = "600000.SH", 1000, 800, 9.5, 10_000.0

    trader.asset, trader.positions = FakeAsset(), [FakePosition()]
    assert b.remote_snapshot() == {
        "cash": 50_000.0, "frozen": 1_000.0, "market_value": 20_000.0, "total": 71_000.0,
        "positions": [{"code": "600000", "qty": 1000, "available": 800, "cost": 9.5, "market_value": 10_000.0}],
    }
    assert qmt.QmtBroker(make_account("qmt"), {"live": {}}).remote_snapshot() is None


# ================================================================ 同花顺（easytrader）

class FakeThsUser:
    """记录调用参数的假 easytrader 客户端"""

    def __init__(self) -> None:
        self.connected_path: str | None = None
        self.fail_connect = False
        self.buy_calls: list[dict] = []
        self.sell_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.today_trades: list[dict] = []
        self.balance: dict = {}
        self.position: list[dict] = []
        self._next_no = 9000
        self.buy = self._buy                # 允许测试里整个替换成会抛异常的函数
        self.sell = self._sell

    def connect(self, path: str) -> None:
        if self.fail_connect:
            raise RuntimeError("同花顺下单程序没有打开")
        self.connected_path = path

    def _entrust(self) -> dict:
        self._next_no += 1
        return {"entrust_no": str(self._next_no)}

    def _buy(self, code, price, amount):
        r = self._entrust()
        self.buy_calls.append({"code": code, "price": price, "amount": amount})
        return r

    def _sell(self, code, price, amount):
        r = self._entrust()
        self.sell_calls.append({"code": code, "price": price, "amount": amount})
        return r

    def cancel_entrust(self, entrust_no: str) -> None:
        self.cancel_calls.append(entrust_no)


def install_fake_easytrader(monkeypatch: pytest.MonkeyPatch, user: FakeThsUser, use_calls: list | None = None) -> None:
    module = types.ModuleType("easytrader")

    def use(name: str):
        if use_calls is not None:
            use_calls.append(name)
        return user

    module.use = use
    monkeypatch.setitem(sys.modules, "easytrader", module)


def easytrader_client(monkeypatch: pytest.MonkeyPatch, cash: float = 100_000) -> tuple[easytrader_broker.EasytraderBroker, FakeThsUser]:
    user = FakeThsUser()
    install_fake_easytrader(monkeypatch, user)
    acc = make_account("easytrader", cash)
    b = easytrader_broker.EasytraderBroker(acc, {"live": {"easytrader_client": "C:/ths/xiadan.exe"}})
    return b, user


def test_easytrader_available_messages(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = easytrader_broker.EasytraderBroker({"id": "x", "broker": "easytrader"}, {"live": {}})
    ok, why = b.available()
    assert not ok and "easytrader" in why
    install_fake_easytrader(monkeypatch, FakeThsUser())
    b2 = easytrader_broker.EasytraderBroker({"id": "x", "broker": "easytrader"}, {"live": {}})
    ok2, why2 = b2.available()
    assert not ok2 and "xiadan.exe" in why2
    b3 = easytrader_broker.EasytraderBroker({"id": "x", "broker": "easytrader"}, {"live": {"easytrader_client": "C:/x.exe"}})
    assert b3.available() == (True, "")
    assert "实验" in easytrader_broker.EasytraderBroker.description


def test_easytrader_falls_back_to_ths_client_type(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = FakeThsUser()
    calls: list[str] = []

    def use(name: str):
        calls.append(name)
        if name == "universal_client":
            raise RuntimeError("这个版本不支持 universal_client")
        return user

    module = types.ModuleType("easytrader")
    module.use = use
    monkeypatch.setitem(sys.modules, "easytrader", module)
    acc = make_account("easytrader")
    b = easytrader_broker.EasytraderBroker(acc, {"live": {"easytrader_client": "C:/ths/xiadan.exe"}})
    client = b._client()
    assert client is user and calls == ["universal_client", "ths"] and user.connected_path == "C:/ths/xiadan.exe"


def test_easytrader_place_limit_buy(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, user = easytrader_client(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.5), ref_price=10.4)
        assert order["status"] == "submitted" and order["broker_order_id"]
        assert user.buy_calls[-1] == {"code": "600000", "price": 10.5, "amount": 1000}


def test_easytrader_market_sell_uses_protective_limit_and_limit_down(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, user = easytrader_client(monkeypatch)
    with ledger.connect() as c:
        _seed_position(c, b.account["id"], "600000", 1000, 9.0)
        _seed_position(c, b.account["id"], "600001", 1000, 9.0)
        b.place(c, OrderRequest("600000", "sell", 300, "market"), ref_price=10.0)
        assert user.sell_calls[-1]["price"] == 9.8
        b2 = easytrader_broker.EasytraderBroker(b.account, {"live": {"easytrader_client": "C:/ths/xiadan.exe"},
                                                            "quote": {"limit_down": 9.9}})
        b2.place(c, OrderRequest("600001", "sell", 300, "market"), ref_price=10.0)
        assert user.sell_calls[-1]["price"] == 9.9


def test_easytrader_stop_order_not_sent_to_broker(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, user = easytrader_client(monkeypatch)
    with ledger.connect() as c:
        _seed_position(c, b.account["id"], "600000", 1000, 9.0)
        order = b.place(c, OrderRequest("600000", "sell", 500, "stop", trigger=9.0))
        assert order["status"] == "waiting_trigger" and order["broker_order_id"] is None
    assert user.sell_calls == [] and easytrader_broker._CLIENTS == {}


def test_easytrader_failure_rejects_order_and_alerts(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, user = easytrader_client(monkeypatch)

    def boom(code, price, amount):
        raise RuntimeError("下单窗口没有弹出（同花顺被切到后台）")

    # 发送时出错：券商那边可能已经收到了——不能当成"被拒绝"（否则再下一次就可能变成两笔），保持已提交并紧急提醒去核对
    user.buy = boom
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        assert order["status"] == "submitted" and "结果不确定" in order["message"]
        alerts = ledger.rows(c, "SELECT * FROM alerts WHERE account_id=?", (b.account["id"],))
        assert len(alerts) == 1 and alerts[0]["level"] == "urgent" and alerts[0]["kind"] == "broker" and "重复下单" in alerts[0]["title"]

    # entrust_no 拿不到（字典里没有这个键）：同样不确定
    b2, user2 = easytrader_client(monkeypatch)
    user2.buy = lambda code, price, amount: {"其他字段": "1"}
    with ledger.connect() as c:
        order2 = b2.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        assert order2["status"] == "submitted" and "结果不确定" in order2["message"]

    # 还没发出去就出错（连不上客户端）：确定没有下单 → 拒绝
    b3, _ = easytrader_client(monkeypatch)

    def no_client():
        raise RuntimeError("连不上同花顺")
    b3._client = no_client
    with ledger.connect() as c:
        order3 = b3.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        assert order3["status"] == "rejected" and "券商下单失败" in order3["message"]


def test_easytrader_sync_dedups_and_books_untracked_trade(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, user = easytrader_client(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        user.today_trades = [{"证券代码": "600000", "证券名称": "浦发银行", "买卖标志": "证券买入", "成交数量": "1000",
                              "成交均价": "9.98", "成交编号": "TID1", "合同编号": order["broker_order_id"]}]
        res = b.sync(c)
        assert res == {"fills": 1, "message": "同步完成，新增 1 笔成交"}
        assert b.sync(c)["fills"] == 0                       # 不重复记账
        assert ledger.one(c, "SELECT qty FROM positions WHERE account_id=?", (b.account["id"],))["qty"] == 1000

        # 用户直接在同花顺里手动买的（账本里没有对应委托）：照样按无挂单成交记账
        user.today_trades.append({"证券代码": "000001", "操作": "买入", "成交数量": "100", "成交价格": "11.2",
                                  "成交编号": "TID2"})
        res2 = b.sync(c)
        assert res2["fills"] == 1
        pos = ledger.one(c, "SELECT * FROM positions WHERE account_id=? AND code=?", (b.account["id"], "000001"))
        assert pos["qty"] == 100

    assert easytrader_broker.EasytraderBroker(make_account("easytrader"), {"live": {}}).sync(None)["fills"] == 0


def test_easytrader_cancel_calls_broker(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, user = easytrader_client(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        result = b.cancel(c, order["id"])
        assert result["status"] == "cancelled" and user.cancel_calls == [order["broker_order_id"]]


def test_easytrader_remote_snapshot(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b, user = easytrader_client(monkeypatch)
    user.balance = {"可用金额": "50000", "总资产": "71000", "股票市值": "20000"}
    user.position = [{"证券代码": "600000", "股票余额": "1000", "可用余额": "800", "成本价": "9.5", "市值": "10000"}]
    assert b.remote_snapshot() == {
        "cash": 50_000.0, "frozen": 0.0, "market_value": 20_000.0, "total": 71_000.0,
        "positions": [{"code": "600000", "qty": 1000, "available": 800, "cost": 9.5, "market_value": 10_000.0}],
    }


# ================================================================ vnpy 网关

class FakeGateway:
    default_name = "FAKE"
    exchanges: list = []

    def __init__(self, event_engine: object, gateway_name: str) -> None:
        self.event_engine, self.gateway_name = event_engine, gateway_name


def install_fake_gateway_module(monkeypatch: pytest.MonkeyPatch, module_name: str = "fake_vnpy_gateway") -> None:
    module = types.ModuleType(module_name)
    module.FakeGateway = FakeGateway
    monkeypatch.setitem(sys.modules, module_name, module)


class FakeEventEngine:
    pass


class FakeMainEngine:
    """记录调用参数的假 MainEngine；只实现 vnpy_bridge 用到的那几个方法"""

    def __init__(self, event_engine: object) -> None:
        self.event_engine = event_engine
        self.connect_calls: list[tuple] = []
        self.send_order_calls: list[tuple] = []
        self.cancel_calls: list[tuple] = []
        self.fail_send = False
        self.orders: dict[str, object] = {}
        self.trades: list[object] = []
        self.accounts: list[object] = []
        self.positions: list[object] = []
        self._next_id = 1

    def add_gateway(self, cls: type, gateway_name: str) -> object:
        return cls(self.event_engine, gateway_name)

    def connect(self, setting: dict, gateway_name: str) -> None:
        self.connect_calls.append((setting, gateway_name))

    def send_order(self, req: object, gateway_name: str) -> str:
        self.send_order_calls.append((req, gateway_name))
        if self.fail_send:
            return ""
        vt_orderid = f"{gateway_name}.{self._next_id}"
        self._next_id += 1
        return vt_orderid

    def get_order(self, vt_orderid: str) -> object:
        return self.orders.get(vt_orderid)

    def cancel_order(self, req: object, gateway_name: str) -> None:
        self.cancel_calls.append((req, gateway_name))

    def get_all_trades(self) -> list:
        return self.trades

    def get_all_orders(self) -> list:
        return list(self.orders.values())

    def get_all_accounts(self) -> list:
        return self.accounts

    def get_all_positions(self) -> list:
        return self.positions


def install_fake_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vnpy_bridge, "EventEngine", FakeEventEngine)
    monkeypatch.setattr(vnpy_bridge, "MainEngine", FakeMainEngine)


def vnpy_client(monkeypatch: pytest.MonkeyPatch, cash: float = 100_000) -> vnpy_bridge.VnpyGatewayBroker:
    install_fake_gateway_module(monkeypatch)
    install_fake_engines(monkeypatch)
    acc = make_account("vnpy_gateway", cash)
    live = {"vnpy_gateway": "fake_vnpy_gateway.FakeGateway", "vnpy_setting": {"账号": "1"}}
    return vnpy_bridge.VnpyGatewayBroker(acc, {"live": live})


def test_vnpy_available_messages(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = vnpy_bridge.VnpyGatewayBroker({"id": "x", "broker": "vnpy_gateway"}, {"live": {}})
    ok, why = b.available()
    assert not ok and "网关" in why
    b2 = vnpy_bridge.VnpyGatewayBroker({"id": "x", "broker": "vnpy_gateway"},
                                       {"live": {"vnpy_gateway": "nope.NoGateway", "vnpy_setting": {"x": 1}}})
    ok2, why2 = b2.available()
    assert not ok2 and "没有找到" in why2
    install_fake_gateway_module(monkeypatch)
    b3 = vnpy_bridge.VnpyGatewayBroker({"id": "x", "broker": "vnpy_gateway"},
                                       {"live": {"vnpy_gateway": "fake_vnpy_gateway.FakeGateway", "vnpy_setting": {"账号": "1"}}})
    assert b3.available() == (True, "")
    assert "实验" in vnpy_bridge.VnpyGatewayBroker.description or "高级" in vnpy_bridge.VnpyGatewayBroker.description


def test_vnpy_place_limit_buy(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = vnpy_client(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.5), ref_price=10.4)
        assert order["status"] == "submitted" and order["broker_order_id"] == "FAKE.1"
        req, gw = b._client().engine.send_order_calls[-1]
        assert gw == "FAKE" and req.symbol == "600000" and req.price == 10.5 and req.volume == 1000
        assert req.direction == Direction.LONG and req.exchange == Exchange.SSE and req.type == OrderType.LIMIT
        assert req.reference == "quant_web"


def test_vnpy_market_sell_uses_protective_limit_and_limit_down(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = vnpy_client(monkeypatch)
    with ledger.connect() as c:
        _seed_position(c, b.account["id"], "000001", 1000, 9.0)
        _seed_position(c, b.account["id"], "000002", 1000, 9.0)
        b.place(c, OrderRequest("000001", "sell", 300, "market"), ref_price=10.0)
        req1, _ = b._client().engine.send_order_calls[-1]
        assert req1.price == 9.8 and req1.direction == Direction.SHORT and req1.exchange == Exchange.SZSE
        b2 = vnpy_bridge.VnpyGatewayBroker(b.account, {"live": {"vnpy_gateway": "fake_vnpy_gateway.FakeGateway",
                                                                "vnpy_setting": {"账号": "1"}}, "quote": {"limit_down": 9.9}})
        b2.place(c, OrderRequest("000002", "sell", 300, "market"), ref_price=10.0)
        req2, _ = b._client().engine.send_order_calls[-1]
        assert req2.price == 9.9


def test_vnpy_stop_order_not_sent_to_broker(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = vnpy_client(monkeypatch)
    with ledger.connect() as c:
        _seed_position(c, b.account["id"], "600000", 1000, 9.0)
        order = b.place(c, OrderRequest("600000", "sell", 500, "stop", trigger=9.0))
        assert order["status"] == "waiting_trigger" and order["broker_order_id"] is None
    assert vnpy_bridge._CLIENTS == {}


def test_vnpy_send_order_failure_rejects_and_alerts(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = vnpy_client(monkeypatch)
    b._client().engine.fail_send = True
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        assert order["status"] == "rejected" and "券商下单失败" in order["message"]
        alerts = ledger.rows(c, "SELECT * FROM alerts WHERE account_id=?", (b.account["id"],))
        assert len(alerts) == 1 and alerts[0]["level"] == "urgent" and alerts[0]["kind"] == "broker"


def test_vnpy_sync_dedups_fills_and_marks_broker_cancelled(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = vnpy_client(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        me = b._client().engine

        class FakeTrade:
            vt_tradeid, vt_orderid, symbol = "T1", order["broker_order_id"], "600000"
            direction, price, volume = Direction.LONG, 9.98, 1000

        me.trades = [FakeTrade()]
        res = b.sync(c)
        assert res == {"fills": 1, "message": "同步完成，新增 1 笔成交"}
        assert ledger.one(c, "SELECT qty FROM positions WHERE account_id=?", (b.account["id"],))["qty"] == 1000
        assert b.sync(c)["fills"] == 0

        order2 = b.place(c, OrderRequest("600001", "buy", 500, "limit", 10.0), ref_price=10.0)

        class FakeOrderData:
            vt_orderid, status = order2["broker_order_id"], Status.CANCELLED

        me.orders = {order2["broker_order_id"]: FakeOrderData()}
        b.sync(c)
        assert ledger.one(c, "SELECT status FROM orders WHERE id=?", (order2["id"],))["status"] == "cancelled"


def test_vnpy_cancel_calls_broker(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = vnpy_client(monkeypatch)
    with ledger.connect() as c:
        order = b.place(c, OrderRequest("600000", "buy", 1000, "limit", 10.0), ref_price=10.0)
        me = b._client().engine

        class FakeOrderData:
            vt_orderid = order["broker_order_id"]

            def create_cancel_request(self):
                return "CANCEL_REQ"

        me.orders = {order["broker_order_id"]: FakeOrderData()}
        result = b.cancel(c, order["id"])
        assert result["status"] == "cancelled"
        assert me.cancel_calls == [("CANCEL_REQ", "FAKE")]

        order2 = b.place(c, OrderRequest("600000", "buy", 500, "limit", 10.0), ref_price=10.0)
        me.orders = {}                                       # 网关那边已经查不到这笔委托了
        with pytest.raises(ValueError, match="找不到"):
            b.cancel(c, order2["id"])


def test_vnpy_remote_snapshot(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = vnpy_client(monkeypatch)
    me = b._client().engine

    class FakeAccountData:
        balance, frozen = 50_000.0, 1_000.0

    class FakePositionData:
        symbol, volume, frozen, price = "600000", 1000, 200, 9.5

    me.accounts, me.positions = [FakeAccountData()], [FakePositionData()]
    snap = b.remote_snapshot()
    assert snap["cash"] == 50_000.0 and snap["frozen"] == 1_000.0 and snap["market_value"] == 9_500.0
    assert snap["total"] == 59_500.0
    assert snap["positions"] == [{"code": "600000", "qty": 1000, "available": 800, "cost": 9.5, "market_value": 9_500.0}]
    assert vnpy_bridge.VnpyGatewayBroker(make_account("vnpy_gateway"), {"live": {}}).remote_snapshot() is None


# ================================================================ 接口清单

def test_listing_has_all_five_and_optional_ones_are_unavailable(ws: Path) -> None:
    out = brokers.listing({"live": {}})
    by_name = {b["name"]: b for b in out}
    assert set(by_name) == {"paper", "manual", "qmt", "easytrader", "vnpy_gateway"}
    assert by_name["paper"]["available"] and not by_name["paper"]["optional"]
    assert by_name["manual"]["available"] and not by_name["manual"]["optional"]
    for name in ("qmt", "easytrader", "vnpy_gateway"):
        row = by_name[name]
        assert row["is_live"] and row["can_auto"] and row["optional"] is True
        assert row["available"] is False and row["reason"] and "接口加载失败" not in row["reason"]
