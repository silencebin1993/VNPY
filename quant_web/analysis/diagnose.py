"""
个股诊断：主力阶段（逐条证据）+ 阶段时间线 + 量价关系摘要 + 参考信息（资金流、股东户数，不参与打分）+ 历史验证结论。
单只股票用它自己的全部历史计算；RPS（相对强度）用全市场最近一年的截面排名。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from ..formula import engine
from . import features, stage, stage_stats

TIMELINE_DAYS: int = 120


def rps_recent(panel_last: date) -> pl.DataFrame:
    """全市场最近约一年的 RPS120（供单只股票诊断使用）"""
    from ..market import history

    raw: pl.DataFrame = history.load_panel(start=panel_last - timedelta(days=420), columns=["close", "preclose"])
    if raw.is_empty():
        return pl.DataFrame(schema={"code": pl.Utf8, "date": pl.Date, "rps120": pl.Float64})
    from ..indicators.adjust import add_qfq
    q: pl.DataFrame = add_qfq(raw.sort(["code", "date"])).select("code", "date", pl.col("qclose").alias("close"))
    return stage_stats.rps_table(q)


def stock_frame(code: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    from ..market import history

    raw: pl.DataFrame = history.load_panel(codes=[code], columns=engine.FRAME_COLS)
    if raw.is_empty():
        return raw, raw
    return raw, engine.prepare_frame(raw)


def volume_price_summary(row: dict) -> list[dict]:
    """最近的量价关系，逐条白话（tone: good/bad/neutral）"""
    out: list[dict] = []
    vr, udr = row.get("vr20"), row.get("udr20")
    if vr is not None:
        if vr >= 2:
            out.append({"tone": "neutral", "text": f"今天成交量是 20 日均量的 {vr:.1f} 倍，明显放量"})
        elif vr <= 0.6:
            out.append({"tone": "neutral", "text": f"今天成交量只有 20 日均量的 {vr:.1f} 倍，明显缩量"})
    if udr is not None:
        if udr >= 1.3:
            out.append({"tone": "good", "text": f"最近 20 天上涨日的成交量是下跌日的 {udr:.1f} 倍，买的力量更强"})
        elif udr <= 0.77:
            out.append({"tone": "bad", "text": f"最近 20 天下跌日的成交量是上涨日的 {1 / max(udr, 1e-9):.1f} 倍，卖的力量更强"})
    if row.get("stall10"):
        out.append({"tone": "bad", "text": "10 天内出现过高位放量滞涨（量很大但价格涨不动）"})
    if row.get("div10"):
        out.append({"tone": "bad", "text": "10 天内出现过量价顶背离（价格新高、量和 MACD 没跟上）"})
    if row.get("brk10"):
        out.append({"tone": "bad", "text": "10 天内出现过放量跌破 20 日线"})
    if row.get("breakout5"):
        out.append({"tone": "good", "text": "5 天内出现过放量突破 60 日高点"})
    if row.get("black_vol5"):
        out.append({"tone": "bad", "text": "5 天内出现过放量长阴"})
    if row.get("obv_slope") is not None and row.get("price_slope") is not None:
        if row["obv_slope"] > 0 and row["price_slope"] < 0:
            out.append({"tone": "good", "text": "股价在跌，但 OBV（资金累计）在往上走：有资金逢低买入的迹象"})
        elif row["obv_slope"] < 0 and row["price_slope"] > 0:
            out.append({"tone": "bad", "text": "股价在涨，但 OBV 在往下走：上涨缺少资金支持"})
    if not out:
        out.append({"tone": "neutral", "text": "最近量价关系没有明显异常"})
    return out


def _segments(dates: list, stages: list[str]) -> list[dict]:
    segs: list[dict] = []
    for d, s in zip(dates, stages):
        if segs and segs[-1]["stage"] == s:
            segs[-1]["end"] = str(d)
            segs[-1]["days"] += 1
        else:
            segs.append({"stage": s, "label": stage.STAGES[s]["label"], "start": str(d), "end": str(d), "days": 1})
    return segs


def diagnose(code: str, rps: pl.DataFrame | None = None, flow: list[dict] | None = None,
             holders: pl.DataFrame | None = None) -> dict:
    raw, frame = stock_frame(code)
    if frame.is_empty():
        raise FileNotFoundError(f"本地没有 {code} 的日线数据")
    chips: pl.DataFrame | None = engine.chips_for_frame(frame, raw) if frame.height >= 30 else None
    feat: pl.DataFrame = features.compute(frame, chips, rps=False).drop("rps120")
    if rps is not None and rps.height:
        feat = feat.join(rps.filter(pl.col("code") == code), on=["code", "date"], how="left")
    else:
        feat = feat.with_columns(pl.lit(None, dtype=pl.Float64).alias("rps120"))
    cls: pl.DataFrame = stage.classify(feat, keep_evidence=True)
    last: dict = cls.row(cls.height - 1, named=True)
    st: dict = stage.explain_row(last)
    res = stage_stats.load()
    st["history"] = {s: stage_stats.verdict(s, res) for s in stage.STAGES}
    st["validated_at"] = res.get("generated_at") if res else None
    tail: pl.DataFrame = cls.tail(TIMELINE_DAYS)
    timeline: list[dict] = _segments(tail["date"].to_list(), tail["stage"].to_list())
    reference: list[dict] = []
    if flow:
        recent: list[dict] = sorted(flow, key=lambda x: str(x.get("date")))[-10:]
        tot: float = sum(float(x.get("main_net") or 0) for x in recent)
        pos: int = sum(1 for x in recent if (x.get("main_net") or 0) > 0)
        reference.append({"key": "fund_flow", "label": "主力资金（仅实时参考，未经历史验证）",
                          "tone": "good" if tot > 0 else "bad",
                          "text": f"最近 {len(recent)} 天大单合计净{'流入' if tot > 0 else '流出'} {abs(tot) / 1e8:.2f} 亿元，其中 {pos} 天净流入"})
    if holders is not None and holders.height:
        h: dict = holders.sort("end_date").row(holders.height - 1, named=True)
        chg = h.get("change_pct")
        if chg is not None:
            reference.append({"key": "holders", "label": "股东户数（季度数据，仅参考）",
                              "tone": "good" if chg < 0 else "bad",
                              "text": f"截至 {h.get('end_date')} 股东户数比上一期{'减少' if chg < 0 else '增加'} {abs(chg):.1f}%"
                                      f"（{'筹码在集中' if chg < 0 else '筹码在分散'}）"})
    amt20 = frame.tail(20)["amount"].mean() if frame.height else None
    last_bar: dict = {"raw_close": last.get("raw_close"), "close": last.get("close"), "amt20": amt20,
                      "date": str(last["date"])}
    return {
        "code": code, "date": str(last["date"]), "stage": st, "timeline": timeline, "last_bar": last_bar,
        "volume_price": volume_price_summary(last), "reference": reference,
        "chips_used": chips is not None, "rps120": last.get("rps120"),
        "note": "阶段判断是事先定好的经验规则（没有用历史数据调参），只能说明“像不像”，不能确定主力真实意图；"
                "每个阶段之后的真实表现以历史验证为准。",
    }
