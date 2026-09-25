"""
交易复盘：每笔已平仓交易（盈亏、R 倍数、持有天数、违规）+ 汇总统计 + "人性陷阱"报告。
R 倍数 = (卖出价 − 买入价) ÷ (买入价 − 当初的止损价)：赚了 2R 就是赚了计划风险的 2 倍；长期平均 R > 0 才是赚钱的方法。
"""
from __future__ import annotations

import json
from collections import Counter

from . import ledger

TRAP_TEXT: dict[str, str] = {
    "挪低过止损": "止损是买之前冷静时定的，跌下来再改，往往是不想认错。",
    "追高买入": "当天大涨时买，常常买在短期高点。",
    "亏损时补仓摊平": "越跌越买会让一笔小亏变成大亏。",
    "超过计划持有天数": "说好的时间到了还拿着，多半是在等回本。",
    "无视风控提醒下单": "风控提醒过，还是下了单。",
    "没有交易计划": "没有止损价的买入，错了不知道在哪认输。",
}


def trades(account_id: str) -> list[dict]:
    with ledger.connect() as c:
        rows = ledger.rows(c, "SELECT * FROM journal WHERE account_id=? ORDER BY closed DESC, id DESC", (account_id,))
    for r in rows:
        r["violations"] = json.loads(r.get("violations") or "[]")
    return rows


def stats(account_id: str) -> dict:
    rows = trades(account_id)
    n: int = len(rows)
    if n == 0:
        return {"n": 0, "note": "还没有平仓的交易。每笔交易卖完后会自动出现在这里。"}
    pnl = [r["pnl"] or 0 for r in rows]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    rs = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
    streak = best = 0
    for x in reversed(pnl):                       # 时间正序数连续亏损
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    viol = Counter(v for r in rows for v in r["violations"])
    clean: int = sum(1 for r in rows if not r["violations"])
    return {
        "n": n, "win_rate": len(wins) / n, "total_pnl": sum(pnl), "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "avg_r": sum(rs) / len(rs) if rs else None, "avg_days": sum(r["days"] or 0 for r in rows) / n,
        "max_losing_streak": best, "discipline": clean / n,
        "traps": [{"name": k, "count": v, "text": TRAP_TEXT.get(k, "")} for k, v in viol.most_common()],
        "note": "胜率高不代表赚钱（赢小亏大照样亏）；平均 R 大于 0、并且违规越少越好。",
    }
