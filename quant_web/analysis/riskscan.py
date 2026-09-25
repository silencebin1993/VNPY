"""
排雷：买入前检查明显的风险（红 = 建议回避，黄 = 注意，绿 = 未发现，灰 = 没有数据）。

检查项（每项都说明依据和数据日期；缺数据时标"没有数据"，不当作没风险）：
ST/退市、面值退市、市值退市、流动性、次新、亏损/净资产为负、业绩预告、限售解禁、股权质押、股东减持、
主力阶段（出货/下跌）、监管关键词（立案/问询/警示等，只查本地新闻）。
单只股票用 scan_stock()；选股器批量用 scan_many()（只做不联网、全市场能批量算的项目）。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

LEVELS: dict[str, int] = {"red": 3, "yellow": 2, "green": 1, "none": 0}
LEVEL_TEXT: dict[str, str] = {"red": "建议回避", "yellow": "需要注意", "green": "未发现", "none": "没有数据"}
BAD_FORECAST: dict[str, str] = {"首亏": "red", "预亏": "red", "续亏": "red", "预减": "yellow", "略减": "yellow", "增亏": "red"}
REG_WORDS: tuple[str, ...] = ("立案", "调查", "问询函", "警示函", "处罚", "退市风险", "被实施", "违规", "监管函")


def item(key: str, title: str, level: str, detail: str, when: str | None = None) -> dict:
    text = LEVEL_TEXT[level] if not (level == "red" and key not in HARD_KEYS) else "风险提示"     # 软风险的红灯不说"建议回避"
    return {"key": key, "title": title, "level": level, "level_text": text, "detail": detail, "date": when}


# "硬"风险：退市 / ST / 面值和市值退市线 / 资不抵债 / 成交极冷卖不掉 / 监管立案处罚——这些是规则层面的硬伤，红灯时交易页直接禁止买入。
# 其余（亏损、业绩预告、解禁、质押、减持、主力阶段、次新）是"软"提示：红灯只要求你确认。
# 依据：量化选股样本外持仓里，近四季亏损的股票下一周超额 +0.70%/周，盈利的 +0.34%/周（同周配对差 +0.08%，t 0.4，没有显著差别）；
# 选股时直接去掉亏损股，年化 22.1% → 20.1%、超额 13.9% → 12.2%——并不更好。
HARD_KEYS: frozenset[str] = frozenset({"st", "par", "cap", "equity", "liq", "reg"})


def overall(items: list[dict]) -> str:
    lv: int = max((LEVELS[i["level"]] for i in items), default=0)
    return {3: "red", 2: "yellow"}.get(lv, "green")


def hard_red(items: list[dict]) -> list[dict]:
    return [i for i in items if i["level"] == "red" and i["key"] in HARD_KEYS]


def _ext(name: str) -> pl.DataFrame:
    try:
        from ..providers import store
        return store.load(name)
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def scan_stock(code: str, name: str | None, today: date, last_bar: dict | None, list_date: date | None,
               fundamentals: pl.DataFrame | None, stage_key: str | None, float_cap: float | None = None,
               total_cap: float | None = None, news_titles: list[tuple[str, str]] | None = None) -> dict:
    """一只股票的排雷结果：{level, items: [...]}"""
    items: list[dict] = []
    nm: str = name or ""
    # 1 ST / 退市
    if "退" in nm:
        items.append(item("st", "退市风险", "red", f"名称“{nm}”带“退”字：已进入退市整理，不要买"))
    elif "ST" in nm.upper():
        items.append(item("st", "ST 风险警示", "red", f"名称“{nm}”带 ST：公司有退市风险或财务异常，被交易所特别处理"))
    else:
        items.append(item("st", "ST / 退市", "green", "不是 ST 股"))
    # 2 面值退市（股价连续 20 天低于 1 元会被强制退市）
    price = (last_bar or {}).get("raw_close") or (last_bar or {}).get("close")
    if price is None:
        items.append(item("par", "面值退市风险", "none", "没有最新价格"))
    elif price < 1.2:
        items.append(item("par", "面值退市风险", "red", f"股价 {price:.2f} 元，接近 1 元（连续 20 个交易日低于 1 元会被强制退市）"))
    elif price < 2:
        items.append(item("par", "面值退市风险", "yellow", f"股价 {price:.2f} 元，低价股波动大，离 1 元退市线不远"))
    else:
        items.append(item("par", "面值退市风险", "green", f"股价 {price:.2f} 元"))
    # 3 市值退市（主板总市值连续 20 天低于 5 亿）
    cap = total_cap or float_cap
    if cap is None:
        items.append(item("cap", "市值退市风险", "none", "没有市值数据"))
    elif cap < 5e8:
        items.append(item("cap", "市值退市风险", "red", f"市值约 {cap / 1e8:.1f} 亿元，低于 5 亿（主板连续 20 天低于 5 亿会退市）"))
    elif cap < 10e8:
        items.append(item("cap", "市值退市风险", "yellow", f"市值约 {cap / 1e8:.1f} 亿元，是很小的公司"))
    else:
        items.append(item("cap", "市值", "green", f"市值约 {cap / 1e8:.0f} 亿元"))
    # 4 流动性
    amt20 = (last_bar or {}).get("amt20")
    if amt20 is None:
        items.append(item("liq", "成交活跃度", "none", "没有成交额数据"))
    elif amt20 < 1e7:
        items.append(item("liq", "成交活跃度", "red", f"20 天平均每天只成交 {amt20 / 1e4:.0f} 万元，想卖的时候可能卖不出去"))
    elif amt20 < 3e7:
        items.append(item("liq", "成交活跃度", "yellow", f"20 天平均每天成交 {amt20 / 1e8:.2f} 亿元，偏冷门"))
    else:
        items.append(item("liq", "成交活跃度", "green", f"20 天平均每天成交 {amt20 / 1e8:.1f} 亿元"))
    # 5 次新
    if list_date is not None:
        days = (today - list_date).days
        if days < 120:
            items.append(item("new", "次新股", "yellow", f"上市才 {days} 天，历史数据少、波动大", str(list_date)))
        else:
            items.append(item("new", "上市时间", "green", f"上市于 {list_date}"))
    # 6 亏损 / 净资产（按"已公布"的财报，避免用到当时还没公布的数据）
    if fundamentals is not None and fundamentals.height:
        f = fundamentals.filter(pl.col("code") == code)
        if "avail_date" in f.columns:
            f = f.filter(pl.col("avail_date") <= today)
        f = f.sort("report_date")
        if f.height:
            last = f.row(f.height - 1, named=True)
            ttm = last.get("net_profit_ttm")
            bvps = last.get("bvps")
            rep = str(last.get("report_date"))
            annual = f.filter(pl.col("report_date").dt.month() == 12).tail(2)
            losses = int((annual["net_profit"] < 0).sum()) if annual.height else 0
            if bvps is not None and bvps < 0:
                items.append(item("equity", "净资产为负", "red", f"每股净资产 {bvps:.2f} 元（资不抵债）", rep))
            if losses >= 2:
                items.append(item("loss", "连续亏损", "red", "最近两个年度都亏损", rep))
            elif ttm is not None and ttm < 0:
                items.append(item("loss", "亏损", "yellow", f"最近四个季度合计亏损 {abs(ttm) / 1e8:.2f} 亿元", rep))
            else:
                items.append(item("loss", "盈利情况", "green", "最近四个季度合计盈利" if ttm else "没有亏损记录", rep))
        else:
            items.append(item("loss", "盈利情况", "none", "没有财报数据"))
    else:
        items.append(item("loss", "盈利情况", "none", "没有财报数据（到“数据中心”更新财报）"))
    # 7 业绩预告（最近 120 天公告）
    fc = _ext("forecast")
    if fc.height:
        f = fc.filter((pl.col("code") == code) & (pl.col("notice_date") >= today - timedelta(days=120))).sort("notice_date")
        if f.height:
            r = f.row(f.height - 1, named=True)
            lv = BAD_FORECAST.get(str(r.get("kind")), "green")
            items.append(item("forecast", "业绩预告", lv, f"{r.get('notice_date')} 公告：{r.get('kind')}" + (f"（{r.get('summary')}）" if r.get("summary") else ""),
                              str(r.get("notice_date"))))
        else:
            items.append(item("forecast", "业绩预告", "green", "最近 4 个月没有业绩预告"))
    else:
        items.append(item("forecast", "业绩预告", "none", "还没有下载业绩预告（到“数据源”页更新扩展数据）"))
    # 8 限售解禁（未来 30 天）
    un = _ext("unlock")
    if un.height:
        u = un.filter((pl.col("code") == code) & (pl.col("date") >= today) & (pl.col("date") <= today + timedelta(days=30)))
        if u.height:
            ratio = float(u["float_ratio"].fill_null(0).sum())
            lv = "red" if ratio >= 20 else "yellow"
            items.append(item("unlock", "近期限售解禁", lv, f"未来 30 天有解禁，约占流通股 {ratio:.1f}%（解禁多可能带来抛压）", str(u["date"].min())))
        else:
            items.append(item("unlock", "限售解禁", "green", "未来 30 天没有解禁"))
    else:
        items.append(item("unlock", "限售解禁", "none", "还没有下载解禁数据"))
    # 9 股权质押
    pl_ = _ext("pledge")
    if pl_.height:
        p = pl_.filter(pl.col("code") == code).sort("date")
        if p.height:
            r = p.row(p.height - 1, named=True)
            ratio = r.get("pledge_ratio") or 0
            lv = "red" if ratio >= 50 else "yellow" if ratio >= 30 else "green"
            items.append(item("pledge", "股权质押", lv, f"质押股份约占总股本 {ratio:.1f}%" + ("，股价大跌可能触发平仓" if lv != "green" else ""),
                              str(r.get("date"))))
        else:
            items.append(item("pledge", "股权质押", "green", "没有质押记录"))
    else:
        items.append(item("pledge", "股权质押", "none", "还没有下载质押数据"))
    # 10 股东减持（最近 90 天公告）
    ht = _ext("holder_trades")
    if ht.height:
        h = ht.filter((pl.col("code") == code) & (pl.col("notice_date") >= today - timedelta(days=90)) & (pl.col("direction") == "减持"))
        if h.height:
            ratio = float(h["ratio_float"].fill_null(0).sum())
            lv = "red" if ratio >= 2 else "yellow"
            items.append(item("reduce", "股东减持", lv, f"最近 90 天有 {h.height} 条减持公告，合计约占流通股 {ratio:.2f}%", str(h["notice_date"].max())))
        else:
            items.append(item("reduce", "股东减持", "green", "最近 90 天没有减持公告"))
    else:
        items.append(item("reduce", "股东减持", "none", "还没有下载增减持数据"))
    # 11 主力阶段
    if stage_key in ("distribution", "decline"):
        items.append(item("stage", "主力阶段", "red" if stage_key == "distribution" else "yellow",
                          "疑似主力出货：高位放量滞涨/破位" if stage_key == "distribution" else "处在下跌趋势中"))
    elif stage_key:
        items.append(item("stage", "主力阶段", "green", "没有出货或下跌的迹象"))
    # 12 监管关键词（本地新闻标题）
    if news_titles is not None:
        hits = [(t, d) for t, d in news_titles if any(w in t for w in REG_WORDS)]
        if hits:
            items.append(item("reg", "监管 / 违规消息", "red", "；".join(t for t, _ in hits[:3]), hits[0][1]))
        else:
            items.append(item("reg", "监管 / 违规消息", "green", "最近的新闻里没有立案、问询、处罚等字样"))
    hard = hard_red(items)
    lv = overall(items)
    return {"level": lv, "level_text": "有风险提示" if lv == "red" and not hard else LEVEL_TEXT[lv], "items": items, "hard": bool(hard),
            "hard_titles": [i["title"] for i in hard], "red_titles": [i["title"] for i in items if i["level"] == "red"],
            "note": "排雷只能发现公开数据里的明显风险，不能保证没有其他问题。退市 / ST / 面值和市值退市线 / 资不抵债 / 成交极冷 / 监管处罚是硬伤；"
                    "亏损、业绩预告、解禁、质押、减持这些红灯只是提示——量化选股的历史持仓里，亏损股下一周并不比盈利股差。"}


def scan_many(frame_last: pl.DataFrame, today: date, fundamentals: pl.DataFrame | None = None) -> pl.DataFrame:
    """选股器批量排雷（只做本地能批量算的项目）：输入最新一天的表（code, name, raw_close, amt20, float_cap 可选, stage 可选），
    返回 code, risk（red/yellow/green）, risk_reasons（中文，分号隔开）"""
    df: pl.DataFrame = frame_last
    reasons: list[pl.Expr] = []
    red: list[pl.Expr] = []
    yellow: list[pl.Expr] = []

    def add(cond: pl.Expr, text: str, level: str) -> None:
        c = cond.fill_null(False)
        reasons.append(pl.when(c).then(pl.lit(text)).otherwise(None))
        (red if level == "red" else yellow).append(c)

    if "name" in df.columns:
        add(pl.col("name").str.contains("退"), "退市整理", "red")
        add(pl.col("name").str.to_uppercase().str.contains("ST"), "ST", "red")
    add(pl.col("raw_close") < 1.2, "股价接近1元", "red")
    add((pl.col("raw_close") >= 1.2) & (pl.col("raw_close") < 2), "低价股", "yellow")
    if "amt20" in df.columns:
        add(pl.col("amt20") < 1e7, "成交极冷", "red")
        add((pl.col("amt20") >= 1e7) & (pl.col("amt20") < 3e7), "成交偏冷", "yellow")
    if "stage" in df.columns:
        add(pl.col("stage") == "distribution", "疑似出货", "red")
        add(pl.col("stage") == "decline", "下跌趋势", "yellow")
    if fundamentals is not None and fundamentals.height:
        f = fundamentals
        if "avail_date" in f.columns:
            f = f.filter(pl.col("avail_date") <= today)
        latest = f.sort("report_date").group_by("code").last().select("code", "net_profit_ttm", "bvps")
        df = df.join(latest, on="code", how="left")
        add(pl.col("bvps") < 0, "净资产为负", "red")
        add(pl.col("net_profit_ttm") < 0, "近四季亏损", "yellow")
    fc = _ext("forecast")
    if fc.height:
        # 和单只诊断（scan_stock）同一个口径：首亏 / 预亏 / 续亏 / 增亏 红灯，预减 / 略减 黄灯
        recent = fc.filter((pl.col("notice_date") >= today - timedelta(days=120)) & pl.col("kind").is_in(list(BAD_FORECAST)))
        red_kinds = [k for k, v in BAD_FORECAST.items() if v == "red"]
        bad = recent.group_by("code").agg(pl.col("kind").is_in(red_kinds).any().alias("_fc_red"), pl.lit(True).alias("_bad_fc"))
        df = df.join(bad, on="code", how="left")
        add(pl.col("_fc_red").fill_null(False), "业绩预告亏损", "red")
        add(pl.col("_bad_fc").fill_null(False) & ~pl.col("_fc_red").fill_null(False), "业绩预告下降", "yellow")
    un = _ext("unlock")
    if un.height:
        u = (un.filter((pl.col("date") >= today) & (pl.col("date") <= today + timedelta(days=30)))
             .group_by("code").agg(pl.col("float_ratio").fill_null(0).sum().alias("_unlock")))
        df = df.join(u, on="code", how="left")
        add(pl.col("_unlock") >= 20, "近期大额解禁", "red")
        add((pl.col("_unlock") > 0) & (pl.col("_unlock") < 20), "近期解禁", "yellow")
    pg = _ext("pledge")
    if pg.height:
        p = pg.sort("date").group_by("code").last().select("code", pl.col("pledge_ratio").alias("_pledge"))
        df = df.join(p, on="code", how="left")
        add(pl.col("_pledge") >= 50, "高质押", "red")
        add((pl.col("_pledge") >= 30) & (pl.col("_pledge") < 50), "质押偏高", "yellow")
    risk = (pl.when(pl.any_horizontal(red) if red else pl.lit(False)).then(pl.lit("red"))
            .when(pl.any_horizontal(yellow) if yellow else pl.lit(False)).then(pl.lit("yellow")).otherwise(pl.lit("green")))
    return df.with_columns(risk.alias("risk"), pl.concat_str(reasons, separator="；", ignore_nulls=True).alias("risk_reasons")) \
        .select("code", "risk", "risk_reasons")
