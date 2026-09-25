"""
实验室的下拉选项（股票范围、预测目标、参数预设）和一次训练配置的校验。因子组在 factors.py，模型在 models.py。
"""
from __future__ import annotations

from datetime import date
from typing import Any

UNIVERSES: dict[str, dict[str, Any]] = {
    "main": {"label": "沪深主板（推荐：和你的交易权限一致）", "boards": ["main"],
             "desc": "60、00 开头的主板股票，排除 ST、上市不满 60 天、20 日平均成交额不到 5000 万的"},
    "hs300": {"label": "沪深300 成分股", "boards": ["main", "chinext", "star"], "index": "hs300",
              "desc": "大盘蓝筹。注意：用的是现在的成分股回看历史（有幸存者偏差），结果会偏乐观"},
    "zz500": {"label": "中证500 成分股", "boards": ["main", "chinext", "star"], "index": "zz500",
              "desc": "中盘股。注意：用的是现在的成分股回看历史（有幸存者偏差），结果会偏乐观"},
    "all": {"label": "全部 A 股", "boards": ["main", "chinext", "star", "bj"],
            "desc": "含创业板、科创板、北交所（需要对应的交易权限）。股票最多、训练最慢"},
    "watchlist": {"label": "我的自选股", "boards": ["main", "chinext", "star", "bj"],
                  "desc": "只用自选股训练和评估。股票太少时（少于 50 只）结果基本不可信"},
}

# 预测目标：全部按"信号日收盘后 → 第二天开盘买（开盘涨停买不进）→ 持有 N 天收盘卖（跌停卖不出顺延）→ 扣成本"计算，
# 超额 = 这只股票的净收益 − 同一天范围内所有买得进的股票的平均净收益（即"同日随机买"的基准）
LABELS: dict[str, dict[str, Any]] = {
    "excess_5": {"label": "未来 5 天的超额收益", "hold": 5, "task": "reg", "kind": "excess",
                 "desc": "预测买进后 5 个交易日比同一天随便买多赚多少。适合短线，换手高、成本占比大"},
    "excess_10": {"label": "未来 10 天的超额收益（推荐）", "hold": 10, "task": "reg", "kind": "excess",
                  "desc": "预测买进后 10 个交易日比同一天随便买多赚多少。和你“波段、晚上看盘”的习惯最匹配"},
    "excess_20": {"label": "未来 20 天的超额收益", "hold": 20, "task": "reg", "kind": "excess",
                  "desc": "持有约一个月。信号更慢、更稳，但样本重叠多，t 值要更严格地看"},
    "rank_10": {"label": "未来 10 天收益的排名", "hold": 10, "task": "reg", "kind": "rank",
                "desc": "只预测同一天里谁涨得相对多（排名），不管具体多少。对极端涨跌不敏感，通常更稳"},
    "up5_10": {"label": "10 天内赚超过 5%（分类）", "hold": 10, "task": "cls", "kind": "up", "threshold": 0.05,
               "desc": "预测“买进 10 天后净赚超过 5%”的概率。直观，但丢掉了涨多少的信息"},
}

# 参数预设：滚动训练的步长（每隔几个月重新训练一次）+ 模型的轮数/深度（在 models.py 按预设取）
PRESETS: dict[str, dict[str, Any]] = {
    "fast": {"label": "快速", "step_months": 12, "desc": "每年重训一次、模型小，几分钟到十几分钟，先看看方向"},
    "standard": {"label": "标准（推荐）", "step_months": 6, "desc": "每半年重训一次，和系统里原有模型的做法一样"},
    "fine": {"label": "精细", "step_months": 3, "desc": "每季度重训一次、模型更大，最慢（可能要一小时以上）"},
}

NORMALIZE: dict[str, str] = {
    "rank": "按天截面排名（推荐：每天把因子换成当天的相对位置，不怕极端值）",
    "none": "原始数值（只适合树模型）",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "universe": "main", "factor_sets": ["basic", "ta", "mainforce", "fundamental"], "label": "excess_10",
    "model": "lightgbm", "preset": "standard", "start_year": 2020, "end": None, "top_k": 10, "normalize": "rank",
    "min_amount": 5e7, "note": "",
}
MIN_START_YEAR: int = 2019


def validate_config(raw: dict | None) -> dict:
    """补全默认值并检查；出错抛中文 ValueError"""
    from . import factors, models

    cfg: dict[str, Any] = {**DEFAULT_CONFIG, **{k: v for k, v in (raw or {}).items() if v is not None}}
    if cfg["universe"] not in UNIVERSES:
        raise ValueError(f"不认识的股票范围「{cfg['universe']}」")
    if cfg["label"] not in LABELS:
        raise ValueError(f"不认识的预测目标「{cfg['label']}」")
    if cfg["preset"] not in PRESETS:
        raise ValueError(f"不认识的参数预设「{cfg['preset']}」")
    if cfg["normalize"] not in NORMALIZE:
        raise ValueError(f"不认识的标准化方式「{cfg['normalize']}」")
    sets = cfg["factor_sets"]
    if isinstance(sets, str):
        sets = [sets]
    sets = list(dict.fromkeys(str(s) for s in sets))
    if not sets:
        raise ValueError("至少要选一个因子组")
    for s in sets:
        if s not in factors.FACTOR_SETS:
            raise ValueError(f"不认识的因子组「{s}」")
    cfg["factor_sets"] = sets
    spec = models.get(cfg["model"])                        # 不认识会抛 ValueError
    task: str = LABELS[cfg["label"]]["task"]
    if task not in spec.tasks:
        raise ValueError(f"模型“{spec.label}”不能做{'分类' if task == 'cls' else '数值'}预测，请换一个预测目标或模型")
    if cfg["normalize"] == "none" and spec.family not in ("gbdt", "forest"):
        raise ValueError(f"模型“{spec.label}”需要标准化后的因子（原始数值只适合树模型），请选“按天截面排名”")
    try:
        cfg["start_year"] = int(cfg["start_year"])
    except (TypeError, ValueError):
        raise ValueError("起始年份要是整数") from None
    this_year: int = date.today().year
    if not MIN_START_YEAR <= cfg["start_year"] <= this_year - 1:
        raise ValueError(f"起始年份要在 {MIN_START_YEAR} 到 {this_year - 1} 之间")
    if cfg.get("end"):
        cfg["end"] = str(date.fromisoformat(str(cfg["end"])[:10]))
    try:
        cfg["top_k"] = int(cfg["top_k"])
        cfg["min_amount"] = float(cfg["min_amount"])
    except (TypeError, ValueError):
        raise ValueError("每期选几只、成交额下限要是数字") from None
    if not 1 <= cfg["top_k"] <= 100:
        raise ValueError("每期选几只要在 1 到 100 之间")
    if not 0 <= cfg["min_amount"] <= 1e10:
        raise ValueError("成交额下限不合理")
    cfg["note"] = str(cfg.get("note") or "")[:100]
    return {k: cfg[k] for k in DEFAULT_CONFIG}


def hold_of(cfg: dict) -> int:
    return int(LABELS[cfg["label"]]["hold"])


def task_of(cfg: dict) -> str:
    return str(LABELS[cfg["label"]]["task"])


def describe(cfg: dict) -> str:
    """一句话描述一次训练配置（列表和提醒里用）"""
    from . import factors, models

    sets: str = "+".join(factors.FACTOR_SETS[s]["short"] for s in cfg["factor_sets"])
    return (f"{UNIVERSES[cfg['universe']]['label'].split('（')[0]} · {sets} · {LABELS[cfg['label']]['label'].split('（')[0]} · "
            f"{models.get(cfg['model']).label.split('（')[0]} · {PRESETS[cfg['preset']]['label'].split('（')[0]}")
