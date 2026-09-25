"""
行情数据：从免费公开接口（腾讯/东方财富/新浪，经 akshare）下载ETF日线，保存到 vnpy.alpha 的 AlphaLab。

- 回测和信号计算用"后复权"价格：分红再投资后的真实收益，不会因为分红出现假的下跌；
- 下单数量用"不复权"的真实成交价格计算。
"""
import contextlib
import io
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import polars as pl

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData
from vnpy.alpha import AlphaLab

from .config import ETFS, Etf, LAB_PATH, TradingParams, Universe


CHINA_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")

# 国内行情网站直连，不走系统代理（开着VPN/代理时，这些网站经常拒绝连接）
DOMESTIC_HOSTS: list[str] = [
    "eastmoney.com", "sina.com.cn", "sinajs.cn", "qq.com", "gtimg.cn", "sse.com.cn", "szse.cn"
]

META_FILE: str = "meta.json"


def china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def last_complete_day(now: datetime | None = None) -> date:
    """最近一个已收盘的自然日：15:10 之前当天行情还不完整"""
    now = now or china_now()
    if now.hour * 60 + now.minute < 15 * 60 + 10:
        return (now - timedelta(days=1)).date()
    return now.date()


@contextlib.contextmanager
def _network_mode(direct: bool):
    """direct=True 时国内网站绕过代理"""
    old: str | None = os.environ.get("NO_PROXY")
    if direct:
        hosts: list[str] = [h for h in (old or "").split(",") if h] + DOMESTIC_HOSTS
        os.environ["NO_PROXY"] = ",".join(dict.fromkeys(hosts))
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = old


def _call(func: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    """依次尝试：直连 -> 系统代理 -> 等待后直连"""
    errors: list[str] = []
    for attempt, direct in enumerate([True, False, True]):
        try:
            with _network_mode(direct), contextlib.redirect_stderr(io.StringIO()):
                df: pd.DataFrame = func()
            if df is not None and not df.empty:
                return df
            errors.append("返回数据为空")
        except Exception as e:  # noqa: BLE001  网络异常种类很多，统一重试
            errors.append(f"{type(e).__name__}: {str(e)[:80]}")
        time.sleep(1 + attempt * 2)
    raise ConnectionError("；".join(errors))


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={
        "日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
        "成交量": "volume", "成交额": "amount",
    })
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce") if col in df else 0.0
    df = df[["date", "open", "high", "low", "close", "volume", "amount"]].fillna({"volume": 0, "amount": 0})
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df["close"] > 0) & (df["open"] > 0)]
    df = df.drop_duplicates("date").sort_values("date")
    return df[df["date"].dt.date <= last_complete_day()].reset_index(drop=True)


def fetch_history(etf: Etf, adjust: str = "hfq", start: str = "20100101") -> pd.DataFrame:
    """下载日线。adjust: hfq 后复权 / "" 不复权。主源腾讯，备源东方财富。"""
    import akshare as ak

    end: str = china_now().strftime("%Y%m%d")
    try:
        return _clean(_call(lambda: ak.stock_zh_a_hist_tx(
            symbol=etf.market_symbol, start_date=start, end_date=end, adjust=adjust
        )))
    except ConnectionError as tx_error:
        try:
            return _clean(_call(lambda: ak.fund_etf_hist_em(
                symbol=etf.code, period="daily", start_date=start, end_date=end, adjust=adjust
            )))
        except ConnectionError as em_error:
            raise ConnectionError(f"腾讯接口：{tx_error}；东方财富接口：{em_error}") from em_error


def fetch_latest_prices(etfs: list[Etf]) -> dict[str, tuple[date, float]]:
    """最新不复权收盘价 {vt_symbol: (日期, 价格)}，用于计算下单数量。获取失败的不在结果中。"""
    import akshare as ak

    start: str = (china_now() - timedelta(days=20)).strftime("%Y%m%d")
    prices: dict[str, tuple[date, float]] = {}
    for etf in etfs:
        try:
            df: pd.DataFrame = fetch_history(etf, adjust="", start=start)
        except ConnectionError:
            try:
                df = _clean(_call(lambda e=etf: ak.fund_etf_hist_sina(symbol=e.market_symbol)))
            except ConnectionError:
                continue
        if df.empty:
            continue
        row = df.iloc[-1]
        prices[etf.vt_symbol] = (row["date"].date(), float(row["close"]))
    return prices


def to_bars(etf: Etf, df: pd.DataFrame) -> list[BarData]:
    exchange: Exchange = Exchange(etf.exchange)
    bars: list[BarData] = []
    for row in df.itertuples(index=False):
        bars.append(BarData(
            symbol=etf.code,
            exchange=exchange,
            datetime=row.date.to_pydatetime(),
            interval=Interval.DAILY,
            open_price=float(row.open),
            high_price=float(row.high),
            low_price=float(row.low),
            close_price=float(row.close),
            volume=float(row.volume),
            turnover=float(row.amount),
            gateway_name="AKSHARE",
        ))
    return bars


def get_lab() -> AlphaLab:
    return AlphaLab(str(LAB_PATH))


def setup_contracts(lab: AlphaLab, universe: Universe, trading: TradingParams) -> None:
    """写入回测用的合约交易配置（手续费、合约乘数、最小价格变动）"""
    for etf in ETFS:
        rate: float = trading.cash_rate if etf.vt_symbol == universe.cash else trading.backtest_rate
        lab.add_contract_setting(etf.vt_symbol, long_rate=rate, short_rate=rate, size=1, pricetick=0.001)


def update_all(log: Callable[[str], None] = print) -> tuple[list[str], list[str]]:
    """全量刷新所有ETF的后复权日线。返回 (成功列表, 失败列表)。失败的保留旧数据。"""
    lab: AlphaLab = get_lab()
    setup_contracts(lab, Universe(), TradingParams())

    ok: list[str] = []
    failed: list[str] = []
    for i, etf in enumerate(ETFS, 1):
        try:
            df: pd.DataFrame = fetch_history(etf, adjust="hfq")
        except ConnectionError as e:
            failed.append(etf.name)
            log(f"  [{i:>2}/{len(ETFS)}] {etf.name:<10} 下载失败，保留旧数据（{str(e)[:60]}）")
            continue

        # 全量覆盖，避免新旧复权基准混在一起
        file_path = lab.daily_path.joinpath(f"{etf.vt_symbol}.parquet")
        if file_path.exists():
            file_path.unlink()
        lab.save_bar_data(to_bars(etf, df))

        ok.append(etf.name)
        log(f"  [{i:>2}/{len(ETFS)}] {etf.name:<10} {len(df)} 条  最新 {df['date'].iloc[-1]:%Y-%m-%d}")

    if ok:
        meta: dict = {"updated_at": china_now().strftime("%Y-%m-%d %H:%M:%S"), "last_complete_day": str(last_complete_day())}
        LAB_PATH.joinpath(META_FILE).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return ok, failed


def data_is_fresh() -> bool:
    """今天是否已经更新过（以收盘时点为准）"""
    path = LAB_PATH.joinpath(META_FILE)
    if not path.exists():
        return False
    meta: dict = json.loads(path.read_text(encoding="utf-8"))
    return meta.get("last_complete_day") == str(last_complete_day())


def load_prices(vt_symbols: list[str], field: str = "close", lab: AlphaLab | None = None) -> pd.DataFrame:
    """从 AlphaLab 读取价格宽表：index=日期，columns=vt_symbol"""
    lab = lab or get_lab()
    series: dict[str, pd.Series] = {}
    for vt_symbol in vt_symbols:
        path = lab.daily_path.joinpath(f"{vt_symbol}.parquet")
        if not path.exists():
            continue
        df: pl.DataFrame = pl.read_parquet(path, columns=["datetime", field]).sort("datetime")
        series[vt_symbol] = pd.Series(
            df[field].to_numpy(), index=pd.DatetimeIndex(df["datetime"].to_numpy()), name=vt_symbol
        )
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index()
