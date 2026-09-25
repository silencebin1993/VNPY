"""
股票列表：代码/名称、交易所与板块、上市日期、ST、行业。

- 代码+名称：akshare stock_info_a_code_name()（沪深京在市股票），失败则用腾讯快照补名称/沿用缓存；
- 上市日期、退市股票：baostock query_stock_basic()；行业：baostock query_stock_industry()（证监会行业）。
  baostock 很慢且会限流，放在后台线程里并设硬超时，超时就保留旧值；
- 上市日期兜底：腾讯K线第一根日期（history 回填时写入 first_bars.parquet）。
- ST：用"当前名称含 ST"判断（近似，历史 ST 状态没有免费数据源）。
"""
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from datetime import date, datetime

import polars as pl

from .. import config, net


# baostock 单个查询的硬超时（秒）：实测 query_stock_basic 约55秒、query_stock_industry 约65秒
BAOSTOCK_TIMEOUT: float = 90.0

SCHEMA: dict[str, pl.DataType] = {
    "code": pl.Utf8,
    "name": pl.Utf8,
    "exchange": pl.Utf8,
    "board": pl.Utf8,
    "list_date": pl.Date,
    "delist_date": pl.Date,
    "is_st": pl.Boolean,
    "industry": pl.Utf8,
    "status": pl.Int8,
}

BOARD_LABELS: dict[str, str] = {"main": "主板", "chinext": "创业板", "star": "科创板", "bj": "北交所"}

# baostock 用进程内全局连接，不能并发；超时放弃的后台线程可能还在跑，新的查询要排队
BAOSTOCK_LOCK = threading.Lock()


class _GuardedSocket:
    """包住 baostock 的连接：recv 设超时，服务器断开（recv 返回空）时抛异常。

    baostock 自己的 send_msg 在连接被服务器关闭后会无限循环等结束符（实测全市场连续查询约 1.4 万次后出现），
    线程永远不退出、一直占着 BAOSTOCK_LOCK。包一层后异常被 baostock 捕获，查询返回错误码，调用方可以放弃或重登。
    """

    def __init__(self, sock: object, timeout: float) -> None:
        self._sock = sock
        sock.settimeout(timeout)

    def recv(self, size: int) -> bytes:
        data: bytes = self._sock.recv(size)
        if not data:
            raise ConnectionError("baostock 服务器断开了连接")
        return data

    def __getattr__(self, name: str) -> object:
        return getattr(self._sock, name)


def guard_baostock_socket(timeout: float = 30.0) -> None:
    """bs.login() 成功后调用：给当前 baostock 连接加超时与断线检测"""
    try:
        from baostock.common import context
    except ImportError:
        return
    sock = getattr(context, "default_socket", None)
    if sock is not None and not isinstance(sock, _GuardedSocket):
        context.default_socket = _GuardedSocket(sock, timeout)


# ---------------------------------------------------------------- 代码工具

def exchange_of(code: str) -> str:
    """6→SSE；0、3→SZSE；4、8、92→BSE。B股（200、900）及其他代码抛 ValueError"""
    code = str(code).zfill(6)
    if code.startswith(("200", "900")):
        raise ValueError(f"不支持B股：{code}")
    if code.startswith("6"):
        return "SSE"
    if code.startswith(("0", "3")):
        return "SZSE"
    if code.startswith(("4", "8", "92")):
        return "BSE"
    raise ValueError(f"无法识别的股票代码：{code}")


def is_a_share(code: str) -> bool:
    code = str(code)
    if len(code) != 6 or not code.isdigit():
        return False
    try:
        exchange_of(code)
    except ValueError:
        return False
    return True


def board_of(code: str) -> str:
    """main 主板 / chinext 创业板（30开头）/ star 科创板（688、689）/ bj 北交所"""
    code = str(code).zfill(6)
    exchange: str = exchange_of(code)
    if exchange == "BSE":
        return "bj"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith("30"):          # 300、301，及新号段 302
        return "chinext"
    return "main"


_PREFIX: dict[str, str] = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}


def market_symbol(code: str) -> str:
    """腾讯/新浪行情代码：sh600000 / sz001216 / bj430047"""
    code = str(code).zfill(6)
    return _PREFIX[exchange_of(code)] + code


def bs_symbol(code: str) -> str:
    """baostock 代码：sh.600000"""
    code = str(code).zfill(6)
    return f"{_PREFIX[exchange_of(code)]}.{code}"


def vt_symbol(code: str) -> str:
    """vnpy 代码：001216.SZSE"""
    code = str(code).zfill(6)
    return f"{code}.{exchange_of(code)}"


def clean_name(name: str) -> str:
    """去掉名称里的空白（如"万  科Ａ"→"万科Ａ"）"""
    return re.sub(r"\s+", "", str(name or ""))


def name_is_st(name: str) -> bool:
    """名称含 ST（ST、*ST、SST…）或"退"（退市整理期/已退市股票，退市前都是 *ST）视为 ST"""
    text: str = clean_name(name).upper()
    return "ST" in text or "退" in text


def st_expr(name: pl.Expr) -> pl.Expr:
    """name_is_st 的向量化版本"""
    return name.str.replace_all(r"\s+", "").str.to_uppercase().str.contains("ST|退").fill_null(False)


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMA)


# ---------------------------------------------------------------- 读取

_cache: dict[str, tuple[tuple[float, int], pl.DataFrame]] = {}


def load_universe() -> pl.DataFrame:
    """读取股票列表缓存（按文件修改时间缓存在内存）；不存在时返回带列的空表"""
    path = config.STOCK_LAB.joinpath("universe.parquet")
    try:
        stat = path.stat()
    except OSError:
        return _empty()
    stamp: tuple[float, int] = (stat.st_mtime, stat.st_size)
    hit = _cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        df: pl.DataFrame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001  文件损坏时当作没有
        return _empty()
    for col, dtype in SCHEMA.items():
        if col not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dtype).alias(col))
    df = df.select([pl.col(c).cast(t) for c, t in SCHEMA.items()] + [c for c in df.columns if c not in SCHEMA])
    # 旧文件的 is_st 没把名称带"退"的股票算进去：读取时按名称补上
    df = df.with_columns((pl.col("is_st").fill_null(False) | st_expr(pl.col("name"))).alias("is_st"))
    _cache[str(path)] = (stamp, df)
    return df


def load_first_bars() -> pl.DataFrame:
    """腾讯K线第一根日期：code, first_date, complete（True 表示拿到了完整历史，first_date 即上市首日）"""
    path = config.STOCK_LAB.joinpath("first_bars.parquet")
    if not path.exists():
        return pl.DataFrame(schema={"code": pl.Utf8, "first_date": pl.Date, "complete": pl.Boolean})
    return pl.read_parquet(path)


def save_first_bars(records: pl.DataFrame) -> None:
    """合并写入 first_bars（同一代码取最早日期；complete 任一为真即真）"""
    if records.is_empty():
        return
    old: pl.DataFrame = load_first_bars()
    merged: pl.DataFrame = (
        pl.concat([old, records.select(["code", "first_date", "complete"])], how="vertical_relaxed")
        .group_by("code")
        .agg(pl.col("first_date").min(), pl.col("complete").any())
        .sort("code")
    )
    if old.height and merged.equals(old.select(merged.columns).sort("code")):
        return                      # 没有变化就不重写（避免文件时间变化让各处缓存失效）
    config.STOCK_LAB.mkdir(parents=True, exist_ok=True)
    merged.write_parquet(config.STOCK_LAB.joinpath("first_bars.parquet"))


def fill_list_dates(universe: pl.DataFrame | None = None, save: bool = True) -> pl.DataFrame:
    """用腾讯K线第一根日期补齐缺失的上市日期（baostock 不可用或北交所股票）"""
    uni: pl.DataFrame = load_universe() if universe is None else universe
    first: pl.DataFrame = load_first_bars().filter(pl.col("complete"))
    if uni.is_empty() or first.is_empty():
        return uni
    filled: pl.DataFrame = (
        uni.join(first.select(["code", pl.col("first_date").alias("_fd")]), on="code", how="left")
        .with_columns(pl.coalesce(["list_date", "_fd"]).alias("list_date"))
        .drop("_fd")
    )
    if save and not filled.equals(uni):     # 没有补上新日期就不重写文件（文件时间变化会让面板缓存失效）
        _write(filled)
    return filled


def _write(df: pl.DataFrame) -> None:
    config.STOCK_LAB.mkdir(parents=True, exist_ok=True)
    df.write_parquet(config.STOCK_LAB.joinpath("universe.parquet"))


# ---------------------------------------------------------------- 刷新

def _fetch_names() -> pl.DataFrame:
    """在市A股代码+名称（akshare，约10~25秒）"""
    import akshare as ak

    pdf = net.call_with_fallback(lambda: ak.stock_info_a_code_name(), empty_ok=False)
    df: pl.DataFrame = pl.from_pandas(pdf[["code", "name"]].astype(str))
    return df.with_columns(
        pl.col("code").str.zfill(6),
        pl.col("name").map_elements(clean_name, return_dtype=pl.Utf8),
    )


def _names_from_tencent(codes: list[str]) -> pl.DataFrame:
    """备用：用腾讯批量报价取名称"""
    rows: list[dict] = []
    for i in range(0, len(codes), 500):
        batch: list[str] = [market_symbol(c) for c in codes[i:i + 500]]
        text: str = net.get_text("https://qt.gtimg.cn/q=" + ",".join(batch), encoding="gbk", timeout=15)
        for line in text.split(";"):
            if "~" not in line:
                continue
            fields: list[str] = line.split('="', 1)[-1].split("~")
            if len(fields) > 2 and fields[2]:
                rows.append({"code": fields[2], "name": clean_name(fields[1])})
    return pl.DataFrame(rows, schema={"code": pl.Utf8, "name": pl.Utf8})


def _baostock_worker(result: dict, events: dict[str, threading.Event]) -> None:
    """后台线程：登录 baostock，依次查询基本信息与行业，结果写入 result"""
    import baostock as bs

    def fetch_rows(rs) -> list[list[str]]:
        rows: list[list[str]] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise ConnectionError(rs.error_msg)
        return rows

    try:
        with BAOSTOCK_LOCK, net.domestic_direct():
            lg = bs.login()
            if lg.error_code != "0":
                raise ConnectionError(f"baostock 登录失败：{lg.error_msg}")
            guard_baostock_socket(BAOSTOCK_TIMEOUT)
            try:
                result["basic"] = fetch_rows(bs.query_stock_basic())
                events["basic"].set()
                result["industry"] = fetch_rows(bs.query_stock_industry())
            finally:
                bs.logout()
    except Exception as e:  # noqa: BLE001  baostock 各种异常都只影响可选字段
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        events["basic"].set()
        events["industry"].set()


def _fetch_baostock(timeout: float = BAOSTOCK_TIMEOUT) -> dict:
    """带硬超时的 baostock 查询，返回 {"basic": rows|None, "industry": rows|None, "error": str|None}"""
    result: dict = {"basic": None, "industry": None, "error": None}
    events: dict[str, threading.Event] = {"basic": threading.Event(), "industry": threading.Event()}
    worker = threading.Thread(target=_baostock_worker, args=(result, events), daemon=True)
    worker.start()
    if not events["basic"].wait(timeout):
        result["error"] = f"baostock 基本信息查询超过 {timeout:.0f} 秒，已跳过"
        return dict(result)
    if not events["industry"].wait(timeout):
        result["error"] = f"baostock 行业查询超过 {timeout:.0f} 秒，已跳过"
    return dict(result)


def _parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date() if text else None
    except ValueError:
        return None


def _industry_label(text: str) -> str | None:
    """"C39计算机、通信和其他电子设备制造业" → "计算机、通信和其他电子设备制造业" """
    text = re.sub(r"^[A-Z]\d*", "", str(text or "")).strip()
    return text or None


def refresh_universe(progress: Callable[[float, str], None] | None = None) -> pl.DataFrame:
    """刷新股票列表并写入 UNIVERSE_FILE，返回新表"""
    def report(frac: float, msg: str) -> None:
        if progress:
            progress(frac, msg)

    old: pl.DataFrame = load_universe()
    warnings: list[str] = []

    report(0.0, "正在获取股票代码和名称…")
    try:
        names: pl.DataFrame = _fetch_names()
    except ConnectionError as e:
        warnings.append(f"股票列表接口失败：{e}")
        old_codes: list[str] = old.filter(pl.col("status") == 1)["code"].to_list()
        names = pl.DataFrame(schema={"code": pl.Utf8, "name": pl.Utf8})
        if old_codes:
            try:
                names = _names_from_tencent(old_codes)
            except ConnectionError as e2:
                warnings.append(f"腾讯行情备用也失败：{e2}")
        if names.is_empty():
            if old.is_empty():
                raise ConnectionError("获取股票列表失败，且本地没有缓存：" + "；".join(warnings)) from e
            report(1.0, "股票列表获取失败，沿用本地缓存")
            return old
    names = names.filter(pl.col("code").map_elements(is_a_share, return_dtype=pl.Boolean)).unique("code")

    report(0.2, "正在从 baostock 获取上市日期和行业（较慢，最多等待约3分钟）…")
    t0: float = time.time()
    bs_result: dict = _fetch_baostock()
    if bs_result["error"]:
        warnings.append(bs_result["error"])

    uni: pl.DataFrame = names.with_columns(pl.lit(1, dtype=pl.Int8).alias("status"))

    # baostock 基本信息：上市/退市日期，补充 HISTORY_START 之后退市的股票（减少幸存者偏差）
    if bs_result["basic"]:
        basic: pl.DataFrame = pl.DataFrame(
            bs_result["basic"], schema=["bs_code", "bs_name", "ipo", "out", "type", "bs_status"], orient="row"
        ).filter(pl.col("type") == "1").with_columns(
            pl.col("bs_code").str.slice(3).alias("code"),
            pl.col("ipo").map_elements(_parse_date, return_dtype=pl.Date).alias("list_date"),
            pl.col("out").map_elements(_parse_date, return_dtype=pl.Date).alias("delist_date"),
        )
        uni = uni.join(basic.select(["code", "list_date", "delist_date"]), on="code", how="left")
        start: date = _parse_date(config.HISTORY_START) or date(2019, 1, 1)
        delisted: pl.DataFrame = basic.filter(
            (pl.col("bs_status") == "0") & pl.col("delist_date").is_not_null()
            & (pl.col("delist_date") >= start) & ~pl.col("code").is_in(uni["code"].to_list())
        ).select(
            "code",
            pl.col("bs_name").map_elements(clean_name, return_dtype=pl.Utf8).alias("name"),
            pl.lit(0, dtype=pl.Int8).alias("status"),
            "list_date", "delist_date",
        ).filter(pl.col("code").map_elements(is_a_share, return_dtype=pl.Boolean))
        uni = pl.concat([uni, delisted], how="diagonal_relaxed")
    else:
        # 沿用旧的上市日期，旧表里的退市股票也保留
        keep_cols: list[str] = ["code", "list_date", "delist_date"]
        uni = uni.join(old.select(keep_cols), on="code", how="left") if not old.is_empty() else uni.with_columns(
            pl.lit(None, dtype=pl.Date).alias("list_date"), pl.lit(None, dtype=pl.Date).alias("delist_date")
        )
        if not old.is_empty():
            gone: pl.DataFrame = old.filter((pl.col("status") == 0) & ~pl.col("code").is_in(uni["code"].to_list()))
            uni = pl.concat([uni, gone.select(["code", "name", "status", "list_date", "delist_date"])],
                            how="diagonal_relaxed")

    # 行业
    if bs_result["industry"]:
        ind: pl.DataFrame = pl.DataFrame(
            bs_result["industry"], schema=["upd", "bs_code", "bs_name", "industry", "cls"], orient="row"
        ).select(
            pl.col("bs_code").str.slice(3).alias("code"),
            pl.col("industry").map_elements(_industry_label, return_dtype=pl.Utf8).alias("industry"),
        ).unique("code")
        uni = uni.join(ind, on="code", how="left")
    else:
        uni = uni.join(old.select(["code", "industry"]), on="code", how="left") if not old.is_empty() \
            else uni.with_columns(pl.lit(None, dtype=pl.Utf8).alias("industry"))
    if not old.is_empty() and bs_result["industry"]:
        # 新接口没给行业的沿用旧值
        uni = uni.join(old.select(["code", pl.col("industry").alias("_old_ind")]), on="code", how="left") \
            .with_columns(pl.coalesce(["industry", "_old_ind"]).alias("industry")).drop("_old_ind")
    if not old.is_empty():
        uni = uni.join(old.select(["code", pl.col("list_date").alias("_old_ld")]), on="code", how="left") \
            .with_columns(pl.coalesce(["list_date", "_old_ld"]).alias("list_date")).drop("_old_ld")

    report(0.9, "正在整理股票列表…")
    uni = uni.with_columns(
        pl.col("code").map_elements(exchange_of, return_dtype=pl.Utf8).alias("exchange"),
        pl.col("code").map_elements(board_of, return_dtype=pl.Utf8).alias("board"),
        pl.col("name").map_elements(name_is_st, return_dtype=pl.Boolean).alias("is_st"),
    )
    uni = uni.select([pl.col(c).cast(t) for c, t in SCHEMA.items()]).unique("code").sort("code")
    uni = fill_list_dates(uni, save=False)
    _write(uni)
    n_live: int = uni.filter(pl.col("status") == 1).height
    msg: str = f"股票列表已更新：在市 {n_live} 只，已退市 {uni.height - n_live} 只（用时 {time.time() - t0:.0f} 秒）"
    if warnings:
        msg += "；提示：" + "；".join(warnings)
    report(1.0, msg)
    return uni


# ---------------------------------------------------------------- 搜索

_initials_cache: dict[str, str] = {}


# GB2312 一级汉字（常用字）按拼音排序：各首字母的起始编码。没装 pypinyin 时用它估算首字母
_GB_INITIALS: list[tuple[int, str]] = [
    (0xB0A1, "a"), (0xB0C5, "b"), (0xB2C1, "c"), (0xB4EE, "d"), (0xB6EA, "e"), (0xB7A2, "f"),
    (0xB8C1, "g"), (0xB9FE, "h"), (0xBBF7, "j"), (0xBFA6, "k"), (0xC0AC, "l"), (0xC2E8, "m"),
    (0xC4C3, "n"), (0xC5B6, "o"), (0xC5BE, "p"), (0xC6DA, "q"), (0xC8BB, "r"), (0xC8F6, "s"),
    (0xCBFA, "t"), (0xCDDA, "w"), (0xCEF4, "x"), (0xD1B9, "y"), (0xD4D1, "z"),
]
_GB_LEVEL1_END: int = 0xD7F9
_POLYPHONES: dict[str, str] = {"银行": "yh", "重庆": "cq", "重工": "zg", "行业": "hy", "长沙": "cs", "厦门": "xm"}


def _gb_initial(ch: str) -> str:
    """单个字符的拼音首字母（GB2312 一级汉字；字母数字原样小写；其他字符返回空串）"""
    if ch.isascii():
        return ch.lower() if ch.isalnum() else ""
    try:
        raw: bytes = ch.encode("gb2312")
    except UnicodeEncodeError:
        return ""
    if len(raw) != 2:
        return ""
    code: int = raw[0] << 8 | raw[1]
    if code < _GB_INITIALS[0][0] or code > _GB_LEVEL1_END:
        return ""                   # 二级汉字按部首排序，无法估算
    letter: str = ""
    for start, first in _GB_INITIALS:
        if code < start:
            break
        letter = first
    return letter


def _initials(name: str) -> str:
    """名称拼音首字母，如 贵州茅台 → gzmt。装了 pypinyin 用它（多音字更准），否则按 GB2312 编码估算"""
    if name in _initials_cache:
        return _initials_cache[name]
    try:
        from pypinyin import Style, lazy_pinyin  # type: ignore[import-not-found]

        text: str = "".join(p[0] for p in lazy_pinyin(name, style=Style.FIRST_LETTER) if p).lower()
    except ImportError:
        plain: str = clean_name(name)
        for word, letters in _POLYPHONES.items():      # 股票名称里常见的多音字
            plain = plain.replace(word, letters)
        text = "".join(_gb_initial(ch) for ch in plain)
    _initials_cache[name] = text
    return text


def _norm(text: str) -> str:
    """全角转半角、去空白、小写，便于匹配"""
    return unicodedata.normalize("NFKC", clean_name(text)).lower()


def search(q: str, limit: int = 20) -> list[dict]:
    """按代码前缀 / 名称包含 / 拼音首字母搜索在市股票：[{"code","name","board","industry"}]"""
    q = _norm(q)
    if not q:
        return []
    uni: pl.DataFrame = load_universe()
    if uni.is_empty():
        return []
    uni = uni.sort(["status", "code"], descending=[True, False])
    scored: list[tuple[int, dict]] = []
    for row in uni.iter_rows(named=True):
        code: str = row["code"]
        name: str = _norm(row["name"] or "")
        if code == q:
            rank = 0
        elif code.startswith(q):
            rank = 1
        elif name.startswith(q):
            rank = 2
        elif q in name:
            rank = 3
        elif q.isascii() and q.isalpha() and _initials(row["name"] or "").startswith(q):
            rank = 4
        else:
            continue
        if row["status"] != 1:
            rank += 10
        scored.append((rank, {
            "code": code, "name": row["name"], "board": row["board"], "industry": row["industry"],
        }))
    scored.sort(key=lambda x: x[0])
    return [item for _, item in scored[:limit]]
