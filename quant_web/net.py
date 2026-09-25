"""
网络工具：国内站点优先直连（绕过系统代理），失败再走代理，带重试和浏览器 UA。

- get/get_json：线程安全。每个线程各自持有"直连"和"代理"两个 Session，
  通过显式 proxies 参数切换，不修改环境变量，可在下载线程池里放心使用。
- domestic_direct：临时把国内站点加进 NO_PROXY（修改进程环境变量），
  只用来包裹 akshare/baostock 这类内部自己发请求的库；可重入、计数加锁。
- call_with_fallback：给 akshare 函数用，直连→代理→直连轮换重试。
"""
import contextlib
import os
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any, TypeVar
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter


T = TypeVar("T")

# 国内行情/资讯网站：直连优先
DOMESTIC_HOSTS: list[str] = [
    "qq.com", "gtimg.cn", "eastmoney.com", "sina.com.cn", "sinajs.cn", "10jqka.com.cn",
    "cls.cn", "baostock.com", "legulegu.com", "sse.com.cn", "szse.cn", "bse.cn",
    "cninfo.com.cn", "cctv.com", "jin10.com", "hexun.com", "ifeng.com", "xueqiu.com",
    "csindex.com.cn", "chinabond.com.cn", "stats.gov.cn",
]

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DIRECT: dict[str, str | None] = {"http": None, "https": None}
_local = threading.local()
_env_lock = threading.Lock()
_env_depth: int = 0
_env_saved: str | None = None


def is_domestic(url: str) -> bool:
    host: str = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in DOMESTIC_HOSTS)


def system_proxies() -> dict[str, str]:
    """系统代理（环境变量 HTTP(S)_PROXY 优先，其次 Windows 注册表），没有配置时为空"""
    proxies: dict[str, str] = {
        k: v for k, v in urllib.request.getproxies_environment().items() if k in ("http", "https")
    }
    if not proxies and hasattr(urllib.request, "getproxies_registry"):
        registry: dict[str, str] = urllib.request.getproxies_registry()
        proxies = {k: v for k, v in registry.items() if k in ("http", "https")}
    return proxies


def _session() -> requests.Session:
    """当前线程专用 Session（trust_env=False，代理由调用方显式指定）"""
    session: requests.Session | None = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        session.headers["User-Agent"] = USER_AGENT
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _local.session = session
    return session


def _modes(url: str, retries: int) -> list[str]:
    """尝试顺序：国内站点 直连→代理→直连…；其他站点 代理→直连→代理…"""
    has_proxy: bool = bool(system_proxies())
    first, second = ("direct", "proxy") if is_domestic(url) else ("proxy", "direct")
    if not has_proxy:
        return ["direct"] * max(retries, 1)
    return [first if i % 2 == 0 else second for i in range(max(retries, 1))]


def get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10,
    encoding: str | None = None,
    retries: int = 3,
) -> requests.Response:
    """GET 请求：直连→代理→直连（国内站点），失败抛 ConnectionError（中文说明）"""
    errors: list[str] = []
    for attempt, mode in enumerate(_modes(url, retries)):
        proxies: dict = _DIRECT if mode == "direct" else system_proxies()
        try:
            r: requests.Response = _session().get(
                url, params=params, headers=headers, timeout=timeout, proxies=proxies
            )
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            if encoding:
                r.encoding = encoding
            return r
        except requests.HTTPError as e:
            status: int | None = e.response.status_code if e.response is not None else None
            errors.append(f"{'直连' if mode == 'direct' else '代理'}: {e}")
            if status is not None and 400 <= status < 500 and status != 429:
                break           # 4xx（除限流）重试无意义
        except requests.RequestException as e:
            errors.append(f"{'直连' if mode == 'direct' else '代理'}: {type(e).__name__}")
        time.sleep(min(0.5 * (2 ** attempt), 4))
    host: str = urlparse(url).hostname or url
    raise ConnectionError(f"无法访问 {host}（已重试 {len(errors)} 次）：" + "；".join(errors))


def get_text(url: str, **kw: Any) -> str:
    return get(url, **kw).text


def get_json(url: str, **kw: Any) -> Any:
    """GET 并解析 JSON；兼容 "var x = {...};" 这类 JSONP 包装"""
    r: requests.Response = get(url, **kw)
    try:
        return r.json()
    except ValueError:
        import json

        text: str = r.text
        starts: list[int] = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if not starts:
            raise ConnectionError(f"返回内容不是 JSON：{text[:80]}") from None
        body: str = text[min(starts):].rstrip().rstrip(";").rstrip(")")
        return json.loads(body)


@contextlib.contextmanager
def domestic_direct() -> Iterator[None]:
    """临时让国内站点绕过系统代理（设置 NO_PROXY）。可重入、多线程共享同一次设置。"""
    global _env_depth, _env_saved
    with _env_lock:
        if _env_depth == 0:
            _env_saved = os.environ.get("NO_PROXY")
            hosts: list[str] = [h for h in (_env_saved or "").split(",") if h] + DOMESTIC_HOSTS
            os.environ["NO_PROXY"] = ",".join(dict.fromkeys(hosts))
        _env_depth += 1
    try:
        yield
    finally:
        with _env_lock:
            _env_depth -= 1
            if _env_depth == 0:
                if _env_saved is None:
                    os.environ.pop("NO_PROXY", None)
                else:
                    os.environ["NO_PROXY"] = _env_saved


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    empty = getattr(value, "empty", None)       # pandas
    if isinstance(empty, bool):
        return empty
    is_empty = getattr(value, "is_empty", None)  # polars
    if callable(is_empty):
        return bool(is_empty())
    return False


def call_with_fallback(func: Callable[[], T], retries: int = 3, empty_ok: bool = True) -> T:
    """执行 akshare 之类的函数：直连→系统代理→直连 轮换重试，全部失败抛 ConnectionError。

    empty_ok=False 时返回空表也算失败（接口偶发返回空）。
    """
    errors: list[str] = []
    for attempt in range(max(retries, 1)):
        direct: bool = attempt % 2 == 0
        try:
            with contextlib.ExitStack() as stack:
                if direct:
                    stack.enter_context(domestic_direct())
                result: T = func()
            if empty_ok or not _is_empty(result):
                return result
            errors.append("返回数据为空")
        except Exception as e:  # noqa: BLE001  网络异常种类很多，统一重试
            errors.append(f"{'直连' if direct else '代理'}: {type(e).__name__} {str(e)[:60]}")
        if attempt < retries - 1:
            time.sleep(1 + attempt * 2)
    raise ConnectionError("数据接口暂时不可用：" + "；".join(errors))
