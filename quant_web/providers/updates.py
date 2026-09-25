"""
扩展数据存档的定时更新：调用 registry().fetch(...) 取数，store.save(...) 落盘。

约定：这里每个 update_* 函数都不对调用方抛异常——单个能力/单个日期/单个代码失败只记录原因，
返回 {"ok", "rows", "source", "error"}（外层 update_all 汇总每个存档一份）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta

import polars as pl

from .. import config
from . import store
from .base import ProviderError, registry


log = logging.getLogger("quant_web.providers.updates")

Progress = Callable[[float, str], None]

# sh000001 上证指数, sz399001 深证成指, sz399006 创业板指, sh000300 沪深300, sh000905 中证500, sh000852 中证1000
DEFAULT_INDICES: tuple[str, ...] = ("sh000001", "sz399001", "sz399006", "sh000300", "sh000905", "sh000852")


def _scaled(progress: Progress | None, lo: float, hi: float) -> Progress:
    def report(fraction: float, message: str = "") -> None:
        if progress is not None:
            try:
                f: float = min(max(float(fraction), 0.0), 1.0)
            except (TypeError, ValueError):
                f = 0.0
            progress(lo + (hi - lo) * f, message)
    return report


def _today() -> date:
    """北京时间的今天（这台电脑的时区是纽约，date.today() 会差一天）"""
    from datetime import datetime
    return datetime.now(config.CHINA_TZ).date()


def _summary(ok: bool, rows: int, source: str | None, error: str | None) -> dict:
    return {"ok": ok, "rows": rows, "source": source, "error": error}


def _loop_ok(attempted: int, got_data: bool, errors: list[str]) -> bool:
    """循环取多天/多个指数时的 ok：没有要取的、或至少成功一个就算 ok；全部尝试都出错才算失败"""
    return attempted == 0 or got_data or not errors


def _guard(name: str, fn: Callable[[], dict]) -> dict:
    """最后一道安全网：万一函数体里漏了什么异常，也不让 update_all 崩掉"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.exception("更新「%s」意外失败", name)
        return _summary(False, 0, None, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 逐个能力

def update_margin(days: int = 5, progress: Progress | None = None) -> dict:
    """最近 days 个交易日的融资融券明细"""
    def run() -> dict:
        from ..market import realtime

        report: Progress = progress or (lambda f, m="": None)
        targets: list[date] = realtime.recent_trading_days(days, until=_today())
        frames: list[pl.DataFrame] = []
        source: str | None = None
        errors: list[str] = []
        for i, day in enumerate(targets):
            report(i / max(len(targets), 1), f"正在取 {day} 的融资融券明细…")
            try:
                res = registry().fetch("margin", day=day)
            except ProviderError as e:
                errors.append(f"{day}: {e}")
                continue
            if not res.data.is_empty():
                frames.append(res.data)
                source = res.source
        rows: int = store.save("margin", pl.concat(frames, how="vertical_relaxed")) if frames else store.load("margin").height
        report(1.0, f"融资融券明细更新完成，共 {rows} 行")
        return _summary(_loop_ok(len(targets), bool(frames), errors), rows, source, "；".join(errors) or None)
    return _guard("margin", run)


def update_holder_count(progress: Progress | None = None) -> dict:
    def run() -> dict:
        report: Progress = progress or (lambda f, m="": None)
        report(0.0, "正在取最新股东户数…")
        try:
            res = registry().fetch("holder_count")
        except ProviderError as e:
            return _summary(False, store.load("holder_count").height, None, str(e))
        rows: int = store.save("holder_count", res.data) if not res.data.is_empty() else store.load("holder_count").height
        report(1.0, f"股东户数更新完成，共 {rows} 行")
        return _summary(True, rows, res.source, None)
    return _guard("holder_count", run)


def update_unlock(ahead_days: int = 60, progress: Progress | None = None) -> dict:
    """未来 ahead_days 天内的限售解禁"""
    def run() -> dict:
        report: Progress = progress or (lambda f, m="": None)
        start, end = _today(), _today() + timedelta(days=ahead_days)
        report(0.0, f"正在取 {start} 至 {end} 的限售解禁…")
        try:
            res = registry().fetch("unlock", start=start, end=end)
        except ProviderError as e:
            return _summary(False, store.load("unlock").height, None, str(e))
        rows: int = store.save("unlock", res.data) if not res.data.is_empty() else store.load("unlock").height
        report(1.0, f"限售解禁更新完成，共 {rows} 行")
        return _summary(True, rows, res.source, None)
    return _guard("unlock", run)


def update_pledge(progress: Progress | None = None) -> dict:
    def run() -> dict:
        report: Progress = progress or (lambda f, m="": None)
        report(0.0, "正在取最新股权质押比例…")
        try:
            res = registry().fetch("pledge")
        except ProviderError as e:
            return _summary(False, store.load("pledge").height, None, str(e))
        rows: int = store.save("pledge", res.data) if not res.data.is_empty() else store.load("pledge").height
        report(1.0, f"股权质押更新完成，共 {rows} 行")
        return _summary(True, rows, res.source, None)
    return _guard("pledge", run)


def update_forecast(progress: Progress | None = None) -> dict:
    def run() -> dict:
        report: Progress = progress or (lambda f, m="": None)
        report(0.0, "正在取最新业绩预告…")
        try:
            res = registry().fetch("forecast")
        except ProviderError as e:
            return _summary(False, store.load("forecast").height, None, str(e))
        rows: int = store.save("forecast", res.data) if not res.data.is_empty() else store.load("forecast").height
        report(1.0, f"业绩预告更新完成，共 {rows} 行")
        return _summary(True, rows, res.source, None)
    return _guard("forecast", run)


def update_holder_trades(days: int = 90, progress: Progress | None = None) -> dict:
    """最近 days 天公告的股东增减持"""
    def run() -> dict:
        report: Progress = progress or (lambda f, m="": None)
        start, end = _today() - timedelta(days=days), _today()
        report(0.0, f"正在取 {start} 至 {end} 的股东增减持…")
        try:
            res = registry().fetch("holder_trades", start=start, end=end)
        except ProviderError as e:
            return _summary(False, store.load("holder_trades").height, None, str(e))
        rows: int = store.save("holder_trades", res.data) if not res.data.is_empty() else store.load("holder_trades").height
        report(1.0, f"股东增减持更新完成，共 {rows} 行")
        return _summary(True, rows, res.source, None)
    return _guard("holder_trades", run)


def update_index_bars(indices: tuple[str, ...] = DEFAULT_INDICES, days: int = 3650,
                      progress: Progress | None = None) -> dict:
    """指数日线：每个指数只补最后存档日期之后的部分；从没存过的指数按 days 天全量下载"""
    def run() -> dict:
        report: Progress = progress or (lambda f, m="": None)
        old: pl.DataFrame = store.load("index_bars")
        have: dict[str, date] = {}
        if not old.is_empty():
            have = dict(old.group_by("index").agg(pl.col("date").max()).rows())
        today: date = _today()
        frames: list[pl.DataFrame] = []
        source: str | None = None
        errors: list[str] = []
        for i, idx in enumerate(indices):
            report(i / max(len(indices), 1), f"正在取指数 {idx} 的日线…")
            last: date | None = have.get(idx)
            start: date = (last + timedelta(days=1)) if last else today - timedelta(days=days)
            if start > today:
                continue
            try:
                res = registry().fetch("index_bars", index=idx, start=start, end=today)
            except ProviderError as e:
                errors.append(f"{idx}: {e}")
                continue
            if not res.data.is_empty():
                frames.append(res.data)
                source = res.source
        rows: int = store.save("index_bars", pl.concat(frames, how="vertical_relaxed")) if frames \
            else store.load("index_bars").height
        report(1.0, f"指数日线更新完成，共 {rows} 行")
        return _summary(_loop_ok(len(indices), bool(frames), errors), rows, source, "；".join(errors) or None)
    return _guard("index_bars", run)


def update_index_members(indices: tuple[str, ...] = ("hs300", "zz500", "zz1000"),
                         progress: Progress | None = None) -> dict:
    """指数成分股：每个指数整体替换（不是合并/去重）"""
    def run() -> dict:
        report: Progress = progress or (lambda f, m="": None)
        source: str | None = None
        errors: list[str] = []
        rows: int = store.load("index_members").height
        for i, idx in enumerate(indices):
            report(i / max(len(indices), 1), f"正在取指数「{idx}」的成分股…")
            try:
                res = registry().fetch("index_members", index=idx)
            except ProviderError as e:
                errors.append(f"{idx}: {e}")
                continue
            if not res.data.is_empty():
                rows = store.save("index_members", res.data, replace_where=pl.col("index") == idx)
                source = res.source
        report(1.0, f"指数成分股更新完成，共 {rows} 行")
        got: bool = source is not None
        return _summary(_loop_ok(len(indices), got, errors), rows, source, "；".join(errors) or None)
    return _guard("index_members", run)


def update_fund_flow(codes: list[str], limit: int = 300, progress: Progress | None = None) -> dict:
    """给定代码列表的个股资金流向（新浪只有近约20天，按天存档累积历史）"""
    def run() -> dict:
        report: Progress = progress or (lambda f, m="": None)
        todo: list[str] = list(dict.fromkeys(str(c).zfill(6) for c in codes))[:max(limit, 0)]
        frames: list[pl.DataFrame] = []
        source: str | None = None
        errors: list[str] = []
        got_any: bool = False
        rows: int = store.load("fund_flow").height
        for i, code in enumerate(todo):
            if i % 10 == 0:
                report(i / max(len(todo), 1), f"正在取资金流向 {i}/{len(todo)}…")
            try:
                res = registry().fetch("fund_flow", code=code)
            except ProviderError as e:
                errors.append(f"{code}: {e}")
                continue
            if not res.data.is_empty():
                frames.append(res.data)
                source = res.source
                got_any = True
            if len(frames) >= 50:
                rows = store.save("fund_flow", pl.concat(frames, how="vertical_relaxed"))
                frames = []
        if frames:
            rows = store.save("fund_flow", pl.concat(frames, how="vertical_relaxed"))
        report(1.0, f"资金流向更新完成，共 {rows} 行" + (f"，{len(errors)} 只失败" if errors else ""))
        error: str | None = ("；".join(errors[:5]) + ("…" if len(errors) > 5 else "")) if errors else None
        return _summary(_loop_ok(len(todo), got_any, errors), rows, source, error)
    return _guard("fund_flow", run)


# ---------------------------------------------------------------- 汇总

def update_all(progress: Progress | None = None, flow_codes: list[str] | None = None) -> dict:
    """依次跑一遍全部扩展数据更新，返回 {存档名: {"ok","rows","source","error"}}"""
    report: Progress = progress or (lambda f, m="": None)
    config.ensure_dirs()
    steps: list[tuple[str, Callable[[Progress], dict]]] = [
        ("margin", lambda p: update_margin(progress=p)),
        ("holder_count", lambda p: update_holder_count(progress=p)),
        ("unlock", lambda p: update_unlock(progress=p)),
        ("pledge", lambda p: update_pledge(progress=p)),
        ("forecast", lambda p: update_forecast(progress=p)),
        ("holder_trades", lambda p: update_holder_trades(progress=p)),
        ("index_bars", lambda p: update_index_bars(progress=p)),
        ("index_members", lambda p: update_index_members(progress=p)),
    ]
    if flow_codes:
        steps.append(("fund_flow", lambda p: update_fund_flow(flow_codes, progress=p)))
    out: dict[str, dict] = {}
    n: int = len(steps)
    for i, (name, fn) in enumerate(steps):
        report(i / n, f"正在更新「{name}」…")
        out[name] = fn(_scaled(report, i / n, (i + 1) / n))
    report(1.0, "扩展数据更新完成：" + "、".join(
        f"{k}{'✓' if v['ok'] else '✗'}({v['rows']})" for k, v in out.items()
    ))
    return out
