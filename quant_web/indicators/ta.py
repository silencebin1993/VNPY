"""
技术指标（通达信口径，公式写在各函数的文档里）。

输入 d：dict 或 DataFrame，含 open/high/low/close/volume（成交量，股）列，可选 amount/turnover（换手率 %）。
价格应是前复权价（单只股票用行情接口的前复权 K 线；全市场用 adjust.add_qfq 的 q* 列）。
ctx：funcs.Ctx，多只股票时按 (code, date) 排序。
返回 [(key, label, Series, style)]，style ∈ line / bar（柱）/ macd（红绿柱）/ vol（成交量柱）/ dot（点）/ dash（虚线）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import polars as pl

from . import funcs as F
from .funcs import Ctx

Line = tuple[str, str, pl.Series, str]


def _c(d: Any, name: str) -> pl.Series:
    s = d[name]
    return s.cast(pl.Float64, strict=False) if isinstance(s, pl.Series) else pl.Series(name, s, dtype=pl.Float64)


def _cols(d: Any) -> tuple[pl.Series, pl.Series, pl.Series, pl.Series, pl.Series]:
    return _c(d, "open"), _c(d, "high"), _c(d, "low"), _c(d, "close"), _c(d, "volume")


def _pct(a: pl.Series, b: pl.Series) -> pl.Series:
    return F.safe_div(a, b) * 100


# ---------------------------------------------------------------- 主图

def ma(d, ctx: Ctx, n1: int = 5, n2: int = 10, n3: int = 20, n4: int = 60) -> list[Line]:
    """MA：MA1:MA(C,N1); MA2:MA(C,N2); MA3:MA(C,N3); MA4:MA(C,N4)"""
    c = _c(d, "close")
    return [(f"ma{i}", f"MA{n}", F.ma(c, n, ctx), "line") for i, n in enumerate((n1, n2, n3, n4), start=1) if n > 0]


def expma(d, ctx: Ctx, n1: int = 12, n2: int = 50) -> list[Line]:
    """EXPMA：EXP1:EMA(C,N1); EXP2:EMA(C,N2)"""
    c = _c(d, "close")
    return [("exp1", f"EMA{n1}", F.ema(c, n1, ctx), "line"), ("exp2", f"EMA{n2}", F.ema(c, n2, ctx), "line")]


def boll(d, ctx: Ctx, n: int = 20, k: float = 2.0) -> list[Line]:
    """BOLL：MID:MA(C,N); UPPER:MID+K*STD(C,N); LOWER:MID-K*STD(C,N)（STD 为样本标准差）"""
    c = _c(d, "close")
    mid = F.ma(c, n, ctx)
    sd = F.std(c, n, ctx)
    return [("mid", "中轨", mid, "line"), ("upper", "上轨", mid + k * sd, "line"), ("lower", "下轨", mid - k * sd, "line")]


def ene(d, ctx: Ctx, n: int = 25, m1: float = 6, m2: float = 6) -> list[Line]:
    """ENE 轨道：UPPER:(1+M1/100)*MA(C,N); LOWER:(1-M2/100)*MA(C,N); ENE:(UPPER+LOWER)/2"""
    m = F.ma(_c(d, "close"), n, ctx)
    up, lo = (1 + m1 / 100) * m, (1 - m2 / 100) * m
    return [("upper", "上轨", up, "line"), ("ene", "中线", (up + lo) / 2, "dash"), ("lower", "下轨", lo, "line")]


def sar(d, ctx: Ctx) -> list[Line]:
    """SAR 抛物线转向（加速因子 0.02 起、每次 +0.02、最大 0.2）"""
    return [("sar", "SAR", F.sar(_c(d, "high"), _c(d, "low"), ctx), "dot")]


# ---------------------------------------------------------------- 副图

def vol(d, ctx: Ctx, m1: int = 5, m2: int = 10) -> list[Line]:
    """VOL：成交量柱；MAVOL1:MA(V,M1); MAVOL2:MA(V,M2)"""
    v = _c(d, "volume")
    return [("vol", "成交量", v, "vol"), ("mav1", f"MA{m1}", F.ma(v, m1, ctx), "line"), ("mav2", f"MA{m2}", F.ma(v, m2, ctx), "line")]


def macd(d, ctx: Ctx, short: int = 12, long: int = 26, mid: int = 9) -> list[Line]:
    """MACD：DIF:EMA(C,SHORT)-EMA(C,LONG); DEA:EMA(DIF,MID); MACD:(DIF-DEA)*2"""
    c = _c(d, "close")
    dif = F.ema(c, short, ctx) - F.ema(c, long, ctx)
    dea = F.ema(dif, mid, ctx)
    return [("dif", "DIF", dif, "line"), ("dea", "DEA", dea, "line"), ("macd", "MACD", (dif - dea) * 2, "macd")]


def kdj(d, ctx: Ctx, n: int = 9, m1: int = 3, m2: int = 3) -> list[Line]:
    """KDJ：RSV:(C-LLV(L,N))/(HHV(H,N)-LLV(L,N))*100; K:SMA(RSV,M1,1); D:SMA(K,M2,1); J:3*K-2*D
    （最高价等于最低价、区间为 0 时 RSV 取 50）"""
    _, h, lo, c, _ = _cols(d)
    llv, hhv = F.llv(lo, n, ctx), F.hhv(h, n, ctx)
    rsv = F.safe_div(c - llv, hhv - llv).fill_null(0.5) * 100
    k = F.sma(rsv, m1, 1, ctx)
    dd = F.sma(k, m2, 1, ctx)
    return [("k", "K", k, "line"), ("d", "D", dd, "line"), ("j", "J", 3 * k - 2 * dd, "line")]


def rsi(d, ctx: Ctx, n1: int = 6, n2: int = 12, n3: int = 24) -> list[Line]:
    """RSI：LC:=REF(C,1); RSI:SMA(MAX(C-LC,0),N,1)/SMA(ABS(C-LC),N,1)*100（第一天涨跌按 0）"""
    c = _c(d, "close")
    diff = (c - F.ref(c, 1, ctx)).fill_null(0.0)
    up = F.max_(diff, 0.0, ctx)
    ab = diff.abs()
    out: list[Line] = []
    for i, n in enumerate((n1, n2, n3), start=1):
        out.append((f"rsi{i}", f"RSI{n}", F.safe_div(F.sma(up, n, 1, ctx), F.sma(ab, n, 1, ctx)) * 100, "line"))
    return out


def wr(d, ctx: Ctx, n1: int = 10, n2: int = 6) -> list[Line]:
    """WR（通达信口径，数值越大越接近区间低点）：WR:100*(HHV(H,N)-C)/(HHV(H,N)-LLV(L,N))"""
    _, h, lo, c, _ = _cols(d)
    out: list[Line] = []
    for i, n in enumerate((n1, n2), start=1):
        hh, ll = F.hhv(h, n, ctx), F.llv(lo, n, ctx)
        out.append((f"wr{i}", f"WR{n}", _pct(hh - c, hh - ll), "line"))
    return out


def cci(d, ctx: Ctx, n: int = 14) -> list[Line]:
    """CCI：TYP:=(H+L+C)/3; CCI:(TYP-MA(TYP,N))/(0.015*AVEDEV(TYP,N))"""
    _, h, lo, c, _ = _cols(d)
    typ = (h + lo + c) / 3
    return [("cci", "CCI", F.safe_div(typ - F.ma(typ, n, ctx), 0.015 * F.avedev(typ, n, ctx)), "line")]


def dmi(d, ctx: Ctx, n: int = 14, m: int = 6) -> list[Line]:
    """DMI：MTR:=SUM(MAX(MAX(H-L,ABS(H-REF(C,1))),ABS(REF(C,1)-L)),N); HD:=H-REF(H,1); LD:=REF(L,1)-L;
    DMP:=SUM(IF(HD>0&&HD>LD,HD,0),N); DMM:=SUM(IF(LD>0&&LD>HD,LD,0),N);
    PDI:DMP*100/MTR; MDI:DMM*100/MTR; ADX:MA(ABS(MDI-PDI)/(MDI+PDI)*100,M); ADXR:(ADX+REF(ADX,M))/2"""
    _, h, lo, c, _ = _cols(d)
    pc = F.ref(c, 1, ctx)
    tr = F.max_(F.max_(h - lo, (h - pc).abs(), ctx), (pc - lo).abs(), ctx)
    mtr = F.sum_(tr, n, ctx)
    hd = h - F.ref(h, 1, ctx)
    ld = F.ref(lo, 1, ctx) - lo
    dmp = F.sum_(F.if_((hd > 0) & (hd > ld), hd, 0.0, ctx), n, ctx)
    dmm = F.sum_(F.if_((ld > 0) & (ld > hd), ld, 0.0, ctx), n, ctx)
    pdi = _pct(dmp, mtr)
    mdi = _pct(dmm, mtr)
    adx = F.ma(_pct((mdi - pdi).abs(), mdi + pdi), m, ctx)
    adxr = (adx + F.ref(adx, m, ctx)) / 2
    return [("pdi", "PDI", pdi, "line"), ("mdi", "MDI", mdi, "line"), ("adx", "ADX", adx, "line"), ("adxr", "ADXR", adxr, "line")]


def bias(d, ctx: Ctx, n1: int = 6, n2: int = 12, n3: int = 24) -> list[Line]:
    """BIAS：(C-MA(C,N))/MA(C,N)*100"""
    c = _c(d, "close")
    out: list[Line] = []
    for i, n in enumerate((n1, n2, n3), start=1):
        m = F.ma(c, n, ctx)
        out.append((f"bias{i}", f"BIAS{n}", _pct(c - m, m), "line"))
    return out


def obv(d, ctx: Ctx, m: int = 30) -> list[Line]:
    """OBV：SUM(IF(C>REF(C,1),V,IF(C<REF(C,1),-V,0)),0); MAOBV:MA(OBV,M)（成交量单位为股）"""
    c, v = _c(d, "close"), _c(d, "volume")
    pc = F.ref(c, 1, ctx)
    signed = F.if_(c > pc, v, F.if_(c < pc, -v, 0.0, ctx), ctx)
    o = F.sum_(signed, 0, ctx)
    return [("obv", "OBV", o, "line"), ("maobv", f"MA{m}", F.ma(o, m, ctx), "line")]


def mfi(d, ctx: Ctx, n: int = 14) -> list[Line]:
    """MFI：TYP:=(H+L+C)/3; V1:=SUM(IF(TYP>REF(TYP,1),TYP*V,0),N)/SUM(IF(TYP<REF(TYP,1),TYP*V,0),N);
    MFI:100-(100/(1+V1))"""
    _, h, lo, c, v = _cols(d)
    typ = (h + lo + c) / 3
    pt = F.ref(typ, 1, ctx)
    pos = F.sum_(F.if_(typ > pt, typ * v, 0.0, ctx), n, ctx)
    neg = F.sum_(F.if_(typ < pt, typ * v, 0.0, ctx), n, ctx)
    v1 = F.safe_div(pos, neg)
    return [("mfi", "MFI", 100 - 100 / (1 + v1), "line")]


def vr(d, ctx: Ctx, n: int = 26, m: int = 6) -> list[Line]:
    """VR：TH:=SUM(IF(C>REF(C,1),V,0),N); TL:=SUM(IF(C<REF(C,1),V,0),N); TQ:=SUM(IF(C=REF(C,1),V,0),N);
    VR:100*(TH*2+TQ)/(TL*2+TQ); MAVR:MA(VR,M)"""
    c, v = _c(d, "close"), _c(d, "volume")
    pc = F.ref(c, 1, ctx)
    th = F.sum_(F.if_(c > pc, v, 0.0, ctx), n, ctx)
    tl = F.sum_(F.if_(c < pc, v, 0.0, ctx), n, ctx)
    tq = F.sum_(F.if_(c == pc, v, 0.0, ctx), n, ctx)
    val = F.safe_div(100 * (th * 2 + tq), tl * 2 + tq)
    return [("vr", "VR", val, "line"), ("mavr", f"MA{m}", F.ma(val, m, ctx), "line")]


def atr(d, ctx: Ctx, n: int = 14) -> list[Line]:
    """ATR：TR:=MAX(MAX(H-L,ABS(REF(C,1)-H)),ABS(REF(C,1)-L)); ATR:MA(TR,N)"""
    _, h, lo, c, _ = _cols(d)
    pc = F.ref(c, 1, ctx)
    tr = F.max_(F.max_(h - lo, (pc - h).abs(), ctx), (pc - lo).abs(), ctx)
    a = F.ma(tr, n, ctx)
    return [("atr", "ATR", a, "line"), ("atrp", "ATR占股价%", _pct(a, c), "dash")]


def trix(d, ctx: Ctx, n: int = 12, m: int = 9) -> list[Line]:
    """TRIX：MTR:=EMA(EMA(EMA(C,N),N),N); TRIX:(MTR-REF(MTR,1))/REF(MTR,1)*100; MATRIX:MA(TRIX,M)"""
    c = _c(d, "close")
    mtr = F.ema(F.ema(F.ema(c, n, ctx), n, ctx), n, ctx)
    pm = F.ref(mtr, 1, ctx)
    t = _pct(mtr - pm, pm)
    return [("trix", "TRIX", t, "line"), ("matrix", f"MA{m}", F.ma(t, m, ctx), "line")]


def dma(d, ctx: Ctx, n1: int = 10, n2: int = 50, m: int = 10) -> list[Line]:
    """DMA（平行线差）：DIF:MA(C,N1)-MA(C,N2); DIFMA:MA(DIF,M)"""
    c = _c(d, "close")
    dif = F.ma(c, n1, ctx) - F.ma(c, n2, ctx)
    return [("dif", "DIF", dif, "line"), ("difma", "DIFMA", F.ma(dif, m, ctx), "line")]


def psy(d, ctx: Ctx, n: int = 12, m: int = 6) -> list[Line]:
    """PSY（心理线）：PSY:COUNT(C>REF(C,1),N)/N*100; PSYMA:MA(PSY,M)"""
    c = _c(d, "close")
    p = F.count(c > F.ref(c, 1, ctx), n, ctx) / n * 100
    return [("psy", "PSY", p, "line"), ("psyma", f"MA{m}", F.ma(p, m, ctx), "line")]


def brar(d, ctx: Ctx, n: int = 26) -> list[Line]:
    """BRAR：BR:SUM(MAX(0,H-REF(C,1)),N)/SUM(MAX(0,REF(C,1)-L),N)*100; AR:SUM(H-O,N)/SUM(O-L,N)*100"""
    o, h, lo, c, _ = _cols(d)
    pc = F.ref(c, 1, ctx)
    br = _pct(F.sum_(F.max_(0.0, h - pc, ctx), n, ctx), F.sum_(F.max_(0.0, pc - lo, ctx), n, ctx))
    ar = _pct(F.sum_(h - o, n, ctx), F.sum_(o - lo, n, ctx))
    return [("br", "BR", br, "line"), ("ar", "AR", ar, "line")]


def cr(d, ctx: Ctx, n: int = 26) -> list[Line]:
    """CR：MID:=REF(H+L,1)/2; CR:SUM(MAX(0,H-MID),N)/SUM(MAX(0,MID-L),N)*100"""
    _, h, lo, _, _ = _cols(d)
    mid = F.ref(h + lo, 1, ctx) / 2
    return [("cr", "CR", _pct(F.sum_(F.max_(0.0, h - mid, ctx), n, ctx), F.sum_(F.max_(0.0, mid - lo, ctx), n, ctx)), "line")]


def turnover(d, ctx: Ctx, m: int = 5) -> list[Line]:
    """换手率（%）及其 M 日均线"""
    if "turnover" not in (d.columns if isinstance(d, pl.DataFrame) else d):
        raise ValueError("这组数据没有换手率")
    t = _c(d, "turnover")
    return [("turn", "换手率%", t, "bar"), ("maturn", f"MA{m}", F.ma(t, m, ctx), "line")]


def volratio(d, ctx: Ctx, n: int = 5) -> list[Line]:
    """量比（日线口径）：V/REF(MA(V,N),1)——今天成交量是前 N 天平均的几倍"""
    v = _c(d, "volume")
    return [("vratio", "量比", F.safe_div(v, F.ref(F.ma(v, n, ctx), 1, ctx)), "line")]


def updown(d, ctx: Ctx, n: int = 20) -> list[Line]:
    """阳量阴量比：SUM(IF(C>REF(C,1),V,0),N)/SUM(IF(C<REF(C,1),V,0),N)；大于 1 说明上涨日成交更多"""
    c, v = _c(d, "close"), _c(d, "volume")
    pc = F.ref(c, 1, ctx)
    up = F.sum_(F.if_(c > pc, v, 0.0, ctx), n, ctx)
    dn = F.sum_(F.if_(c < pc, v, 0.0, ctx), n, ctx)
    return [("udr", "阳量/阴量", F.safe_div(up, dn), "line")]


COMPUTE: dict[str, Callable[..., list[Line]]] = {
    "ma": ma, "expma": expma, "boll": boll, "ene": ene, "sar": sar,
    "vol": vol, "macd": macd, "kdj": kdj, "rsi": rsi, "wr": wr, "cci": cci, "dmi": dmi, "bias": bias, "obv": obv,
    "mfi": mfi, "vr": vr, "atr": atr, "trix": trix, "dma": dma, "psy": psy, "brar": brar, "cr": cr,
    "turnover": turnover, "volratio": volratio, "updown": updown,
}
