"""
强势股波段（kind="swing"）：候选池 → 特征 + "可实现收益"标签 → LightGBM 回归滚动训练 → 样本外逐笔交易评价 → 今日决策。

- 候选池（T 日收盘）：涨幅 ≥5% 但收盘没涨停、非无涨跌幅限制期、非 ST（含名称带"退"）、非北交所、收盘价 ≥2 元、有成交
  （features.candidate_mask("swing")）。训练用主板+创业板+科创板，决策时按设置的板块（默认只做主板）；
- 特征：与首板模型相同的五组特征（features.feature_columns("swing")），不用任何 T+1 信息；
- 标签 net：T+1 开盘买，第 2 根K线起首个收盘没涨停的那天按收盘价卖，第 5 根强制卖（跌停顺延，最多到第 7 根），扣全部费用
  （labels.swing_labels）；训练只用买得进（fill）且卖出日 ≤ 训练截止日的样本（净化），标签 clip ±15%；
- 决策：每天按预测净收益从高到低取 top_n（默认 5）且 ≥ threshold（默认 1%），没有达标的 → 今天不操作；
- 训练：每一折用 3 个随机种子（model.REG_SEEDS）各训练一次、预测取平均，每个至少 50 轮（model.REG_MIN_ROUNDS）——
  只为减小重训波动，不按结果调参；最终模型（全部数据、固定轮数 = 各折树数量中位数 × 1.1，至少 50）用单个种子；
- 样本外逐笔评价（trade_oos）：每天 r_d = 当天成交的 net 之和 / N，5 份资金错开轮动复利；按日收益 t 值（daily_t）
  与 Newey-West t 值（nw_t，滞后 5 天，持有最多 5 天导致相邻几天收益相关，更保守）；
  同一只股票上一笔还没卖出时不再买（drop_held，held_skipped = 跳过个数，和账户回测"已持有跳过"一致）；
  选择期（2022-01 ~ 2025-06）/ 留出期（2025-07 起）分开；同池随机挑选基准（random：每天都挑 n 个；
  random_matched：只在策略出信号的日子挑同样多个；各 20 次平均，同样去掉仍持有的）。
  注意：这里每笔按比例收费、不受资金限制；按用户账户（最低佣金 5 元、100 股一手）的结果见 backtest.run_backtest，通常更低。
  评价不设开盘涨幅上限（标签就是这样算的），所以设置里波段的 max_gap_pct 默认 30%（等于不设上限）。
"""
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from . import features, labels, model, stats


KIND: str = "swing"
LABEL: str = "net"
SELECTION_START: date = date(2022, 1, 1)
HOLDOUT_START: date = date(2025, 7, 1)
EVAL_TOP_N: int = 5
EVAL_THRESHOLD: float = 0.01
EVAL_BOARDS: tuple[str, ...] = ("main",)
SLEEVES: int = 5                    # 最长持有 5 个交易日 → 资金分 5 份错开
RANDOM_SEEDS: int = 20
ANNUAL_DAYS: int = 244
N_CHUNKS: int = 4
OOS_KEEP: list[str] = [LABEL, "y", "fill", "status", "label_end", "hold_days", *features.DISPLAY_COLUMNS]

Progress = Callable[[float, str], None] | None


def _report(progress: Progress, frac: float, msg: str) -> None:
    if progress:
        progress(min(max(frac, 0.0), 1.0), msg)


def _sub(progress: Progress, lo: float, hi: float) -> Progress:
    if progress is None:
        return None
    return lambda f, m: progress(lo + (hi - lo) * min(max(f, 0.0), 1.0), m)


# ---------------------------------------------------------------- 数据集

def build_dataset(panel_lim: pl.DataFrame, universe: pl.DataFrame, sentiment: pl.DataFrame, start: date | None,
                  delisted: set[str] | None = None, progress: Progress = None, n_chunks: int = N_CHUNKS,
                  fund: pl.DataFrame | None = None, lhb: pl.DataFrame | None = None,
                  zt_pool: pl.DataFrame | None = None) -> pl.DataFrame:
    """波段候选的特征 + 标签（见 labels.swing_labels）。按股票分块算特征，峰值内存约为全量的一半以下"""
    feat: pl.DataFrame = features.build_features_chunked(
        panel_lim, universe, sentiment, KIND, start=start, n_chunks=n_chunks, fund=fund, lhb=lhb, zt_pool=zt_pool,
        progress=_sub(progress, 0.0, 0.8))
    _report(progress, 0.85, f"候选 {feat.height:,} 条，正在按真实成交规则计算每条的可实现收益…")
    ds: pl.DataFrame = labels.swing_labels(feat, panel_lim, delisted=delisted)
    _report(progress, 1.0, f"样本 {ds.height:,} 条（买得进 {int(ds['fill'].sum()):,} 条）")
    return ds


# ---------------------------------------------------------------- 逐笔交易评价

def select(df: pl.DataFrame, n: int, thr: float | None, col: str = "pred") -> pl.DataFrame:
    """收盘后决策：在当天全部候选中（不知道明天能不能买进）按 col 从高到低取前 n 个且 ≥ thr"""
    d: pl.DataFrame = df if thr is None else df.filter(pl.col(col) >= thr)
    d = d.with_columns(pl.col(col).rank("ordinal", descending=True).over("date").alias("_r"))
    return d.filter(pl.col("_r") <= n).drop("_r")


def drop_held(picks: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """同一只股票更早的一笔还没卖出时不再选它（账户里已经持有，和 backtest 的 skipped_held 一致）。
    picks：date, code[, fill, label_end]。上一笔买进了（fill）且卖出日 label_end > 本次信号日（或到数据末尾还没卖出，
    label_end 为空）→ 跳过；卖出日 ≤ 信号日（当天收盘已卖出）可以再选。返回 (保留的行, 跳过的个数)"""
    if picks.is_empty() or "code" not in picks.columns:
        return picks, 0
    df: pl.DataFrame = picks.with_row_index("_i").sort(["date", "_i"])
    n: int = df.height
    dates: list = df["date"].to_list()
    codes: list = df["code"].to_list()
    fills: list = df["fill"].to_list() if "fill" in df.columns else [True] * n
    ends: list = df["label_end"].to_list() if "label_end" in df.columns else [None] * n
    held_until: dict[str, date | None] = {}           # 代码 → 卖出日（None = 一直没卖出）
    keep: list[bool] = []
    for i in range(n):
        code, day = codes[i], dates[i]
        if code in held_until:
            until: date | None = held_until[code]
            if until is None or until > day:
                keep.append(False)
                continue
            del held_until[code]
        keep.append(True)
        if fills[i]:
            held_until[code] = ends[i]
    skipped: int = keep.count(False)
    if not skipped:
        return picks, 0
    return df.filter(pl.Series(keep)).sort("_i").drop("_i"), skipped


def trade_metrics(picks: pl.DataFrame, all_days: list[date], n: int, sleeves: int = SLEEVES) -> dict:
    """picks：选中的行（date, fill, net）。每天组合收益 r_d = 当天成交的 net 之和 / n（没成交的份额空仓），
    资金分 sleeves 份错开轮动复利；daily_t = 日均 r_d / 标准差 × √天数，nw_t = Newey-West（滞后 5 天）t 值。
    买不进/还没卖出的不算成交。"""
    tr: pl.DataFrame = picks.filter(pl.col("fill") & pl.col(LABEL).is_not_null())
    days: pl.DataFrame = pl.DataFrame({"date": pl.Series(sorted(set(all_days)), dtype=pl.Date)})
    daily: pl.DataFrame = days.join(tr.group_by("date").agg((pl.col(LABEL).sum() / n).alias("r")), on="date",
                                    how="left").with_columns(pl.col("r").fill_null(0.0)).sort("date")
    r: np.ndarray = daily["r"].to_numpy()
    net: np.ndarray = tr[LABEL].to_numpy()
    nt: int = len(net)
    out: dict[str, Any] = {
        "n": nt, "signals": picks.height, "days": len(r),
        "fill_rate": round(nt / picks.height, 4) if picks.height else None,
        "trades_per_day": round(nt / max(len(r), 1), 3),
        "mean": round(float(net.mean()), 5) if nt else None,
        "median": round(float(np.median(net)), 5) if nt else None,
        "win_rate": round(float((net > 0).mean()), 4) if nt else None,
        "daily_mean": round(float(r.mean()), 6) if len(r) else None,
        "daily_t": None, "nw_t": stats.rounded(stats.nw_t(r)) if len(r) else None,
        "cagr": None, "max_drawdown": None, "total_return": None, "by_year": [],
    }
    if not len(r):
        return out
    k: int = max(1, sleeves)
    parts: np.ndarray = np.ones(k)
    curve: list[float] = [1.0]
    for i, x in enumerate(r):
        parts[i % k] *= 1 + x
        curve.append(float(parts.mean()))
    c: np.ndarray = np.array(curve)
    peak: np.ndarray = np.maximum.accumulate(c)
    years: float = max(len(r) / ANNUAL_DAYS, 1e-9)
    std: float = float(r.std(ddof=1)) if len(r) > 2 else 0.0
    out.update(
        daily_t=round(float(r.mean() / std * np.sqrt(len(r))), 3) if std > 0 else None,
        total_return=round(float(c[-1] - 1), 4),
        cagr=round(float(c[-1] ** (1 / years) - 1), 4) if c[-1] > 0 else -1.0,
        max_drawdown=round(float((c / peak - 1).min()), 4),
    )
    yr: pl.DataFrame = daily.with_columns(pl.col("date").dt.year().alias("y"))
    ty: pl.DataFrame = tr.with_columns(pl.col("date").dt.year().alias("y"))
    for y in sorted(set(yr["y"].to_list())):
        ry: np.ndarray = yr.filter(pl.col("y") == y)["r"].to_numpy()
        ny: pl.Series = ty.filter(pl.col("y") == y)[LABEL]
        out["by_year"].append({
            "year": int(y), "n": ny.len(), "mean": round(float(ny.mean()), 5) if ny.len() else None,
            "win_rate": round(float((ny > 0).mean()), 4) if ny.len() else None,
            "return": round(float(np.prod(1 + ry / k) - 1), 4),
        })
    return out


RANDOM_KEYS: list[str] = ["n", "fill_rate", "mean", "median", "win_rate", "daily_t", "nw_t", "cagr", "max_drawdown",
                           "total_return", "daily_mean", "held_skipped"]


def _metrics_dedup(sel: pl.DataFrame, all_days: list[date], n: int) -> dict:
    kept, held = drop_held(sel)
    m: dict = trade_metrics(kept, all_days, n)
    m["held_skipped"] = held
    return m


def _mean_of(res: list[dict], seeds: int) -> dict:
    out: dict[str, Any] = {}
    for k in RANDOM_KEYS:
        vals: list[float] = [float(m[k]) for m in res if m.get(k) is not None]
        out[k] = round(float(np.mean(vals)), 5) if vals else None
    out["seeds"] = seeds
    return out


def random_baseline(df: pl.DataFrame, all_days: list[date], n: int, seeds: int = RANDOM_SEEDS) -> dict:
    """同一候选池每天都随机挑 n 个（不设门槛，包括策略"不操作"的日子），同样的规则，seeds 次平均。
    和策略比，差值里既有"挑股票"也有"挑日子"（择时）的作用，见 matched_baseline"""
    if df.is_empty():
        return {k: None for k in RANDOM_KEYS}
    base: pl.DataFrame = df.sort(["date", "code"])
    res: list[dict] = []
    for s in range(seeds):
        rnd: np.ndarray = np.random.default_rng(1000 + s).random(base.height)
        res.append(_metrics_dedup(select(base.with_columns(pl.Series("_u", rnd)), n, None, "_u"), all_days, n))
    return _mean_of(res, seeds)


def matched_baseline(df: pl.DataFrame, picks: pl.DataFrame, all_days: list[date], n: int,
                     seeds: int = RANDOM_SEEDS) -> dict:
    """同日同数量随机基准：只在策略出信号的日子，从当天同一候选池里随机挑和策略同样多只，同样的规则，seeds 次平均。
    策略减去它 = 纯"挑股票"的本事；它减去 random_baseline = "挑日子"（择时）的作用"""
    if df.is_empty() or picks.is_empty():
        return {k: None for k in RANDOM_KEYS} | {"seeds": seeds}
    want: pl.DataFrame = picks.group_by("date").agg(pl.len().alias("_want"))
    base: pl.DataFrame = df.join(want, on="date", how="inner").sort(["date", "code"])
    res: list[dict] = []
    for s in range(seeds):
        rnd: np.ndarray = np.random.default_rng(1000 + s).random(base.height)
        sel: pl.DataFrame = (base.with_columns(pl.Series("_u", rnd))
                             .with_columns(pl.col("_u").rank("ordinal", descending=True).over("date").alias("_r"))
                             .filter(pl.col("_r") <= pl.col("_want")).drop("_r", "_want"))
        res.append(_metrics_dedup(sel, all_days, n))
    return _mean_of(res, seeds)


def _period(df: pl.DataFrame, lo: date | None, hi: date | None) -> pl.DataFrame:
    cond: pl.Expr = pl.lit(True)
    if lo is not None:
        cond = cond & (pl.col("date") >= lo)
    if hi is not None:
        cond = cond & (pl.col("date") < hi)
    return df.filter(cond)


def trade_oos(oos: pl.DataFrame, top_n: int = EVAL_TOP_N, threshold: float = EVAL_THRESHOLD,
              boards: tuple[str, ...] | list[str] | None = EVAL_BOARDS, seeds: int = RANDOM_SEEDS) -> dict:
    """样本外逐笔交易评价（按默认决策规则），含选择期/留出期与同池随机基准。
    仍持有的股票再次入选时跳过（held_skipped）；同日同数量随机按去重前的每日个数挑，再同样去重"""
    d: pl.DataFrame = oos if not boards else oos.filter(pl.col("board").is_in(list(boards)))
    # 最后几天的信号还没卖出（标签未揭晓）：评价只到最后一个有已完成交易的信号日
    done: pl.DataFrame = d.filter(pl.col(LABEL).is_not_null())
    last: date | None = done["date"].max() if done.height else None
    if last is not None:
        d = d.filter(pl.col("date") <= last)
    out: dict[str, Any] = {"config": {"top_n": top_n, "threshold": threshold, "boards": list(boards or []),
                                      "exit_rule": labels.SWING_EXIT_RULE, "sleeves": SLEEVES,
                                      "holdout_start": HOLDOUT_START.isoformat()}}
    for name, lo, hi in (("all", None, None), ("selection", SELECTION_START, HOLDOUT_START),
                         ("holdout", HOLDOUT_START, None)):
        x: pl.DataFrame = _period(d, lo, hi)
        days: list[date] = x["date"].unique().to_list()
        picks: pl.DataFrame = select(x, top_n, threshold)
        m: dict = _metrics_dedup(picks, days, top_n)
        m["start"] = min(days).isoformat() if days else None
        m["end"] = max(days).isoformat() if days else None
        m["random"] = random_baseline(x, days, top_n, seeds)
        m["random_matched"] = matched_baseline(x, picks, days, top_n, seeds)
        if name == "all":
            out.update(m)
        else:
            out[name] = m
    return out


def regression_calibration(oos: pl.DataFrame, col: str = "pred") -> list[dict]:
    """按预测净收益分桶：平均预测 vs 实际平均净收益（只用买得进且已卖出的行）"""
    edges: list[float] = [-1.0, -0.01, 0.0, 0.01, 0.02, 0.03, 0.05, 1.0]
    lab: pl.DataFrame = oos.filter(pl.col(LABEL).is_not_null())
    rows: list[dict] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=False):
        part: pl.DataFrame = lab.filter((pl.col(col) >= lo) & (pl.col(col) < hi))
        if part.is_empty():
            continue
        name: str = (f"<{hi * 100:g}%" if lo <= -1 else f"≥{lo * 100:g}%" if hi >= 1 else f"{lo * 100:g}~{hi * 100:g}%")
        rows.append({"bucket": name, "pred": round(float(part[col].mean()), 5),
                     "actual": round(float(part[LABEL].mean()), 5), "n": part.height})
    return rows


# ---------------------------------------------------------------- 训练

def train_model(panel_lim: pl.DataFrame, universe: pl.DataFrame, sentiment: pl.DataFrame, start: date,
                progress: Progress = None, first_test_year: int = 2022, delisted: set[str] | None = None,
                n_chunks: int = N_CHUNKS, fund: pl.DataFrame | None = None, lhb: pl.DataFrame | None = None,
                zt_pool: pl.DataFrame | None = None) -> tuple[Any, dict, pl.DataFrame]:
    """构建数据集 → 滚动训练（样本外预测）→ 最终模型 → 评价。返回 (booster, meta 主体, 样本外预测)"""
    t0: float = time.time()
    if delisted is None:
        delisted = set(universe.filter(pl.col("status") == 0)["code"].to_list()) \
            if universe.height and "status" in universe.columns else set()
    ds: pl.DataFrame = build_dataset(panel_lim, universe, sentiment, start, delisted, _sub(progress, 0.0, 0.3),
                                     n_chunks, fund, lhb, zt_pool)
    if ds.is_empty():
        raise RuntimeError("没有符合条件的强势股候选，无法训练")
    cols: list[str] = features.feature_columns(KIND)
    folds: list[model.Fold] = model.make_folds(ds["date"].unique().to_list(), first_test_year)
    if not folds:
        raise RuntimeError(f"数据时间太短，无法做滚动训练（需要 {first_test_year} 年以前的数据）")
    oos, fold_metrics = model.train_walk_forward_reg(
        ds, cols, folds, progress=_sub(progress, 0.3, 0.85), label=LABEL, keep_cols=OOS_KEEP,
        train_filter=pl.col("fill"))
    if oos.is_empty():
        raise RuntimeError("每一轮的训练样本都太少，无法训练波段模型")
    _report(progress, 0.86, "正在用全部数据训练最终模型…")
    iters: list[int] = [int(x) for m in fold_metrics if "best_iter" in m
                        for x in (m.get("best_iters") or [m["best_iter"]])]
    rounds: int = max(model.REG_MIN_ROUNDS, int(np.median(iters) * 1.1)) if iters else model.FALLBACK_ROUNDS
    train_rows: pl.DataFrame = ds.filter(pl.col("fill") & pl.col(LABEL).is_not_null())
    booster, _ = model.fit_regression(train_rows, cols, LABEL, num_boost_round=rounds)
    imp, top = model.importance(booster, cols)
    _report(progress, 0.9, "正在评价样本外逐笔交易（含同池随机基准）…")
    main_oos: pl.DataFrame = oos.filter(pl.col("board").is_in(list(EVAL_BOARDS)))
    overall: dict = model.evaluate_reg(main_oos, LABEL)
    overall_all: dict = model.evaluate_reg(oos, LABEL)
    trade: dict = trade_oos(oos)
    meta: dict[str, Any] = {
        "kind": KIND,
        "objective": "regression",
        "label_desc": "T+1 开盘买，第2个交易日起首个收盘没涨停的交易日收盘卖（第5天强制，跌停顺延），扣全部费用后的净收益",
        "data_start": ds["date"].min().isoformat(),
        "n_samples": train_rows.height,
        "n_pool": ds.height,
        "fill_rate": round(float(ds["fill"].mean()), 4) if ds.height else None,
        "n_positive": int((train_rows[LABEL] > 0).sum()),
        "base_rate": overall.get("base_win"),
        "base_return": overall.get("base_mean"),
        "train_base_rate": float((train_rows[LABEL] > 0).mean()) if train_rows.height else None,
        "train_base_return": float(train_rows[LABEL].mean()) if train_rows.height else None,
        "neg_sample": None,
        "logit_offset": 0.0,
        "n_rounds": booster.current_iteration(),
        "seeds": list(model.REG_SEEDS),
        "min_rounds": model.REG_MIN_ROUNDS,
        "feature_cols": cols,
        "feature_labels": {c: features.FEATURE_LABELS.get(c, c) for c in cols},
        "group_labels": features.GROUP_LABELS,
        "folds": fold_metrics,
        "oos": overall,
        "oos_all_boards": overall_all,
        "oos_start": oos["date"].min().isoformat() if oos.height else None,
        "oos_end": oos["date"].max().isoformat() if oos.height else None,
        "topn_hit": [{"n": k, "hit_rate": overall.get(f"top{k}_win"), "avg_return": overall.get(f"top{k}_mean")}
                     for k in model.REG_TOP_KS],
        "calibration": regression_calibration(main_oos),
        "importance": imp,
        "top_features": top,
        "trade_oos": trade,
        "eval_boards": list(EVAL_BOARDS),
        "train_seconds": round(time.time() - t0, 1),
    }
    return booster, meta, oos


# ---------------------------------------------------------------- 今日决策

def _t_low(part: dict) -> float | None:
    """一段评价里 daily_t 与 nw_t 较低（更保守）的那个"""
    vals: list[float] = [float(v) for v in (part.get("daily_t"), part.get("nw_t")) if v is not None]
    return min(vals) if vals else None


def record_text(meta: dict | None) -> str:
    """模型检验成绩的一句话（数字取自 meta.trade_oos，重新训练后自动更新）"""
    t: dict = (meta or {}).get("trade_oos") or {}
    if t.get("mean") is None:
        return "历史成绩见预测页上方（模型还没有样本外交易记录）"
    hold: dict = t.get("holdout") or {}
    text: str = f"模型检验（按比例收费、不受资金限制）历史样本外 {t.get('n')} 笔平均每笔 {t['mean'] * 100:+.2f}%"
    if hold.get("mean") is not None:
        tl: float | None = _t_low(hold)
        text += f"，其中留出期 {hold.get('n')} 笔 {hold['mean'] * 100:+.2f}%"
        extra: list[str] = []
        mat: Any = (hold.get("random_matched") or {}).get("mean")
        if mat is not None:
            extra.append(f"同一天随便挑同样多只 {float(mat) * 100:+.2f}%"
                         f"{'，模型并不比随便挑好' if hold['mean'] <= float(mat) else ''}")
        if tl is not None:
            extra.append(f"t 值 {tl:.1f}，{'还不够可信' if tl < 2 else '超过 2，但仍只是历史回放'}")
        if extra:
            text += "（" + "；".join(extra) + "）"
    return text + "；按你的本金（每笔佣金最低 5 元、一手 100 股）回测通常更低，以预测页上方的数字为准"


def gate_and_plan(picks: list[dict], ps: dict, ts: dict, signal_date: str | None, ok: bool = True,
                  meta: dict | None = None) -> tuple[dict, dict]:
    """(闸门 {"trade": bool, "reason": str, "n_picks": int, "threshold": float}, 计划 {"buy","sell","position"})。
    meta：模型 meta（position 里的历史成绩取自 meta.trade_oos；没有时不写具体数字）"""
    thr: float = float(ps.get("threshold") or 0.0)
    top_n: int = int(ps.get("top_n", EVAL_TOP_N))
    pos_pct: float = float(ts.get("position_pct", 0.04))
    max_pos: int = int(ts.get("max_positions", 25))
    rule: str = str(ts.get("exit_rule", labels.SWING_EXIT_RULE))
    n: int = len(picks)
    if not ok:
        gate: dict = {"trade": False, "reason": "模型或数据还没准备好，今天不操作", "n_picks": 0, "threshold": thr}
    elif n:
        names: str = "、".join(f"{p.get('name') or p['code']}" for p in picks[:top_n])
        gate = {"trade": True, "n_picks": n, "threshold": thr,
                "reason": f"今天有 {n} 只股票预期净收益达到 {thr * 100:g}%：{names}"}
    else:
        gate = {"trade": False, "n_picks": 0, "threshold": thr,
                "reason": f"今天没有股票的预期净收益达到 {thr * 100:g}%，今天不操作（空仓也是一种操作）"}
    day: str = f"（{signal_date} 收盘后的信号）" if signal_date else ""
    gap: float | None = ts.get("max_gap_pct")
    gap_text: str = (f"开盘涨幅超过 {float(gap):g}% 的也放弃，" if gap is not None and float(gap) < 20 else "")
    if gate["trade"]:
        buy: str = (f"下一个交易日开盘按开盘价买入标记「选中」的 {n} 只{day}；开盘就涨停（买不进）的直接放弃，"
                    f"{gap_text}不追高；手里还拿着的同一只股票不重复买。")
    else:
        buy = f"下一个交易日不买入新的股票{day}。"
    sell_rules: dict[str, str] = {
        "until_break_close": "买入后第二个交易日起，每天收盘前看：当天没有涨停就按收盘价卖出；一直涨停最多拿到第5个交易日收盘卖出；"
                             "决定卖出那天遇到跌停卖不出，就在下一个交易日收盘卖出（不管那天涨不涨停，仍跌停再顺延，最晚第7个交易日）。",
        "next_open": "买入后的下一个交易日开盘卖出（开盘一字跌停卖不出就顺延）。",
        "next_close": "买入后的下一个交易日收盘卖出（收盘跌停卖不出就顺延）。",
        "until_break": "涨停就继续拿，某天收盘没涨停，下一个交易日开盘卖出（最多持有10天）。",
    }
    sell: str = sell_rules.get(rule, sell_rules["until_break_close"])
    stop: float = float(ts.get("stop_loss_pct") or 0.0)
    if stop > 0:
        sell += f"另外跌到买入价的 -{stop * 100:g}% 就止损。"
    position: str = (f"每只用总资金的 {pos_pct * 100:g}%，最多同时持有 {max_pos} 只。这是模拟跟踪中的策略，"
                     "目前没有证据证明它能赚钱（成绩看预测页上方“按你的本金回测”的最近一段），不建议直接用真钱。")
    return gate, {"buy": buy, "sell": sell, "position": position}
