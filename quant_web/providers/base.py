"""
通用数据接口：数据源注册表（第三版）。

- **数据能力**（CAPABILITIES，如日线、实时行情、资金流、融资融券）各有一条数据源优先级链。
  按顺序尝试，前一个失败或返回空时自动换下一个。用户可在设置 providers.chains 里调整顺序，
  留空则用 DEFAULT_CHAINS。
- **数据源**是 DataProvider 的子类。它声明自己支持哪些能力，并实现 fetch_<能力>(**kwargs)，
  返回统一格式的 polars 表（列名和类型见 SCHEMAS，由 conform() 统一）。换数据源时上层代码不用改。
- **失败不打断页面**：registry().fetch() 返回 FetchResult(data, source, tried)。
  全部失败时抛 ProviderError，中文说明里列出每个源失败的原因。
- **接新数据源**（tushare、券商 QMT、自建数据库）：写一个 DataProvider 子类，register() 一下，
  再到"数据源"设置页把它排到前面即可。

现有模块（market/history 的日线面板、pools、fundamentals、news）仍按原来的方式工作。
这里的适配器只是把它们包装成统一接口，给新功能和"数据源"页面使用。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl


log = logging.getLogger("quant_web.providers")

# ---------------------------------------------------------------- 数据能力与统一格式

# 能力名 → (中文名, 白话说明)
CAPABILITIES: dict[str, tuple[str, str]] = {
    "daily_bars": ("个股日线", "每天的开高低收、成交量、成交额、换手率（不复权原始价格，另带昨收用于复权）"),
    "quotes": ("实时行情", "盘中最新价、涨跌幅、买一卖一等，交易时段几秒一更新"),
    "minute_bars": ("分钟线", "1/5/15/30/60 分钟 K 线，用来看盘中走势"),
    "index_bars": ("指数日线", "上证指数、沪深300 等指数的日线，用来判断大盘环境"),
    "index_members": ("指数成分股", "沪深300、中证500 等指数包含哪些股票"),
    "stock_list": ("股票列表", "全部 A 股的代码和名称"),
    "trade_calendar": ("交易日历", "哪天开市、哪天休市"),
    "industry": ("行业分类", "每只股票属于哪个行业"),
    "fund_flow": ("个股资金流向", "按单笔成交大小估算的超大单/大单/中单/小单净流入（各家算法不同，只能参考）"),
    "margin": ("融资融券", "借钱买股（融资）和借股票卖（融券）的余额，反映杠杆资金的态度"),
    "holder_count": ("股东户数", "一只股票有多少个股东；户数减少说明筹码在集中（可能有人在吸筹）"),
    "unlock": ("限售解禁", "哪天有多少原本不能卖的股票可以卖了（解禁多的股票可能有抛压）"),
    "pledge": ("股权质押", "大股东把股票抵押出去借钱的比例；比例高时股价大跌可能引发平仓风险"),
    "forecast": ("业绩预告", "公司提前公布的业绩预增/预减/预亏"),
    "holder_trades": ("股东增减持", "大股东、高管买入或卖出自家股票的公告"),
    "lhb": ("龙虎榜", "异常波动股票的买卖前五名营业部/机构"),
    "fundamentals": ("财务数据", "季度财报：营收、利润、负债等"),
    "news": ("新闻快讯", "财经快讯和个股新闻"),
}

_F = pl.Float64
_S = pl.Utf8
_D = pl.Date

# 统一格式（上层代码只依赖这些列；数据源可以多给列，conform() 会只保留这些并转换类型）
SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    # 价格单位元；volume 单位股；amount 单位元；turn 为百分数（1.5 = 1.5%）；价格为不复权原始价
    "daily_bars": {"date": _D, "code": _S, "open": _F, "high": _F, "low": _F, "close": _F, "preclose": _F,
                   "volume": _F, "amount": _F, "turn": _F},
    "index_bars": {"date": _D, "index": _S, "open": _F, "high": _F, "low": _F, "close": _F, "volume": _F, "amount": _F},
    "minute_bars": {"time": pl.Datetime("us"), "code": _S, "open": _F, "high": _F, "low": _F, "close": _F,
                    "volume": _F, "amount": _F},
    "quotes": {"code": _S, "name": _S, "price": _F, "preclose": _F, "open": _F, "high": _F, "low": _F,
               "volume": _F, "amount": _F, "pct": _F, "limit_up": _F, "limit_down": _F,
               "bid1": _F, "ask1": _F, "bid1_vol": _F, "ask1_vol": _F, "time": _S},
    "index_members": {"index": _S, "code": _S, "name": _S, "weight": _F},
    "stock_list": {"code": _S, "name": _S},
    "trade_calendar": {"date": _D},
    "industry": {"code": _S, "industry": _S},
    # 单位元；main = 超大单 + 大单；main_pct 为主力净流入占成交额的百分数
    "fund_flow": {"date": _D, "code": _S, "main_net": _F, "main_pct": _F, "super_net": _F, "big_net": _F,
                  "mid_net": _F, "small_net": _F, "net": _F},
    # rzye 融资余额(元) rzmre 融资买入额(元) rzche 融资偿还额(元) rqyl 融券余量(股) rqmcl 融券卖出量(股) rzrqye 两融余额(元)
    "margin": {"date": _D, "code": _S, "rzye": _F, "rzmre": _F, "rzche": _F, "rqyl": _F, "rqmcl": _F, "rzrqye": _F},
    # end_date 统计截止日；notice_date 公告日（回测只能用公告日之后的数据）；change_pct 户数变化百分数
    "holder_count": {"code": _S, "end_date": _D, "notice_date": _D, "holders": _F, "holders_prev": _F,
                     "change_pct": _F, "avg_shares": _F},
    # shares 解禁股数；float_ratio 占解禁前流通股的百分数；kind 限售股类型
    "unlock": {"code": _S, "date": _D, "shares": _F, "float_ratio": _F, "kind": _S},
    # pledge_ratio 质押股数占总股本的百分数
    "pledge": {"code": _S, "date": _D, "pledge_ratio": _F, "pledge_shares": _F},
    # period 报告期（如 2026-06-30）；kind 预增/预减/扭亏/首亏/续亏/续盈/略增/略减/不确定；change_* 变动幅度百分数
    "forecast": {"code": _S, "notice_date": _D, "period": _D, "kind": _S, "change_low": _F, "change_high": _F,
                 "summary": _S},
    # direction 增持/减持；shares 变动股数；ratio_float 占流通股百分数
    "holder_trades": {"code": _S, "notice_date": _D, "holder": _S, "direction": _S, "shares": _F,
                      "ratio_float": _F, "start": _D, "end": _D},
}


def conform(df: Any, capability: str) -> pl.DataFrame:
    """把数据源返回的表整理成统一格式：缺的列补空值、多的列丢掉、类型转换失败的值变空。
    capability 没有登记格式（lhb/fundamentals/news 沿用原模块格式）时原样返回。"""
    schema: dict[str, pl.DataType] | None = SCHEMAS.get(capability)
    if df is None:
        df = pl.DataFrame()
    if not isinstance(df, pl.DataFrame):
        df = pl.DataFrame(df)
    if schema is None:
        return df
    if df.height == 0:              # 空表：直接给统一格式的空表（只有字面量的 select 会变成一行空值）
        return pl.DataFrame(schema=schema)
    missing: list[pl.Expr] = [pl.lit(None, dtype=dt).alias(n) for n, dt in schema.items() if n not in df.columns]
    if missing:
        df = df.with_columns(missing)
    cols: list[pl.Expr] = []
    for name, dtype in schema.items():
        src: pl.DataType = df.schema[name]
        if src == dtype:
            cols.append(pl.col(name))
        elif dtype == pl.Date and src == pl.Utf8:
            cols.append(pl.col(name).str.strip_chars().str.slice(0, 10).str.replace_all("/", "-")
                        .str.to_date("%Y-%m-%d", strict=False).alias(name))
        elif dtype == pl.Date and isinstance(src, pl.Datetime):
            cols.append(pl.col(name).dt.date().alias(name))
        elif dtype == pl.Utf8:
            cols.append(pl.col(name).cast(pl.Utf8, strict=False).alias(name))
        else:
            cols.append(pl.col(name).cast(dtype, strict=False).alias(name))
    return df.select(cols)


def is_empty(data: Any) -> bool:
    if data is None:
        return True
    if isinstance(data, pl.DataFrame):
        return data.height == 0
    if isinstance(data, list | tuple | dict | set):
        return len(data) == 0
    return False


# ---------------------------------------------------------------- 数据源基类

class ProviderError(RuntimeError):
    """所有数据源都失败（消息已是中文，列出每个源的原因）"""


class Unavailable(RuntimeError):
    """这个数据源当前不能用（没安装、没填 token、不支持这个参数等），直接换下一个源"""


class DataProvider:
    """数据源基类。子类设置 name/label/capabilities，并实现 fetch_<能力>(**kwargs) -> pl.DataFrame"""

    name: str = ""
    label: str = ""
    description: str = ""
    capabilities: tuple[str, ...] = ()
    optional: bool = False          # 可选数据源（需要额外安装或 token）

    def available(self) -> tuple[bool, str]:
        """(能否使用, 不能用的中文原因)。检查安装/配置，不发网络请求"""
        return True, ""

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities and callable(getattr(self, f"fetch_{capability}", None))

    def fetch(self, capability: str, **kwargs: Any) -> Any:
        fn: Callable[..., Any] | None = getattr(self, f"fetch_{capability}", None)
        if capability not in self.capabilities or fn is None:
            raise Unavailable(f"{self.label} 不提供「{CAPABILITIES.get(capability, (capability,))[0]}」")
        return fn(**kwargs)


@dataclass
class FetchResult:
    data: Any                                   # 统一格式的 polars 表（或原模块格式）
    source: str                                 # 实际提供数据的数据源名称
    tried: list[tuple[str, str]] = field(default_factory=list)     # 先前失败的 (数据源, 原因)

    @property
    def warnings(self) -> list[str]:
        return [f"{name}：{why}" for name, why in self.tried]


@dataclass
class _Stat:
    ok: int = 0
    fail: int = 0
    last_ok: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    last_ms: float | None = None


# 内置默认顺序（设置里没配时用）；不在这里的能力按注册顺序取所有支持它的数据源
# "local" = 本机已下载的数据（日线面板、股票列表、龙虎榜/财务存档），最快、不联网；本地没有时才去网上取
DEFAULT_CHAINS: dict[str, list[str]] = {
    "daily_bars": ["local", "tencent", "akshare", "baostock"],
    "index_bars": ["tencent", "akshare"],
    "minute_bars": ["tencent", "sina"],
    "quotes": ["tencent"],
    "index_members": ["baostock", "akshare"],
    "stock_list": ["local", "akshare", "baostock"],
    "trade_calendar": ["local", "akshare", "baostock"],
    "industry": ["local", "baostock"],
    "fund_flow": ["sina"],
    "margin": ["exchange", "em_datacenter"],
    "holder_count": ["em_datacenter"],
    "unlock": ["em_datacenter"],
    "pledge": ["em_datacenter"],
    "forecast": ["em_datacenter"],
    "holder_trades": ["em_datacenter"],
    "lhb": ["local", "em_datacenter"],
    "fundamentals": ["local", "em_datacenter"],
    "news": ["builtin_news"],
}


def _describe(e: BaseException) -> str:
    text: str = str(e).strip() or type(e).__name__
    if isinstance(e, ImportError):
        return f"缺少组件 {getattr(e, 'name', '') or text}"
    if isinstance(e, TimeoutError):
        return "连接超时"
    return text[:200]


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, DataProvider] = {}
        self._stats: dict[tuple[str, str], _Stat] = {}
        self._lock = threading.Lock()
        self._chains_fn: Callable[[], dict[str, list[str]]] | None = None

    # ------------------------------------------------ 注册
    def register(self, provider: DataProvider) -> DataProvider:
        if not provider.name:
            raise ValueError("数据源必须有 name")
        with self._lock:
            self._providers[provider.name] = provider
        return provider

    def unregister(self, name: str) -> None:
        with self._lock:
            self._providers.pop(name, None)

    def get(self, name: str) -> DataProvider | None:
        return self._providers.get(name)

    def providers(self) -> list[DataProvider]:
        return list(self._providers.values())

    def set_chains_source(self, fn: Callable[[], dict[str, list[str]]] | None) -> None:
        """设置"用户自定义顺序"的读取函数（默认读 settings.providers.chains）；测试里可替换"""
        self._chains_fn = fn

    # ------------------------------------------------ 顺序
    def _user_chains(self) -> dict[str, list[str]]:
        if self._chains_fn is not None:
            return self._chains_fn() or {}
        try:
            from .. import settings as settings_mod
            return dict(settings_mod.load().providers.chains)
        except Exception:  # noqa: BLE001  设置读不到时用默认顺序
            return {}

    def capable(self, capability: str) -> list[str]:
        return [p.name for p in self._providers.values() if p.supports(capability)]

    def default_chain(self, capability: str) -> list[str]:
        capable: list[str] = self.capable(capability)
        preferred: list[str] = [n for n in DEFAULT_CHAINS.get(capability, []) if n in capable]
        return preferred + [n for n in capable if n not in preferred]

    def chain(self, capability: str) -> list[str]:
        """实际使用的顺序：用户配置过就按用户的（去掉不认识/不支持的），否则默认顺序"""
        if capability not in CAPABILITIES:
            raise ValueError(f"不认识的数据类型「{capability}」")
        user: list[str] = [n for n in self._user_chains().get(capability, []) if n in self._providers
                           and self._providers[n].supports(capability)]
        return list(dict.fromkeys(user)) if user else self.default_chain(capability)

    # ------------------------------------------------ 取数
    def _record(self, capability: str, name: str, ok: bool, ms: float, error: str | None = None) -> None:
        now: str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            st: _Stat = self._stats.setdefault((capability, name), _Stat())
            st.last_ms = round(ms, 1)
            if ok:
                st.ok += 1
                st.last_ok = now
            else:
                st.fail += 1
                st.last_error = error
                st.last_error_at = now

    def fetch(self, capability: str, *, chain: list[str] | None = None, empty_ok: bool = False,
              **kwargs: Any) -> FetchResult:
        """按优先级链取数据。empty_ok=False 时返回空表也算失败、换下一个源（最后仍为空则返回空结果，不报错）"""
        names: list[str] = chain if chain is not None else self.chain(capability)
        if not names:
            raise ProviderError(f"没有可用的数据源提供「{CAPABILITIES[capability][0]}」")
        tried: list[tuple[str, str]] = []
        empty: FetchResult | None = None
        for name in names:
            provider: DataProvider | None = self._providers.get(name)
            if provider is None:
                tried.append((name, "没有这个数据源"))
                continue
            ok, why = provider.available()
            if not ok:
                tried.append((provider.label or name, why or "当前不可用"))
                continue
            t0: float = time.perf_counter()
            try:
                data: Any = conform(provider.fetch(capability, **kwargs), capability)
            except Unavailable as e:
                tried.append((provider.label or name, str(e)))
                continue
            except Exception as e:  # noqa: BLE001  单个数据源失败只记录，换下一个
                ms: float = (time.perf_counter() - t0) * 1000
                reason: str = _describe(e)
                self._record(capability, name, False, ms, reason)
                log.info("数据源 %s 取「%s」失败：%s", name, capability, reason)
                tried.append((provider.label or name, reason))
                continue
            ms = (time.perf_counter() - t0) * 1000
            if is_empty(data) and not empty_ok:
                self._record(capability, name, False, ms, "返回空数据")
                tried.append((provider.label or name, "返回空数据"))
                if empty is None:
                    empty = FetchResult(data, name, [])
                continue
            self._record(capability, name, True, ms)
            return FetchResult(data, name, tried)
        if empty is not None:          # 所有源都只给了空数据：不算错误（可能确实没有数据）
            empty.tried = tried
            return empty
        detail: str = "；".join(f"{n}：{w}" for n, w in tried)
        raise ProviderError(f"「{CAPABILITIES[capability][0]}」所有数据源都取不到：{detail}")

    # ------------------------------------------------ 状态与测试
    def status(self) -> list[dict]:
        """数据源页面用：每种能力的当前顺序、可选数据源、各源可用性和最近成功/失败"""
        out: list[dict] = []
        user: dict[str, list[str]] = self._user_chains()
        for cap, (label, desc) in CAPABILITIES.items():
            capable: list[str] = self.capable(cap)
            sources: list[dict] = []
            for name in self.default_chain(cap):
                p: DataProvider = self._providers[name]
                ok, why = p.available()
                st: _Stat | None = self._stats.get((cap, name))
                sources.append({
                    "name": name, "label": p.label, "optional": p.optional, "available": ok, "reason": why,
                    "ok": st.ok if st else 0, "fail": st.fail if st else 0,
                    "last_ok": st.last_ok if st else None, "last_error": st.last_error if st else None,
                    "last_error_at": st.last_error_at if st else None, "last_ms": st.last_ms if st else None,
                })
            out.append({
                "capability": cap, "label": label, "description": desc, "chain": self.chain(cap) if capable else [],
                "customized": bool(user.get(cap)), "default_chain": self.default_chain(cap), "sources": sources,
            })
        return out

    def probe(self, capability: str, name: str) -> dict:
        """用一组小参数试取一次（"测试连接"按钮）：不写入任何文件"""
        provider: DataProvider | None = self._providers.get(name)
        if provider is None:
            raise ValueError(f"没有「{name}」这个数据源")
        if not provider.supports(capability):
            raise ValueError(f"{provider.label} 不提供「{CAPABILITIES.get(capability, (capability,))[0]}」")
        kwargs: dict = sample_kwargs(capability)
        t0: float = time.perf_counter()
        try:
            res: FetchResult = self.fetch(capability, chain=[name], empty_ok=True, **kwargs)
        except ProviderError as e:
            return {"ok": False, "ms": round((time.perf_counter() - t0) * 1000), "rows": 0, "error": str(e),
                    "params": kwargs, "sample": []}
        data: Any = res.data
        rows: int = data.height if isinstance(data, pl.DataFrame) else (len(data) if hasattr(data, "__len__") else 0)
        sample: list = data.head(3).to_dicts() if isinstance(data, pl.DataFrame) else []
        return {"ok": rows > 0, "ms": round((time.perf_counter() - t0) * 1000), "rows": rows,
                "error": None if rows else "请求成功但没有返回数据（可能是休市或参数没有数据）",
                "params": kwargs, "sample": sample}


def sample_kwargs(capability: str) -> dict:
    """"测试连接"用的小参数"""
    today: date = date.today()
    last_weekday: date = today - timedelta(days=1)
    while last_weekday.weekday() >= 5:
        last_weekday -= timedelta(days=1)
    return {
        "daily_bars": {"code": "600519", "start": today - timedelta(days=20), "end": today},
        "index_bars": {"index": "sh000300", "start": today - timedelta(days=20), "end": today},
        "minute_bars": {"code": "600519", "period": "5"},
        "quotes": {"codes": ["600519", "000001"]},
        "index_members": {"index": "hs300"},
        "fund_flow": {"code": "600519"},
        "margin": {"day": last_weekday},
        "holder_count": {},
        "unlock": {"start": today, "end": today + timedelta(days=30)},
        "pledge": {},
        "forecast": {},
        "holder_trades": {"start": today - timedelta(days=30), "end": today},
        "lhb": {"start": last_weekday, "end": last_weekday},
    }.get(capability, {})


# ---------------------------------------------------------------- 全局注册表

_REGISTRY: ProviderRegistry | None = None
_REG_LOCK = threading.Lock()


def registry() -> ProviderRegistry:
    """全局注册表（第一次调用时注册内置数据源）"""
    global _REGISTRY
    if _REGISTRY is None:
        with _REG_LOCK:
            if _REGISTRY is None:
                reg = ProviderRegistry()
                try:
                    from . import builtin
                    builtin.register_all(reg)
                except Exception:  # noqa: BLE001  内置适配器出错也要让注册表可用（页面会显示没有数据源）
                    log.exception("注册内置数据源失败")
                _REGISTRY = reg
    return _REGISTRY


def fetch(capability: str, **kwargs: Any) -> FetchResult:
    """快捷方式：registry().fetch(...)"""
    return registry().fetch(capability, **kwargs)
