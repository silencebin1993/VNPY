"""
训练数据：股票范围 → 标签（真实交易规则的净收益和超额）→ 因子 → 按天标准化。
- 标签和公式验证、选股器回测同一个口径（formula.validate.forward_returns）：信号日收盘后决定，第二天开盘买
  （开盘涨停买不进 → 这一行没有标签），持有 N 天收盘卖（跌停卖不出顺延），扣佣金、印花税、滑点；
  超额 = 净收益 − 同一天范围内所有买得进的股票的平均净收益（"同日随机买"）；label_end = 卖出日（净化用）。
- 因子只用当天及以前的数据；按股票分块计算控制内存；最后只保留"范围内"的行（主板、上市满 60 天、非 ST、
  20 日平均成交额够）。
- 基础数据（全历史字段表 + 公式表）直接用选股器回测的缓存（按日线文件签名，第一次约 3 分钟，之后几秒）。
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from . import factors as F
from . import options as O

Progress = Callable[[float, str], None]
MIN_LISTED: int = 60
CHUNK_CODES: int = 400                  # 按股票分块算因子，每块多少只
FEATURE_LOOKBACK_DAYS: int = 600        # 算因子时往前多取多少个日历日（最长 250 个交易日的窗口 + 递推指标的预热；训练和打分一致）


@dataclass
class Dataset:
    ds: pl.DataFrame                                    # date, code, net, excess, target, y, label_end, filled, 因子...
    features: list[str]
    sets: dict[str, list[str]] = field(default_factory=dict)
    info: dict = field(default_factory=dict)


def _say(progress: Progress | None) -> Progress:
    return progress or (lambda f, m: None)


TABLE_COLS: list[str] = ["code", "date", "board", "pos", "is_st", "amt20", "open", "close", "raw_open", "raw_close", "preclose",
                          "limit_up", "limit_down", "float_cap", "stage"]


def load_base(progress: Progress | None = None, boards: list[str] | None = None,
              codes: list[str] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """全历史字段表（特征 + 主力阶段 + 交易列）和公式表（前复权），和选股器回测共用缓存。
    只读需要的股票和列（全市场全部读进来要 6GB 多内存）；缓存还没有时先整理一次（第一次约 1~3 分钟、内存峰值较高）"""
    import gc

    from ..formula import engine as fengine
    from ..screener import backtest as sbt

    if not _base_cached():
        _ = sbt.load_or_build_history(use_chips=False, progress=progress)
        del _
        gc.collect()
    path, _stamp = sbt._cache_paths(False)
    want: list[str] = TABLE_COLS + F.MAINFORCE_COLS + [f"score_{s}" for s in F.STAGES]
    have: list[str] = list(pl.read_parquet_schema(path).keys())
    q = pl.scan_parquet(path).select([c for c in dict.fromkeys(want) if c in have])
    if codes is not None:
        q = q.filter(pl.col("code").is_in(codes))
    elif boards:
        q = q.filter(pl.col("board").is_in(boards))
    table: pl.DataFrame = q.collect()
    frame: pl.DataFrame = fengine.load_frame(codes=table["code"].unique().to_list()) if table.height else table.head(0)
    return table, frame


def _watchlist() -> list[str]:
    from .. import config
    try:
        data = json.loads(config.WATCHLIST_FILE.read_text(encoding="utf-8")) if config.WATCHLIST_FILE.exists() else []
    except (OSError, ValueError):
        return []
    return [str(x["code"] if isinstance(x, dict) else x) for x in data if x]


def _index_members(index: str) -> list[str]:
    from ..providers import store
    df: pl.DataFrame = store.load("index_members")
    if df.is_empty() or "index" not in df.columns:
        return []
    return df.filter(pl.col("index") == index)["code"].unique().to_list()


def universe_codes(cfg: dict, all_codes: list[str] | None = None) -> list[str] | None:
    """范围里的股票代码；None 表示按板块过滤即可（main / all）"""
    u: dict = O.UNIVERSES[cfg["universe"]]
    if cfg["universe"] == "watchlist":
        codes = _watchlist()
        if not codes:
            raise ValueError("自选股是空的，请先添加自选股，或换一个股票范围")
        return codes
    if u.get("index"):
        codes = _index_members(u["index"])
        if not codes:
            raise ValueError(f"本地还没有{u['label']}名单：请在“数据中心 → 扩展数据”里更新一次指数成分股")
        return codes
    return None


def eligible_expr(cfg: dict, start: date | None, end: date | None) -> pl.Expr:
    u: dict = O.UNIVERSES[cfg["universe"]]
    e: pl.Expr = pl.col("board").is_in(u["boards"]) & (pl.col("pos") >= MIN_LISTED) & ~pl.col("is_st").fill_null(False)
    if cfg["min_amount"] > 0:
        e = e & (pl.col("amt20").fill_null(0) >= cfg["min_amount"])
    if start is not None:
        e = e & (pl.col("date") >= start)
    if end is not None:
        e = e & (pl.col("date") <= end)
    return e.fill_null(False)


def add_labels(table: pl.DataFrame, cfg: dict, cost: Any | None = None) -> pl.DataFrame:
    """table（已按范围里的股票过滤、按 code/date 排序）→ 加 net、filled、label_end（卖出日）"""
    from ..formula import validate
    from ..predict import costs as costs_mod

    need: list[str] = ["code", "date", "open", "close", "raw_open", "raw_close", "limit_up", "limit_down"]
    lab: pl.DataFrame = validate.forward_returns(table.select(need), O.hold_of(cfg), cost or costs_mod.DEFAULT)
    return table.with_columns(lab["net"].cast(pl.Float32).alias("net"), lab["filled"].alias("filled"),
                              lab["exit_date"].alias("label_end"))


def add_targets(rows: pl.DataFrame, cfg: dict) -> pl.DataFrame:
    """同日基准（范围内买得进的股票的平均净收益）→ 超额；训练目标按预测目标取：超额 / 排名 / 是否赚够"""
    lab: dict = O.LABELS[cfg["label"]]
    ok = pl.col("net").is_not_null()
    base = pl.col("net").filter(ok).mean().over("date")
    rows = rows.with_columns(base.cast(pl.Float32).alias("base"))
    rows = rows.with_columns((pl.col("net") - pl.col("base")).cast(pl.Float32).alias("excess"))
    if lab["kind"] == "rank":
        cnt = pl.col("net").count().over("date")
        target = ((pl.col("net").rank("average").over("date") - 1) / (cnt - 1).clip(lower_bound=1) - 0.5)
    elif lab["kind"] == "up":
        target = (pl.col("net") >= lab["threshold"]).cast(pl.Float32)
    else:
        target = pl.col("excess")
    return rows.with_columns(pl.when(ok).then(target).otherwise(None).cast(pl.Float32).alias("target"))


def normalize(df: pl.DataFrame, features: list[str], how: str) -> pl.DataFrame:
    """按天截面排名：每个因子换成当天范围内的相对位置（−0.5 ~ 0.5）；空值留空（训练时按模型需要填 0）"""
    if how != "rank" or not features:
        return df
    exprs: list[pl.Expr] = []
    for f in features:
        cnt = pl.col(f).count().over("date")
        r = (pl.col(f).rank("average").over("date") - 1) / (cnt - 1).clip(lower_bound=1) - 0.5
        exprs.append(pl.when(pl.col(f).is_not_null()).then(r).otherwise(None).cast(pl.Float32).alias(f))
    return df.with_columns(exprs)


def _clean(df: pl.DataFrame, features: list[str]) -> pl.DataFrame:
    return df.with_columns([pl.when(pl.col(f).is_finite()).then(pl.col(f)).otherwise(None).cast(pl.Float32).alias(f)
                            for f in features if f in df.columns])


def compute_features(cfg: dict, table: pl.DataFrame, frame: pl.DataFrame, keys: pl.DataFrame,
                     progress: Progress | None = None) -> tuple[pl.DataFrame, dict[str, list[str]]]:
    """在 keys（code, date：需要因子的行）上算选中的因子组。table / frame 已按范围里的股票过滤（含更早的历史）"""
    say = _say(progress)
    sets: list[str] = cfg["factor_sets"]
    out: pl.DataFrame = keys
    by_set: dict[str, list[str]] = {}
    codes: list[str] = keys["code"].unique(maintain_order=True).to_list()
    frame = frame.filter(pl.col("code").is_in(codes) & (pl.col("date") >= keys["date"].min() - timedelta(days=FEATURE_LOOKBACK_DAYS)))
    n_steps: int = max(len(sets), 1)
    for i, s in enumerate(sets):
        src: str = F.FACTOR_SETS[s]["source"]
        base_f: float = i / n_steps
        say(base_f, f"计算因子：{F.FACTOR_SETS[s]['label']}……")
        if src == "table":
            part: pl.DataFrame = F.table_features(table.join(keys, on=["code", "date"], how="semi"), [s])
        elif src == "cross":
            sub = frame.select(["code", "date", "open", "high", "low", "close", "raw_open", "raw_close", "preclose", "volume",
                                "amount", "turn", "is_st"])
            part = F.cross_builder(s)(sub, progress=lambda f, m, b=base_f: say(b + f / n_steps, m))
            part = part.join(keys, on=["code", "date"], how="semi")
        else:
            items = F._formula_items() if src == "formula" else None
            parts: list[pl.DataFrame] = []
            for j in range(0, len(codes), CHUNK_CODES):
                chunk: list[str] = codes[j:j + CHUNK_CODES]
                sub = frame.filter(pl.col("code").is_in(chunk))
                res = F.formula_features(sub, items) if src == "formula" else F.frame_builder(s)(sub)
                parts.append(res.join(keys, on=["code", "date"], how="semi"))
                say(base_f + min(1.0, (j + len(chunk)) / max(len(codes), 1)) / n_steps,
                    f"计算因子：{F.FACTOR_SETS[s]['label']}（{min(j + len(chunk), len(codes))}/{len(codes)} 只）")
            part = pl.concat(parts, how="diagonal_relaxed") if parts else keys
        names: list[str] = [c for c in part.columns if c not in ("code", "date")]
        by_set[s] = names
        out = out.join(_clean(part, names), on=["code", "date"], how="left")
    return out, by_set


def build(cfg: dict, progress: Progress | None = None, *, base: tuple[pl.DataFrame, pl.DataFrame] | None = None,
          scoring_only: bool = False) -> Dataset:
    """一次训练的完整数据；scoring_only=True 时只取最近一天（给启用的模型打分用，不需要标签）"""
    say = _say(progress)
    t0: float = time.time()
    say(0.0, "读取全历史数据（第一次约 3 分钟，之后几秒）……")
    codes: list[str] | None = universe_codes(cfg)
    table, frame = base or load_base(lambda f, m: say(0.25 * f, m), boards=O.UNIVERSES[cfg["universe"]]["boards"], codes=codes)
    if table.is_empty():
        raise ValueError("本地还没有日线数据，请先点“一键更新”下载数据")
    if codes is not None:
        table = table.filter(pl.col("code").is_in(codes))
    else:
        table = table.filter(pl.col("board").is_in(O.UNIVERSES[cfg["universe"]]["boards"]))
    last: date = table["date"].max()
    if scoring_only:
        start: date | None = last
        end: date | None = last
    else:
        start = date(cfg["start_year"], 1, 1)
        end = date.fromisoformat(cfg["end"]) if cfg.get("end") else None
    say(0.27, "计算标签（按真实交易规则：次日开盘买、持有到期卖、扣成本）……")
    if not scoring_only:
        table = add_labels(table, cfg)
    elig: pl.DataFrame = table.filter(eligible_expr(cfg, start, end))
    if elig.is_empty():
        raise ValueError("这个范围和时间段里没有符合条件的股票")
    keep: list[str] = ["code", "date"] + ([] if scoring_only else ["net", "filled", "label_end"])
    rows: pl.DataFrame = elig.select(keep)
    if not scoring_only:
        rows = add_targets(rows, cfg)
    say(0.3, f"范围内 {rows['code'].n_unique()} 只股票、{rows['date'].n_unique()} 个交易日、{rows.height:,} 行")
    if scoring_only:
        table = table.filter(pl.col("date") >= last - timedelta(days=FEATURE_LOOKBACK_DAYS))
    feats_df, by_set = compute_features(cfg, table, frame, rows.select("code", "date"),
                                        progress=lambda f, m: say(0.3 + 0.6 * f, m))
    features: list[str] = [f for s in cfg["factor_sets"] for f in by_set.get(s, [])]
    if not features:
        raise ValueError("选中的因子组没有算出任何因子")
    ds: pl.DataFrame = rows.join(feats_df, on=["code", "date"], how="left")
    # 全部为空的因子去掉（例如没有财报数据时的基本面）
    empty: list[str] = [f for f in features if ds[f].null_count() == ds.height]
    features = [f for f in features if f not in empty]
    by_set = {k: [f for f in v if f not in empty] for k, v in by_set.items()}
    say(0.92, "按天标准化因子……")
    ds = normalize(ds.drop(empty), features, cfg["normalize"]).sort(["date", "code"])
    info: dict = {
        "rows": ds.height, "stocks": ds["code"].n_unique(), "days": ds["date"].n_unique(),
        "start": str(ds["date"].min()), "end": str(ds["date"].max()), "features": len(features),
        "labeled": 0 if scoring_only else int(ds["target"].is_not_null().sum()),
        "dropped_empty": empty, "seconds": round(time.time() - t0, 1),
        "survivorship": bool(O.UNIVERSES[cfg["universe"]].get("index")),
    }
    return Dataset(ds=ds, features=features, sets=by_set, info=info)


# ---------------------------------------------------------------- 训练前估算（不读全量数据，几毫秒）

_COUNT_CACHE: dict[str, tuple[float, dict]] = {}


def _board_counts() -> dict:
    hit = _COUNT_CACHE.get("boards")
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    from ..market import universe
    u: pl.DataFrame = universe.load_universe()
    counts: dict[str, int] = {}
    if not u.is_empty():
        b = u.with_columns(pl.col("code").map_elements(universe.board_of, return_dtype=pl.Utf8).alias("_b"))
        for row in b.group_by("_b").len().iter_rows():
            counts[str(row[0])] = int(row[1])
    res: dict = {"boards": counts, "total": int(sum(counts.values()))}
    _COUNT_CACHE["boards"] = (time.time(), res)
    return res


ROUNDS_TYPICAL: dict[str, int] = {"fast": 150, "standard": 400, "fine": 800}      # 提前停止后大概的轮数（估算用）


def _base_cached() -> bool:
    """选股器的全历史字段表是否已经缓存（没缓存时第一次要整理约 3 分钟）"""
    try:
        from ..analysis.market import _panel_stamp
        from ..screener import backtest as sbt
        path, stamp_path = sbt._cache_paths(False)
        return path.exists() and stamp_path.exists() and stamp_path.read_text(encoding="utf-8") == f"v{sbt.CACHE_VERSION}|{_panel_stamp()}"
    except Exception:  # noqa: BLE001
        return False


def estimate(cfg: dict) -> dict:
    """行数、因子数、内存（GB）、大概时间（分钟）；超过上限时 blocked=True 并说明原因"""
    from .. import settings as settings_mod
    from . import models

    bc: dict = _board_counts()
    u: dict = O.UNIVERSES[cfg["universe"]]
    try:
        codes = universe_codes(cfg)
        warn: str = ""
    except ValueError as e:
        codes, warn = [], str(e)
    n_stocks: int = len(codes) if codes is not None else sum(bc["boards"].get(b, 0) for b in u["boards"])
    if not n_stocks and not warn:
        n_stocks = 3000 if cfg["universe"] == "main" else 5000
        warn = "本地股票列表还没下载，按经验值估算"
    end_year: float = date.fromisoformat(cfg["end"]).year + 0.7 if cfg.get("end") else date.today().year + (date.today().timetuple().tm_yday / 366)
    years: float = max(0.25, end_year - cfg["start_year"])
    rows: int = int(n_stocks * 243 * years * 0.8)                       # 0.8：上市不满 60 天、ST、成交额太小、停牌
    n_feat: int = F.count_of(cfg["factor_sets"])
    # 内存按真实数据实测标定（2026-09-25）：全历史（2019 年至今）每百万行，只读范围内的股票和列约 0.45GB；
    # 第一次整理全历史字段表时峰值约 1.6GB / 百万行（全市场）；因子表 × 4（计算时的临时副本 + 训练矩阵 + 模型分箱，偏保守）
    total_m: float = max(bc["total"], n_stocks) * 243 * 7.5 / 1e6
    base_gb: float = 0.45 * n_stocks * 243 * 7.5 / 1e6 + 0.3
    feat_gb: float = rows * n_feat * 4 * 4.0 / 1e9
    alpha_gb: float = (CHUNK_CODES * 243 * 7.5 * 158 * 8 * 3 / 1e9) if "alpha158" in cfg["factor_sets"] else 0.0
    if "alpha101" in cfg["factor_sets"]:
        alpha_gb = max(alpha_gb, n_stocks * 380 * 80 * 8 * 3 / 1e9)
    build_gb: float = 0.0 if _base_cached() else 1.6 * total_m
    mem_gb: float = round(max(build_gb, base_gb + feat_gb + alpha_gb), 1)
    spec = models.get(cfg["model"])
    preset: dict = O.PRESETS[cfg["preset"]]
    n_folds: int = max(1, int(math.ceil((years - 2) * 12 / preset["step_months"])))
    train_rows: float = min(rows * 0.6, spec.max_rows(cfg["preset"]) or rows)
    # 本机实测（2026-09-25）：LightGBM 100 万行 × 100 个因子，每轮约 0.0275 秒；其他模型按相对速度折算
    fit_min: float = (n_folds + 1) * (train_rows / 1e6) * (n_feat / 100) * ROUNDS_TYPICAL[cfg["preset"]] * 0.0275 / 60 * spec.speed
    since_2019: float = max(0.25, date.today().year + date.today().timetuple().tm_yday / 366 - 2019)
    frame_m: float = n_stocks * 243 * min(years + FEATURE_LOOKBACK_DAYS / 365, since_2019) / 1e6      # 含回看期的行数
    feat_min: float = frame_m * F.minutes_per_million(cfg["factor_sets"])
    base_min: float = 0.5 if _base_cached() else 3.0
    minutes: float = round(max(1.0, base_min + fit_min + feat_min), 0)
    cap: float = float(settings_mod.load().lab.max_mem_gb)
    avail: float | None = None
    try:
        import psutil
        avail = psutil.virtual_memory().available / 1e9
    except Exception:  # noqa: BLE001
        avail = None
    blocked, reason = False, ""
    if mem_gb > cap:
        blocked, reason = True, f"预计要用约 {mem_gb:.0f} GB 内存，超过了设置的上限 {cap:.0f} GB：请缩小股票范围、推后起始年份，或少选几个因子组（Alpha 因子最占内存）"
    elif avail is not None and mem_gb > avail * 0.9:
        blocked, reason = True, f"预计要用约 {mem_gb:.0f} GB 内存，电脑现在只剩约 {avail:.0f} GB 可用：请关掉其他程序，或缩小范围"
    ok, why = spec.available()
    if not ok:
        blocked, reason = True, why
    return {"stocks": n_stocks, "rows": rows, "features": n_feat, "mem_gb": mem_gb, "mem_cap_gb": cap,
            "mem_avail_gb": round(avail, 1) if avail is not None else None, "folds": n_folds, "minutes": minutes,
            "blocked": blocked, "reason": reason, "warning": warn,
            "note": "粗略估计（按本机实测速度算）：全历史数据还没整理过时，第一次要多花约 3 分钟；电脑越忙越慢。训练在后台运行，可以关掉页面。"}
