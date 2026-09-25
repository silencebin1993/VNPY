"""
多因子选股的数据层：主板日线 → 宽表（行 = 交易日，列 = 股票代码）。

- 收益一律用交易所昨收（preclose，已按除权除息调整）：ret = close / preclose - 1；
  复权价 adj_close = ∏(1+ret)，同一天开盘价与收盘价同一口径：adj_open = open × adj_close / close。
- 停牌（没有K线或 tradestatus=0）的日子全部为 NaN；能否买卖另外由 limit_up_open / limit_down_open 判断。
- ST：优先用 baostock 逐日 isST（stock_lab/bs_hist，历史真实状态）；没有的股票退回"当前名称"近似。
- 估值：baostock 逐日 peTTM / pbMRQ / psTTM / pcfNcfTTM（官方口径、总股本）；最新几天没有时由
  腾讯快照（今日市盈率TTM/市净率）补上，见 service.py。
- 财报：公告日（notice_date）之后的下一个交易日才可用；同一天可用多期时取最新报告期，
  之后公布的旧报告期（补发、更正）不会让数据倒退。
"""
from __future__ import annotations

import glob
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from .. import config


PRICE_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "preclose", "volume", "amount", "turn")
BS_DIR: Path = config.STOCK_LAB.joinpath("bs_hist")
ST_MAIN_10PCT: date = date(2026, 7, 6)       # 主板 ST 涨跌幅 5% → 10%（见 predict/limits.py）
FUND_FIELDS: tuple[str, ...] = (
    "eps", "bvps", "roe", "revenue", "revenue_yoy", "net_profit", "net_profit_yoy", "debt_ratio",
    "gross_margin", "net_profit_deducted", "net_margin", "ocfps", "net_profit_ttm", "revenue_ttm", "eps_ttm",
)


def main_board(code: str) -> bool:
    """沪深主板：60xxxx、00xxxx（含原中小板 002/003、001）"""
    return code.startswith(("60", "00"))


@dataclass
class Panel:
    dates: pd.DatetimeIndex
    codes: pd.Index
    px: dict[str, pd.DataFrame]                   # 原始字段宽表：open/high/low/close/preclose/volume/amount/turn
    ret: pd.DataFrame                             # 当日收益（收盘/昨收-1），停牌为 NaN
    adj_close: pd.DataFrame                       # 复权收盘价（停牌日沿用前值）
    adj_open: pd.DataFrame                        # 复权开盘价（停牌日 NaN）
    trading: pd.DataFrame                         # 当天有正常成交
    st: pd.DataFrame                              # 当天是否 ST（布尔）
    listed_days: pd.DataFrame                     # 上市以来的交易日数（按面板计）
    limit_up_open: pd.DataFrame                   # 开盘即涨停（买不进）
    limit_down_open: pd.DataFrame                 # 开盘即跌停（卖不出）
    limit_up_close: pd.DataFrame                  # 收盘涨停
    industry: pd.Series                           # 代码 → 行业（证监会口径，当前）
    names: pd.Series
    valuation: dict[str, pd.DataFrame] = field(default_factory=dict)   # pe_ttm / pb / ps_ttm / pcf（baostock）
    st_source: str = "current"

    @property
    def close(self) -> pd.DataFrame:
        return self.px["close"]

    def wide(self, name: str) -> pd.DataFrame:
        return self.px[name]


def _panel_files(start: date | None, end: date | None) -> list[str]:
    files: list[str] = []
    for path in sorted(glob.glob(str(config.PANEL_DIR.joinpath("*.parquet")))):
        year = int(Path(path).stem)
        if (start is None or year >= start.year) and (end is None or year <= end.year):
            files.append(path)
    return files


def load_long(start: date | None = None, end: date | None = None) -> pl.DataFrame:
    files = _panel_files(start, end)
    lf = pl.scan_parquet(files).filter(pl.col("code").str.starts_with("60") | pl.col("code").str.starts_with("00"))
    if start is not None:
        lf = lf.filter(pl.col("date") >= start)
    if end is not None:
        lf = lf.filter(pl.col("date") <= end)
    return lf.select(["date", "code", *PRICE_FIELDS, "tradestatus"]).collect()


def _pivot(df: pl.DataFrame, value: str, dates: pd.DatetimeIndex, codes: pd.Index, dtype=np.float64) -> pd.DataFrame:
    """长表 → 宽表（numpy 直接按行列号填值，比 pivot 快一个数量级）；重复的 (date, code) 取最后一行"""
    d = df["date"].to_numpy().astype("datetime64[ns]")
    ri = np.searchsorted(dates.values, d)
    ci = codes.get_indexer(df["code"].to_numpy())
    ok = (ci >= 0) & (ri < len(dates))
    ok[ok] &= dates.values[ri[ok]] == d[ok]
    arr = np.full((len(dates), len(codes)), np.nan, dtype=np.float64)
    arr[ri[ok], ci[ok]] = df[value].cast(pl.Float64).fill_null(np.nan).to_numpy()[ok]
    return pd.DataFrame(arr.astype(dtype, copy=False), index=dates, columns=codes)


def load_bs_hist() -> pl.DataFrame | None:
    """baostock 逐日 isST 与估值（研究用；缺失时返回 None）"""
    files = sorted(glob.glob(str(BS_DIR.joinpath("part_*.parquet"))))
    if not files:
        return None
    df = pl.concat([pl.read_parquet(f) for f in files], how="vertical_relaxed")
    num = lambda c: pl.col(c).cast(pl.Utf8).str.strip_chars().replace("", None).cast(pl.Float64, strict=False)  # noqa: E731
    return df.select(
        pl.col("date").str.to_date(),
        pl.col("code").cast(pl.Utf8),
        (pl.col("isST").cast(pl.Utf8) == "1").alias("st"),
        num("peTTM").alias("pe_ttm"), num("pbMRQ").alias("pb"), num("psTTM").alias("ps_ttm"),
        num("pcfNcfTTM").alias("pcf"),
    ).unique(["code", "date"], keep="last")


def from_wide(px: dict[str, pd.DataFrame], uni: pd.DataFrame | None = None, st: pd.DataFrame | None = None,
              valuation: dict[str, pd.DataFrame] | None = None, st_source: str = "current") -> Panel:
    """原始字段宽表 → Panel（复权价、可交易标记、涨跌停、上市天数）。测试和每日打分也走这里。

    uni：以代码为索引，可含 name / industry / is_st / list_date。上市早于面板起点的股票，上市天数从 1000 起算。
    """
    px = {k: v.copy() for k, v in px.items()}
    close = px["close"]
    dates, codes = close.index, close.columns
    vol = px.get("volume")
    trading = close.notna() & (vol > 0 if vol is not None else True)
    for f in list(px):
        px[f] = px[f].where(trading)
    ret = px["close"] / px["preclose"] - 1.0
    growth = (1.0 + ret.fillna(0.0)).cumprod()
    first_seen = px["close"].notna().cumsum() > 0
    adj_close = growth.where(first_seen)
    adj_open = px["open"] * adj_close / px["close"]

    uni = (uni if uni is not None else pd.DataFrame(index=codes)).reindex(codes)
    offset = pd.Series(0.0, index=codes)
    if "list_date" in uni.columns:
        ld = pd.to_datetime(uni["list_date"], errors="coerce")
        offset[(ld < dates[0]).fillna(False).values] = 1000.0
    # 不知道上市日期、但面板第一天就有行情的老股票
    unknown = uni["list_date"].isna() if "list_date" in uni.columns else pd.Series(True, index=codes)
    offset[(unknown & first_seen.iloc[0]).values] = 1000.0
    listed_days = (first_seen.cumsum() + offset).where(first_seen)

    if st is None:
        st = pd.DataFrame(False, index=dates, columns=codes)
        if "is_st" in uni.columns:
            cur = uni["is_st"].fillna(False).astype(bool)
            st.loc[:, cur[cur].index] = True
    ratio = pd.DataFrame(0.10, index=dates, columns=codes)
    before = pd.DataFrame(np.repeat((dates < pd.Timestamp(ST_MAIN_10PCT))[:, None], len(codes), axis=1),
                          index=dates, columns=codes)
    ratio = ratio.mask(st & before, 0.05)
    up_px = np.round(px["preclose"] * (1 + ratio) + 1e-9, 2)
    dn_px = np.round(px["preclose"] * (1 - ratio) + 1e-9, 2)
    # 新股前几天没有涨跌幅限制，也不会被选进来（上市天数过滤），这里不特殊处理
    return Panel(
        dates=dates, codes=codes, px=px, ret=ret, adj_close=adj_close.ffill().where(first_seen),
        adj_open=adj_open, trading=trading, st=st, listed_days=listed_days,
        limit_up_open=(px["open"] >= up_px - 0.005) & trading,
        limit_down_open=(px["open"] <= dn_px + 0.005) & trading,
        limit_up_close=(px["close"] >= up_px - 0.005) & trading,
        industry=uni["industry"].fillna("未知") if "industry" in uni.columns else pd.Series("未知", index=codes),
        names=uni["name"].fillna("") if "name" in uni.columns else pd.Series("", index=codes),
        valuation=valuation or {}, st_source=st_source,
    )


def load_panel(start: date | None = None, end: date | None = None, with_bs: bool = True) -> Panel:
    long = load_long(start, end)
    dates = pd.DatetimeIndex(sorted(long["date"].unique().to_list()))
    codes = pd.Index(sorted(long["code"].unique().to_list()))
    active = long.filter(pl.col("tradestatus") != 0)
    px = {f: _pivot(active, f, dates, codes) for f in PRICE_FIELDS}
    uni = pl.read_parquet(config.UNIVERSE_FILE).to_pandas().set_index("code").reindex(codes)

    # ST：baostock 历史 isST 优先，其余用当前状态
    st = pd.DataFrame(False, index=dates, columns=codes)
    cur_st = uni["is_st"].fillna(False).astype(bool)
    st.loc[:, cur_st[cur_st].index] = True
    valuation: dict[str, pd.DataFrame] = {}
    st_source = "current"
    bs = load_bs_hist() if with_bs else None
    if bs is not None and bs.height:
        bs = bs.filter(pl.col("code").is_in(list(codes)))
        have = pd.Index(sorted(bs["code"].unique().to_list()))
        st_bs = _pivot(bs.with_columns(pl.col("st").cast(pl.Float64)), "st", dates, codes).ffill()
        st.loc[:, have] = st_bs[have].fillna(0.0).astype(bool)
        for f in ("pe_ttm", "pb", "ps_ttm", "pcf"):
            valuation[f] = _pivot(bs, f, dates, codes)
        st_source = f"baostock({len(have)}/{len(codes)})"
    if with_bs:
        # baostock 之后的日子：收盘后腾讯快照存下的市盈率TTM / 市净率（同口径）
        from . import valuation as val_mod
        tx = val_mod.load_tx()
        if tx.height:
            for f in ("pe_ttm", "pb"):
                w = _pivot(tx, f, dates, codes)
                valuation[f] = valuation[f].combine_first(w) if f in valuation else w
    return from_wide(px, uni, st=st, valuation=valuation, st_source=st_source)


# ---------------------------------------------------------------- 财报（按公告日生效）

def load_fund_long() -> pd.DataFrame:
    from ..market import fundamentals as fund_mod

    f = fund_mod.load_fundamentals().to_pandas()
    f = f[f["code"].map(main_board)].copy()
    f["report_date"] = pd.to_datetime(f["report_date"])
    notice = pd.to_datetime(f["notice_date"])
    avail = pd.to_datetime(f["avail_date"])
    # 公告日缺失时用法定披露截止日（avail_date）兜底
    f["eff"] = notice.fillna(avail)
    return f.sort_values(["code", "report_date"])


def single_quarter(f: pd.DataFrame, col: str) -> pd.Series:
    """累计值（年初至今）→ 单季度值"""
    g = f.groupby(["code", f["report_date"].dt.year])[col]
    prev = g.shift(1)
    first = f["report_date"].dt.month == 3
    return f[col].where(first, f[col] - prev)


def fund_asof(f: pd.DataFrame, values: dict[str, pd.Series], dates: pd.DatetimeIndex, codes: pd.Index) -> dict[str, pd.DataFrame]:
    """把按报告期的数据放到"公告后下一个交易日"起生效，并向后填充成宽表。

    同一只股票：只接受报告期比已生效的更新的记录（旧报告期补发不回退）。
    """
    idx = np.searchsorted(dates.values, f["eff"].values.astype("datetime64[ns]"), side="right")
    ok = idx < len(dates)
    base = pd.DataFrame({"code": f["code"].values, "report_date": f["report_date"].values, "pos": idx},
                        index=f.index)
    base = base[ok & base["code"].isin(codes).values]
    base = base.sort_values(["code", "pos", "report_date"])
    base["runmax"] = base.groupby("code")["report_date"].cummax()
    base = base[base["report_date"] >= base["runmax"]]
    base = base.drop_duplicates(["code", "pos"], keep="last")
    out: dict[str, pd.DataFrame] = {}
    for name, series in values.items():
        vals = series.reindex(base.index)
        wide = pd.DataFrame(np.nan, index=range(len(dates)), columns=codes)
        col_pos = codes.get_indexer(base["code"])
        arr = wide.values
        arr[base["pos"].values, col_pos] = vals.values
        # 报告期记录存在但数值缺失：用 -inf 占位，前向填充后再还原为 NaN，避免沿用更旧一期的数值
        mask = np.zeros_like(arr, dtype=bool)
        mask[base["pos"].values, col_pos] = True
        arr = np.where(mask & np.isnan(arr), -np.inf, arr)
        filled = pd.DataFrame(arr, index=dates, columns=codes).ffill()
        out[name] = filled.replace(-np.inf, np.nan)
    return out


def report_age(f: pd.DataFrame, dates: pd.DatetimeIndex, codes: pd.Index) -> pd.DataFrame:
    """最新可用财报的公告距今多少个交易日（用于盈余公告漂移类因子）"""
    pos = pd.Series(np.arange(len(dates)), dtype=float)
    days = fund_asof(f, {"pos": pd.Series(np.searchsorted(dates.values, f["eff"].values.astype("datetime64[ns]"), side="right"),
                                         index=f.index, dtype=float)}, dates, codes)["pos"]
    return pd.DataFrame(pos.values[:, None] - days.values, index=dates, columns=codes)
