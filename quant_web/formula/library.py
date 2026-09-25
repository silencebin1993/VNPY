"""
内置公式库：常用的选股/预警公式，每个都有白话说明、用法和提醒。

kind：select = 找买入候选；warn = 风险预警（持有的股票出现时要警惕）。
所有公式都只用当天及以前的数据（没有未来函数）。成绩好不好不在这里下结论：
页面上一律用"验证"（formula.validate，样本外 + 扣成本 + 和同日随机比）的结果说话。
"""
from __future__ import annotations


GROUPS: list[str] = ["趋势", "突破", "量价主力", "低吸反转", "风险预警", "筹码"]

LIBRARY: list[dict] = [
    # ------------------------------------------------ 趋势
    {
        "id": "ma_bull", "name": "均线多头排列", "group": "趋势", "kind": "select",
        "text": "MA5:=MA(C,5);MA10:=MA(C,10);MA20:=MA(C,20);MA60:=MA(C,60);\n"
                "XG:MA5>MA10 AND MA10>MA20 AND MA20>MA60 AND MA20>REF(MA20,1);",
        "explain": "5、10、20、60 日均线从上到下依次排列，而且 20 日线还在往上走——典型的上升趋势。",
        "usage": "适合做波段的“候选池”，再从里面挑回调到 10/20 日线附近、成交量缩小的股票。",
        "trap": "多头排列出现时往往已经涨了一段，追在高位容易买在回调前；要配合止损。",
    },
    {
        "id": "above_year", "name": "年线上方强势股", "group": "趋势", "kind": "select",
        "text": "MA250:=MA(C,250);\nXG:C>MA250 AND MA250>REF(MA250,5) AND C>HHV(C,120)*0.85;",
        "explain": "股价在向上的年线（250 日均线）之上，并且离半年内最高价不到 15%——长期趋势向上、仍然强势。",
        "usage": "作为中线选股的第一道筛子，排除长期下跌的股票。",
        "trap": "条件很宽，选出来的股票很多，需要再结合量价和位置挑选。",
    },
    {
        "id": "ma_converge", "name": "均线粘合后放量发散", "group": "趋势", "kind": "select",
        "text": "M5:=MA(C,5);M10:=MA(C,10);M20:=MA(C,20);\nHI:=MAX(MAX(M5,M10),M20);LO:=MIN(MIN(M5,M10),M20);\n"
                "XG:REF((HI-LO)/C,1)<0.02 AND C>HI AND V>MA(V,5)*1.5 AND C>O;",
        "explain": "昨天 5、10、20 日均线几乎粘在一起（相差不到 2%），今天放量阳线站上所有均线——横盘整理后可能选择向上。",
        "usage": "粘合越久、放量越明显越好；突破后回踩不破均线是更稳的买点。",
        "trap": "粘合后也可能向下发散，第二天跌回均线下方要果断止损。",
    },
    {
        "id": "cross_year", "name": "放量站上年线", "group": "趋势", "kind": "select",
        "text": "XG:CROSS(C,MA(C,250)) AND V>MA(V,20)*1.5;",
        "explain": "收盘价从下向上穿过年线，同时成交量明显放大——长期趋势可能由弱转强。",
        "usage": "适合找刚走出长期下跌的股票；站上后几天不跌回年线更可靠。",
        "trap": "第一次突破年线经常失败（假突破），要等确认或轻仓试。",
    },
    # ------------------------------------------------ 突破
    {
        "id": "breakout_volume", "name": "放量突破平台", "group": "突破", "kind": "select",
        "text": "H60:=REF(HHV(H,60),1);L60:=REF(LLV(L,60),1);\n"
                "XG:C>H60 AND V>MA(V,20)*1.8 AND C>O AND (H60-L60)/L60<0.35;",
        "explain": "过去 60 天在一个不太宽的平台里震荡（高低相差不到 35%），今天放量阳线收在平台最高点之上。",
        "usage": "经典的突破买点；止损可以放在平台上沿下方。",
        "trap": "尾盘偷袭拉高的“假突破”很多，第二天跌回平台内要止损。",
    },
    {
        "id": "boll_squeeze", "name": "布林收口后突破上轨", "group": "突破", "kind": "select",
        "text": "MID:=MA(C,20);UB:=MID+2*STD(C,20);LB:=MID-2*STD(C,20);WIDTH:=(UB-LB)/MID;\n"
                "XG:REF(WIDTH,1)<LLV(WIDTH,60)*1.1 AND C>UB AND V>MA(V,10)*1.5;",
        "explain": "布林带宽度接近 60 天里最窄（波动很小、蓄势），今天放量突破上轨。",
        "usage": "波动从小变大时往往有一段趋势；配合大盘环境使用。",
        "trap": "也可能突破后马上回落，收回中轨以下就要离场。",
    },
    {
        "id": "new_high", "name": "放量创一年新高", "group": "突破", "kind": "select",
        "text": "XG:C>=HHV(C,250) AND V>MA(V,20)*1.5 AND C/REF(C,20)<1.4;",
        "explain": "收盘价创一年新高并且放量，但最近 20 天涨幅还没超过 40%（没有涨得太疯）。",
        "usage": "新高意味着上方没有套牢盘，强者恒强；适合趋势跟随。",
        "trap": "新高后常有回踩，追在当天最高处要做好回调准备。",
    },
    # ------------------------------------------------ 量价主力
    {
        "id": "accumulation", "name": "底部吸筹迹象", "group": "量价主力", "kind": "select",
        "text": "POS:=(C-LLV(L,250))/(HHV(H,250)-LLV(L,250));RNG:=(HHV(H,30)-LLV(L,30))/LLV(L,30);\n"
                "UPV:=SUM(IF(C>REF(C,1),V,0),20);DNV:=SUM(IF(C<REF(C,1),V,0),20);\n"
                "XG:POS<0.35 AND RNG<0.2 AND UPV>DNV*1.3 AND SLOPE(OBV.OBV,20)>0;",
        "explain": "股价在一年区间的低位（下 35%），最近一个月横着走（振幅不到 20%），上涨日的成交量明显多于下跌日，OBV 在往上走——有资金在低位悄悄买的迹象。",
        "usage": "吸筹阶段可能很长，适合放进观察名单，等放量突破时再买。",
        "trap": "低位横盘也可能是没人要，继续阴跌；“吸筹”只是推测，不是确定的事。",
    },
    {
        "id": "washout_pullback", "name": "缩量回踩（洗盘候选）", "group": "量价主力", "kind": "select",
        "text": "M20:=MA(C,20);UP:=HHV(C,20)/LLV(C,40)-1;\n"
                "XG:UP>0.2 AND C>M20 AND L<=M20*1.02 AND V<MA(V,20)*0.7 AND M20>REF(M20,3) AND C<HHV(C,20)*0.95;",
        "explain": "前面涨过一波（40 天内涨了 20% 以上），现在缩量回调到仍在向上的 20 日均线附近，没有跌破——像是洗盘。",
        "usage": "洗盘后放量重新上涨是买点；跌破 20 日线且放量就要放弃（可能是出货）。",
        "trap": "洗盘和出货当下很难分清，关键看成交量：缩量跌像洗盘，放量跌像出货。",
    },
    {
        "id": "vol_price_up", "name": "量价齐升", "group": "量价主力", "kind": "select",
        "text": "XG:COUNT(C>REF(C,1) AND V>REF(V,1),5)>=3 AND C>MA(C,20) AND C/REF(C,5)<1.25;",
        "explain": "最近 5 天里至少 3 天价格和成交量同时上升，股价在 20 日线之上，5 天涨幅没超过 25%——上涨有资金支持。",
        "usage": "健康的上涨形态；回调缩量时是加仓/买入的机会。",
        "trap": "连续放量上涨后如果出现放量滞涨，要警惕见顶。",
    },
    {
        "id": "updown_ratio", "name": "阳量主导", "group": "量价主力", "kind": "select",
        "text": "UPV:=SUM(IF(C>REF(C,1),V,0),20);DNV:=SUM(IF(C<REF(C,1),V,0),20);\n"
                "XG:UPV>DNV*1.5 AND C>MA(C,60) AND C/REF(C,20)<1.3;",
        "explain": "最近 20 天，上涨日的成交量是下跌日的 1.5 倍以上，股价在 60 日线之上——买的力量明显更强。",
        "usage": "和均线、突破类公式一起用，作为“量能确认”。",
        "trap": "一两天的巨量就能拉高这个比值，要看是不是持续的。",
    },
    {
        "id": "dry_volume_rebound", "name": "地量后放量回升", "group": "量价主力", "kind": "select",
        "text": "DRY:=V<=LLV(V,60)*1.1;\nXG:EXIST(DRY,5) AND V>MA(V,5)*1.5 AND C>REF(C,1)*1.02 AND C>MA(C,120);",
        "explain": "最近 5 天内出现过 60 天来的地量（几乎没人卖了），今天放量上涨 2% 以上，而且在半年线之上。",
        "usage": "卖盘枯竭后资金进场的信号，适合在上升趋势中的回调末端使用。",
        "trap": "下跌趋势里的地量不代表见底，所以加了半年线条件。",
    },
    # ------------------------------------------------ 低吸反转
    {
        "id": "kdj_low_cross", "name": "KDJ 低位金叉（趋势向上）", "group": "低吸反转", "kind": "select",
        "text": "XG:CROSS(KDJ.K,KDJ.D) AND KDJ.D<25 AND C>MA(C,60);",
        "explain": "KDJ 在 25 以下的低位金叉，并且股价仍在 60 日线之上——上升趋势中的短期超跌反弹。",
        "usage": "只在上升趋势里用，避免在下跌趋势里“抄底抄在半山腰”。",
        "trap": "KDJ 很灵敏，金叉后可能很快又死叉；要设止损。",
    },
    {
        "id": "macd_zero_cross", "name": "MACD 零轴上金叉", "group": "低吸反转", "kind": "select",
        "text": "XG:CROSS(MACD.DIF,MACD.DEA) AND MACD.DEA>0 AND C>MA(C,60);",
        "explain": "MACD 在零轴上方金叉（上升趋势中的回调结束信号），股价在 60 日线之上。",
        "usage": "比零轴下方的金叉更可靠，适合波段。",
        "trap": "震荡市里金叉死叉频繁，单独使用胜率一般。",
    },
    {
        "id": "rsi_rebound", "name": "RSI 超卖回升（长期向上）", "group": "低吸反转", "kind": "select",
        "text": "XG:CROSS(RSI.RSI1,30) AND C>MA(C,120);",
        "explain": "6 日 RSI 从 30 以下回升穿过 30，而且股价在半年线之上——长期向上中的短期超跌。",
        "usage": "适合长期趋势好的股票逢低买入。",
        "trap": "超卖可以更超卖，一定要有止损。",
    },
    {
        "id": "pullback_ma10", "name": "强势股回踩 10 日线", "group": "低吸反转", "kind": "select",
        "text": "M10:=MA(C,10);\nXG:C/REF(C,20)>1.15 AND L<=M10*1.01 AND C>=M10 AND V<MA(V,5) AND M10>REF(M10,1);",
        "explain": "20 天涨幅超过 15% 的强势股，今天缩量回踩到仍在上升的 10 日均线附近，收盘没跌破。",
        "usage": "强势股的第一次回踩往往是较好的低吸点。",
        "trap": "如果回踩时放量或者跌破 10 日线，说明强势可能结束。",
    },
    {
        "id": "limit_pullback", "name": "涨停后缩量回调（龙回头）", "group": "低吸反转", "kind": "select",
        "text": "ZT:=C>=REF(C,1)*1.095;N:=BARSLAST(ZT);\n"
                "XG:N>=3 AND N<=10 AND V<MA(V,10)*0.6 AND C>REF(C,N+1);",
        "explain": "3~10 天前出现过涨停（按主板 10% 近似），之后缩量回调，但股价还在涨停前的位置之上。",
        "usage": "短线常见的“龙回头”低吸思路。",
        "trap": "我们之前的研究发现：和涨停相关的短线策略按真实规则交易整体是亏钱的。一定要先看验证结果，不要凭感觉用。",
    },
    # ------------------------------------------------ 风险预警
    {
        "id": "high_volume_stall", "name": "高位放量滞涨", "group": "风险预警", "kind": "warn",
        "text": "POS:=(C-LLV(L,120))/(HHV(H,120)-LLV(L,120));\n"
                "XG:POS>0.8 AND V>MA(V,20)*2 AND ABS(C/REF(C,1)-1)<0.02 AND (H-MAX(O,C))/(H-L+0.001)>0.4;",
        "explain": "股价在半年区间的高位（上 20%），成交量是平时的 2 倍以上，但价格几乎没涨，还留下长上影线——大量卖盘在高位出货的典型迹象。",
        "usage": "持有的股票出现时，建议减仓或提高止损价。",
        "trap": "单独一天不一定是顶，如果随后几天放量下跌就基本确认。",
    },
    {
        "id": "top_divergence", "name": "量价顶背离", "group": "风险预警", "kind": "warn",
        "text": "PH:=REF(HHV(H,60),10);\n"
                "XG:H>PH AND MA(V,5)<REF(HHV(MA(V,5),60),10)*0.8 AND MACD.DIF<REF(HHV(MACD.DIF,60),10);",
        "explain": "价格创出新高，但成交量没有跟上（比前高时小 20% 以上），MACD 的 DIF 也没有创新高——上涨的力量在减弱。",
        "usage": "持股出现时提高警惕，跌破 10 日线可以先走一部分。",
        "trap": "背离可以持续很久，不是马上就跌；要结合破位信号。",
    },
    {
        "id": "breakdown", "name": "放量跌破 20 日线", "group": "风险预警", "kind": "warn",
        "text": "M20:=MA(C,20);\nXG:C<M20 AND REF(C,1)>=REF(M20,1) AND V>MA(V,20)*1.5 AND C<REF(C,1)*0.97;",
        "explain": "放量下跌 3% 以上，收盘跌破 20 日均线——短期趋势被破坏，可能是出货而不是洗盘。",
        "usage": "持股出现时，按计划减仓或止损；不要幻想“主力在洗盘”。",
        "trap": "偶尔会是急跌洗盘，但放量破位时先走、确认后再买回更安全。",
    },
    {
        "id": "slow_bleed", "name": "阴跌不止", "group": "风险预警", "kind": "warn",
        "text": "XG:COUNT(C<REF(C,1),10)>=7 AND C<MA(C,60) AND MA(C,20)<REF(MA(C,20),5);",
        "explain": "最近 10 天里有 7 天以上在跌，股价在 60 日线下方，20 日线也在往下——典型的弱势。",
        "usage": "不要在这种走势里抄底；持有的要按止损执行。",
        "trap": "越跌越买（补仓摊平）是新手最常见的亏大钱方式。",
    },
    {
        "id": "big_black", "name": "放巨量长阴", "group": "风险预警", "kind": "warn",
        "text": "XG:C<O*0.95 AND V>MA(V,20)*2.5 AND C<REF(C,1)*0.95;",
        "explain": "一根大阴线（从开盘跌了 5% 以上，比昨天跌 5% 以上），成交量是平时的 2.5 倍以上——大资金在集中卖出。",
        "usage": "持股出现时应认真考虑离场。",
        "trap": "在长期下跌末端也可能是恐慌盘出尽，但新手很难判断，先走为上。",
    },
    {
        "id": "gap_up_fade", "name": "高开低走放量", "group": "风险预警", "kind": "warn",
        "text": "XG:O>REF(C,1)*1.03 AND C<O*0.97 AND V>MA(V,10)*1.5;",
        "explain": "开盘高开 3% 以上，收盘却比开盘跌了 3% 以上，而且放量——常见于借利好高开出货。",
        "usage": "追高开的人当天就被套；持股出现时要警惕。",
        "trap": "有时只是情绪回落，但放量时要格外小心。",
    },
    # ------------------------------------------------ 筹码（需要筹码数据，计算较慢）
    {
        "id": "chips_concentrated", "name": "筹码集中 + 大部分获利", "group": "筹码", "kind": "select",
        "text": "CON:=(COST(95)-COST(5))/(COST(95)+COST(5));\nXG:CON<0.12 AND WINNER(C)>0.6 AND C<COST(50)*1.15 AND C>MA(C,60);",
        "explain": "90% 的筹码集中在很窄的价格范围里（成本接近），大部分持股人赚钱，股价离中位成本不远，且在 60 日线上方——筹码稳定、上方抛压小。",
        "usage": "适合配合突破类公式使用。",
        "trap": "筹码分布是估算值，和其他软件会有差异；这个公式计算较慢。",
    },
    {
        "id": "chips_trapped", "name": "上方套牢盘沉重", "group": "筹码", "kind": "warn",
        "text": "XG:WINNER(C)<0.1 AND C<COST(50)*0.8;",
        "explain": "只有不到 10% 的持股人赚钱，股价比中位成本低 20% 以上——上方全是想“解套就卖”的人，反弹很难走远。",
        "usage": "这类股票反弹时容易遇到抛压，不适合追。",
        "trap": "长期看可能是底部，但需要很长时间消化。",
    },
]

BY_ID: dict[str, dict] = {f["id"]: f for f in LIBRARY}


def get(fid: str) -> dict:
    if fid not in BY_ID:
        raise ValueError(f"没有「{fid}」这个内置公式")
    return BY_ID[fid]
