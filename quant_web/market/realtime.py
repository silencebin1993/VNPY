"""
腾讯实时行情：批量报价、全市场快照、K线、分时、指数，以及交易状态（北京时间 + 交易日历）。

- 报价 qt.gtimg.cn（GBK，'~' 分隔，已逐字段核对）：1名称 2代码 3现价 4昨收 5今开 6成交量 9~28 五档买卖
  30时间 31涨跌 32涨跌% 33最高 34最低 35"价/量/额(元)" 37成交额(万) 38换手% 39市盈率TTM 43振幅%
  44流通市值(亿) 45总市值(亿) 46市净率 47涨停价 48跌停价 49量比 51均价；
  成交量多数是"手"，科创板是"股"，用 成交额/成交量 是否落在最低~最高价之间自动识别。
- K线 newfqkline：每根 [日期, 开, 收, 高, 低, 量, {}, 换手%, 额(万), ...]，注意是 开 收 高 低；一次最多 2000 根；
  北交所没有前复权数据（返回不复权 day 键）。
- 分时 minute/query：每点 "HHMM 价格 累计量 累计额(元)"；15:00 之后是盘后固定价格交易，不画进分时。
- 为保护数据源，页面轮询时有短时内存缓存：报价 3 秒、K线 60 秒、分时 5 秒。
"""
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from .. import config, net
from . import universe as uni_mod


logger = logging.getLogger(__name__)

QUOTE_URL: str = "https://qt.gtimg.cn/q="
KLINE_URL: str = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
MINUTE_URL: str = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
FIVE_DAY_URL: str = "https://web.ifzq.gtimg.cn/appstock/app/day/query"

QUOTE_BATCH: int = 700          # 腾讯每批最多约 800 只
KLINE_PAGE: int = 2000
QUOTE_TTL: float = 3.0
KLINE_TTL: float = 60.0
MINUTE_TTL: float = 5.0
CALENDAR_MAX_AGE: float = 3 * 86400.0

INDEXES: list[tuple[str, str]] = [
    ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ("sh000688", "科创50"), ("bj899050", "北证50"),
]

QUOTE_SCHEMA: dict[str, pl.DataType] = {
    "code": pl.Utf8, "name": pl.Utf8, "price": pl.Float64, "prev_close": pl.Float64, "open": pl.Float64,
    "high": pl.Float64, "low": pl.Float64, "volume": pl.Float64, "amount": pl.Float64, "pct": pl.Float64,
    "change": pl.Float64, "turnover": pl.Float64, "pe": pl.Float64, "float_cap": pl.Float64,
    "total_cap": pl.Float64, "limit_up": pl.Float64, "limit_down": pl.Float64, "time": pl.Utf8,
    "pb": pl.Float64, "amplitude": pl.Float64, "volume_ratio": pl.Float64, "avg_price": pl.Float64,
    "symbol": pl.Utf8,
}

_SYMBOL_RE = re.compile(r"^(sh|sz|bj)\d{6}$")
_lock = threading.Lock()
_quote_cache: dict[str, tuple[float, dict]] = {}
_kline_cache: dict[tuple, tuple[float, list[dict]]] = {}
_minute_cache: dict[tuple, tuple[float, dict]] = {}
_index_symbols: set[str] = {s for s, _ in INDEXES}


# ---------------------------------------------------------------- 工具

def china_now() -> datetime:
    return datetime.now(config.CHINA_TZ)


def to_symbol(code: str) -> str:
    """6位代码 → 腾讯代码；已是 sh/sz/bj 前缀的原样返回（指数用这种写法，避免 000001 与平安银行混淆）"""
    code = str(code).strip().lower()
    if _SYMBOL_RE.match(code):
        return code
    code = code.zfill(6) if code.isdigit() else code
    if not uni_mod.is_a_share(code):
        raise ValueError(f"无法识别的股票代码：{code}")
    return uni_mod.market_symbol(code)


def _num(fields: list[str], i: int) -> float | None:
    if i >= len(fields):
        return None
    text: str = fields[i].strip()
    if text in ("", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_shares(vol: float | None, amount: float | None, low: float | None, high: float | None) -> float | None:
    """腾讯成交量 → 股：多数是手（×100），科创板本身是股；用 成交额/成交量 落在价格区间内来判断"""
    if vol is None:
        return None
    if not vol or not amount or not low or not high:
        return vol * 100
    lo, hi = low * 0.9 - 0.02, high * 1.1 + 0.02
    fits_lot: bool = lo <= amount / (vol * 100) <= hi
    fits_share: bool = lo <= amount / vol <= hi
    return vol if fits_share and not fits_lot else vol * 100


def _positive(value: float | None) -> float | None:
    return value if value is not None and value > 0 else None


# ---------------------------------------------------------------- 报价

def parse_quote_line(line: str) -> dict | None:
    """解析一行 v_sz001216="51~华瓷股份~001216~..."; 返回约定字段（量=股，额=元，市值=元）"""
    if "~" not in line or '="' not in line:
        return None
    head, body = line.split('="', 1)
    fields: list[str] = body.rstrip('";\n\r ').split("~")
    if len(fields) < 49 or not fields[2]:
        return None
    symbol: str = head.strip().rsplit("_", 1)[-1]
    prev_close: float | None = _positive(_num(fields, 4))
    open_, high, low = _positive(_num(fields, 5)), _positive(_num(fields, 33)), _positive(_num(fields, 34))
    price: float | None = _positive(_num(fields, 3)) or prev_close

    amount: float | None = None
    detail: list[str] = fields[35].split("/") if len(fields) > 35 else []
    if len(detail) == 3:
        try:
            amount = float(detail[2])
        except ValueError:
            amount = None
    if amount is None and _num(fields, 37) is not None:
        amount = _num(fields, 37) * 10000
    volume: float | None = to_shares(_num(fields, 6), amount, low, high)

    t: str = fields[30]
    time_text: str = f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}" if len(t) >= 14 else ""
    float_cap, total_cap = _num(fields, 44), _num(fields, 45)
    bids: list[list[float]] = []
    asks: list[list[float]] = []
    for k in range(5):
        bp, bv = _num(fields, 9 + 2 * k), _num(fields, 10 + 2 * k)
        ap, av = _num(fields, 19 + 2 * k), _num(fields, 20 + 2 * k)
        if bp:
            bids.append([bp, (bv or 0) * 100])
        if ap:
            asks.append([ap, (av or 0) * 100])
    avg_price: float | None = round(amount / volume, 3) if amount and volume else None
    return {
        "code": fields[2], "name": uni_mod.clean_name(fields[1]), "symbol": symbol,
        "price": price, "prev_close": prev_close, "open": open_, "high": high, "low": low,
        "volume": volume, "amount": amount,
        "pct": _num(fields, 32), "change": _num(fields, 31), "turnover": _num(fields, 38),
        "pe": _num(fields, 39), "pb": _num(fields, 46),
        "float_cap": round(float_cap * 1e8, 2) if float_cap is not None else None,
        "total_cap": round(total_cap * 1e8, 2) if total_cap is not None else None,
        "limit_up": _positive(_num(fields, 47)), "limit_down": _positive(_num(fields, 48)),
        "amplitude": _num(fields, 43), "volume_ratio": _num(fields, 49), "avg_price": avg_price,
        "time": time_text, "bids": bids, "asks": asks,
    }


def parse_quote_text(text: str) -> dict[str, dict]:
    """整段报价文本 → {腾讯代码: 报价}"""
    out: dict[str, dict] = {}
    for line in text.split(";"):
        row: dict | None = parse_quote_line(line.strip())
        if row:
            out[row["symbol"]] = row
    return out


def _fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    batches: list[list[str]] = [symbols[i:i + QUOTE_BATCH] for i in range(0, len(symbols), QUOTE_BATCH)]

    def run(batch: list[str]) -> dict[str, dict]:
        return parse_quote_text(net.get_text(QUOTE_URL + ",".join(batch), encoding="gbk", timeout=15))

    out: dict[str, dict] = {}
    if len(batches) <= 1:
        for batch in batches:
            out.update(run(batch))
        return out
    with ThreadPoolExecutor(max_workers=4) as pool:
        for part in pool.map(run, batches):
            out.update(part)
    return out


def quotes(codes: list[str]) -> list[dict]:
    """批量实时报价（按输入顺序返回，查不到的跳过）；3 秒内重复请求走缓存"""
    symbols: list[str] = []
    for c in codes:
        try:
            symbols.append(to_symbol(c))
        except ValueError:
            continue
    symbols = list(dict.fromkeys(symbols))
    now: float = time.time()
    with _lock:
        stale: list[str] = [s for s in symbols if s not in _quote_cache or now - _quote_cache[s][0] > QUOTE_TTL]
    if stale:
        fresh: dict[str, dict] = _fetch_quotes(stale)
        with _lock:
            for sym, row in fresh.items():
                _quote_cache[sym] = (now, row)
            if len(_quote_cache) > 20000:
                _quote_cache.clear()
                _quote_cache.update({s: (now, r) for s, r in fresh.items()})
    with _lock:
        return [dict(_quote_cache[s][1]) for s in symbols if s in _quote_cache]


def quote(code: str) -> dict | None:
    rows: list[dict] = quotes([code])
    return rows[0] if rows else None


def snapshot_all(codes: list[str]) -> pl.DataFrame:
    """全市场快照（分批并发，约 5 秒），列同 quotes（不含五档）"""
    symbols: list[str] = []
    for c in codes:
        try:
            symbols.append(to_symbol(c))
        except ValueError:
            continue
    rows: dict[str, dict] = _fetch_quotes(list(dict.fromkeys(symbols)))
    now: float = time.time()
    with _lock:
        for sym, row in rows.items():
            _quote_cache[sym] = (now, row)
    records: list[dict] = [{k: r.get(k) for k in QUOTE_SCHEMA} for r in rows.values()]
    if not records:
        return pl.DataFrame(schema=QUOTE_SCHEMA)
    return pl.DataFrame(records, schema=QUOTE_SCHEMA, orient="row")


def index_quotes() -> list[dict]:
    """主要指数行情；code 为带前缀的腾讯代码（如 sh000001）"""
    names: dict[str, str] = dict(INDEXES)
    out: list[dict] = []
    for row in quotes([s for s, _ in INDEXES]):
        sym: str = row["symbol"]
        # 指数成交量单位是手（to_shares 已按手换算成股）
        row.update({"code": sym, "name": names.get(sym, row["name"]), "turnover": None, "pe": None, "pb": None,
                    "limit_up": None, "limit_down": None, "avg_price": None, "bids": [], "asks": []})
        out.append(row)
    return out


# ---------------------------------------------------------------- K线

def parse_kline(payload: Any, symbol: str, period: str = "day", adjust: str = "qfq") -> list[dict]:
    """newfqkline 返回 → [{date, open, close, high, low, volume(股), amount(元), turnover(%)}]"""
    data = payload.get("data") if isinstance(payload, dict) else None
    item = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return []
    bars = item.get(f"{adjust}{period}") or item.get(period) or []
    out: list[dict] = []
    for b in bars:
        if not isinstance(b, list) or len(b) < 9:
            continue
        try:
            o, c, h, lo = float(b[1]), float(b[2]), float(b[3]), float(b[4])
            vol: float = float(b[5])
        except (TypeError, ValueError):
            continue
        try:
            amount: float | None = round(float(b[8]) * 10000, 2)
        except (TypeError, ValueError):
            amount = None
        try:
            turnover: float | None = float(b[7])
        except (TypeError, ValueError):
            turnover = None
        out.append({
            "date": str(b[0])[:10], "open": o, "close": c, "high": h, "low": lo,
            "volume": to_shares(vol, amount, lo, h) if symbol not in _index_symbols else vol * 100,
            "amount": amount, "turnover": turnover,
        })
    return out


def _kline_request(symbol: str, period: str, adjust: str, end: str, count: int) -> list[dict]:
    payload = net.get_json(KLINE_URL, params={"param": f"{symbol},{period},,{end},{count},{adjust}"}, timeout=10)
    if isinstance(payload, dict) and payload.get("code") not in (0, "0", None) and not payload.get("data"):
        raise ConnectionError(f"腾讯K线接口报错：{payload.get('msg')}")
    return parse_kline(payload, symbol, period, adjust)


def kline(code: str, period: str = "day", adjust: str = "qfq", count: int = 600) -> list[dict]:
    """日/周/月 K线（按日期升序，最近 count 根）。adjust: "qfq" 前复权 / "" 不复权 / "hfq" 后复权"""
    if period not in ("day", "week", "month"):
        raise ValueError(f"不支持的K线周期：{period}")
    adjust = "" if adjust in (None, "none", "raw") else adjust
    if adjust not in ("", "qfq", "hfq"):
        raise ValueError(f"不支持的复权方式：{adjust}")
    symbol: str = to_symbol(code)
    count = max(1, min(int(count), 10000))
    key: tuple = (symbol, period, adjust, count)
    now: float = time.time()
    with _lock:
        hit = _kline_cache.get(key)
        if hit and now - hit[0] <= KLINE_TTL:
            return [dict(b) for b in hit[1]]
    page: int = min(count, KLINE_PAGE)
    bars: list[dict] = _kline_request(symbol, period, adjust, "", page)
    got: int = len(bars)
    while bars and got >= page and len(bars) < count:
        end: str = (date.fromisoformat(bars[0]["date"]) - timedelta(days=1)).isoformat()
        page = min(count - len(bars), KLINE_PAGE)
        chunk: list[dict] = _kline_request(symbol, period, adjust, end, page)
        got = len(chunk)
        older: list[dict] = [b for b in chunk if b["date"] < bars[0]["date"]]
        if not older:
            break
        bars = older + bars
    bars = bars[-count:]
    with _lock:
        if len(_kline_cache) > 500:
            _kline_cache.clear()
        _kline_cache[key] = (now, bars)
    return [dict(b) for b in bars]


# ---------------------------------------------------------------- 分时

def _hhmm(text: str) -> str:
    return f"{text[:2]}:{text[2:4]}"


def parse_minute_points(raw: list[str], is_index: bool = False) -> list[dict]:
    """ "HHMM 价格 累计量 累计额" → 每分钟 {time, price, volume(股), amount(元), avg_price}（只保留 15:00 及以前）"""
    parsed: list[tuple[str, float, float, float]] = []
    for item in raw or []:
        parts: list[str] = str(item).split()
        if len(parts) < 4 or parts[0] > "1500":
            continue
        try:
            parsed.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
        except ValueError:
            continue
    if not parsed:
        return []
    # 判断累计量单位：用最后一点 累计额/累计量 与价格比较
    _, last_price, last_vol, last_amt = parsed[-1]
    unit: float = 100.0
    if not is_index and last_vol > 0 and last_price > 0:
        per: float = last_amt / last_vol
        unit = 1.0 if abs(per / last_price - 1) < abs(per / (last_price * 100) - 1) else 100.0
    points: list[dict] = []
    prev_vol, prev_amt = 0.0, 0.0
    for hhmm, price, cum_vol, cum_amt in parsed:
        shares: float = cum_vol * unit
        points.append({
            "time": _hhmm(hhmm), "price": price,
            "volume": max(shares - prev_vol, 0.0), "amount": round(max(cum_amt - prev_amt, 0.0), 2),
            "avg_price": None if is_index or shares <= 0 else round(cum_amt / shares, 3),
        })
        prev_vol, prev_amt = shares, cum_amt
    return points


def _fmt_day(text: Any) -> str:
    s: str = str(text or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def parse_minute(payload: Any, symbol: str) -> dict:
    """minute/query 返回 → {"code","date","prev_close","points"}"""
    item = ((payload or {}).get("data") or {}).get(symbol) or {}
    data = item.get("data") or {}
    qt = (item.get("qt") or {}).get(symbol) or []
    prev_close: float | None = _positive(_num(qt, 4)) if qt else None
    return {
        "code": symbol[2:], "symbol": symbol, "name": uni_mod.clean_name(qt[1]) if len(qt) > 1 else None,
        "date": _fmt_day(data.get("date")), "prev_close": prev_close,
        "points": parse_minute_points(data.get("data") or [], symbol in _index_symbols),
    }


def parse_five_day(payload: Any, symbol: str) -> list[dict]:
    """day/query 返回 → [{"date","prev_close","points"}]，按日期升序"""
    item = ((payload or {}).get("data") or {}).get(symbol) or {}
    days: list[dict] = []
    for d in item.get("data") or []:
        if not isinstance(d, dict):
            continue
        try:
            prec: float | None = float(d.get("prec")) if d.get("prec") not in (None, "") else None
        except ValueError:
            prec = None
        days.append({"date": _fmt_day(d.get("date")), "prev_close": prec,
                     "points": parse_minute_points(d.get("data") or [], symbol in _index_symbols)})
    return sorted(days, key=lambda x: x["date"])


def minute(code: str, days: int = 1) -> dict:
    """当日分时 {"prev_close", "points":[{"time":"09:31","price","volume","amount","avg_price"}], "date", ...}；
    days>1（最多5）时另附 "days":[{"date","prev_close","points"}]（五日分时，升序）"""
    symbol: str = to_symbol(code)
    days = max(1, min(int(days), 5))
    key: tuple = (symbol, days)
    now: float = time.time()
    with _lock:
        hit = _minute_cache.get(key)
        if hit and now - hit[0] <= MINUTE_TTL:
            return json.loads(json.dumps(hit[1]))
    result: dict = parse_minute(net.get_json(MINUTE_URL, params={"code": symbol}, timeout=10), symbol)
    if days > 1:
        five: list[dict] = parse_five_day(net.get_json(FIVE_DAY_URL, params={"code": symbol}, timeout=10), symbol)
        result["days"] = five[-days:]
    with _lock:
        if len(_minute_cache) > 500:
            _minute_cache.clear()
        _minute_cache[key] = (now, result)
    return json.loads(json.dumps(result))


# ---------------------------------------------------------------- 交易日历与交易状态

_calendar: dict[str, Any] = {"days": None, "loaded_at": 0.0, "source": ""}
_calendar_lock = threading.Lock()


def _calendar_file():
    return config.CACHE_DIR.joinpath("trade_calendar.json")


def _calendar_from_sina() -> list[date]:
    import akshare as ak

    df = net.call_with_fallback(lambda: ak.tool_trade_date_hist_sina(), retries=2, empty_ok=False)
    return sorted({d if isinstance(d, date) else date.fromisoformat(str(d)[:10]) for d in df["trade_date"]})


def _calendar_from_baostock(timeout: float = 20.0) -> list[date]:
    """baostock query_trade_dates（后台线程 + 硬超时，与其他 baostock 调用共用一把锁）"""
    result: dict[str, Any] = {}

    def worker() -> None:
        import baostock as bs

        if not uni_mod.BAOSTOCK_LOCK.acquire(timeout=timeout):
            result["error"] = "baostock 正忙"
            return
        try:
            with net.domestic_direct():
                lg = bs.login()
                if lg.error_code != "0":
                    raise ConnectionError(lg.error_msg)
                try:
                    end: date = date(china_now().year, 12, 31)
                    rs = bs.query_trade_dates(start_date="2014-01-01", end_date=end.isoformat())
                    rows: list[list[str]] = []
                    while rs.error_code == "0" and rs.next():
                        rows.append(rs.get_row_data())
                    result["days"] = [date.fromisoformat(r[0]) for r in rows if len(r) > 1 and r[1] == "1"]
                finally:
                    bs.logout()
        except Exception as e:  # noqa: BLE001  baostock 失败只影响日历来源
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            uni_mod.BAOSTOCK_LOCK.release()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if not result.get("days"):
        raise ConnectionError(f"baostock 交易日历获取失败：{result.get('error', '超时')}")
    return result["days"]


def _load_calendar_file() -> tuple[list[date], float] | None:
    try:
        obj: dict = json.loads(_calendar_file().read_text(encoding="utf-8"))
        return [date.fromisoformat(d) for d in obj["days"]], float(obj["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def trade_calendar(refresh: bool = False) -> list[date]:
    """交易日历（升序，含今年剩余的已公布交易日）：新浪 → baostock → 本地缓存；都失败返回空表"""
    with _calendar_lock:
        now: float = time.time()
        # 注意用 is not None：两个来源都失败时缓存的是空表，1 小时内不再重试，免得每次判断交易状态都卡住
        if not refresh and _calendar["days"] is not None and now - _calendar["loaded_at"] < 6 * 3600:
            return _calendar["days"]
        cached = None if refresh else _load_calendar_file()
        if cached and now - cached[1] < CALENDAR_MAX_AGE and cached[0] and cached[0][-1].year >= china_now().year:
            _calendar.update(days=cached[0], loaded_at=now, source="cache")
            return cached[0]
        days: list[date] = []
        for name, fetch in (("sina", _calendar_from_sina), ("baostock", _calendar_from_baostock)):
            try:
                days = fetch()
            except Exception as e:  # noqa: BLE001  逐个来源尝试
                logger.warning("交易日历（%s）获取失败：%s", name, e)
                continue
            if days:
                _calendar.update(days=days, loaded_at=now, source=name)
                try:
                    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    _calendar_file().write_text(json.dumps(
                        {"days": [d.isoformat() for d in days], "fetched_at": now, "source": name}), encoding="utf-8")
                except OSError:
                    pass
                return days
        if cached:
            _calendar.update(days=cached[0], loaded_at=now - 5 * 3600, source="stale-cache")
            return cached[0]
        _calendar.update(days=[], loaded_at=now - 5 * 3600, source="none")
        return []


_cal_set: dict[int, set[date]] = {}


def _calendar_set(cal: list[date]) -> set[date]:
    key: int = id(cal)
    if key not in _cal_set:
        _cal_set.clear()
        _cal_set[key] = set(cal)
    return _cal_set[key]


def _panel_dates() -> set[date]:
    try:
        from . import history

        return set(history.trade_dates(start=china_now().date() - timedelta(days=60)))
    except Exception:  # noqa: BLE001  面板不存在时忽略
        return set()


def is_trading_day(day: date) -> bool:
    """是否交易日：日历覆盖的日期以日历为准；日历没覆盖（如明年、或接口都失败）就按"周一到周五"估计"""
    if day.weekday() >= 5:
        return False
    cal: list[date] = trade_calendar()
    if cal and cal[0] <= day <= cal[-1]:
        return day in _calendar_set(cal)
    panel: set[date] = _panel_dates()
    if panel and min(panel) <= day <= max(panel):
        return day in panel
    return True


def recent_trading_days(n: int, until: date | None = None) -> list[date]:
    """截至 until（含）最近 n 个交易日，升序"""
    until = until or china_now().date()
    out: list[date] = []
    day: date = until
    for _ in range(n * 3 + 30):
        if is_trading_day(day):
            out.append(day)
            if len(out) >= n:
                break
        day -= timedelta(days=1)
    return sorted(out)


def market_phase(now: datetime | None = None) -> str:
    """"盘前"/"交易中"/"午间休市"/"已收盘"/"休市"（北京时间 + 交易日历；9:15~9:30 集合竞价算盘前）"""
    now = (now or china_now()).astimezone(config.CHINA_TZ)
    if not is_trading_day(now.date()):
        return "休市"
    minutes: int = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        return "盘前"
    if minutes < 11 * 60 + 30:
        return "交易中"
    if minutes < 13 * 60:
        return "午间休市"
    if minutes < 15 * 60:
        return "交易中"
    return "已收盘"
