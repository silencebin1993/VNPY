"""
多因子打分模型（研究、回测、每日打分共用）：

- LIT  文献等权：事先按公开研究选好的因子，每类等权（不用本机数据定权重）；
- ICIR 滚动信息比率加权：每期只用"已经完整知道结果"的历史 IC（滞后 2 期）算每个因子的 IC均值/IC标准差，负的记 0；
- LGB  滚动 LightGBM：每期用之前 train_periods 期的截面标准化因子 → 下一期收益排名训练，预测当期（每 retrain_every 期重训）；
- ENS  上面三个分数各自标准化后取平均（有几个用几个）。

所有模型在第 t 期只用 t 期收盘及以前的数据；标签（下一期收益）要到 t+1 期之后才完整，所以训练数据滞后 lag=2 期。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import evaluate as E
from . import factors as F


LIT_GROUPS: dict[str, list[str]] = {
    "reversal": ["ideal_rev", "rev_20"],
    "activity": ["abn_turn", "turn_std"],
    "risk": ["ivol_20", "max_20", "ideal_amp"],
    "sentiment": ["gap_turn_corr", "pv_corr"],
    "value": ["ep", "bp"],
    "surprise": ["sue"],
}
LIT_WEIGHTS: dict[str, float] = {k: 1.0 / len(LIT_GROUPS) / len(v) for v in LIT_GROUPS.values() for k in v}
# 每日打分能拿到最新值的因子（市销率、市现率只有 baostock 历史，最新几天没有 → 不进生产模型）
PROD_KEYS: list[str] = [f.key for f in F.FACTORS if f.key not in ("sp", "cfp")]


@dataclass
class Spec:
    method: str = "ENS"
    freq: str = "W"
    keys: list[str] = field(default_factory=lambda: list(PROD_KEYS))
    lookback: int | None = None          # ICIR 回看期数（None：周 104 / 月 24）
    train_periods: int | None = None     # LGB 训练期数（None：周 156 / 月 36）
    retrain_every: int | None = None     # LGB 重训间隔（None：周 4 / 月 1）
    lag: int = 2
    seeds: tuple[int, ...] = (1, 2, 3)

    def resolved(self) -> Spec:
        weekly = self.freq in ("W", "2W")
        return Spec(self.method, self.freq, list(self.keys), self.lookback or (104 if weekly else 24),
                    self.train_periods or (156 if weekly else 36), self.retrain_every or (4 if weekly else 1),
                    self.lag, tuple(self.seeds))


def directions() -> dict[str, int]:
    return {f.key: f.direction for f in F.FACTORS}


def zscores(fac: dict[str, pd.DataFrame], keys: list[str], sd: pd.DatetimeIndex, msk: pd.DataFrame) -> dict[str, pd.DataFrame]:
    d = directions()
    return {k: E.zscore(fac[k].reindex(sd) * d[k], msk) for k in keys}


def icir_weights(ic: pd.DataFrame, lookback: int, lag: int = 2, min_n: int | None = None) -> pd.DataFrame:
    min_n = min_n or max(6, lookback // 2)
    rows = {}
    for i, d in enumerate(ic.index):
        hist = ic.iloc[max(0, i - lag - lookback + 1): max(0, i - lag + 1)]
        if hist.notna().sum().max() < min_n:
            rows[d] = pd.Series(np.nan, index=ic.columns)
            continue
        ir = hist.mean() / hist.std()
        w = ir.clip(lower=0).fillna(0)
        rows[d] = w / w.sum() if w.sum() > 0 else w
    return pd.DataFrame(rows).T


def lgb_fit(X: np.ndarray, y: np.ndarray, seeds: tuple[int, ...]) -> list:
    import lightgbm as lgb

    ok = ~np.isnan(y) & (np.isnan(X).mean(axis=1) < 0.5)
    models = []
    for s in seeds:
        m = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.03, num_leaves=31, min_child_samples=300,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5.0,
                              random_state=s, verbose=-1, n_jobs=8)
        m.fit(X[ok], y[ok])
        models.append(m)
    return models


def lgb_predict(models: list, X: np.ndarray) -> np.ndarray:
    return np.mean([m.predict(X) for m in models], axis=0)


def lgb_walk(zs: dict[str, pd.DataFrame], fwd: pd.DataFrame, msk: pd.DataFrame, spec: Spec, start: str | None,
             predict_last: bool = True) -> tuple[pd.DataFrame, list | None]:
    """滚动训练 → 每期的样本外分数；返回 (分数, 最后一次训练的模型)"""
    dates = fwd.index
    keys = list(zs)
    X_all = np.stack([zs[k].reindex(dates).values for k in keys], axis=2).astype(np.float32)
    y_rank = (fwd.where(msk).rank(axis=1, pct=True).values - 0.5).astype(np.float32)
    out = pd.DataFrame(np.nan, index=dates, columns=fwd.columns)
    models = None
    first = int(np.searchsorted(dates, pd.Timestamp(start))) if start else spec.train_periods + spec.lag
    first = max(first, spec.lag + 8)
    last_fit = None
    for i in range(first, len(dates)):
        if not predict_last and i == len(dates) - 1:
            break
        if models is None or (i - first) % spec.retrain_every == 0:
            lo, hi = max(0, i - spec.lag - spec.train_periods + 1), i - spec.lag + 1
            models = lgb_fit(X_all[lo:hi].reshape(-1, len(keys)), y_rank[lo:hi].reshape(-1), spec.seeds)
            last_fit = i
        Xi = X_all[i]
        ok_i = msk.iloc[i].values & (np.isnan(Xi).mean(axis=1) < 0.5)
        if ok_i.any():
            pred = np.full(len(fwd.columns), np.nan)
            pred[ok_i] = lgb_predict(models, Xi[ok_i])
            out.iloc[i] = pred
    del last_fit
    return out, models


def scores(fac: dict[str, pd.DataFrame], p, mask: pd.DataFrame, sd: pd.DatetimeIndex, spec: Spec,
           lgb_start: str | None = "2022-01-01", fwd: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """全部模型在每个信号日的分数（只用当时可得的数据）；返回 {LIT, ICIR, LGB, ENS}"""
    spec = spec.resolved()
    msk = mask.reindex(sd).fillna(False)
    fwd = fwd if fwd is not None else E.forward_open_returns(p, sd)
    d = directions()
    keys = spec.keys
    zs = zscores(fac, keys, sd, msk)
    out: dict[str, pd.DataFrame] = {}
    lit = {k: w for k, w in LIT_WEIGHTS.items() if k in keys}
    out["LIT"] = E.composite(fac, lit, d, mask, dates=sd)
    if spec.method in ("ICIR", "ENS"):
        ic = pd.DataFrame({k: E.rank_ic(fac[k] * d[k], fwd, mask) for k in keys})
        w = icir_weights(ic, spec.lookback, spec.lag)
        tot = sum(zs[k].fillna(0.0).mul(w[k].fillna(0.0), axis=0) for k in keys)
        out["ICIR"] = tot.where(msk).where(w.sum(axis=1) > 0, axis=0)
        out["_icir_weights"] = w
        out["_ic"] = ic
    if spec.method in ("LGB", "ENS"):
        out["LGB"], out["_lgb_models"] = lgb_walk(zs, fwd, msk, spec, lgb_start)
    if spec.method == "ENS":
        parts = [E.zscore(out[k], msk) for k in ("LIT", "ICIR", "LGB") if k in out]
        num = sum(x.fillna(0.0) for x in parts)
        den = sum(x.notna().astype(float) for x in parts).replace(0, np.nan)
        out["ENS"] = (num / den).where(msk)
    return out
