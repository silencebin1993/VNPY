"""
基本面（财报，时点一致）。

数据源：
- 主：东方财富数据中心 datacenter-web，按报告期一次取全市场（每期约 12 页×3 张表，无逐只限流）：
  业绩报表 RPT_LICO_FN_CPD（每股收益、营业总收入及同比、归母净利润及同比、每股净资产、加权 ROE、
  每股经营现金流、销售毛利率、公告日期）、资产负债表 RPT_DMSK_FN_BALANCE（资产负债率）、
  利润表 RPT_DMSK_FN_INCOME（扣非归母净利润）。首次下载 HISTORY_START 前两年起约 38 个报告期，实测约 1 分钟；
  之后只重下"没下载过"或"下载时仍在披露期内"的报告期（几十秒）。
- 备用：同花顺 F10 财务摘要页 basic.10jqka.com.cn/new/{code}/finance.html（逐只；按 IP 限流，
  实测连续约 700 次请求后封禁约 15 分钟）。东方财富整体不可用时才用；info.py 查单只股票也用 fetch_one。

单位约定：金额（revenue、net_profit 等）为 元；每股指标（eps、bvps、ocfps）为 元/股；
比率与增长率（roe、revenue_yoy、net_profit_yoy、debt_ratio、gross_margin、net_margin）为 百分数（12.5 表示 12.5%）。
roe、eps、revenue、net_profit 均为"报告期累计值"（一季报=1季度，半年报=上半年…），不是年化值；
eps_ttm / net_profit_ttm / revenue_ttm 为滚动12个月（本期累计 + 上年年报 - 上年同期累计）。
net_margin：东方财富来源为 归母净利润/营业总收入（同花顺为 净利润/营业收入，略有差别）。
source：数据来源 "em" / "ths"；notice_date：实际公告日期（东方财富"最新公告日期"，同花顺来源为空），仅供参考。

可用日期 avail_date：按法定披露截止日（一季报 4/30、半年报 8/31、三季报 10/31、年报 次年4/30）的**次日**，
因为报告常在截止日收盘后才发布，这样 T 日收盘后计算特征时绝不会用到当天晚上才公布的数据。
"""
import html
import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import polars as pl

from .. import config, net
from . import universe as uni_mod


EM_URL: str = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_PAGE_SIZE: int = 500         # 数据中心每页上限
EM_A_SHARES: str = '(SECURITY_TYPE_CODE in ("058001001","058001008"))'     # 沪深京 A 股（不含新三板、B 股）
# (报表, 报告期字段, {东方财富字段: 我们的列})
EM_REPORTS: list[tuple[str, str, dict[str, str]]] = [
    ("RPT_LICO_FN_CPD", "REPORTDATE", {
        "BASIC_EPS": "eps", "BPS": "bvps", "WEIGHTAVG_ROE": "roe", "TOTAL_OPERATE_INCOME": "revenue",
        "YSTZ": "revenue_yoy", "PARENT_NETPROFIT": "net_profit", "SJLTZ": "net_profit_yoy",
        "XSMLL": "gross_margin", "MGJYXJJE": "ocfps", "NOTICE_DATE": "notice_date",
    }),
    ("RPT_DMSK_FN_BALANCE", "REPORT_DATE", {"DEBT_ASSET_RATIO": "debt_ratio"}),
    ("RPT_DMSK_FN_INCOME", "REPORT_DATE", {"DEDUCT_PARENT_NETPROFIT": "net_profit_deducted"}),
]
REOPEN_DAYS: int = 45           # 披露截止日后这么多天内下载的报告期视为可能不全（补发、更正），过期后再下一次
PERIOD_FLUSH: int = 8           # 每下载这么多个报告期合并写盘一次

URL: str = "https://basic.10jqka.com.cn/new/{code}/finance.html"
FLUSH_EVERY: int = 500          # 同花顺逐只下载：每这么多只合并写盘一次（中断可续传）
# 同花顺按 IP 限流：实测约 30 次/秒 持续 20 秒、或 4 次/秒 约 750 次就被封（HTTP 403 "Nginx forbidden"，
# 直连和代理同一出口 IP），约 15 分钟后解除。
MIN_INTERVAL: float = 0.25      # 全部线程合计的请求间隔下限（秒）
BLOCK_ABORT: int = 3            # 连续这么多次 403 视为被封：停止本次下载（已下载的已保存，下次继续）


class Blocked(ConnectionError):
    """同花顺暂时封禁了本机 IP"""


_throttle_lock = threading.Lock()
_next_at: float = 0.0


def _throttle(interval: float = MIN_INTERVAL) -> None:
    """全局节流：多线程合计每 interval 秒最多发出一个请求"""
    global _next_at
    with _throttle_lock:
        now: float = time.monotonic()
        at: float = max(now, _next_at)
        _next_at = at + interval
    if at > now:
        time.sleep(at - now)

# 同花顺科目 → 我们的列名
FIELD_MAP: dict[str, str] = {
    "基本每股收益": "eps",
    "每股净资产": "bvps",
    "净资产收益率": "roe",
    "营业总收入": "revenue",
    "营业总收入同比增长率": "revenue_yoy",
    "净利润": "net_profit",
    "净利润同比增长率": "net_profit_yoy",
    "资产负债率": "debt_ratio",
    "销售毛利率": "gross_margin",
    "扣非净利润": "net_profit_deducted",
    "销售净利率": "net_margin",
    "每股经营现金流": "ocfps",
}

VALUE_COLUMNS: list[str] = [
    "eps", "eps_ttm", "bvps", "roe", "revenue", "revenue_yoy", "net_profit", "net_profit_yoy",
    "debt_ratio", "gross_margin", "net_profit_deducted", "net_margin", "ocfps", "net_profit_ttm", "revenue_ttm",
]

SCHEMA: dict[str, pl.DataType] = {
    "code": pl.Utf8,
    "report_date": pl.Date,
    "avail_date": pl.Date,
    **{c: pl.Float64 for c in VALUE_COLUMNS},
    "notice_date": pl.Date,
    "source": pl.Utf8,
    "fetched_at": pl.Date,
}

_UNITS: list[tuple[str, float]] = [("万亿", 1e12), ("亿", 1e8), ("万", 1e4)]


def _file():
    return config.STOCK_LAB.joinpath("fundamentals.parquet")


def parse_value(text: object) -> float | None:
    """"1.23亿"→1.23e8，"5442.58万"→5.44258e7，"12.5%"→12.5，"False"/"--"/""→None"""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, int | float):
        return float(text)
    s: str = str(text).strip().replace(",", "")
    if not s or s in ("False", "True", "--", "-", "nan", "None"):
        return None
    factor: float = 1.0
    if s.endswith("%"):
        s = s[:-1]
    else:
        for suffix, mult in _UNITS:
            if s.endswith(suffix):
                s, factor = s[: -len(suffix)], mult
                break
        if s.endswith("元"):
            s = s[:-1]
    try:
        return float(s) * factor
    except ValueError:
        return None


def avail_date_of(report_date: date) -> date:
    """法定披露截止日的次日"""
    md: tuple[int, int] = (report_date.month, report_date.day)
    if md == (3, 31):
        deadline = date(report_date.year, 4, 30)
    elif md == (6, 30):
        deadline = date(report_date.year, 8, 31)
    elif md == (9, 30):
        deadline = date(report_date.year, 10, 31)
    elif md == (12, 31):
        deadline = date(report_date.year + 1, 4, 30)
    else:   # 非标准报告期：保守地按 4 个月后
        deadline = report_date + timedelta(days=122)
    return deadline + timedelta(days=1)


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMA)


def fetch_one(code: str) -> pl.DataFrame:
    """下载单只股票的全部报告期财务摘要；被同花顺封禁时抛 Blocked"""
    _throttle()
    try:
        r = net.get(URL.format(code=code), headers={"Referer": "https://basic.10jqka.com.cn/"},
                    timeout=15, encoding="utf-8", retries=2)
    except ConnectionError as e:
        if "403" in str(e):
            raise Blocked(f"同花顺暂时限制了本机访问（HTTP 403）：{code}") from e
        raise
    m = re.search(r'<p[^>]*id="main"[^>]*>(.*?)</p>', r.text, re.S)
    if not m:
        raise ConnectionError(f"同花顺财务页面格式变化或无数据：{code}")
    data: dict = json.loads(html.unescape(m.group(1)))
    titles: list[str] = [t[0] if isinstance(t, list) else str(t) for t in data.get("title", [])][1:]
    report: list[list] = data.get("report") or []
    if len(report) < 2:
        return _empty()
    dates: list[str] = report[0]
    rows: list[dict] = []
    min_year: int = int(config.HISTORY_START[:4]) - 2     # 更早的报告期用不上（TTM 需要前一年）
    for j, d in enumerate(dates):
        try:
            rd: date = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if rd.year < min_year:
            continue
        row: dict = {"code": code, "report_date": rd, "avail_date": avail_date_of(rd)}
        for title, values in zip(titles, report[1:], strict=False):
            col: str | None = FIELD_MAP.get(title)
            if col and j < len(values):
                row[col] = parse_value(values[j])
        rows.append(row)
    if not rows:
        return _empty()
    df = pl.DataFrame(rows).with_columns(
        pl.lit("ths").alias("source"), pl.lit(datetime.now(config.CHINA_TZ).date()).alias("fetched_at")
    )
    for col, dtype in SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    return add_ttm(df.select([pl.col(c).cast(t) for c, t in SCHEMA.items()]))


def add_ttm(df: pl.DataFrame) -> pl.DataFrame:
    """滚动12个月：本期累计 + 上年年报 - 上年同期累计（年报即全年值）"""
    if df.is_empty():
        return df
    base = df.select(["code", "report_date", "eps", "net_profit", "revenue"]).with_columns(
        pl.col("report_date").dt.year().alias("_y"),
        pl.col("report_date").dt.month().alias("_m"),
    )
    annual = base.filter(pl.col("_m") == 12).select(
        "code", (pl.col("_y") + 1).alias("_y"),
        pl.col("eps").alias("_eps_a"), pl.col("net_profit").alias("_np_a"), pl.col("revenue").alias("_rev_a"),
    )
    same = base.select(
        "code", (pl.col("_y") + 1).alias("_y"), "_m",
        pl.col("eps").alias("_eps_l"), pl.col("net_profit").alias("_np_l"), pl.col("revenue").alias("_rev_l"),
    )
    out = (
        df.with_columns(pl.col("report_date").dt.year().alias("_y"), pl.col("report_date").dt.month().alias("_m"))
        .join(annual, on=["code", "_y"], how="left")
        .join(same, on=["code", "_y", "_m"], how="left")
    )
    exprs: list[pl.Expr] = []
    for col, a, last in (("eps", "_eps_a", "_eps_l"), ("net_profit", "_np_a", "_np_l"),
                         ("revenue", "_rev_a", "_rev_l")):
        exprs.append(
            pl.when(pl.col("_m") == 12).then(pl.col(col))
            .otherwise(pl.col(col) + pl.col(a) - pl.col(last)).alias(f"{col}_ttm")
        )
    out = out.with_columns(exprs)
    return out.select([pl.col(c).cast(t) for c, t in SCHEMA.items()]).sort(["code", "report_date"])


def load_fundamentals() -> pl.DataFrame:
    path = _file()
    if not path.exists():
        return _empty()
    df: pl.DataFrame = pl.read_parquet(path)
    for col, dtype in SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    return df.select([pl.col(c).cast(t) for c, t in SCHEMA.items()])


def report_periods(today: date | None = None) -> list[date]:
    """HISTORY_START 前两年起、到今天为止已结束的全部季度报告期（TTM 需要前一年的数据）"""
    today = today or datetime.now(config.CHINA_TZ).date()
    periods: list[date] = []
    for year in range(int(config.HISTORY_START[:4]) - 2, today.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            if date(year, month, day) < today:
                periods.append(date(year, month, day))
    return periods


def _em_rows(report: str, date_field: str, period: date, fields: list[str]) -> list[dict]:
    """东方财富数据中心：某张报表某个报告期的全部 A 股行（分页，每页 500）"""
    rows: list[dict] = []
    page: int = 1
    while True:
        j = net.get_json(EM_URL, params={
            "reportName": report, "columns": ",".join(["SECURITY_CODE", *fields]),
            "filter": f"{EM_A_SHARES}({date_field}='{period.isoformat()}')",
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            "pageSize": str(EM_PAGE_SIZE), "pageNumber": str(page),
        }, timeout=20)
        result = j.get("result") if isinstance(j, dict) else None
        if not result:
            if isinstance(j, dict) and j.get("code") == 9201:     # "返回数据为空"：该期还没有任何公司披露
                return rows
            raise ConnectionError(f"东方财富数据中心返回异常：{str(j)[:80]}")
        rows.extend(result.get("data") or [])
        if page >= int(result.get("pages") or 1):
            return rows
        page += 1


def _em_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return parse_value(value) if isinstance(value, str) else None


def fetch_period(period: date) -> pl.DataFrame:
    """下载一个报告期的全市场财报（三张表按代码合并），列同 SCHEMA，TTM 列为空（合并后统一计算）"""
    merged: dict[str, dict] = {}
    for report, date_field, fields in EM_REPORTS:
        for r in _em_rows(report, date_field, period, list(fields)):
            code: str = str(r.get("SECURITY_CODE") or "")
            if not uni_mod.is_a_share(code):
                continue
            row: dict = merged.setdefault(code, {"code": code})
            for src, col in fields.items():
                if col == "notice_date":
                    text: str = str(r.get(src) or "")[:10]
                    row[col] = datetime.strptime(text, "%Y-%m-%d").date() if len(text) == 10 else None
                else:
                    row[col] = _em_value(r.get(src))
    if not merged:
        return _empty()
    schema: dict[str, pl.DataType] = {"code": pl.Utf8, **{c: SCHEMA[c] for r in EM_REPORTS for c in r[2].values()}}
    df: pl.DataFrame = pl.DataFrame([{c: row.get(c) for c in schema} for row in merged.values()], schema=schema,
                                    orient="row")
    for col, dtype in SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    return df.with_columns(
        pl.lit(period).alias("report_date"),
        pl.lit(avail_date_of(period)).alias("avail_date"),
        pl.when(pl.col("revenue") > 0).then(pl.col("net_profit") / pl.col("revenue") * 100).alias("net_margin"),
        pl.lit("em").alias("source"),
        pl.lit(datetime.now(config.CHINA_TZ).date()).alias("fetched_at"),
    ).select([pl.col(c).cast(t) for c, t in SCHEMA.items()])


def _save(df: pl.DataFrame) -> None:
    """去重（同一股票同一报告期保留后者）、重算 TTM 后原子写盘（Windows 上文件正被读取时稍等重试）"""
    df = df.unique(["code", "report_date"], keep="last", maintain_order=True)
    config.STOCK_LAB.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    add_ttm(df).write_parquet(tmp)
    for k in range(10):
        try:
            tmp.replace(_file())
            return
        except PermissionError:
            if k == 9:
                raise
            time.sleep(0.5 * (k + 1))


def update_fundamentals(
    progress: Callable[[float, str], None] | None = None,
    codes: list[str] | None = None,
    workers: int = 6,
    max_age_days: int = 7,
) -> dict:
    """更新财报数据：东方财富按报告期批量下载全市场；整体不可用时退回同花顺逐只下载。

    - 没下载过的报告期都下载（首次约 38 期，约 1 分钟）；
    - 下载时还在披露期内（截止日后 REOPEN_DAYS 天内）的报告期，距上次下载满 max_age_days 天就重下；
    - max_age_days<=0 或显式给出 codes 时全部报告期重下；codes 只决定合并哪些股票。
    返回 {"updated": 更新的股票数, "failed": 失败的报告期/代码, "skipped": 跳过的报告期数, "rows", "seconds",
          "periods": 下载的报告期数, "source": "em"/"ths", "blocked"}
    """
    t0: float = time.time()

    def report(frac: float, msg: str) -> None:
        if progress:
            progress(min(max(frac, 0.0), 1.0), msg)

    today: date = datetime.now(config.CHINA_TZ).date()
    periods: list[date] = report_periods(today)
    keep: list[str] | None = list(dict.fromkeys(str(c).zfill(6) for c in codes)) if codes is not None else None
    old: pl.DataFrame = load_fundamentals()
    todo: list[date] = periods
    if keep is None and max_age_days > 0 and not old.is_empty():
        seen: dict[date, date] = dict(
            old.filter(pl.col("source") == "em").group_by("report_date").agg(pl.col("fetched_at").min()).rows()
        )

        def need(p: date) -> bool:
            fetched: date | None = seen.get(p)
            if fetched is None:
                return True
            settled: bool = fetched >= avail_date_of(p) + timedelta(days=REOPEN_DAYS)
            return not settled and (today - fetched).days >= max_age_days

        todo = [p for p in periods if need(p)]
    skipped: int = len(periods) - len(todo)
    if not todo:
        report(1.0, f"财报数据已是最新（{skipped} 个报告期无需重下）")
        return {"updated": 0, "failed": [], "skipped": skipped, "rows": 0, "seconds": round(time.time() - t0, 1),
                "periods": 0, "source": "em", "blocked": False}

    report(0.0, f"正在从东方财富下载 {len(todo)} 个报告期的财报数据…")
    frames: list[pl.DataFrame] = []
    updated: set[str] = set()
    rows: int = 0
    done: int = 0

    def flush() -> None:
        nonlocal frames, rows
        parts: list[pl.DataFrame] = [f for f in frames if not f.is_empty()]
        frames = []
        if not parts:
            return
        new: pl.DataFrame = pl.concat(parts, how="vertical_relaxed")
        if keep is not None:
            new = new.filter(pl.col("code").is_in(keep))
        cur: pl.DataFrame = load_fundamentals()
        _save(pl.concat([cur.join(new.select(["code", "report_date"]), on=["code", "report_date"], how="anti"), new],
                        how="vertical_relaxed").sort(["code", "report_date"]))
        updated.update(new["code"].unique().to_list())
        rows += new.height

    def run(batch: list[date], n_workers: int) -> list[date]:
        nonlocal done
        failed_periods: list[date] = []
        with ThreadPoolExecutor(max_workers=max(1, n_workers)) as pool:
            futures = {pool.submit(fetch_period, p): p for p in batch}
            for fut in as_completed(futures):
                period: date = futures[fut]
                try:
                    frames.append(fut.result())
                    done += 1
                except Exception:  # noqa: BLE001  单期失败稍后重试
                    failed_periods.append(period)
                if len(frames) >= PERIOD_FLUSH:
                    flush()
                report(0.95 * done / len(todo), f"正在下载财报数据 {done}/{len(todo)} 个报告期（{period}）")
        return failed_periods

    failed: list[date] = run(todo, min(workers, 4))
    if failed:
        time.sleep(3)
        failed = run(sorted(failed), 1)
    flush()
    if failed and not done:
        report(0.02, "东方财富财报接口暂时不可用，改用同花顺逐只下载（较慢，可能被限流）…")
        return {**_update_ths(progress, codes, workers, max_age_days), "source": "ths", "periods": 0}
    seconds: float = round(time.time() - t0, 1)
    report(1.0, f"财报数据更新完成：{done} 个报告期、{len(updated)} 只股票、{rows} 条，用时 {seconds:.0f} 秒"
           + (f"；{len(failed)} 个报告期失败（下次重试）" if failed else ""))
    return {"updated": len(updated), "failed": [p.isoformat() for p in failed], "skipped": skipped, "rows": rows,
            "seconds": seconds, "periods": done, "source": "em", "blocked": False}


def _update_ths(
    progress: Callable[[float, str], None] | None = None,
    codes: list[str] | None = None,
    workers: int = 6,
    max_age_days: int = 7,
) -> dict:
    """备用：同花顺逐只下载财报摘要并合并保存；max_age_days 天内下载过的股票跳过（codes 显式给出时不跳过）。

    同花顺按 IP 限流，请求经全局节流；连续被拒（403）时停止本次下载并返回 blocked=True。
    返回 {"updated","failed","skipped","rows","seconds","blocked"}
    """
    t0: float = time.time()

    def report(frac: float, msg: str) -> None:
        if progress:
            progress(min(max(frac, 0.0), 1.0), msg)

    old: pl.DataFrame = load_fundamentals()
    explicit: bool = codes is not None
    if codes is None:
        uni: pl.DataFrame = uni_mod.load_universe()
        codes = uni.filter(pl.col("status") == 1)["code"].to_list()
    codes = list(dict.fromkeys(str(c).zfill(6) for c in codes))
    skipped: int = 0
    if not explicit and not old.is_empty() and max_age_days > 0:
        cutoff: date = datetime.now(config.CHINA_TZ).date() - timedelta(days=max_age_days)
        fresh: set[str] = set(old.filter(pl.col("fetched_at") >= cutoff)["code"].unique().to_list())
        skipped = sum(1 for c in codes if c in fresh)
        codes = [c for c in codes if c not in fresh]

    frames: list[pl.DataFrame] = []
    frame_codes: list[str] = []
    done: int = 0
    failed: list[str] = []
    total: int = len(codes)
    last_report: float = 0.0
    rows: int = 0

    def flush() -> None:
        """把已下载的部分合并写盘（中断后下次运行会按 fetched_at 跳过这些股票）"""
        nonlocal frames, frame_codes, rows
        if not frame_codes:
            return
        parts: list[pl.DataFrame] = [f for f in frames if not f.is_empty()]
        new: pl.DataFrame = pl.concat(parts, how="vertical_relaxed") if parts else _empty()
        _save(pl.concat([load_fundamentals().filter(~pl.col("code").is_in(frame_codes)), new],
                        how="vertical_relaxed"))
        rows += new.height
        frames, frame_codes = [], []

    stop = threading.Event()
    state_lock = threading.Lock()
    blocked_run: list[int] = [0]

    def task(code: str) -> pl.DataFrame:
        """在下载线程里数连续 403：够 BLOCK_ABORT 次立即叫停，排队中的股票不再发请求"""
        if stop.is_set():
            raise Blocked("已暂停")
        try:
            df: pl.DataFrame = fetch_one(code)
        except Blocked:
            with state_lock:
                blocked_run[0] += 1
                if blocked_run[0] >= BLOCK_ABORT:
                    stop.set()
            raise
        with state_lock:
            blocked_run[0] = 0
        return df

    def run(batch: list[str], n_workers: int) -> list[str]:
        nonlocal last_report, done
        retry: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, n_workers)) as pool:
            futures = {pool.submit(task, c): c for c in batch}
            for fut in as_completed(futures):
                code: str = futures[fut]
                try:
                    df: pl.DataFrame = fut.result()
                    frames.append(df)
                    frame_codes.append(code)
                    done += 1
                except Exception:  # noqa: BLE001  单只失败不影响其他
                    retry.append(code)
                if len(frame_codes) >= FLUSH_EVERY:
                    flush()
                if time.time() - last_report > 1.0:
                    last_report = time.time()
                    report(0.9 * done / max(total, 1), f"正在下载财报摘要 {done}/{total}（失败 {len(retry)}）")
        return retry

    if codes:
        retry: list[str] = run(codes, workers)
        failed = retry
        if retry and not stop.is_set():
            time.sleep(3)
            failed = run(retry, max(1, workers // 3))
    flush()
    seconds: float = round(time.time() - t0, 1)
    report(1.0, f"财报摘要更新完成：{done} 只，{rows} 条"
           + (f"；{len(failed)} 只失败" if failed else "") + (f"；{skipped} 只近期已更新，跳过" if skipped else "")
           + ("；同花顺暂时限制了访问，已下载的已保存，其余请过一段时间再更新（会自动接着下载）"
              if stop.is_set() else ""))
    return {"updated": done, "failed": failed, "skipped": skipped, "rows": rows, "seconds": seconds,
            "blocked": stop.is_set()}


def asof_join(df: pl.DataFrame, fund: pl.DataFrame) -> pl.DataFrame:
    """按 code + date >= avail_date 做 as-of 连接（取当时已公布的最新一期），杜绝未来数据。

    同一天可用的多期（如年报与一季报都在 4/30 截止）取报告期最新的一期。保持 df 原有行顺序。
    """
    if fund.is_empty():
        extra: dict[str, pl.DataType] = {c: t for c, t in SCHEMA.items()
                                      if c not in ("code", "fetched_at", "source")}
        return df.with_columns([pl.lit(None, dtype=t).alias(c) for c, t in extra.items() if c not in df.columns])
    right: pl.DataFrame = (
        fund.drop([c for c in ["fetched_at", "source"] if c in fund.columns])
        .sort(["code", "avail_date", "report_date"])
        .unique(["code", "avail_date"], keep="last", maintain_order=True)
        .sort("avail_date")
    )
    right = right.with_columns(pl.col("avail_date").alias("_on"))
    right = right.drop([c for c in right.columns if c in df.columns and c != "code"])
    left: pl.DataFrame = df.with_row_index("_row").sort("date")
    joined: pl.DataFrame = left.join_asof(right, left_on="date", right_on="_on", by="code", strategy="backward",
                                          check_sortedness=False)
    return joined.sort("_row").drop(["_row", "_on"])
