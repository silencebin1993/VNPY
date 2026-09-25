"""
策略模板：每个模板说明"用什么选股、怎么买、怎么卖、适合谁"。参数都可以在页面上改（resolve 合并并校验）。
历史回测结论写在各自的说明里；只有样本外显著跑赢同池随机的才标 verified（目前只有"量化选股 每周调仓"）。
信号类型：scheme（选股器方案）/ model（模型实验室启用的模型）/ mf（量化选股的每周组合：规则固定，回测就是量化选股页的回测）。
"""
from __future__ import annotations

from typing import Any

ENTRY: dict[str, str] = {
    "open": "第二天开盘买（开盘涨停买不进就算了）",
    "pullback": "第二天挂低 2% 的限价单（回踩才买，没回踩就不买）",
}
TRAIL: dict[str, str] = {
    "none": "固定止损（不移动）",
    "breakeven": "赚到 1 倍风险后，止损提到买入价（保本）",
    "trail_pct8": "止损跟着最高收盘价走：始终在它下方 8%",
    "trail_atr": "止损跟着最高收盘价走：它下方 2 倍平均波幅",
    "ma10": "收盘跌破 10 日线就卖",
    "ma20": "收盘跌破 20 日线就卖",
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "mf_weekly": {
        "name": "量化选股 每周调仓", "signal": {"type": "mf"}, "fixed": True, "verified": True,
        "who": "想用样本外验证过的方法、每周只花一次时间的人", "defaults": {"entry": "open", "trail": "none", "target_r": 0.0, "max_days": 250,
                                                              "max_positions": 50, "exit_distribution": False, "regime": False},
        "desc": "跟随“量化选股”页的每周组合（多因子 + LightGBM，50 只等权）：每周最后一个交易日收盘后定组合，"
                "下一个交易日开盘卖掉掉出组合的、买入新进组合的；单只不设止损、周中不换股——和量化选股的回测完全同一套规则。"
                "这是目前唯一在样本外显著跑赢同池随机的方法。",
    },
    "reversal_value": {
        "name": "反转 + 低估值 波段", "signal": {"type": "scheme", "scheme_id": "reversal_value"},
        "who": "只能晚上看盘、想少折腾的人", "defaults": {"entry": "open", "trail": "breakeven", "target_r": 2.0, "max_days": 20,
                                                   "max_positions": 5, "exit_distribution": True, "regime": True},
        "desc": "盈利的公司里，最近跌得多、估值低、换手低的排前面（学术研究里 A 股比较稳定的规律，事先定好、没用我们的数据调参）。"
                "选股方案回测：收益和随机差不多，但回撤明显更小。",
    },
    "trend_swing": {
        "name": "趋势动量波段", "signal": {"type": "scheme", "scheme_id": "trend_swing"},
        "who": "相信“强者恒强”、能接受大起大落的人", "defaults": {"entry": "open", "trail": "trail_atr", "target_r": 0.0, "max_days": 30,
                                                          "max_positions": 5, "exit_distribution": True, "regime": True},
        "desc": "相对强度高、均线多头的股票，跟着趋势拿，跌破移动止损就走。注意：选股方案回测里这类方法明显跑输随机（年化约 −21%）。",
    },
    "mainforce_follow": {
        "name": "主力吸筹 → 拉升跟随", "signal": {"type": "scheme", "scheme_id": "mainforce_follow"},
        "who": "想跟着“主力”做、愿意严格止损的人", "defaults": {"entry": "open", "trail": "trail_pct8", "target_r": 0.0, "max_days": 30,
                                                         "max_positions": 5, "exit_distribution": True, "regime": True},
        "desc": "按量价证据判断主力在吸筹或刚开始拉升时买，出现出货迹象或跌破移动止损就卖。注意：主力阶段标签的历史验证没有跑赢随机。",
    },
    "washout_dip": {
        "name": "洗盘回踩低吸", "signal": {"type": "scheme", "scheme_id": "washout_dip"},
        "who": "不想追高、愿意等回调的人", "defaults": {"entry": "pullback", "trail": "ma20", "target_r": 2.0, "max_days": 20,
                                                   "max_positions": 5, "exit_distribution": True, "regime": True},
        "desc": "涨过一波后缩量回踩 20 日线附近的股票，第二天回踩 2% 才买，收盘跌破 20 日线就卖。",
    },
    "value_trend": {
        "name": "价值成长 + 趋势", "signal": {"type": "scheme", "scheme_id": "value_growth"},
        "who": "偏好基本面、持有时间长一点的人", "defaults": {"entry": "open", "trail": "trail_pct8", "target_r": 0.0, "max_days": 40,
                                                       "max_positions": 6, "exit_distribution": True, "regime": True},
        "desc": "估值不贵、赚钱效率高、利润在增长的公司，加一点相对强度避免“便宜的烂股”。持有时间长，换手少。",
    },
    "model_rotation": {
        "name": "多因子模型轮动", "signal": {"type": "model"},
        "who": "在模型实验室训练过模型、并且它在留出期有优势的人", "defaults": {"entry": "open", "trail": "none", "target_r": 0.0, "max_days": 10,
                                                                  "max_positions": 8, "exit_distribution": False, "regime": True},
        "desc": "每天买实验室启用的模型打分最高的股票，持有到期（默认 10 天，和训练目标一致）就卖。回测只用模型的样本外预测。"
                "需要先在模型实验室训练并启用一个模型。",
    },
}

LIMITS: dict[str, tuple[float, float]] = {"target_r": (0.0, 10.0), "max_days": (1, 250), "max_positions": (1, 30)}
PARAM_NAMES: dict[str, str] = {"target_r": "目标（几倍风险，0 = 不设）", "max_days": "最长持有天数", "max_positions": "最多同时持有几只"}


def resolve(template_id: str, params: dict | None = None) -> dict:
    """模板 + 用户改的参数 → 一份完整、校验过的策略设置"""
    if template_id not in TEMPLATES:
        raise ValueError(f"不认识的策略模板「{template_id}」")
    t: dict = TEMPLATES[template_id]
    if t.get("fixed"):                                     # 规则固定的模板（量化选股）：参数不能改，回测才对得上
        return {"template": template_id, "name": t["name"], "signal": dict(t["signal"]), **t["defaults"]}
    p: dict = {**t["defaults"], **{k: v for k, v in (params or {}).items() if k in t["defaults"] and v is not None}}
    if p["entry"] not in ENTRY:
        raise ValueError("买入方式不对")
    if p["trail"] not in TRAIL:
        raise ValueError("卖出规则不对")
    for k, (lo, hi) in LIMITS.items():
        try:
            v = float(p[k])
        except (TypeError, ValueError):
            raise ValueError(f"{k} 要是数字") from None
        if not lo <= v <= hi:
            raise ValueError(f"{PARAM_NAMES[k]}要在 {lo:g} 到 {hi:g} 之间")
        p[k] = int(v) if k != "target_r" else float(v)
    p["exit_distribution"] = bool(p["exit_distribution"])
    p["regime"] = bool(p["regime"])
    return {"template": template_id, "name": t["name"], "signal": dict(t["signal"]), **p}


def listing() -> list[dict]:
    return [{"id": k, "name": v["name"], "who": v["who"], "desc": v["desc"], "signal": v["signal"], "defaults": v["defaults"],
             "fixed": bool(v.get("fixed")), "verified": bool(v.get("verified"))} for k, v in TEMPLATES.items()]
