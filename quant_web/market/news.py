"""
快讯与消息热度：财联社电报 + 东方财富快讯 + 新浪7x24 合并去重，打政策标签，识别提到的股票。

- 三个来源直接请求各自的公开接口（即 akshare stock_info_global_cls / _em / _sina 的同一接口，可翻页取满24小时），
  直连失败再退回 akshare 函数；任何一个来源失败都不影响其他来源。
- 快讯滚动保存在 CACHE_DIR/news_store.parquet（保留3天），60 秒内重复调用直接读缓存。
- 股票识别：来源自带的股票标签（财联社 stock_list、东财 stockList、新浪 ext.stocks）+ 按股票简称匹配正文。
  两个字的简称（如"柳工""万科"）和容易与普通词混淆的简称（如"机器人""太阳能""陆家嘴"）只在
  "简称（代码）"、"简称："、"简称股份/集团/公司" 或正文里出现代码时才算。
- news_heat：近 N 小时每只股票被提到的条数、相关政策消息条数（政策消息提到了个股，或提到了它所属行业），
  news_z = 热度分相对全部股票的标准化值（以中位数为 0，没有消息或没有数据时为 0）。
  消息面只有实时数据，没有历史，不参与训练和回测。
"""
import hashlib
import json
import logging
import math
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import polars as pl

from .. import config, net
from . import universe as uni_mod


logger = logging.getLogger(__name__)

POLICY_KEYWORDS: dict[str, list[str]] = {
    "国家政策": [
        "国务院", "中共中央", "党中央", "中央经济工作会议", "政治局", "全国人大", "人大常委会", "全国政协",
        "国常会", "常务会议", "政府工作报告", "五年规划", "十五五", "顶层设计", "总书记", "国家主席", "国务院总理",
        "中办", "国办", "指导意见", "实施意见", "行动方案", "行动计划", "若干措施", "若干意见", "纲要",
        "国家战略", "新质生产力", "高质量发展", "扩大内需", "统一大市场", "深化改革", "两会", "中央财经委",
    ],
    "部委": [
        "发改委", "发展改革委", "工信部", "工业和信息化部", "财政部", "商务部", "科技部", "国资委", "教育部",
        "交通运输部", "农业农村部", "自然资源部", "生态环境部", "住建部", "住房和城乡建设部", "水利部",
        "人社部", "民政部", "文旅部", "文化和旅游部", "卫健委", "卫生健康委", "医保局", "药监局",
        "市场监管总局", "网信办", "国家数据局", "国家能源局", "国防科工局", "中国民航局", "海关总署", "税务总局",
        "国家统计局", "知识产权局", "应急管理部", "外汇局", "广电总局", "体育总局", "邮政局", "国铁集团",
    ],
    "产业扶持": [
        "补贴", "扶持", "政策支持", "大力支持", "加大支持", "支持政策", "鼓励", "产业规划", "发展规划", "专项规划",
        "专项资金", "产业基金", "引导基金", "大基金", "减税", "降费", "税收优惠", "以旧换新", "设备更新",
        "试点", "示范区", "先行区", "首台套", "国产替代", "自主可控", "专精特新", "揭榜挂帅", "重大项目",
        "加快发展", "加快推进", "培育", "新基建", "消费券", "贴息", "奖励", "财政资金",
    ],
    "货币财政": [
        "央行", "人民银行", "人行", "降准", "降息", "加息", "LPR", "MLF", "逆回购", "公开市场", "再贷款",
        "准备金", "货币政策", "财政政策", "特别国债", "超长期", "专项债", "地方债", "赤字率", "国债",
        "流动性", "社融", "信贷", "M2", "汇率", "人民币", "利率", "稳增长", "化债", "政策性银行",
    ],
    "地方政策": [
        "省政府", "市政府", "区政府", "省委", "市委", "自治区", "地方政府", "印发", "北京市", "上海市",
        "天津市", "重庆市", "深圳市", "广州市", "杭州市", "广东省", "浙江省", "江苏省", "山东省", "四川省",
        "湖北省", "福建省", "安徽省", "河南省", "湖南省", "海南", "自贸港", "自贸区", "雄安", "大湾区",
        "长三角", "京津冀", "成渝", "西部大开发", "东北振兴", "中部崛起",
    ],
    "监管": [
        "证监会", "金融监管总局", "银保监", "上交所", "深交所", "北交所", "交易所", "立案", "处罚", "罚款",
        "问询函", "关注函", "监管函", "警示函", "纪律处分", "退市", "风险警示", "严监管", "减持", "IPO",
        "并购重组", "注册制", "异常波动", "停牌", "规范", "整治", "反垄断", "调查", "新规", "征求意见",
    ],
    "国际": [
        "美联储", "鲍威尔", "关税", "贸易战", "贸易摩擦", "制裁", "出口管制", "实体清单", "特朗普", "白宫",
        "美国", "欧盟", "日本央行", "欧洲央行", "地缘", "冲突", "停火", "中美", "谈判", "会谈", "峰会",
        "G20", "OPEC", "欧佩克", "伊朗", "以色列", "俄罗斯", "乌克兰", "霍尔木兹", "非农", "CPI", "美债",
    ],
    "产业热点": [
        "人工智能", "AI", "大模型", "算力", "芯片", "半导体", "光刻", "机器人", "人形机器人", "低空经济",
        "无人机", "新能源", "光伏", "储能", "锂电", "固态电池", "氢能", "核电", "特高压", "数据要素",
        "量子", "商业航天", "卫星", "6G", "5G", "创新药", "医疗器械", "脑机接口", "智能驾驶", "自动驾驶",
        "新能源汽车", "消费电子", "军工", "稀土", "黄金", "有色", "煤炭", "石油", "电力", "化工",
    ],
}
# 计入 policy_count 的标签（"产业热点"只是题材词，不算政策）
POLICY_TAGS: list[str] = [t for t in POLICY_KEYWORDS if t != "产业热点"]
# 国内政策类标签：关键词前面紧挨外国名时不算；其中前四类的消息还会按"提到的行业"加到该行业所有股票上
DOMESTIC_TAGS: set[str] = {"国家政策", "部委", "产业扶持", "货币财政", "地方政策", "监管"}
INDUSTRY_POLICY_TAGS: set[str] = {"国家政策", "部委", "产业扶持", "地方政策"}
FOREIGN_PREFIXES: tuple[str, ...] = (
    "美国", "美", "日本", "日", "欧盟", "欧洲", "英国", "英", "德国", "法国", "俄罗斯", "俄", "韩国", "印度",
    "以色列", "伊朗", "乌克兰", "阿联酋", "沙特", "加拿大", "澳大利亚", "澳洲", "土耳其", "巴西", "越南",
    "新加坡", "瑞士", "瑞典", "挪威", "意大利", "西班牙", "墨西哥", "阿根廷", "南非", "菲律宾", "泰国", "印尼",
)
# 行业词匹配前先去掉这些机构/媒体名（"人民银行"不代表银行业消息）
INDUSTRY_MASKS: tuple[str, ...] = (
    "人民银行", "中央银行", "世界银行", "银行间", "开发银行", "进出口银行", "农业发展银行",
    "证券时报", "证券日报", "上海证券报", "中国证券报",
)

# 与普通词/地名/媒体名容易混淆的简称：必须有明确上下文才算提到
AMBIGUOUS_NAMES: set[str] = {
    "太阳能", "机器人", "农产品", "金融街", "新希望", "中关村", "新大陆", "新世界", "太平洋", "新天地",
    "三人行", "人民网", "新华网", "生物谷", "数字人", "驱动力", "指南针", "同花顺", "值得买", "向日葵",
    "创世纪", "电子城", "大智慧", "新经典", "新产业", "新城市", "长白山", "连云港", "天津港", "日照港",
    "青岛港", "宁波港", "唐山港", "广州港", "重庆港", "南京港", "珠海港", "盐田港", "张家界", "黑芝麻",
    "好想你", "火星人", "孩子王", "步步高", "老百姓", "好当家", "好太太", "三六零", "七一二", "二六三",
    "新里程", "探路者", "外高桥", "陆家嘴", "王府井", "徐家汇", "大东方", "大西洋", "会稽山", "天目湖",
    "轻纺城", "东方财富", "美国", "中国", "全新好", "好上好", "新坐标", "天下秀", "大名城",
}

# 证监会行业名 → 新闻里常见的说法（没列出的自动拆分）
INDUSTRY_OVERRIDES: dict[str, list[str]] = {
    "货币金融服务": ["银行"],
    "资本市场服务": ["券商", "证券公司", "资本市场"],
    "保险业": ["保险"],
    "其他金融业": ["金融"],
    "房地产业": ["房地产", "楼市", "住房"],
    "化学原料和化学制品制造业": ["化工", "化学品", "化肥", "农药"],
    "电气机械和器材制造业": ["电气设备", "电网", "光伏", "储能", "电池", "家电"],
    "计算机、通信和其他电子设备制造业": ["电子", "半导体", "芯片", "通信", "消费电子", "计算机"],
    "软件和信息技术服务业": ["软件", "信息技术", "人工智能", "数据", "信创"],
    "互联网和相关服务": ["互联网", "平台经济"],
    "医药制造业": ["医药", "创新药", "中药", "药品"],
    "酒、饮料和精制茶制造业": ["白酒", "饮料", "酒类"],
    "汽车制造业": ["汽车", "新能源汽车", "智能驾驶"],
    "铁路、船舶、航空航天和其他运输设备制造业": ["铁路", "船舶", "航空航天", "航天", "低空经济", "军工"],
    "有色金属冶炼和压延加工业": ["有色金属", "稀土", "铜", "铝", "黄金", "锂"],
    "有色金属矿采选业": ["有色金属", "稀土", "矿产"],
    "黑色金属冶炼和压延加工业": ["钢铁"],
    "煤炭开采和洗选业": ["煤炭"],
    "石油和天然气开采业": ["石油", "天然气", "油气"],
    "电力、热力生产和供应业": ["电力", "发电", "核电", "绿电"],
    "燃气生产和供应业": ["燃气", "天然气"],
    "农业": ["农业", "种业", "粮食"],
    "畜牧业": ["养殖", "猪肉", "生猪"],
    "渔业": ["渔业", "水产"],
    "专用设备制造业": ["专用设备", "工程机械", "医疗器械"],
    "通用设备制造业": ["通用设备", "机床", "机器人", "工业母机"],
    "零售业": ["零售", "消费"],
    "批发业": ["批发", "流通"],
    "土木工程建筑业": ["基建", "建筑"],
    "航空运输业": ["航空", "机场"],
    "水上运输业": ["航运", "港口"],
    "装卸搬运和仓储业": ["物流", "仓储"],
    "生态保护和环境治理业": ["环保", "生态"],
    "新闻和出版业": ["出版", "传媒"],
    "广播、电视、电影和录音制作业": ["影视", "电影", "传媒"],
}
_INDUSTRY_STOP: set[str] = {"其他", "相关", "专业", "辅助性活动", "综合", "服务", "供应", "产品", "制品"}
_INDUSTRY_SUFFIXES: tuple[str, ...] = (
    "冶炼和压延加工业", "开采和洗选业", "生产和供应业", "制造业", "加工业", "采选业", "服务业", "供应业",
    "修理业", "建筑业", "运输业", "管理业", "冶炼", "生产", "服务", "业",
)

CLS_URL: str = "https://www.cls.cn/v1/roll/get_roll_list"
EM_URL: str = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
SINA_URL: str = "https://zhibo.sina.com.cn/api/zhibo/feed"
FLASH_TTL: float = 60.0
KEEP_HOURS: int = 72
DEEP_PAGES: dict[str, int] = {"cls": 12, "em": 4, "sina": 6}     # 约覆盖24小时
SHALLOW_PAGES: dict[str, int] = {"cls": 2, "em": 1, "sina": 1}
SOURCE_LABELS: dict[str, str] = {"cls": "财联社", "em": "东方财富", "sina": "新浪财经"}
SOURCE_ORDER: dict[str, int] = {"cls": 0, "em": 1, "sina": 2}

STORE_SCHEMA: dict[str, pl.DataType] = {
    "time": pl.Utf8, "title": pl.Utf8, "content": pl.Utf8, "url": pl.Utf8, "source": pl.Utf8,
    "codes": pl.List(pl.Utf8), "important": pl.Boolean, "key": pl.Utf8,
}

_lock = threading.Lock()
_state: dict[str, Any] = {"fetched_at": 0.0, "errors": {}, "store": None}
_heat_cache: dict[tuple, tuple[float, pl.DataFrame]] = {}


# ---------------------------------------------------------------- 文本工具

def china_now() -> datetime:
    return datetime.now(config.CHINA_TZ)


def _clean_html(text: Any) -> str:
    s: str = re.sub(r"<[^>]+>", "", str(text or ""))
    return re.sub(r"\s+", " ", s.replace("&nbsp;", " ")).strip()


def split_title(title: str, content: str) -> tuple[str, str]:
    """没有标题时从"【标题】正文"里取；再没有就取正文前 40 字"""
    title = _clean_html(title)
    content = _clean_html(content)
    if not title:
        m = re.match(r"^\s*【([^】]{2,80})】", content)
        if m:
            title = m.group(1).strip()
        else:
            title = re.sub(r"^财联社\d+月\d+日电[，,]?", "", content)[:40]
    return title, content


def news_key(title: str, content: str) -> str:
    """去重键：标题（或正文开头）去掉标点空白后的前 24 个字"""
    base: str = title or content
    base = re.sub(r"^财联社\d+月\d+日电[，,]?", "", base)
    norm: str = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", base)).lower()
    return norm[:24]


def _domestic_hit(text: str, word: str) -> bool:
    """关键词出现且前面紧挨的不是外国名（"美国财政部""日本央行"不算国内政策）"""
    start: int = text.find(word)
    while start >= 0:
        if not text[max(0, start - 5):start].endswith(FOREIGN_PREFIXES):
            return True
        start = text.find(word, start + 1)
    return False


def tag_text(text: str) -> list[str]:
    """按 POLICY_KEYWORDS 给文本打标签（按字典顺序）"""
    tags: list[str] = []
    for tag, words in POLICY_KEYWORDS.items():
        if tag in DOMESTIC_TAGS:
            hit: bool = any(_domestic_hit(text, w) for w in words)
        else:
            hit = any(w in text for w in words)
        if hit:
            tags.append(tag)
    return tags


def _code_from_symbol(sym: str) -> str | None:
    """"sh688498" / "0.920873" / "1.600000" → 6位A股代码；板块、基金、港美股返回 None"""
    sym = str(sym or "").strip().lower()
    m = re.match(r"^(?:sh|sz|bj)(\d{6})$", sym) or re.match(r"^[01]\.(\d{6})$", sym)
    if not m:
        return None
    code: str = m.group(1)
    return code if uni_mod.is_a_share(code) else None


# ---------------------------------------------------------------- 各来源解析

def parse_cls(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows or []:
        try:
            t: str = datetime.fromtimestamp(int(r["ctime"]), config.CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except (KeyError, TypeError, ValueError):
            continue
        title, content = split_title(r.get("title") or "", r.get("content") or r.get("brief") or "")
        codes: list[str] = [c for c in (_code_from_symbol(s.get("StockID")) for s in r.get("stock_list") or []
                                        if isinstance(s, dict)) if c]
        out.append({"time": t, "title": title, "content": content, "url": r.get("shareurl") or "",
                    "source": "cls", "codes": codes, "important": r.get("level") in ("A", "B")})
    return out


def parse_em(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows or []:
        t: str = str(r.get("showTime") or "")[:19]
        if len(t) < 16:
            continue
        title, content = split_title(r.get("title") or "", r.get("summary") or "")
        codes: list[str] = [c for c in (_code_from_symbol(s) for s in r.get("stockList") or []) if c]
        code_id: str = str(r.get("code") or "")
        out.append({"time": t, "title": title, "content": content,
                    "url": f"https://finance.eastmoney.com/a/{code_id}.html" if code_id else "",
                    "source": "em", "codes": codes, "important": bool(r.get("titleColor"))})
    return out


def parse_sina(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows or []:
        t: str = str(r.get("create_time") or "")[:19]
        if len(t) < 16:
            continue
        title, content = split_title("", r.get("rich_text") or "")
        url: str = r.get("docurl") or ""
        try:
            url = url or json.loads(r.get("ext") or "{}").get("docurl") or ""
        except (TypeError, ValueError, AttributeError):
            pass
        # 新浪自带的股票标签是机器打的，误标很多（如把"费城联储"标成长安汽车），不采用，只按简称匹配
        out.append({"time": t, "title": title, "content": content, "url": url, "source": "sina",
                    "codes": [], "important": bool(r.get("is_focus"))})
    return out


# ---------------------------------------------------------------- 抓取

def _cls_page(last_time: int, rn: int = 50) -> list[dict]:
    params: dict[str, Any] = {"app": "CailianpressWeb", "category": "", "last_time": last_time, "os": "web",
                              "refresh_type": "1", "rn": str(rn), "sv": "8.4.6"}
    params["sign"] = hashlib.md5(hashlib.sha1(urlencode(params).encode()).hexdigest().encode()).hexdigest()
    payload = net.get_json(CLS_URL, params=params, headers={"Referer": "https://www.cls.cn/telegraph"}, timeout=10)
    return ((payload or {}).get("data") or {}).get("roll_data") or []


def _fetch_cls(pages: int) -> list[dict]:
    rows: list[dict] = []
    last: int = int(time.time())
    for _ in range(pages):
        chunk: list[dict] = _cls_page(last)
        if not chunk:
            break
        rows.extend(chunk)
        oldest: int = min(int(r.get("ctime") or last) for r in chunk)
        if oldest >= last:
            break
        last = oldest
    return parse_cls(rows)


def _fetch_em(pages: int) -> list[dict]:
    rows: list[dict] = []
    sort_end: str = ""
    for _ in range(pages):
        payload = net.get_json(EM_URL, params={
            "client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": sort_end, "pageSize": "200",
            "req_trace": str(int(time.time() * 1000)),
        }, timeout=10)
        data: dict = (payload or {}).get("data") or {}
        chunk: list[dict] = data.get("fastNewsList") or []
        rows.extend(chunk)
        sort_end = str(data.get("sortEnd") or "")
        if not chunk or not sort_end:
            break
    return parse_em(rows)


def _fetch_sina(pages: int) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, pages + 1):
        payload = net.get_json(SINA_URL, params={
            "page": str(page), "page_size": "100", "zhibo_id": "152", "tag_id": "0", "dire": "f", "dpc": "1",
            "pagesize": "100", "type": "1",
        }, timeout=10)
        feed: list[dict] = ((((payload or {}).get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
        rows.extend(feed)
        if not feed:
            break
    return parse_sina(rows)


def _ak_fallback(source: str) -> list[dict]:
    """直连接口失败时改用 akshare 同名函数（只有最新一页）"""
    import akshare as ak

    if source == "cls":
        df = net.call_with_fallback(lambda: ak.stock_info_global_cls(symbol="全部"), retries=2)
        return [{"time": f"{r['发布日期']} {r['发布时间']}"[:19], **dict(zip(("title", "content"), split_title(
            r.get("标题") or "", r.get("内容") or ""), strict=True)), "url": "", "source": "cls", "codes": [],
                 "important": False} for r in df.to_dict("records")]
    if source == "em":
        df = net.call_with_fallback(lambda: ak.stock_info_global_em(), retries=2)
        return [{"time": str(r["发布时间"])[:19], **dict(zip(("title", "content"), split_title(
            r.get("标题") or "", r.get("摘要") or ""), strict=True)), "url": r.get("链接") or "", "source": "em",
                 "codes": [], "important": False} for r in df.to_dict("records")]
    df = net.call_with_fallback(lambda: ak.stock_info_global_sina(), retries=2)
    return [{"time": str(r["时间"])[:19], **dict(zip(("title", "content"), split_title("", r.get("内容") or ""),
                                                   strict=True)), "url": "", "source": "sina", "codes": [],
             "important": False} for r in df.to_dict("records")]


_FETCHERS = {"cls": _fetch_cls, "em": _fetch_em, "sina": _fetch_sina}


def _fetch_source(source: str, pages: int) -> tuple[list[dict], str | None]:
    try:
        return _FETCHERS[source](pages), None
    except Exception as e:  # noqa: BLE001  单个来源失败不影响其他来源
        err: str = f"{type(e).__name__}: {str(e)[:80]}"
    try:
        return _ak_fallback(source), None
    except Exception as e:  # noqa: BLE001
        return [], f"{SOURCE_LABELS[source]}快讯获取失败：{err}；akshare 也失败：{str(e)[:60]}"


# ---------------------------------------------------------------- 存储与合并

def _store_file() -> Path:
    return config.CACHE_DIR.joinpath("news_store.parquet")


def _empty_store() -> pl.DataFrame:
    return pl.DataFrame(schema=STORE_SCHEMA)


def _load_store() -> pl.DataFrame:
    path: Path = _store_file()
    if not path.exists():
        return _empty_store()
    try:
        return pl.read_parquet(path).select([pl.col(c).cast(t) for c, t in STORE_SCHEMA.items()])
    except Exception:  # noqa: BLE001  缓存损坏就当没有
        return _empty_store()


def merge_items(store: pl.DataFrame, items: list[dict], now: datetime | None = None) -> pl.DataFrame:
    """把新抓的快讯合并进存储：同一条消息（去重键相同）只保留一条（财联社优先），合并各来源的股票标签"""
    now = now or china_now()
    cutoff: str = (now - timedelta(hours=KEEP_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    new: list[dict] = []
    for it in items:
        key: str = news_key(it.get("title") or "", it.get("content") or "")
        if not key:
            continue
        new.append({**{k: it.get(k) for k in STORE_SCHEMA if k != "key"}, "key": key,
                    "codes": list(dict.fromkeys(it.get("codes") or []))})
    frames: list[pl.DataFrame] = [store] if not store.is_empty() else []
    if new:
        frames.append(pl.DataFrame(new, schema=STORE_SCHEMA, orient="row"))
    if not frames:
        return _empty_store()
    df: pl.DataFrame = pl.concat(frames, how="vertical_relaxed").filter(pl.col("time") >= cutoff)
    if df.is_empty():
        return _empty_store()
    df = df.with_columns(pl.col("source").replace_strict(SOURCE_ORDER, default=9, return_dtype=pl.Int8).alias("_o"))
    codes: pl.DataFrame = df.sort("_o").group_by("key").agg(
        pl.col("codes").list.explode(keep_nulls=False, empty_as_null=False).unique(maintain_order=True)
        .alias("_codes"),
        pl.col("important").any().alias("_imp"),
    )
    df = (df.sort(["_o", "time"]).unique("key", keep="first", maintain_order=True)
          .join(codes, on="key", how="left")
          .with_columns(pl.col("_codes").alias("codes"), pl.col("_imp").fill_null(False).alias("important"))
          .select(list(STORE_SCHEMA)).sort("time", descending=True))
    return df


def refresh(force: bool = False) -> pl.DataFrame:
    """抓取三个来源并合并入库（60 秒内不重复抓取），返回按时间倒序的全部快讯"""
    with _lock:
        now_ts: float = time.time()
        store: pl.DataFrame | None = _state["store"]
        if store is None:
            store = _load_store()
            _state["store"] = store
            try:
                _state["fetched_at"] = _store_file().stat().st_mtime
            except OSError:
                _state["fetched_at"] = 0.0
        if not force and now_ts - _state["fetched_at"] < FLASH_TTL:
            return store
        newest: str | None = store["time"].max() if not store.is_empty() else None
        stale_cut: str = (china_now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        pages: dict[str, int] = DEEP_PAGES if newest is None or newest < stale_cut else SHALLOW_PAGES
        items: list[dict] = []
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = {s: pool.submit(_fetch_source, s, pages[s]) for s in _FETCHERS}
            for source, fut in results.items():
                got, err = fut.result()
                items.extend(got)
                if err:
                    errors[source] = err
                    logger.warning(err)
        _state["errors"] = errors
        _state["fetched_at"] = now_ts
        if items:
            store = merge_items(store, items)
            _state["store"] = store
            _heat_cache.clear()
            try:
                config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp: Path = _store_file().with_suffix(".tmp")
                store.write_parquet(tmp)
                tmp.replace(_store_file())
            except OSError as e:
                logger.warning("快讯缓存写入失败：%s", e)
        return store


def last_errors() -> dict[str, str]:
    """最近一次抓取各来源的失败原因（空字典表示都正常）"""
    return dict(_state["errors"])


# ---------------------------------------------------------------- 股票识别

class StockMatcher:
    """按股票简称在文本里找股票（二字索引 + 前缀比对，几千只股票也很快）"""

    def __init__(self, universe: pl.DataFrame) -> None:
        self.names: dict[str, str] = {}
        self.index: dict[str, list[tuple[str, str, bool]]] = {}
        live: pl.DataFrame = universe.filter(pl.col("status") == 1) if "status" in universe.columns else universe
        for code, name in live.select(["code", "name"]).iter_rows():
            if not code or not name:
                continue
            self.names[code] = name
            for alias in self.aliases(name):
                mode: str = "word" if alias in AMBIGUOUS_NAMES else ("short" if len(alias) <= 2 else "")
                self.index.setdefault(alias[:2], []).append((alias, code, mode))

    @staticmethod
    def aliases(name: str) -> list[str]:
        """"XD源杰科"→"源杰科"，"*ST金泰"→"ST金泰"，"万科Ａ"→"万科A"、"万科" """
        n: str = unicodedata.normalize("NFKC", uni_mod.clean_name(name)).replace("*", "")
        n = re.sub(r"^(XD|XR|DR|N|C)(?=[一-鿿])", "", n)
        out: list[str] = [n]
        m = re.match(r"^(.+?)[AB]$", n)
        if m and re.search(r"[一-鿿]", m.group(1)):
            out.append(m.group(1))
        return [a for a in dict.fromkeys(out) if len(a) >= 2]

    @staticmethod
    def _exact(text: str, start: int, alias: str, code: str, mode: str) -> bool:
        """容易误判的简称：正文有代码、后面紧跟"（"，或作为标题开头的"简称："才算；两字简称另可跟"股份/集团/："""
        if code in text:
            return True
        after: str = text[start + len(alias):start + len(alias) + 2]
        if after[:1] in ("(", "（"):
            return True
        if after[:1] in (":", "：") and start <= 1:
            return True
        return mode == "short" and (after in ("股份", "集团") or after[:1] in (":", "："))

    def match(self, text: str) -> list[str]:
        text = unicodedata.normalize("NFKC", text or "").replace("*", "")
        found: list[str] = []
        for i in range(len(text) - 1):
            for alias, code, mode in self.index.get(text[i:i + 2], ()):
                if code in found or not text.startswith(alias, i):
                    continue
                if mode and not self._exact(text, i, alias, code, mode):
                    continue
                found.append(code)
        return found


_matcher: dict[str, Any] = {"key": None, "obj": None}


def _get_matcher() -> StockMatcher:
    uni: pl.DataFrame = uni_mod.load_universe()
    key: tuple = (id(uni), uni.height)
    if _matcher["key"] != key:
        _matcher.update(key=key, obj=StockMatcher(uni))
    return _matcher["obj"]


def industry_keywords(industry: str | None) -> list[str]:
    """证监会行业名 → 新闻里可能出现的行业词"""
    if not industry:
        return []
    if industry in INDUSTRY_OVERRIDES:
        return INDUSTRY_OVERRIDES[industry]
    out: list[str] = []
    for part in re.split(r"[、，,和及]", industry):
        part = re.sub(r"^其他", "", part.strip())
        for suf in _INDUSTRY_SUFFIXES:
            if part.endswith(suf) and len(part) > len(suf):
                part = part[: -len(suf)]
                break
        if len(part) >= 2 and part not in _INDUSTRY_STOP:
            out.append(part)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- 对外接口

def annotate(store: pl.DataFrame, matcher: StockMatcher | None = None) -> list[dict]:
    """给快讯加 tags 与 stocks"""
    matcher = matcher or _get_matcher()
    out: list[dict] = []
    for r in store.iter_rows(named=True):
        text: str = f"{r['title']} {r['content']}"
        codes: list[str] = list(dict.fromkeys([*(r["codes"] or []), *matcher.match(text)]))
        out.append({
            "time": r["time"], "title": r["title"], "content": r["content"], "tags": tag_text(text),
            "stocks": [{"code": c, "name": matcher.names.get(c, "")} for c in codes if c in matcher.names],
            "source": SOURCE_LABELS.get(r["source"], r["source"]), "url": r["url"], "important": r["important"],
        })
    return out


def flash(limit: int = 100) -> list[dict]:
    """最新快讯（时间倒序）：[{"time","title","content","tags","stocks":[{"code","name"}],"source","url","important"}]"""
    try:
        store: pl.DataFrame = refresh()
    except Exception as e:  # noqa: BLE001  快讯失败不能影响页面
        logger.warning("快讯获取失败：%s", e)
        store = _state["store"] if _state["store"] is not None else _empty_store()
    return annotate(store.head(max(int(limit), 0)))


def compute_heat(items: list[dict], universe: pl.DataFrame, codes: list[str]) -> pl.DataFrame:
    """由已标注的快讯计算热度：news_count 直接提到的条数；policy_count 政策消息提到个股或其行业的条数；
    热度分 = ln(1+提到条数) + 0.5·ln(1+直接政策条数) + 0.25·ln(1+行业政策条数)，
    news_z = (热度分 - 全部股票中位数) / 全部股票标准差（截断到 -3~5）"""
    schema: dict[str, pl.DataType] = {"code": pl.Utf8, "news_count": pl.Int32, "policy_count": pl.Int32,
                                      "news_z": pl.Float64}
    codes = [str(c).zfill(6) for c in codes]
    live: pl.DataFrame = universe.filter(pl.col("status") == 1) if "status" in universe.columns else universe
    if not items or live.is_empty():
        return pl.DataFrame({"code": codes, "news_count": [0] * len(codes), "policy_count": [0] * len(codes),
                             "news_z": [0.0] * len(codes)}, schema=schema)
    kw_by_industry: dict[str, list[str]] = {
        ind: industry_keywords(ind) for ind in live["industry"].drop_nulls().unique().to_list()
    }
    news: dict[str, int] = {}
    policy: dict[str, int] = {}
    policy_ind: dict[str, int] = {}
    for it in items:
        mentioned: set[str] = {s["code"] for s in it["stocks"]}
        for c in mentioned:
            news[c] = news.get(c, 0) + 1
        if not any(t in POLICY_TAGS for t in it["tags"]):
            continue
        for c in mentioned:
            policy[c] = policy.get(c, 0) + 1
        if not any(t in INDUSTRY_POLICY_TAGS for t in it["tags"]):
            continue
        text: str = f"{it['title']} {it['content']}"
        for mask in INDUSTRY_MASKS:
            text = text.replace(mask, "")
        for ind, words in kw_by_industry.items():
            if any(w in text for w in words):
                policy_ind[ind] = policy_ind.get(ind, 0) + 1
    df: pl.DataFrame = live.select(["code", "industry"]).with_columns(
        pl.col("code").replace_strict(news, default=0, return_dtype=pl.Int32).alias("news_count"),
        pl.col("code").replace_strict(policy, default=0, return_dtype=pl.Int32).alias("_direct"),
        pl.col("industry").replace_strict(policy_ind, default=0, return_dtype=pl.Int32).alias("_ind"),
    ).with_columns(
        (pl.col("_direct") + pl.col("_ind")).alias("policy_count"),
        (pl.col("news_count").cast(pl.Float64).log1p() + 0.5 * pl.col("_direct").cast(pl.Float64).log1p()
         + 0.25 * pl.col("_ind").cast(pl.Float64).log1p()).alias("_s"),
    )
    std: float | None = df["_s"].std()
    med: float = df["_s"].median() or 0.0
    if not std or math.isnan(std):
        df = df.with_columns(pl.lit(0.0).alias("news_z"))
    else:
        df = df.with_columns(((pl.col("_s") - med) / std).clip(-3.0, 5.0).alias("news_z"))
    req: pl.DataFrame = pl.DataFrame({"code": codes}, schema={"code": pl.Utf8})
    return req.join(df.select(["code", "news_count", "policy_count", "news_z"]), on="code", how="left").with_columns(
        pl.col("news_count").fill_null(0), pl.col("policy_count").fill_null(0), pl.col("news_z").fill_null(0.0),
    ).cast(schema)


def news_heat(codes: list[str], hours: int = 24) -> pl.DataFrame:
    """[code, news_count, policy_count, news_z]：近 hours 小时的消息热度（实时，无历史）"""
    try:
        store: pl.DataFrame = refresh()
    except Exception as e:  # noqa: BLE001
        logger.warning("快讯获取失败：%s", e)
        store = _state["store"] if _state["store"] is not None else _empty_store()
    cutoff: str = (china_now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    key: tuple = (hours, store.height, store["time"].max() if not store.is_empty() else None)
    now: float = time.time()
    hit = _heat_cache.get(key)
    if hit and now - hit[0] < FLASH_TTL:
        full: pl.DataFrame = hit[1]
    else:
        items: list[dict] = annotate(store.filter(pl.col("time") >= cutoff))
        uni: pl.DataFrame = uni_mod.load_universe()
        full = compute_heat(items, uni, uni.filter(pl.col("status") == 1)["code"].to_list())
        _heat_cache.clear()
        _heat_cache[key] = (now, full)
    codes = [str(c).zfill(6) for c in codes]
    req: pl.DataFrame = pl.DataFrame({"code": codes}, schema={"code": pl.Utf8})
    return req.join(full, on="code", how="left").with_columns(
        pl.col("news_count").fill_null(0), pl.col("policy_count").fill_null(0), pl.col("news_z").fill_null(0.0),
    )
