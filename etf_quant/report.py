"""
生成可以用浏览器打开的中文报告（回测报告 / 本周操作建议），以及命令行表格输出。
"""
import html
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go       # type: ignore

from .advisor import Advice
from .backtest import BacktestResult
from .config import CLASS_NOTES, REPORT_PATH, StrategyParams


# 参考配色（已通过色盲安全校验），顺序固定
SERIES: list[str] = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
TEXT_PRIMARY: str = "#0b0b0b"
TEXT_SECONDARY: str = "#52514e"
GRID: str = "#e8e7e3"
SURFACE: str = "#fcfcfb"

CSS: str = f"""
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: {SURFACE}; color: {TEXT_PRIMARY};
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", system-ui, sans-serif; line-height: 1.6; }}
main {{ max-width: 1080px; margin: 0 auto; padding: 24px 16px 64px; }}
h1 {{ font-size: 26px; margin: 8px 0 4px; }}
h2 {{ font-size: 19px; margin: 36px 0 12px; padding-top: 12px; border-top: 1px solid {GRID}; }}
.sub {{ color: {TEXT_SECONDARY}; font-size: 14px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 20px 0; }}
.tile {{ background: #fff; border: 1px solid {GRID}; border-radius: 10px; padding: 14px 16px; }}
.tile .label {{ color: {TEXT_SECONDARY}; font-size: 13px; }}
.tile .value {{ font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.tile .hint {{ color: {TEXT_SECONDARY}; font-size: 12px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; background: #fff; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid {GRID}; text-align: left; vertical-align: top; }}
th {{ color: {TEXT_SECONDARY}; font-weight: 600; background: #f6f5f2; }}
td.num, th.num {{ text-align: right; white-space: nowrap; }}
.wrap {{ overflow-x: auto; border: 1px solid {GRID}; border-radius: 10px; }}
.box {{ background: #fff; border: 1px solid {GRID}; border-radius: 10px; padding: 14px 18px; margin: 12px 0; }}
.warn {{ border-left: 4px solid #fab219; }}
.good {{ border-left: 4px solid #0ca30c; }}
.side-buy {{ font-weight: 600; }}
.side-sell {{ font-weight: 600; }}
.bar {{ height: 10px; background: {SERIES[0]}; border-radius: 0 4px 4px 0; display: inline-block; vertical-align: middle; }}
ol li, ul li {{ margin: 4px 0; }}
.chart {{ background: #fff; border: 1px solid {GRID}; border-radius: 10px; padding: 8px; margin: 12px 0; }}
footer {{ margin-top: 40px; color: {TEXT_SECONDARY}; font-size: 12px; }}
"""


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><main>{body}
<footer>本报告由"稳健ETF量化助手"（基于 VeighNa vnpy.alpha）生成。历史表现不代表未来收益，投资有风险。</footer>
</main></body></html>"""


def _pct(v: float, digits: int = 1, sign: bool = False) -> str:
    return f"{v:+.{digits}%}" if sign else f"{v:.{digits}%}"


def _money(v: float) -> str:
    return f"{v:,.0f}"


def _tile(label: str, value: str, hint: str = "") -> str:
    return f'<div class="tile"><div class="label">{label}</div><div class="value">{value}</div><div class="hint">{hint}</div></div>'


def _layout(fig: go.Figure, height: int, y_format: str = "") -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=56, r=16, t=36, b=40),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        font=dict(family="Microsoft YaHei, PingFang SC, sans-serif", color=TEXT_SECONDARY, size=13),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(color=TEXT_PRIMARY)),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, ticks="outside", tickcolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor="#c9c8c3", tickformat=y_format)
    return fig


def _fig_html(fig: go.Figure, first: bool) -> str:
    return '<div class="chart">' + fig.to_html(
        full_html=False, include_plotlyjs=True if first else False, config={"displaylogo": False, "responsive": True}
    ) + "</div>"


def render_backtest(result: BacktestResult, params: StrategyParams) -> Path:
    m: dict = result.metrics
    bench_name: str = next(iter(result.benchmark_metrics), "")
    bm: dict = result.benchmark_metrics.get(bench_name, {})
    start, end = result.balance.index[0], result.balance.index[-1]
    years: float = (end - start).days / 365.25

    body: list[str] = [
        "<h1>策略历史回测报告</h1>",
        f'<div class="sub">回测区间 {start:%Y-%m-%d} 至 {end:%Y-%m-%d}（约 {years:.1f} 年）· 初始资金 {_money(result.capital)} 元 · '
        f"按真实规则模拟：每周五收盘计算、下个交易日开盘成交、100份整手、扣除手续费和滑点</div>",
        '<div class="tiles">',
        _tile("年化收益", _pct(m["cagr"]), f"沪深300一直持有：{_pct(bm.get('cagr', 0))}"),
        _tile("最大回撤", _pct(m["max_drawdown"]), f"沪深300：{_pct(bm.get('max_drawdown', 0))}"),
        _tile("赚钱的年份", f"{m['positive_years']:.0%}", f"最差一年 {_pct(m['worst_year'], sign=True)}"),
        _tile("期末资产", _money(result.balance.iloc[-1]), f"总收益 {_pct(m['total_return'], 0, True)}"),
        "</div>",
        '<div class="box good"><b>一句话总结：</b>'
        f"过去 {years:.0f} 年，这个策略年化约 {_pct(m['cagr'])}，最惨的时候从高点回撤 {_pct(-m['max_drawdown'])}；"
        f"同期一直持有沪深300年化 {_pct(bm.get('cagr', 0))}，但中途最多亏过 {_pct(-bm.get('max_drawdown', 0))}。"
        "它的目标不是赚最多，而是用小得多的波动拿到稳定的收益，让人拿得住。</div>",
    ]

    # 净值曲线
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=result.balance.index, y=result.balance, name="本策略",
                             line=dict(color=SERIES[0], width=2), hovertemplate="%{y:,.0f} 元"))
    for i, (name, s) in enumerate(result.benchmarks.items(), 1):
        fig.add_trace(go.Scatter(x=s.index, y=s, name=name, line=dict(color=SERIES[i], width=2),
                                 hovertemplate="%{y:,.0f} 元"))
    body += ["<h2>资产曲线（元）</h2>", _fig_html(_layout(fig, 420, ",.0f"), True)]

    # 回撤
    fig = go.Figure()
    series: dict[str, pd.Series] = {"本策略": result.balance}
    if bench_name:
        series[bench_name] = result.benchmarks[bench_name].dropna()
    for i, (name, s) in enumerate(series.items()):
        dd: pd.Series = s / s.cummax() - 1
        fig.add_trace(go.Scatter(x=dd.index, y=dd, name=name, line=dict(color=SERIES[i], width=2),
                                 fill="tozeroy" if i == 0 else None, fillcolor="rgba(42,120,214,0.15)",
                                 hovertemplate="%{y:.1%}"))
    body += [
        "<h2>回撤：最难熬的时候亏多少</h2>",
        '<div class="sub">回撤 = 从之前的最高点跌下来的幅度。它决定了你会不会因为害怕而中途放弃——这是普通人投资失败最常见的原因。</div>',
        _fig_html(_layout(fig, 320, ".0%"), False),
    ]

    # 年度收益
    yearly: pd.DataFrame = result.yearly
    fig = go.Figure()
    for i, col in enumerate(yearly.columns[:2]):
        fig.add_trace(go.Bar(x=[str(y) for y in yearly.index], y=yearly[col], name=col,
                             marker=dict(color=SERIES[i], line=dict(color="#ffffff", width=2)),
                             hovertemplate="%{y:.1%}"))
    fig.update_layout(bargap=0.25)
    rows: str = "".join(
        f"<tr><td>{y}</td>" + "".join(f'<td class="num">{_pct(v, sign=True)}</td>' for v in yearly.loc[y]) + "</tr>"
        for y in yearly.index
    )
    head: str = "".join(f'<th class="num">{html.escape(c)}</th>' for c in yearly.columns)
    body += [
        "<h2>每年收益</h2>", _fig_html(_layout(fig, 340, ".0%"), False),
        f'<div class="wrap"><table><tr><th>年份</th>{head}</tr>{rows}</table></div>',
        f'<div class="sub">首年从回测起点 {start:%Y-%m} 开始计算，最后一年为截至目前的收益，都不满一整年。</div>',
    ]

    # 仓位分布
    cw: pd.DataFrame = result.class_weights
    fig = go.Figure()
    for i, col in enumerate(cw.columns):
        fig.add_trace(go.Scatter(x=cw.index, y=cw[col], name=col, stackgroup="one", mode="lines",
                                 line=dict(width=0.5, color=SERIES[i % len(SERIES)]), hovertemplate="%{y:.0%}"))
    fig.update_yaxes(range=[0, 1])
    avg: pd.Series = cw.mean()
    avg_rows: str = "".join(
        f'<tr><td>{html.escape(c)}</td><td>{html.escape(CLASS_NOTES.get(c, "随时可用的钱"))}</td><td class="num">{_pct(v, 0)}</td></tr>'
        for c, v in avg.items()
    )
    body += [
        "<h2>钱放在哪里</h2>",
        '<div class="sub">股票类资产走弱时，策略会自动把钱转到国债和货币基金；趋势好转后再买回来。</div>',
        _fig_html(_layout(fig, 360, ".0%"), False),
        f'<div class="wrap"><table><tr><th>大类</th><th>是什么</th><th class="num">平均仓位</th></tr>{avg_rows}</table></div>',
    ]

    # 指标
    metric_rows: list[tuple[str, str, str, str]] = [
        ("年化收益率", _pct(m["cagr"], 2), _pct(bm.get("cagr", 0), 2), "平均每年赚多少（复利）"),
        ("最大回撤", _pct(m["max_drawdown"], 2), _pct(bm.get("max_drawdown", 0), 2), "历史上从最高点最多跌了多少"),
        ("最长恢复时间", f"{m['underwater_days']} 天", f"{bm.get('underwater_days', 0)} 天", "跌下去后最长多久才涨回之前的最高点"),
        ("年化波动率", _pct(m["vol"], 2), _pct(bm.get("vol", 0), 2), "资产上下起伏的剧烈程度，越小越平稳"),
        ("夏普比率", f"{m['sharpe']:.2f}", f"{bm.get('sharpe', 0):.2f}", "每承担一份风险换来多少超额收益，>1 算优秀"),
        ("卡玛比率", f"{m['calmar']:.2f}", f"{bm.get('calmar', 0):.2f}", "年化收益 ÷ 最大回撤，越大越好"),
        ("赚钱年份占比", f"{m['positive_years']:.0%}", f"{bm.get('positive_years', 0):.0%}", ""),
        ("赚钱月份占比", f"{m['monthly_win_rate']:.0%}", f"{bm.get('monthly_win_rate', 0):.0%}", ""),
        ("最好 / 最差年份", f"{_pct(m['best_year'], 1, True)} / {_pct(m['worst_year'], 1, True)}",
         f"{_pct(bm.get('best_year', 0), 1, True)} / {_pct(bm.get('worst_year', 0), 1, True)}", ""),
    ]
    trows: str = "".join(
        f'<tr><td>{a}</td><td class="num">{b}</td><td class="num">{c}</td><td>{d}</td></tr>' for a, b, c, d in metric_rows
    )
    body += [
        "<h2>详细指标</h2>",
        f'<div class="wrap"><table><tr><th>指标</th><th class="num">本策略</th><th class="num">沪深300</th><th>含义</th></tr>{trows}</table></div>',
        f'<div class="sub">交易统计：共调仓 {result.rebalance_count} 次（平均每年约 {result.rebalance_count / max(years, 1e-9):.0f} 次），'
        f"成交 {result.trade_count} 笔，累计手续费和滑点 {_money(result.total_commission)} 元。</div>",
    ]

    body += [
        "<h2>策略规则（每周自动执行）</h2>",
        '<div class="box"><ol>'
        "<li><b>分散</b>：6个大类——A股大盘、A股成长、美股、黄金、债券、商品，它们很少同时下跌。</li>"
        f"<li><b>顺势</b>：只买价格在{params.trend_window}日均线之上、近期上涨的ETF；已持有的跌破均线{params.exit_buffer:.0%}以上才卖出。</li>"
        f"<li><b>择优</b>：每个大类只买最强的一只；新的必须比手上的强{params.switch_margin:.0%}以上才换。</li>"
        f"<li><b>控险</b>：波动大的少买，单只最多{params.max_weight:.0%}；组合波动超过{params.target_vol:.0%}时整体降仓。</li>"
        "<li><b>避险</b>：卖出后空出的钱放国债ETF（趋势向上时）或货币基金。</li>"
        "</ol></div>",
        '<div class="box warn"><b>请注意：</b>回测是用历史数据模拟的结果，未来市场可能和过去不同。'
        "策略在单边大牛市中会明显跑输股票（因为一直分散持有债券和黄金），这是为了稳而付出的代价。"
        "历史上也出现过连续一年多不创新高的阶段，需要耐心。</div>",
    ]

    REPORT_PATH.mkdir(parents=True, exist_ok=True)
    path: Path = REPORT_PATH.joinpath(f"回测报告_{datetime.now():%Y%m%d_%H%M%S}.html")
    path.write_text(_page("策略回测报告", "".join(body)), encoding="utf-8")
    return path


def render_advice(advice: Advice, params: StrategyParams) -> Path:
    a = advice
    n_orders: int = len(a.orders)
    body: list[str] = [
        "<h1>本周操作建议</h1>",
        f'<div class="sub">信号依据：{a.signal_date:%Y-%m-%d} 收盘 · 参考价：{a.price_date:%Y-%m-%d} 收盘价 · '
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M}</div>",
        '<div class="tiles">',
        _tile("账户总资产", _money(a.total_value) + " 元", f"其中现金 {_money(a.cash)} 元"),
        _tile("需要下单", f"{n_orders} 笔", "无需操作，继续持有" if not n_orders else "先卖后买"),
        _tile("调仓后剩余现金", _money(a.cash_after) + " 元", "留作手续费和价格波动缓冲"),
        "</div>",
    ]

    for w in a.warnings:
        body.append(f'<div class="box warn">⚠ {html.escape(w)}</div>')

    if a.orders:
        orows: str = "".join(
            f'<tr><td>{i}</td><td class="side-{"sell" if o.side == "卖出" else "buy"}">{o.side}</td>'
            f"<td>{o.code}</td><td>{html.escape(o.name)}</td>"
            f'<td class="num">{o.volume:,}</td><td class="num">{o.price:.3f}</td><td class="num">{_money(o.amount)}</td></tr>'
            for i, o in enumerate(a.orders, 1)
        )
        body += [
            "<h2>下单清单（按顺序操作）</h2>",
            f'<div class="wrap"><table><tr><th>#</th><th>方向</th><th>代码</th><th>名称</th><th class="num">数量（份）</th>'
            f'<th class="num">参考价</th><th class="num">约金额（元）</th></tr>{orows}</table></div>',
            '<div class="box"><b>怎么操作：</b><ol>'
            "<li>下一个交易日上午 9:30 开盘后，打开券商APP，进入“交易”。</li>"
            "<li><b>先完成所有“卖出”</b>：输入代码 → 选择卖出 → 数量按上表 → 价格填当时的“买一价”（立即成交）。</li>"
            "<li>再完成“买入”：输入代码 → 选择买入 → 数量按上表 → 价格填当时的“卖一价”。</li>"
            "<li>成交价和参考价差一点很正常。全部成交后，回到本程序选择【我已按建议下单】，自动更新持仓记录。</li>"
            "</ol></div>",
        ]
    else:
        body.append('<div class="box good"><b>本周不需要任何操作。</b>当前持仓和策略目标一致（偏差都在允许范围内），继续持有即可。</div>')

    prows: str = ""
    for r in a.rows:
        width: int = round(r.target_weight * 300)
        prows += (
            f"<tr><td>{html.escape(r.asset_class)}</td><td>{r.code} {html.escape(r.name)}</td>"
            f'<td class="num">{_pct(r.target_weight, 0)} <span class="bar" style="width:{width}px"></span></td>'
            f'<td class="num">{r.current_volume:,}</td><td class="num">{r.target_volume:,}</td>'
            f'<td class="num">{_money(r.target_value)}</td></tr>'
        )
    body += [
        "<h2>目标持仓</h2>",
        f'<div class="wrap"><table><tr><th>大类</th><th>ETF</th><th class="num">目标仓位</th><th class="num">当前（份）</th>'
        f'<th class="num">调整后（份）</th><th class="num">调整后市值</th></tr>{prows}</table></div>',
    ]
    if any(r.asset_class == "现金管理" and r.target_weight > 0 and r.target_volume == 0 for r in a.rows):
        body.append('<div class="sub">货币基金ETF一手约1万元，剩余资金不足一手时直接留作现金即可'
                    '（也可以用券商APP里的“现金理财”或“国债逆回购”，效果类似）。</div>')

    drows: str = "".join(
        f"<tr><td>{html.escape(d.asset_class)}</td><td>{html.escape(CLASS_NOTES.get(d.asset_class, ''))}</td>"
        f'<td>{html.escape(d.reason)}</td><td class="num">{_pct(d.weight, 0) if d.chosen else "—"}</td></tr>'
        for d in a.allocation.decisions
    )
    extra: str = ""
    if a.allocation.vol_scale < 0.999:
        extra = (f'<div class="sub">近期市场波动较大，预估组合波动 {_pct(a.allocation.portfolio_vol)}，'
                 f"超过上限 {_pct(params.target_vol, 0)}，股票等风险资产已整体降仓至 {_pct(a.allocation.vol_scale, 0)}。</div>")
    body += [
        "<h2>为什么这样配</h2>",
        f'<div class="wrap"><table><tr><th>大类</th><th>是什么</th><th>本周判断</th><th class="num">仓位</th></tr>{drows}</table></div>',
        extra,
    ]
    if a.ignored:
        items: str = "、".join(f"{c}（{v}份）" for c, v in a.ignored.items())
        body.append(f'<div class="sub">你还持有 {html.escape(items)}，不在策略范围内，程序不会处理，也不计入上面的总资产。</div>')

    REPORT_PATH.mkdir(parents=True, exist_ok=True)
    path: Path = REPORT_PATH.joinpath(f"操作建议_{a.signal_date:%Y%m%d}.html")
    path.write_text(_page("本周操作建议", "".join(body)), encoding="utf-8")
    return path


# ---------------- 命令行表格 ----------------

def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap: str = " " * max(width - _width(text), 0)
    return gap + text if right else text + gap


def text_table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> str:
    right = right or set()
    widths: list[int] = [max([_width(h)] + [_width(r[i]) for r in rows]) for i, h in enumerate(headers)]
    line: str = "  ".join(_pad(h, widths[i], i in right) for i, h in enumerate(headers))
    out: list[str] = [line, "-" * _width(line)]
    for r in rows:
        out.append("  ".join(_pad(c, widths[i], i in right) for i, c in enumerate(r)))
    return "\n".join(out)
