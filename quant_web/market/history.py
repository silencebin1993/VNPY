"""
全市场日线面板：腾讯 newfqkline 不复权日K（多线程，约 20 只/秒）→ PANEL_DIR/{YYYY}.parquet。

昨收 preclose（涨跌幅、涨停价的基准），与 baostock/交易所逐条核对过：
- 普通交易日 = 上一根K线的不复权收盘价（停牌日腾讯K线直接没有，复牌日昨收 = 停牌前收盘）；
- 除权除息日：腾讯不复权K线在除权日附带 {"fh_sh": 每10股派息, "FHcontent": "10送3股, 10转2股, 10派1元"}：
  · 纯现金分红：交易所公式 (前收盘 - 每股派息)，四舍五入到分；
  · 有送转/配股：用前复权K线估算（q = A·raw + B 分段线性，拟合 A 后 参考价 = raw_t + (q_{t-1} - q_t)/A），
    能反映回购库存股导致的"虚拟"送转比例；配股与无公告除权日再用 baostock 复核（后台线程+硬超时）；
  · 数据自洽修正：除权日收盘价超出按参考价算的涨跌停价 ≤2 分钱（库存股"虚拟分派"）→ 参考价调 1~2 分钱；
    没有除权信息却超出涨跌停范围（破产重整转增等）→ 前复权估算，变化超过3%才采用。
  （约定文档里的"f=qfq/raw 等比换算"实测不准：腾讯前复权对现金分红做减法，不是等比。）
- 新股上市首日：preclose 为空，first_bars.parquet 记录首根日期，供股票列表补上市日期。
- 北交所：只保留 2021-11-15 开市后的行情；腾讯没有北交所的分红信息，小额分红日的昨收会略偏。

单位：成交量 手→股（科创板腾讯本身就是股，按成交额/均价自动识别），成交额 万元→元，换手率 %。
"""
import json
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import NamedTuple

import numpy as np
import polars as pl

from .. import config, net
from . import universe as uni_mod


KLINE_URL: str = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
QUOTE_URL: str = "https://qt.gtimg.cn/q="
RAW_PAGE: int = 2000            # 不复权一次最多 2000 根
QUOTE_BATCH: int = 500          # 腾讯报价每批最多约 800 只
CHECKPOINT_EVERY: int = 300     # 每下载这么多只股票落盘一次（中断可续传）
DEFAULT_WORKERS: int = 8
CLOSE_MINUTE: int = 15 * 60 + 5     # 北京时间 15:05 之后认为当天已收盘
BSE_OPEN: date = date(2021, 11, 15)  # 北交所开市日，之前是新三板行情，不要
INDEX_SYMBOL: str = "sh000001"
MAX_RT_DAYS: int = 5            # 快照(rt)行积累到这么多天就整体用K线重新校正一次
PRECISE_BARS: int = 20          # 最近这么多根K线内的除权日一律用前复权取交易所参考价（更准，每次多一个请求）
EMPTY_SKIP_DAYS: int = 5        # 下载后一根新K线都没有的股票（长期停牌等），之后这么多个交易日内不再下载（force 除外）
EMPTY_FILE_NAME: str = "empty_fetch.json"

PANEL_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date,
    "code": pl.Utf8,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "preclose": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "turn": pl.Float64,
    "tradestatus": pl.Int8,
    "is_st": pl.Boolean,
    "source": pl.Utf8,
}

_write_lock = threading.Lock()


# ---------------------------------------------------------------- 时间

def china_now() -> datetime:
    return datetime.now(config.CHINA_TZ)


def _is_closed(day: date, now: datetime | None = None) -> bool:
    """day 这一天的行情是否已收盘定型"""
    now = now or china_now()
    if day < now.date():
        return True
    return day == now.date() and now.hour * 60 + now.minute >= CLOSE_MINUTE


def _parse_day(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------- 腾讯K线

def _request(sym: str, end: str, count: int, adjust: str = "") -> list[list]:
    """请求一页K线（截止 end 的最近 count 根；start 参数腾讯会忽略）"""
    j = net.get_json(KLINE_URL, params={"param": f"{sym},day,,{end},{count},{adjust}"}, timeout=10)
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict):
        if isinstance(j, dict) and j.get("msg"):
            raise ConnectionError(f"腾讯K线接口报错：{j.get('msg')}")
        return []
    item = data.get(sym) or {}
    bars = item.get("qfqday") or item.get("day") or []
    return [b for b in bars if isinstance(b, list) and len(b) >= 9]


def _fetch_raw(sym: str, need_from: date, first_count: int) -> tuple[list[list], bool]:
    """取不复权K线，直到拿到 need_from 之前的一根（用于算昨收）或到达上市首日。

    返回 (按日期升序的K线, complete)；complete=True 表示已取到完整历史（第一根即上市首日）。
    """
    bars: list[list] = []
    end: str = ""
    count: int = max(2, min(first_count, RAW_PAGE))
    while True:
        chunk: list[list] = _request(sym, end, count)
        n: int = len(chunk)
        if bars:
            chunk = [b for b in chunk if b[0] < bars[0][0]]
        bars = chunk + bars
        if n < count:
            return bars, True
        earliest: date = _parse_day(bars[0][0])
        if earliest < need_from:
            return bars, False
        end = (earliest - timedelta(days=1)).isoformat()
        count = RAW_PAGE


def qfq_estimate(bars: list[list], i: int, qfq: list[list]) -> tuple[float, float] | None:
    """用前复权K线估算第 i 根的除权参考价，返回 (参考价, 误差范围)。

    腾讯前复权在两次除权之间是不复权价的线性变换 q = A·raw + B（现金分红做减法、送转做除法），
    且用的是交易所的参考价。用第 i 根起到下一次除权前的开高低收拟合 A，参考价 = raw_i + (q_{i-1} - q_i) / A。
    A=1（最近一段或之后只有现金分红）时是精确值（误差 0）；否则拟合误差约 0.03 元。
    与交易所/baostock 核对：送转（含库存股"虚拟"比例）一致，配股约差 0.03 元（另用 baostock 复核）。
    """
    qmap: dict[str, list] = {b[0]: b for b in qfq}
    prev_q = qmap.get(bars[i - 1][0])
    if prev_q is None or bars[i][0] not in qmap:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for j in range(i, len(bars)):
        if j > i and isinstance(bars[j][6], dict) and bars[j][6]:
            break
        qb = qmap.get(bars[j][0])
        if qb is None:
            break
        for k in (1, 2, 3, 4):
            try:
                xv, yv = float(bars[j][k]), float(qb[k])
            except (TypeError, ValueError, IndexError):
                continue
            xs.append(xv)
            ys.append(yv)
    if not xs:
        return None
    x, y = np.array(xs), np.array(ys)
    if np.ptp(x) > 0:
        slope = float(np.polyfit(x, y, 1)[0])
    else:
        slope = float(y[0] / x[0]) if x[0] else 0.0
    if not 0.05 < slope < 5:
        return None
    uncertainty: float = 0.03
    if abs(slope - 1) < 0.003:      # 之后只有现金分红（做减法）：斜率就是 1
        slope, uncertainty = 1.0, 0.0
    q_prev, q_now, raw_now = float(prev_q[2]), float(qmap[bars[i][0]][2]), float(bars[i][2])
    ref: float = raw_now + (q_prev - q_now) / slope
    return (round(ref, 2), uncertainty) if ref > 0 else None


def _qfq_bars(sym: str, bars: list[list], i: int, span: int = 8) -> list[list]:
    """取第 i-1 根到同一段（下一次除权前，最多 span 根）的前复权K线"""
    end_i: int = i
    for j in range(i + 1, min(i + span, len(bars))):
        if isinstance(bars[j][6], dict) and bars[j][6]:
            break
        end_i = j
    try:
        return _request(sym, bars[end_i][0], end_i - i + 2, "qfq")
    except ConnectionError:
        return []


# ---------------------------------------------------------------- 昨收

def _round2(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_dividend(info: dict) -> tuple[Decimal, Decimal] | None:
    """解析除权信息 → (每股派息, 每股送转股数)；配股或无法识别时返回 None"""
    import re

    content: str = str(info.get("FHcontent") or "")
    if "配" in content:
        return None
    try:
        cash10 = Decimal(str(info.get("fh_sh") or "0"))
    except ArithmeticError:
        cash10 = Decimal(0)
    if cash10 == 0:
        cash_parts: list[str] = re.findall(r"派([\d.]+)元", content)
        cash10 = sum((Decimal(x) for x in cash_parts), Decimal(0))
    shares10: Decimal = Decimal(0)
    for pattern in (r"送(?:红股)?([\d.]+)股", r"转(?:增)?([\d.]+)股"):
        for x in re.findall(pattern, content):
            shares10 += Decimal(x)
    if cash10 == 0 and shares10 == 0:
        return None
    return cash10 / 10, shares10 / 10


def ex_rights_price(prev_close: float, cash: Decimal, shares: Decimal) -> float:
    """交易所除权除息参考价 =(前收盘-每股派息)/(1+每股送转)，四舍五入到分"""
    return _round2((Decimal(str(prev_close)) - cash) / (1 + shares))


def _limit(preclose: float, ratio: float) -> float:
    return _round2(Decimal(str(preclose)) * (1 + Decimal(str(ratio))))


def _limit_vec(pre: np.ndarray, ratio: np.ndarray) -> np.ndarray:
    return np.floor(pre * (1 + ratio) * 100 + 0.5 + 1e-6) / 100


def _limit_candidates(ratios: np.ndarray | None, i: int, with_st: bool) -> set[float]:
    return ({0.05} if with_st else set()) | ({float(ratios[i])} if ratios is not None else {0.1})


def _near_limit(bar: list, ref: float, ratios: np.ndarray | None, i: int, with_st: bool = True) -> bool:
    """当日最高/收盘贴近涨停价、或最低/收盘贴近跌停价（with_st 时含 5% 的 ST 限制），昨收差 1 分钱就可能影响涨停判断"""
    try:
        close, high, low = float(bar[2]), float(bar[3]), float(bar[4])
    except (TypeError, ValueError):
        return False
    for r in _limit_candidates(ratios, i, with_st):
        if max(close, high) >= _limit(ref, r) - 0.03 or min(close, low) <= _limit(ref, -r) + 0.03:
            return True
    return False


def _hits_limit(bar: list, ref: float, ratios: np.ndarray | None, i: int, with_st: bool) -> bool:
    """按参考价 ref 算，当日是否恰好收在涨停价（且收在最高）或跌停价（且收在最低）"""
    try:
        close, high, low = float(bar[2]), float(bar[3]), float(bar[4])
    except (TypeError, ValueError):
        return False
    for r in _limit_candidates(ratios, i, with_st):
        if abs(close - _limit(ref, r)) < 0.005 and close >= high - 0.005:
            return True
        if abs(close - _limit(ref, -r)) < 0.005 and close <= low + 0.005:
            return True
    return False


def _as_estimate(value: float | tuple[float, float] | None) -> tuple[float, float] | None:
    if value is None:
        return None
    if isinstance(value, tuple):
        return float(value[0]), float(value[1])
    return float(value), 0.0


def compute_preclose(
    bars: list[list],
    qfq_ref: Callable[[int], float | tuple[float, float] | None] | None = None,
    need_from: date | None = None,
    move_limit: Callable[[str], float] | None = None,
    free_bars: int = 0,
    precise_from: int | None = None,
    st: bool = True,
    review_near: bool = False,
) -> tuple[list[float | None], list[int]]:
    """逐根计算昨收，返回 (昨收列表, 需要 baostock 复核的K线下标：配股、无公告的除权、review_near 时贴近涨跌停的除权日)。

    - bars：腾讯原始K线（升序）；第一根没有上一根，昨收为空（完整历史时即新股上市首日）；
    - 纯现金分红：交易所公式（前收盘 - 每股派息，四舍五入到分）；
    - 有送转股或配股：qfq_ref(i) 前复权估算 (参考价, 误差)（能反映库存股导致的"虚拟"送转比例）：
      与公式之差超出误差才采用估算，否则用公式；配股没有公式，直接用估算；
    - move_limit(day_iso)：该股当日的（非ST）涨跌幅比例，用于两种数据自洽修正：
      1) 除权日收盘价超出按参考价算出的涨跌停价 ≤2 分钱（回购库存股的"虚拟分派"每股派息略少）→ 调整 1~2 分钱；
      2) 没有除权信息、但收盘价超出涨跌停范围（如破产重整资本公积转增）→ 前复权估算，变化超过3%才采用。
    - free_bars：前几根K线（新股无涨跌幅限制期）不做第2种检查。
    - 现金分红：有库存股的公司交易所按"虚拟分派"算参考价，可能比公式高 1 分钱；腾讯前复权用的是交易所参考价，
      所以下标 ≥ precise_from 的除权日（最近一个月）或当日价格贴近涨跌停价时，也用前复权的精确值（须比公式略高且合理才采用）。
      但腾讯前复权价只保留 2 位小数，较早的除权日估算会差 ±1 分钱：公式与估算不一致时，若其中一个使当日恰好收在
      涨跌停价（且收在最高/最低），就采用它（封板的概率远大于恰好差 1 分钱）。
    - st：该股当前是否 ST（贴近涨跌停的判断是否也看 5% 限制）；review_near：贴近涨跌停的除权日交给 baostock 复核
      （交易所昨收是涨停识别的基准，差 1 分钱就会漏判/错判涨停）。
    """
    n: int = len(bars)
    closes = np.array([float(b[2]) for b in bars], dtype=float)
    pre: list[float | None] = [None] + closes[:-1].tolist()
    review: list[int] = []
    if n < 2:
        return pre, review
    need: str = need_from.isoformat() if need_from else ""
    dates: list[str] = [b[0] for b in bars]
    events: set[int] = {i for i in range(1, n) if isinstance(bars[i][6], dict) and bars[i][6] and dates[i] >= need}
    ratios: np.ndarray | None = None
    suspects: set[int] = set()
    if move_limit is not None:
        ratios = np.array([move_limit(d) for d in dates], dtype=float)
        prev = np.concatenate([[np.nan], closes[:-1]])
        with np.errstate(invalid="ignore"):
            beyond = (closes > _limit_vec(prev, ratios) + 0.005) | (closes < _limit_vec(prev, -ratios) - 0.005)
        beyond[:max(free_bars, 1)] = False
        suspects = {int(i) for i in np.nonzero(beyond)[0] if dates[i] >= need} - events

    for i in sorted(events | suspects):
        close, prev_close = float(closes[i]), float(closes[i - 1])
        ref: float | None
        if i in events:
            parsed = parse_dividend(bars[i][6])
            formula: float | None = ex_rights_price(prev_close, *parsed) if parsed else None
            cash_only: bool = parsed is not None and parsed[1] == 0
            got: tuple[float, float] | None = None
            if qfq_ref is not None and (
                not cash_only or (precise_from is not None and i >= precise_from)
                or (formula is not None and _near_limit(bars[i], formula, ratios, i, st))
            ):
                got = _as_estimate(qfq_ref(i))
            ref = formula
            if got is not None:
                est, unc = got
                if formula is None:
                    ref = est
                elif cash_only:
                    # 现金分红：只采用精确的前复权值。"虚拟分派"每股派息只会略少 → 参考价只会略高（最多派息的 1/4）
                    extra: float = est - formula
                    ref = est if unc == 0 and -0.011 <= extra <= float(parsed[0]) * 0.25 + 0.011 else formula
                elif abs(est - formula) > unc:
                    ref = est       # 送转：差异超出估算误差，说明交易所用了"虚拟"比例
                if ratios is not None and ref is not None and formula is not None:
                    other: float = formula if abs(ref - formula) > 1e-9 else round(est, 2)
                    if abs(other - ref) > 1e-9 and other > 0 and _hits_limit(bars[i], other, ratios, i, st) \
                            and not _hits_limit(bars[i], ref, ratios, i, st):
                        ref = other
            if ref is None:
                continue
            if parsed is None or (review_near and _near_limit(bars[i], ref, ratios, i, st)):
                review.append(i)
        else:
            got = _as_estimate(qfq_ref(i)) if qfq_ref is not None else None
            if got is None or abs(got[0] / prev_close - 1) <= 0.03:
                continue
            ref = got[0]
            review.append(i)
        if ratios is not None and ref > 0:
            ratio: float = float(ratios[i])
            for step in (0.01, 0.02):
                if close > _limit(ref, ratio) + 0.005 and close <= _limit(ref + step, ratio) + 0.005:
                    ref = round(ref + step, 2)
                    break
                if close < _limit(ref, -ratio) - 0.005 and close >= _limit(ref - step, -ratio) - 0.005:
                    ref = round(ref - step, 2)
                    break
        pre[i] = ref
    return pre, review


def _volume_shares(vol: np.ndarray, amount: np.ndarray, low: np.ndarray, high: np.ndarray) -> np.ndarray:
    """腾讯成交量多数是"手"，科创板是"股"：用 成交额/成交量 是否落在当日最低~最高价之间自动识别"""
    with np.errstate(divide="ignore", invalid="ignore"):
        avg_lot: np.ndarray = amount / (vol * 100)
        avg_share: np.ndarray = amount / vol
    lo, hi = low * 0.9 - 0.02, high * 1.1 + 0.02
    fits_lot = (avg_lot >= lo) & (avg_lot <= hi)
    fits_share = (avg_share >= lo) & (avg_share <= hi)
    return np.where(fits_share & ~fits_lot, vol, vol * 100)


def _floats(values: list) -> np.ndarray:
    return pl.Series(values, dtype=pl.Utf8, strict=False).cast(pl.Float64, strict=False) \
        .fill_null(np.nan).to_numpy().astype(float)


def bars_to_frame(code: str, bars: list[list], preclose: list[float | None]) -> pl.DataFrame:
    """腾讯K线 → 面板行（开 收 高 低 顺序、单位换算）"""
    if not bars:
        return pl.DataFrame(schema=PANEL_SCHEMA)
    cols: list[list] = [list(c) for c in zip(*[b[:9] for b in bars], strict=True)]
    o, c, h, lo = (_floats(cols[k]) for k in (1, 2, 3, 4))
    vol: np.ndarray = _floats(cols[5])
    amount: np.ndarray = np.round(_floats(cols[8]) * 10000, 2)
    turn: np.ndarray = _floats(cols[7])
    volume: np.ndarray = np.round(_volume_shares(vol, amount, lo, h), 0)
    n: int = len(bars)
    df = pl.DataFrame({
        "date": pl.Series(cols[0], dtype=pl.Utf8).str.to_date("%Y-%m-%d"),
        "code": [code] * n,
        "open": o, "high": h, "low": lo, "close": c,
        "preclose": pl.Series(preclose, dtype=pl.Float64, strict=False),
        "volume": volume, "amount": amount, "turn": turn,
        "tradestatus": (vol > 0).astype(np.int8),
        "is_st": pl.Series([False] * n, dtype=pl.Boolean),
        "source": ["tx"] * n,
    })
    return df.with_columns(pl.col(["open", "high", "low", "close", "volume", "amount", "turn"]).fill_nan(None)) \
        .cast(PANEL_SCHEMA)


def _bj_recent_ex_rights(sym: str, bars: list[list], pre: list[float | None], need_from: date) -> None:
    """北交所K线没有分红送转信息：用最近 PRECISE_BARS 根的前复权找出最近一次除权（之后前复权=不复权），
    该日参考价 = 前一根的前复权收盘价（最近一段是精确值）。原地修改 pre。"""
    n: int = min(len(bars), PRECISE_BARS)
    if n < 2:
        return
    try:
        qfq: list[list] = _request(sym, "", n, "qfq")
    except ConnectionError:
        return
    qmap: dict[str, float] = {}
    for b in qfq:
        try:
            qmap[b[0]] = float(b[2])
        except (TypeError, ValueError, IndexError):
            continue
    need: str = need_from.isoformat()
    for i in range(len(bars) - 1, len(bars) - n, -1):
        q_prev = qmap.get(bars[i - 1][0])
        if q_prev is None or bars[i][0] not in qmap:
            return
        if abs(q_prev - float(bars[i - 1][2])) > 0.0015:   # 第 i 根是最近一次除权日
            if bars[i][0] >= need and q_prev > 0:
                pre[i] = round(q_prev, 2)
            return


def _is_st_now(code: str) -> bool:
    uni: pl.DataFrame = uni_mod.load_universe()
    if uni.is_empty():
        return True        # 不知道时按可能是 ST 处理（只影响是否多做一次复核）
    hit: pl.DataFrame = uni.filter(pl.col("code") == code)
    return bool(hit["is_st"].fill_null(False).any()) if hit.height else False


def fetch_stock(code: str, need_from: date, first_count: int = RAW_PAGE,
                end: date | None = None, st: bool | None = None) -> tuple[pl.DataFrame, dict]:
    """下载单只股票 need_from ~ end 的日线（含正确昨收），返回 (面板行, 首根K线信息)。

    st：该股当前是否 ST（None 时查股票列表）；first_info["approx_days"] 为需要 baostock 复核昨收的日期。
    """
    sym: str = uni_mod.market_symbol(code)
    if st is None:
        st = _is_st_now(code)
    bars, complete = _fetch_raw(sym, need_from, first_count)
    if uni_mod.board_of(code) == "bj":
        # 北交所：只保留开市后的行情，开市首日昨收仍取新三板最后收盘
        keep_from: int = next((i for i, b in enumerate(bars) if _parse_day(b[0]) >= BSE_OPEN), len(bars))
        if keep_from > 0:
            bars = bars[keep_from - 1:]
            complete = False
    first_info: dict = {
        "code": code,
        "first_date": _parse_day(bars[0][0]) if bars else None,
        "complete": complete,
    }

    board: str = uni_mod.board_of(code)

    def qfq_ref(i: int) -> float | None:
        return qfq_estimate(bars, i, _qfq_bars(sym, bars, i))

    def move_limit(day: str) -> float:
        if board == "bj":
            return 0.30
        if board == "star" or (board == "chinext" and day >= "2020-08-24"):
            return 0.20
        return 0.10

    pre, review = compute_preclose(bars, qfq_ref, need_from, move_limit, free_bars=5 if complete else 0,
                                   precise_from=len(bars) - PRECISE_BARS, st=st, review_near=board != "bj")
    if board == "bj":
        _bj_recent_ex_rights(sym, bars, pre, need_from)
    first_info["approx_days"] = [bars[i][0] for i in review]
    df: pl.DataFrame = bars_to_frame(code, bars, pre)
    keep: date = max(need_from, BSE_OPEN) if uni_mod.board_of(code) == "bj" else need_from
    df = df.filter(pl.col("date") >= keep)
    if end is not None:
        df = df.filter(pl.col("date") <= end)
    return df, first_info


# ---------------------------------------------------------------- 腾讯快照

def _fnum(fields: list[str], i: int) -> float | None:
    try:
        return float(fields[i]) if i < len(fields) and fields[i] not in ("", "-") else None
    except ValueError:
        return None


def _parse_quote_line(line: str) -> dict | None:
    if "~" not in line or '="' not in line:
        return None
    fields: list[str] = line.split('="', 1)[1].rstrip('";\n ').split("~")
    if len(fields) < 49 or not fields[2]:
        return None
    price, vol, low, high = _fnum(fields, 3), _fnum(fields, 6), _fnum(fields, 34), _fnum(fields, 33)
    amount: float | None = None
    detail: list[str] = fields[35].split("/") if len(fields) > 35 else []
    if len(detail) == 3:
        try:
            amount = float(detail[2])
        except ValueError:
            amount = None
    if amount is None and _fnum(fields, 37) is not None:
        amount = _fnum(fields, 37) * 10000
    volume: float | None = None
    if vol is not None:
        volume = vol * 100
        if vol > 0 and amount and low and high:
            volume = float(_volume_shares(np.array([vol]), np.array([amount]), np.array([low]), np.array([high]))[0])
    t: str = fields[30]
    time_text: str = f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}" if len(t) >= 14 else ""
    float_cap, total_cap = _fnum(fields, 44), _fnum(fields, 45)
    limit_up, limit_down = _fnum(fields, 47), _fnum(fields, 48)
    return {
        "code": fields[2], "name": uni_mod.clean_name(fields[1]),
        "price": price, "prev_close": _fnum(fields, 4), "open": _fnum(fields, 5),
        "high": high, "low": low, "volume": volume, "amount": amount,
        "pct": _fnum(fields, 32), "change": _fnum(fields, 31), "turnover": _fnum(fields, 38),
        "pe": _fnum(fields, 39),
        "float_cap": float_cap * 1e8 if float_cap is not None else None,
        "total_cap": total_cap * 1e8 if total_cap is not None else None,
        "limit_up": limit_up if limit_up and limit_up > 0 else None,
        "limit_down": limit_down if limit_down and limit_down > 0 else None,
        "time": time_text,
    }


def _fetch_snapshot(codes: list[str]) -> pl.DataFrame:
    """腾讯批量报价（全市场约5秒），列同 realtime.quotes：volume 股、amount 元、市值 元"""
    rows: list[dict] = []
    symbols: list[str] = []
    for c in codes:
        try:
            symbols.append(uni_mod.market_symbol(c))
        except ValueError:
            continue
    for i in range(0, len(symbols), QUOTE_BATCH):
        text: str = net.get_text(QUOTE_URL + ",".join(symbols[i:i + QUOTE_BATCH]), encoding="gbk", timeout=15)
        for line in text.split(";"):
            row: dict | None = _parse_quote_line(line.strip())
            if row:
                rows.append(row)
    schema: dict[str, pl.DataType] = {
        "code": pl.Utf8, "name": pl.Utf8, "price": pl.Float64, "prev_close": pl.Float64, "open": pl.Float64,
        "high": pl.Float64, "low": pl.Float64, "volume": pl.Float64, "amount": pl.Float64, "pct": pl.Float64,
        "change": pl.Float64, "turnover": pl.Float64, "pe": pl.Float64, "float_cap": pl.Float64,
        "total_cap": pl.Float64, "limit_up": pl.Float64, "limit_down": pl.Float64, "time": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema, orient="row") if rows else pl.DataFrame(schema=schema)


# ---------------------------------------------------------------- 面板读写

def _year_files() -> dict[int, Path]:
    if not config.PANEL_DIR.exists():
        return {}
    files: dict[int, Path] = {}
    for path in config.PANEL_DIR.glob("*.parquet"):
        if path.stem.isdigit() and len(path.stem) == 4:
            files[int(path.stem)] = path
    return dict(sorted(files.items()))


def _pending_dir() -> Path:
    return config.PANEL_DIR.joinpath("_pending")


def _st_map() -> pl.DataFrame:
    uni: pl.DataFrame = uni_mod.load_universe()
    return uni.select(["code", pl.col("is_st").alias("_st")])


def _stamp_st(df: pl.DataFrame) -> pl.DataFrame:
    """用股票列表中的当前 ST 状态填 is_st（近似：当前名称应用于全部历史）"""
    st: pl.DataFrame = _st_map()
    if st.is_empty():
        return df
    return df.join(st, on="code", how="left").with_columns(
        pl.coalesce(["_st", "is_st"]).fill_null(False).alias("is_st")
    ).drop("_st")


def _replace(tmp: Path, path: Path, attempts: int = 10) -> None:
    """原子替换文件；Windows 上目标文件正被别的进程读取时会 PermissionError，稍等重试"""
    for k in range(attempts):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if k == attempts - 1:
                raise
            time.sleep(0.5 * (k + 1))


def _write_rows(df: pl.DataFrame, overwrite_tx: bool = True) -> int:
    """把新行合并进按年文件：同 (date, code) 新行覆盖旧行；overwrite_tx=False 时不覆盖已有的腾讯K线行"""
    if df.is_empty():
        return 0
    df = df.select([pl.col(c).cast(t) for c, t in PANEL_SCHEMA.items()])
    config.PANEL_DIR.mkdir(parents=True, exist_ok=True)
    written: int = 0
    with _write_lock:
        for (year,), part in df.group_by(pl.col("date").dt.year()):
            path: Path = config.PANEL_DIR.joinpath(f"{year}.parquet")
            if path.exists():
                old: pl.DataFrame = pl.read_parquet(path).select(list(PANEL_SCHEMA)).cast(PANEL_SCHEMA)
                if not overwrite_tx:
                    part = part.join(old.filter(pl.col("source") == "tx").select(["date", "code"]),
                                     on=["date", "code"], how="anti")
                old = old.join(part.select(["date", "code"]), on=["date", "code"], how="anti")
                merged: pl.DataFrame = pl.concat([old, part])
            else:
                merged = part
            merged = merged.unique(["date", "code"], keep="last").sort(["date", "code"])
            tmp: Path = path.with_suffix(".tmp")
            merged.write_parquet(tmp, statistics=True)
            _replace(tmp, path)
            written += part.height
    return written


def _save_pending(frames: list[pl.DataFrame], firsts: list[dict], tag: str,
                  approx: list[tuple[str, date]] | None = None) -> None:
    """分批落盘（中断后下次运行先合并这些文件）；approx 为待 baostock 复核昨收的 (代码, 日期)"""
    if not frames and not firsts and not approx:
        return
    pending: Path = _pending_dir()
    pending.mkdir(parents=True, exist_ok=True)
    stamp: str = f"{tag}_{int(time.time() * 1000)}"
    if frames:
        pl.concat(frames).write_parquet(pending.joinpath(f"part_{stamp}.parquet"))
    if firsts:
        pending.joinpath(f"first_{stamp}.json").write_text(
            json.dumps([{"code": f["code"], "complete": f["complete"],
                         "first_date": f["first_date"].isoformat() if f["first_date"] else None}
                        for f in firsts]), encoding="utf-8")
    if approx:
        pending.joinpath(f"approx_{stamp}.json").write_text(
            json.dumps([[c, d.isoformat()] for c, d in approx]), encoding="utf-8")


def _merge_pending(report: Callable[[float, str], None] | None = None) -> int:
    """合并上次（或本次）分批落盘的数据到年文件，再用 baostock 校正特殊除权日的昨收，返回行数"""
    pending: Path = _pending_dir()
    if not pending.exists():
        return 0
    parts: list[Path] = sorted(pending.glob("part_*.parquet"))
    rows: int = 0
    if parts:
        lf: pl.LazyFrame = pl.scan_parquet([str(p) for p in parts])
        years: list[int] = lf.select(pl.col("date").dt.year().unique()).collect().to_series().to_list()
        for year in sorted(years):
            part: pl.DataFrame = lf.filter(pl.col("date").dt.year() == year).collect()
            rows += _write_rows(_stamp_st(part))
    firsts: list[dict] = []
    for path in sorted(pending.glob("first_*.json")):
        try:
            firsts.extend(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    if firsts:
        rec = pl.DataFrame(
            [{"code": f["code"], "first_date": f["first_date"], "complete": f["complete"]} for f in firsts],
            schema={"code": pl.Utf8, "first_date": pl.Utf8, "complete": pl.Boolean}, orient="row",
        ).with_columns(pl.col("first_date").str.to_date()).drop_nulls("first_date")
        uni_mod.save_first_bars(rec)
        uni_mod.fill_list_dates()
    approx: list[tuple[str, date]] = []
    for path in sorted(pending.glob("approx_*.json")):
        try:
            approx.extend((str(c), _parse_day(d)) for c, d in json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
    if approx:
        approx = list(dict.fromkeys(approx))
        if report:
            report(0.96, f"正在用 baostock 校正 {len(approx)} 个特殊除权日的昨收…")
        processed: list[tuple[str, date]] = []
        fixes: dict[tuple[str, date], float] = _baostock_preclose(approx, processed=processed)
        _patch_preclose(fixes)
        _save_checked(processed, fixes)
    shutil.rmtree(pending, ignore_errors=True)
    return rows


def _code_state() -> pl.DataFrame:
    """每只股票在面板中的状态：last_tx 最后一根腾讯K线日期、last_any 最后日期、first_rt 最早快照日期"""
    files: list[Path] = list(_year_files().values())
    schema = {"code": pl.Utf8, "last_tx": pl.Date, "last_any": pl.Date, "first_rt": pl.Date}
    if not files:
        return pl.DataFrame(schema=schema)
    return (
        pl.scan_parquet([str(p) for p in files]).select(["date", "code", "source"])
        .group_by("code")
        .agg(
            pl.col("date").filter(pl.col("source") == "tx").max().alias("last_tx"),
            pl.col("date").max().alias("last_any"),
            pl.col("date").filter(pl.col("source") == "rt").min().alias("first_rt"),
        )
        .collect()
    )


def _rt_days() -> int:
    files: list[Path] = list(_year_files().values())[-2:]
    if not files:
        return 0
    return pl.scan_parquet([str(p) for p in files]).filter(pl.col("source") == "rt") \
        .select(pl.col("date").n_unique()).collect().item()


# ---------------------------------------------------------------- 交易日历

def _index_calendar(count: int = RAW_PAGE) -> list[date]:
    bars: list[list] = _request(INDEX_SYMBOL, "", count)
    return [_parse_day(b[0]) for b in bars]


def latest_closed_day(calendar: list[date] | None = None) -> tuple[date, list[date]]:
    """最近一个已收盘交易日（北京时间），以及交易日历（上证指数K线日期）"""
    now: datetime = china_now()
    try:
        cal: list[date] = calendar or _index_calendar()
    except ConnectionError:
        cal = []
    if not cal:
        # 退化：按工作日估算（节假日会多算，只影响补数据的条数）
        day: date = now.date()
        cal = [day - timedelta(days=i) for i in range(400, -1, -1) if (day - timedelta(days=i)).weekday() < 5]
    closed: list[date] = [d for d in cal if _is_closed(d, now)]
    return closed[-1], closed


# ---------------------------------------------------------------- 更新

def append_realtime_snapshot(snapshot: pl.DataFrame, day: date) -> int:
    """收盘后用全市场快照生成当日 source="rt" 的行；已有腾讯K线的行不覆盖。返回写入行数"""
    if snapshot.is_empty() or not _is_closed(day):
        return 0
    df: pl.DataFrame = snapshot
    if "time" in df.columns:
        df = df.filter(pl.col("time").str.slice(0, 10) == day.isoformat())
    df = df.filter(
        (pl.col("price") > 0) & (pl.col("open") > 0) & (pl.col("prev_close") > 0) & (pl.col("volume") > 0)
    )
    if df.is_empty():
        return 0
    rows: pl.DataFrame = df.select(
        pl.lit(day).alias("date"),
        pl.col("code").cast(pl.Utf8),
        pl.col("open"), pl.col("high"), pl.col("low"), pl.col("price").alias("close"),
        pl.col("prev_close").alias("preclose"),
        pl.col("volume"), pl.col("amount"),
        (pl.col("turnover") if "turnover" in df.columns else pl.lit(None)).cast(pl.Float64).alias("turn"),
        pl.lit(1, dtype=pl.Int8).alias("tradestatus"),
        pl.lit(False).alias("is_st"),
        pl.lit("rt").alias("source"),
    )
    return _write_rows(_stamp_st(rows), overwrite_tx=False)


def _empty_file() -> Path:
    return config.STOCK_LAB.joinpath(EMPTY_FILE_NAME)


def load_empty_fetch() -> dict[str, date]:
    """{代码: 最近一次下载却没有任何新K线时的目标交易日}；文件不存在或损坏时为空"""
    try:
        raw: object = json.loads(_empty_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, date] = {}
    for code, day in raw.items():
        d: date | None = _parse_day(day) if isinstance(day, str) else None
        if d is not None:
            out[str(code)] = d
    return out


def _save_empty_fetch(memo: dict[str, date]) -> None:
    path: Path = _empty_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({c: d.isoformat() for c, d in sorted(memo.items())}, ensure_ascii=False, indent=0),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass            # 只是省流量的记录，写不了不影响更新


def update_history(
    progress: Callable[[float, str], None] | None = None,
    codes: list[str] | None = None,
    start: str = config.HISTORY_START,
    workers: int = DEFAULT_WORKERS,
    force: bool = False,
) -> dict:
    """下载/增量更新日线面板。返回 {"updated","failed","last_date","rows","mode","seconds","skipped_empty"}

    - 面板里没有的股票：从 start 起全量下载；
    - 已有的股票：从最后一根腾讯K线之后补（快照行 rt 也一并用K线重下覆盖）；
    - 面板只缺最近一个已收盘交易日时，改用全市场快照（几秒）；快照行累计 MAX_RT_DAYS 天后整体用K线校正一次；
    - 下载后一根新K线都没有的股票（长期停牌、还没开始交易等）记在 STOCK_LAB/empty_fetch.json（代码 → 当时的目标交易日），
      之后 EMPTY_SKIP_DAYS 个交易日内不再下载（skipped_empty 为跳过只数）；force=True 时照常全部下载。
      跳过不丢数据：再次下载时仍从面板里最后一根K线之后补。
    """
    t0: float = time.time()

    def report(frac: float, msg: str) -> None:
        if progress:
            progress(min(max(frac, 0.0), 1.0), msg)

    config.ensure_dirs()
    report(0.0, "正在检查需要更新的股票…")
    if _pending_dir().exists():
        report(0.01, "发现上次未完成的下载，正在合并已下载的部分…")
        _merge_pending(lambda _f, m: report(0.01, m))

    start_day: date = _parse_day(start)
    target, calendar = latest_closed_day()
    uni: pl.DataFrame = uni_mod.load_universe()
    if codes is None:
        if uni.is_empty():
            report(0.02, "本地没有股票列表，先获取股票列表…")
            uni = uni_mod.refresh_universe()
        live: pl.DataFrame = uni.filter(
            (pl.col("status") == 1)
            | (pl.col("delist_date").is_not_null() & (pl.col("delist_date") >= start_day))
        )
        codes = live["code"].to_list()
    codes = [c for c in dict.fromkeys(str(c).zfill(6) for c in codes) if uni_mod.is_a_share(c)]
    delisted: set[str] = set(uni.filter(pl.col("status") == 0)["code"].to_list()) if not uni.is_empty() else set()

    state: dict[str, dict] = {r["code"]: r for r in _code_state().iter_rows(named=True)}
    cal = np.array(calendar, dtype="datetime64[D]")

    def gap_after(day: date) -> int:
        return int((cal > np.datetime64(day)).sum())

    memo: dict[str, date] = {} if force else load_empty_fetch()
    skipped_empty: int = 0
    full_tasks: list[_Task] = []
    inc_tasks: list[_Task] = []
    for code in codes:
        seen_empty: date | None = memo.get(code)
        if seen_empty is not None and seen_empty <= target and gap_after(seen_empty) < EMPTY_SKIP_DAYS:
            skipped_empty += 1          # 最近下载过但一根新K线都没有：过几个交易日再看
            continue
        st: dict | None = state.get(code)
        if st is None or st["last_any"] is None:
            full_tasks.append(_Task(code, start_day, RAW_PAGE, True))
            continue
        if code in delisted:
            continue
        base: date = st["last_tx"] or st["last_any"]
        if st["first_rt"] is not None:
            base = min(base, st["first_rt"] - timedelta(days=1))
        gap: int = gap_after(base)
        if gap > 0:
            inc_tasks.append(_Task(code, base + timedelta(days=1), gap + 6, False))

    mode: str = "full" if full_tasks and not inc_tasks else ("kline" if inc_tasks else "none")
    rows_written: int = 0
    updated: int = 0
    panel_last: date | None = last_date()
    # 快照：面板整体只缺最近一个已收盘交易日（停牌股自然没有快照行，漏掉的由定期K线校正补上）
    if inc_tasks and panel_last is not None and gap_after(panel_last) == 1 and _rt_days() < MAX_RT_DAYS:
        report(0.05, f"正在用全市场快照补 {target} 的行情…")
        try:
            snap: pl.DataFrame = _fetch_snapshot([t.code for t in inc_tasks])
            traded: pl.DataFrame = snap.filter(pl.col("volume") > 0)
            snap_day: str = traded["time"].str.slice(0, 10).max() or "" if not traded.is_empty() else ""
            if snap_day == target.isoformat():
                n: int = append_realtime_snapshot(snap, target)
                rows_written += n
                updated += n
                inc_tasks = []
                mode = "snapshot" if not full_tasks else "snapshot+full"
        except ConnectionError as e:
            report(0.05, f"快照获取失败（{e}），改为逐只下载K线")

    failed: list[str] = []
    tasks: list[_Task] = full_tasks + inc_tasks
    if tasks:
        empty_codes: list[str] = []
        rows_k, ok_k, failed = _run_tasks(tasks, target, workers, report, empty_codes)
        rows_written += rows_k
        updated += ok_k
        memo = load_empty_fetch()           # force 时也要更新记录（上面为了不跳过用了空字典）
        empty_set: set[str] = set(empty_codes)
        failed_set: set[str] = set(failed)
        for t in tasks:
            if t.code in empty_set:
                memo[t.code] = target
            elif t.code not in failed_set:
                memo.pop(t.code, None)
        _save_empty_fetch(memo)
    last: date | None = last_date()
    seconds: float = round(time.time() - t0, 1)
    report(1.0, f"日线更新完成：{updated} 只股票，写入 {rows_written} 行，最新日期 {last}，用时 {seconds:.0f} 秒"
           + (f"；{len(failed)} 只下载失败" if failed else ""))
    return {
        "updated": updated, "failed": failed, "last_date": last.isoformat() if last else None,
        "rows": rows_written, "mode": mode, "seconds": seconds, "skipped_empty": skipped_empty,
    }


class _Task(NamedTuple):
    code: str
    need_from: date
    count: int
    full: bool


def _run_tasks(
    tasks: list[_Task], target: date, workers: int, report: Callable[[float, str], None],
    empty: list[str] | None = None,
) -> tuple[int, int, list[str]]:
    """多线程下载K线；每 CHECKPOINT_EVERY 只落盘一次，最后合并。返回 (行数, 成功只数, 失败代码)；
    empty（可选）收集下载成功但一根K线都没有的代码"""
    frames: list[pl.DataFrame] = []
    firsts: list[dict] = []
    approx: list[tuple[str, date]] = []
    ok: int = 0
    total: int = len(tasks)
    tag: str = datetime.now().strftime("%Y%m%d%H%M%S")
    last_report: float = 0.0

    def run(batch: list[_Task], n_workers: int, offset: int) -> list[_Task]:
        nonlocal ok, frames, firsts, approx, last_report
        retry: list[_Task] = []
        finished: int = 0
        with ThreadPoolExecutor(max_workers=max(1, n_workers)) as pool:
            futures = {pool.submit(fetch_stock, t.code, t.need_from, t.count, target): t for t in batch}
            for fut in as_completed(futures):
                task: _Task = futures[fut]
                finished += 1
                try:
                    df, first = fut.result()
                except Exception:  # noqa: BLE001  单只失败不影响其他
                    retry.append(task)
                    continue
                ok += 1
                approx.extend((task.code, _parse_day(d)) for d in first.get("approx_days", []))
                if not df.is_empty():
                    frames.append(df)
                elif empty is not None:
                    empty.append(task.code)
                if task.full and first["first_date"] is not None:
                    firsts.append(first)
                if len(frames) >= CHECKPOINT_EVERY:
                    _save_pending(frames, firsts, tag, approx)
                    frames, firsts, approx = [], [], []
                now: float = time.time()
                if now - last_report > 1.0:
                    last_report = now
                    report(0.05 + 0.85 * (offset + finished) / total,
                           f"正在下载日线 {offset + finished}/{total}（失败 {len(retry)}）")
        return retry

    retry: list[_Task] = run(tasks, workers, 0)
    failed: list[str] = []
    if retry:
        report(0.9, f"{len(retry)} 只下载失败，稍后降速重试…")
        time.sleep(3)
        failed = [t.code for t in run(retry, max(1, workers // 4), total - len(retry))]
    _save_pending(frames, firsts, tag, approx)
    report(0.92, "正在合并写入日线文件…")
    rows: int = _merge_pending(report)
    return rows, ok, failed


def _baostock_preclose(
    items: list[tuple[str, date]],
    timeout: float | None = None,
    report: Callable[[int, int], None] | None = None,
    processed: list[tuple[str, date]] | None = None,
) -> dict[tuple[str, date], float]:
    """特殊除权日的交易所昨收（baostock，后台线程+硬超时；北交所没有数据）。

    实测单日查询约 0.02~0.1 秒；硬超时默认 60 秒 + 每条 0.3 秒（最多 15 分钟），超时就停止并保留已取到的部分。
    report(已查条数, 总条数) 约每 5 秒回调一次；processed 非空时追加已查询过的条目（含 baostock 没有数据的）。
    """
    items = [(c, d) for c, d in items if uni_mod.board_of(c) != "bj"]
    if timeout is None:
        timeout = min(60.0 + 0.3 * len(items), 900.0)
    found: list[tuple[tuple[str, date], float]] = []
    if not items:
        return {}
    stop = threading.Event()
    done: list[int] = [0]

    def worker() -> None:
        import baostock as bs

        try:
            with uni_mod.BAOSTOCK_LOCK, net.domestic_direct():
                if stop.is_set() or bs.login().error_code != "0":
                    return
                uni_mod.guard_baostock_socket(20.0)
                errors: int = 0
                try:
                    for code, day in items:
                        if stop.is_set():
                            return
                        rs = bs.query_history_k_data_plus(
                            uni_mod.bs_symbol(code), "date,preclose", start_date=day.isoformat(),
                            end_date=day.isoformat(), frequency="d", adjustflag="3",
                        )
                        if rs.error_code != "0":
                            errors += 1
                            if errors >= 3:         # 连接断了/被限流：放弃本批，已查的保留
                                return
                            continue
                        errors = 0
                        while rs.error_code == "0" and rs.next():
                            row: list[str] = rs.get_row_data()
                            if row[0] == day.isoformat() and row[1]:
                                found.append(((code, day), round(float(row[1]), 3)))
                        done[0] += 1
                        if processed is not None:
                            processed.append((code, day))
                finally:
                    bs.logout()
        except Exception:  # noqa: BLE001  拿不到就保留前复权估算值
            return

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline: float = time.time() + timeout
    while thread.is_alive() and time.time() < deadline:
        thread.join(min(5.0, max(0.0, deadline - time.time())))
        if report:
            report(done[0], len(items))
    stop.set()          # 超时：让后台线程尽快退出并释放 baostock 锁
    return dict(list(found))


def _checked_file() -> Path:
    return config.STOCK_LAB.joinpath("preclose_checked.parquet")


CHECKED_SCHEMA: dict[str, pl.DataType] = {"code": pl.Utf8, "date": pl.Date, "bs_preclose": pl.Float64}


def load_checked_preclose() -> pl.DataFrame:
    """已用 baostock 核对过的除权日昨收：code, date, bs_preclose（baostock 没有数据时为空）"""
    path: Path = _checked_file()
    if not path.exists():
        return pl.DataFrame(schema=CHECKED_SCHEMA)
    try:
        return pl.read_parquet(path).select(list(CHECKED_SCHEMA)).cast(CHECKED_SCHEMA)
    except Exception:  # noqa: BLE001  文件损坏当作没有
        return pl.DataFrame(schema=CHECKED_SCHEMA)


def _save_checked(processed: list[tuple[str, date]], fixes: dict[tuple[str, date], float]) -> None:
    """记录已核对的除权日。baostock 当天晚上才更新：最近 5 天内查不到的不记，下次再查"""
    recent: date = china_now().date() - timedelta(days=5)
    rows: list[tuple[str, date, float | None]] = [
        (c, d, fixes.get((c, d))) for c, d in processed if (c, d) in fixes or d < recent
    ]
    if not rows:
        return
    new = pl.DataFrame(rows, schema=CHECKED_SCHEMA, orient="row")
    merged: pl.DataFrame = pl.concat([load_checked_preclose(), new]).unique(["code", "date"], keep="last") \
        .sort(["code", "date"])
    config.STOCK_LAB.mkdir(parents=True, exist_ok=True)
    tmp: Path = _checked_file().with_suffix(".tmp")
    merged.write_parquet(tmp)
    _replace(tmp, _checked_file())


def calibrate_ex_rights(
    progress: Callable[[float, str], None] | None = None,
    start: str | date | None = None,
    timeout: float = 3600.0,
    batch: int = 1000,
) -> dict:
    """一次性校准：用 baostock 把面板里全部除权除息日（昨收 ≠ 上一根收盘）的昨收改成交易所值。

    日常更新不需要：最近的除权日用腾讯前复权精确值，贴近涨跌停的除权日下载时已自动复核；
    这里补的是较早的普通除权日（有回购库存股的公司按"虚拟分派"，公式会差 1~20 分钱，只影响当日涨跌幅）。
    全市场约 3 万个除权日，baostock 约 0.05~0.3 秒/个；按日期从新到旧、每 batch 个落盘一次，
    核对结果记在 STOCK_LAB/preclose_checked.parquet：中断后再运行只查剩下的，面板重建后先离线重放已核对的值。
    返回 {"events","checked","fixed","changed","reapplied","remaining","seconds"}
    """
    t0: float = time.time()

    def report(frac: float, msg: str) -> None:
        if progress:
            progress(min(max(frac, 0.0), 1.0), msg)

    files: list[Path] = list(_year_files().values())
    empty: dict = {"events": 0, "checked": 0, "fixed": 0, "changed": 0, "reapplied": 0, "remaining": 0,
                   "seconds": 0.0}
    if not files:
        return empty
    start_d: date | None = _parse_day(start)
    events: pl.DataFrame = (
        pl.scan_parquet([str(p) for p in files]).select(["date", "code", "close", "preclose"])
        .sort(["code", "date"])
        .with_columns(pl.col("close").shift(1).over("code").alias("_prev"))
        .filter(pl.col("preclose").is_not_null() & pl.col("_prev").is_not_null()
                & ((pl.col("preclose") - pl.col("_prev")).abs() > 0.0005))
        .collect()
    )
    if start_d is not None:
        events = events.filter(pl.col("date") >= start_d)
    events = events.filter(~pl.col("code").map_elements(lambda c: uni_mod.board_of(c) == "bj", return_dtype=pl.Boolean))
    events = events.sort("date", descending=True)          # 新的先校准：中断时最近几年已是准确值
    current: dict[tuple[str, date], float] = dict(
        zip(zip(events["code"].to_list(), events["date"].to_list(), strict=True), events["preclose"].to_list(),
            strict=True)
    )
    items: list[tuple[str, date]] = list(current)

    # 以前核对过的：直接重放（面板重建后不用再联网），并且不再重复查询
    checked: pl.DataFrame = load_checked_preclose()
    known: dict[tuple[str, date], float | None] = {(c, d): v for c, d, v in checked.rows()}
    replay: dict[tuple[str, date], float] = {
        k: v for k, v in known.items() if v is not None and k in current and abs(v - current[k]) > 0.0005
    }
    reapplied: int = _patch_preclose(replay) if replay else 0
    todo: list[tuple[str, date]] = [k for k in items if k not in known]
    report(0.0, f"除权除息日 {len(items)} 个，已核对 {len(items) - len(todo)} 个，正在用 baostock 核对其余 {len(todo)} 个…")

    deadline: float = time.time() + timeout
    fixed: int = 0
    changed: int = 0
    done: int = 0
    queue: list[tuple[str, date]] = list(todo)
    pos: int = 0
    while pos < len(queue):
        remaining_time: float = deadline - time.time()
        if remaining_time <= 5:
            break
        chunk: list[tuple[str, date]] = queue[pos:pos + max(1, batch)]
        pos += len(chunk)
        processed: list[tuple[str, date]] = []
        fixes: dict[tuple[str, date], float] = _baostock_preclose(
            chunk, min(remaining_time, 60.0 + 1.0 * len(chunk)),
            lambda n, total, base=done: report(0.98 * (base + n) / max(len(todo), 1),
                                               f"正在核对除权日昨收 {base + n}/{len(todo)}"),
            processed,
        )
        _save_checked(processed, fixes)
        diff: dict[tuple[str, date], float] = {k: v for k, v in fixes.items() if abs(v - current[k]) > 0.0005}
        _patch_preclose(diff)
        fixed += len(fixes)
        changed += len(diff)
        done += len(processed)
        if not processed:           # 整批一个都没查成：baostock 不可用，下次再说
            break
        if len(processed) < len(chunk):   # 中途断线：没查到的放到队尾，下一批换新连接再查
            got: set[tuple[str, date]] = set(processed)
            queue.extend(k for k in chunk if k not in got)
    seconds: float = round(time.time() - t0, 1)
    remaining: int = len(todo) - done
    report(1.0, f"除权日昨收核对完成：本次核对 {done} 个（{changed} 个有修正），重放 {reapplied} 个"
           + (f"，还剩 {remaining} 个下次继续" if remaining else "") + f"，用时 {seconds:.0f} 秒")
    return {"events": len(items), "checked": len(items) - remaining, "fixed": fixed, "changed": changed,
            "reapplied": reapplied, "remaining": remaining, "seconds": seconds}


def _patch_preclose(fixes: dict[tuple[str, date], float]) -> int:
    """按 (code, date) 修正面板里的昨收"""
    if not fixes:
        return 0
    patch = pl.DataFrame(
        [{"code": c, "date": d, "_pre": v} for (c, d), v in fixes.items()],
        schema={"code": pl.Utf8, "date": pl.Date, "_pre": pl.Float64},
    )
    n: int = 0
    with _write_lock:
        for (year,), part in patch.group_by(pl.col("date").dt.year()):
            path: Path = config.PANEL_DIR.joinpath(f"{year}.parquet")
            if not path.exists():
                continue
            df: pl.DataFrame = pl.read_parquet(path).join(part, on=["date", "code"], how="left")
            n += df["_pre"].is_not_null().sum()
            df = df.with_columns(pl.coalesce(["_pre", "preclose"]).alias("preclose")).drop("_pre")
            tmp: Path = path.with_suffix(".tmp")
            df.write_parquet(tmp, statistics=True)
            _replace(tmp, path)
    return n


# ---------------------------------------------------------------- 读取

def load_panel(
    start: str | date | None = None,
    end: str | date | None = None,
    codes: list[str] | None = None,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    """读取日线面板（去掉停牌行），按 code, date 排序；is_st 使用股票列表中的当前状态"""
    start_d, end_d = _parse_day(start), _parse_day(end)
    files: list[Path] = [
        p for y, p in _year_files().items()
        if (start_d is None or y >= start_d.year) and (end_d is None or y <= end_d.year)
    ]
    cols: list[str] = list(PANEL_SCHEMA) if columns is None else list(dict.fromkeys(["date", "code", *columns]))
    if not files:
        return pl.DataFrame(schema={c: PANEL_SCHEMA.get(c, pl.Float64) for c in cols})
    lf: pl.LazyFrame = pl.scan_parquet([str(p) for p in files]).filter(pl.col("tradestatus") != 0)
    if start_d is not None:
        lf = lf.filter(pl.col("date") >= start_d)
    if end_d is not None:
        lf = lf.filter(pl.col("date") <= end_d)
    if codes is not None:
        lf = lf.filter(pl.col("code").is_in([str(c).zfill(6) for c in codes]))
    df: pl.DataFrame = lf.select([c for c in cols if c in PANEL_SCHEMA]).collect()
    if "is_st" in cols:
        df = _stamp_st(df)
    return df.select([c for c in cols if c in df.columns]).sort(["code", "date"])


def last_date() -> date | None:
    for _, path in reversed(list(_year_files().items())):
        value = pl.scan_parquet(str(path)).select(pl.col("date").max()).collect().item()
        if value is not None:
            return value
    return None


def trade_dates(start: str | date | None = None, end: str | date | None = None) -> list[date]:
    """面板中出现过的交易日"""
    start_d, end_d = _parse_day(start), _parse_day(end)
    files: list[Path] = [
        p for y, p in _year_files().items()
        if (start_d is None or y >= start_d.year) and (end_d is None or y <= end_d.year)
    ]
    if not files:
        return []
    lf: pl.LazyFrame = pl.scan_parquet([str(p) for p in files]).select("date").unique()
    if start_d is not None:
        lf = lf.filter(pl.col("date") >= start_d)
    if end_d is not None:
        lf = lf.filter(pl.col("date") <= end_d)
    return sorted(lf.collect().to_series().to_list())
