"""quant_web 多因子选股：财报按公告日生效（无未来数据）、除权收益、次日开盘买卖与涨跌停约束、费用、因子方向（全部离线，用假数据）"""
import numpy as np
import pandas as pd
import pytest

from quant_web.multifactor import evaluate as E
from quant_web.multifactor import panel as P


def _frame(values: dict[str, list[float]], dates: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(values, index=dates, dtype=float)


def make_panel(opens: dict, closes: dict, precloses: dict | None = None, st: dict | None = None) -> P.Panel:
    """closes/opens：{code: [价格...]}；precloses 缺省为上一日收盘（第一天为当日开盘）；NaN = 停牌"""
    n = len(next(iter(closes.values())))
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = _frame(closes, dates)
    open_ = _frame(opens, dates)
    if precloses is None:
        pre = close.ffill().shift(1)
        pre.iloc[0] = open_.iloc[0]
    else:
        pre = _frame(precloses, dates)
    px = {"open": open_, "close": close, "preclose": pre, "high": np.maximum(open_, close), "low": np.minimum(open_, close),
          "volume": close.notna().astype(float) * 1e6, "amount": close.notna().astype(float) * 1e8, "turn": close * 0 + 1.0}
    uni = pd.DataFrame({"industry": ["A"] * len(closes), "name": list(closes), "list_date": ["2000-01-01"] * len(closes)},
                       index=list(closes))
    st_df = None
    if st:
        st_df = pd.DataFrame(False, index=dates, columns=list(closes))
        for c, flags in st.items():
            st_df[c] = flags
    return P.from_wide(px, uni, st=st_df)


def test_adjusted_returns_use_exchange_preclose() -> None:
    # 第 3 天 10 送 10：前收盘 20 → 除权参考价 10，收盘 10.5 实际涨 5%
    p = make_panel(opens={"A": [10, 20, 10.2, 10.5]}, closes={"A": [20, 20, 10.5, 10.5]},
                   precloses={"A": [10, 20, 10, 10.5]})
    assert p.ret["A"].round(6).tolist() == [1.0, 0.0, 0.05, 0.0]
    # 复权开盘价与复权收盘价同口径：第 3 天开盘 10.2 相对参考价 10 为 +2%
    assert p.adj_open["A"].iloc[2] / p.adj_close["A"].iloc[1] == pytest.approx(1.02)


def test_limit_flags_and_st_ratio() -> None:
    p = make_panel(opens={"A": [10, 11, 11], "B": [10, 10.5, 9.97]}, closes={"A": [10, 11, 11], "B": [10, 10.5, 9.98]},
                   st={"B": [True, True, True]})
    assert bool(p.limit_up_open["A"].iloc[1]) and bool(p.limit_up_close["A"].iloc[1])      # 10 → 11 主板 10%
    assert bool(p.limit_up_close["B"].iloc[1])                                           # ST 5%：10 → 10.5
    assert bool(p.limit_down_open["B"].iloc[2])                                          # 10.5×0.95 = 9.975 → 9.98
    assert not bool(p.limit_up_open["A"].iloc[2])


def test_fund_asof_is_point_in_time() -> None:
    dates = pd.bdate_range("2024-04-01", periods=40)
    codes = pd.Index(["000001"])
    f = pd.DataFrame({
        "code": ["000001"] * 3,
        "report_date": pd.to_datetime(["2023-12-31", "2024-03-31", "2023-09-30"]),
        "eff": pd.to_datetime(["2024-04-10", "2024-04-10", "2024-04-20"]),     # 年报与一季报同日公布；三季报补发
        "v": [1.0, 2.0, 9.0],
    })
    out = P.fund_asof(f, {"v": f["v"]}, dates, codes)["v"]["000001"]
    assert out.loc[:"2024-04-10"].isna().all()                     # 公告当天还不能用（收盘后才知道）
    assert out.loc["2024-04-11"] == 2.0                            # 同日多期取最新报告期
    assert out.loc["2024-04-23"] == 2.0                            # 旧报告期补发不回退


def test_fund_asof_missing_value_does_not_carry_old() -> None:
    dates = pd.bdate_range("2024-04-01", periods=30)
    f = pd.DataFrame({"code": ["000001"] * 2, "report_date": pd.to_datetime(["2023-12-31", "2024-03-31"]),
                      "eff": pd.to_datetime(["2024-04-02", "2024-04-15"]), "v": [1.0, np.nan]})
    out = P.fund_asof(f, {"v": f["v"]}, dates, pd.Index(["000001"]))["v"]["000001"]
    assert out.loc["2024-04-10"] == 1.0 and np.isnan(out.loc["2024-04-17"])


def test_single_quarter_from_cumulative() -> None:
    f = pd.DataFrame({"code": ["X"] * 4, "report_date": pd.to_datetime(["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]),
                      "np": [1.0, 3.0, 6.0, 10.0]})
    assert P.single_quarter(f, "np").tolist() == [1.0, 2.0, 3.0, 4.0]


def test_rebalance_dates_last_trading_day() -> None:
    dates = pd.bdate_range("2024-01-01", "2024-03-29")
    m = E.rebalance_dates(dates, "M")
    assert [d.strftime("%m-%d") for d in m] == ["01-31", "02-29"]          # 最后一天（3/29）没有次日开盘
    w = E.rebalance_dates(dates, "W")
    assert all(d.weekday() == 4 for d in w)


def test_forward_returns_next_open_and_unbuyable_limit_up() -> None:
    # 信号日 d0、d2；A 在 d1 开盘涨停（买不进 → NaN）；B 正常
    p = make_panel(opens={"A": [10, 11, 11.5, 12, 12], "B": [10, 10, 10.2, 10.4, 10.6]},
                   closes={"A": [10, 11, 11.8, 12, 12], "B": [10, 10.1, 10.3, 10.5, 10.6]})
    sd = p.dates[[0, 2]]
    fwd = E.forward_open_returns(p, sd)
    assert np.isnan(fwd.loc[sd[0], "A"])
    assert fwd.loc[sd[0], "B"] == pytest.approx(10.4 / 10.0 - 1)             # d1 开盘买 → d3 开盘卖


def test_backtest_skips_limit_up_and_charges_costs() -> None:
    # 3 只股票，分数 A > B > C；A 次日开盘涨停买不进 → 买 B（top_n=1）
    n = 6
    p = make_panel(opens={"A": [10, 11, 11, 11, 11, 11], "B": [10, 10, 10, 10, 10, 10], "C": [10] * n},
                   closes={"A": [10, 11, 11, 11, 11, 11], "B": [10, 10, 10, 10, 10, 10], "C": [10] * n})
    sd = p.dates[[0, 3]]
    score = pd.DataFrame([[3.0, 2.0, 1.0]] * n, index=p.dates, columns=p.codes)
    mask = pd.DataFrame(True, index=p.dates, columns=p.codes)
    bt = E.backtest(p, score, mask, sd, top_n=1, slippage=0.001)
    assert bt.holdings[sd[0]] == ["B"]
    # 第一期：B 价格不变，只付买入费用（佣金+滑点）
    assert bt.period_ret.iloc[0] == pytest.approx(-(0.00025 + 0.001), rel=1e-6)


def test_backtest_cannot_sell_at_limit_down() -> None:
    # A 第一期买入；第二期分数掉到最后，但卖出日开盘跌停 → 继续持有
    n = 6
    p = make_panel(opens={"A": [10, 10, 10, 9, 9, 9], "B": [10] * n}, closes={"A": [10, 10, 10, 9, 9, 9], "B": [10] * n})
    sd = p.dates[[0, 2]]
    score = pd.DataFrame({"A": [2.0, 2.0, 1.0, 1.0, 1.0, 1.0], "B": [1.0, 1.0, 2.0, 2.0, 2.0, 2.0]}, index=p.dates)
    mask = pd.DataFrame(True, index=p.dates, columns=p.codes)
    bt = E.backtest(p, score, mask, sd, top_n=1)
    assert bt.holdings[sd[0]] == ["A"] and bt.holdings[sd[1]] == ["A"]


def test_zscore_is_rank_based_and_centered() -> None:
    df = pd.DataFrame([[1.0, 2.0, 3.0, 1000.0]])
    z = E.zscore(df)
    assert z.iloc[0].mean() == pytest.approx(0.0, abs=1e-12)
    assert z.iloc[0, 3] - z.iloc[0, 2] == pytest.approx(z.iloc[0, 1] - z.iloc[0, 0])     # 极值不放大


def test_factor_directions_are_prespecified() -> None:
    from quant_web.multifactor import factors as F
    d = {f.key: f.direction for f in F.FACTORS}
    # 文献先验：A股短期反转、低换手、低波动、价值、业绩超预期
    assert d["rev_20"] == -1 and d["turn_20"] == -1 and d["ivol_20"] == -1 and d["ep"] == 1 and d["sue"] == 1
    assert len(d) == len(F.FACTORS)                                          # key 不重复


# ---------------------------------------------------------------- 接口（临时 workspace，假结果文件）

@pytest.fixture()
def mf_client(tmp_path, monkeypatch):
    import json
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from quant_web import config
    from quant_web.api import server
    from quant_web.multifactor import service
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("QUANT_WEB_NO_SCHEDULER", "1")
    d = tmp_path / "multifactor"
    monkeypatch.setattr(service, "DIR", d)
    monkeypatch.setattr(service, "TODAY_FILE", d / "today.json")
    monkeypatch.setattr(service, "REPORT_FILE", d / "report.json")
    monkeypatch.setattr(service, "TRACK_FILE", d / "track.json")
    server.CACHE.clear()
    with TestClient(server.create_app(background=False)) as c:
        yield c, d, json


def test_mf_api_empty_and_plan(mf_client) -> None:
    c, d, json = mf_client
    r = c.get("/api/mf/today").json()
    assert r["today"] is None and r["config"]["top_n"] == 50
    assert c.get("/api/mf/plan").status_code == 404
    d.mkdir(parents=True)
    today = {"date": "2026-09-24", "next_trade_day": "2026-09-28", "rebalance_day": True,
             "rows": [{"code": "600001", "name": "甲", "close": 10.0, "amount20": 1e8, "rank": 1, "industry": "A"},
                      {"code": "600002", "name": "乙", "close": 333.0, "amount20": 1e8, "rank": 2, "industry": "B"},
                      {"code": "600003", "name": "丙", "close": 5.0, "amount20": 1e8, "rank": 9, "industry": "C"}],
             "target": ["600001", "600002"], "prev_target": ["600001", "600003"]}
    (d / "today.json").write_text(json.dumps(today, ensure_ascii=False), encoding="utf-8")
    plan = c.get("/api/mf/plan", params={"capital": 60000}).json()
    items = {x["code"]: x for x in plan["items"]}
    assert items["600001"]["shares"] == 3000 and items["600001"]["action"] == "继续持有"      # 3 万 / 10 元 = 3000 股
    assert items["600002"]["shares"] == 0 and items["600002"]["too_small"]                   # 一手 3.33 万 > 3 万
    assert [s["code"] for s in plan["sells"]] == ["600003"] and plan["notes"]
    assert all(x["shares"] % 100 == 0 for x in plan["items"])


def test_target_portfolio_keeps_and_caps_industry() -> None:
    from quant_web.multifactor import service
    score = pd.Series(np.arange(300, 0, -1, dtype=float), index=[f"{600000 + i}" for i in range(300)])
    industry = pd.Series({c: ("X" if i < 20 else f"I{i % 30}") for i, c in enumerate(score.index)})
    prev = [score.index[120], score.index[299]]                  # 一个排名 121（留）、一个排名 300（卖）
    tgt = service.target_portfolio(score, prev, industry)
    assert len(tgt) == service.TOP_N and score.index[120] in tgt and score.index[299] not in tgt
    assert sum(1 for c in tgt if industry[c] == "X") == service.INDUSTRY_CAP
