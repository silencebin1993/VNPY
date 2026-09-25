"""
东方财富涨停板行情（push2ex）与龙虎榜（datacenter-web）。

- 涨停/昨日涨停/炸板/跌停/强势/次新 六类股池：直接请求 push2ex（与 akshare stock_zt_pool_*_em 同一接口，
  按字段名映射，比 akshare 的按位置改列名更稳，并且有超时和直连/代理切换）；直连失败再退回 akshare 函数。
  实测（2026-09-24）push2ex 只保留最近约 15 个交易日，更早的日期返回空 → 每天收盘后存档，首次可 backfill 补最近15天。
- 龙虎榜：RPT_DAILYBILLBOARD_DETAILSNEW（即 ak.stock_lhb_detail_em），实测最早到 2004-06-25；每页最多 500 行，按月分段抓取。
  不保存"上榜后1/2/5/10日涨幅"这些事后才知道的列，防止被当成特征误用（未来函数）。
接口不可用时返回空表并记录警告，不让上层崩溃。
"""
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from .. import config, net
from . import universe as uni_mod


logger = logging.getLogger(__name__)

KINDS: dict[str, str] = {"zt": "涨停", "zb": "炸板", "dt": "跌停", "prev": "昨日涨停", "strong": "强势", "sub_new": "次新"}

PUSH2EX_URL: str = "https://push2ex.eastmoney.com/"
PUSH2EX_UT: str = "7eea3edcaed734bea9cbfc24409ed989"
# kind → (接口, 排序, akshare 函数名)
ENDPOINTS: dict[str, tuple[str, str, str]] = {
    "zt": ("getTopicZTPool", "fbt:asc", "stock_zt_pool_em"),
    "prev": ("getYesterdayZTPool", "zs:desc", "stock_zt_pool_previous_em"),
    "zb": ("getTopicZBPool", "fbt:asc", "stock_zt_pool_zbgc_em"),
    "dt": ("getTopicDTPool", "fund:asc", "stock_zt_pool_dtgc_em"),
    "strong": ("getTopicQSPool", "zdp:desc", "stock_zt_pool_strong_em"),
    "sub_new": ("getTopicCXPooll", "ods:asc", "stock_zt_pool_sub_new_em"),
}
POOL_DEPTH_DAYS: int = 15       # push2ex 能取到的历史交易日数（实测）
CLOSE_MINUTE: int = 15 * 60 + 5
POOL_TTL_TODAY: float = 15.0
POOL_TTL_PAST: float = 3600.0

POOL_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date,
    "code": pl.Utf8,
    "name": pl.Utf8,
    "pct": pl.Float64,
    "price": pl.Float64,
    "amount": pl.Float64,
    "float_cap": pl.Float64,
    "total_cap": pl.Float64,
    "turn": pl.Float64,
    "seal_amount": pl.Float64,      # 封板资金（跌停池为封单资金）
    "first_time": pl.Utf8,          # 首次封板时间 HH:MM:SS（昨日涨停池为昨日封板时间）
    "last_time": pl.Utf8,
    "open_times": pl.Int32,         # 炸板次数（跌停池为开板次数）
    "streak": pl.Int32,             # 连板数（昨日涨停池为昨日连板数，跌停池为连续跌停天数）
    "industry": pl.Utf8,
    "stat": pl.Utf8,                # 涨停统计 "天数/涨停次数"，如 "5/4" = 最近5天4次涨停
    "stat_days": pl.Int32,
    "stat_count": pl.Int32,
    "limit_price": pl.Float64,
    "speed": pl.Float64,            # 涨速
    "amplitude": pl.Float64,
    "pe": pl.Float64,
    "board_amount": pl.Float64,     # 板上成交额（跌停池）
    "volume_ratio": pl.Float64,
    "is_new_high": pl.Boolean,
    "reason": pl.Utf8,              # 入选理由（强势池）
    "open_days": pl.Int32,          # 开板几日（次新池）
    "open_date": pl.Date,
    "list_date": pl.Date,
    "fetched_at": pl.Utf8,          # 抓取时间（北京时间）
}

STRONG_REASONS: dict[int, str] = {1: "60日新高", 2: "近期多次涨停", 3: "60日新高且近期多次涨停"}

# push2ex 原始字段 → 统一列名（不需要换算的）
_RAW_FIELDS: dict[str, str] = {
    "c": "code", "n": "name", "zdp": "pct", "amount": "amount", "ltsz": "float_cap", "tshare": "total_cap",
    "hs": "turn", "hybk": "industry", "fund": "seal_amount", "zbc": "open_times", "lbc": "streak",
    "zf": "amplitude", "zs": "speed", "lb": "volume_ratio", "pe": "pe", "fba": "board_amount", "ods": "open_days",
}
_RAW_TIMES: dict[str, str] = {"fbt": "first_time", "lbt": "last_time", "yfbt": "first_time"}

# akshare 中文列 → 统一列名
AK_COLUMNS: dict[str, str] = {
    "代码": "code", "名称": "name", "涨跌幅": "pct", "最新价": "price", "成交额": "amount",
    "流通市值": "float_cap", "总市值": "total_cap", "换手率": "turn", "转手率": "turn",
    "封板资金": "seal_amount", "封单资金": "seal_amount",
    "首次封板时间": "first_time", "昨日封板时间": "first_time", "最后封板时间": "last_time",
    "炸板次数": "open_times", "开板次数": "open_times",
    "连板数": "streak", "昨日连板数": "streak", "连续跌停": "streak",
    "所属行业": "industry", "涨停统计": "stat", "涨停价": "limit_price", "涨速": "speed", "振幅": "amplitude",
    "动态市盈率": "pe", "板上成交额": "board_amount", "量比": "volume_ratio", "是否新高": "is_new_high",
    "入选理由": "reason", "开板几日": "open_days", "开板日期": "open_date", "上市日期": "list_date",
}

LHB_URL: str = "https://datacenter-web.eastmoney.com/api/data/v1/get"
LHB_PAGE: int = 500
LHB_EARLIEST: date = date(2004, 6, 25)     # 实测最早上榜日
LHB_COLUMNS: str = (
    "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLAIN,CLOSE_PRICE,CHANGE_RATE,BILLBOARD_NET_AMT,"
    "BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,BILLBOARD_DEAL_AMT,ACCUM_AMOUNT,DEAL_NET_RATIO,DEAL_AMOUNT_RATIO,"
    "TURNOVERRATE,FREE_MARKET_CAP,EXPLANATION"
)
LHB_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date,
    "code": pl.Utf8,
    "name": pl.Utf8,
    "reason": pl.Utf8,              # 上榜原因
    "net_buy": pl.Float64,          # 龙虎榜净买额（元）
    "buy": pl.Float64,
    "sell": pl.Float64,
    "amount_total": pl.Float64,     # 龙虎榜成交额
    "market_amount": pl.Float64,    # 当日市场总成交额
    "turnover": pl.Float64,         # 换手率 %
    "close": pl.Float64,
    "pct": pl.Float64,
    "explain": pl.Utf8,             # 解读，如"2家机构买入"
    "net_ratio": pl.Float64,        # 净买额占总成交比 %
    "amount_ratio": pl.Float64,     # 龙虎榜成交额占总成交比 %
    "float_cap": pl.Float64,
}
_LHB_FIELDS: dict[str, str] = {
    "SECURITY_CODE": "code", "SECURITY_NAME_ABBR": "name", "EXPLANATION": "reason",
    "BILLBOARD_NET_AMT": "net_buy", "BILLBOARD_BUY_AMT": "buy", "BILLBOARD_SELL_AMT": "sell",
    "BILLBOARD_DEAL_AMT": "amount_total", "ACCUM_AMOUNT": "market_amount", "TURNOVERRATE": "turnover",
    "CLOSE_PRICE": "close", "CHANGE_RATE": "pct", "EXPLAIN": "explain", "DEAL_NET_RATIO": "net_ratio",
    "DEAL_AMOUNT_RATIO": "amount_ratio", "FREE_MARKET_CAP": "float_cap",
}

_cache_lock = threading.Lock()
_pool_cache: dict[tuple[str, date], tuple[float, pl.DataFrame]] = {}


# ---------------------------------------------------------------- 解析

def china_now() -> datetime:
    return datetime.now(config.CHINA_TZ)


def time_text(value: Any) -> str | None:
    """92500 / "092500" → "09:25:00"；0/空 → None"""
    if value is None:
        return None
    s: str = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit() or int(s) == 0:
        return None
    s = s.zfill(6)
    return f"{s[:2]}:{s[2:4]}:{s[4:6]}"


def _int_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s: str = str(value or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    try:
        if len(s) == 8 and s.isdigit() and int(s) > 0:
            return datetime.strptime(s, "%Y%m%d").date()
        if len(s) >= 10:
            return date.fromisoformat(s[:10])
    except ValueError:
        return None
    return None


def _split_stat(stat: str | None) -> tuple[int | None, int | None]:
    try:
        a, b = str(stat).split("/")
        return int(a), int(b)
    except (ValueError, AttributeError):
        return None, None


def _to_frame(rows: list[dict], day: date) -> pl.DataFrame:
    fetched: str = china_now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        r["date"] = day
        r.setdefault("fetched_at", fetched)
        if r.get("stat") and r.get("stat_days") is None:
            r["stat_days"], r["stat_count"] = _split_stat(r["stat"])
    if not rows:
        return pl.DataFrame(schema=POOL_SCHEMA)
    df = pl.DataFrame([{k: r.get(k) for k in POOL_SCHEMA} for r in rows], schema=POOL_SCHEMA, orient="row",
                      strict=False)
    return df.with_columns(pl.col("code").str.zfill(6)).filter(pl.col("code").is_not_null()).unique(
        "code", keep="first", maintain_order=True)


def _float(value: Any) -> float | None:
    try:
        f: float = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f     # NaN → None


def parse_pool_raw(kind: str, pool: list[dict], day: date) -> pl.DataFrame:
    """push2ex 原始 pool 列表 → 统一列"""
    rows: list[dict] = []
    for item in pool or []:
        if not isinstance(item, dict) or not item.get("c"):
            continue
        r: dict = {}
        for src, dst in _RAW_FIELDS.items():
            if src in item:
                r[dst] = item[src] if dst in ("code", "name", "industry") else _float(item[src])
        for src, dst in _RAW_TIMES.items():
            if src in item:
                r[dst] = time_text(item[src])
        if "p" in item:
            r["price"] = _float(item["p"]) / 1000 if _float(item["p"]) is not None else None
        ztp: float | None = _float(item.get("ztp"))
        r["limit_price"] = ztp / 1000 if ztp is not None and 0 < ztp < 1e8 else None
        if kind == "prev" and "ylbc" in item:
            r["streak"] = _float(item["ylbc"])
        if kind == "dt":
            r["streak"] = _float(item.get("days"))
            r["open_times"] = _float(item.get("oc"))
        zttj = item.get("zttj")
        if isinstance(zttj, dict) and "days" in zttj:
            r["stat_days"], r["stat_count"] = int(zttj.get("days") or 0), int(zttj.get("ct") or 0)
            r["stat"] = f"{r['stat_days']}/{r['stat_count']}"
        if "nh" in item:
            r["is_new_high"] = bool(item["nh"])
        if "cc" in item:
            r["reason"] = STRONG_REASONS.get(int(item["cc"] or 0), str(item["cc"]))
        if "od" in item:
            r["open_date"] = _int_date(item["od"])
        if "ipod" in item:
            r["list_date"] = _int_date(item["ipod"])
        for key in ("open_times", "streak", "open_days"):
            if r.get(key) is not None:
                r[key] = int(r[key])
        rows.append(r)
    return _to_frame(rows, day)


def parse_pool_ak(df: Any, day: date) -> pl.DataFrame:
    """akshare stock_zt_pool_*_em 返回的中文列 DataFrame（pandas）→ 统一列"""
    if df is None or len(df) == 0:
        return pl.DataFrame(schema=POOL_SCHEMA)
    rows: list[dict] = []
    for rec in df.to_dict("records"):
        r: dict = {}
        for zh, en in AK_COLUMNS.items():
            if zh not in rec:
                continue
            v = rec[zh]
            if en in ("first_time", "last_time"):
                r[en] = time_text(v)
            elif en in ("code", "name", "industry", "stat", "reason"):
                r[en] = None if v is None or (isinstance(v, float) and v != v) else str(v)
            elif en == "is_new_high":
                r[en] = v in ("是", 1, True)
            elif en in ("open_date", "list_date"):
                r[en] = _int_date(v)
            elif en in ("open_times", "streak", "open_days"):
                f = _float(v)
                r[en] = int(f) if f is not None else None
            else:
                r[en] = _float(v)
        rows.append(r)
    return _to_frame(rows, day)


# ---------------------------------------------------------------- 抓取

def _fetch_raw(kind: str, day: date) -> pl.DataFrame:
    path, sort, _ = ENDPOINTS[kind]
    payload = net.get_json(PUSH2EX_URL + path, params={
        "ut": PUSH2EX_UT, "dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "10000", "sort": sort,
        "date": day.strftime("%Y%m%d"),
    }, timeout=10)
    if not isinstance(payload, dict):
        raise ConnectionError("东方财富涨停池返回格式异常")
    data = payload.get("data") or {}
    return parse_pool_raw(kind, data.get("pool") or [], day)


def _fetch_ak(kind: str, day: date) -> pl.DataFrame:
    import akshare as ak

    func = getattr(ak, ENDPOINTS[kind][2])
    pdf = net.call_with_fallback(lambda: func(date=day.strftime("%Y%m%d")), retries=2)
    return parse_pool_ak(pdf, day)


def fetch_pool(kind: str, day: date) -> pl.DataFrame:
    """抓取某日股池（统一英文列）。push2ex 只有最近约15个交易日；失败返回空表并记录警告"""
    if kind not in KINDS:
        raise ValueError(f"未知股池类型：{kind}（可选 {', '.join(KINDS)}）")
    key: tuple[str, date] = (kind, day)
    ttl: float = POOL_TTL_TODAY if day >= china_now().date() else POOL_TTL_PAST
    now: float = time.time()
    with _cache_lock:
        hit = _pool_cache.get(key)
        if hit and now - hit[0] <= ttl:
            return hit[1]
    try:
        df: pl.DataFrame = _fetch_raw(kind, day)
    except Exception as e:  # noqa: BLE001  直连接口失败再试 akshare
        logger.warning("东方财富%s池直连失败（%s），改用 akshare", KINDS[kind], e)
        try:
            df = _fetch_ak(kind, day)
        except Exception as e2:  # noqa: BLE001
            logger.warning("东方财富%s池获取失败：%s", KINDS[kind], e2)
            return pl.DataFrame(schema=POOL_SCHEMA)
    with _cache_lock:
        if len(_pool_cache) > 200:
            _pool_cache.clear()
        _pool_cache[key] = (now, df)
    return df


# ---------------------------------------------------------------- 存档

def _pool_file(kind: str, day: date) -> Path:
    return config.POOLS_DIR.joinpath(kind, f"{day:%Y%m%d}.parquet")


def archive(day: date, kinds: list[str] | None = None, force: bool = False) -> dict:
    """抓取并存档股池 → POOLS_DIR/{kind}/{YYYYMMDD}.parquet（默认全部六类）。

    当天未收盘（北京时间 15:05 前）不存档，除非 force=True。返回 {"date","counts","failed","skipped","warnings"}
    """
    kinds = kinds or list(KINDS)
    result: dict = {"date": day.isoformat(), "counts": {}, "failed": [], "skipped": None, "warnings": []}
    now: datetime = china_now()
    if day > now.date():
        result["skipped"] = f"{day} 还没到，不能存档"
        return result
    if not force:
        try:
            from . import realtime

            if not realtime.is_trading_day(day):
                result["skipped"] = f"{day} 不是交易日，没有股池数据"
                return result
        except Exception:  # noqa: BLE001  日历不可用时照常抓取
            pass
        if day == now.date() and now.hour * 60 + now.minute < CLOSE_MINUTE:
            result["skipped"] = "当天还没收盘，股池数据不完整，收盘后（15:05以后）再存档"
            return result
    for kind in kinds:
        with _cache_lock:
            _pool_cache.pop((kind, day), None)
        df: pl.DataFrame = fetch_pool(kind, day)
        result["counts"][kind] = df.height
        if df.is_empty():
            # 跌停/炸板池可能真的为空；涨停池为空多半是接口问题或非交易日
            if kind in ("zt", "prev", "strong"):
                result["failed"].append(kind)
            continue
        path: Path = _pool_file(kind, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path = path.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(path)
    if result["failed"]:
        result["warnings"].append(
            "以下股池没有取到数据：" + "、".join(KINDS[k] for k in result["failed"])
            + "（非交易日、超过约15个交易日的历史日期或接口暂时不可用）"
        )
    return result


def archived_days(kind: str) -> list[date]:
    folder: Path = config.POOLS_DIR.joinpath(kind)
    if not folder.exists():
        return []
    days: list[date] = []
    for p in folder.glob("*.parquet"):
        d: date | None = _int_date(p.stem)
        if d:
            days.append(d)
    return sorted(days)


def backfill(days: int = POOL_DEPTH_DAYS, progress: Callable[[float, str], None] | None = None) -> dict:
    """补存最近 days 个已收盘交易日里还没存档的股池（push2ex 只保留约15个交易日）"""
    from . import realtime

    now: datetime = china_now()
    until: date = now.date() if now.hour * 60 + now.minute >= CLOSE_MINUTE else now.date() - timedelta(days=1)
    targets: list[date] = realtime.recent_trading_days(days, until=until)
    have: dict[str, set[date]] = {k: set(archived_days(k)) for k in KINDS}
    done: list[str] = []
    for i, day in enumerate(targets):
        missing: list[str] = [k for k in KINDS if day not in have[k]]
        if progress:
            progress(i / max(len(targets), 1), f"正在存档 {day} 的涨停池…")
        if missing:
            res: dict = archive(day, kinds=missing)
            if any(res["counts"].get(k) for k in missing):
                done.append(day.isoformat())
    if progress:
        progress(1.0, f"涨停池存档完成，新补 {len(done)} 天")
    return {"days": [d.isoformat() for d in targets], "archived": done}


def load_archive(kind: str, start: str | date | None = None, end: str | date | None = None) -> pl.DataFrame:
    """读取股池存档（按 date, code 排序）；没有存档时返回空表"""
    start_d: date | None = _int_date(start) if isinstance(start, str) else start
    end_d: date | None = _int_date(end) if isinstance(end, str) else end
    files: list[Path] = []
    for day in archived_days(kind):
        if (start_d is None or day >= start_d) and (end_d is None or day <= end_d):
            files.append(_pool_file(kind, day))
    if not files:
        return pl.DataFrame(schema=POOL_SCHEMA)
    frames: list[pl.DataFrame] = []
    for p in files:
        try:
            frames.append(pl.read_parquet(p))
        except Exception as e:  # noqa: BLE001  单个文件损坏跳过
            logger.warning("股池存档读取失败 %s：%s", p, e)
    if not frames:
        return pl.DataFrame(schema=POOL_SCHEMA)
    df: pl.DataFrame = pl.concat(frames, how="diagonal_relaxed")
    for col, dtype in POOL_SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    return df.select([pl.col(c).cast(t, strict=False) for c, t in POOL_SCHEMA.items()]).sort(["date", "code"])


def get_pool(kind: str, day: date) -> pl.DataFrame:
    """页面用：有存档读存档，否则实时抓取"""
    if _pool_file(kind, day).exists():
        return load_archive(kind, day, day)
    return fetch_pool(kind, day)


# ---------------------------------------------------------------- 龙虎榜

def parse_lhb_rows(rows: list[dict]) -> pl.DataFrame:
    """datacenter-web 龙虎榜明细 → LHB_SCHEMA（只保留 A 股）"""
    out: list[dict] = []
    for item in rows or []:
        code: str = str(item.get("SECURITY_CODE") or "").zfill(6)
        if not uni_mod.is_a_share(code):
            continue
        r: dict = {"date": _int_date(str(item.get("TRADE_DATE") or "")[:10])}
        for src, dst in _LHB_FIELDS.items():
            v = item.get(src)
            r[dst] = (str(v).strip() if v is not None else None) if dst in ("code", "name", "reason", "explain") \
                else _float(v)
        r["code"] = code
        r["name"] = uni_mod.clean_name(r.get("name") or "")
        out.append(r)
    if not out:
        return pl.DataFrame(schema=LHB_SCHEMA)
    return pl.DataFrame(out, schema=LHB_SCHEMA, orient="row", strict=False).filter(pl.col("date").is_not_null())


def _lhb_page(start: date, end: date, page: int, code: str | None = None) -> tuple[list[dict], int]:
    flt: str = f"(TRADE_DATE<='{end.isoformat()}')(TRADE_DATE>='{start.isoformat()}')"
    if code:
        flt += f'(SECURITY_CODE="{code}")'
    payload = net.get_json(LHB_URL, params={
        "sortColumns": "TRADE_DATE,SECURITY_CODE", "sortTypes": "-1,1", "pageSize": str(LHB_PAGE),
        "pageNumber": str(page), "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": LHB_COLUMNS,
        "source": "WEB", "client": "WEB", "filter": flt,
    }, timeout=20)
    if not isinstance(payload, dict):
        raise ConnectionError("龙虎榜接口返回格式异常")
    result = payload.get("result")
    if not result:     # 没有数据时 result 为 null（code=9201）
        return [], 0
    return result.get("data") or [], int(result.get("pages") or 0)


def fetch_lhb(start: date, end: date, code: str | None = None) -> pl.DataFrame:
    """抓取 [start, end] 的龙虎榜明细（自动翻页）"""
    rows, pages = _lhb_page(start, end, 1, code)
    for page in range(2, pages + 1):
        more, _ = _lhb_page(start, end, page, code)
        rows.extend(more)
    return parse_lhb_rows(rows)


def _lhb_file() -> Path:
    return config.STOCK_LAB.joinpath("lhb.parquet")


def load_lhb() -> pl.DataFrame:
    """龙虎榜：date, code, name, reason, net_buy, buy, sell, amount_total, market_amount, turnover, close, pct, ..."""
    path: Path = _lhb_file()
    if not path.exists():
        return pl.DataFrame(schema=LHB_SCHEMA)
    try:
        df: pl.DataFrame = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("龙虎榜文件读取失败：%s", e)
        return pl.DataFrame(schema=LHB_SCHEMA)
    for col, dtype in LHB_SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    return df.select([pl.col(c).cast(t) for c, t in LHB_SCHEMA.items()])


def _merge_lhb(old: pl.DataFrame, new: list[pl.DataFrame]) -> pl.DataFrame:
    frames: list[pl.DataFrame] = [old, *new] if not old.is_empty() else list(new)
    if not frames:
        return pl.DataFrame(schema=LHB_SCHEMA)
    df: pl.DataFrame = pl.concat(frames, how="vertical_relaxed")
    return df.unique(["date", "code", "reason"], keep="last").sort(["date", "code", "reason"])


def _save_lhb(df: pl.DataFrame) -> None:
    config.STOCK_LAB.mkdir(parents=True, exist_ok=True)
    path: Path = _lhb_file()
    tmp: Path = path.with_suffix(".tmp")
    df.write_parquet(tmp)
    tmp.replace(path)


def _months(start: date, end: date) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cur: date = start
    while cur <= end:
        nxt: date = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        out.append((cur, min(nxt - timedelta(days=1), end)))
        cur = nxt
    return out


def update_lhb(start: date | None = None, progress: Callable[[float, str], None] | None = None,
               workers: int = 4) -> dict:
    """龙虎榜增量更新 → STOCK_LAB/lhb.parquet。

    start 为空时：已有数据则从最后日期前7天开始（补抓晚公布的记录），否则从 HISTORY_START 起；按月分段、多线程。
    返回 {"rows","added","start","end","months","failed","seconds","earliest"}
    """
    t0: float = time.time()
    old: pl.DataFrame = load_lhb()
    if start is None:
        last: date | None = old["date"].max() if not old.is_empty() else None
        start = last - timedelta(days=7) if last else date.fromisoformat(config.HISTORY_START)
    start = max(start, LHB_EARLIEST)
    end: date = china_now().date()
    months: list[tuple[date, date]] = _months(start, end)
    frames: list[pl.DataFrame] = []
    failed: list[str] = []
    n_done: int = 0
    merged: pl.DataFrame = old

    def report(msg: str) -> None:
        if progress:
            progress(n_done / max(len(months), 1), msg)

    report(f"正在下载龙虎榜（{start} 至 {end}，共 {len(months)} 个月）…")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_lhb, a, b): (a, b) for a, b in months}
        for fut in as_completed(futures):
            a, _ = futures[fut]
            n_done += 1
            try:
                frames.append(fut.result())
            except Exception as e:  # noqa: BLE001  单月失败记录下来，下次增量会补
                failed.append(f"{a:%Y-%m}")
                logger.warning("龙虎榜 %s 下载失败：%s", f"{a:%Y-%m}", e)
            if n_done % 24 == 0:        # 分批落盘，中断后已下载的不丢
                merged = _merge_lhb(merged, frames)
                frames = []
                _save_lhb(merged)
            report(f"龙虎榜已下载 {n_done}/{len(months)} 个月")
    merged = _merge_lhb(merged, frames)
    if not merged.is_empty() and not merged.equals(old):     # 没有新记录就不重写文件
        _save_lhb(merged)
    if failed:
        logger.warning("龙虎榜有 %d 个月下载失败：%s", len(failed), ",".join(sorted(failed)))
    result: dict = {
        "rows": merged.height, "added": merged.height - old.height, "start": start.isoformat(),
        "end": end.isoformat(), "months": len(months), "failed": sorted(failed),
        "seconds": round(time.time() - t0, 1),
        "earliest": merged["date"].min().isoformat() if not merged.is_empty() else None,
    }
    if progress:
        progress(1.0, f"龙虎榜更新完成：共 {result['rows']} 条，新增 {result['added']} 条"
                      + (f"，{len(failed)} 个月失败" if failed else ""))
    return result
