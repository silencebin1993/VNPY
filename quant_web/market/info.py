"""
个股资料：基本信息、估值、财报、概念、资金流、龙虎榜、新闻、近一年涨停记录。

每一部分单独获取、互不影响（并发执行），失败的部分返回空并在 errors 里写明原因。结果缓存 10 分钟。
数据源（本机实测）：
- 估值：腾讯实时报价（市盈率TTM、市净率、市值）；财报：本地基本面表最新一期，没有就现查同花顺；
- 概念：同花顺 F10 概念题材页（basic.10jqka.com.cn/{code}/concept.html），失败再用东方财富 F10 核心题材
  （emweb，IS_PRECISE=1 的才是概念，行业/地域/指数成分/风格标签不算）；主营业务也取自东方财富 F10；
- 资金流：东方财富资金流（push2his）本机不可用，改用新浪 MoneyFlow.ssl_qsfx_lscjfb（逐日：特大单/大单/小单/散单净额，
  主力 = 特大单 + 大单）；
- 龙虎榜：本地 lhb.parquet（pools.update_lhb），本地没有或过旧时现查该股近90天；
- 新闻：ak.stock_news_em；涨停记录：日线面板 + 涨跌停规则。
"""
import html
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from .. import config, net
from . import universe as uni_mod


logger = logging.getLogger(__name__)

CACHE_SECONDS: float = 600.0
ERROR_CACHE_SECONDS: float = 120.0          # 有部分失败时缓存短一些，便于稍后重试
FUND_FLOW_DAYS: int = 20
LHB_LIVE_DAYS: int = 90
LIMIT_HISTORY_DAYS: int = 250

THS_CONCEPT_URL: str = "https://basic.10jqka.com.cn/{code}/concept.html"
EM_F10_URL: str = "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
SINA_FLOW_URL: str = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb"

PARTS: list[str] = ["basic", "valuation", "finance", "concepts", "fund_flow", "lhb", "news", "limit_history"]
PART_LABELS: dict[str, str] = {
    "basic": "基本信息", "valuation": "估值", "finance": "财报", "concepts": "概念题材", "fund_flow": "资金流向",
    "lhb": "龙虎榜", "news": "个股新闻", "limit_history": "涨停记录",
}

_lock = threading.Lock()
_mem_cache: dict[str, tuple[float, dict]] = {}


def china_today() -> date:
    return datetime.now(config.CHINA_TZ).date()


def _clean(value: Any) -> Any:
    """转成可 JSON 序列化的值：日期→字符串，NaN→None"""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _float(value: Any) -> float | None:
    try:
        f: float = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


# ---------------------------------------------------------------- 解析（便于单测）

def parse_ths_concepts(page: str) -> list[str]:
    """同花顺概念题材页 → 概念名列表"""
    names: list[str] = []
    for raw in re.findall(r'<td[^>]*class="gnName"[^>]*>(.*?)</td>', page, re.S):
        name: str = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if name and name not in names:
            names.append(name)
    return names


def parse_em_core(payload: Any) -> tuple[list[str], str | None]:
    """东方财富 F10 核心题材 → (概念列表, 主营业务)"""
    if not isinstance(payload, dict):
        return [], None
    concepts: list[str] = []
    for b in payload.get("ssbk") or []:
        name: str = str(b.get("BOARD_NAME") or "").strip()
        if str(b.get("IS_PRECISE")) == "1" and name and name not in concepts:
            concepts.append(name)
    business: list[str] = [str(h.get("KEYWORD") or "").strip() for h in payload.get("hxtc") or []
                           if h.get("KEY_CLASSIF") == "主营业务" and h.get("KEYWORD")]
    return concepts, "、".join(dict.fromkeys(business)) or None


def parse_sina_flow(rows: Any, days: int = FUND_FLOW_DAYS) -> list[dict]:
    """新浪逐日资金流 → [{"date","main_net","main_pct","super_net","big_net","mid_net","small_net"}]（按日期升序）。

    r0/r1/r2/r3 = 特大单/大单/小单/散单 的成交额，*_net 为净流入（元）；主力 = 特大单 + 大单，
    main_pct = 主力净流入 / 四类成交额合计 ×100。
    """
    out: list[dict] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("opendate"):
            continue
        nets: list[float | None] = [_float(r.get(f"r{i}_net")) for i in range(4)]
        total: float = sum(_float(r.get(f"r{i}")) or 0.0 for i in range(4))
        main: float | None = None if nets[0] is None or nets[1] is None else nets[0] + nets[1]
        out.append({
            "date": str(r["opendate"])[:10],
            "main_net": round(main, 2) if main is not None else None,
            "main_pct": round(main / total * 100, 2) if main is not None and total > 0 else None,
            "super_net": nets[0], "big_net": nets[1], "mid_net": nets[2], "small_net": nets[3],
            "net": _float(r.get("netamount")),
        })
    out.sort(key=lambda x: x["date"])
    return out[-days:]


def latest_finance(fund: pl.DataFrame, code: str) -> dict | None:
    """基本面表里该股最新一期"""
    if fund.is_empty():
        return None
    rows: pl.DataFrame = fund.filter(pl.col("code") == code).sort("report_date")
    if rows.is_empty():
        return None
    r: dict = rows.row(-1, named=True)
    out: dict = {"report_date": r["report_date"]}
    for key in ("revenue", "revenue_yoy", "net_profit", "net_profit_yoy", "roe", "eps", "eps_ttm", "bvps",
                "debt_ratio", "gross_margin"):
        v: float | None = _float(r.get(key))
        out[key] = round(v, 4) if v is not None else None
    return out


# ---------------------------------------------------------------- 各部分

def _basic(code: str) -> dict:
    uni: pl.DataFrame = uni_mod.load_universe()
    row: pl.DataFrame = uni.filter(pl.col("code") == code)
    info: dict = {"exchange": uni_mod.exchange_of(code), "board": uni_mod.board_of(code)}
    if row.is_empty():
        return info
    r: dict = row.row(0, named=True)
    info.update(name=r["name"], industry=r["industry"], list_date=r["list_date"], is_st=r["is_st"],
                status=r["status"])
    return info


def _valuation(code: str) -> dict:
    from . import realtime

    q: dict | None = realtime.quote(code)
    if not q:
        raise ConnectionError("腾讯行情没有这只股票的报价")
    return {"pe_ttm": q.get("pe"), "pb": q.get("pb"), "total_cap": q.get("total_cap"),
            "float_cap": q.get("float_cap"), "price": q.get("price"), "pct": q.get("pct"), "name": q.get("name")}


def _finance(code: str) -> dict | None:
    from . import fundamentals

    fin: dict | None = latest_finance(fundamentals.load_fundamentals(), code)
    if fin is None:
        fin = latest_finance(fundamentals.fetch_one(code), code)
    return fin


def _concepts(code: str) -> dict:
    """{"concepts": [...], "business": str|None, "error": str|None}"""
    concepts: list[str] = []
    errors: list[str] = []
    try:
        r = net.get(THS_CONCEPT_URL.format(code=code), headers={"Referer": "https://basic.10jqka.com.cn/"},
                    timeout=10, retries=2)
        concepts = parse_ths_concepts(r.content.decode("gbk", errors="replace"))
    except Exception as e:  # noqa: BLE001
        errors.append(f"同花顺概念：{str(e)[:60]}")
    business: str | None = None
    try:
        payload = net.get_json(EM_F10_URL, params={"code": uni_mod.market_symbol(code).upper()}, timeout=10, retries=2)
        em_concepts, business = parse_em_core(payload)
        if not concepts:
            concepts = em_concepts
    except Exception as e:  # noqa: BLE001
        errors.append(f"东方财富F10：{str(e)[:60]}")
    error: str | None = None
    if not concepts:
        error = "没有取到概念题材" + (f"（{'；'.join(errors)}）" if errors else "（数据源没有收录）")
    return {"concepts": concepts, "business": business, "error": error}


def _fund_flow(code: str) -> list[dict]:
    rows = net.get_json(SINA_FLOW_URL, params={
        "page": 1, "num": FUND_FLOW_DAYS, "sort": "opendate", "asc": 0, "daima": uni_mod.market_symbol(code),
    }, timeout=10, retries=2)
    if isinstance(rows, dict) and rows.get("__ERROR"):
        raise ConnectionError(f"新浪资金流接口报错：{rows.get('__ERRORMSG')}")
    return parse_sina_flow(rows)


def _lhb(code: str) -> list[dict]:
    from . import pools

    store: pl.DataFrame = pools.load_lhb()
    today: date = china_today()
    fresh: bool = not store.is_empty() and store["date"].max() >= today - timedelta(days=5)
    if fresh:
        rows: pl.DataFrame = store.filter((pl.col("code") == code) & (pl.col("date") >= today - timedelta(days=365)))
    else:
        rows = pools.fetch_lhb(today - timedelta(days=LHB_LIVE_DAYS), today, code=code)
    rows = rows.sort("date", descending=True)
    return [{"date": r["date"], "reason": r["reason"], "net_buy": r["net_buy"], "buy": r["buy"], "sell": r["sell"],
             "explain": r["explain"], "pct": r["pct"]} for r in rows.head(30).iter_rows(named=True)]


def _news(code: str) -> list[dict]:
    import akshare as ak

    df = net.call_with_fallback(lambda: ak.stock_news_em(symbol=code), retries=2)
    out: list[dict] = []
    for r in df.to_dict("records"):
        title: str = re.sub(r"</?em>", "", str(r.get("新闻标题") or "")).strip()
        if not title:
            continue
        out.append({"time": str(r.get("发布时间") or "")[:19], "title": title,
                    "url": str(r.get("新闻链接") or ""), "source": str(r.get("文章来源") or "")})
    out.sort(key=lambda x: x["time"], reverse=True)
    return out


def _limit_history(code: str) -> list[dict]:
    from ..predict import limits
    from . import history

    start: date = china_today() - timedelta(days=LIMIT_HISTORY_DAYS * 7 // 5 + 90)
    panel: pl.DataFrame = history.load_panel(start=start, codes=[code])
    if panel.is_empty():
        return []
    lim: pl.DataFrame = limits.add_limit_columns(panel, uni_mod.load_universe()).sort("date")
    lim = lim.tail(LIMIT_HISTORY_DAYS).filter(pl.col("is_limit_up"))
    return [{"date": r["date"], "streak": int(r["streak"] or 0), "one_word": bool(r["one_word"])}
            for r in lim.iter_rows(named=True)]


# ---------------------------------------------------------------- 对外接口

def _cache_file(code: str) -> Path:
    return config.CACHE_DIR.joinpath("profile", f"{code}.json")


def _read_cache(code: str) -> dict | None:
    now: float = time.time()
    with _lock:
        hit = _mem_cache.get(code)
    if hit and now - hit[0] < hit[1].get("_ttl", CACHE_SECONDS):
        return hit[1]
    try:
        obj: dict = json.loads(_cache_file(code).read_text(encoding="utf-8"))
        if now - float(obj.get("_cached_at", 0)) < obj.get("_ttl", CACHE_SECONDS):
            with _lock:
                _mem_cache[code] = (float(obj["_cached_at"]), obj)
            return obj
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_cache(code: str, result: dict) -> None:
    with _lock:
        if len(_mem_cache) > 300:
            _mem_cache.clear()
        _mem_cache[code] = (result["_cached_at"], result)
    try:
        path: Path = _cache_file(code)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning("个股资料缓存写入失败：%s", e)


def profile(code: str, refresh: bool = False) -> dict:
    """个股资料（字段见 ARCHITECTURE 3.14），各部分失败互不影响，原因写在 errors 里；缓存 10 分钟"""
    code = str(code).strip().zfill(6)
    uni_mod.exchange_of(code)       # 非法代码直接抛 ValueError
    if not refresh:
        cached: dict | None = _read_cache(code)
        if cached is not None:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    funcs = {"basic": _basic, "valuation": _valuation, "finance": _finance, "concepts": _concepts,
             "fund_flow": _fund_flow, "lhb": _lhb, "news": _news, "limit_history": _limit_history}
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(funcs)) as pool:
        futures = {name: pool.submit(fn, code) for name, fn in funcs.items()}
        for name, fut in futures.items():
            try:
                values[name] = fut.result(timeout=60)
            except Exception as e:  # noqa: BLE001  单个部分失败不影响整体
                values[name] = None
                errors[name] = f"{PART_LABELS[name]}获取失败：{str(e)[:120] or type(e).__name__}"

    basic: dict = values["basic"] or {}
    val: dict = values["valuation"] or {}
    concept_part: dict = values["concepts"] or {}
    if concept_part.get("error"):
        errors["concepts"] = concept_part["error"]
    result: dict = {
        "code": code,
        "name": basic.get("name") or val.get("name"),
        "exchange": basic.get("exchange"),
        "board": basic.get("board"),
        "board_label": uni_mod.BOARD_LABELS.get(basic.get("board") or "", ""),
        "industry": basic.get("industry"),
        "list_date": basic.get("list_date"),
        "is_st": basic.get("is_st"),
        "business": concept_part.get("business"),
        "concepts": concept_part.get("concepts") or [],
        "valuation": {k: val.get(k) for k in ("pe_ttm", "pb", "total_cap", "float_cap", "price", "pct")},
        "finance": values["finance"],
        "fund_flow": values["fund_flow"] or [],
        "lhb": values["lhb"] or [],
        "news": values["news"] or [],
        "limit_history": values["limit_history"] or [],
        "errors": errors,
        "updated_at": datetime.now(config.CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if values["finance"] is None and "finance" not in errors:
        errors["finance"] = "暂无财报数据"
    result = _clean(result)
    stored: dict = {**result, "_cached_at": time.time(), "_ttl": ERROR_CACHE_SECONDS if errors else CACHE_SECONDS}
    _write_cache(code, stored)
    return result
