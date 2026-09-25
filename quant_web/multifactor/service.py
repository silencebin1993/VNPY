"""
量化选股（多因子 + LightGBM）服务：每日打分、每周调仓名单、前向跟踪、完整回测报告。

- 每日（收盘、日线更新后由投资助手流水线调用，也可以在页面上点）：补当天估值 → 读主板面板 → 算因子 →
  用和回测完全相同的代码给最新一天打分（model.scores），存 today.json；如果今天是本周最后一个交易日，
  把"下周第一个交易日开盘要持有的组合"记进前向跟踪（track.json，记下后不再改动，成绩事后才知道）。
- 回测报告（后台任务，约 10 分钟）：2020 年起滚动回测，每期只用当时能拿到的数据；和同池随机（全池等权 + 蒙特卡洛
  随机组合）、中证1000/500、沪深300 比；按资金规模加冲击成本做容量表；存 report.json。
研究依据与结论见 ARCHITECTURE.md §9。文件都在 workspace/multifactor/。
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .. import config
from . import evaluate as E
from . import factors as F
from . import model as M
from . import panel as P

log = logging.getLogger("quant_web.multifactor")
DIR = config.WORKSPACE.joinpath("multifactor")
TODAY_FILE = DIR.joinpath("today.json")
REPORT_FILE = DIR.joinpath("report.json")
TRACK_FILE = DIR.joinpath("track.json")

Progress = Callable[[float, str], None] | None

# 生产配置（研究结论，见 ARCHITECTURE.md §9）
SPEC = M.Spec(method="LGB", freq="W")
TOP_N: int = 50
KEEP_RANK: int = 150
INDUSTRY_CAP: int = 8
MIN_AMOUNT: float = 2e7
MAX_PARTICIPATION: float = 0.05        # 单只股票的下单金额不超过它 20 日日均成交额的 5%
CAPITAL_LEVELS: list[float] = [1e6, 5e6, 2e7, 5e7, 1e8]
DEFAULT_CAPITAL: float = 5e6
OOS_START: str = "2022-01-01"          # 模型第一次训练用 2019-2021 的数据，之后每 4 周滚动重训
SEGMENTS: dict[str, tuple[str, str]] = {
    "2022-2024": ("2022-01-01", "2024-12-31"), "2025 至今": ("2025-01-01", "2099-12-31"), "全部样本外": (OOS_START, "2099-12-31"),
}
GROUP_ORDER: list[str] = ["reversal", "activity", "risk", "sentiment", "value", "quality", "surprise", "size", "trend"]
INDEXES: dict[str, str] = {"sh000852": "中证1000", "sh000905": "中证500", "sh000300": "沪深300"}


def _say(progress: Progress, f: float, msg: str) -> None:
    if progress:
        progress(f, msg)
    log.info(msg)


def _json_default(o):
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.isoformat()[:10]
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _clean(obj):
    """NaN/inf → None（JSON 不认 NaN）"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (float, np.floating)) and not np.isfinite(obj):
        return None
    return obj


def _write(path, obj) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_clean(obj), ensure_ascii=False, default=_json_default), encoding="utf-8")
    tmp.replace(path)


def _read(path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        return None


def load_today() -> dict | None:
    return _read(TODAY_FILE)


def load_report() -> dict | None:
    return _read(REPORT_FILE)


def load_track() -> dict:
    return _read(TRACK_FILE) or {"started": None, "records": []}


def profile_capital() -> float:
    try:
        from .. import settings as settings_mod
        return float(settings_mod.load().profile.capital)
    except Exception:  # noqa: BLE001
        return DEFAULT_CAPITAL


# ---------------------------------------------------------------- 公共：面板、因子、股票池

def universe_mask(p: P.Panel, ctx: F.Ctx, min_amount: float = MIN_AMOUNT) -> pd.DataFrame:
    """可买股票池：主板、非ST、上市满一年、近20日日均成交额达标、股价 ≥ 2 元、
    不是"亏损且营收 < 3 亿"（新国九条 *ST 线）"""
    base = E.eligible(p, E.Universe(min_amount=min_amount))
    fund = ctx.fund
    risk = ((fund["net_profit_ttm"] < 0) & (fund["revenue_ttm"] < 3e8)).fillna(False)
    return base & (p.px["close"] >= 2.0) & ~risk


def capacity_mask(p: P.Panel, mask: pd.DataFrame, capital: float) -> pd.DataFrame:
    """按资金规模再去掉"下单金额会超过日均成交额 5%"的股票"""
    return mask & (E.participation(p, capital, TOP_N) <= MAX_PARTICIPATION)


def prepare(progress: Progress = None) -> tuple[P.Panel, F.Ctx, dict, pd.DataFrame]:
    _say(progress, 0.05, "量化选股：读取主板日线、财报和估值……")
    p = P.load_panel()
    ctx = F.Ctx(p)
    _say(progress, 0.15, f"量化选股：计算 {len(SPEC.keys)} 个因子……")
    fac = F.compute(ctx, SPEC.keys)
    mask = universe_mask(p, ctx)
    return p, ctx, fac, mask


def calendar_next(day: date) -> tuple[date | None, bool]:
    """(下一个交易日, 今天是不是本周最后一个交易日)"""
    cal: list[date] = []
    try:
        from ..market import realtime
        cal = [d for d in realtime.trade_calendar() if d > day]
    except Exception:  # noqa: BLE001  取不到日历就按工作日推
        cal = []
    if not cal:
        cal = [(pd.Timestamp(day) + pd.offsets.BDay(1)).date()]
    nxt = cal[0]
    return nxt, nxt.isocalendar()[:2] != day.isocalendar()[:2]


def _pct_rank_by_group(fac: dict[str, pd.DataFrame], day: pd.Timestamp, msk_row: pd.Series, keys: list[str]) -> pd.DataFrame:
    """每只股票在各类因子上的平均分位（0~1，1 = 这一类里最符合"好股票"方向）"""
    d = M.directions()
    groups = {f.key: f.group for f in F.FACTORS}
    pct = pd.DataFrame({k: (fac[k].loc[day].where(msk_row) * d[k]).rank(pct=True) for k in keys})
    out = {}
    for grp in GROUP_ORDER:
        ks = [k for k in keys if groups[k] == grp]
        if ks:
            out[grp] = pct[ks].mean(axis=1)
    return pd.DataFrame(out)


def target_portfolio(score: pd.Series, prev: list[str], industry: pd.Series) -> list[str]:
    """和回测同样的规则：上期持仓只要排名还在 KEEP_RANK 以内就留着；空位按分数从高到低补，单行业最多 INDUSTRY_CAP 只"""
    rank = {c: i for i, c in enumerate(score.index)}
    keep = [c for c in prev if rank.get(c, 10 ** 9) < KEEP_RANK]
    ind_n: dict[str, int] = {}
    for c in keep:
        k = industry.get(c, "未知")
        ind_n[k] = ind_n.get(k, 0) + 1
    out = list(keep)
    for c in score.index:
        if len(out) >= TOP_N:
            break
        if c in out:
            continue
        k = industry.get(c, "未知")
        if ind_n.get(k, 0) >= INDUSTRY_CAP:
            continue
        out.append(c)
        ind_n[k] = ind_n.get(k, 0) + 1
    return out


# ---------------------------------------------------------------- 每日打分

def previous_holdings(records: list[dict], day: str) -> list[str]:
    """上一期组合 = day 之前最后一次记下的组合（调仓日重跑时，track 里已经有当天自己的记录，不能拿它当上一期）"""
    return next((r["holdings"] for r in reversed(records) if r["date"] < day), [])


def run_daily(progress: Progress = None, refresh_valuation: bool = True) -> dict:
    t0 = time.time()
    from ..market import history
    last = history.last_date()
    if last is None:
        raise RuntimeError("还没有日线数据，先在数据中心下载")
    val_note = None
    if refresh_valuation:
        try:
            from . import valuation
            codes = [c for c in pd.read_parquet(config.UNIVERSE_FILE)["code"] if P.main_board(c)]
            val_note = valuation.refresh(last, codes)
        except Exception as e:  # noqa: BLE001  快照失败时估值沿用前几天
            val_note = {"error": str(e)}
    p, ctx, fac, mask = prepare(progress)
    capital = profile_capital()
    cmask = capacity_mask(p, mask, capital)
    spec = SPEC.resolved()
    day = p.dates[-1]
    weekly = E.rebalance_dates(p.dates, spec.freq, start="2019-06-01")
    sd = weekly.append(pd.DatetimeIndex([day])).unique().sort_values()
    _say(progress, 0.4, "量化选股：用最近三年的数据训练模型并给最新一天打分……")
    fwd = E.forward_open_returns(p, sd)
    sc = M.scores(fac, p, mask, sd, spec, lgb_start=str(day.date()), fwd=fwd)
    score_all = sc[spec.method].loc[day].where(mask.loc[day]).dropna().sort_values(ascending=False)
    score = score_all[cmask.loc[day].reindex(score_all.index).fillna(False).values]
    grp_df = _pct_rank_by_group(fac, day, mask.loc[day], spec.keys)

    amount20 = p.px["amount"].rolling(20, min_periods=10).mean().loc[day]
    close = p.px["close"].loc[day]
    chg = p.ret.loc[day]
    fcap = ctx.float_cap.loc[day]
    lu = p.limit_up_close.loc[day]
    n_all = len(score_all)
    rank_all = {c: i + 1 for i, c in enumerate(score_all.index)}
    rows = []
    for rank, (code, s) in enumerate(score.head(300).items(), start=1):
        rows.append({
            "rank": rank, "rank_all": rank_all.get(code), "code": code, "name": p.names.get(code, ""),
            "industry": p.industry.get(code, ""), "score": float(s),
            "pct": float(1 - (rank_all.get(code, n_all) - 1) / max(n_all, 1)),
            "close": float(close.get(code, np.nan)), "chg": float(chg.get(code, np.nan)),
            "amount20": float(amount20.get(code, np.nan)), "float_cap": float(fcap.get(code, np.nan)),
            "limit_up": bool(lu.get(code, False)),
            "groups": {g: (None if pd.isna(v) else round(float(v), 3)) for g, v in grp_df.loc[code].items()},
        })
    track = load_track()
    prev = previous_holdings(track["records"], str(day.date()))
    target = target_portfolio(score, prev, p.industry)
    nxt, week_end = calendar_next(day.date())
    out = {
        "date": str(day.date()), "generated_at": datetime.now(config.CHINA_TZ).strftime("%Y-%m-%d %H:%M"),
        "next_trade_day": str(nxt) if nxt else None, "rebalance_day": bool(week_end),
        "spec": {**asdict(spec), "top_n": TOP_N, "keep_rank": KEEP_RANK, "industry_cap": INDUSTRY_CAP,
                 "min_amount": MIN_AMOUNT, "max_participation": MAX_PARTICIPATION},
        "capital": capital, "universe_n": int(mask.loc[day].sum()), "capacity_n": int(cmask.loc[day].sum()),
        "scored_n": n_all, "rows": rows, "target": target, "prev_target": prev,
        "valuation": val_note, "st_source": p.st_source, "seconds": round(time.time() - t0, 1),
    }
    _write(TODAY_FILE, out)
    if week_end:
        record_track(out)
    _say(progress, 1.0, f"量化选股：完成（{n_all} 只打分，用时 {out['seconds']:.0f} 秒）")
    return {"date": out["date"], "scored": n_all, "rebalance_day": out["rebalance_day"], "seconds": out["seconds"]}


def health(today: dict | None = None) -> dict:
    """下单前自检：日线是不是更新到最新交易日、最新一天下载全不全、名单是不是按最新数据打的分、
    组合里有没有停牌 / 现在是 ST 的股票。items 的 level：ok / warn（要注意）/ bad（今天不要按名单下单）"""
    import polars as pl

    from ..market import history, universe
    from ..trading import calendar as tcal
    today = today if today is not None else load_today()
    items: list[dict] = []

    def add(key: str, level: str, text: str) -> None:
        items.append({"key": key, "level": level, "text": text})

    now = tcal.china_now()
    closed = tcal.is_trading_day(now.date()) and now.hour * 60 + now.minute >= history.CLOSE_MINUTE
    expected: date = now.date() if closed else tcal.prev_trading_day(now.date())
    data_date: date | None = history.last_date()
    if data_date is None:
        add("data", "bad", "本地还没有日线数据：先到“数据中心”下载。")
        return {"ok": False, "items": items, "expected": str(expected), "data_date": None}
    if data_date < expected:
        add("data", "bad", f"日线只更新到 {data_date}，最新一个已收盘的交易日是 {expected}：先点右上角“一键更新”，更新完再看名单。")
    else:
        add("data", "ok", f"日线已更新到最新交易日 {data_date}。")
    prev = tcal.prev_trading_day(data_date)
    bars = history.load_panel(start=prev, end=data_date, columns=["close"])
    bars = bars.filter(pl.col("code").str.starts_with("60") | pl.col("code").str.starts_with("00"))
    n_last = int(bars.filter(pl.col("date") == data_date).height)
    n_prev = int(bars.filter(pl.col("date") == prev).height)
    if n_prev and n_last < 0.97 * n_prev:
        add("coverage", "bad", f"{data_date} 只有 {n_last} 只主板股票有日线（前一个交易日 {n_prev} 只）：可能没下载全，名单可能不准。先点“一键更新”补齐。")
    else:
        add("coverage", "ok", f"{data_date} 主板 {n_last} 只股票有日线，下载完整。")
    if not today:
        add("score", "bad", "还没有量化选股的名单：点“重新打分”。")
        return {"ok": False, "items": items, "expected": str(expected), "data_date": str(data_date)}
    if today["date"] < str(data_date):
        add("score", "bad", f"名单是按 {today['date']} 的数据打的分，日线已经更新到 {data_date}：点“重新打分”（约 1 分钟）再看。")
    else:
        add("score", "ok", f"名单按 {today['date']} 收盘数据打分（{today.get('generated_at', '')} 生成）。")
    if tcal._days() is None:
        add("calendar", "warn", "交易日历暂时取不到：按“周一到周五”估算，节假日前后的调仓日判断可能不准。")
    target: list[str] = list(today.get("target") or [])
    if len(target) != TOP_N:
        add("size", "warn", f"组合只有 {len(target)} 只（应该是 {TOP_N} 只）：可买股票太少，结果会和回测差得比较多。")
    held_today = set(bars.filter(pl.col("date") == data_date)["code"].to_list())
    names = {r["code"]: r.get("name") or "" for r in today.get("rows") or []}
    uni = universe.load_universe()
    cur = dict(zip(uni["code"].to_list(), uni["name"].to_list(), strict=True)) if uni.height else {}
    st_now = [c for c in target if any(k in (cur.get(c) or names.get(c) or "").upper() for k in ("ST", "退"))]
    if st_now:
        add("st", "bad", "现在名称带 ST / 退：" + "、".join(f"{cur.get(c) or names.get(c)}（{c}）" for c in st_now) + "——不要买，按规则换排名下一只。")
    stopped = [c for c in target if c not in held_today] if today["date"] == str(data_date) else []
    if stopped:
        add("halt", "warn", "最新一天没有成交（停牌）：" + "、".join(f"{names.get(c) or c}" for c in stopped) + "——下个交易日可能买不进，买不进就跳过换下一只。")
    return {"ok": not any(i["level"] == "bad" for i in items), "items": items, "expected": str(expected), "data_date": str(data_date)}


def record_track(today: dict) -> None:
    """前向跟踪：每周最后一个交易日记下目标组合（记下后不再修改）"""
    track = load_track()
    recs = [r for r in track["records"] if r["date"] != today["date"]]
    recs.append({"date": today["date"], "next_trade_day": today.get("next_trade_day"), "holdings": today["target"],
                 "capital": today.get("capital"), "recorded_at": today["generated_at"]})
    recs.sort(key=lambda r: r["date"])
    _write(TRACK_FILE, {"started": track.get("started") or today["date"], "records": recs})


def track_performance() -> dict:
    """前向跟踪的真实成绩：记录日的下一个交易日开盘买入、下一条记录的次日开盘卖出（含费用），和同池等权比"""
    track = load_track()
    recs = track["records"]
    if not recs:
        return {"started": track.get("started"), "periods": [], "records": 0}
    p = P.load_panel(start=date.fromisoformat(recs[0]["date"]) - timedelta(days=90))
    ctx = F.Ctx(p)
    mask = universe_mask(p, ctx)
    sd = pd.DatetimeIndex([pd.Timestamp(r["date"]) for r in recs if pd.Timestamp(r["date"]) in p.dates])
    if not len(sd):
        return {"started": track.get("started"), "periods": [], "records": len(recs)}
    hold = {pd.Timestamp(r["date"]): r["holdings"] for r in recs}
    score = pd.DataFrame(np.nan, index=sd, columns=p.codes)
    for d in sd:
        for i, c in enumerate(hold[d]):
            if c in score.columns:
                score.loc[d, c] = len(hold[d]) - i
    capital = float(recs[-1].get("capital") or DEFAULT_CAPITAL)
    bt = E.backtest(p, score, score.notna(), sd, top_n=TOP_N, keep_rank=10 ** 6,
                    slippage=E.impact_slippage(p, capital, TOP_N))
    bench = E.forward_open_returns(p, sd).where(mask.reindex(sd).fillna(False)).mean(axis=1)
    periods = []
    order = list(sd)
    for k, (d, r) in enumerate(bt.period_ret.items()):
        # 一期在"下一条记录的次日开盘"卖出才算结束；最后一条记录永远是进行中（收益按最新收盘估算，每天都会变）
        done = k + 1 < len(order) and p.dates.get_indexer([order[k + 1]])[0] + 1 < len(p.dates)
        periods.append({"date": str(d.date()), "ret": float(r), "bench": float(bench.get(d, np.nan)), "complete": bool(done)})
    return {"started": track.get("started"), "periods": periods, "records": len(recs)}


# ---------------------------------------------------------------- 完整回测报告

def _index_returns(p: P.Panel, sd: pd.DatetimeIndex) -> dict[str, pd.Series]:
    try:
        from ..providers import store
        idx = store.load("index_bars").to_pandas()
    except Exception:  # noqa: BLE001
        return {}
    if idx.empty:
        return {}
    out = {}
    pos = p.dates.get_indexer(sd)
    ent = np.minimum(pos + 1, len(p.dates) - 1)
    ext = np.minimum(np.append(pos[1:] + 1, len(p.dates) - 1), len(p.dates) - 1)
    for code in INDEXES:
        s = idx[idx["index"] == code]
        if s.empty:
            continue
        o = pd.Series(s["open"].values, index=pd.to_datetime(s["date"])).sort_index().reindex(p.dates).ffill()
        out[code] = pd.Series(o.values[ext] / o.values[ent] - 1, index=sd)
    return out


def _seg_stats(bt: E.BacktestResult, idx_ret: dict[str, pd.Series]) -> dict:
    out = {}
    for name, (a, b) in SEGMENTS.items():
        st = bt.stats(a, b, periods_per_year=52)
        r = bt.period_ret.loc[a:b]
        for code, s in idx_ret.items():
            st[f"vs_{code}"] = float((r - s.reindex(r.index)).mean() * 52)
        out[name] = st
    return out


def _yearly(bt: E.BacktestResult, idx_ret: dict[str, pd.Series]) -> list[dict]:
    r = bt.period_ret.loc[OOS_START:]
    b = bt.bench_ret.reindex(r.index)
    rows = []
    for y in sorted(set(r.index.year)):
        ry, by = r[r.index.year == y], b[b.index.year == y]
        row = {"year": int(y), "strategy": float((1 + ry).prod() - 1), "random": float((1 + by).prod() - 1), "weeks": len(ry)}
        for code, s in idx_ret.items():
            row[code] = float((1 + s.reindex(ry.index).fillna(0)).prod() - 1)
        rows.append(row)
    return rows


STAGE_LABELS: dict[str, str] = {"accumulation": "吸筹", "washout": "洗盘", "markup": "拉升", "distribution": "出货",
                                 "decline": "下跌", "unclear": "不明确"}


def stage_check(p: P.Panel, mask: pd.DataFrame, sd: pd.DatetimeIndex, fwd: pd.DataFrame,
                holdings: dict[pd.Timestamp, list[str]]) -> dict | None:
    """样本外每一期：组合里的股票和全部可买股票，按选股当天的"主力阶段"标签分组，看下一期（次日开盘买 → 下期次日开盘卖）
    比同池平均多赚多少。回答"选出来的怎么都是洗盘 / 吸筹 / 下跌、没有能直接跟进的拉升股"。
    主力阶段逐日历史来自选股器的历史表（选股器回测时生成）；没有这张表就跳过。"""
    import polars as pl

    from ..screener import backtest as sbt
    path = next((x for x in (sbt._cache_paths(True)[0], sbt._cache_paths(False)[0]) if x.exists()), None)
    if path is None:
        return None
    oos = [d for d in sd if d >= pd.Timestamp(OOS_START) and d in fwd.index]
    st = (pl.read_parquet(path, columns=["code", "date", "stage"]).filter(pl.col("date").is_in([d.date() for d in oos]))
          .to_pandas())
    if st.empty:
        return None
    st["date"] = pd.to_datetime(st["date"])
    stage = st.pivot(index="date", columns="code", values="stage").reindex(index=oos, columns=fwd.columns)
    msk = mask.reindex(index=oos, columns=fwd.columns).fillna(False).values.astype(bool)
    ret = fwd.reindex(index=oos).values
    lab = stage.fillna("unclear").values
    cols = {c: i for i, c in enumerate(fwd.columns)}
    held = np.zeros_like(msk)
    for i, d in enumerate(oos):
        for c in holdings.get(d) or []:
            j = cols.get(c)
            if j is not None:
                held[i, j] = True
    ok = msk & np.isfinite(ret)
    base = np.nanmean(np.where(ok, ret, np.nan), axis=1, keepdims=True)
    exc = ret - base

    def summarize(sel: np.ndarray) -> list[dict]:
        total = int(sel.sum())
        out = []
        for key, name in STAGE_LABELS.items():
            m = sel & (lab == key)
            n = int(m.sum())
            if n == 0:
                continue
            wk = np.array([exc[i][m[i]].mean() for i in range(len(oos)) if m[i].any()])
            t = float(wk.mean() / (wk.std(ddof=1) / np.sqrt(len(wk)))) if len(wk) > 2 and wk.std(ddof=1) > 0 else None
            out.append({"stage": key, "label": name, "n": n, "share": n / total if total else None,
                        "excess_wk": float(exc[m].mean()), "win": float((exc[m] > 0).mean()), "t": t})
        return sorted(out, key=lambda x: -x["n"])

    sel_h = ok & held
    drop = sel_h & ~np.isin(lab, ["washout", "accumulation", "decline"])
    return {"source": "chips" if "chips" in path.name and "nochips" not in path.name else "nochips", "weeks": len(oos),
            "held": summarize(sel_h), "pool": summarize(ok),
            "held_excess_wk": float(exc[sel_h].mean()) if sel_h.any() else None,
            "drop_excess_wk": float(exc[drop].mean()) if drop.any() else None,
            "drop_share": float(drop.sum() / sel_h.sum()) if sel_h.any() else None}


def expectations(fwd: pd.DataFrame, holdings: dict[pd.Timestamp, list[str]], period_ret: pd.Series) -> dict:
    """给用户的真实预期（样本外）：单只持仓下一期下跌的概率、组合每周亏钱的概率、最差一周 / 最差连续 4 周"""
    vals = []
    for d, codes in holdings.items():
        if d < pd.Timestamp(OOS_START) or d not in fwd.index or not codes:
            continue
        v = fwd.loc[d].reindex(codes).to_numpy(dtype=float)
        vals.append(v[np.isfinite(v)])
    allv = np.concatenate(vals) if vals else np.array([])
    r = period_ret.loc[OOS_START:]
    nav = (1 + r).cumprod()
    four = nav.pct_change(4).dropna()
    return {
        "stock_down": float((allv < 0).mean()) if allv.size else None, "stock_down5": float((allv < -0.05).mean()) if allv.size else None,
        "stock_down10": float((allv < -0.10).mean()) if allv.size else None, "stock_n": int(allv.size),
        "week_down": float((r < 0).mean()) if len(r) else None, "worst_week": float(r.min()) if len(r) else None,
        "worst_4w": float(four.min()) if len(four) else None, "weeks": int(len(r)),
    }


def build_report(progress: Progress = None) -> dict:
    t0 = time.time()
    p, ctx, fac, mask = prepare(progress)
    spec = SPEC.resolved()
    sd = E.rebalance_dates(p.dates, spec.freq, start="2020-01-01")
    fwd = E.forward_open_returns(p, sd)
    _say(progress, 0.25, "量化选股回测：滚动训练 LightGBM（2022 年起每 4 周重训，每次只用之前的数据）……")
    sc = M.scores(fac, p, mask, sd, M.Spec(method="ENS", freq="W", keys=spec.keys, seeds=spec.seeds), lgb_start=OOS_START,
                  fwd=fwd)
    lgb = sc["LGB"]
    idx_ret = _index_returns(p, sd)

    _say(progress, 0.6, "量化选股回测：按资金规模算容量……")
    capacity = []
    main_bt = None
    for capital in CAPITAL_LEVELS:
        cm = capacity_mask(p, mask, capital)
        bt = E.backtest(p, lgb.where(cm.reindex(sd).fillna(False)), cm, sd, top_n=TOP_N, keep_rank=KEEP_RANK,
                        industry_cap=INDUSTRY_CAP, slippage=E.impact_slippage(p, capital, TOP_N),
                        n_random=(60 if capital == DEFAULT_CAPITAL else 0))
        capacity.append({"capital": capital, "segments": _seg_stats(bt, idx_ret), "yearly": _yearly(bt, idx_ret),
                         "universe_median": int(cm.reindex(sd).loc[OOS_START:].sum(axis=1).median())})
        if capital == DEFAULT_CAPITAL:
            main_bt = bt
        _say(progress, 0.6 + 0.2 * len(capacity) / len(CAPITAL_LEVELS), f"量化选股回测：{capital / 1e4:.0f} 万元完成")

    # 主图：默认资金规模的净值 + 同池等权 + 指数（样本外起点 = 1）
    r = main_bt.period_ret.loc[OOS_START:]
    nav = {"dates": [str(d.date()) for d in r.index], "strategy": (1 + r).cumprod().tolist(),
           "random": (1 + main_bt.bench_ret.reindex(r.index)).cumprod().tolist()}
    for code, s in idx_ret.items():
        nav[code] = (1 + s.reindex(r.index).fillna(0)).cumprod().tolist()
    rnd_final: list[float] = []
    if main_bt.random_navs is not None:
        rr = main_bt.random_navs.pct_change()
        rr.iloc[0] = main_bt.random_navs.iloc[0] - 1
        rr = rr.loc[r.index]
        rnav = (1 + rr).cumprod()
        yrs = len(r) / 52
        rnd_final = (rnav.iloc[-1] ** (1 / yrs) - 1).tolist()
        nav["random_p10"] = rnav.quantile(0.1, axis=1).tolist()
        nav["random_p90"] = rnav.quantile(0.9, axis=1).tolist()

    # 为什么用 LightGBM：同样条件下，简单线性打分的样本外成绩
    methods = []
    cm = capacity_mask(p, mask, DEFAULT_CAPITAL)
    for name, label in (("LIT", "文献因子等权"), ("ICIR", "滚动 IC 加权"), ("ENS", "三者平均"), ("LGB", "LightGBM（采用）")):
        bt = E.backtest(p, sc[name].where(cm.reindex(sd).fillna(False)), cm, sd, top_n=TOP_N, keep_rank=KEEP_RANK,
                        industry_cap=INDUSTRY_CAP, slippage=E.impact_slippage(p, DEFAULT_CAPITAL, TOP_N))
        st = bt.stats(OOS_START, None, periods_per_year=52)
        methods.append({"key": name, "label": label,
                        **{k: st[k] for k in ("cagr", "bench_cagr", "excess_ann", "excess_t", "maxdd", "turnover")},
                        "ic": float(E.rank_ic(sc[name], fwd, mask).loc[OOS_START:].mean())})

    _say(progress, 0.85, "量化选股回测：整理因子表……")
    d = M.directions()
    ic = sc.get("_ic")
    if ic is None:
        ic = pd.DataFrame({k: E.rank_ic(fac[k] * d[k], fwd, mask) for k in spec.keys})
    models = sc.get("_lgb_models") or []
    imp = pd.Series(np.mean([m.booster_.feature_importance("gain") for m in models], axis=0), index=spec.keys) \
        if models else pd.Series(0.0, index=spec.keys)
    imp = imp / imp.sum() if imp.sum() > 0 else imp
    fmeta = {f.key: f for f in F.FACTORS}
    factor_rows = []
    for k in spec.keys:
        a, b = ic[k].loc[:"2024-12-31"], ic[k].loc["2025-01-01":]
        factor_rows.append({"key": k, "label": fmeta[k].label, "group": fmeta[k].group, "group_label": F.GROUPS[fmeta[k].group],
                            "direction": fmeta[k].direction, "desc": fmeta[k].desc,
                            "ic_dev": float(a.mean()), "t_dev": E.nw_t(a), "ic_hold": float(b.mean()), "t_hold": E.nw_t(b),
                            "importance": float(imp.get(k, 0.0))})
    factor_rows.sort(key=lambda x: -x["importance"])

    # 持仓特征（默认资金规模）：在可买股票池里的平均分位（0.5 = 和全池平均一样）
    size = np.log(ctx.float_cap)
    turn20 = p.px["turn"].rolling(20, min_periods=10).mean()
    vol20 = p.ret.rolling(20, min_periods=10).std()
    traits = []
    for day, codes in main_bt.holdings.items():
        if day < pd.Timestamp(OOS_START) or not codes:
            continue
        mrow = mask.loc[day]

        def pr(df: pd.DataFrame, day=day, mrow=mrow, codes=codes) -> float:
            return float(df.loc[day].where(mrow).rank(pct=True).reindex(codes).mean())
        traits.append({"size": pr(size), "turn": pr(turn20), "vol": pr(vol20),
                       "cap": float(ctx.float_cap.loc[day].reindex(codes).median()),
                       "univ_cap": float(ctx.float_cap.loc[day].where(mrow).median())})
    tr = pd.DataFrame(traits).mean().to_dict() if traits else {}
    try:
        stages = stage_check(p, capacity_mask(p, mask, DEFAULT_CAPITAL), sd, fwd, main_bt.holdings)
    except Exception as e:  # noqa: BLE001  这一节只是解释，出错不影响报告
        log.warning("主力阶段对照失败：%s", e)
        stages = None
    expect = expectations(fwd, main_bt.holdings, main_bt.period_ret)

    report = {
        "generated_at": datetime.now(config.CHINA_TZ).strftime("%Y-%m-%d %H:%M"),
        "data_start": str(p.dates[0].date()), "data_end": str(p.dates[-1].date()), "oos_start": OOS_START,
        "spec": {**asdict(spec), "top_n": TOP_N, "keep_rank": KEEP_RANK, "industry_cap": INDUSTRY_CAP, "min_amount": MIN_AMOUNT,
                 "max_participation": MAX_PARTICIPATION, "default_capital": DEFAULT_CAPITAL},
        "st_source": p.st_source, "capacity": capacity, "nav": nav, "random_cagr": rnd_final,
        "methods": methods, "factors": factor_rows, "traits": tr, "indexes": INDEXES, "stage_check": stages, "expect": expect,
        "seconds": round(time.time() - t0, 1),
    }
    _write(REPORT_FILE, report)
    _say(progress, 1.0, f"量化选股回测完成（用时 {report['seconds'] / 60:.1f} 分钟）")
    return {"generated_at": report["generated_at"], "seconds": report["seconds"]}
