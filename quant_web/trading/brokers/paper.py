"""模拟盘：账本就是全部真相。下单后在交易时段用实时行情撮合（盘中监控每隔几十秒也会撮合），收盘后按日线补撮合。"""
from __future__ import annotations

import sqlite3

from .. import calendar as tcal
from ..engine import OrderRequest, match_with_quotes, place
from .base import Broker


class PaperBroker(Broker):
    name = "paper"
    label = "模拟盘"
    description = "用虚拟资金按真实规则交易：T+1、整手、手续费、涨跌停买卖不了、停牌不能交易、条件单。"
    is_live = False
    can_auto = True

    def place(self, conn: sqlite3.Connection, req: OrderRequest, ref_price: float | None = None) -> dict:
        o = place(conn, self.account["id"], req, ref_price=ref_price)
        quote: dict | None = self.settings.get("quote")
        if quote and tcal.session() == "open" and req.kind in ("limit", "market"):
            match_with_quotes(conn, self.account["id"], tcal.china_now().date(), {req.code: quote})
        return o
