"""
策略的每日信号和行情（回测和模拟跟踪共用）：
- daily_scores(spec, table, frame, boards, random_seed=None)：全历史每天"可以被选中的股票"及打分（选股方案 / 实验室模型）；
  random_seed 不为空时，在同一个范围里（同样的排雷，但不用方案条件和打分）随机打分——"同样规则随便挑"的对照；
- top_by_day(scores, top)：每天前 top 名（{日期: [代码...]}）；
- PriceBook：真实价格的日线（撮合用）、均线/平均波幅（止损和移动止盈用，前复权算好再换回真实价）、主力阶段、名称；
- regime_by_day()：每天的大盘环境（只用当天及以前的数据）。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from ..trading import rules

CAND_TOP: int = 40
UNIVERSE_DEFAULT: dict = {"exclude_st": True, "min_amount": 3e7, "price_min": 2, "price_max": 1000}


def _scheme(spec: dict) -> dict:
    from ..screener import schemes as S
    from ..screener import store as sstore
    return S.validate_scheme(sstore.get(spec["signal"]["scheme_id"]))


def _model_scheme() -> dict:
    """模型信号用的范围（和选股器默认一样：非 ST、成交额够、去掉排雷红灯）"""
    from ..screener import schemes as S
    return S.validate_scheme({"name": "模型", "universe": dict(UNIVERSE_DEFAULT), "conditions": [], "scoring": {"scheme": "model"},
                              "risk": {"exclude_red": True, "exclude_yellow": False}, "top_n": CAND_TOP})


def selection_table(spec: dict, table: pl.DataFrame, frame: pl.DataFrame, boards: list[str]) -> tuple[dict, pl.DataFrame]:
    """(方案, 全历史每天的表：eligible / cond / red / yellow)。回测里只算一次，策略和随机对照共用"""
    from ..screener import backtest as sbt

    scheme: dict = _scheme(spec) if spec["signal"]["type"] == "scheme" else _model_scheme()
    return scheme, sbt.build_daily(scheme, table, frame, boards)


def scores_from(spec: dict, scheme: dict, df: pl.DataFrame, random_seed: int | None = None,
                include_latest: bool = False) -> pl.DataFrame:
    """每天可以被选中的股票及分数：date, code, score（越大越好）。
    random_seed 不为空：同一范围、同样排雷，但不用方案条件和打分，随机排序（对照组）"""
    from ..screener import backtest as sbt

    if random_seed is not None:
        base = pl.col("eligible")
        if scheme["risk"]["exclude_red"]:
            base = base & ~pl.col("red")
        if scheme["risk"]["exclude_yellow"]:
            base = base & ~pl.col("yellow")
        pool = df.filter(base).select("date", "code")
        rng = np.random.default_rng(random_seed)
        return pool.with_columns(pl.Series("score", rng.random(pool.height)))
    cand = df.filter(sbt.pick_expr(scheme))
    if spec["signal"]["type"] == "model":
        from ..modellab import store as lab
        ms = lab.model_scores(sorted(cand["date"].unique().to_list()), include_latest=include_latest)
        return cand.select("date", "code").join(ms, on=["date", "code"], how="inner").rename({"model_score": "score"})
    return sbt._score_by_date(cand, scheme).select("date", "code", "score")


def daily_scores(spec: dict, table: pl.DataFrame, frame: pl.DataFrame, boards: list[str], random_seed: int | None = None,
                 days: list | None = None) -> pl.DataFrame:
    scheme, df = selection_table(spec, table, frame, boards)
    if days is not None:
        df = df.filter(pl.col("date").is_in(days))
    return scores_from(spec, scheme, df, random_seed, include_latest=days is not None)


def top_by_day(scores: pl.DataFrame, top: int = CAND_TOP) -> dict:
    ranked = (scores.filter(pl.col("score").is_not_null()).sort(["date", "score"], descending=[False, True])
              .group_by("date", maintain_order=True).head(top))
    out: dict = {}
    for d, code in zip(ranked["date"].to_list(), ranked["code"].to_list(), strict=True):
        out.setdefault(d, []).append(code)
    return out


class PriceBook:
    """按 (日期, 代码) 查真实价格日线和计划用的指标。数组按 (日期, 代码) 排序，每天一段，用二分查找"""

    def __init__(self, table: pl.DataFrame, frame: pl.DataFrame, start=None) -> None:
        from ..market import universe
        f = frame.select("code", "date", "high", "low", "close", pl.col("raw_close").alias("_rc"))
        f = f.with_columns((pl.col("_rc") / pl.col("close")).alias("_ratio"))
        prev = pl.col("close").shift(1).over("code")
        tr = pl.max_horizontal(pl.col("high") - pl.col("low"), (pl.col("high") - prev).abs(), (prev - pl.col("low")).abs())
        f = f.with_columns(
            (pl.col("high") * pl.col("_ratio")).alias("raw_high"), (pl.col("low") * pl.col("_ratio")).alias("raw_low"),
            (pl.col("close").rolling_mean(10, min_samples=10).over("code") * pl.col("_ratio")).alias("ma10"),
            (pl.col("close").rolling_mean(20, min_samples=20).over("code") * pl.col("_ratio")).alias("ma20"),
            (tr.rolling_mean(14, min_samples=14).over("code") * pl.col("_ratio")).alias("atr"),
        ).select("code", "date", "raw_high", "raw_low", "ma10", "ma20", "atr")
        t = table.select("code", "date", "raw_open", "raw_close", "preclose", "limit_up", "limit_down",
                         *(["stage"] if "stage" in table.columns else []))
        df = t.join(f, on=["code", "date"], how="left")
        if start is not None:
            df = df.filter(pl.col("date") >= start)
        df = df.sort(["date", "code"])
        self.days: list = df["date"].unique(maintain_order=True).to_list()
        dates = df["date"].to_numpy()
        uniq = np.array(self.days, dtype=dates.dtype)
        self._start = np.searchsorted(dates, uniq, side="left")
        self._end = np.searchsorted(dates, uniq, side="right")
        self._index = {d: i for i, d in enumerate(self.days)}
        self.code = df["code"].to_numpy().astype(str)
        num = lambda c: df[c].cast(pl.Float64).fill_null(np.nan).to_numpy()          # noqa: E731
        self.open, self.high, self.low, self.close = num("raw_open"), num("raw_high"), num("raw_low"), num("raw_close")
        self.preclose, self.limit_up, self.limit_down = num("preclose"), num("limit_up"), num("limit_down")
        self.ma10, self.ma20, self.atr = num("ma10"), num("ma20"), num("atr")
        self.stage = df["stage"].to_numpy() if "stage" in df.columns else None
        u = universe.load_universe()
        self.names: dict[str, str] = dict(zip(u["code"].to_list(), u["name"].to_list(), strict=True)) if not u.is_empty() else {}

    def _row(self, day, code: str) -> int | None:
        i = self._index.get(day)
        if i is None:
            return None
        a, b = self._start[i], self._end[i]
        j = a + int(np.searchsorted(self.code[a:b], code))
        return j if j < b and self.code[j] == code else None

    def bar(self, day, code: str) -> rules.Bar | None:
        j = self._row(day, code)
        if j is None or not np.isfinite(self.open[j]) or not np.isfinite(self.close[j]):
            return None
        hi = self.high[j] if np.isfinite(self.high[j]) else max(self.open[j], self.close[j])
        lo = self.low[j] if np.isfinite(self.low[j]) else min(self.open[j], self.close[j])
        f = lambda x: float(x) if np.isfinite(x) else None                          # noqa: E731
        return rules.Bar(day, float(self.open[j]), float(max(hi, self.open[j], self.close[j])), float(min(lo, self.open[j], self.close[j])),
                         float(self.close[j]), float(self.preclose[j]), f(self.limit_up[j]), f(self.limit_down[j]))

    def info(self, day, code: str) -> dict | None:
        j = self._row(day, code)
        if j is None:
            return None
        f = lambda x: float(x) if np.isfinite(x) else None                          # noqa: E731
        return {"code": code, "name": self.names.get(code), "close": f(self.close[j]), "ma10": f(self.ma10[j]), "ma20": f(self.ma20[j]),
                "atr": f(self.atr[j]), "stage": (str(self.stage[j]) if self.stage is not None and self.stage[j] is not None else None)}

    def levels(self, day):
        """给交易计划收盘评估用：levels(code) -> {ma10, ma20, atr}"""
        def fn(code: str) -> dict:
            x = self.info(day, code) or {}
            return {"ma10": x.get("ma10"), "ma20": x.get("ma20"), "atr": x.get("atr")}
        return fn


def regime_by_day() -> dict:
    """{日期: strong / neutral / weak}（全市场宽度表按日线签名缓存；取不到时返回空 = 不做大盘过滤）"""
    try:
        from ..analysis import market, regime
        sc = regime.score_table(market.daily_table(), market.index_bars())
        return {d: r for d, r in zip(sc["date"].to_list(), sc["regime"].to_list(), strict=True) if r}
    except Exception:  # noqa: BLE001
        return {}
