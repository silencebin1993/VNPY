"""
指标与图表接口：指标目录、个股图表（K 线 + 指标 + 筹码分布，一次返回）。
"""
import json
from typing import Annotated, Any

import polars as pl
from fastapi import APIRouter, HTTPException, Query

from ..common import CACHE, check_code, describe_error, mod, ok, panel_sig


router = APIRouter()

PANEL_COLS: list[str] = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn"]
PRICE_STATS: tuple[str, ...] = ("avg_cost", "p5", "p15", "p50", "p85", "p95", "peak")


@router.get("/api/indicators/catalog")
def indicators_catalog() -> Any:
    cat = mod("indicators.catalog")
    return ok({"items": cat.listing(), "cats": cat.CATS})


def _panel_rows(code: str) -> pl.DataFrame:
    """本地日线（不复权原始价 + preclose + 换手），按日期升序；没有时返回空表"""
    def build() -> pl.DataFrame:
        try:
            df: pl.DataFrame = mod("market.history").load_panel(codes=[code], columns=PANEL_COLS)
        except Exception:  # noqa: BLE001  本地没有日线
            return pl.DataFrame()
        return df.sort("date") if df.height else df
    return CACHE.get(("panel_rows", code), build, ttl=600, sig=panel_sig())


def _panel_qfq_bars(code: str, period: str, count: int) -> list[dict]:
    """在线 K 线不可用时：用本地日线算前复权 K 线（周/月线按周/月聚合）"""
    df: pl.DataFrame = _panel_rows(code)
    if df.is_empty():
        return []
    q: pl.DataFrame = mod("indicators.adjust").add_qfq(df, by=None)
    q = q.select(pl.col("date"), pl.col("qopen").alias("open"), pl.col("qhigh").alias("high"),
                 pl.col("qlow").alias("low"), pl.col("qclose").alias("close"), "volume", "amount",
                 pl.col("turn").alias("turnover"))
    if period in ("week", "month"):
        q = q.group_by_dynamic("date", every="1w" if period == "week" else "1mo").agg(
            pl.col("date").last().alias("last"), pl.col("open").first(), pl.col("high").max(), pl.col("low").min(),
            pl.col("close").last(), pl.col("volume").sum(), pl.col("amount").sum(), pl.col("turnover").sum(),
        ).drop("date").rename({"last": "date"})
    return q.tail(count).with_columns(pl.col("date").cast(pl.Utf8)).to_dicts()


def load_bars(code: str, period: str, count: int) -> tuple[list[dict], str, str, list[str]]:
    """(bars, source, adjust, warnings)：优先在线前复权 K 线，失败用本地日线算的前复权"""
    warnings: list[str] = []
    try:
        bars: list[dict] = mod("market.realtime").kline(code, period=period, adjust="qfq", count=count)
        if bars:
            return bars, "realtime", "qfq", warnings
        warnings.append("在线K线没有返回数据，已改用本地日线")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"在线K线暂时取不到（{describe_error(e)[1]}），已改用本地日线（前复权）")
    bars = _panel_qfq_bars(code, period, count)
    if not bars:
        raise HTTPException(404, f"{code} 没有K线数据（在线取不到，本地也没有）")
    return bars, "panel", "qfq", warnings


def chips_for(code: str, bars: list[dict]) -> dict | None:
    """筹码分布：优先用本地日线（不复权价 + preclose + 换手，和全市场选股的口径一致），
    价格类统计换算成前复权口径后按日期对齐到 bars；本地没有这只股票时用 bars 自己估算"""
    chips = mod("indicators.chips")
    df: pl.DataFrame = _panel_rows(code)
    note: str = "筹码分布是根据每天的成交量和换手率推算的估计值，不是真实持仓数据，和其他软件的数值会有差异。"
    if df.height >= 20:
        def build() -> dict:
            res: dict = chips.single(df, code)
            fac: list = mod("indicators.adjust").add_qfq(df, by=None).get_column("adj_factor").to_list()
            series: dict = res["series"]
            for k in PRICE_STATS:
                series[k] = [None if v is None or f is None else round(v * f, 4) for v, f in zip(series[k], fac, strict=True)]
            return {"dates": [str(d) for d in df.get_column("date").to_list()], "series": series,
                    "dist": res["dist"], "last": res["last"], "as_of": str(df.get_column("date")[-1])}
        full: dict = CACHE.get(("chips", code), build, ttl=600, sig=panel_sig())
        pos: dict = {d: i for i, d in enumerate(full["dates"])}
        aligned: dict = {k: [full["series"][k][pos[str(b["date"])[:10]]] if str(b["date"])[:10] in pos else None
                             for b in bars] for k in full["series"]}
        return {"series": aligned, "dist": full["dist"], "last": full["last"], "as_of": full["as_of"],
                "source": "panel", "note": note}
    if not bars or "turnover" not in bars[0]:
        return None
    bdf: pl.DataFrame = pl.DataFrame(bars).with_columns(pl.col("date").cast(pl.Utf8))
    res = chips.single(bdf, code)
    return {"series": res["series"], "dist": res["dist"], "last": res["last"], "as_of": str(bars[-1]["date"]),
            "source": "bars", "note": note}


@router.get("/api/stock/{code}/chart")
def stock_chart(
    code: str,
    period: Annotated[str, Query()] = "day",
    count: Annotated[int, Query(ge=30, le=3000)] = 600,
    ind: Annotated[str, Query()] = "ma,vol,macd",
    params: Annotated[str | None, Query()] = None,
    chips: Annotated[bool, Query()] = True,
) -> Any:
    """个股图表：前复权 K 线 + 指标（ind=逗号分隔的指标 id，params=JSON 如 {"ma":{"n1":5}}）+ 筹码分布（仅日线）"""
    code = check_code(code)
    if period not in ("day", "week", "month"):
        raise HTTPException(400, "period 只能是 day（日K）、week（周K）、month（月K）")
    cat = mod("indicators.catalog")
    ids: list[str] = [i.strip() for i in ind.split(",") if i.strip()]
    unknown: list[str] = [i for i in ids if i not in cat.CATALOG]
    if unknown:
        raise HTTPException(400, f"不认识的指标：{'、'.join(unknown)}")
    try:
        p: dict = json.loads(params) if params else {}
        if not isinstance(p, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "params 应为 JSON 对象，例如 {\"ma\":{\"n1\":5}}") from None
    bars, source, adjust, warnings = load_bars(code, period, count)
    indicators: dict = cat.compute(bars, ids, p)
    chip: dict | None = None
    if chips and period == "day":
        try:
            chip = chips_for(code, bars)
        except Exception as e:  # noqa: BLE001  筹码算不出来不影响 K 线和指标
            warnings.append(f"筹码分布暂时算不出来：{describe_error(e)[1]}")
    server = mod("api.server")
    info: dict = server.stock_info(code)
    return ok({
        "code": code, "name": info.get("name"), "period": period, "adjust": adjust, "source": source, "bars": bars,
        "indicators": indicators, "chips": chip, "limit_days": server.limit_days(code), "warnings": warnings,
    })
