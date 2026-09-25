"""
命令行入口与中文交互菜单。

    python -m etf_quant              打开菜单
    python -m etf_quant update       更新行情数据
    python -m etf_quant advice       生成本周操作建议
    python -m etf_quant apply        按最近一次建议更新持仓
    python -m etf_quant backtest     历史回测
    python -m etf_quant status       查看持仓
    python -m etf_quant init 100000  用10万元现金初始化持仓
"""
import argparse
import os
import sys
import webbrowser
from collections.abc import Callable
from datetime import date
from pathlib import Path

from .config import CODE_MAP, HOLDINGS_FILE, ROOT, StrategyParams, TradingParams, Universe
from .data import china_now, data_is_fresh, fetch_latest_prices, update_all
from .portfolio import Holdings, append_nav, holdings_exist, load_holdings, load_nav, save_holdings


LINE: str = "=" * 60


def open_file(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)     # type: ignore[attr-defined]  # noqa: S606
        else:
            webbrowser.open(path.as_uri())
    except OSError:
        print(f"请手动打开：{path}")


def ask(prompt: str, default: str = "") -> str:
    try:
        value: str = input(prompt).strip()
    except EOFError:
        return default
    return value or default


def confirm(prompt: str) -> bool:
    return ask(f"{prompt}（输入 y 确认，直接回车取消）：").lower() in ("y", "yes", "是")


# ---------------- 各项功能 ----------------

def cmd_update() -> bool:
    print("\n正在下载最新行情（约需1分钟）……")
    ok, failed = update_all()
    if failed and not ok:
        print("\n全部下载失败，请检查网络后重试。")
        return False
    if failed:
        print(f"\n部分下载失败：{'、'.join(failed)}（已保留旧数据，可稍后重试）")
    else:
        print("\n行情数据已更新。")
    return True


def cmd_init(capital: float | None = None) -> None:
    if holdings_exist():
        print(f"\n已有持仓文件：{HOLDINGS_FILE}")
        if not confirm("重新初始化会清空现有持仓记录，确定吗？"):
            return
    if capital is None:
        print("\n请输入准备投入的资金（元）。")
        print("建议：先用“模拟盘”跑一两个月熟悉流程——输入一个金额，只按建议记账、不真的下单。")
        text: str = ask("资金金额，例如 100000：")
        try:
            capital = float(text.replace(",", ""))
        except ValueError:
            print("输入的不是数字，已取消。")
            return
    if capital <= 0:
        print("金额必须大于0。")
        return
    save_holdings(Holdings(cash=capital))
    append_nav(capital, capital, "初始资金", china_now().date())
    print(f"\n已创建持仓文件：{HOLDINGS_FILE}")
    print(f"当前：现金 {capital:,.2f} 元，无持仓。")
    print("如果你账户里已经有这些ETF，可以用Excel打开上面的文件，把份额填进去。")


def cmd_advice(update: bool = True, open_report: bool = True) -> None:
    from .advisor import make_advice
    from .report import render_advice, text_table

    if not holdings_exist():
        print("\n还没有持仓记录，先设置一下资金。")
        cmd_init()
        if not holdings_exist():
            return

    if update and not data_is_fresh():
        cmd_update()        # 下载失败时继续使用本地旧数据，数据过旧会有提示

    print("\n正在计算本周操作建议……")
    params = StrategyParams()
    advice = make_advice(params=params)

    print(f"\n{LINE}\n本周操作建议（依据 {advice.signal_date:%Y-%m-%d} 收盘数据）\n{LINE}")
    print(f"账户总资产：{advice.total_value:,.0f} 元（现金 {advice.cash:,.0f} 元）")
    for w in advice.warnings:
        print(f"⚠ {w}")

    if advice.orders:
        rows: list[list[str]] = [
            [str(i), o.side, o.code, o.name, f"{o.volume:,}", f"{o.price:.3f}", f"{o.amount:,.0f}"]
            for i, o in enumerate(advice.orders, 1)
        ]
        print("\n下单清单（下一个交易日开盘后操作，先卖后买）：\n")
        print(text_table(["#", "方向", "代码", "名称", "数量(份)", "参考价", "约金额(元)"], rows, right={4, 5, 6}))
        print(f"\n调仓后预计剩余现金：{advice.cash_after:,.0f} 元")
        print("全部成交后，回到菜单选择【我已按建议下单】自动更新持仓。")
    else:
        print("\n✔ 本周不需要任何操作，继续持有即可。")

    print("\n目标配置：")
    rows = [
        [d.asset_class, (CODE_MAP[d.chosen.split(".")[0]].name if d.chosen else "不持有"),
         f"{d.weight:.0%}" if d.chosen else "—"]
        for d in advice.allocation.decisions
    ]
    cash_w: float = advice.allocation.weights.get(Universe().cash, 0.0)
    rows.append(["现金管理", "银华日利（货币基金）", f"{cash_w:.0%}"])
    print(text_table(["大类", "持有", "仓位"], rows, right={2}))

    path: Path = render_advice(advice, params)
    print(f"\n详细说明（含每一类的选择理由）已保存：{path}")
    if open_report:
        open_file(path)


def cmd_apply() -> None:
    from .advisor import apply_saved_advice, load_saved_advice

    data: dict | None = load_saved_advice()
    if not data:
        print("\n还没有生成过操作建议。")
        return
    if data.get("applied"):
        print("\n最近一次建议已经更新过持仓了。")
        return
    if not data["orders"]:
        print("\n最近一次建议不需要下单，持仓无需更新。")
        return

    print(f"\n最近一次建议（{data['signal_date']}）包含以下委托：")
    for o in data["orders"]:
        print(f"  {o['side']} {o['code']} {o['name']} {o['volume']:,} 份 @ {o['price']:.3f}")
    print("\n将按参考价更新持仓（手续费按估算扣除）。如果实际成交价格/数量不同，之后可在持仓文件里手动修正。")
    if not confirm("确认这些委托都已成交？"):
        return
    holdings, _, total = apply_saved_advice()
    print(f"\n持仓已更新。当前现金 {holdings.cash:,.2f} 元，按参考价估算总资产 {total:,.0f} 元。")


def cmd_status() -> None:
    if not holdings_exist():
        print("\n还没有持仓记录。")
        return
    holdings: Holdings = load_holdings()
    from .report import text_table

    etfs = [CODE_MAP[c] for c in holdings.positions if c in CODE_MAP]
    print("\n正在获取最新价格……")
    quotes = fetch_latest_prices(etfs) if etfs else {}
    prices: dict[str, float] = {vt.split(".")[0]: p for vt, (_, p) in quotes.items()}

    rows: list[list[str]] = []
    for code, volume in sorted(holdings.positions.items()):
        etf = CODE_MAP.get(code)
        price: float | None = prices.get(code)
        value: str = f"{volume * price:,.0f}" if price else "未知"
        rows.append([code, etf.name if etf else "（非策略品种）", f"{volume:,}", f"{price:.3f}" if price else "-", value])
    rows.append(["现金", "", "", "", f"{holdings.cash:,.0f}"])
    total: float = holdings.total_value(prices)

    print(f"\n{LINE}\n我的持仓（文件：{HOLDINGS_FILE}）\n{LINE}")
    print(text_table(["代码", "名称", "份额", "最新价", "市值(元)"], rows, right={2, 3, 4}))
    print(f"\n总资产约：{total:,.0f} 元")

    history: list[dict] = load_nav()
    if history:
        first: float = float(history[0]["总资产"])
        if first > 0:
            print(f"起始资金 {first:,.0f} 元（{history[0]['日期']}），累计收益 {total / first - 1:+.2%}")


def cmd_backtest(start: str, end: str | None, capital: float, open_report: bool = True) -> None:
    from .backtest import run_backtest
    from .report import render_backtest

    if not data_is_fresh():
        cmd_update()        # 下载失败时继续使用本地旧数据
    print("\n正在回测（用 vnpy.alpha 回测引擎逐日模拟，约10秒）……")
    params = StrategyParams()
    result = run_backtest(start=start, end=end, capital=capital, params=params, trading=TradingParams())
    m: dict = result.metrics
    print(f"\n{LINE}\n回测结果 {result.balance.index[0]:%Y-%m-%d} ~ {result.balance.index[-1]:%Y-%m-%d}\n{LINE}")
    print(f"初始资金 {capital:,.0f} 元 → 期末 {result.balance.iloc[-1]:,.0f} 元（总收益 {m['total_return']:+.1%}）")
    print(f"年化收益 {m['cagr']:.2%}  最大回撤 {m['max_drawdown']:.2%}  夏普比率 {m['sharpe']:.2f}")
    print(f"赚钱年份 {m['positive_years']:.0%}  最差一年 {m['worst_year']:+.2%}  最好一年 {m['best_year']:+.2%}")
    for name, bm in result.benchmark_metrics.items():
        print(f"对比 {name}：年化 {bm['cagr']:.2%}，最大回撤 {bm['max_drawdown']:.2%}")
    path: Path = render_backtest(result, params)
    print(f"\n图文报告已保存：{path}")
    if open_report:
        open_file(path)


def cmd_help() -> None:
    readme: Path = ROOT / "README.md"
    print(f"""
{LINE}
使用方法（每周只需要5分钟）
{LINE}
1. 第一次使用：选【历史回测报告】了解策略；再选【本周操作建议】，按提示输入资金。
2. 每周五收盘后（15:30以后）或周末：选【本周操作建议】。
   - 显示"不需要操作"：什么都不用做。
   - 有下单清单：下周一 9:30 开盘后，在券商APP里先卖后买，按清单下单。
3. 下单成交后：选【我已按建议下单】，程序自动更新你的持仓记录。
4. 不要因为一两周的涨跌去手动改动，严格执行才是这套方法有效的前提。

完整说明书：{readme}
""")
    if readme.exists() and confirm("现在打开完整说明书吗？"):
        open_file(readme)


def menu() -> None:
    actions: dict[str, tuple[str, Callable[[], None]]] = {
        "1": ("本周操作建议（买什么、买多少）", lambda: cmd_advice()),
        "2": ("我已按建议下单 → 更新持仓记录", cmd_apply),
        "3": ("查看我的持仓和收益", cmd_status),
        "4": ("历史回测报告（策略过去表现如何）", lambda: cmd_backtest("2014-06-01", None, 200_000)),
        "5": ("手动更新行情数据", cmd_update),
        "6": ("设置/重置资金和持仓", lambda: cmd_init()),
        "7": ("使用说明", cmd_help),
    }
    while True:
        print(f"\n{LINE}\n   稳健ETF量化助手  ·  基于 VeighNa (vnpy.alpha)\n   今天：{date.today():%Y-%m-%d}   北京时间：{china_now():%H:%M}\n{LINE}")
        for key, (label, _) in actions.items():
            print(f"  {key}. {label}")
        print("  0. 退出")
        try:
            choice: str = input("\n请输入数字并回车：").strip()
        except EOFError:
            return
        if choice == "0":
            return
        if not choice:
            continue
        action = actions.get(choice)
        if not action:
            print("没有这个选项。")
            continue
        try:
            action[1]()
        except KeyboardInterrupt:
            print("\n已取消。")
        except Exception as e:  # noqa: BLE001  菜单模式下任何错误都不应让程序直接退出
            print(f"\n出错了：{e}")
            print("如果反复出现，可以把这段文字发给懂技术的朋友帮忙看看。")
        ask("\n按回车返回菜单……")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="etf_quant", description="稳健ETF量化助手")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("update", help="更新行情数据")
    p_adv = sub.add_parser("advice", help="生成本周操作建议")
    p_adv.add_argument("--no-update", action="store_true", help="不自动更新行情")
    p_adv.add_argument("--no-open", action="store_true", help="不自动打开报告")
    sub.add_parser("apply", help="按最近一次建议更新持仓")
    sub.add_parser("status", help="查看持仓")
    p_bt = sub.add_parser("backtest", help="历史回测")
    p_bt.add_argument("--start", default="2014-06-01")
    p_bt.add_argument("--end", default=None)
    p_bt.add_argument("--capital", type=float, default=200_000)
    p_bt.add_argument("--no-open", action="store_true")
    p_init = sub.add_parser("init", help="初始化持仓")
    p_init.add_argument("capital", type=float, nargs="?")
    args = parser.parse_args(argv)

    if args.command == "update":
        cmd_update()
    elif args.command == "advice":
        cmd_advice(update=not args.no_update, open_report=not args.no_open)
    elif args.command == "apply":
        cmd_apply()
    elif args.command == "status":
        cmd_status()
    elif args.command == "backtest":
        cmd_backtest(args.start, args.end, args.capital, open_report=not args.no_open)
    elif args.command == "init":
        cmd_init(args.capital)
    else:
        menu()
