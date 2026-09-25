"""
稳健ETF（etf_quant）的网页封装：本周建议、按建议更新持仓、持仓读写、初始化、历史回测。

逻辑全部复用 etf_quant（与命令行菜单完全一致），这里只做：
- 路径在调用时从 etf_quant.config 读取并显式传入（便于测试指向临时目录）；
- 参考价优先用腾讯实时报价（一次请求取全部ETF，约0.2秒），取不到的再走 etf_quant 原来的逐只下载；
- 回测结果按 (起点, 资金, 数据日期) 缓存。
"""
import json
import threading
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from .. import config, net


QT_URL: str = "https://qt.gtimg.cn/q="
BENCHMARK: str = "沪深300ETF（一直持有）"
CASH_FUND: str = "货币基金（银华日利）"

_lock = threading.RLock()               # 持仓/建议文件的读写串行化
_bt_lock = threading.Lock()             # 回测会写 lab 的合约配置，串行执行
_bt_cache: dict[tuple, dict] = {}
_advice_cache: dict[str, Any] = {}
_quote_cache: dict[str, Any] = {}
ADVICE_TTL: float = 600.0
QUOTE_TTL: float = 20.0


def _cfg() -> Any:
    from etf_quant import config as cfg

    return cfg


def today() -> date:
    return datetime.now(config.CHINA_TZ).date()


# ---------------------------------------------------------------- 行情数据状态

def data_info() -> dict:
    """ETF 行情数据状态：{"has_data","updated_at","last_complete_day","fresh"}"""
    from etf_quant import data

    cfg = _cfg()
    daily: Path = Path(cfg.LAB_PATH).joinpath("daily")
    has_data: bool = daily.exists() and any(daily.glob("*.parquet"))
    meta: dict = {}
    meta_path: Path = Path(cfg.LAB_PATH).joinpath(data.META_FILE)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    return {
        "has_data": has_data,
        "updated_at": meta.get("updated_at"),
        "last_complete_day": meta.get("last_complete_day"),
        "fresh": meta.get("last_complete_day") == str(data.last_complete_day()),
    }


def update_data(progress: Any = None) -> dict:
    """下载全部ETF的最新行情（约1分钟）"""
    from etf_quant.config import ETFS
    from etf_quant.data import update_all

    count: list[int] = [0]

    def log(line: str) -> None:
        count[0] += 1
        if progress:
            progress(min(count[0] / len(ETFS), 1.0), line.strip())

    with _lock:
        ok, failed = update_all(log=log)
    _advice_cache.clear()
    if failed and not ok:
        raise ConnectionError("ETF行情全部下载失败，请检查网络后重试")
    return {"ok": ok, "failed": failed, **data_info()}


# ---------------------------------------------------------------- 实时价格

def fetch_quotes(etfs: list) -> dict[str, tuple[date, float]]:
    """腾讯实时报价 {vt_symbol: (日期, 价格)}；停牌/未开盘用昨收。失败的不在结果中"""
    if not etfs:
        return {}
    by_symbol: dict[str, Any] = {e.market_symbol: e for e in etfs}
    text: str = net.get(QT_URL + ",".join(by_symbol), encoding="gbk", timeout=6, retries=2).text
    out: dict[str, tuple[date, float]] = {}
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        key, _, body = line.partition("=")
        etf = by_symbol.get(key.removeprefix("v_"))
        fields: list[str] = body.strip('"').split("~")
        if etf is None or len(fields) < 31:
            continue
        try:
            price: float = float(fields[3]) or float(fields[4])
            day: date = datetime.strptime(fields[30][:8], "%Y%m%d").date()
        except ValueError:
            continue
        if price > 0:
            out[etf.vt_symbol] = (day, price)
    return out


def latest_prices(etfs: list) -> dict[str, tuple[date, float]]:
    """先腾讯实时报价，缺的再用 etf_quant 原来的逐只下载"""
    from etf_quant import data

    key: str = ",".join(sorted(e.vt_symbol for e in etfs))
    hit = _quote_cache.get(key)
    if hit and time.monotonic() - hit[0] < QUOTE_TTL:
        return dict(hit[1])
    try:
        prices: dict[str, tuple[date, float]] = fetch_quotes(etfs)
    except ConnectionError:
        prices = {}
    missing: list = [e for e in etfs if e.vt_symbol not in prices]
    if missing:
        prices.update(data.fetch_latest_prices(missing))
    _quote_cache[key] = (time.monotonic(), dict(prices))
    return prices


# ---------------------------------------------------------------- 持仓

def _saved_prices() -> dict[str, float]:
    from etf_quant import advisor

    saved: dict | None = advisor.load_saved_advice()
    return dict((saved or {}).get("prices") or {})


def holdings_view(with_prices: bool = True) -> dict:
    """持仓 + 市值：{"exists","cash","positions":[...],"market_value","total_value","nav":[...],...}"""
    from etf_quant.advisor import class_of
    from etf_quant.config import CODE_MAP, Universe
    from etf_quant.portfolio import holdings_exist, load_holdings, load_nav

    cfg = _cfg()
    exists: bool = holdings_exist(cfg.HOLDINGS_FILE)
    holdings = load_holdings(cfg.HOLDINGS_FILE)
    prices: dict[str, float] = {}
    price_time: str | None = None
    source: str | None = None
    etfs: list = [CODE_MAP[c] for c in holdings.positions if c in CODE_MAP]
    if with_prices and etfs:
        try:
            quotes = latest_prices(etfs)
            prices = {vt.split(".")[0]: p for vt, (_, p) in quotes.items()}
            if quotes:
                price_time = max(d for d, _ in quotes.values()).isoformat()
                source = "realtime"
        except Exception:  # noqa: BLE001  取不到价格时用最近一次建议的参考价
            prices = {}
    if etfs and len(prices) < len(etfs):
        for code, p in _saved_prices().items():
            prices.setdefault(code, p)
        source = source or "advice"

    universe = Universe()
    total: float = holdings.total_value(prices)
    rows: list[dict] = []
    for code, volume in sorted(holdings.positions.items()):
        etf = CODE_MAP.get(code)
        price: float | None = prices.get(code)
        value: float | None = volume * price if price else None
        rows.append({
            "code": code,
            "name": etf.name if etf else "（非策略品种）",
            "asset_class": class_of(etf.vt_symbol, universe) if etf else "",
            "volume": volume,
            "price": price,
            "value": value,
            "weight": value / total if value and total > 0 else None,
            "in_strategy": etf is not None,
        })

    nav: list[dict] = []
    for row in load_nav(cfg.NAV_FILE):
        try:
            nav.append({
                "date": row.get("日期"), "total": float(row.get("总资产") or 0),
                "cash": float(row.get("现金") or 0), "note": row.get("说明", ""),
            })
        except ValueError:
            continue
    start_value: float | None = nav[0]["total"] if nav else None
    return {
        "exists": exists,
        "needs_init": not exists,
        "cash": holdings.cash,
        "positions": rows,
        "market_value": holdings.market_value(prices),
        "total_value": total,
        "price_date": price_time,
        "price_source": source,
        "nav": nav,
        "start_value": start_value,
        "total_return": (total / start_value - 1) if start_value and exists else None,
        "file": str(cfg.HOLDINGS_FILE),
    }


def set_holdings(cash: float, positions: dict[str, Any]) -> dict:
    """手动修改持仓（例如券商实际成交和建议不一致时）"""
    from etf_quant.portfolio import Holdings, save_holdings

    try:
        cash = float(cash)
    except (TypeError, ValueError):
        raise ValueError("现金要填数字") from None
    if not 0 <= cash <= 1e10:
        raise ValueError("现金要在 0 到 100 亿元之间")
    clean: dict[str, int] = {}
    for code, volume in (positions or {}).items():
        code = str(code).strip().zfill(6)
        if not (len(code) == 6 and code.isdigit()):
            raise ValueError(f"「{code}」不是有效的基金代码（6位数字）")
        try:
            v = float(volume)
        except (TypeError, ValueError):
            raise ValueError(f"{code} 的份额要填数字") from None
        if v < 0 or v != int(v):
            raise ValueError(f"{code} 的份额要填不小于0的整数")
        if v:
            clean[code] = clean.get(code, 0) + int(v)

    cfg = _cfg()
    with _lock:
        Path(cfg.HOLDINGS_FILE).parent.mkdir(parents=True, exist_ok=True)
        save_holdings(Holdings(cash=cash, positions=clean), cfg.HOLDINGS_FILE)
    _advice_cache.clear()
    return holdings_view()


def init(capital: float) -> dict:
    """用一笔现金初始化持仓（与命令行 init 相同：清空持仓，记一笔"初始资金"）"""
    from etf_quant.portfolio import Holdings, append_nav, holdings_exist, save_holdings

    try:
        capital = float(capital)
    except (TypeError, ValueError):
        raise ValueError("资金金额要填数字，例如 100000") from None
    if not capital > 0:
        raise ValueError("资金金额必须大于0")
    if capital > 1e10:
        raise ValueError("资金金额太大了，请检查是否多输了几个0")

    cfg = _cfg()
    with _lock:
        replaced: bool = holdings_exist(cfg.HOLDINGS_FILE)
        Path(cfg.HOLDINGS_FILE).parent.mkdir(parents=True, exist_ok=True)
        save_holdings(Holdings(cash=capital), cfg.HOLDINGS_FILE)
        append_nav(capital, capital, "初始资金", today(), path=cfg.NAV_FILE)
    _advice_cache.clear()
    view: dict = holdings_view(with_prices=False)
    view["replaced"] = replaced
    view["message"] = f"已设置：现金 {capital:,.2f} 元，暂无持仓。" + ("（原来的持仓记录已被替换）" if replaced else "")
    return view


# ---------------------------------------------------------------- 建议

def _decisions(allocation: Any) -> list[dict]:
    from etf_quant.config import ETF_MAP

    rows: list[dict] = []
    for d in allocation.decisions:
        etf = ETF_MAP.get(d.chosen) if d.chosen else None
        rows.append({
            "asset_class": d.asset_class,
            "code": etf.code if etf else None,
            "name": etf.name if etf else "不持有",
            "weight": d.weight if d.chosen else 0.0,
            "reason": d.reason,
            "candidates": [
                {
                    "code": c["vt_symbol"].split(".")[0], "name": c["name"],
                    "momentum": c["momentum"], "trend": c["trend"],
                }
                for c in d.candidates
            ],
        })
    return rows


def _cash_weight(allocation: Any) -> float:
    from etf_quant.config import Universe

    return float(allocation.weights.get(Universe().cash, 0.0))


def preview() -> dict:
    """还没初始化持仓时，也先给出本周的目标配置比例"""
    from etf_quant.allocator import compute_allocations, weekly_rebalance_dates
    from etf_quant.config import StrategyParams, Universe
    from etf_quant.data import load_prices

    universe = Universe()
    close = load_prices(universe.vt_symbols)
    if close.empty:
        return {"allocation": [], "cash_weight": None, "signal_date": None}
    dates = weekly_rebalance_dates(close.index, today=today())
    allocation = compute_allocations(close, universe, StrategyParams(), dates)[-1]
    return {
        "allocation": _decisions(allocation),
        "cash_weight": _cash_weight(allocation),
        "signal_date": allocation.date.date().isoformat(),
    }


def _advice_sig() -> tuple:
    cfg = _cfg()
    parts: list = []
    for path in (Path(cfg.HOLDINGS_FILE), Path(cfg.LAB_PATH).joinpath("meta.json")):
        try:
            st = path.stat()
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    return (*parts, today())


def advice(update: bool = True) -> dict:
    """本周操作建议；还没设置资金时返回 {"needs_init": true, ...}"""
    from etf_quant import advisor
    from etf_quant.config import StrategyParams
    from etf_quant.portfolio import holdings_exist, load_holdings

    cfg = _cfg()
    if not holdings_exist(cfg.HOLDINGS_FILE):
        out: dict = {
            "needs_init": True,
            "message": "还没有设置投入资金。请先填写准备投入的金额（建议先用模拟金额跑一两个月熟悉流程）。",
            "orders": [], "positions": [], "warnings": [], "data": data_info(),
        }
        try:
            out.update(preview())
        except Exception as e:  # noqa: BLE001  预览失败不影响引导
            out["warnings"].append(f"暂时算不出目标配置：{e}")
        return out

    with _lock:
        updated: bool = False
        failed: list[str] = []
        info: dict = data_info()
        if update and not info["fresh"]:
            try:
                upd: dict = update_data()
                updated, failed = bool(upd["ok"]), list(upd["failed"])
            except ConnectionError as e:
                failed = [str(e)]

        sig: tuple = _advice_sig()
        hit = _advice_cache.get("advice")
        if hit and hit[0] == sig and time.monotonic() - hit[1] < ADVICE_TTL:
            result: dict = dict(hit[2])
        else:
            holdings = load_holdings(cfg.HOLDINGS_FILE)
            with mock.patch.object(advisor, "fetch_latest_prices", latest_prices):
                adv = advisor.make_advice(holdings=holdings, params=StrategyParams())
            result = _advice_dict(adv)
            _advice_cache["advice"] = (sig, time.monotonic(), result)

    result["updated"] = updated
    result["update_failed"] = failed
    if failed:
        result["warnings"] = [*result["warnings"], "部分行情下载失败，已使用本地旧数据：" + "；".join(failed)[:200]]
    result["data"] = data_info()
    saved: dict | None = advisor.load_saved_advice()
    result["applied"] = bool(saved and saved.get("applied"))
    return result


def _advice_dict(adv: Any) -> dict:
    return {
        "needs_init": False,
        "signal_date": adv.signal_date.isoformat(),
        "price_date": adv.price_date.isoformat(),
        "total_value": adv.total_value,
        "cash": adv.cash,
        "cash_after": adv.cash_after,
        "no_action": not adv.orders,
        "orders": [asdict(o) for o in adv.orders],
        "positions": [
            {**asdict(r), "current_value": r.current_value, "target_value": r.target_value} for r in adv.rows
        ],
        "allocation": _decisions(adv.allocation),
        "cash_weight": _cash_weight(adv.allocation),
        "portfolio_vol": adv.allocation.portfolio_vol,
        "vol_scale": adv.allocation.vol_scale,
        "warnings": list(adv.warnings),
        "ignored": dict(adv.ignored),
        "prices": dict(adv.prices),
    }


def apply() -> dict:
    """按最近一次建议（参考价）更新持仓，逻辑同 advisor.apply_saved_advice"""
    from etf_quant import advisor
    from etf_quant.advisor import Order
    from etf_quant.portfolio import append_nav, holdings_exist, load_holdings, save_holdings

    cfg = _cfg()
    with _lock:
        if not holdings_exist(cfg.HOLDINGS_FILE):
            raise RuntimeError("还没有设置资金，请先初始化持仓")
        data: dict | None = advisor.load_saved_advice()
        if not data:
            raise RuntimeError("还没有生成过操作建议，请先查看本周建议")
        if data.get("applied"):
            raise RuntimeError("最近一次建议已经更新过持仓了，不能重复更新")
        if not data.get("orders"):
            view: dict = holdings_view(with_prices=False)
            view.update({"ok": True, "orders": [], "message": "最近一次建议不需要下单，持仓无需更新。"})
            return view

        orders: list = [Order(**o) for o in data["orders"]]
        holdings = load_holdings(cfg.HOLDINGS_FILE)
        for o in orders:
            sign: int = 1 if o.side == "买入" else -1
            holdings.positions[o.code] = holdings.positions.get(o.code, 0) + sign * o.volume
            holdings.cash += -sign * o.amount - o.commission
        holdings.positions = {k: v for k, v in holdings.positions.items() if v}
        save_holdings(holdings, cfg.HOLDINGS_FILE)

        prices: dict[str, float] = data.get("prices", {})
        total: float = holdings.cash + sum(v * prices.get(code, 0.0) for code, v in holdings.positions.items())
        if all(code in prices for code in holdings.positions):
            append_nav(total, holdings.cash, "按建议调仓", date.fromisoformat(data["price_date"]), path=cfg.NAV_FILE)
        data["applied"] = True
        advice_file: Path = Path(advisor.ADVICE_FILE)
        advice_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _advice_cache.clear()

    view = holdings_view(with_prices=False)
    view.update({
        "ok": True,
        "orders": [asdict(o) for o in orders],
        "message": f"持仓已更新。当前现金 {holdings.cash:,.2f} 元，按参考价估算总资产 {total:,.0f} 元。",
    })
    return view


# ---------------------------------------------------------------- 回测

def _data_stamp() -> str:
    info: dict = data_info()
    cfg = _cfg()
    daily: Path = Path(cfg.LAB_PATH).joinpath("daily")
    mtime: float = max((p.stat().st_mtime for p in daily.glob("*.parquet")), default=0.0) if daily.exists() else 0.0
    return f"{info.get('updated_at')}|{mtime:.0f}"


def backtest(start: str = "2014-06-01", capital: float = 200_000) -> dict:
    """历史回测（vnpy.alpha 回测引擎，约5~10秒；同样参数和数据会直接用缓存）"""
    try:
        start_d: date = datetime.strptime(start, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError("开始日期格式应为 YYYY-MM-DD，例如 2014-06-01") from None
    if start_d < date(2010, 1, 1) or start_d >= today():
        raise ValueError("开始日期要在 2010-01-01 到今天之间")
    try:
        capital = float(capital)
    except (TypeError, ValueError):
        raise ValueError("资金要填数字") from None
    if not 10_000 <= capital <= 1e10:
        raise ValueError("回测资金要在 1 万元以上")
    if not data_info()["has_data"]:
        raise RuntimeError("还没有ETF行情数据，请先点击【更新ETF行情】")

    key: tuple = (start_d.isoformat(), round(capital, 2), _data_stamp())
    with _bt_lock:
        if key in _bt_cache:
            return {**_bt_cache[key], "cached": True}
        result: dict = _run_backtest(start_d.isoformat(), capital)
        _bt_cache.clear()           # 只保留最近一次，避免占内存
        _bt_cache[key] = result
    return {**result, "cached": False}


def _run_backtest(start: str, capital: float) -> dict:
    import math

    import pandas as pd

    from etf_quant.backtest import run_backtest

    r = run_backtest(start=start, capital=capital)
    balance: pd.Series = r.balance
    bench: pd.Series | None = r.benchmarks.get(BENCHMARK)
    cash_fund: pd.Series | None = r.benchmarks.get(CASH_FUND)

    def at(s: pd.Series | None, ts: pd.Timestamp, default: float | None, digits: int = 2) -> float | None:
        if s is None or ts not in s.index:
            return default
        v = float(s.loc[ts])
        return round(v, digits) if math.isfinite(v) else default

    equity: list[dict] = []
    for i, (ts, value) in enumerate(balance.items()):
        first: bool = i == 0
        equity.append({
            "date": ts.date().isoformat(),
            "strategy": round(float(value), 2),
            "benchmark": at(bench, ts, capital if first else None),
            "cash_fund": at(cash_fund, ts, capital if first else None),
        })
    dd: pd.Series = balance / balance.cummax() - 1
    bdd: pd.Series | None = (bench / bench.cummax() - 1) if bench is not None else None
    drawdown: list[dict] = [
        {"date": ts.date().isoformat(), "strategy": round(float(v), 5), "benchmark": at(bdd, ts, None, 5)}
        for ts, v in dd.items()
    ]

    col_map: dict[str, str] = {"策略": "strategy", BENCHMARK: "benchmark", CASH_FUND: "cash_fund"}
    yearly: list[dict] = []
    for year, row in r.yearly.iterrows():
        item: dict = {"year": int(year)}
        for col, key in col_map.items():
            v = row.get(col) if col in r.yearly.columns else None
            item[key] = float(v) if v is not None and math.isfinite(float(v)) else None
        yearly.append(item)

    classes: list[str] = list(r.class_weights.columns)
    class_weights: list[dict] = [
        {"date": pd.Timestamp(ts).date().isoformat(), **{c: round(float(row[c]), 4) for c in classes}}
        for ts, row in r.class_weights.iterrows()
    ]
    return {
        "start": balance.index[1].date().isoformat() if len(balance) > 1 else start,
        "end": balance.index[-1].date().isoformat(),
        "capital": capital,
        "final_value": float(balance.iloc[-1]),
        "metrics": {k: float(v) for k, v in r.metrics.items()},
        "benchmark_metrics": {k: {m: float(x) for m, x in v.items()} for k, v in r.benchmark_metrics.items()},
        "benchmark_name": BENCHMARK,
        "cash_fund_name": CASH_FUND,
        "equity": equity,
        "drawdown": drawdown,
        "yearly": yearly,
        "classes": classes,
        "class_weights": class_weights,
        "trade_count": r.trade_count,
        "rebalance_count": r.rebalance_count,
        "total_commission": r.total_commission,
        "data_date": data_info().get("last_complete_day"),
        # 诚实性：etf_quant 的参数是参考 2014-2026 年这段历史检验、选定的（见 etf_quant/config.py），
        # 这里回测的是同一段历史 → 样本内结果，偏乐观
        "in_sample": True,
        "in_sample_note": IN_SAMPLE_NOTE,
    }


IN_SAMPLE_NOTE: str = ("这是“样本内”回测：策略的参数本来就是参考 2014~2026 年这段历史检验、选定的，再拿同一段历史来检验，"
                       "成绩自然好看（相当于看过答案再考试），以后实际的效果通常会更差。它只能说明规则在过去说得通，不代表以后能赚这么多。")
