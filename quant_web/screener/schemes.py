"""
选股器的"积木"：可用作条件的字段（FIELDS）、打分方案（SCORING）和内置选股方案（PRESETS）。

打分：每个因子先在"当天所有符合范围的股票"里换算成百分位（0~100，缺数据按 50），再按权重加权平均；
方向为负的因子（如 PE、离均线太远）按"越小越好"换算。打分只是排序工具，它有没有用以回测（样本外）为准。
"""
from __future__ import annotations

# 字段：key → (中文名, 单位/说明)。都在选股当天的数据上计算；价格类用真实价格
FIELDS: dict[str, tuple[str, str]] = {
    "close": ("收盘价", "元（真实价格）"),
    "pct": ("今日涨跌幅", "%"),
    "ret5": ("5 日涨幅", "%"), "ret20": ("20 日涨幅", "%"), "ret60": ("60 日涨幅", "%"),
    "rps120": ("相对强度（半年）", "0~100，越大越强"),
    "pos250": ("一年区间位置", "0=一年最低，1=一年最高"),
    "bias20": ("离 20 日线", "%，正数在线上"),
    "vr20": ("量比（对 20 日均量）", "倍"),
    "udr20": ("阳量 ÷ 阴量（20 日）", "倍"),
    "turn": ("换手率", "%"),
    "amt20": ("20 日平均成交额", "亿元"),
    "float_cap": ("流通市值", "亿元（估算）"),
    "atr_pct": ("平均波幅占股价", "%"),
    "winner": ("获利比例（筹码）", "0~1"),
    "conc90": ("筹码集中度", "越小越集中"),
    "pe_ttm": ("市盈率（TTM）", "倍，亏损为负"),
    "pb": ("市净率", "倍"),
    "roe": ("净资产收益率", "%（报告期累计）"),
    "profit_yoy": ("净利润同比", "%"),
    "revenue_yoy": ("营业收入同比", "%"),
    "debt_ratio": ("资产负债率", "%"),
}
# 显示时的换算（内部存小数的字段 → 百分数/亿元）
DISPLAY_SCALE: dict[str, float] = {"pct": 100, "ret5": 100, "ret20": 100, "ret60": 100, "bias20": 100,
                                   "atr_pct": 100, "amt20": 1e-8, "float_cap": 1e-8}

OPS: dict[str, str] = {">": "大于", ">=": "不小于", "<": "小于", "<=": "不大于", "between": "介于"}

# 打分因子：key → (中文名, 方向, 理由模板)；理由模板里的 {p} 为百分位
TERMS: dict[str, tuple[str, int, str]] = {
    "rps120": ("相对强度", 1, "半年相对强度排前 {top}%"),
    "ret20": ("20 日涨幅", 1, "20 日涨幅排前 {top}%"),
    "pos250": ("一年区间位置", 1, "处在一年区间的高位"),
    "ma_bull": ("均线多头", 1, "均线多头排列"),
    "vr20": ("放量程度", 1, "成交量明显放大"),
    "bias20": ("离 20 日线", -1, "离 20 日线不远（没有涨过头）"),
    "score_markup": ("拉升证据", 1, "拉升的证据充分"),
    "score_accumulation": ("吸筹证据", 1, "有吸筹迹象"),
    "score_washout": ("洗盘证据", 1, "像是洗盘后的回调"),
    "score_distribution": ("出货证据", -1, "没有出货迹象"),
    "score_decline": ("下跌证据", -1, "不在下跌趋势中"),
    "udr20": ("阳量阴量比", 1, "上涨日成交量明显更大"),
    "obv_slope": ("OBV 趋势", 1, "OBV 在往上走（资金流入）"),
    "conc90": ("筹码集中度", -1, "筹码比较集中"),
    "rally60": ("前期涨幅", 1, "前期有过一波上涨"),
    "vol_shrink": ("回调缩量", -1, "最近缩量"),
    "dist_ma20": ("贴近 20 日线", -1, "回踩到 20 日线附近"),
    "pe_ttm": ("市盈率", -1, "估值相对便宜"),
    "pb": ("市净率", -1, "市净率较低"),
    "roe": ("净资产收益率", 1, "赚钱效率高（ROE 高）"),
    "profit_yoy": ("利润增长", 1, "利润增长快"),
    "revenue_yoy": ("收入增长", 1, "收入增长快"),
    "debt_ratio": ("负债率", -1, "负债率较低"),
    "rev20": ("短期超跌（反转）", -1, "最近 20 天跌得多（短期反转）"),
    "turn20": ("换手率", -1, "换手率低（没有被炒热）"),
    "float_cap": ("流通市值", -1, "市值偏小"),
}

SCORING: dict[str, dict] = {
    "momentum": {"name": "趋势动量", "desc": "强者恒强：相对强度高、均线多头、处在高位但没有涨过头。",
                 "weights": {"rps120": 2, "ret20": 1, "pos250": 1, "ma_bull": 1, "vr20": 0.5, "bias20": 1}},
    "mainforce": {"name": "量价主力", "desc": "按主力阶段的证据打分：拉升/吸筹/洗盘加分，出货/下跌扣分，量能和筹码辅助。",
                  "weights": {"score_markup": 1.5, "score_accumulation": 1, "score_washout": 1, "udr20": 1, "obv_slope": 1,
                              "conc90": 1, "score_distribution": 2, "score_decline": 1.5}},
    "pullback": {"name": "洗盘低吸", "desc": "涨过一波后缩量回踩 20 日线附近，趋势还在。",
                 "weights": {"score_washout": 2, "rally60": 1, "vol_shrink": 1, "dist_ma20": 1, "rps120": 1}},
    "value": {"name": "价值成长", "desc": "估值不贵、赚钱效率高、利润和收入在增长，负债不高；加一点相对强度避免“便宜的烂股”。",
              "weights": {"pe_ttm": 1.5, "pb": 0.5, "roe": 1.5, "profit_yoy": 1, "revenue_yoy": 0.5, "debt_ratio": 0.5, "rps120": 0.5}},
    "reversal_value": {"name": "反转 + 低估值 + 低换手",
                       "desc": "按学术研究里 A 股较稳定的规律事先定的：最近跌得多、估值低、换手低、市值偏小、赚钱效率不太差的股票排前面（没有用我们的数据调参）。",
                       "weights": {"pe_ttm": 1.5, "turn20": 1, "rev20": 1, "float_cap": 0.5, "roe": 0.5}},
    "custom": {"name": "自定义权重", "desc": "自己决定每个因子的权重（0 表示不用）。", "weights": {}},
}

STAGES: dict[str, str] = {"accumulation": "吸筹", "washout": "洗盘", "markup": "拉升", "distribution": "出货",
                          "decline": "下跌", "unclear": "不明确"}

# 内置方案（默认只看用户能交易的板块：boards=None 表示用"我的情况"里的板块）
PRESETS: list[dict] = [
    {
        "id": "reversal_value", "name": "反转 + 低估值 + 低换手", "builtin": True,
        "desc": "按学术研究里 A 股较稳定的规律事先定好的方案（没有用我们的数据调参）：盈利的公司里，最近跌得多、估值低、换手低、市值偏小的排前面；排除出货阶段和排雷红黄灯。是否有用以回测为准。",
        "universe": {"boards": None, "exclude_st": True, "min_amount": 3e7, "price_min": 2, "price_max": 1000},
        "conditions": [
            {"type": "field", "field": "pe_ttm", "op": "between", "value": [0, 40]},
            {"type": "stage", "exclude": ["distribution"]},
        ],
        "match": "all", "risk": {"exclude_red": True, "exclude_yellow": True},
        "scoring": {"scheme": "reversal_value"}, "top_n": 20,
    },
    {
        "id": "trend_swing", "name": "趋势突破波段", "builtin": True,
        "desc": "最常见的“买强势股”思路：相对强度高、均线多头、离 20 日线不远，排除出货/下跌阶段和排雷红灯。注意：历史回测在 A 股明显跑输随机（追强势容易买在短期高点），留着用来对比学习。",
        "universe": {"boards": None, "exclude_st": True, "min_amount": 5e7, "price_min": 2, "price_max": 500},
        "conditions": [
            {"type": "field", "field": "rps120", "op": ">=", "value": 70},
            {"type": "field", "field": "bias20", "op": "<=", "value": 0.15},
            {"type": "stage", "exclude": ["distribution", "decline"]},
            {"type": "formula", "fid": "ma_bull"},
        ],
        "match": "all", "risk": {"exclude_red": True, "exclude_yellow": False},
        "scoring": {"scheme": "momentum"}, "top_n": 20,
    },
    {
        "id": "mainforce_follow", "name": "主力吸筹→拉升跟随", "builtin": True,
        "desc": "主力阶段判为吸筹或拉升、上涨日量能占优的股票。注意：历史回测没有跑赢随机，只作观察。",
        "universe": {"boards": None, "exclude_st": True, "min_amount": 5e7, "price_min": 2, "price_max": 500},
        "conditions": [
            {"type": "stage", "include": ["accumulation", "markup"]},
            {"type": "field", "field": "udr20", "op": ">=", "value": 1.2},
        ],
        "match": "all", "risk": {"exclude_red": True, "exclude_yellow": False},
        "scoring": {"scheme": "mainforce"}, "top_n": 20,
    },
    {
        "id": "washout_dip", "name": "洗盘回踩低吸", "builtin": True,
        "desc": "涨过一波后缩量回踩 20 日线附近（主力阶段判为洗盘）。注意：历史回测没有跑赢随机，只作观察。",
        "universe": {"boards": None, "exclude_st": True, "min_amount": 5e7, "price_min": 2, "price_max": 500},
        "conditions": [
            {"type": "stage", "include": ["washout"]},
            {"type": "field", "field": "bias20", "op": "between", "value": [-0.04, 0.04]},
        ],
        "match": "all", "risk": {"exclude_red": True, "exclude_yellow": False},
        "scoring": {"scheme": "pullback"}, "top_n": 20,
    },
    {
        "id": "value_growth", "name": "价值成长 + 趋势", "builtin": True,
        "desc": "市盈率 0~30 倍、ROE 不低于 8%、利润同比增长超过 10%，并且股价在年线之上（不接下跌中的“便宜货”）。",
        "universe": {"boards": None, "exclude_st": True, "min_amount": 3e7, "price_min": 2, "price_max": 1000},
        "conditions": [
            {"type": "field", "field": "pe_ttm", "op": "between", "value": [0, 30]},
            {"type": "field", "field": "roe", "op": ">=", "value": 8},
            {"type": "field", "field": "profit_yoy", "op": ">=", "value": 10},
            {"type": "formula", "text": "XG:C>MA(C,250);"},
            {"type": "stage", "exclude": ["distribution"]},
        ],
        "match": "all", "risk": {"exclude_red": True, "exclude_yellow": True},
        "scoring": {"scheme": "value"}, "top_n": 20,
    },
    {
        "id": "breakout", "name": "放量突破平台", "builtin": True,
        "desc": "内置公式“放量突破平台”选出的股票，按趋势动量排序。注意：这个公式的历史验证没有优势，仅供对比学习。",
        "universe": {"boards": None, "exclude_st": True, "min_amount": 5e7, "price_min": 2, "price_max": 500},
        "conditions": [{"type": "formula", "fid": "breakout_volume"}],
        "match": "all", "risk": {"exclude_red": True, "exclude_yellow": False},
        "scoring": {"scheme": "momentum"}, "top_n": 20,
    },
]
PRESET_BY_ID: dict[str, dict] = {p["id"]: p for p in PRESETS}


def validate_scheme(s: dict) -> dict:
    """检查并补全一个选股方案（前端传来或读自文件）；有问题抛 ValueError（中文）"""
    if not isinstance(s, dict):
        raise ValueError("选股方案格式不对")
    out: dict = {k: s.get(k) for k in ("id", "name", "desc")}
    out["name"] = str(out.get("name") or "我的方案").strip()[:40]
    uni: dict = dict(s.get("universe") or {})
    boards = uni.get("boards")
    if boards is not None:
        boards = [b for b in boards if b in ("main", "chinext", "star", "bj")]
        if not boards:
            raise ValueError("至少要选一个板块")
    out["universe"] = {"boards": boards, "exclude_st": bool(uni.get("exclude_st", True)),
                       "min_amount": max(0.0, float(uni.get("min_amount") or 0)),
                       "price_min": max(0.0, float(uni.get("price_min") or 0)),
                       "price_max": float(uni.get("price_max") or 1e9)}
    conds: list[dict] = []
    for c in s.get("conditions") or []:
        t = c.get("type")
        if t == "field":
            if c.get("field") not in FIELDS:
                raise ValueError(f"不认识的字段「{c.get('field')}」")
            if c.get("op") not in OPS:
                raise ValueError(f"不认识的比较方式「{c.get('op')}」")
            v = c.get("value")
            if c["op"] == "between":
                if not (isinstance(v, list | tuple) and len(v) == 2):
                    raise ValueError(f"「{FIELDS[c['field']][0]}」介于的两个数要都填")
                v = [float(v[0]), float(v[1])]
            else:
                v = float(v)
            conds.append({"type": "field", "field": c["field"], "op": c["op"], "value": v})
        elif t == "formula":
            if not (c.get("fid") or c.get("text")):
                raise ValueError("公式条件要选一个公式或填写公式")
            conds.append({"type": "formula", "fid": c.get("fid"), "text": c.get("text")})
        elif t == "stage":
            inc = [x for x in (c.get("include") or []) if x in STAGES]
            exc = [x for x in (c.get("exclude") or []) if x in STAGES]
            conds.append({"type": "stage", "include": inc, "exclude": exc})
        else:
            raise ValueError(f"不认识的条件类型「{t}」")
    out["conditions"] = conds
    out["match"] = "any" if s.get("match") == "any" else "all"
    risk: dict = dict(s.get("risk") or {})
    out["risk"] = {"exclude_red": bool(risk.get("exclude_red", True)), "exclude_yellow": bool(risk.get("exclude_yellow", False))}
    sc: dict = dict(s.get("scoring") or {"scheme": "momentum"})
    scheme: str = sc.get("scheme") or "momentum"
    if scheme not in SCORING and scheme != "model":
        raise ValueError(f"不认识的打分方案「{scheme}」")
    weights: dict = {k: float(v) for k, v in (sc.get("weights") or {}).items() if k in TERMS}
    if scheme == "custom" and not any(abs(w) > 0 for w in weights.values()):
        raise ValueError("自定义打分至少要给一个因子设置权重")
    out["scoring"] = {"scheme": scheme, "weights": weights, "model_run": sc.get("model_run")}
    out["top_n"] = int(min(max(int(s.get("top_n") or 20), 1), 200))
    return out
