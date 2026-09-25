"""
用户设置：预测筛选条件、模拟交易规则、自动更新开关。保存在 workspace/settings.json。

- 所有数值都有合理范围，超出范围给出中文提示（format_errors）；
- 网页 PUT 可以只传要改的部分（merge_update 做深度合并后再整体校验）；
- PRESETS 三套风格：稳健 / 均衡 / 激进（kind_overrides 给出强势股波段的对应参数）；
- 只有一份设置（predict.kind 表示当前看的模型）。不同模型的门槛含义不同（连板/首板是概率，波段是预期净收益），
  切换模型时用 defaults_for(kind) / for_kind(s, kind) 取该模型合适的默认值（KIND_FIELDS 里的字段）。
"""
import json
import os
import threading
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import config


# 五个维度（与 predict/features.GROUPS 一致；这里单独定义，避免设置模块依赖模型代码）
GROUPS: list[str] = ["sentiment", "capital", "fundamental", "theme", "technical"]
BOARDS: list[str] = ["main", "chinext", "star", "bj"]
KINDS: dict[str, str] = {"streak": "连板晋级", "first": "首板潜力", "swing": "强势股波段"}
EXIT_RULES: dict[str, str] = {
    "next_open": "第二天开盘卖出", "next_close": "第二天收盘卖出", "until_break": "涨停就拿着，断板次日开盘卖（最多10天）",
    "until_break_close": "第一个收盘没涨停的交易日收盘卖（最多5天）",
}


class PredictSettings(BaseModel):
    """预测与筛选"""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["streak", "first", "swing"] = "streak"
    weights: dict[str, float] = Field(default_factory=lambda: {g: 1.0 for g in GROUPS})
    news_weight: float = Field(0.0, ge=0, le=2)     # 消息面（仅实时，历史回测中恒为0；热度≠利好，默认不参与排序）
    exclude_st: bool = True
    boards: list[str] = Field(default_factory=lambda: ["main", "chinext", "star"])
    min_float_cap: float = Field(0, ge=0, le=100_000)       # 亿
    max_float_cap: float = Field(500, gt=0, le=100_000)     # 亿
    min_price: float = Field(2, ge=0, le=10_000)
    max_price: float = Field(200, gt=0, le=10_000)
    streak_min: int = Field(1, ge=0, le=30)
    streak_max: int = Field(10, ge=0, le=30)
    exclude_one_word: bool = False                  # 排除今天一字板（明天大概率买不进）
    threshold: float = Field(0.0, ge=0, le=1)       # 门槛：连板/首板为综合概率下限；波段为预期净收益下限（0.01 = 1%）
    top_n: int = Field(5, ge=1, le=50)

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, value: dict[str, float]) -> dict[str, float]:
        unknown: list[str] = [k for k in value if k not in GROUPS]
        if unknown:
            raise ValueError(f"不认识的维度：{'、'.join(unknown)}")
        for k, v in value.items():
            if not 0 <= v <= 2:
                raise ValueError(f"「{GROUP_LABELS[k]}」的权重要在 0 到 2 之间（现在是 {v}）")
        return {g: float(value.get(g, 1.0)) for g in GROUPS}

    @field_validator("boards")
    @classmethod
    def _check_boards(cls, value: list[str]) -> list[str]:
        unknown: list[str] = [b for b in value if b not in BOARDS]
        if unknown:
            raise ValueError(f"不认识的板块：{'、'.join(unknown)}（可选 main/chinext/star/bj）")
        if not value:
            raise ValueError("至少要选一个板块")
        return [b for b in BOARDS if b in value]

    @model_validator(mode="after")
    def _check_ranges(self) -> "PredictSettings":
        if self.max_float_cap <= self.min_float_cap:
            raise ValueError("流通市值上限必须大于下限")
        if self.max_price <= self.min_price:
            raise ValueError("股价上限必须大于下限")
        if self.streak_max < self.streak_min:
            raise ValueError("连板数上限不能小于下限")
        return self


class TradeSettings(BaseModel):
    """模拟交易规则（回测用）"""

    model_config = ConfigDict(extra="ignore")

    capital: float = Field(100_000, ge=1_000, le=1e9)
    position_pct: float = Field(0.2, gt=0, le=1)            # 每只买入金额占总资产比例（0.2 = 20%）
    max_positions: int = Field(5, ge=1, le=50)
    max_gap_pct: float = Field(7.0, ge=0, le=30)            # 开盘涨幅超过这么多（%）就放弃买入
    exit_rule: Literal["next_open", "next_close", "until_break", "until_break_close"] = "next_close"
    stop_loss_pct: float = Field(0.0, ge=0, le=0.5)         # 止损比例（小数，0.05 = 跌5%止损；0 表示不止损）
    fee_rate: float = Field(0.00025, ge=0, le=0.01)         # 佣金费率（双边）
    stamp_duty: float = Field(0.0005, ge=0, le=0.01)        # 印花税（卖出；stamp_by_date=False 时才用）
    stamp_by_date: bool = True          # 印花税按卖出日期：2023-08-28 前 0.1%，之后 0.05%
    slippage: float = Field(0.001, ge=0, le=0.05)           # 滑点（双边）


# ---------------------------------------------------------------- 第三版（投资助手）新增的设置分组
# 默认值来自用户 2026-09-24 的回答：只有沪深主板权限、只能晚上看盘（做波段）、单笔最多亏总资金 1%、
# 实盘止损可以自动卖但买入要确认。所有分组都只新增，不改动上面的 predict/trade。

def _check_board_list(value: list[str]) -> list[str]:
    unknown: list[str] = [b for b in value if b not in BOARDS]
    if unknown:
        raise ValueError(f"不认识的板块：{'、'.join(unknown)}（可选 main/chinext/star/bj）")
    if not value:
        raise ValueError("至少要选一个板块")
    return [b for b in BOARDS if b in value]


class ProfileSettings(BaseModel):
    """我的情况（首次使用向导填写）：决定选股范围、仓位大小和提醒方式的默认值"""

    model_config = ConfigDict(extra="ignore")

    capital: float = Field(100_000, ge=1_000, le=1e10)      # 准备用来炒股的资金（元）
    boards: list[str] = Field(default_factory=lambda: ["main"])     # 账户能交易的板块
    horizon: Literal["short", "swing", "long"] = "swing"    # 短线1-3天 / 波段几天到几周 / 中长线几个月
    risk_per_trade: float = Field(0.01, ge=0.002, le=0.05)  # 单笔最多亏总资金的比例（0.01 = 1%）
    watch_time: Literal["evening", "sometimes", "fulltime"] = "evening"     # 只能晚上看 / 白天偶尔看 / 全天盯盘
    onboarded: bool = False             # 是否已完成首次使用向导

    @field_validator("boards")
    @classmethod
    def _check_boards(cls, value: list[str]) -> list[str]:
        return _check_board_list(value)


class ProviderSettings(BaseModel):
    """数据源：每种数据按顺序尝试，前一个失败自动换下一个（空 = 用内置默认顺序）"""

    model_config = ConfigDict(extra="ignore")

    chains: dict[str, list[str]] = Field(default_factory=dict)     # 数据能力 → 数据源名称顺序
    tushare_token: str = ""             # tushare pro 的 token（可选）
    qmt_path: str = ""                  # 券商 QMT 安装目录（可选，用 xtdata 取数据时需要）


class RiskSettings(BaseModel):
    """纪律与风控：每笔下单前都检查一遍（模拟盘和实盘一样）"""

    model_config = ConfigDict(extra="ignore")

    max_single_pct: float = Field(0.20, gt=0, le=1)         # 单只股票最多占总资金的比例
    # 大盘环境 → 总仓位上限。历史验证（2026-09）：强势之后并不比震荡涨得多，弱势之后波动明显更大，
    # 所以强势/震荡一样、只在弱势时降仓（控制大起大落的伤害），而不是"行情好就加仓"
    regime_caps: dict[str, float] = Field(default_factory=lambda: {"strong": 0.7, "neutral": 0.7, "weak": 0.4})
    daily_loss_limit: float = Field(0.03, ge=0, le=0.2)     # 当天亏损达到总资金的这个比例，当天禁止再买（0 = 不启用）
    cooldown_losses: int = Field(3, ge=0, le=20)            # 连续亏几笔进入冷静期（0 = 不启用）
    cooldown_days: int = Field(2, ge=0, le=30)              # 冷静期几个交易日不能买入
    chase_warn_pct: float = Field(5.0, ge=0, le=30)         # 当天已经涨了这么多（%）还要买，需要二次确认
    require_stop: bool = True                               # 买入必须设止损价
    default_stop_pct: float = Field(0.08, ge=0.01, le=0.5)  # 找不到更合适的止损位时，默认买入价下方 8%
    block_distribution: bool = True                         # 买"疑似出货 / 拉升"阶段的股票前提醒（历史上之后一周平均偏弱）；策略中心的策略也据此跳过出货/下跌阶段
    block_risk_red: bool = True                             # 排雷硬伤（退市/ST/面值和市值退市线/资不抵债/成交极冷/监管处罚）禁止买入；其余红灯只提醒
    warn_average_down: bool = True                          # 给亏损的股票补仓摊平时提醒
    min_amount_20d: float = Field(5e7, ge=0, le=1e11)       # 20日平均成交额下限（元），太小的股票不好卖

    @field_validator("regime_caps")
    @classmethod
    def _check_caps(cls, value: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {"strong": 0.7, "neutral": 0.7, "weak": 0.4}
        for k, v in value.items():
            if k not in out:
                raise ValueError(f"不认识的大盘环境「{k}」（可选 strong/neutral/weak）")
            if not 0 <= float(v) <= 1:
                raise ValueError("大盘环境对应的总仓位上限要在 0 到 1 之间")
            out[k] = float(v)
        return out


class LiveSettings(BaseModel):
    """实盘（真钱）：总开关默认关；自动下单只限止损卖出（auto_policy=stop_only），买入必须人工确认"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    broker: Literal["manual", "qmt", "easytrader", "vnpy_gateway"] = "manual"
    auto_policy: Literal["none", "stop_only", "full"] = "stop_only"
    max_order_amount: float = Field(50_000, ge=0, le=1e10)  # 单笔最大金额（元），0 = 不限
    qmt_path: str = ""                  # miniQMT 的 userdata_mini 目录
    qmt_account: str = ""               # 资金账号
    easytrader_client: str = ""         # 同花顺下单程序 xiadan.exe 的路径
    vnpy_gateway: str = ""              # 例如 vnpy_xtp.XtpGateway
    vnpy_setting: dict[str, Any] = Field(default_factory=dict)


class NotifySettings(BaseModel):
    """提醒推送：网页站内信一直有；微信/邮件需要填写对应的 token 或邮箱"""

    model_config = ConfigDict(extra="ignore")

    channels: list[Literal["web", "pushplus", "serverchan", "email"]] = Field(default_factory=lambda: ["web"])
    pushplus_token: str = ""
    serverchan_key: str = ""
    email_host: str = ""
    email_port: int = Field(465, ge=1, le=65535)
    email_user: str = ""
    email_password: str = ""
    email_to: str = ""
    min_level: Literal["info", "warn", "urgent"] = "warn"   # 这个级别及以上才推送到微信/邮件
    quiet_start: str = "22:30"          # 免打扰时段（紧急提醒不受限制）
    quiet_end: str = "07:30"

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def _check_hhmm(cls, value: str) -> str:
        parts: list[str] = str(value).strip().split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts) or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
            raise ValueError("时间格式应为 时:分，例如 22:30")
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


class MonitorSettings(BaseModel):
    """盘中监控（程序开着时，交易时段定时检查持仓、挂单和交易计划）"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    interval_sec: int = Field(30, ge=10, le=600)
    watch_watchlist: bool = True        # 自选股也做异动提醒
    move_alert_pct: float = Field(3.0, ge=0.5, le=20)       # 5 分钟内涨跌超过这么多（%）提醒


class AssistantSettings(BaseModel):
    """每日收盘流水线里"投资助手"部分的开关和规模"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    screeners: list[str] = Field(default_factory=list)      # 每天收盘后自动运行的选股方案
    fund_flow_limit: int = Field(300, ge=0, le=3000)        # 每天最多拉多少只股票的资金流（持仓+自选+候选）


class LabSettings(BaseModel):
    """模型实验室：训练前先估算内存，超过上限就拦下（本机 31GB，默认留一半余量）"""

    model_config = ConfigDict(extra="ignore")

    max_mem_gb: float = Field(20.0, ge=2, le=512)           # 一次训练最多用多少内存（GB）
    threads: int = Field(0, ge=0, le=256)                   # 训练用几个 CPU 线程（0 = 自动：全部核心减 1）


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    predict: PredictSettings = Field(default_factory=PredictSettings)
    trade: TradeSettings = Field(default_factory=TradeSettings)
    auto_update: bool = True            # 交易日收盘后自动更新数据并预测
    risk_ack: bool = False              # 用户是否已阅读短线风险提示
    # 第三版新增
    profile: ProfileSettings = Field(default_factory=ProfileSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    live: LiveSettings = Field(default_factory=LiveSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    assistant: AssistantSettings = Field(default_factory=AssistantSettings)
    lab: LabSettings = Field(default_factory=LabSettings)


# load() 里某一部分损坏时逐部分恢复用（新增分组必须登记在这里，否则别处出错时会被悄悄重置成默认值）
PARTS: dict[str, type[BaseModel]] = {
    "predict": PredictSettings, "trade": TradeSettings, "profile": ProfileSettings, "providers": ProviderSettings,
    "risk": RiskSettings, "live": LiveSettings, "notify": NotifySettings, "monitor": MonitorSettings,
    "assistant": AssistantSettings, "lab": LabSettings,
}


GROUP_LABELS: dict[str, str] = {
    "sentiment": "情绪面", "capital": "资金面", "fundamental": "基本面", "theme": "题材政策面", "technical": "技术面",
}

FIELD_LABELS: dict[str, str] = {
    "predict": "预测设置", "trade": "交易规则",
    "kind": "预测类型", "weights": "维度权重", "news_weight": "消息面权重", "exclude_st": "排除ST",
    "boards": "板块", "min_float_cap": "流通市值下限(亿)", "max_float_cap": "流通市值上限(亿)",
    "min_price": "最低股价", "max_price": "最高股价", "streak_min": "连板数下限", "streak_max": "连板数上限",
    "exclude_one_word": "排除一字板", "threshold": "门槛（概率或预期收益）", "top_n": "每天选几只",
    "capital": "初始资金", "position_pct": "单只仓位", "max_positions": "最多同时持有",
    "max_gap_pct": "开盘涨幅上限(%)", "exit_rule": "卖出规则", "stop_loss_pct": "止损比例",
    "fee_rate": "佣金费率", "stamp_duty": "印花税", "stamp_by_date": "印花税按日期", "slippage": "滑点",
    "auto_update": "自动更新", "risk_ack": "风险提示确认",
    # 第三版新增
    "profile": "我的情况", "providers": "数据源", "risk": "纪律风控", "live": "实盘", "notify": "提醒推送",
    "monitor": "盘中监控", "assistant": "投资助手", "lab": "模型实验室", "max_mem_gb": "内存上限(GB)", "threads": "训练线程数",
    "horizon": "操作周期", "risk_per_trade": "单笔最多亏", "watch_time": "看盘时间", "onboarded": "已完成向导",
    "chains": "数据源顺序", "tushare_token": "tushare token", "qmt_path": "QMT 目录",
    "max_single_pct": "单只最多占比", "regime_caps": "大盘环境仓位上限", "daily_loss_limit": "单日亏损上限",
    "cooldown_losses": "连亏几笔冷静", "cooldown_days": "冷静期天数", "chase_warn_pct": "追高提醒涨幅(%)",
    "require_stop": "必须设止损", "default_stop_pct": "默认止损比例", "block_distribution": "出货/拉升阶段提醒",
    "block_risk_red": "禁买排雷硬伤", "warn_average_down": "摊平补仓提醒", "min_amount_20d": "20日均成交额下限",
    "enabled": "开关", "broker": "券商接口", "auto_policy": "自动下单范围", "max_order_amount": "单笔最大金额",
    "qmt_account": "资金账号", "easytrader_client": "同花顺下单程序", "vnpy_gateway": "vnpy 网关",
    "vnpy_setting": "vnpy 网关参数", "channels": "推送渠道", "pushplus_token": "PushPlus token",
    "serverchan_key": "Server酱 key", "email_host": "邮件服务器", "email_port": "邮件端口", "email_user": "邮箱账号",
    "email_password": "邮箱授权码", "email_to": "收件邮箱", "min_level": "推送级别", "quiet_start": "免打扰开始",
    "quiet_end": "免打扰结束", "interval_sec": "检查间隔(秒)", "watch_watchlist": "自选股异动提醒",
    "move_alert_pct": "异动幅度(%)", "screeners": "每天自动选股方案", "fund_flow_limit": "资金流每天最多几只",
}


# 各模型的默认值（在 Settings() 默认值上覆盖；KIND_FIELDS 是随模型切换的字段）。
# 波段：只做主板、预期净收益 ≥1%、每天最多 5 只、每只 4% 仓位、最多 25 只、首个非涨停收盘卖出；
# 不按流通市值/股价上限筛、开盘涨幅上限 30%（主板最多涨 10%，等于不设上限）——模型的标签和样本外评价
# （swing.trade_oos）都是这样算的，默认设置不应悄悄删掉模型选出的股票或放弃模型算过的买入。
KIND_DEFAULTS: dict[str, dict] = {
    "streak": {"predict": {}, "trade": {}},
    "first": {"predict": {}, "trade": {}},
    "swing": {
        "predict": {"boards": ["main"], "threshold": 0.01, "top_n": 5, "max_float_cap": 100_000.0,
                    "max_price": 10_000.0},
        "trade": {"position_pct": 0.04, "max_positions": 25, "exit_rule": "until_break_close", "max_gap_pct": 30.0},
    },
}
KIND_FIELDS: dict[str, list[str]] = {
    "predict": ["threshold", "top_n", "boards", "max_float_cap", "max_price"],
    "trade": ["position_pct", "max_positions", "exit_rule", "max_gap_pct"],
}

# 三套风格：对 predict/trade 的部分覆盖。threshold 以"连板晋级"模型为准；
# 首板模型的概率整体偏低，apply_preset 会按 threshold_by_kind 换成对应门槛；波段再套用 kind_overrides。
PRESETS: dict[str, dict] = {
    "稳健": {
        "description": "只挑把握最大的少数几只，小仓位、跌5%止损、第二天开盘就走，宁可错过不做错。",
        "predict": {
            "threshold": 0.30, "top_n": 3, "exclude_one_word": True, "exclude_st": True,
            "boards": ["main", "chinext", "star"], "min_price": 3.0, "max_float_cap": 300.0,
        },
        "trade": {
            "position_pct": 0.10, "max_positions": 3, "stop_loss_pct": 0.05,
            "max_gap_pct": 5.0, "exit_rule": "next_open",
        },
        "threshold_by_kind": {"streak": 0.30, "first": 0.08, "swing": 0.02},
        "kind_overrides": {"swing": {
            "predict": {"boards": ["main"], "top_n": 3},
            "trade": {"position_pct": 0.03, "max_positions": 15, "exit_rule": "until_break_close", "stop_loss_pct": 0.0,
                      "max_gap_pct": 30.0},
        }},
    },
    "均衡": {
        "description": "默认风格：每天看前5名，中等仓位，第二天收盘卖出，跌7%止损。",
        "predict": {
            "threshold": 0.15, "top_n": 5, "exclude_one_word": False, "exclude_st": True,
            "boards": ["main", "chinext", "star"], "min_price": 2.0, "max_float_cap": 500.0,
        },
        "trade": {
            "position_pct": 0.15, "max_positions": 5, "stop_loss_pct": 0.07,
            "max_gap_pct": 7.0, "exit_rule": "next_close",
        },
        "threshold_by_kind": {"streak": 0.15, "first": 0.04, "swing": 0.01},
        "kind_overrides": {"swing": {
            "predict": {"boards": ["main"], "top_n": 5},
            "trade": {"position_pct": 0.04, "max_positions": 25, "exit_rule": "until_break_close", "stop_loss_pct": 0.0,
                      "max_gap_pct": 30.0},
        }},
    },
    "激进": {
        "description": "门槛放低、多选几只、仓位更重，涨停就继续拿，波动和回撤都会明显更大。",
        "predict": {
            "threshold": 0.05, "top_n": 8, "exclude_one_word": False, "exclude_st": True,
            "boards": ["main", "chinext", "star"], "min_price": 2.0, "max_float_cap": 1000.0,
        },
        "trade": {
            "position_pct": 0.20, "max_positions": 5, "stop_loss_pct": 0.10,
            "max_gap_pct": 9.0, "exit_rule": "until_break",
        },
        "threshold_by_kind": {"streak": 0.05, "first": 0.02, "swing": 0.005},
        "kind_overrides": {"swing": {
            "predict": {"boards": ["main"], "top_n": 8},
            "trade": {"position_pct": 0.05, "max_positions": 30, "exit_rule": "until_break_close", "stop_loss_pct": 0.0,
                      "max_gap_pct": 30.0},
        }},
    },
}

_lock = threading.Lock()


# ---------------------------------------------------------------- 校验错误翻译

_TYPE_MESSAGES: dict[str, str] = {
    "greater_than_equal": "不能小于 {ge}",
    "greater_than": "必须大于 {gt}",
    "less_than_equal": "不能大于 {le}",
    "less_than": "必须小于 {lt}",
    "int_parsing": "要填整数",
    "int_from_float": "要填整数",
    "int_type": "要填整数",
    "float_parsing": "要填数字",
    "float_type": "要填数字",
    "bool_parsing": "只能是 是/否",
    "bool_type": "只能是 是/否",
    "literal_error": "只能是 {expected}",
    "list_type": "格式不对（应为列表）",
    "dict_type": "格式不对（应为对象）",
    "model_type": "格式不对（应为对象）",
    "string_type": "要填文字",
    "missing": "不能为空",
    "finite_number": "要填有限的数字",
}


def error_text(err: dict) -> str:
    """单条 pydantic 错误 → 中文说明（不含字段名）"""
    etype: str = err.get("type", "")
    ctx: dict = err.get("ctx") or {}
    if etype == "value_error":
        return str(ctx.get("error") or err.get("msg", "")).removeprefix("Value error, ")
    if etype in _TYPE_MESSAGES:
        try:
            return _TYPE_MESSAGES[etype].format(**{k: _ctx_text(v) for k, v in ctx.items()})
        except (KeyError, IndexError):
            return _TYPE_MESSAGES[etype]
    return f"取值不正确（{err.get('msg', etype)}）"


def _ctx_text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("' or '", "、").replace("', '", "、").replace("'", "")


def field_name(loc: tuple | list) -> str:
    """("predict", "threshold") → "预测设置 / 门槛（概率或预期收益）"（最多显示两级）"""
    names: list[str] = []
    for i, p in enumerate(loc):
        if p == "__root__":
            continue
        labels: dict[str, str] = GROUP_LABELS if i and loc[i - 1] == "weights" else FIELD_LABELS
        names.append(labels.get(str(p), str(p)))
    return " / ".join(names[-2:])


def format_errors(e: ValidationError) -> str:
    """把 pydantic 校验错误翻译成中文，多条用分号隔开"""
    lines: list[str] = []
    for err in e.errors():
        name: str = field_name(err.get("loc", ()))
        msg: str = error_text(err)
        lines.append(f"{name}：{msg}" if name else msg)
    return "；".join(dict.fromkeys(lines)) or "设置内容不正确"


class SettingsError(ValueError):
    """设置校验失败（消息已是中文）"""


# ---------------------------------------------------------------- 读写

def defaults() -> Settings:
    return Settings()


def deep_merge(base: dict, patch: dict) -> dict:
    """patch 覆盖 base（字典递归合并，其他类型直接替换），返回新字典"""
    out: dict = deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def validate(data: dict) -> Settings:
    """完整校验；失败抛 SettingsError（中文）"""
    try:
        return Settings.model_validate(data)
    except ValidationError as e:
        raise SettingsError(format_errors(e)) from None


def merge_update(current: Settings, patch: dict) -> Settings:
    """在当前设置上应用部分修改；patch 里可带 "preset": "稳健" 先套用风格再覆盖其余字段"""
    if not isinstance(patch, dict):
        raise SettingsError("设置内容格式不对")
    patch = dict(patch)
    preset: Any = patch.pop("preset", None)
    if preset:
        # 同时切换预测类型时，按新类型取风格门槛
        kind: Any = patch["predict"].get("kind") if isinstance(patch.get("predict"), dict) else None
        if kind:
            current = validate(deep_merge(current.model_dump(), {"predict": {"kind": kind}}))
        current = apply_preset(current, str(preset))
    return validate(deep_merge(current.model_dump(), patch))


def apply_preset(s: Settings, name: str) -> Settings:
    if name not in PRESETS:
        raise SettingsError(f"没有「{name}」这套风格，可选：{'、'.join(PRESETS)}")
    preset: dict = PRESETS[name]
    data: dict = s.model_dump()
    data["predict"].update(deepcopy(preset.get("predict", {})))
    data["trade"].update(deepcopy(preset.get("trade", {})))
    kind: str = data["predict"]["kind"]
    over: dict = preset.get("kind_overrides", {}).get(kind, {})
    data["predict"].update(deepcopy(over.get("predict", {})))
    data["trade"].update(deepcopy(over.get("trade", {})))
    by_kind: dict = preset.get("threshold_by_kind", {})
    if kind in by_kind:
        data["predict"]["threshold"] = by_kind[kind]
    return validate(data)


def defaults_for(kind: str) -> dict:
    """某个模型的完整默认设置 {"predict": {...}, "trade": {...}}（网页切换模型时用来填默认值）"""
    if kind not in KIND_DEFAULTS:
        raise SettingsError(f"不认识的模型类型「{kind}」，可选：{'、'.join(f'{k}（{v}）' for k, v in KINDS.items())}")
    base: dict = Settings().model_dump()
    over: dict = KIND_DEFAULTS[kind]
    predict: dict = {**base["predict"], **deepcopy(over.get("predict", {})), "kind": kind}
    trade: dict = {**base["trade"], **deepcopy(over.get("trade", {}))}
    s: Settings = validate({"predict": predict, "trade": trade})
    return {"predict": s.predict.model_dump(), "trade": s.trade.model_dump()}


def for_kind(s: Settings, kind: str) -> Settings:
    """把设置切到某个模型：kind 相同原样返回；不同时 KIND_FIELDS 里的字段（门槛、每天几只、板块、仓位、
    最多持有、卖出规则、开盘涨幅上限）换成该模型的默认值，其余（权重、价格/市值筛选、资金、费用、止损等）保留"""
    if s.predict.kind == kind:
        return s
    d: dict = defaults_for(kind)
    data: dict = s.model_dump()
    data["predict"]["kind"] = kind
    for part, fields in KIND_FIELDS.items():
        for f in fields:
            data[part][f] = deepcopy(d[part][f])
    return validate(data)


def _load_part(model: type[BaseModel], raw: Any) -> BaseModel:
    try:
        return model.model_validate(raw if isinstance(raw, dict) else {})
    except ValidationError:
        return model()


def load() -> Settings:
    """读取设置；文件不存在返回默认值；某一部分损坏时只把那部分恢复默认"""
    path = config.SETTINGS_FILE
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):       # 文件不存在或损坏
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    try:
        return Settings.model_validate(raw)
    except ValidationError:
        parts: dict[str, Any] = {name: _load_part(model, raw.get(name)) for name, model in PARTS.items()}
        return Settings(
            **parts,
            auto_update=raw.get("auto_update") if isinstance(raw.get("auto_update"), bool) else True,
            risk_ack=raw.get("risk_ack") if isinstance(raw.get("risk_ack"), bool) else False,
        )


def exists() -> bool:
    """用户是否保存过设置（设置文件存在）；没有时 load() 返回的是默认值"""
    return config.SETTINGS_FILE.exists()


def save(s: Settings) -> None:
    """原子写入：先写临时文件再替换，断电也不会留下半个文件"""
    path = config.SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text: str = json.dumps(s.model_dump(), ensure_ascii=False, indent=2)
    with _lock:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
