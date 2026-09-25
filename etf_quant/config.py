"""
策略、资产池与路径配置。

普通使用者不需要修改本文件。所有参数都经过 2014-2026 年历史数据的稳健性检验，
单独调某一个参数去追求更高的历史收益，大概率只会带来"过拟合"（对未来无效）。
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


ROOT: Path = Path(__file__).parent
# 数据、持仓、报告都保存在这里（可用环境变量 ETF_QUANT_WORKSPACE 指定其他目录）
WORKSPACE: Path = Path(os.environ.get("ETF_QUANT_WORKSPACE") or ROOT.joinpath("workspace"))
LAB_PATH: Path = WORKSPACE.joinpath("lab")            # vnpy.alpha AlphaLab 数据目录
REPORT_PATH: Path = WORKSPACE.joinpath("reports")
HOLDINGS_FILE: Path = WORKSPACE.joinpath("持仓.csv")
NAV_FILE: Path = WORKSPACE.joinpath("净值记录.csv")
ADVICE_FILE: Path = WORKSPACE.joinpath("last_advice.json")


@dataclass(frozen=True)
class Etf:
    """场内ETF"""

    code: str           # 证券代码，如 510300
    name: str           # 简称
    exchange: str       # SSE 上交所 / SZSE 深交所
    note: str = ""      # 给使用者看的一句话说明

    @property
    def vt_symbol(self) -> str:
        return f"{self.code}.{self.exchange}"

    @property
    def market_symbol(self) -> str:
        """腾讯/新浪行情接口使用的代码格式，如 sh510300"""
        prefix: str = "sh" if self.exchange == "SSE" else "sz"
        return prefix + self.code


# 资产池：全部是A股账户就能买的场内ETF（不需要开通期货、融资融券等权限）
ETFS: list[Etf] = [
    Etf("510300", "沪深300ETF", "SSE", "中国最大的300家上市公司"),
    Etf("510880", "红利ETF", "SSE", "高分红的大公司"),
    Etf("512890", "红利低波ETF", "SSE", "高分红且波动小的公司"),
    Etf("510500", "中证500ETF", "SSE", "中型公司"),
    Etf("159915", "创业板ETF", "SZSE", "创业板成长型公司"),
    Etf("512100", "中证1000ETF", "SSE", "小型公司"),
    Etf("588000", "科创50ETF", "SSE", "科创板科技公司"),
    Etf("513100", "纳指ETF", "SSE", "美国纳斯达克100（科技股）"),
    Etf("513500", "标普500ETF", "SSE", "美国500家大公司"),
    Etf("518880", "黄金ETF", "SSE", "实物黄金"),
    Etf("511010", "国债ETF", "SSE", "5年期国债"),
    Etf("511260", "十年国债ETF", "SSE", "10年期国债"),
    Etf("159985", "豆粕ETF", "SZSE", "大宗商品（豆粕期货）"),
    Etf("511880", "银华日利", "SSE", "货币基金，相当于活期理财"),
]

ETF_MAP: dict[str, Etf] = {etf.vt_symbol: etf for etf in ETFS}
CODE_MAP: dict[str, Etf] = {etf.code: etf for etf in ETFS}


def vt(code: str) -> str:
    """代码 -> vt_symbol"""
    return CODE_MAP[code].vt_symbol


@dataclass
class Universe:
    """资产大类划分"""

    classes: dict[str, list[str]] = field(default_factory=lambda: {
        "A股大盘": [vt("510300"), vt("510880"), vt("512890")],
        "A股成长": [vt("510500"), vt("159915"), vt("512100"), vt("588000")],
        "美股": [vt("513100"), vt("513500")],
        "黄金": [vt("518880")],
        "债券": [vt("511010"), vt("511260")],
        "商品": [vt("159985")],
    })
    bonds: list[str] = field(default_factory=lambda: [vt("511010"), vt("511260")])
    cash: str = field(default_factory=lambda: vt("511880"))

    @property
    def vt_symbols(self) -> list[str]:
        symbols: list[str] = [s for codes in self.classes.values() for s in codes]
        return symbols + [self.cash]


CLASS_NOTES: dict[str, str] = {
    "A股大盘": "中国大公司股票",
    "A股成长": "中国中小盘/科技成长股票",
    "美股": "美国股票（QDII）",
    "黄金": "避险资产",
    "债券": "国债，收益稳定",
    "商品": "大宗商品，与股票相关性低",
    "避险仓": "股票走弱时的避风港（国债）",
}


@dataclass
class StrategyParams:
    """
    稳健多资产趋势配置策略参数

    思路（每周最后一个交易日收盘后计算，下周一开盘执行）：
    1. 把ETF分成6个大类，靠"不把鸡蛋放在一个篮子里"获得长期稳定收益；
    2. 趋势过滤：只买价格在200日均线之上、且动量为正的ETF，躲开长期下跌；
       已持有的要跌破均线5%才卖，避免在均线附近反复买卖；
    3. 每个大类里只买最强的一只，且新的必须明显更强才换（减少无谓换仓）；
    4. 风险预算：波动大的资产少买，波动小的多买；单只最多30%；组合波动超过10%时整体降仓；
    5. 大类走弱时，空出来的资金转入走势良好的国债ETF，其余放货币基金。
    """

    momentum_windows: tuple[int, ...] = (20, 60, 120)   # 动量：约1个月/3个月/半年涨幅的平均
    trend_window: int = 200                              # 趋势：200日均线
    vol_window: int = 60                                 # 波动率估计窗口
    min_history: int = 130                               # 上市不足该天数的ETF不参与
    risk_alpha: float = 0.5                              # 风险预算指数：0=等权，1=完全按波动倒数
    max_weight: float = 0.30                             # 单只ETF最大仓位
    target_vol: float = 0.10                             # 组合年化波动上限
    switch_margin: float = 0.03                          # 同类换仓门槛（动量差）
    exit_buffer: float = 0.05                            # 已持有的ETF跌破均线5%以上才卖出，减少来回买卖
    bond_max_total: float = 0.60                         # 债券ETF合计最大仓位
    rebalance_band: float = 0.03                         # 仓位偏离不足3%不调整
    annual_days: int = 244


@dataclass
class TradingParams:
    """交易执行参数"""

    lot_size: int = 100                  # A股ETF一手100份
    cash_buffer: float = 0.01            # 保留1%现金，应对手续费和开盘价波动
    commission_rate: float = 0.0003      # 佣金费率（万3，多数券商ETF佣金更低）
    min_commission: float = 5.0          # 单笔最低佣金（部分券商ETF免五，按5元保守估计）
    backtest_rate: float = 0.0005        # 回测单边成本：佣金+滑点
    cash_rate: float = 0.00005           # 货币ETF买卖成本（基本免佣）
    price_add: float = 0.02              # 回测委托价相对收盘价的超价比例（保证成交）
