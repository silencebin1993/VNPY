"""
筹码分布（CYQ）估算：推算"现在所有持股人的买入成本分布在哪些价格"。

算法（与通达信的换手衰减思路一致，属于估算，数值和各家软件会有差异）：
- 价格网格：全市场共用的对数网格，每格 1%（0.01 元 ~ 10 万元），不同股票、不同时期不用重新分格；
- 每天：旧筹码按当天换手率 r 等比例减少（×(1−r)），新成交的 r 份筹码按三角形分布在当天最低价~最高价之间，
  峰值在当天均价（成交额÷成交量）；一字板（最高=最低）全部放在那一格；
- 除权除息日：前一天的分布整体按 preclose/昨收 的比例平移（价格口径始终是"当天的真实价格"，
  所以可以每天增量更新，不需要重算历史）；
- 每只股票第一天：全部筹码按当天的三角形分布初始化。

输出（每只股票每天）：
- winner 获利比例：成本不高于收盘价的筹码占比（0~1），即通达信 WINNER(C)；
- avg_cost 平均成本；p5/p15/p50/p85/p95 成本分位（COST(5) 等）；
- conc70/conc90 集中度：(高分位−低分位)/(高分位+低分位)，越小越集中；peak 筹码峰价格。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import polars as pl


STEP: float = math.log(1.01)
P_MIN: float = 0.01
P_MAX: float = 100_000.0
N_BINS: int = int(math.ceil(math.log(P_MAX / P_MIN) / STEP)) + 1
LOG_CENTERS: np.ndarray = math.log(P_MIN) + np.arange(N_BINS) * STEP
CENTERS: np.ndarray = np.exp(LOG_CENTERS)
QUANTS: tuple[float, ...] = (0.05, 0.15, 0.50, 0.85, 0.95)
STAT_COLS: list[str] = ["winner", "avg_cost", "p5", "p15", "p50", "p85", "p95", "conc70", "conc90", "peak"]


def bin_pos(price: np.ndarray) -> np.ndarray:
    """价格 → 网格上的（小数）位置"""
    p: np.ndarray = np.clip(np.asarray(price, dtype=np.float64), P_MIN, P_MAX)
    return (np.log(p) - LOG_CENTERS[0]) / STEP


class ChipEngine:
    """多只股票的筹码状态（每只一行，行和为 1）。按日期顺序调用 step()。"""

    def __init__(self, codes: list[str] | None = None) -> None:
        self.codes: list[str] = []
        self.row: dict[str, int] = {}
        self.state: np.ndarray = np.zeros((0, N_BINS), dtype=np.float32)
        self.last_close: np.ndarray = np.zeros(0)
        self.alive: np.ndarray = np.zeros(0, dtype=bool)
        if codes:
            self._ensure(codes)

    # ------------------------------------------------ 状态管理
    def _ensure(self, codes: list[str]) -> np.ndarray:
        new: list[str] = [c for c in dict.fromkeys(codes) if c not in self.row]
        if new:
            start: int = len(self.codes)
            for i, c in enumerate(new):
                self.row[c] = start + i
            self.codes.extend(new)
            self.state = np.vstack([self.state, np.zeros((len(new), N_BINS), dtype=np.float32)])
            self.last_close = np.concatenate([self.last_close, np.full(len(new), np.nan)])
            self.alive = np.concatenate([self.alive, np.zeros(len(new), dtype=bool)])
        return np.array([self.row[c] for c in codes], dtype=np.int64)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path = path.with_name(path.name + ".tmp.npz")
        np.savez_compressed(tmp, codes=np.array(self.codes), state=self.state, last_close=self.last_close,
                            alive=self.alive)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> ChipEngine:
        eng = cls()
        with np.load(path, allow_pickle=False) as z:
            eng.codes = [str(c) for c in z["codes"]]
            eng.row = {c: i for i, c in enumerate(eng.codes)}
            eng.state = z["state"].astype(np.float32)
            eng.last_close = z["last_close"].astype(np.float64)
            eng.alive = z["alive"].astype(bool)
        return eng

    # ------------------------------------------------ 每天更新
    def step(self, day: pl.DataFrame) -> np.ndarray:
        """用一天的行情更新（每只股票一行）。需要列：code, high, low, close, volume, amount, turn(%)；
        可选 preclose（用于除权平移）。停牌/无成交的股票不变。返回参与更新的行号。"""
        if day.height == 0:
            return np.zeros(0, dtype=np.int64)
        rows: np.ndarray = self._ensure(day.get_column("code").to_list())
        g = lambda c: day.get_column(c).cast(pl.Float64).to_numpy() if c in day.columns else np.full(day.height, np.nan)  # noqa: E731
        high, low, close = g("high"), g("low"), g("close")
        vol, amount, turn, pre = g("volume"), g("amount"), g("turn"), g("preclose")

        # 除权平移：preclose 与上一个收盘价不一致
        prev: np.ndarray = self.last_close[rows]
        ratio: np.ndarray = np.where((pre > 0) & (prev > 0), pre / prev, 1.0)
        shift_mask: np.ndarray = self.alive[rows] & (np.abs(ratio - 1.0) > 1e-4)
        for r, f in zip(rows[shift_mask], ratio[shift_mask], strict=True):
            self._shift(int(r), float(f))

        ok: np.ndarray = (vol > 0) & (high > 0) & (low > 0) & (high >= low) & np.isfinite(close)
        rate: np.ndarray = np.clip(np.nan_to_num(turn, nan=0.0) / 100.0, 0.0, 1.0)
        avg: np.ndarray = np.where((vol > 0) & (amount > 0), amount / np.where(vol > 0, vol, 1), (high + low + close) / 3)
        avg = np.clip(avg, low, high)
        first: np.ndarray = ok & ~self.alive[rows]
        rate = np.where(first, 1.0, rate)                                  # 第一天：全部筹码按当天分布
        upd: np.ndarray = ok & (rate > 0)
        if upd.any():
            r_idx: np.ndarray = rows[upd]
            self.state[r_idx] *= (1.0 - rate[upd])[:, None].astype(np.float32)
            self._add_triangles(r_idx, low[upd], high[upd], avg[upd], rate[upd])
            self.alive[r_idx] = True
        valid_close: np.ndarray = np.isfinite(close) & (close > 0)
        self.last_close[rows[valid_close]] = close[valid_close]
        return rows[upd]

    def _shift(self, r: int, f: float) -> None:
        """价格整体乘以 f（除权）：分布在对数网格上平移 log(f)/STEP 格（线性插值，保持总量）"""
        old: np.ndarray = self.state[r].astype(np.float64)
        total: float = float(old.sum())
        if total <= 0:
            return
        new: np.ndarray = np.interp(LOG_CENTERS - math.log(f), LOG_CENTERS, old, left=0.0, right=0.0)
        s: float = float(new.sum())
        self.state[r] = (new * (total / s) if s > 0 else old).astype(np.float32)

    def _add_triangles(self, rows: np.ndarray, low: np.ndarray, high: np.ndarray, apex: np.ndarray,
                       mass: np.ndarray) -> None:
        """每只股票在 [low, high] 上加一个峰值在 apex、总量为 mass 的三角形分布（只写覆盖到的那几格）"""
        lo_b: np.ndarray = np.floor(bin_pos(low)).astype(np.int64)
        hi_b: np.ndarray = np.ceil(bin_pos(high)).astype(np.int64)
        width: int = int(max(1, (hi_b - lo_b).max() + 1))
        cols: np.ndarray = np.clip(lo_b[:, None] + np.arange(width)[None, :], 0, N_BINS - 1)
        p: np.ndarray = CENTERS[cols]                                       # (k, width) 各格中心价
        L, H, A = low[:, None], high[:, None], apex[:, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            left: np.ndarray = np.where(A > L, (p - L) / (A - L), 1.0)
            right: np.ndarray = np.where(H > A, (H - p) / (H - A), 1.0)
        w: np.ndarray = np.where(p <= A, left, right)
        w = np.where((p >= L) & (p <= H), np.clip(w, 0.0, 1.0), 0.0)
        w[:, 1:] = np.where(cols[:, 1:] == cols[:, :-1], 0.0, w[:, 1:])    # 截断到网格边缘时不重复计数
        sums: np.ndarray = w.sum(axis=1)
        empty: np.ndarray = sums <= 0                                      # 区间比一格还窄：全部放在均价那一格
        if empty.any():
            k: np.ndarray = np.flatnonzero(empty)
            w[k] = 0.0
            nearest: np.ndarray = np.clip(np.rint(bin_pos(apex[k])).astype(np.int64) - lo_b[k], 0, width - 1)
            w[k, nearest] = 1.0
            sums = w.sum(axis=1)
        w = w / sums[:, None] * mass[:, None]
        np.add.at(self.state, (np.repeat(rows, width), cols.ravel()), w.ravel().astype(np.float32))

    # ------------------------------------------------ 统计
    def stats(self, rows: np.ndarray, close: np.ndarray) -> dict[str, np.ndarray]:
        """指定行的获利比例、平均成本、成本分位、集中度、筹码峰（close 为同顺序的当天收盘价）"""
        st: np.ndarray = self.state[rows].astype(np.float64)
        tot: np.ndarray = st.sum(axis=1)
        tot_safe: np.ndarray = np.where(tot > 0, tot, 1.0)
        cum: np.ndarray = np.cumsum(st, axis=1) / tot_safe[:, None]
        cb: np.ndarray = np.floor(bin_pos(close) + 1e-9).astype(np.int64)
        cb = np.clip(cb, 0, N_BINS - 1)
        winner: np.ndarray = cum[np.arange(len(rows)), cb]
        avg_cost: np.ndarray = (st * CENTERS[None, :]).sum(axis=1) / tot_safe
        out: dict[str, np.ndarray] = {"winner": winner, "avg_cost": avg_cost}
        qv: list[np.ndarray] = []
        for q in QUANTS:
            idx: np.ndarray = np.minimum((cum < q).sum(axis=1), N_BINS - 1)
            qv.append(CENTERS[idx])
        out.update({"p5": qv[0], "p15": qv[1], "p50": qv[2], "p85": qv[3], "p95": qv[4]})
        out["conc70"] = (qv[3] - qv[1]) / (qv[3] + qv[1])
        out["conc90"] = (qv[4] - qv[0]) / (qv[4] + qv[0])
        out["peak"] = CENTERS[np.argmax(st, axis=1)]
        dead: np.ndarray = tot <= 0
        for k in out:
            out[k] = np.where(dead, np.nan, out[k])
        return out

    def distribution(self, code: str, min_share: float = 1e-5) -> dict:
        """某只股票当前的分布（只保留占比不小于 min_share 的格）：{prices, shares(0~1)}"""
        if code not in self.row:
            return {"prices": [], "shares": []}
        st: np.ndarray = self.state[self.row[code]].astype(np.float64)
        tot: float = float(st.sum())
        if tot <= 0:
            return {"prices": [], "shares": []}
        keep: np.ndarray = np.flatnonzero(st / tot >= min_share)
        return {"prices": [round(float(p), 4) for p in CENTERS[keep]],
                "shares": [round(float(s), 6) for s in (st[keep] / tot)]}


# ---------------------------------------------------------------- 批量计算

def compute_panel(panel: pl.DataFrame, want: set | None = None, engine: ChipEngine | None = None,
                  progress=None) -> tuple[pl.DataFrame, ChipEngine]:
    """按日期顺序处理面板（不复权原始价 + preclose + turn），返回每只股票每天的筹码统计。

    want：只输出这些日期的统计（None = 全部）；engine：接着已有状态继续算（增量更新）。
    结果列：date, code, winner, avg_cost, p5..p95, conc70, conc90, peak（价格为当天真实价格口径）。
    """
    eng: ChipEngine = engine or ChipEngine()
    need: list[str] = ["date", "code", "high", "low", "close", "volume", "amount", "turn", "preclose"]
    df: pl.DataFrame = panel.select([c for c in need if c in panel.columns]).sort(["date", "code"])
    frames: list[pl.DataFrame] = []
    days: list = df.get_column("date").unique(maintain_order=True).to_list()
    for i, (key, day) in enumerate(df.group_by("date", maintain_order=True)):
        d = key[0] if isinstance(key, tuple) else key
        rows: np.ndarray = eng.step(day)
        if want is None or d in want:
            codes: list[str] = day.get_column("code").to_list()
            all_rows: np.ndarray = np.array([eng.row[c] for c in codes], dtype=np.int64)
            close: np.ndarray = day.get_column("close").cast(pl.Float64).to_numpy()
            alive: np.ndarray = eng.alive[all_rows]
            if alive.any():
                s: dict = eng.stats(all_rows[alive], close[alive])
                frames.append(pl.DataFrame({"date": [d] * int(alive.sum()),
                                            "code": [c for c, a in zip(codes, alive, strict=True) if a],
                                            **{k: s[k] for k in STAT_COLS}}))
        if progress is not None and len(days) > 20 and i % 20 == 0:
            progress(i / len(days), f"筹码分布：{i}/{len(days)} 天")
        del rows
    out: pl.DataFrame = pl.concat(frames) if frames else pl.DataFrame(
        schema={"date": pl.Date, "code": pl.Utf8, **{k: pl.Float64 for k in STAT_COLS}})
    return out, eng


def single(bars: pl.DataFrame, code: str = "X") -> dict:
    """一只股票（按日期升序；列 date, high, low, close, volume, amount，turn 或 turnover(%)，可选 preclose）：
    返回 {series: {统计名: [逐日数值]}, dist: 最后一天的分布, last: 最后一天的统计}"""
    df: pl.DataFrame = bars
    if "turn" not in df.columns and "turnover" in df.columns:
        df = df.rename({"turnover": "turn"})
    df = df.with_columns(pl.lit(code).alias("code"))        # 统一成同一个代码（引擎里只有这一行）
    eng = ChipEngine([code])
    series: dict[str, list] = {k: [] for k in STAT_COLS}
    for row in df.iter_slices(1):
        eng.step(row)
        if eng.alive[0]:
            s: dict = eng.stats(np.array([0]), row.get_column("close").cast(pl.Float64).to_numpy())
            for k in STAT_COLS:
                v: float = float(s[k][0])
                series[k].append(None if math.isnan(v) else round(v, 6))
        else:
            for k in STAT_COLS:
                series[k].append(None)
    last: dict = {k: (series[k][-1] if series[k] else None) for k in STAT_COLS}
    return {"series": series, "dist": eng.distribution(code), "last": last}
