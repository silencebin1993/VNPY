"""
逐笔成交模拟（向量化，不考虑资金与仓位上限，每个信号独立成交）。与 backtest.run_backtest 同一套成交规则与费用：

- T 日收盘出信号 → T+1（市场的下一个交易日）开盘买入；T+1 停牌、开盘即涨停（含一字）买不到；
  给了 max_gap_pct 时开盘涨幅超过它（%）放弃；
- 卖出（买入当天不能卖；"第 k 根"指买入后该股自己的第 k 根K线，买入日 k=1，停牌日不算）：
  next_open = 第2根开盘；next_close = 第2根收盘（收盘跌停卖不出 → 下一根开盘）；
  until_break = 某根收盘没涨停 → 下一根开盘卖，最多持有 10 根；
  until_break_close = 从第2根起，第一个收盘没涨停的那根按收盘价卖，第5根收盘强制卖；
  要卖的那根收盘跌停则顺延到下一根收盘——一旦决定卖出就"锁定"，下一根收盘不管涨停与否都卖
  （不会因为下一根又收涨停而改成继续拿），仍跌停就再顺延，第7根仍跌停也按收盘价计；
- 止损（stop_loss_pct>0，止损价 = 含滑点买价×(1-止损比例)）：从第2根起开盘价 ≤ 止损价 → 开盘卖；
  盘中最低 ≤ 止损价 → 按 min(开盘价, 止损价) 卖；
- 要在开盘卖但开盘跌停：当日最高价 > 跌停价（开盘后打开过）按开盘（跌停）价成交；一字跌停卖不出，顺延到下一根开盘；
- 买入后股票退市（没有更多K线且在已退市名单里）→ 按最后一根收盘价计；其余没有更多K线的 → "holding"（按最新收盘价估算）。
- 买不进的原因：买入日停牌（SUSPENDED_TEXT）；开盘就涨停——全天最低价也在涨停价（一字板，ONE_WORD_TEXT）
  或开盘涨停后打开过（LIMIT_UP_TEXT）；开盘涨幅超过上限。
- 费用见 costs 模块（佣金、按日期的印花税、滑点）；entry/exit 为含滑点的成交价。
"""
from collections.abc import Iterable
from datetime import date

import numpy as np
import polars as pl

from .costs import DEFAULT, Costs


TOLERANCE: float = 0.005
MAX_HOLD_DAYS: int = 10             # until_break 最多持有根数
SWING_FORCE_K: int = 5              # until_break_close：第5根收盘强制卖出
SWING_MAX_K: int = 7                # until_break_close：跌停顺延最多到第7根
SCAN_LIMIT: int = 60                # 最多往后看这么多根K线（连续一字跌停等极端情况），之后按收盘价计
EXIT_RULES: tuple[str, ...] = ("next_open", "next_close", "until_break", "until_break_close")
STATUSES: tuple[str, ...] = ("pending", "unfilled", "holding", "closed")

# 卖出原因代码 → 中文
R_NEXT_OPEN, R_NEXT_CLOSE, R_BREAK, R_MAX_HOLD, R_STOP_OPEN, R_STOP_INTRADAY = 1, 2, 3, 4, 5, 6
R_BREAK_CLOSE, R_FORCE_CLOSE, R_LD_FORCE, R_DELISTED, R_SCAN_FORCE = 7, 8, 9, 10, 11
REASON_TEXT: dict[int, str] = {
    R_NEXT_OPEN: "第二天开盘卖出",
    R_NEXT_CLOSE: "第二天收盘卖出",
    R_BREAK: "涨停断板，次日开盘卖出",
    R_MAX_HOLD: f"持有满{MAX_HOLD_DAYS}天，开盘卖出",
    R_STOP_OPEN: "开盘跌破止损价，开盘止损",
    R_STOP_INTRADAY: "盘中跌到止损价，止损卖出",
    R_BREAK_CLOSE: "收盘没有涨停，按收盘价卖出",
    R_FORCE_CLOSE: f"持有到第{SWING_FORCE_K}个交易日，收盘卖出",
    R_LD_FORCE: f"连续跌停卖不出，第{SWING_MAX_K}个交易日按收盘价计",
    R_DELISTED: "股票已退市（之后没有行情），按最后收盘价计",
    R_SCAN_FORCE: "长期卖不出，按最后收盘价计",
}
DELAYED_SUFFIX: str = "（曾因跌停卖不出顺延）"
PENDING_TEXT: str = "等待下一个交易日开盘买入"
HOLDING_TEXT: str = "持有中（收益按最新收盘价估算）"
SUSPENDED_TEXT: str = "买入日停牌，没买到"
LIMIT_UP_TEXT: str = "开盘就涨停，买不进"
ONE_WORD_TEXT: str = "一字板，买不进"
BAD_OPEN_TEXT: str = "买入日没有有效的开盘价，没买到"

OUT_SCHEMA: dict[str, pl.DataType] = {
    "status": pl.Utf8, "entry_date": pl.Date, "entry": pl.Float64, "exit_date": pl.Date, "exit": pl.Float64,
    "ret": pl.Float64, "hold_days": pl.Int32, "exit_reason": pl.Utf8,
}
_PRICE_COLS: list[str] = ["open", "high", "low", "close", "preclose", "limit_up", "limit_down"]
_RULE_CODE: dict[str, int] = {r: i for i, r in enumerate(EXIT_RULES)}


def _empty_out(signals: pl.DataFrame) -> pl.DataFrame:
    return signals.with_columns([pl.lit(None, t).alias(c) for c, t in OUT_SCHEMA.items()])


def load_delisted() -> set[str]:
    """已退市股票代码（股票列表 status==0）；读不到时为空集合"""
    try:
        from ..market.universe import load_universe

        uni: pl.DataFrame = load_universe()
        if uni.is_empty() or "status" not in uni.columns:
            return set()
        return set(uni.filter(pl.col("status") == 0)["code"].to_list())
    except Exception:  # noqa: BLE001
        return set()


def market_calendar(panel_lim: pl.DataFrame) -> np.ndarray:
    """面板里出现过的全部交易日（datetime64[D]，升序）"""
    return np.sort(panel_lim["date"].unique().to_numpy().astype("datetime64[D]"))


def simulate(
    signals: pl.DataFrame,
    panel_lim: pl.DataFrame,
    *,
    costs: Costs = DEFAULT,
    exit_rule: str = "next_close",
    stop_loss_pct: float = 0.0,
    max_gap_pct: float | None = None,
    delisted: Iterable[str] | None = None,
    calendar: np.ndarray | None = None,
) -> pl.DataFrame:
    """signals: signal_date, code（可选 exit_rule、stop_loss_pct 列，逐行覆盖参数）。
    返回 signals 原有列 + status, entry_date, entry, exit_date, exit, ret, hold_days, exit_reason（见模块说明）。"""
    n: int = signals.height
    if n == 0:
        return _empty_out(signals)
    if exit_rule not in _RULE_CODE:
        raise ValueError(f"不认识的卖出规则：{exit_rule}")
    sig: pl.DataFrame = signals.select(
        pl.col("signal_date").cast(pl.Date), pl.col("code").cast(pl.Utf8),
        (pl.col("exit_rule").cast(pl.Utf8).fill_null(exit_rule) if "exit_rule" in signals.columns
         else pl.lit(exit_rule)).alias("_rule"),
        (pl.col("stop_loss_pct").cast(pl.Float64).fill_null(float(stop_loss_pct or 0.0))
         if "stop_loss_pct" in signals.columns else pl.lit(float(stop_loss_pct or 0.0))).alias("_stop"),
    )
    bad: list[str] = [r for r in sig["_rule"].unique().to_list() if r not in _RULE_CODE]
    if bad:
        raise ValueError(f"不认识的卖出规则：{'、'.join(map(str, bad))}")
    delisted_set: set[str] = load_delisted() if delisted is None else set(delisted)
    cal: np.ndarray = market_calendar(panel_lim) if calendar is None else np.asarray(calendar, "datetime64[D]")
    # 买入日 = 信号日之后的第一个交易日
    sd: np.ndarray = sig["signal_date"].to_numpy().astype("datetime64[D]")
    pos: np.ndarray = np.searchsorted(cal, sd, side="right")
    pending: np.ndarray = pos >= len(cal)
    entry_day: np.ndarray = np.where(pending, np.datetime64("NaT", "D"), cal[np.minimum(pos, max(len(cal) - 1, 0))]) \
        if len(cal) else np.full(n, np.datetime64("NaT", "D"), "datetime64[D]")
    if not len(cal):
        pending = np.ones(n, dtype=bool)

    px: pl.DataFrame = (
        panel_lim.select(["date", "code", *_PRICE_COLS])
        .filter(pl.col("code").is_in(sig["code"].unique().implode()))
        .sort(["code", "date"])
        .with_row_index("_i")
        .with_columns(pl.col("_i").max().over("code").alias("_end"))
    )
    arr: dict[str, np.ndarray] = {c: px[c].cast(pl.Float64).fill_null(np.nan).to_numpy() for c in _PRICE_COLS}
    pdates: np.ndarray = px["date"].to_numpy().astype("datetime64[D]")
    code_end: np.ndarray = px["_end"].cast(pl.Int64).to_numpy()
    look: pl.DataFrame = pl.DataFrame({"_k": np.arange(n), "code": sig["code"],
                                       "date": pl.Series(entry_day).cast(pl.Date)}).join(
        px.select("date", "code", "_i"), on=["date", "code"], how="left").sort("_k")
    e: np.ndarray = look["_i"].fill_null(-1).cast(pl.Int64).to_numpy()
    has_bar: np.ndarray = (e >= 0) & ~pending
    ee: np.ndarray = np.where(has_bar, e, 0)

    def at(col: str, idx: np.ndarray) -> np.ndarray:
        return arr[col][idx] if len(arr[col]) else np.full(len(idx), np.nan)

    o1, lu1, pc1, low1 = at("open", ee), at("limit_up", ee), at("preclose", ee), at("low", ee)
    at_lu: np.ndarray = has_bar & (o1 >= lu1 - TOLERANCE)
    one_word_lu: np.ndarray = at_lu & (low1 >= lu1 - TOLERANCE)         # 全天都在涨停价：一字板
    bad_open: np.ndarray = has_bar & ~(o1 > 0)
    gap_pct: np.ndarray = np.where(pc1 > 0, (o1 / np.where(pc1 > 0, pc1, 1.0) - 1) * 100, 0.0)
    gap_skip: np.ndarray = has_bar & ~at_lu & ~bad_open & (max_gap_pct is not None) & \
        (gap_pct > (max_gap_pct if max_gap_pct is not None else np.inf))
    unfilled: np.ndarray = ~pending & (~has_bar | at_lu | bad_open | gap_skip)
    filled: np.ndarray = ~pending & ~unfilled

    rule: np.ndarray = np.array([_RULE_CODE[r] for r in sig["_rule"].to_list()], dtype=np.int8)
    stop: np.ndarray = sig["_stop"].to_numpy().astype(float)
    entry_fill: np.ndarray = costs.buy_price(o1)
    stop_price: np.ndarray = np.where(filled & (stop > 0), entry_fill * (1 - stop), np.nan)
    end_of_code: np.ndarray = np.where(has_bar, code_end[ee] if len(code_end) else 0, -1)
    is_delisted: np.ndarray = np.array([c in delisted_set for c in sig["code"].to_list()], dtype=bool)

    done: np.ndarray = ~filled
    pend: np.ndarray = np.zeros(n, dtype=np.int8)          # 计划在下一根开盘卖出的原因
    close_exit: np.ndarray = np.zeros(n, dtype=bool)        # 计划在下一根收盘卖出（next_close）
    committed: np.ndarray = np.zeros(n, dtype=np.int8)      # until_break_close：已决定卖出、因跌停顺延时的原因
    delayed: np.ndarray = np.zeros(n, dtype=bool)
    exit_raw: np.ndarray = np.full(n, np.nan)
    exit_idx: np.ndarray = np.full(n, -1, dtype=np.int64)
    reason: np.ndarray = np.zeros(n, dtype=np.int8)
    k_exit: np.ndarray = np.zeros(n, dtype=np.int64)
    holding: np.ndarray = np.zeros(n, dtype=bool)

    def book(mask: np.ndarray, price: np.ndarray, idx: np.ndarray, why: np.ndarray | int, k: np.ndarray | int) -> None:
        exit_raw[mask] = price[mask]
        exit_idx[mask] = idx[mask]
        reason[mask] = why[mask] if isinstance(why, np.ndarray) else why
        k_exit[mask] = k[mask] if isinstance(k, np.ndarray) else k
        done[mask] = True

    # k = 1：买入当天收盘（不能卖，只定计划）
    c1: np.ndarray = at("close", ee)
    pend[filled & (rule == _RULE_CODE["next_open"])] = R_NEXT_OPEN
    close_exit[filled & (rule == _RULE_CODE["next_close"])] = True
    pend[filled & (rule == _RULE_CODE["until_break"]) & (c1 < lu1 - TOLERANCE)] = R_BREAK

    for k in range(2, SCAN_LIMIT + 1):
        active: np.ndarray = ~done
        if not active.any():
            break
        j: np.ndarray = ee + k - 1
        has: np.ndarray = active & (j <= end_of_code)
        gone: np.ndarray = active & ~has
        if gone.any():                          # 没有更多K线：退市 → 最后收盘价；否则持有中
            last: np.ndarray = np.where(gone, end_of_code, 0)
            lastc: np.ndarray = at("close", last)
            book(gone & is_delisted, lastc, last, R_DELISTED, last - ee + 1)
            hold: np.ndarray = gone & ~is_delisted
            holding[hold] = True
            exit_raw[hold] = lastc[hold]
            exit_idx[hold] = last[hold]
            k_exit[hold] = (last - ee + 1)[hold]
            done[hold] = True
        if not has.any():
            continue
        jj: np.ndarray = np.where(has, j, 0)
        o, h, lo, c = at("open", jj), at("high", jj), at("low", jj), at("close", jj)
        lu, ld = at("limit_up", jj), at("limit_down", jj)
        ld_close: np.ndarray = c <= ld + TOLERANCE
        one_word_ld: np.ndarray = h <= ld + TOLERANCE

        # 1) 开盘：计划卖出 / 开盘止损
        why: np.ndarray = pend.copy()
        why[has & (why == 0) & (o <= stop_price)] = R_STOP_OPEN
        want: np.ndarray = has & (why > 0)
        blocked: np.ndarray = want & (o <= ld + TOLERANCE) & one_word_ld        # 一字跌停卖不出
        book(want & ~blocked, o, jj, why, k)
        pend[blocked] = why[blocked]
        delayed[blocked] = True

        # 2) 盘中止损
        stage: np.ndarray = has & ~done
        hit: np.ndarray = stage & (lo <= stop_price)
        stuck: np.ndarray = hit & one_word_ld
        pend[stuck] = R_STOP_INTRADAY
        delayed[stuck] = True
        book(hit & ~stuck, np.minimum(o, stop_price), jj, R_STOP_INTRADAY, k)
        stage &= ~hit
        # 3) 收盘：next_close
        ce: np.ndarray = stage & close_exit
        roll: np.ndarray = ce & ld_close
        close_exit[roll] = False
        pend[roll] = R_NEXT_CLOSE
        delayed[roll] = True
        book(ce & ~roll, c, jj, R_NEXT_CLOSE, k)
        stage &= ~ce & (pend == 0)
        # 4) 收盘：until_break（断板 → 下一根开盘卖）
        ub: np.ndarray = stage & (rule == _RULE_CODE["until_break"])
        brk: np.ndarray = ub & (c < lu - TOLERANCE)
        pend[brk] = R_BREAK
        pend[ub & ~brk & (k >= MAX_HOLD_DAYS)] = R_MAX_HOLD
        # 5) 收盘：until_break_close（首个非涨停收盘卖，第5根强制，跌停顺延到第7根）
        #    已因跌停顺延过的（committed）这根收盘必卖：不再看是否涨停
        ubc: np.ndarray = stage & (rule == _RULE_CODE["until_break_close"])
        not_lu: np.ndarray = ~(c >= lu - TOLERANCE)
        go: np.ndarray = ubc & ((committed > 0) | not_lu | (k >= SWING_FORCE_K))
        why_ubc: np.ndarray = np.where(committed > 0, committed,
                                       np.where(not_lu, R_BREAK_CLOSE, R_FORCE_CLOSE)).astype(np.int8)
        wait: np.ndarray = go & ld_close & (k < SWING_MAX_K)
        delayed[wait] = True
        committed[wait] = why_ubc[wait]
        sell: np.ndarray = go & ~wait
        code_ubc: np.ndarray = np.where(ld_close & (k >= SWING_MAX_K), R_LD_FORCE, why_ubc).astype(np.int8)
        book(sell, c, jj, code_ubc, k)
        if k == SCAN_LIMIT:
            rest: np.ndarray = has & ~done
            book(rest, c, jj, R_SCAN_FORCE, k)

    # ---- 输出
    closed: np.ndarray = filled & done & ~holding & (exit_idx >= 0)
    live: np.ndarray = closed | holding
    xi: np.ndarray = np.where(live, exit_idx, 0)
    xdate: np.ndarray = np.where(live, pdates[xi] if len(pdates) else np.datetime64("NaT", "D"), np.datetime64("NaT", "D"))
    ret: np.ndarray = np.where(live, costs.net_return(o1, exit_raw, xdate), np.nan)
    status: np.ndarray = np.select([pending, unfilled, holding], ["pending", "unfilled", "holding"], "closed")
    texts: list[str | None] = []
    for i in range(n):
        if pending[i]:
            texts.append(PENDING_TEXT)
        elif unfilled[i]:
            if not has_bar[i]:
                texts.append(SUSPENDED_TEXT)
            elif bad_open[i]:
                texts.append(BAD_OPEN_TEXT)
            elif one_word_lu[i]:
                texts.append(ONE_WORD_TEXT)
            elif at_lu[i]:
                texts.append(LIMIT_UP_TEXT)
            else:
                texts.append(f"开盘涨幅 {gap_pct[i]:.1f}% 超过上限 {max_gap_pct:g}%，放弃买入")
        elif holding[i]:
            texts.append(HOLDING_TEXT)
        else:
            texts.append(REASON_TEXT.get(int(reason[i]), "卖出") + (DELAYED_SUFFIX if delayed[i] else ""))
    out: pl.DataFrame = pl.DataFrame({
        "status": status.astype(str),
        "entry_date": pl.Series(np.where(filled, entry_day, np.datetime64("NaT", "D")).astype("datetime64[D]"))
        .cast(pl.Date),
        "entry": pl.Series(np.where(filled, entry_fill, np.nan)).fill_nan(None),
        "exit_date": pl.Series(np.where(closed, xdate, np.datetime64("NaT", "D")).astype("datetime64[D]")).cast(pl.Date),
        "exit": pl.Series(np.where(closed, costs.sell_price(exit_raw), np.nan)).fill_nan(None),
        "ret": pl.Series(ret).fill_nan(None),
        "hold_days": pl.Series([int(k_exit[i] - 1) if live[i] else None for i in range(n)], dtype=pl.Int32),
        "exit_reason": pl.Series(texts, dtype=pl.Utf8),
    })
    keep: list[str] = [c for c in signals.columns if c not in OUT_SCHEMA]
    return signals.select(keep).hstack(out)


def last_calendar_day(panel_lim: pl.DataFrame) -> date | None:
    return panel_lim["date"].max() if panel_lim.height else None
