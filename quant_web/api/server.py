"""
FastAPI 后端：全部 /api/* JSON 接口 + 前端静态文件。

- 行情/预测等模块一律在函数内延迟导入：某个模块缺失或出错时，只有对应接口返回中文错误
  （{"detail": "..."} + 4xx/5xx），服务器照常运行；
- 返回值统一经 jsonable() 转换（polars/pandas/numpy/日期/NaN → 合法 JSON）；
- 较重的计算（面板统计、历史情绪、涨停天梯、预测结果）按面板文件签名缓存，数据更新后自动失效。
"""
import json
import logging
import math
import os
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import polars as pl
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from .. import __version__, config
from .. import scheduler as scheduler_mod
from .. import settings as settings_mod
from ..jobs import HEAVY, JOBS, run_daily, run_train, run_update
from . import etf
from .common import (  # noqa: F401  公共工具移到 common.py，这里重新导出保持旧名字可用
    CACHE, Cache, ErrorMiddleware, SafeJSONResponse, _file_sig, check_code, china_now, describe_error,
    fallback_phase, jsonable, latest_trading_day, mod, now_text, ok, panel_sig, parse_day, phase, safe,
    temperature_label,
)


log = logging.getLogger("quant_web.api")

KINDS: dict[str, str] = {"streak": "连板晋级", "first": "首板潜力", "swing": "强势股波段"}
PANEL_COLUMNS: list[str] = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "is_st"]
SENTIMENT_DAYS: int = 480           # 历史情绪需要的日历天数（温度按过去250个交易日分位数 + 展示60日）
PREDICT_TTL: float = 300.0
MODEL_MAX_AGE_DAYS: int = 30        # 超过这么多天没重新训练，提示模型过旧


def model_sig(kind: str) -> tuple | None:
    return _file_sig(config.MODEL_DIR.joinpath(f"{kind}_meta.json"))


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        options: str = "、".join(f"{k}（{v}）" for k, v in KINDS.items())
        raise HTTPException(400, f"不认识的模型类型「{kind}」，可选：{options}")
    return kind


def stock_info(code: str) -> dict:
    """股票列表中的名称、板块、行业"""
    try:
        uni: pl.DataFrame = mod("market.universe").load_universe()
        rows: list[dict] = uni.filter(pl.col("code") == code).to_dicts()
    except Exception:  # noqa: BLE001
        rows = []
    return rows[0] if rows else {"code": code}


def read_meta(kind: str) -> dict | None:
    path: Path = config.MODEL_DIR.joinpath(f"{kind}_meta.json")
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def model_summary(kind: str) -> dict | None:
    """模型 meta 摘要：标量字段 + 各折样本外指标的平均值"""
    meta: dict | None = read_meta(kind)
    if meta is None:
        return None
    out: dict = {k: v for k, v in meta.items() if not isinstance(v, list | dict)}
    out["kind"] = kind
    out["label"] = KINDS[kind]
    folds: list = [f for f in meta.get("folds") or [] if isinstance(f, dict)]
    out["n_folds"] = len(folds)
    oos: dict = dict(meta["oos"]) if isinstance(meta.get("oos"), dict) else {}
    if not oos:         # 没有汇总时用各折平均
        for key in dict.fromkeys(k for f in folds for k in f):
            values: list[float] = [
                float(f[key]) for f in folds
                if isinstance(f.get(key), int | float) and not isinstance(f.get(key), bool) and math.isfinite(f[key])
            ]
            if values:
                oos[key] = sum(values) / len(values)
    out["oos"] = oos
    for key in ("auc", "top1_hit", "top5_hit"):
        out.setdefault(key, oos.get(key))
    trade_oos: Any = meta.get("trade_oos")          # swing：样本外逐笔交易指标（去掉逐年等列表）
    if isinstance(trade_oos, dict):
        out["trade_oos"] = {k: v for k, v in trade_oos.items() if not isinstance(v, list)}
    out["age_days"] = None
    trained: Any = meta.get("trained_at")
    if isinstance(trained, str) and len(trained) >= 10:
        try:
            out["age_days"] = (china_now().date() - date.fromisoformat(trained[:10])).days
        except ValueError:
            pass
    out["stale"] = out["age_days"] is None or out["age_days"] > MODEL_MAX_AGE_DAYS
    return out


# ================================================================ 面板统计 / 情绪 / 天梯

def panel_stats() -> dict:
    def build() -> dict:
        files: list[Path] = sorted(config.PANEL_DIR.glob("*.parquet")) if config.PANEL_DIR.exists() else []
        if not files:
            return {"start": None, "end": None, "stocks": 0, "rows": 0, "updated_at": None, "empty": True}
        row: dict = pl.scan_parquet([str(p) for p in files]).select(
            pl.col("date").min().alias("start"),
            pl.col("date").max().alias("end"),
            pl.col("code").n_unique().alias("stocks"),
            pl.len().alias("rows"),
        ).collect().to_dicts()[0]
        mtime: float = max(p.stat().st_mtime for p in files)
        row["updated_at"] = datetime.fromtimestamp(mtime, config.CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
        row["empty"] = not row["rows"]
        return row
    return CACHE.get("panel_stats", build, sig=panel_sig())


def _norm_pct(value: Any) -> float | None:
    """涨幅统一为百分数（10.0 = 10%）。涨停股涨幅至少约5%，小于1的视为小数形式"""
    if not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value) * 100 if abs(value) < 1 else float(value)


def _ladder(stocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """[{code,name,pct,industry,one_word,streak}] → (连板天梯, 行业涨停家数)"""
    groups: dict[int, list[dict]] = {}
    industries: dict[str, int] = {}
    for s in stocks:
        streak: int = int(s.get("streak") or 1)
        groups.setdefault(streak, []).append(s)
        ind: str = s.get("industry") or "其他"
        industries[ind] = industries.get(ind, 0) + 1
    ladder: list[dict] = [
        {"streak": k, "count": len(v), "stocks": sorted(v, key=lambda x: -(x.get("pct") or 0))}
        for k, v in sorted(groups.items(), key=lambda kv: -kv[0])
    ]
    inds: list[dict] = [{"industry": k, "count": v} for k, v in sorted(industries.items(), key=lambda kv: -kv[1])]
    return ladder, inds


def ladder_from_panel(day: pl.DataFrame, uni: pl.DataFrame) -> list[dict]:
    lu: pl.DataFrame = day.filter(pl.col("is_limit_up") & ~pl.col("no_limit"))
    if lu.is_empty():
        return []
    info: pl.DataFrame = uni.select("code", "name", "industry") if not uni.is_empty() else pl.DataFrame(
        schema={"code": pl.Utf8, "name": pl.Utf8, "industry": pl.Utf8}
    )
    lu = lu.join(info, on="code", how="left")
    return [
        {
            "code": r["code"], "name": r.get("name") or r["code"], "pct": _norm_pct(r.get("pct")),
            "industry": r.get("industry"), "one_word": bool(r.get("one_word")),
            "streak": int(r.get("streak") or 1), "close": r.get("close"),
        }
        for r in lu.to_dicts()
    ]


def ladder_from_pool(pool: pl.DataFrame) -> list[dict]:
    out: list[dict] = []
    for r in pool.to_dicts():
        first: str = str(r.get("first_time") or "")
        opened: Any = r.get("open_times")
        one_word: bool = bool(first) and first <= "09:25:59" and not opened
        out.append({
            "code": str(r.get("code")), "name": r.get("name"), "pct": _norm_pct(r.get("pct")),
            "industry": r.get("industry"), "one_word": one_word,
            "streak": int(r.get("streak") or 1), "close": r.get("price"),
            "first_time": r.get("first_time"), "open_times": opened,
        })
    return out


def _history_rows(sent: pl.DataFrame) -> list[dict]:
    rows: list[dict] = sent.sort("date").tail(60).to_dicts() if not sent.is_empty() else []
    for r in rows:
        r["label"] = temperature_label(r.get("temperature"))
    return rows


def sentiment_bundle() -> dict:
    """近60日每日情绪 + 最新交易日涨停天梯。

    优先复用 predict.service 的进程内缓存（全量面板+涨停列+情绪，预测也要用，避免重复占内存）；
    服务模块不可用时，只读最近 SENTIMENT_DAYS 天面板自己算。
    """
    def from_service() -> dict | None:
        try:
            service = mod("predict.service")
        except ImportError:
            return None
        if not hasattr(service, "context"):
            return None
        ctx = service.context()
        last: date | None = ctx.last_date
        out: dict = {"date": last, "history": [], "stocks": [], "errors": {}}
        if last is not None:
            out["stocks"] = ladder_from_panel(ctx.panel_lim.filter(pl.col("date") == last), ctx.universe)
            out["history"] = _history_rows(ctx.sentiment)
        return out

    def build() -> dict:
        via_service: dict | None = from_service()
        if via_service is not None:
            return via_service
        history = mod("market.history")
        universe = mod("market.universe")
        limits = mod("predict.limits")
        out: dict = {"date": None, "history": [], "stocks": [], "errors": {}}
        last: date | None = history.last_date()
        if last is None:
            return out
        panel: pl.DataFrame = history.load_panel(start=last - timedelta(days=SENTIMENT_DAYS), columns=PANEL_COLUMNS)
        uni: pl.DataFrame = universe.load_universe()
        lim: pl.DataFrame = limits.add_limit_columns(panel, uni)
        del panel
        out["date"] = last
        out["stocks"] = ladder_from_panel(lim.filter(pl.col("date") == last), uni)
        try:
            out["history"] = _history_rows(mod("market.sentiment").daily_sentiment(lim))
        except Exception as e:  # noqa: BLE001  情绪模块缺失时天梯照常显示
            out["errors"]["sentiment"] = describe_error(e)[1]
        return out
    return CACHE.get("sentiment_bundle", build, sig=panel_sig())


def build_overview() -> dict:
    warnings: list[str] = []
    now: datetime = china_now()
    today: date = now.date()
    ph: str = phase()
    indexes: list = safe(lambda: mod("market.realtime").index_quotes(), [], warnings, "指数行情")
    bundle: dict | None = safe(sentiment_bundle, None, warnings, "历史情绪")
    history: list[dict] = (bundle or {}).get("history") or []
    last_day: date | None = (bundle or {}).get("date")
    if bundle and bundle.get("errors"):
        warnings.extend(f"历史情绪暂时算不出：{v}" for v in bundle["errors"].values())

    stocks: list[dict] = (bundle or {}).get("stocks") or []
    sentiment: dict | None = None
    source: str = "panel"
    as_of: str | None = f"{last_day.isoformat()} 15:00:00" if last_day else None

    live_needed: bool = ph in ("交易中", "午间休市") or (ph == "已收盘" and (last_day is None or last_day < today))
    if live_needed:
        live: dict | None = safe(lambda: mod("market.sentiment").live_sentiment(), None, warnings, "实时情绪")
        if live:
            sentiment = dict(live)
            as_of = str(live.get("as_of") or now_text())
            source = "live"
        pool: pl.DataFrame | None = safe(lambda: mod("market.pools").fetch_pool("zt", today), None, warnings, "涨停池")
        if pool is not None and not pool.is_empty():
            stocks = ladder_from_pool(pool)
            source = "live"
    if sentiment is None and history:
        sentiment = dict(history[-1])
    if sentiment is not None:
        sentiment["label"] = temperature_label(sentiment.get("temperature"))

    ladder, industries = _ladder(stocks)
    return {
        "as_of": as_of,
        "phase": ph,
        "source": source,
        "indexes": indexes,
        "sentiment": sentiment,
        "ladder": ladder,
        "industries": industries,
        "history": history,
        "warnings": warnings,
    }


# ================================================================ 个股

def limit_days(code: str) -> list[str]:
    """该股在面板中的收盘涨停日期"""
    def build() -> list[str]:
        panel: pl.DataFrame = mod("market.history").load_panel(codes=[code], columns=PANEL_COLUMNS)
        if panel.is_empty():
            return []
        uni: pl.DataFrame = mod("market.universe").load_universe()
        lim: pl.DataFrame = mod("predict.limits").add_limit_columns(panel, uni.filter(pl.col("code") == code))
        return [d.isoformat() for d in lim.filter(pl.col("is_limit_up")).get_column("date").to_list()]
    try:
        return CACHE.get(("limit_days", code), build, sig=panel_sig())
    except Exception:  # noqa: BLE001  涨停标记只是锦上添花
        return []


def panel_bars(code: str, period: str, count: int) -> list[dict]:
    """行情接口不可用时，用本地日线（不复权）拼K线"""
    df: pl.DataFrame = mod("market.history").load_panel(
        codes=[code], columns=["open", "high", "low", "close", "volume", "amount"]
    ).sort("date")
    if df.is_empty():
        return []
    if period in ("week", "month"):
        df = df.group_by_dynamic("date", every="1w" if period == "week" else "1mo").agg(
            pl.col("date").last().alias("last"),
            pl.col("open").first(), pl.col("high").max(), pl.col("low").min(), pl.col("close").last(),
            pl.col("volume").sum(), pl.col("amount").sum(),
        ).drop("date").rename({"last": "date"})
    return df.tail(count).select("date", "open", "close", "high", "low", "volume", "amount").to_dicts()


def with_kind(s: settings_mod.Settings, kind: str) -> settings_mod.Settings:
    try:
        return mod("paper").with_kind(s, kind)
    except ImportError:
        data: dict = s.model_dump()
        data["predict"]["kind"] = kind
        return settings_mod.Settings.model_validate(data)


def kind_settings(kind: str, s: settings_mod.Settings | None = None) -> settings_mod.Settings:
    """某个模型"当前"的设置：连板/首板共用保存的设置（只换 kind）；swing 门槛含义不同，
    保存的设置不是 swing 时用 settings.defaults_for("swing")（见 paper.settings_for）"""
    s = s if s is not None else settings_mod.load()
    try:
        return mod("paper").settings_for(kind, s)
    except ImportError:
        return with_kind(s, kind)


def merge_for_kind(base: settings_mod.Settings, patch: dict, kind: str) -> settings_mod.Settings:
    """merge_update（深合并 + 校验）；设置模块还不认识 kind 时，按 streak 校验其余字段后再换回 kind"""
    try:
        settings_mod.PredictSettings.model_validate({"kind": kind})
    except Exception:  # noqa: BLE001  pydantic ValidationError：kind 还不在设置的可选值里
        return with_kind(settings_mod.merge_update(with_kind(base, "streak"), patch), kind)
    return settings_mod.merge_update(base, patch)


def cached_predict(kind: str, s: settings_mod.Settings) -> dict:
    service = mod("predict.service")
    key: tuple = ("predict", kind, s.model_dump_json())
    return CACHE.get(key, lambda: service.predict_latest(kind, s), ttl=PREDICT_TTL, sig=(panel_sig(), model_sig(kind)))


def find_prediction(code: str) -> dict | None:
    """今日任一模型候选中若有该股，返回其预测行（不受筛选条件影响）；否则 None"""
    if all(read_meta(k) is None for k in KINDS):
        return None
    try:
        service = mod("predict.service")
        s: settings_mod.Settings = settings_mod.load()
        pred: dict | None = CACHE.get(
            ("prediction_for", code, s.predict.model_dump_json()), lambda: service.prediction_for(code, s),
            ttl=PREDICT_TTL, sig=(panel_sig(), *(model_sig(k) for k in KINDS)),
        )
    except Exception:  # noqa: BLE001  预测不可用时个股资料照常返回
        return None
    return align_with_list(code, pred, s) if pred else pred


def align_with_list(code: str, pred: dict, s: settings_mod.Settings) -> dict:
    """让看盘页的排名/综合概率和「连板预测」页的名单一致（名单含消息面加分，prediction_for 不含）。

    新增字段：score_no_news（不含消息面的综合概率）、news_count / policy_count、
    rank_basis（"list" = 排名取自今天的名单）、beyond_rows（在名单里但排在已显示的前 N 行之外时为 N）。
    """
    kind: str = str(pred.get("kind") or "")
    if kind not in KINDS:
        return pred
    try:
        today: dict = cached_predict(kind, kind_settings(kind, s))
    except Exception:  # noqa: BLE001  名单算不出来时保留原结果
        return pred
    rows: list[dict] = today.get("rows") or []
    out: dict = {**pred, "score_no_news": pred.get("score"), "rank_basis": "list"}
    for i, r in enumerate(rows):
        if r.get("code") == code:
            out.update(
                in_list=True, rank=i + 1, pick=bool(r.get("pick")), score=r.get("score"),
                dims=r.get("dims") or pred.get("dims"), news_count=r.get("news_count"), policy_count=r.get("policy_count"),
            )
            return out
    total: int = int(today.get("count") or len(rows))
    if pred.get("in_list") and total > len(rows):
        out.update(rank=None, pick=False, beyond_rows=len(rows))      # 在名单里，但排在已显示的行之后
    else:
        out.update(in_list=False, rank=None, pick=False)
    return out


# ================================================================ 自选股

def load_watchlist() -> list[dict]:
    try:
        raw: Any = json.loads(config.WATCHLIST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items: list[dict] = []
    for x in raw if isinstance(raw, list) else []:
        item: dict = {"code": x} if isinstance(x, str) else dict(x) if isinstance(x, dict) else {}
        if re.fullmatch(r"\d{6}", str(item.get("code", ""))):
            items.append({"code": str(item["code"]), "note": item.get("note") or "", "added_at": item.get("added_at")})
    return items


def save_watchlist(items: list[dict]) -> None:
    path: Path = config.WATCHLIST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def watchlist_view() -> list[dict]:
    items: list[dict] = load_watchlist()
    if not items:
        return []
    codes: list[str] = [i["code"] for i in items]
    quotes: dict[str, dict] = {}
    error: str | None = None
    try:
        rows: list[dict] = CACHE.get(("watch_quotes", tuple(codes)), lambda: mod("market.realtime").quotes(codes), ttl=4)
        quotes = {str(q.get("code")): q for q in rows}
    except Exception as e:  # noqa: BLE001  行情失败时仍返回列表
        error = describe_error(e)[1]
    out: list[dict] = []
    for item in items:
        q: dict | None = quotes.get(item["code"])
        if q is None:
            info: dict = stock_info(item["code"])
            q = {"code": item["code"], "name": info.get("name"), "price": None, "pct": None,
                 "error": error or "没有取到行情"}
        out.append({**q, "note": item["note"], "added_at": item["added_at"]})
    return out


# ================================================================ 数据中心

def data_status() -> dict:
    out: dict = {"panel": panel_stats(), "models": {k: model_summary(k) for k in KINDS}}
    files: dict[str, Path] = {
        "universe": config.UNIVERSE_FILE,
        "fundamentals": config.STOCK_LAB.joinpath("fundamentals.parquet"),
        "lhb": config.STOCK_LAB.joinpath("lhb.parquet"),
    }
    for name, path in files.items():
        if not path.exists():
            out[name] = {"exists": False, "rows": 0, "updated_at": None}
            continue
        try:
            rows: int = pl.scan_parquet(str(path)).select(pl.len()).collect().item()
        except Exception:  # noqa: BLE001
            rows = 0
        out[name] = {
            "exists": True, "rows": rows,
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, config.CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }
    pools: dict[str, dict] = {}
    if config.POOLS_DIR.exists():
        for sub in sorted(p for p in config.POOLS_DIR.iterdir() if p.is_dir()):
            days: list[str] = sorted(p.stem for p in sub.glob("*.parquet"))
            pools[sub.name] = {"days": len(days), "first": days[0] if days else None, "last": days[-1] if days else None}
    out["pools"] = pools
    out["etf"] = safe(etf.data_info, None, [], "ETF数据")
    try:
        out["service"] = mod("predict.service").dataset_status()
    except Exception as e:  # noqa: BLE001
        out["service"] = None
        out["service_error"] = describe_error(e)[1]
    return out


# ================================================================ 静态文件

PLACEHOLDER_HTML: str = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>量化助手</title></head><body style="font-family:sans-serif;padding:40px">
<h2>量化助手后台已启动</h2><p>网页界面文件（quant_web/static/index.html）还没有安装。接口可以访问 <a href="/docs">/docs</a>。</p>
</body></html>"""


class AppStatic(StaticFiles):
    """前端静态文件：vendor 第三方库缓存1天，其余（index.html/app.js/pages）每次校验，程序更新后立即生效"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response: Response = await super().get_response(path, scope)
        _cache_headers(response, path)
        return response


def _cache_headers(response: Response, path: str) -> None:
    if path.replace("\\", "/").startswith("vendor/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    else:
        response.headers["Cache-Control"] = "no-cache"


def _static_file(rel: str) -> Path | None:
    root: Path = config.STATIC_DIR.resolve()
    try:
        path: Path = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if path.is_file() and (path == root or root in path.parents):
        return path
    return None


# ================================================================ 应用

def _warmup() -> None:
    """启动后在后台预先算好面板统计和情绪数据（约10秒），第一次打开首页更快"""
    try:
        if not panel_stats().get("empty"):
            sentiment_bundle()          # 内部会加载 predict.service 的面板缓存
        mod("market.universe").search("zz", limit=1)      # 预先算好名称拼音首字母
    except Exception as e:  # noqa: BLE001
        log.info("预热失败：%s", e)


def _monitor(action: str) -> None:
    try:
        getattr(mod("trading.monitor"), action)()
    except Exception as e:  # noqa: BLE001  监控起不来不影响网页
        log.warning("盘中监控 %s 失败：%s", action, e)


def create_app(background: bool | None = None) -> FastAPI:
    """background=True：启动每日自动更新线程 + 后台预热；False：两者都不启动（测试用）。
    None（默认）：总是预热，自动更新线程可用环境变量 QUANT_WEB_NO_SCHEDULER=1 关闭。"""
    warmup: bool = background is not False
    if background is None:
        background = os.environ.get("QUANT_WEB_NO_SCHEDULER", "").lower() not in ("1", "true", "yes")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config.ensure_dirs()
        if background:
            scheduler_mod.start()
            _monitor("start")           # 盘中监控（交易时段才干活；设置里可关）
        if warmup:
            threading.Thread(target=_warmup, name="quant-web-warmup", daemon=True).start()
        yield
        if background:
            scheduler_mod.stop()
            _monitor("stop")

    app = FastAPI(
        title="量化助手", version=__version__, lifespan=lifespan, default_response_class=SafeJSONResponse,
    )
    app.add_middleware(ErrorMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> SafeJSONResponse:
        parts: list[str] = []
        for err in exc.errors():
            if err.get("type") == "json_invalid":
                parts.append("请求内容不是有效的 JSON 格式")
                continue
            loc: tuple = tuple(p for p in err.get("loc", ()) if p not in ("body", "query", "path"))
            name: str = settings_mod.field_name(loc)
            msg: str = settings_mod.error_text(err)
            parts.append(f"{name}：{msg}" if name else msg)
        return ok({"detail": "请求参数不正确：" + "；".join(dict.fromkeys(parts))}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> SafeJSONResponse:
        detail: Any = exc.detail
        if exc.status_code == 404 and detail == "Not Found":
            detail = "没有这个接口或页面"
        elif exc.status_code == 405:
            detail = "请求方式不对（例如该用 POST 却用了 GET）"
        response: SafeJSONResponse = ok({"detail": detail}, status_code=exc.status_code)
        for k, v in (getattr(exc, "headers", None) or {}).items():
            response.headers[k] = v
        return response

    register_routes(app)
    from .routes import register as register_v3       # 第三版新接口（必须在静态文件兜底路由之前）
    register_v3(app)
    app.mount("/static", AppStatic(directory=config.STATIC_DIR, check_dir=False), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        path: Path | None = _static_file("index.html")
        if path is None:
            return HTMLResponse(PLACEHOLDER_HTML, headers={"Cache-Control": "no-cache"})
        return FileResponse(path, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    @app.get("/{rel:path}", include_in_schema=False)
    def static_fallback(rel: str) -> Response:
        """index.html 里用相对路径引用的 app.js、pages/*.js、vendor/* 也能直接访问"""
        if rel.startswith("api/"):
            raise HTTPException(404, "没有这个接口")
        path = _static_file(rel)
        if path is None:
            if rel == "favicon.ico":
                return Response(status_code=204)
            raise HTTPException(404, f"找不到文件：{rel}")
        response = FileResponse(path)
        _cache_headers(response, rel)
        return response

    return app


def register_routes(app: FastAPI) -> None:
    # ------------------------------------------------------------ 状态与任务

    @app.get("/api/status")
    def status() -> SafeJSONResponse:
        s = settings_mod.load()
        stats: dict = panel_stats()
        return ok({
            "now": now_text(),
            "today": china_now().date(),
            "phase": phase(),
            "panel": stats,
            "first_run": bool(stats.get("empty")),
            "models": {k: model_summary(k) for k in KINDS},
            "jobs": JOBS.running(),
            "settings_risk_ack": s.risk_ack,
            "auto_update": s.auto_update,
            "scheduler": scheduler_mod.SCHEDULER.status(),
            "version": __version__,
        })

    @app.post("/api/jobs/update")
    def job_update() -> SafeJSONResponse:
        job_id: str = JOBS.submit("update", "更新数据", run_update, group=HEAVY)
        return ok({"job_id": job_id})

    @app.post("/api/jobs/train")
    def job_train(body: Annotated[dict | None, Body()] = None) -> SafeJSONResponse:
        kind: str = str((body or {}).get("kind") or "all")
        if kind != "all":
            check_kind(kind)
        label: str = "全部模型" if kind == "all" else KINDS[kind]
        job_id: str = JOBS.submit("train", f"训练模型（{label}）", lambda p: run_train(kind, p), group=HEAVY)
        return ok({"job_id": job_id})

    @app.post("/api/jobs/daily")
    def job_daily() -> SafeJSONResponse:
        job_id: str = JOBS.submit("daily", "一键更新（数据+模型+预测）", run_daily, group=HEAVY)
        return ok({"job_id": job_id})

    @app.post("/api/jobs/etf_update")
    def job_etf_update() -> SafeJSONResponse:
        job_id: str = JOBS.submit("etf_update", "更新ETF行情", etf.update_data)
        return ok({"job_id": job_id})

    @app.get("/api/jobs")
    def jobs_list() -> SafeJSONResponse:
        return ok(JOBS.list())

    @app.get("/api/jobs/{job_id}")
    def job_get(job_id: str) -> SafeJSONResponse:
        job: dict | None = JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "找不到这个任务（程序重启后，之前的任务记录会清空）")
        return ok(job)

    # ------------------------------------------------------------ 市场

    @app.get("/api/market/overview")
    def market_overview() -> SafeJSONResponse:
        return ok(CACHE.get("overview", build_overview, ttl=10))

    @app.get("/api/market/pool")
    def market_pool(kind: str = "zt", day_text: str | None = Query(None, alias="date")) -> SafeJSONResponse:
        pools = mod("market.pools")
        kinds: dict[str, str] = dict(getattr(pools, "KINDS", {}))
        if kind not in kinds:
            options: str = "、".join(f"{k}（{v}）" for k, v in kinds.items())
            raise HTTPException(400, f"不支持的股票池类型「{kind}」，可选：{options}")
        day: date = parse_day(day_text) or latest_trading_day()
        df: pl.DataFrame | None = None
        source: str = "archive"
        warnings: list[str] = []
        if day < china_now().date():
            df = pools.load_archive(kind, start=day, end=day)
        if df is None or df.is_empty():
            # 当天或还没存档的日子：直接抓（东方财富只保留最近约15个交易日）
            live: pl.DataFrame | None = safe(
                lambda: CACHE.get(("pool", kind, day), lambda: pools.fetch_pool(kind, day), ttl=15),
                None, warnings, "股票池",
            )
            if live is not None and (not live.is_empty() or df is None):
                df, source = live, "live"
        rows: list[dict] = df.to_dicts() if df is not None else []
        return ok({
            "kind": kind, "label": kinds[kind], "date": day, "source": source,
            "count": len(rows), "rows": rows, "warnings": warnings,
        })

    # ------------------------------------------------------------ 个股

    @app.get("/api/stock/search")
    def stock_search(q: str = "", limit: int = Query(20, ge=1, le=100)) -> SafeJSONResponse:
        if not q.strip():
            return ok([])
        return ok(mod("market.universe").search(q.strip(), limit=limit))

    @app.get("/api/stock/{code}/quote")
    def stock_quote(code: str) -> SafeJSONResponse:
        code = check_code(code)
        rows: list[dict] = mod("market.realtime").quotes([code])
        if not rows:
            raise HTTPException(404, f"没有取到 {code} 的行情，可能已退市或代码有误")
        return ok(rows[0])

    @app.get("/api/stock/{code}/kline")
    def stock_kline(
        code: str, period: str = "day", adjust: str = "qfq", count: int = Query(600, ge=1, le=5000),
    ) -> SafeJSONResponse:
        code = check_code(code)
        if period not in ("day", "week", "month"):
            raise HTTPException(400, "period 只能是 day（日K）、week（周K）、month（月K）")
        adjust = "" if adjust in ("", "none", "bfq") else adjust
        if adjust not in ("", "qfq"):
            raise HTTPException(400, "adjust 只能是 qfq（前复权）或留空（不复权）")
        warnings: list[str] = []
        source: str = "realtime"
        try:
            bars: list[dict] = mod("market.realtime").kline(code, period=period, adjust=adjust, count=count)
        except Exception as e:
            bars = panel_bars(code, period, count)
            if not bars:
                raise
            source, adjust = "panel", ""
            warnings.append(f"在线K线暂时取不到（{describe_error(e)[1]}），已改用本地日线（不复权）")
        info: dict = stock_info(code)
        return ok({
            "code": code, "name": info.get("name"), "period": period, "adjust": adjust, "source": source,
            "bars": bars, "limit_days": limit_days(code), "warnings": warnings,
        })

    @app.get("/api/stock/{code}/minute")
    def stock_minute(code: str, days: int = Query(1, ge=1, le=5)) -> SafeJSONResponse:
        code = check_code(code)
        realtime = mod("market.realtime")
        return ok(realtime.minute(code) if days == 1 else realtime.minute(code, days=days))

    @app.get("/api/stock/{code}/profile")
    def stock_profile(code: str) -> SafeJSONResponse:
        code = check_code(code)
        data: dict = dict(mod("market.info").profile(code))
        data["prediction"] = find_prediction(code)
        return ok(data)

    # ------------------------------------------------------------ 预测与模型

    @app.get("/api/predict/today")
    def predict_today(kind: str = "streak") -> SafeJSONResponse:
        check_kind(kind)
        return ok(cached_predict(kind, kind_settings(kind)))

    @app.post("/api/predict/backtest")
    def predict_backtest(body: Annotated[dict | None, Body()] = None) -> SafeJSONResponse:
        """body {"predict":{...},"trade":{...}}（可部分）；模型类型取 body.kind / predict.kind / 已保存的类型。
        基础设置 = 该类型"当前"的设置（kind_settings），再合并 body，不保存"""
        body = body or {}
        patch: dict = {k: body[k] for k in ("predict", "trade") if k in body}
        saved: settings_mod.Settings = settings_mod.load()
        pp: Any = patch.get("predict")
        kind: str = str(body.get("kind") or (pp.get("kind") if isinstance(pp, dict) else None) or saved.predict.kind)
        check_kind(kind)
        if isinstance(pp, dict) and "kind" in pp:
            patch["predict"] = {k: v for k, v in pp.items() if k != "kind"}
        s: settings_mod.Settings = merge_for_kind(kind_settings(kind, saved), patch, kind)
        result: dict = dict(mod("predict.service").backtest(kind, s))
        result["settings_used"] = s.model_dump()
        return ok(result)

    @app.get("/api/model/report")
    def model_report(kind: str = "streak") -> SafeJSONResponse:
        check_kind(kind)
        try:
            meta: dict | None = mod("predict.service").models_meta(kind)
        except (ImportError, AttributeError):
            meta = read_meta(kind)
        if meta is None:
            raise HTTPException(404, f"「{KINDS[kind]}」模型还没有训练过，请先在模型中心点击训练")
        meta.setdefault("kind", kind)
        meta["label"] = KINDS[kind]
        return ok(meta)

    # ------------------------------------------------------------ 设置

    @app.get("/api/settings")
    def settings_get() -> SafeJSONResponse:
        # saved：用户是否保存过设置（文件存在）；没保存过时返回的是默认值（前端据此决定要不要提“上次保存的是…”）
        return ok({**settings_mod.load().model_dump(), "saved": settings_mod.exists()})

    @app.put("/api/settings")
    def settings_put(body: Annotated[dict, Body()]) -> SafeJSONResponse:
        s: settings_mod.Settings = settings_mod.merge_update(settings_mod.load(), body)
        settings_mod.save(s)
        return ok({**s.model_dump(), "saved": True})

    @app.get("/api/settings/presets")
    def settings_presets() -> SafeJSONResponse:
        return ok(settings_mod.PRESETS)

    @app.get("/api/settings/defaults")
    def settings_defaults(kind: str | None = None) -> SafeJSONResponse:
        """某个模型类型的默认设置（settings.defaults_for(kind)；设置模块没有它时 = 通用默认值换 kind）"""
        kind = check_kind(kind or settings_mod.load().predict.kind)
        fn: Callable | None = getattr(settings_mod, "defaults_for", None)
        if fn is not None:
            return ok(fn(kind))
        return ok(with_kind(settings_mod.defaults(), kind).model_dump())

    # ------------------------------------------------------------ 模拟盘

    @app.get("/api/paper/summary")
    def paper_summary() -> SafeJSONResponse:
        return ok(mod("paper").summary())

    @app.get("/api/paper/trades")
    def paper_trades(
        kind: str | None = None, status: str | None = None, limit: int = Query(500, ge=1, le=5000),
    ) -> SafeJSONResponse:
        return ok(mod("paper").trades(kind=kind or None, status=status or None, limit=limit))

    @app.post("/api/paper/record")
    def paper_record() -> SafeJSONResponse:
        """手动记录今天三个策略的信号（按当前设置；交易日 15:05 前、数据不是最新时 409 + 中文原因）"""
        paper = mod("paper")
        return ok(paper.record_today(predict_fn=cached_predict, settings_fn=kind_settings))

    # ------------------------------------------------------------ 消息

    @app.get("/api/news/flash")
    def news_flash(limit: int = Query(100, ge=1, le=500)) -> SafeJSONResponse:
        return ok(CACHE.get(("flash", limit), lambda: mod("market.news").flash(limit), ttl=60))

    # ------------------------------------------------------------ 自选股

    @app.get("/api/watchlist")
    def watchlist_get() -> SafeJSONResponse:
        return ok(watchlist_view())

    @app.post("/api/watchlist")
    def watchlist_add(body: Annotated[dict, Body()]) -> SafeJSONResponse:
        code: str = check_code(str(body.get("code", "")))
        items: list[dict] = load_watchlist()
        note: str = str(body.get("note") or "")[:200]
        for item in items:
            if item["code"] == code:
                if "note" in body:
                    item["note"] = note
                break
        else:
            items.append({"code": code, "note": note, "added_at": now_text()})
        save_watchlist(items)
        return ok(watchlist_view())

    def _remove(code: str) -> SafeJSONResponse:
        code = (code or "").strip()
        items: list[dict] = load_watchlist()
        kept: list[dict] = [i for i in items if i["code"] != code]
        if len(kept) == len(items):
            raise HTTPException(404, f"自选股里没有 {code}")
        save_watchlist(kept)
        return ok(watchlist_view())

    @app.delete("/api/watchlist")
    def watchlist_delete(body: Annotated[dict, Body()]) -> SafeJSONResponse:
        return _remove(str(body.get("code", "")))

    @app.delete("/api/watchlist/{code}")
    def watchlist_delete_path(code: str) -> SafeJSONResponse:
        return _remove(code)

    # ------------------------------------------------------------ 稳健ETF

    @app.get("/api/etf/advice")
    def etf_advice(update: bool = True) -> SafeJSONResponse:
        return ok(etf.advice(update=update))

    @app.post("/api/etf/apply")
    def etf_apply() -> SafeJSONResponse:
        return ok(etf.apply())

    @app.get("/api/etf/holdings")
    def etf_holdings() -> SafeJSONResponse:
        return ok(etf.holdings_view())

    @app.put("/api/etf/holdings")
    def etf_holdings_put(body: Annotated[dict, Body()]) -> SafeJSONResponse:
        current: dict = etf.holdings_view(with_prices=False)
        cash: Any = body.get("cash", current["cash"])
        positions: Any = body.get("positions")
        if positions is None:
            positions = {p["code"]: p["volume"] for p in current["positions"]}
        if not isinstance(positions, dict):
            raise HTTPException(400, "positions 格式应为 {\"510300\": 1000}")
        return ok(etf.set_holdings(cash, positions))

    @app.post("/api/etf/init")
    def etf_init(body: Annotated[dict, Body()]) -> SafeJSONResponse:
        if "capital" not in body:
            raise HTTPException(400, "请填写准备投入的资金 capital")
        return ok(etf.init(body["capital"]))

    @app.get("/api/etf/backtest")
    def etf_backtest(start: str = "2014-06-01", capital: float = 200_000) -> SafeJSONResponse:
        return ok(etf.backtest(start=start, capital=capital))

    @app.get("/api/etf/data")
    def etf_data() -> SafeJSONResponse:
        return ok(etf.data_info())

    # ------------------------------------------------------------ 数据中心

    @app.get("/api/data/status")
    def data_status_route() -> SafeJSONResponse:
        return ok(data_status())


app: FastAPI = create_app()
