"""quant_web.predict.limits：涨跌停价格规则、涨停识别、连板数"""
import random
from datetime import date, timedelta

import polars as pl
import pytest

from quant_web.predict import limits
from quant_web.predict.limits import add_limit_columns, limit_price, limit_ratio


def test_limit_ratio_rules() -> None:
    assert limit_ratio("600519", False, date(2019, 1, 2)) == 0.10
    assert limit_ratio("000001", True, date(2025, 7, 7)) == 0.05       # 主板 ST（数据核实：2025-07-07 并未改变）
    assert limit_ratio("000001", True, date(2026, 7, 3)) == 0.05
    assert limit_ratio("000001", True, date(2026, 7, 6)) == 0.10       # 2026-07-06 起 10%
    assert limit_ratio("300750", False, date(2020, 8, 21)) == 0.10     # 创业板改革前
    assert limit_ratio("300750", True, date(2020, 8, 21)) == 0.05
    assert limit_ratio("300750", False, date(2020, 8, 24)) == 0.20
    assert limit_ratio("301001", True, date(2023, 1, 3)) == 0.20       # 改革后创业板 ST 也是 20%
    assert limit_ratio("688981", False, date(2020, 7, 16)) == 0.20
    assert limit_ratio("689009", True, date(2022, 1, 4)) == 0.20
    assert limit_ratio("920002", False, date(2024, 6, 3)) == 0.30
    assert limit_ratio("430047", False, date(2022, 6, 1)) == 0.30


def test_limit_price_round_half_up() -> None:
    assert limit_price(10.0, 0.1) == 11.0
    assert limit_price(7.13, 0.1) == 7.84        # 7.843
    assert limit_price(7.15, 0.1) == 7.87        # 7.865 → 进位（银行家舍入会得到 7.86）
    assert limit_price(1.15, 0.1) == 1.27        # 1.265 浮点表示为 1.26499…，必须按十进制处理
    assert limit_price(11.45, -0.1) == 10.31     # 10.305
    assert limit_price(22.03, 0.2) == 26.44
    assert limit_price(2.98, -0.05) == 2.83      # 2.831
    assert limit_price(28.31, 0.1) == 31.14


def test_vectorized_rounding_matches_decimal() -> None:
    rng = random.Random(7)
    pres: list[float] = [round(rng.uniform(0.5, 500), rng.choice([2, 3])) for _ in range(5000)]
    pres += [1.15, 7.15, 11.45, 2.05, 3.35, 9.95, 100.05, 0.95]
    for ratio in (0.05, 0.1, 0.2, 0.3, -0.05, -0.1, -0.2, -0.3):
        got = pl.DataFrame({"p": pres}).select(limits._round2(pl.col("p") * (1 + ratio)))["p"].to_list()
        want = [limit_price(p, ratio) for p in pres]
        assert got == want


def _frame(code: str, start: date, rows: list[tuple], is_st: bool = False) -> pl.DataFrame:
    """rows: (preclose, open, high, low, close)，日期按工作日连续"""
    dates: list[date] = []
    d = start
    while len(dates) < len(rows):
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    return pl.DataFrame({
        "date": dates,
        "code": [code] * len(rows),
        "preclose": [r[0] for r in rows],
        "open": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "close": [r[4] for r in rows],
        "is_st": [is_st] * len(rows),
    }, schema_overrides={c: pl.Float64 for c in ("preclose", "open", "high", "low", "close")})


def _universe(items: list[tuple[str, date | None]]) -> pl.DataFrame:
    return pl.DataFrame({"code": [c for c, _ in items], "list_date": [d for _, d in items]},
                        schema={"code": pl.Utf8, "list_date": pl.Date})


def test_one_word_broken_streak_and_limit_down() -> None:
    panel = _frame("600001", date(2024, 3, 4), [
        (10.00, 11.00, 11.00, 11.00, 11.00),   # 一字涨停
        (11.00, 11.50, 12.10, 11.30, 12.10),   # 涨停（非一字），2连板
        (12.10, 12.50, 13.31, 12.20, 13.00),   # 触板未封 = 炸板
        (13.00, 12.00, 12.30, 11.70, 11.70),   # 跌停 13*0.9=11.7
        (11.70, 11.80, 12.87, 11.80, 12.87),   # 涨停 11.7*1.1=12.87
        (12.87, 12.90, 13.00, 12.50, 12.80),   # 普通
    ])
    out = add_limit_columns(panel, _universe([("600001", date(2010, 1, 4))])).sort("date")
    assert out["limit_up"].to_list() == [11.0, 12.1, 13.31, 14.3, 12.87, 14.16]
    assert out["is_limit_up"].to_list() == [True, True, False, False, True, False]
    assert out["one_word"].to_list() == [True, False, False, False, False, False]
    assert out["touched_up"].to_list() == [True, True, True, False, True, False]
    assert out["is_broken"].to_list() == [False, False, True, False, False, False]
    assert out["is_limit_down"].to_list() == [False, False, False, True, False, False]
    assert out["streak"].to_list() == [1, 2, 0, 0, 1, 0]
    assert out["streak"].dtype == pl.Int16
    assert out["list_days"].dtype == pl.Int32
    assert out["pct"][0] == pytest.approx(0.1)
    assert not out["no_limit"].any()
    assert set(out["board"].to_list()) == {"main"}


def test_streak_not_broken_by_suspension_and_counts_per_stock() -> None:
    a = _frame("000002", date(2024, 1, 2), [(10.0, 10.5, 11.0, 10.5, 11.0), (11.0, 12.1, 12.1, 12.1, 12.1)])
    # 停牌一周后继续涨停
    b = _frame("000002", date(2024, 1, 15), [(12.1, 13.31, 13.31, 13.31, 13.31)])
    c = _frame("000003", date(2024, 1, 2), [(5.0, 5.5, 5.5, 5.5, 5.5), (5.5, 5.6, 5.7, 5.4, 5.5)])
    uni = _universe([("000002", date(2000, 1, 4)), ("000003", date(2000, 1, 4))])
    out = add_limit_columns(pl.concat([a, b, c]), uni)
    s2 = out.filter(pl.col("code") == "000002").sort("date")["streak"].to_list()
    s3 = out.filter(pl.col("code") == "000003").sort("date")["streak"].to_list()
    assert s2 == [1, 2, 3]
    assert s3 == [1, 0]


def test_st_main_board_before_and_after_2026_07_06() -> None:
    before = _frame("600002", date(2026, 6, 1), [(10.0, 10.2, 10.5, 10.1, 10.5), (10.5, 10.4, 10.5, 9.97, 9.98)],
                    is_st=True)
    after = _frame("600002", date(2026, 7, 6), [(10.0, 10.2, 10.5, 10.1, 10.5), (10.5, 11.0, 11.55, 11.0, 11.55)],
                   is_st=True)
    out = add_limit_columns(pl.concat([before, after]), _universe([("600002", date(2001, 1, 2))])).sort("date")
    assert out["limit_up"].to_list() == [10.5, 11.03, 11.0, 11.55]
    assert out["is_limit_up"].to_list() == [True, False, False, True]
    assert out["is_limit_down"].to_list() == [False, True, False, False]   # 10.5*0.95=9.975→9.98


def test_st_approximation_uses_10pct_after_big_move() -> None:
    """当前名称是 ST，但历史上某天涨了 9%（5% 限制下不可能）→ 之后20个交易日按 10% 处理"""
    rows = [(10.0, 10.0, 10.9, 10.0, 10.9)] + [(10.9, 10.9, 11.2, 10.8, 11.0)] * 3 + [(11.0, 11.2, 11.55, 11.1, 11.55)]
    panel = _frame("000004", date(2024, 5, 6), rows, is_st=True)
    out = add_limit_columns(panel, _universe([("000004", date(1995, 1, 3))])).sort("date")
    assert out["limit_up"][-1] == 12.1          # 仍按 10%
    assert not out["is_limit_up"][-1]           # +5% 不再被误判为涨停
    # 没有大涨过的 ST 股，+5% 就是涨停
    panel2 = _frame("000005", date(2024, 5, 6), [(10.0, 10.1, 10.5, 10.0, 10.5)], is_st=True)
    out2 = add_limit_columns(panel2, _universe([("000005", date(1995, 1, 3))]))
    assert out2["is_limit_up"][0]


def test_chinext_before_and_after_reform() -> None:
    before = _frame("300001", date(2020, 8, 20), [(10.0, 10.5, 11.0, 10.4, 11.0), (11.0, 11.5, 12.0, 11.2, 11.9)])
    after = _frame("300001", date(2020, 8, 24), [(11.9, 12.5, 14.28, 12.4, 14.28), (14.28, 14.5, 15.71, 14.3, 15.71)])
    out = add_limit_columns(pl.concat([before, after]), _universe([("300001", date(2009, 10, 30))])).sort("date")
    assert out["limit_up"].to_list() == [11.0, 12.1, 14.28, 17.14]
    assert out["is_limit_up"].to_list() == [True, False, True, False]
    assert out["board"][0] == "chinext"


def test_ipo_no_limit_periods() -> None:
    star = _frame("688001", date(2021, 1, 4), [(None, 30, 60, 28, 50)] + [(50, 50, 70, 45, 50)] * 5
                  + [(50, 50, 60, 50, 60)])
    main_old = _frame("603001", date(2022, 3, 1), [(None, 12, 14.4, 12, 14.4), (14.4, 15.84, 15.84, 15.84, 15.84)])
    main_new = _frame("603002", date(2023, 5, 8), [(None, 20, 50, 20, 40)] + [(40, 41, 44, 39, 44)] * 5)
    bj = _frame("920001", date(2024, 6, 3), [(None, 10, 20, 9, 18), (18, 18, 23.4, 18, 23.4)])
    uni = _universe([("688001", date(2021, 1, 4)), ("603001", date(2022, 3, 1)),
                     ("603002", date(2023, 5, 8)), ("920001", date(2024, 6, 3))])
    out = add_limit_columns(pl.concat([star, main_old, main_new, bj]), uni)

    def col(code: str, name: str) -> list:
        return out.filter(pl.col("code") == code).sort("date")[name].to_list()

    assert col("688001", "no_limit") == [True] * 5 + [False] * 2
    assert col("688001", "list_days")[:3] == [1, 2, 3]
    assert col("688001", "is_limit_up") == [False] * 6 + [True]
    assert col("688001", "limit_up")[0] is None
    assert col("603001", "no_limit") == [True, False]            # 注册制前主板：仅首日
    assert col("603001", "one_word") == [False, True]
    assert col("603002", "no_limit") == [True] * 5 + [False]     # 主板注册制：前5日
    assert col("920001", "no_limit") == [True, False]            # 北交所：首日
    assert col("920001", "is_limit_up") == [False, True]         # 30%


def test_list_days_estimated_when_unknown() -> None:
    panel = _frame("000006", date(2019, 1, 2), [(10.0, 10, 10.2, 9.9, 10.1)] * 3)
    out = add_limit_columns(panel, _universe([]))
    assert out["list_days"].min() >= 250          # 不知道上市日期 → 视为老股票
    assert not out["no_limit"].any()
    old = add_limit_columns(panel, _universe([("000006", date(2010, 1, 4))]))
    assert old["list_days"][0] > 2000             # 面板开始前上市：按日历估算


def test_resume_and_delisting_days_without_limit() -> None:
    # 长期停牌后恢复上市首日大涨 45%：不设涨跌幅；普通复牌（涨幅在范围内）照常
    a = _frame("600145", date(2024, 1, 2), [(7.4, 7.4, 7.5, 7.3, 7.4)])
    b = _frame("600145", date(2024, 6, 3), [(1.87, 2.71, 2.71, 2.71, 2.71), (2.71, 2.6, 2.6, 2.57, 2.57)])
    c = _frame("600146", date(2024, 1, 2), [(5.0, 5.0, 5.1, 4.9, 5.0)])
    d = _frame("600146", date(2024, 6, 3), [(5.0, 5.5, 5.5, 5.5, 5.5)])
    # 已退市股票：退市整理期首日跌 70%
    rows = [(1.0, 1.0, 1.0, 1.0, 1.0)] * 5 + [(1.0, 0.3, 0.35, 0.28, 0.30)] + [(0.30, 0.29, 0.29, 0.285, 0.285)] * 14
    e = _frame("600147", date(2024, 1, 2), rows)
    uni = pl.DataFrame({
        "code": ["600145", "600146", "600147"], "list_date": [date(1998, 1, 5)] * 3, "status": [1, 1, 0],
    }, schema={"code": pl.Utf8, "list_date": pl.Date, "status": pl.Int8})
    out = add_limit_columns(pl.concat([a, b, c, d, e]), uni)

    def col(code: str, name: str) -> list:
        return out.filter(pl.col("code") == code).sort("date")[name].to_list()

    assert col("600145", "no_limit") == [False, True, False]
    assert col("600146", "no_limit") == [False, False] and col("600146", "is_limit_up") == [False, True]
    assert col("600147", "no_limit")[5] and sum(col("600147", "no_limit")) == 1
    assert not col("600147", "is_limit_down")[5]
    # 面板第一行就带昨收且离面板起点很远（停牌跨过面板起点）也按长期停牌处理
    f = _frame("600148", date(2024, 6, 3), [(1.87, 2.71, 2.71, 2.71, 2.71)])
    g = _frame("600149", date(2024, 1, 2), [(5.0, 5.0, 5.1, 4.9, 5.0)])
    out2 = add_limit_columns(pl.concat([f, g]), uni)
    assert out2.filter(pl.col("code") == "600148")["no_limit"].to_list() == [True]
