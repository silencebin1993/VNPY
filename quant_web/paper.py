"""
模拟盘（前向跟踪）：每天收盘后把各策略的正式信号记下来，之后随着新日线到来，按回测同样的规则计算
"如果照做会怎样"。只记录、不下单；不补记历史（历史表现看回测）。

文件（`PAPER_DIR = WORKSPACE/paper`）：
- `signals.parquet`：每条信号一行，主键 (signal_date, kind, code)：
  signal_date, kind, code, name, rank, score, exp_ret(%，仅 swing), prob, exit_rule, stop_loss_pct(小数), created_at，
  以及记录时的交易设置 max_gap_pct(%), fee_rate, slippage, stamp_by_date, stamp_duty, position_pct（小数）——
  之后算结果一律用记录时的值（改设置不会改写历史）；早期没有这些列的记录用该策略当前的设置补；
- `days.parquet`：每个 (signal_date, kind) 记录过一次就锁定（先记先得，之后再记同一天同一策略直接跳过，
  避免改了设置再记一遍"挑结果"）：signal_date, kind, picks, trade(闸门是否允许操作), note, created_at。

只在"收盘后、信号日 = 最新完整交易日"时记录：交易日 15:05 之前拒绝（那时只能拿到上一交易日的信号，
而它的买入时点——今天开盘——已经过去，不算事前记录）；日线还没更新到最新交易日时也拒绝。

结果计算用 `predict.backtest.simulate_trades`（与回测相同的成交规则与费用，逐笔、不受资金约束），
状态：pending 待买入 / unfilled 未成交 / holding 持有中 / closed 已卖出。
同一策略里还"持有中/待买入"的股票再次入选时不记录（held_skipped，和回测"已持有跳过"一致）。
收益曲线按账户算：每笔持仓占记录时的 position_pct（波段默认 4%），其余资金不产生收益（见 EQUITY_METHOD）。
"""
import json
import logging
import math
import os
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from . import config
from . import settings as settings_mod


log = logging.getLogger("quant_web.paper")

PAPER_DIR: Path = config.WORKSPACE.joinpath("paper")
_DEFAULT_PAPER_DIR: Path = PAPER_DIR

KIND_LABELS: dict[str, str] = {"swing": "强势股波段", "streak": "连板晋级", "first": "首板潜力"}
FAMILY: dict[str, str] = {"streak": "limit", "first": "limit", "swing": "swing"}     # 门槛含义相同的一组
EXIT_RULES: tuple[str, ...] = ("next_open", "next_close", "until_break", "until_break_close")
DEFAULT_EXIT: dict[str, str] = {"swing": "until_break_close"}
STATUS_LABELS: dict[str, str] = {"pending": "待买入", "unfilled": "未成交", "holding": "持有中", "closed": "已卖出"}
RECORD_AFTER: time = time(15, 5)
PANEL_COLUMNS: list[str] = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "is_st"]
MAX_TRADES: int = 5000
SWING_POSITION_PCT: float = 0.04       # 波段默认单只仓位（设置模块读不到时用）

SIGNAL_SCHEMA: dict[str, Any] = {
    "signal_date": pl.Date, "kind": pl.Utf8, "code": pl.Utf8, "name": pl.Utf8, "rank": pl.Int32,
    "score": pl.Float64, "exp_ret": pl.Float64, "prob": pl.Float64, "exit_rule": pl.Utf8,
    "stop_loss_pct": pl.Float64, "created_at": pl.Utf8,
    # 记录时的交易设置（算结果用；早期记录没有这些列 → 用当前设置补）
    "max_gap_pct": pl.Float64, "fee_rate": pl.Float64, "slippage": pl.Float64, "stamp_by_date": pl.Boolean,
    "stamp_duty": pl.Float64, "position_pct": pl.Float64,
}
COST_COLUMNS: tuple[str, ...] = ("max_gap_pct", "fee_rate", "slippage", "stamp_by_date", "stamp_duty")
DAY_SCHEMA: dict[str, Any] = {
    "signal_date": pl.Date, "kind": pl.Utf8, "picks": pl.Int32, "trade": pl.Boolean, "note": pl.Utf8,
    "created_at": pl.Utf8,
}
RESULT_SCHEMA: dict[str, Any] = {
    "status": pl.Utf8, "entry_date": pl.Date, "entry": pl.Float64, "exit_date": pl.Date, "exit": pl.Float64,
    "ret": pl.Float64, "hold_days": pl.Int32, "exit_reason": pl.Utf8,
}
EQUITY_METHOD: str = (
    "收益曲线按「账户」算：每笔持仓占总资金的一个固定比例（记录时设置里的单只仓位，强势股波段默认 4%），"
    "其余资金不产生收益；某天的账户收益 = Σ 每笔持仓的仓位比例 × 它当天的收益（买入日按买入价到收盘、"
    "卖出日按前一日收盘到卖出价、中间按收盘到收盘，费用计入卖出日，持有中的按最新收盘价估算）。"
    "同时持有的仓位合计超过 100% 时按比例缩小到 100%（钱不够全买）。没有持仓的日子收益为 0，从 1 开始逐日复利。"
    "不考虑一手 100 股和最低佣金，和真实账户会有差异。"
)

_lock = threading.RLock()
_eval_cache: dict[str, Any] = {"key": None, "value": None}


# ================================================================ 基础工具

def paper_dir() -> Path:
    """PAPER_DIR 被显式改过就用它，否则跟随 config.WORKSPACE（测试会改 WORKSPACE）"""
    if PAPER_DIR != _DEFAULT_PAPER_DIR:
        return PAPER_DIR
    return config.WORKSPACE.joinpath("paper")


def signals_path() -> Path:
    return paper_dir().joinpath("signals.parquet")


def days_path() -> Path:
    return paper_dir().joinpath("days.parquet")


def china_now() -> datetime:
    return datetime.now(config.CHINA_TZ)


def _now_text() -> str:
    return china_now().strftime("%Y-%m-%d %H:%M:%S")


def _file_sig(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _panel_sig() -> tuple:
    items: list[tuple] = []
    try:
        with os.scandir(config.PANEL_DIR) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(".parquet"):
                    st = entry.stat()
                    items.append((entry.name, st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    return (str(config.PANEL_DIR), tuple(sorted(items)), _file_sig(config.UNIVERSE_FILE))


def _empty(schema: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _conform(df: pl.DataFrame, schema: dict[str, Any]) -> pl.DataFrame:
    """补齐缺的列、统一类型、只保留约定的列（文件是旧版本或手工改过也能读）"""
    exprs: list[pl.Expr] = [
        (pl.col(c) if c in df.columns else pl.lit(None)).cast(t, strict=False).alias(c) for c, t in schema.items()
    ]
    return df.select(exprs)


def _read(path: Path, schema: dict[str, Any]) -> pl.DataFrame:
    """不存在或 0 字节 → 空表；文件损坏 → 抛 RuntimeError（中文），绝不静默覆盖"""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return _empty(schema)
        return _conform(pl.read_parquet(path), schema)
    except OSError as e:
        raise RuntimeError(f"模拟盘记录文件读不了（{path.name}）：{e}。请关闭占用它的程序后重试") from e
    except Exception as e:  # noqa: BLE001  parquet 损坏
        raise RuntimeError(f"模拟盘记录文件已损坏（{path}），为避免覆盖已保留原文件，请把它改名或删除后重试：{e}") from e


def _write(df: pl.DataFrame, path: Path) -> None:
    """原子写入：先写临时文件再替换"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    df.write_parquet(tmp)
    os.replace(tmp, path)


def load_signals() -> pl.DataFrame:
    return _read(signals_path(), SIGNAL_SCHEMA)


def load_days() -> pl.DataFrame:
    return _read(days_path(), DAY_SCHEMA)


def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(vars(obj)) if hasattr(obj, "__dict__") else {}


def _trade_dict(settings: Any) -> dict:
    if settings is None:
        return {}
    part: Any = settings.get("trade") if isinstance(settings, dict) else getattr(settings, "trade", None)
    return _as_dict(part)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _check_kind(kind: str) -> str:
    if kind not in KIND_LABELS:
        options: str = "、".join(f"{k}（{v}）" for k, v in KIND_LABELS.items())
        raise ValueError(f"不认识的策略「{kind}」，可选：{options}")
    return kind


# ================================================================ 每个策略用的设置

def with_kind(s: settings_mod.Settings, kind: str) -> settings_mod.Settings:
    """把设置的预测类型换成 kind（设置模块还不认识该类型时退回不校验的拷贝）"""
    data: dict = s.model_dump()
    data["predict"]["kind"] = kind
    try:
        return settings_mod.Settings.model_validate(data)
    except Exception as e:  # noqa: BLE001  pydantic ValidationError
        errors: list = e.errors() if hasattr(e, "errors") else []
        if errors and all(tuple(err.get("loc", ()))[-1:] == ("kind",) for err in errors):
            return s.model_copy(update={"predict": s.predict.model_copy(update={"kind": kind})})
        if hasattr(settings_mod, "format_errors") and errors:
            raise settings_mod.SettingsError(settings_mod.format_errors(e)) from None
        raise


def settings_for(kind: str, base: settings_mod.Settings | None = None) -> settings_mod.Settings:
    """某个策略"当前"的设置。

    保存的设置只有一套 predict/trade（带一个 kind）。门槛含义相同的策略（连板/首板：概率）直接沿用，
    只换 kind；门槛含义不同的（swing 的门槛是预期收益%）把随模型切换的字段（`settings.KIND_FIELDS`：门槛、
    每天几只、板块、仓位、最多持有、卖出规则…）换成 `settings.defaults_for(kind)` 的该策略默认值，其余
    （权重、资金、费用、止损等）保留——与 `settings.for_kind` 相同；设置模块没有 defaults_for 时同样只换 kind。
    """
    s: settings_mod.Settings = base if base is not None else settings_mod.load()
    current: str = str(s.predict.kind)
    if current == kind or FAMILY.get(current, current) == FAMILY.get(kind, kind):
        return with_kind(s, kind)
    fn: Callable | None = getattr(settings_mod, "defaults_for", None)
    if fn is None:
        return with_kind(s, kind)
    d: dict = _as_dict(fn(kind))
    fields: dict | None = getattr(settings_mod, "KIND_FIELDS", None)
    data: dict = s.model_dump()
    for part in ("predict", "trade"):
        src: Any = d.get(part)
        if isinstance(src, dict):
            keys: Any = fields.get(part, []) if isinstance(fields, dict) else src.keys()
            data[part].update({k: src[k] for k in keys if k in src})
    data["predict"]["kind"] = kind
    try:
        return settings_mod.Settings.model_validate(data)
    except Exception:  # noqa: BLE001  设置模块还不认识该 kind：先按原 kind 校验再换 kind
        data["predict"]["kind"] = current
        try:
            return with_kind(settings_mod.Settings.model_validate(data), kind)
        except Exception:  # noqa: BLE001  默认值本身校验不过：退回只换 kind
            return with_kind(s, kind)


# ================================================================ 什么时候可以记录

def _is_trading_day(day: date) -> bool:
    try:
        from .market import realtime

        return bool(realtime.is_trading_day(day))
    except Exception:  # noqa: BLE001  日历不可用：按工作日估算
        return day.weekday() < 5


def _prev_trading_day(day: date) -> date:
    """day 之前（不含）最近的交易日"""
    d: date = day - timedelta(days=1)
    for _ in range(40):
        if _is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d


def record_block_reason(now: datetime | None = None) -> str | None:
    """现在不能记录时返回中文原因，可以记录返回 None"""
    now = now or china_now()
    if _is_trading_day(now.date()) and now.time() < RECORD_AFTER:
        return (
            f"今天（{now:%m月%d日}）是交易日，要等 15:05 收盘数据出来之后才能记录今天的信号。"
            "现在能拿到的只是上一个交易日的信号，它们的买入时间（今天开盘）已经过去，再记录就不算「事前」跟踪了。"
        )
    return None


def expected_signal_day(now: datetime | None = None) -> date:
    """此刻应当记录的信号日：交易日 15:05 以后是今天，否则是上一个交易日"""
    now = now or china_now()
    today: date = now.date()
    if _is_trading_day(today) and now.time() >= RECORD_AFTER:
        return today
    return _prev_trading_day(today)


# ================================================================ 记录

def _record(kind: str, result: dict, settings: Any, now: datetime | None = None, strict: bool = True) -> dict:
    """记录一个策略的信号，返回 {"status","recorded","picks","signal_date","message"}。

    status：recorded 已记录 / no_trade 今天不操作（也会锁定这一天）/ already 已记录过 /
    stale 数据不是最新 / too_early 交易日收盘前 / no_data 没有数据或模型。
    held_skipped：选中但该策略里还"持有中/待买入"而没有记录的只数（不重复买同一只）。
    """
    _check_kind(kind)
    label: str = KIND_LABELS[kind]
    result = result or {}
    if result.get("kind") not in (None, kind):
        raise ValueError(f"预测结果的策略（{result.get('kind')}）和要记录的策略（{kind}）不一致")
    day: date | None = _parse_date(result.get("signal_date"))
    out: dict[str, Any] = {"status": "no_data", "recorded": 0, "picks": 0, "held_skipped": 0,
                           "signal_date": day.isoformat() if day else None, "message": ""}
    if day is None or (result.get("model") is None and not result.get("rows")):
        warnings: list = [w for w in result.get("warnings") or [] if isinstance(w, str)]
        out["message"] = f"{label}：没有可记录的预测（{warnings[0] if warnings else '还没有数据或模型'}）"
        return out
    if strict:
        block: str | None = record_block_reason(now)
        if block:
            out.update(status="too_early", message=block)
            return out
        expected: date = expected_signal_day(now)
        if day < expected:
            out.update(status="stale", message=(
                f"{label}：日线数据只到 {day}，还没有 {expected} 的收盘数据，请先更新数据再记录"))
            return out

    rows: list[dict] = [r for r in result.get("rows") or [] if isinstance(r, dict)]
    picks: list[tuple[int, dict]] = [(i + 1, r) for i, r in enumerate(rows) if r.get("pick") and r.get("code")]
    gate: Any = result.get("gate")
    trade_ok: bool | None = None
    note: str = ""
    if isinstance(gate, dict):
        trade_ok = bool(gate.get("trade")) if gate.get("trade") is not None else None
        note = str(gate.get("reason") or "")
        if trade_ok is False:
            picks = []
    ts: dict = _trade_dict(settings)
    exit_rule: str = str(ts.get("exit_rule") or "")
    if exit_rule not in EXIT_RULES:
        exit_rule = DEFAULT_EXIT.get(kind, "next_close")
    stop: float = _num(ts.get("stop_loss_pct")) or 0.0
    stored: dict[str, Any] = _stored_trade(kind, ts)
    created: str = _now_text()

    with _lock:
        days: pl.DataFrame = load_days()
        if not days.filter((pl.col("signal_date") == day) & (pl.col("kind") == kind)).is_empty():
            out.update(status="already", message=f"{label}：{day} 的信号已经记录过了（每天只记第一次，改设置不会重记）")
            return out
        sig: pl.DataFrame = load_signals()
        seen: set[str] = set(sig.filter((pl.col("signal_date") == day) & (pl.col("kind") == kind))["code"].to_list())
        held: set[str] = _held_codes(kind, day) if picks else set()
        held_skipped: list[str] = []
        new_rows: list[dict] = []
        for rank, r in picks:
            code: str = str(r["code"]).zfill(6)
            if code in seen:
                continue
            seen.add(code)
            if code in held:                        # 还拿着（或还没买进）同一只：不重复买
                held_skipped.append(str(r.get("name") or code))
                continue
            new_rows.append({
                "signal_date": day, "kind": kind, "code": code, "name": str(r.get("name") or ""), "rank": rank,
                "score": _num(r.get("score")), "exp_ret": _num(r.get("exp_ret")), "prob": _num(r.get("prob")),
                "exit_rule": exit_rule, "stop_loss_pct": stop, "created_at": created, **stored,
            })
        if new_rows:
            merged: pl.DataFrame = pl.concat([sig, pl.DataFrame(new_rows, schema=SIGNAL_SCHEMA)], how="vertical")
            _write(merged.sort(["signal_date", "kind", "rank"]), signals_path())
        held_note: str = (f"{'、'.join(held_skipped)} 还在持有中，不重复买" if held_skipped else "")
        day_row: pl.DataFrame = pl.DataFrame([{
            "signal_date": day, "kind": kind, "picks": len(new_rows), "trade": trade_ok if trade_ok is not None else bool(new_rows),
            "note": "；".join(x for x in (note, held_note) if x), "created_at": created,
        }], schema=DAY_SCHEMA)
        _write(pl.concat([days, day_row], how="vertical").sort(["signal_date", "kind"]), days_path())
        _eval_cache["key"] = None
    out["held_skipped"] = len(held_skipped)
    if new_rows:
        out.update(status="recorded", recorded=len(new_rows), picks=len(new_rows),
                   message=f"{label}：记录了 {len(new_rows)} 只" + (f"（{held_note}）" if held_note else ""))
    elif held_skipped:
        out.update(status="no_trade", message=f"{label}：选中的 {held_note}，今天没有新买入")
    else:
        out.update(status="no_trade", message=f"{label}：今天不操作" + (f"（{note}）" if note else ""))
    return out


def _stored_trade(kind: str, ts: dict) -> dict[str, Any]:
    """记录时存进信号表的交易设置（开盘涨幅上限、费用、滑点、印花税、单只仓位）。
    没给的项留空（算结果时用当前设置补）；波段没给单只仓位时用它的默认 4%"""
    by_date: Any = ts.get("stamp_by_date")
    out: dict[str, Any] = {
        "max_gap_pct": _num(ts.get("max_gap_pct")), "fee_rate": _num(ts.get("fee_rate")),
        "slippage": _num(ts.get("slippage")), "stamp_by_date": by_date if isinstance(by_date, bool) else None,
        "stamp_duty": _num(ts.get("stamp_duty")), "position_pct": _num(ts.get("position_pct")),
    }
    if out["position_pct"] is None:
        out["position_pct"] = _default_position_pct(kind, fallback_current=False)
    return out


def _default_position_pct(kind: str, fallback_current: bool = True) -> float | None:
    """波段：该模型的默认单只仓位（settings.defaults_for，4%）；其他策略：当前设置里的（fallback_current=False 时为空）"""
    if kind == "swing":
        fn: Callable | None = getattr(settings_mod, "defaults_for", None)
        try:
            return float(fn(kind)["trade"]["position_pct"]) if fn is not None else SWING_POSITION_PCT
        except Exception:  # noqa: BLE001
            return SWING_POSITION_PCT
    if not fallback_current:
        return None
    try:
        return _num(_trade_dict(settings_for(kind)).get("position_pct"))
    except Exception:  # noqa: BLE001
        return None


def _held_codes(kind: str, day: date) -> set[str]:
    """该策略在 day 之前的信号里现在还"持有中/待买入"的股票（算不出结果时为空，不影响记录）"""
    try:
        frame: pl.DataFrame = evaluate()
    except Exception as e:  # noqa: BLE001
        log.info("模拟盘：算不出当前持仓，不做重复买入检查：%s", e)
        return set()
    if frame.is_empty():
        return set()
    return set(frame.filter((pl.col("kind") == kind) & (pl.col("signal_date") < day)
                            & pl.col("status").is_in(["holding", "pending"]))["code"].to_list())


def record_signals(kind: str, result: dict, settings: Any, now: datetime | None = None, strict: bool = True) -> int:
    """记录 predict_latest 结果中 pick==True 的行（swing 闸门 gate.trade=False 时一只都不记），返回新增条数。

    主键 (signal_date, kind, code) 去重；同一 (signal_date, kind) 只记第一次。strict=True 时只接受
    "收盘后、信号日 = 最新完整交易日"的结果（见模块说明），否则不记录、返回 0。
    """
    detail: dict = _record(kind, result, settings, now=now, strict=strict)
    if detail["status"] not in ("recorded", "no_trade", "already"):
        log.info("模拟盘没有记录：%s", detail["message"])
    return int(detail["recorded"])


def _service_predict(kind: str, s: Any) -> dict:
    from .predict import service

    return service.predict_latest(kind, s)


def record_today(
    predict_fn: Callable[[str, Any], dict] | None = None,
    settings_fn: Callable[[str], Any] | None = None,
    now: datetime | None = None,
    kinds: list[str] | None = None,
) -> dict:
    """记录今天三个策略的信号（网页「记录今天的信号」和每日流程共用）。

    交易日 15:05 前、或日线还没更新到最新交易日（所有策略都是旧数据）时抛 RuntimeError（中文原因）。
    返回 {"signal_date","kinds":{kind:{status,recorded,picks,held_skipped,signal_date,message}},"recorded","message"}
    （held_skipped：选中但该策略里还持有中/待买入、没有重复记录的只数）。
    """
    now = now or china_now()
    block: str | None = record_block_reason(now)
    if block:
        raise RuntimeError(block)
    expected: date = expected_signal_day(now)
    predict: Callable[[str, Any], dict] = predict_fn or _service_predict
    get_settings: Callable[[str], Any] = settings_fn or settings_for
    out: dict[str, Any] = {"signal_date": expected.isoformat(), "kinds": {}, "recorded": 0}
    for kind in kinds or list(KIND_LABELS):
        label: str = KIND_LABELS.get(kind, kind)
        try:
            s: Any = get_settings(kind)
            detail: dict = _record(kind, predict(kind, s), s, now=now)
        except Exception as e:  # noqa: BLE001  一个策略失败不影响其他策略
            detail = {"status": "error", "recorded": 0, "picks": 0, "held_skipped": 0, "signal_date": None,
                      "message": f"{label}：预测失败（{str(e) or type(e).__name__}）"}
        out["kinds"][kind] = detail
        out["recorded"] += int(detail["recorded"])
    statuses: set[str] = {d["status"] for d in out["kinds"].values()}
    if statuses == {"stale"}:
        raise RuntimeError(
            f"日线数据还没有 {expected} 的收盘数据，今天的信号还没算出来。请先点「更新数据」（或「一键更新」），完成后再记录。")
    parts: list[str] = [d["message"] for d in out["kinds"].values() if d.get("message")]
    out["message"] = f"{expected} 的信号：" + "；".join(parts) if parts else "没有可记录的信号"
    return out


def recorded_kinds(day: date) -> set[str]:
    try:
        days: pl.DataFrame = load_days()
    except RuntimeError:
        return set(KIND_LABELS)          # 文件坏了：不让调度器反复尝试
    return set(days.filter(pl.col("signal_date") == day)["kind"].to_list())


def pending_kinds(day: date, now: datetime | None = None) -> list[str]:
    """调度器用：现在可以记录、day 是应记录的信号日、模型已训练，但这一天还没记录的策略"""
    now = now or china_now()
    if record_block_reason(now) is not None or expected_signal_day(now) != day:
        return []
    done: set[str] = recorded_kinds(day)
    return [k for k in KIND_LABELS if k not in done and config.MODEL_DIR.joinpath(f"{k}_meta.json").exists()]


# ================================================================ 计算结果

def _simulate_fn() -> Callable[..., pl.DataFrame]:
    from .predict import backtest

    fn: Callable | None = getattr(backtest, "simulate_trades", None)
    if fn is None:
        raise ImportError("模拟盘需要的逐笔成交模块（predict.backtest.simulate_trades）还没有准备好",
                          name="predict.backtest.simulate_trades")
    return fn


def _load_panel_lim(codes: list[str], start: date) -> pl.DataFrame:
    """只读信号涉及的股票（信号日前 15 天起），加涨跌停列"""
    from .market import history, universe
    from .predict import limits

    panel: pl.DataFrame = history.load_panel(start=start - timedelta(days=15), codes=codes, columns=PANEL_COLUMNS)
    if panel.is_empty():
        return panel
    return limits.add_limit_columns(panel, universe.load_universe())


def _calendar_kw(simulate: Callable[..., pl.DataFrame], start: date) -> dict:
    """全市场交易日历（simulate_trades 的 calendar 参数）：面板只含信号股时，也能认出买入日停牌。
    替身函数不接受 calendar、或日历读不出来时返回 {}"""
    import inspect

    try:
        if "calendar" not in inspect.signature(simulate).parameters:
            return {}
        from .market import history

        cal: list[date] = history.trade_dates(start - timedelta(days=15))
    except Exception:  # noqa: BLE001
        return {}
    return {"calendar": cal} if cal else {}


def _last_panel_date() -> date | None:
    try:
        from .market import history

        return history.last_date()
    except Exception:  # noqa: BLE001
        return None


def _texts() -> tuple[str, str]:
    """(待买入, 买入日停牌) 的说明文字，与逐笔成交模块一致"""
    try:
        from .predict import execution

        return execution.PENDING_TEXT, execution.SUSPENDED_TEXT
    except Exception:  # noqa: BLE001
        return "等待下一个交易日开盘买入", "买入日停牌，没买到"


def _pending_frame(sig: pl.DataFrame) -> pl.DataFrame:
    return sig.with_columns(
        pl.lit("pending").alias("status"), pl.lit(None, pl.Date).alias("entry_date"),
        pl.lit(None, pl.Float64).alias("entry"), pl.lit(None, pl.Date).alias("exit_date"),
        pl.lit(None, pl.Float64).alias("exit"), pl.lit(None, pl.Float64).alias("ret"),
        pl.lit(None, pl.Int32).alias("hold_days"), pl.lit(_texts()[0]).alias("exit_reason"),
    )


def _trade_for(kind: str, trade: Any = None) -> dict:
    """早期记录（没有存交易设置）算结果时补用的交易设置（费用、滑点、开盘涨幅上限等）：给了 trade 用它，
    否则用该策略当前的设置。卖出规则/止损不在这里：按每条信号记录时的设置"""
    if trade is not None:
        d: dict = _as_dict(trade)
    else:
        try:
            d = _trade_dict(settings_for(kind))
        except Exception:  # noqa: BLE001  设置读不出来时用默认
            d = {}
    for key in ("exit_rule", "stop_loss_pct"):
        d.pop(key, None)
    return d


def _fill_stored(sig: pl.DataFrame, trades_by_kind: dict[str, dict]) -> pl.DataFrame:
    """信号表里空着的交易设置（早期记录）用各策略的补用设置填上；单只仓位还空着时用该策略的默认"""
    exprs: list[pl.Expr] = []
    for col in (*COST_COLUMNS, "position_pct"):
        dtype: Any = SIGNAL_SCHEMA[col]
        mapping: dict[str, Any] = {}
        for kind, td in trades_by_kind.items():
            v: Any = td.get(col)
            if col == "position_pct" and _num(v) is None:
                v = _default_position_pct(kind)
            if col == "stamp_by_date":
                mapping[kind] = v if isinstance(v, bool) else None
            else:
                mapping[kind] = _num(v)
        fallback: pl.Expr = pl.col("kind").replace_strict(mapping, default=None, return_dtype=dtype)
        exprs.append(pl.col(col).fill_null(fallback).alias(col))
    return sig.with_columns(exprs)


def _evaluate_bundle(trade: Any = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(每条信号的结果, 相关股票的收盘价 [code,date,close])，按 (信号文件, 面板, 各策略补用设置) 缓存。
    每条信号按记录时存下的费用/滑点/开盘涨幅上限计算（早期记录没有时用 trade 或该策略当前的设置补）"""
    trades_by_kind: dict[str, dict] = {k: _trade_for(k, trade) for k in KIND_LABELS}
    key: tuple = (str(signals_path()), _file_sig(signals_path()), _panel_sig(),
                  json.dumps(trades_by_kind, sort_keys=True, default=str))
    with _lock:
        if _eval_cache["key"] == key:
            return _eval_cache["value"]
    sig: pl.DataFrame = _fill_stored(load_signals(), trades_by_kind)
    closes: pl.DataFrame = pl.DataFrame(schema={"code": pl.Utf8, "date": pl.Date, "close": pl.Float64})
    if sig.is_empty():
        frame: pl.DataFrame = sig.with_columns(**{c: pl.lit(None, t) for c, t in RESULT_SCHEMA.items()})
    else:
        codes: list[str] = sig["code"].unique().to_list()
        panel_lim: pl.DataFrame = _load_panel_lim(codes, sig["signal_date"].min())
        if panel_lim.is_empty():
            frame = _pending_frame(sig)
        else:
            simulate: Callable[..., pl.DataFrame] = _simulate_fn()
            cal_kw: dict = _calendar_kw(simulate, sig["signal_date"].min())
            parts: list[pl.DataFrame] = []
            keys: list[str] = ["kind", *COST_COLUMNS]
            for grp in sig.partition_by(keys, maintain_order=True):
                first: dict = grp.row(0, named=True)
                kind: str = first["kind"]
                td: dict = dict(trades_by_kind.get(kind) or _trade_for(kind, trade) or {})
                td.update({c: first[c] for c in COST_COLUMNS if first[c] is not None})
                inp: pl.DataFrame = grp.select("signal_date", "code", "kind", "exit_rule", "stop_loss_pct")
                res: pl.DataFrame = simulate(inp, panel_lim, td or None, **cal_kw)
                parts.append(res.select("signal_date", "code", "kind", *[
                    (pl.col(c) if c in res.columns else pl.lit(None)).cast(t, strict=False).alias(c)
                    for c, t in RESULT_SCHEMA.items()
                ]))
            res_all: pl.DataFrame = pl.concat(parts, how="vertical")
            frame = sig.join(res_all, on=["signal_date", "code", "kind"], how="left", maintain_order="left")
            closes = panel_lim.select("code", "date", pl.col("close").cast(pl.Float64))
        # 买入日已经有全市场数据、这只股票却没有 → 停牌，买不进
        last: date | None = _last_panel_date()
        if last is not None:
            suspended: pl.Expr = (pl.col("status") == "pending") & (pl.col("signal_date") < last)
            frame = frame.with_columns(
                pl.when(suspended).then(pl.lit("unfilled")).otherwise(pl.col("status")).alias("status"),
                pl.when(suspended).then(pl.lit(_texts()[1])).otherwise(pl.col("exit_reason")).alias("exit_reason"),
            )
        frame = frame.with_columns(pl.col("status").fill_null("pending"))
    value: tuple[pl.DataFrame, pl.DataFrame] = (frame, closes)
    with _lock:
        _eval_cache.update(key=key, value=value)
    return value


def evaluate(trade: Any = None) -> pl.DataFrame:
    """用最新面板计算每条信号的实际结果（与该策略的回测规则一致）。

    列：signals.parquet 的全部列 + status, entry_date, entry, exit_date, exit, ret(扣费后，小数；持有中为按最新收盘价的浮动收益),
    hold_days, exit_reason。费用/滑点/开盘涨幅上限/卖出规则/止损/单只仓位都按每条信号记录时存下的设置；
    早期记录没有存的项用 trade（默认各策略当前的设置 settings_for(kind)）补。
    """
    return _evaluate_bundle(trade)[0]


def equity_curve(frame: pl.DataFrame, closes: pl.DataFrame, start: date | None = None,
                 default_pct: float = 1.0) -> list[dict]:
    """账户式净值曲线（算法见 EQUITY_METHOD）。

    每笔已买入的交易（持有中/已卖出）拆成逐日收益系数：买入日 = 收盘/买入价，之后 = 收盘/前收，
    卖出日 = 卖出价/前收（卖出当天就是买入当天时 = 卖出价/买入价）；再把最后一天的系数乘上
    (1+ret)/(毛收益)，使每笔各天系数的乘积恰好等于扣费后的 1+ret。
    每笔的仓位比例 w = position_pct 列（没有该列或为空时 default_pct）；账户当天收益 = Σ w×(系数-1)，
    当天持仓的 Σw 超过 1 时按比例缩小到 1（default_pct=1 时就是"当天各持仓等权平均"）；
    没有持仓为 0；从信号起始日的 1.0 开始逐日复利。
    """
    filled: pl.DataFrame = frame.filter(
        pl.col("status").is_in(["holding", "closed"]) & pl.col("entry_date").is_not_null()
        & pl.col("entry").is_not_null() & (pl.col("entry") > 0) & pl.col("ret").is_not_null()
    )
    if filled.is_empty():
        return []
    weight: pl.Expr = (pl.col("position_pct").cast(pl.Float64).fill_null(default_pct)
                       if "position_pct" in filled.columns else pl.lit(float(default_pct)))
    trades: pl.DataFrame = filled.select(
        pl.int_range(pl.len()).alias("_id"), "code", "entry_date", "entry", "exit_date", "exit", "ret", "status",
        weight.clip(0.0, 1.0).alias("_w"),
    )
    joined: pl.DataFrame = (
        trades.join(closes.filter(pl.col("close") > 0), on="code", how="inner")
        .filter((pl.col("date") >= pl.col("entry_date"))
                & (pl.col("exit_date").is_null() | (pl.col("date") <= pl.col("exit_date"))))
        .sort(["_id", "date"])
        .with_columns(pl.col("close").shift(1).over("_id").alias("_prev"))
    )
    if joined.is_empty():
        return []
    is_exit: pl.Expr = (pl.col("status") == "closed") & (pl.col("date") == pl.col("exit_date")) & pl.col("exit").is_not_null()
    base: pl.Expr = pl.when(pl.col("_prev").is_null()).then(pl.col("entry")).otherwise(pl.col("_prev"))
    joined = joined.with_columns(
        pl.when(is_exit).then(pl.col("exit") / base).otherwise(pl.col("close") / base).alias("_f")
    ).with_columns(
        pl.col("_f").log().sum().over("_id").exp().alias("_gross"),
        (pl.col("date") == pl.col("date").max().over("_id")).alias("_last"),
    ).with_columns(
        pl.when(pl.col("_last")).then(pl.col("_f") * (1 + pl.col("ret")) / pl.col("_gross"))
        .otherwise(pl.col("_f")).alias("_f")
    )
    daily: pl.DataFrame = joined.group_by("date").agg(
        ((pl.col("_f") - 1) * pl.col("_w")).sum().alias("_s"), pl.col("_w").sum().alias("_ws"),
    ).with_columns(
        (pl.col("_s") / pl.max_horizontal(pl.col("_ws"), pl.lit(1.0))).alias("r")
    ).sort("date")
    first: date = start or filled["entry_date"].min()
    axis: list[date] = sorted(d for d in set(closes["date"].to_list()) if d >= first)
    rets: dict[date, float] = dict(zip(daily["date"].to_list(), daily["r"].to_list(), strict=False))
    value: float = 1.0
    curve: list[dict] = []
    if start is not None and start < (axis[0] if axis else start + timedelta(days=1)):
        curve.append({"date": start.isoformat(), "value": 1.0})
    for d in axis:
        r: float | None = rets.get(d)
        if r is not None and math.isfinite(r):
            value *= 1 + r
        curve.append({"date": d.isoformat(), "value": round(value, 6)})
    return curve


def _mean(values: list[float]) -> float | None:
    vals: list[float] = [v for v in values if v is not None and math.isfinite(v)]
    return sum(vals) / len(vals) if vals else None


def _round(v: float | None, digits: int = 5) -> float | None:
    return round(v, digits) if v is not None and math.isfinite(v) else None


def _kind_summary(kind: str, frame: pl.DataFrame, closes: pl.DataFrame, days: pl.DataFrame) -> dict:
    df: pl.DataFrame = frame.filter(pl.col("kind") == kind)
    kd: pl.DataFrame = days.filter(pl.col("kind") == kind)
    count: dict[str, int] = {s: df.filter(pl.col("status") == s).height for s in STATUS_LABELS}
    closed: pl.DataFrame = df.filter(pl.col("status") == "closed")
    rets: list[float] = [v for v in closed["ret"].to_list() if v is not None]
    holding_rets: list[float] = [v for v in df.filter(pl.col("status") == "holding")["ret"].to_list() if v is not None]
    start: date | None = min([*kd["signal_date"].to_list(), *df["signal_date"].to_list()], default=None)
    pct_default: float | None = _default_position_pct(kind)
    curve: list[dict] = equity_curve(df, closes, start=df["signal_date"].min() if df.height else None,
                                     default_pct=pct_default if pct_default is not None else 1.0)
    pcts: list = df.sort("signal_date")["position_pct"].drop_nulls().to_list() if "position_pct" in df.columns else []
    position_pct: float | None = float(pcts[-1]) if pcts else pct_default
    hold: list[float] = [float(v) for v in closed["hold_days"].to_list() if v is not None]
    last_day: date | None = max([*kd["signal_date"].to_list(), *df["signal_date"].to_list()], default=None)
    return {
        "label": KIND_LABELS[kind],
        "since": start.isoformat() if start else None,
        "last_signal_date": last_day.isoformat() if last_day else None,
        "days": kd.height,
        "no_trade_days": kd.filter(pl.col("picks") == 0).height,
        "signals": df.height,
        "filled": count["holding"] + count["closed"],
        "unfilled": count["unfilled"],
        "pending": count["pending"],
        "open": count["holding"],
        "closed": count["closed"],
        "win_rate": _round(sum(1 for v in rets if v > 0) / len(rets), 4) if rets else None,
        "avg_return": _round(_mean(rets)),
        "open_return": _round(_mean(holding_rets)),
        "avg_hold_days": _round(_mean(hold), 2),
        "total_return": _round(curve[-1]["value"] - 1) if curve else None,
        "position_pct": _round(position_pct, 4) if position_pct is not None else None,
        "equity": curve,
    }


def summary() -> dict:
    """{"since","kinds":{kind:{signals,filled,unfilled,pending,open,closed,win_rate,avg_return,open_return,
    total_return,position_pct,equity[{date,value}],days,no_trade_days,last_signal_date,...}},"updated_at","data_end",
    "equity_method","warnings"}。win_rate/avg_return 只算已卖出的（每笔的收益）；total_return 取账户式收益曲线
    （每笔占 position_pct，其余资金不产生收益，含持有中的浮动盈亏，见 EQUITY_METHOD）"""
    warnings: list[str] = []
    try:
        days: pl.DataFrame = load_days()
    except RuntimeError as e:
        warnings.append(str(e))
        days = _empty(DAY_SCHEMA)
    closes: pl.DataFrame = pl.DataFrame(schema={"code": pl.Utf8, "date": pl.Date, "close": pl.Float64})
    try:
        frame, closes = _evaluate_bundle()
    except Exception as e:  # noqa: BLE001  算不出结果时至少显示记录了哪些信号
        warnings.append(f"暂时算不出模拟盘结果：{str(e) or type(e).__name__}")
        try:
            frame = _pending_frame(load_signals())
        except RuntimeError as e2:
            warnings.append(str(e2))
            frame = _pending_frame(_empty(SIGNAL_SCHEMA))
    starts: list[date] = [*days["signal_date"].to_list(), *frame["signal_date"].to_list()]
    last: date | None = _last_panel_date()
    return {
        "since": min(starts).isoformat() if starts else None,
        "kinds": {k: _kind_summary(k, frame, closes, days) for k in KIND_LABELS},
        "updated_at": _now_text(),
        "data_end": last.isoformat() if last else None,
        "equity_method": EQUITY_METHOD,
        "warnings": warnings,
    }


def trades(kind: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
    """每条信号的明细（新的在前）：信号字段 + status/status_label/kind_label + entry/exit 日期价格、ret、hold_days、exit_reason"""
    if kind:
        _check_kind(kind)
    if status and status not in STATUS_LABELS:
        options: str = "、".join(f"{k}（{v}）" for k, v in STATUS_LABELS.items())
        raise ValueError(f"不认识的状态「{status}」，可选：{options}")
    frame: pl.DataFrame = evaluate()
    if kind:
        frame = frame.filter(pl.col("kind") == kind)
    if status:
        frame = frame.filter(pl.col("status") == status)
    order: dict[str, int] = {k: i for i, k in enumerate(KIND_LABELS)}
    frame = (
        frame.with_columns(pl.col("kind").replace_strict(order, default=len(order), return_dtype=pl.Int32).alias("_k"))
        .sort(["signal_date", "_k", "rank"], descending=[True, False, False], nulls_last=True)
        .drop("_k")
        .head(max(1, min(int(limit), MAX_TRADES)))
    )
    out: list[dict] = []
    for r in frame.iter_rows(named=True):
        row: dict = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in r.items()}
        for key in ("entry", "exit"):
            row[key] = _round(row.get(key), 3)
        row["ret"] = _round(row.get("ret"))
        row["kind_label"] = KIND_LABELS.get(row.get("kind") or "", row.get("kind"))
        row["status_label"] = STATUS_LABELS.get(row.get("status") or "", row.get("status"))
        out.append(row)
    return out
