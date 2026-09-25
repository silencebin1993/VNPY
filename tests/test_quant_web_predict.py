"""quant_web 预测核心：情绪、特征（无未来函数）、标签、模型、评分、回测"""
import math
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from quant_web.market import sentiment
from quant_web.predict import features, labels, model, scoring
from quant_web.predict.backtest import run_backtest
from quant_web.predict.limits import add_limit_columns, limit_price, limit_ratio


# ---------------------------------------------------------------- 合成数据

def _bdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d: date = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


CODES: list[str] = [f"600{i:03d}" for i in range(10)] + [f"000{i:03d}" for i in range(10)] + \
    [f"300{i:03d}" for i in range(8)]
ST_CODE: str = "600001"
INDUSTRIES: list[str | None] = ["电子", "计算机", "医药", None]


def _synth_panel(n_days: int = 240, seed: int = 1) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    days: list[date] = _bdays(date(2023, 1, 2), n_days)
    rows: list[dict] = []
    for code in CODES:
        pre: float = round(float(rng.uniform(5, 30)), 2)
        shares: float = float(rng.uniform(5e7, 5e8))
        st: bool = code == ST_CODE
        for d in days:
            if rng.random() < 0.02:          # 停牌
                continue
            r: float = limit_ratio(code, st, d)
            up, dn = limit_price(pre, r), limit_price(pre, -r)
            u: float = float(rng.random())
            if u < 0.08:
                close = up
            elif u < 0.10:
                close = dn
            else:
                close = min(up, max(dn, round(pre * (1 + rng.normal(0, 0.025)), 2)))
            if close == up and rng.random() < 0.3:
                o = h = lo = close
            else:
                o = min(up, max(dn, round(pre * (1 + rng.normal(0, 0.01)), 2)))
                h = min(up, round(max(o, close) * (1 + abs(rng.normal(0, 0.01))), 2))
                lo = max(dn, round(min(o, close) * (1 - abs(rng.normal(0, 0.01))), 2))
                if close < up and rng.random() < 0.05:
                    h = up                  # 炸板
            vol: float = shares * float(rng.uniform(0.005, 0.15))
            rows.append({
                "date": d, "code": code, "open": o, "high": h, "low": lo, "close": close, "preclose": pre,
                "volume": vol, "amount": vol * (o + close) / 2, "turn": round(vol / shares * 100, 2),
                "tradestatus": 1, "is_st": st,
            })
            pre = close
    return pl.DataFrame(rows).with_columns(pl.col("tradestatus").cast(pl.Int8))


def _synth_panel_long(seed: int = 21) -> pl.DataFrame:
    """2019-01 ~ 2023-12 的合成面板（给 service 端到端测试用）"""
    rng = np.random.default_rng(seed)
    days: list[date] = _bdays(date(2019, 1, 2), 1250)
    rows: list[dict] = []
    for code in CODES:
        pre: float = round(float(rng.uniform(5, 30)), 2)
        heat: float = float(rng.uniform(0.03, 0.12))        # 每只股票涨停频率不同，给模型一点可学的东西
        for d in days:
            r: float = limit_ratio(code, code == ST_CODE, d)
            up, dn = limit_price(pre, r), limit_price(pre, -r)
            close: float = up if rng.random() < heat else min(up, max(dn, round(pre * (1 + rng.normal(0, 0.02)), 2)))
            o: float = min(up, max(dn, round(pre * (1 + rng.normal(0, 0.008)), 2)))
            vol: float = float(rng.uniform(1e6, 2e7))
            rows.append({"date": d, "code": code, "open": o, "high": max(o, close), "low": min(o, close),
                         "close": close, "preclose": pre, "volume": vol, "amount": vol * close,
                         "turn": round(vol / 1e8 * 100, 2), "tradestatus": 1, "is_st": code == ST_CODE, "source": "tx"})
            pre = close
    return pl.DataFrame(rows).with_columns(pl.col("tradestatus").cast(pl.Int8))


def _universe() -> pl.DataFrame:
    return pl.DataFrame({
        "code": CODES,
        "name": [("*ST测试" if c == ST_CODE else f"测试{c}") for c in CODES],
        "exchange": ["SSE" if c.startswith("6") else "SZSE" for c in CODES],
        "board": ["chinext" if c.startswith("300") else "main" for c in CODES],
        "list_date": [date(2015, 1, 5)] * len(CODES),
        "delist_date": pl.Series([None] * len(CODES), dtype=pl.Date),
        "is_st": [c == ST_CODE for c in CODES],
        "industry": [INDUSTRIES[i % len(INDUSTRIES)] for i in range(len(CODES))],
        "status": pl.Series([1] * len(CODES), dtype=pl.Int8),
    })


def _fund(seed: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    reports: list[tuple[date, date]] = [
        (date(2022, 9, 30), date(2022, 11, 1)), (date(2022, 12, 31), date(2023, 5, 1)),
        (date(2023, 3, 31), date(2023, 5, 1)), (date(2023, 6, 30), date(2023, 9, 1)),
        (date(2023, 9, 30), date(2023, 11, 1)),
    ]
    rows: list[dict] = []
    for code in CODES:
        for rd, ad in reports:
            rows.append({
                "code": code, "report_date": rd, "avail_date": ad, "eps": float(rng.normal(0.3, 0.3)),
                "eps_ttm": float(rng.normal(0.5, 0.5)), "bvps": float(rng.uniform(1, 10)),
                "roe": float(rng.normal(5, 3)), "revenue_yoy": float(rng.normal(10, 20)),
                "net_profit_yoy": float(rng.normal(10, 40)), "debt_ratio": float(rng.uniform(10, 80)),
                "gross_margin": float(rng.uniform(5, 60)),
            })
    return pl.DataFrame(rows)


def _lhb(panel_lim: pl.DataFrame, seed: int = 4) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    lu: pl.DataFrame = panel_lim.filter(pl.col("is_limit_up")).select("date", "code")
    return lu.with_columns(pl.Series("net_buy", rng.normal(1e7, 3e7, lu.height)))


def _zt(panel_lim: pl.DataFrame, seed: int = 5) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    lu: pl.DataFrame = panel_lim.filter(pl.col("is_limit_up")).select("date", "code")
    n: int = lu.height
    times: list[str] = [f"{9 + int(m) // 60:02d}:{int(m) % 60:02d}:00" for m in rng.uniform(30, 300, n)]
    return lu.with_columns(
        pl.Series("first_time", times), pl.Series("open_times", rng.integers(0, 4, n)),
        pl.Series("seal_amount", rng.uniform(1e6, 1e8, n)),
    )


def _pipeline(panel: pl.DataFrame, fund: pl.DataFrame, lhb: pl.DataFrame | None, zt: pl.DataFrame | None
              ) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, pl.DataFrame]]:
    uni: pl.DataFrame = _universe()
    panel_lim: pl.DataFrame = add_limit_columns(panel, uni)
    sent: pl.DataFrame = sentiment.daily_sentiment(panel_lim)
    lhb_df: pl.DataFrame = _lhb(panel_lim) if lhb is None else lhb
    zt_df: pl.DataFrame = _zt(panel_lim) if zt is None else zt
    out: dict[str, pl.DataFrame] = {
        kind: features.build_features(panel_lim, uni, sent, kind, fund=fund, lhb=lhb_df, zt_pool=zt_df)
        for kind in ("streak", "first")
    }
    return panel_lim, sent, out


@pytest.fixture(scope="module")
def synth() -> dict:
    panel: pl.DataFrame = _synth_panel()
    fund: pl.DataFrame = _fund()
    panel_lim, sent, feats = _pipeline(panel, fund, None, None)
    return {"panel": panel, "fund": fund, "panel_lim": panel_lim, "sent": sent, "feats": feats,
            "lhb": _lhb(panel_lim), "zt": _zt(panel_lim)}


# ---------------------------------------------------------------- 情绪

def test_daily_sentiment_basic(synth: dict) -> None:
    pl_: pl.DataFrame = synth["panel_lim"]
    sent: pl.DataFrame = synth["sent"]
    assert sent.columns == list(sentiment.SCHEMA)
    day: date = sent["date"][100]
    row: dict = sent.filter(pl.col("date") == day).row(0, named=True)
    today: pl.DataFrame = pl_.filter((pl.col("date") == day) & ~pl.col("no_limit"))
    assert row["n_stocks"] == today.height
    assert row["n_limit_up"] == int(today["is_limit_up"].sum())
    assert row["max_streak"] == int(today["streak"].max())
    # 昨日涨停股今日平均涨幅（%）：上一交易日涨停且今天有行情
    prev_day: date = sent["date"][99]
    prev_lu: list[str] = pl_.filter((pl.col("date") == prev_day) & pl.col("is_limit_up"))["code"].to_list()
    sel: pl.DataFrame = today.filter(pl.col("code").is_in(prev_lu))
    if sel.height:
        assert row["prev_lu_premium"] == pytest.approx(float(sel["pct"].mean()) * 100)
        assert row["prev_lu_promote"] == pytest.approx(float(sel["is_limit_up"].mean()))
    temps = sent["temperature"].drop_nulls()
    assert temps.min() >= 0 and temps.max() <= 100
    assert sentiment.temperature_label(10) == "冰点" and sentiment.temperature_label(85) == "过热"
    assert sentiment.temperature_label(None) == "未知"


# ---------------------------------------------------------------- 特征：无未来函数

def _mutate_after(df: pl.DataFrame, cut: date, cols: list[str], seed: int, lo: float = 0.5, hi: float = 50.0
                  ) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    after = pl.col("date") > cut
    n: int = df.height
    return df.with_columns([
        pl.when(after).then(pl.Series(c, rng.uniform(lo, hi, n))).otherwise(pl.col(c)).alias(c) for c in cols
    ])


def test_features_have_no_lookahead(synth: dict) -> None:
    panel: pl.DataFrame = synth["panel"]
    days: list[date] = sorted(panel["date"].unique().to_list())
    cut: date = days[150]
    # 截止日之后的行情全部随机化（价格、成交量、昨收都乱掉），再多加一份截止日后的财报/龙虎榜/涨停池
    mutated: pl.DataFrame = _mutate_after(panel, cut, ["open", "high", "low", "close", "preclose"], 11, 1, 60)
    mutated = _mutate_after(mutated, cut, ["volume", "amount"], 12, 1e5, 1e9)
    mutated = _mutate_after(mutated, cut, ["turn"], 13, 0.1, 40)
    mutated = mutated.with_columns(
        pl.max_horizontal("open", "high", "low", "close").alias("high"),
        pl.min_horizontal("open", "high", "low", "close").alias("low"),
    )
    fund2: pl.DataFrame = _mutate_after(synth["fund"].rename({"avail_date": "date"}), cut,
                                        ["eps_ttm", "bvps", "roe", "revenue_yoy"], 14, -5, 5).rename(
        {"date": "avail_date"})
    lhb2: pl.DataFrame = _mutate_after(synth["lhb"], cut, ["net_buy"], 15, -1e8, 1e8)
    zt2: pl.DataFrame = _mutate_after(synth["zt"], cut, ["seal_amount"], 16, 1, 1e9)
    extra_lhb: pl.DataFrame = mutated.filter(pl.col("date") > cut).select("date", "code").head(30).with_columns(
        pl.lit(5e7).alias("net_buy"))
    lhb2 = pl.concat([lhb2, extra_lhb], how="vertical_relaxed")
    _, sent2, feats2 = _pipeline(mutated, fund2, lhb2, zt2)

    before = pl.col("date") <= cut
    s1, s2 = synth["sent"].filter(before), sent2.filter(before)
    assert s1.equals(s2)
    for kind in ("streak", "first"):
        f1: pl.DataFrame = synth["feats"][kind].filter(before)
        f2: pl.DataFrame = feats2[kind].filter(before)
        assert f1.height > 50
        assert f1.select(["date", "code"]).equals(f2.select(["date", "code"]))
        for col in f1.columns:
            assert f1[col].equals(f2[col]), f"{kind}.{col} 在截止日之前的值被未来数据改变了"
        # 截止日之后确实变了（说明测试有效）
        assert not synth["feats"][kind].filter(~before).equals(feats2[kind].filter(~before))


def test_feature_schema_and_candidates(synth: dict) -> None:
    streak: pl.DataFrame = synth["feats"]["streak"]
    first: pl.DataFrame = synth["feats"]["first"]
    for f in features.ALL_FEATURES:
        assert streak.schema[f] == pl.Float32
    for c in ["close", "pct", "streak", "turn", "float_cap", "is_st", "board", "one_word"]:
        assert c in streak.columns
    assert (streak["streak"] >= 1).all()
    assert (first["streak"] == 0).all()
    # 首板候选：近5日没涨停
    pl_: pl.DataFrame = synth["panel_lim"].sort(["code", "date"]).with_columns(
        pl.col("is_limit_up").cast(pl.Int32).rolling_sum(5, min_samples=1).over("code").alias("lu5"))
    j: pl.DataFrame = first.join(pl_.select("date", "code", "lu5"), on=["date", "code"])
    assert (j["lu5"] == 0).all()
    # 每个特征只属于一个组，且都有中文说明
    names: list[str] = [f for g in features.GROUPS for f in features.FEATURE_GROUPS[g]]
    assert len(names) == len(set(names))
    assert all(f in features.FEATURE_LABELS for f in names)
    # 龙虎榜、涨停池特征在有数据的连板候选上不为空
    assert streak["lhb_on"].drop_nulls().sum() > 0
    assert streak["zt_first_min"].null_count() < streak.height


def test_negative_sampling_keeps_all_positives(synth: dict) -> None:
    uni: pl.DataFrame = _universe()
    pl_: pl.DataFrame = synth["panel_lim"]
    full: pl.DataFrame = labels.add_labels(synth["feats"]["first"], pl_)
    kw = {"fund": synth["fund"], "lhb": synth["lhb"], "zt_pool": synth["zt"]}
    samp: pl.DataFrame = labels.add_labels(
        features.build_features(pl_, uni, synth["sent"], "first", neg_sample=0.2, **kw), pl_)
    assert int(samp["y"].sum()) == int(full["y"].sum())
    neg_ratio: float = (samp["y"] == 0).sum() / (full["y"] == 0).sum()
    assert 0.12 < neg_ratio < 0.3
    again: pl.DataFrame = features.build_features(pl_, uni, synth["sent"], "first", neg_sample=0.2, **kw)
    assert again.select("date", "code").equals(samp.select("date", "code"))       # 确定性抽样


def test_tail_panel_gives_same_latest_features(synth: dict) -> None:
    """只用最近 130 个交易日算最新一天的特征，应与用全部历史算的一致（预测时就是这么做的）"""
    pl_: pl.DataFrame = synth["panel_lim"]
    days: list[date] = sorted(pl_["date"].unique().to_list())
    last: date = days[-1]
    uni: pl.DataFrame = _universe()
    kw = {"fund": synth["fund"], "lhb": synth["lhb"], "zt_pool": synth["zt"]}
    for kind in ("streak", "first"):
        full: pl.DataFrame = synth["feats"][kind].filter(pl.col("date") == last)
        tail: pl.DataFrame = features.build_features(pl_.filter(pl.col("date") >= days[-130]), uni, synth["sent"],
                                                     kind, dates=[last], **kw)
        assert full.select("code").equals(tail.select("code"))
        for f in features.ALL_FEATURES:
            a, b = full[f].to_numpy(), tail[f].to_numpy()
            assert np.allclose(a, b, rtol=1e-4, atol=1e-6, equal_nan=True), f"{kind}.{f}"


# ---------------------------------------------------------------- 标签

def test_labels_align_to_next_row_of_same_code() -> None:
    days: list[date] = _bdays(date(2024, 3, 1), 12)
    # A：连续交易；B：第3天后停牌 3 周再复牌（>10 自然日 → 标签为空）；C：只有一天
    rows: list[dict] = []
    pre: float = 10.0
    for i, d in enumerate(days):
        close: float = limit_price(pre, 0.1) if i in (2, 3) else round(pre * 1.01, 2)
        rows.append({"date": d, "code": "600100", "open": pre, "high": close, "low": pre, "close": close,
                     "preclose": pre})
        pre = close
    b_days: list[date] = days[:3] + [days[2] + timedelta(days=21)]
    pre = 20.0
    for d in b_days:
        rows.append({"date": d, "code": "600200", "open": pre, "high": pre, "low": pre, "close": pre,
                     "preclose": pre})
    rows.append({"date": days[5], "code": "600300", "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0,
                 "preclose": 5.0})
    panel: pl.DataFrame = pl.DataFrame(rows).with_columns(pl.lit(1e6).alias("volume"))
    uni: pl.DataFrame = pl.DataFrame({"code": ["600100", "600200", "600300"], "list_date": [date(2010, 1, 4)] * 3,
                                      "status": pl.Series([1, 1, 1], dtype=pl.Int8)})
    pl_: pl.DataFrame = add_limit_columns(panel, uni)
    feat: pl.DataFrame = pl_.select("date", "code").sort(["date", "code"])
    lab: pl.DataFrame = labels.add_labels(feat, pl_)
    assert lab.select("date", "code").equals(feat)          # 行顺序不变

    a: pl.DataFrame = lab.filter(pl.col("code") == "600100").sort("date")
    assert a["next_date"].to_list()[:-1] == days[1:]
    assert a["y"].to_list() == [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, None]
    assert a["next_close"][1] == limit_price(round(10.0 * 1.01 * 1.01, 2), 0.1)
    assert a["next_limit_up"][1] == a["next_close"][1]
    b: pl.DataFrame = lab.filter(pl.col("code") == "600200").sort("date")
    assert b["next_date"].to_list() == [b_days[1], b_days[2], b_days[3], None]
    assert b["y"].to_list()[:2] == [0, 0]
    assert b["y"][2] is None and b["y"][3] is None           # 停牌超过 10 天 / 没有下一天
    c: pl.DataFrame = lab.filter(pl.col("code") == "600300")
    assert c["y"][0] is None and c["next_date"][0] is None


# ---------------------------------------------------------------- 模型

def test_make_folds() -> None:
    days: list[date] = _bdays(date(2020, 1, 2), 1300)
    folds: list[model.Fold] = model.make_folds(days, 2022, 6)
    assert folds[0].test_start == date(2022, 1, 3) and folds[0].train_end == date(2021, 12, 31)
    assert folds[1].test_start.month == 7 and folds[0].test_end.month == 6
    for f in folds:
        assert f.train_end < f.test_start <= f.test_end
    assert folds[-1].test_end == days[-1]


def _toy_dataset(n_days: int = 700, per_day: int = 40, seed: int = 9) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    days: list[date] = _bdays(date(2020, 1, 2), n_days)
    n: int = n_days * per_day
    data: dict = {"date": [d for d in days for _ in range(per_day)], "code": [f"{i:06d}" for i in range(n)]}
    for f in features.ALL_FEATURES:
        data[f] = rng.normal(0, 1, n).astype(np.float32)
    logit = -1.5 + 1.2 * data["turnover"] - 0.8 * data["m_break_rate"] + 0.6 * data["ind_lu"]
    data["y"] = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(np.int8)
    return pl.DataFrame(data)


def test_walk_forward_and_contributions(tmp_path, monkeypatch) -> None:
    ds: pl.DataFrame = _toy_dataset()
    cols: list[str] = features.feature_columns("streak")
    folds: list[model.Fold] = model.make_folds(ds["date"].unique().to_list(), 2021, 6)
    msgs: list[str] = []
    oos, metrics = model.train_walk_forward(ds, cols, folds, progress=lambda f, m: msgs.append(m))
    assert oos.height > 0 and len(metrics) == len(folds)
    assert oos["date"].min() >= folds[0].test_start
    for m in metrics:
        assert m["auc"] > 0.65
    # prob = sigmoid(bias + Σ contrib)
    logit = oos["bias"].to_numpy() + sum(oos[c].to_numpy() for c in model.CONTRIB_COLUMNS)
    assert np.allclose(1 / (1 + np.exp(-logit)), oos["prob"].to_numpy(), atol=1e-6)
    ev: dict = model.evaluate(oos)
    assert ev["top1_hit"] > ev["base_rate"]
    assert "第 1/" in msgs[0]

    booster = model.train_final(ds, cols)
    imp, top = model.importance(booster, cols)
    assert abs(sum(imp.values()) - 1) < 0.01
    assert imp["capital"] > imp["fundamental"]          # 信号放在 turnover（资金面）上
    assert top[0]["feature"] in ("turnover", "m_break_rate", "ind_lu")

    last: pl.DataFrame = ds.filter(pl.col("date") == ds["date"].max())
    pred: pl.DataFrame = model.predict(booster, last, cols, offset=math.log(0.05))
    logit2 = pred["bias"].to_numpy() + sum(pred[c].to_numpy() for c in model.CONTRIB_COLUMNS)
    assert np.allclose(1 / (1 + np.exp(-logit2)), pred["prob"].to_numpy(), atol=1e-6)
    r0: list[dict] = pred["reasons"][0].to_list()
    assert 1 <= len(r0) <= 5 and {"feature", "label", "value", "contrib"} <= set(r0[0])
    assert abs(r0[0]["contrib"]) >= abs(r0[-1]["contrib"])

    # 保存/加载：对数几率偏移随 meta 恢复
    monkeypatch.setattr(model.config, "MODEL_DIR", tmp_path)
    model.save("first", booster, {"kind": "first", "logit_offset": math.log(0.05)}, oos)
    booster2, meta = model.load("first")
    assert meta["kind"] == "first"
    p2: pl.DataFrame = model.predict_frame(booster2, last, cols)
    assert np.allclose(p2["prob"].to_numpy(), pred["prob"].to_numpy(), atol=1e-9)
    assert model.load_oos("first").height == oos.height
    # contrib_top：只给每天前 N 名算贡献
    p3: pl.DataFrame = model.predict_frame(booster2, ds.filter(pl.col("date") >= folds[-1].test_start), cols,
                                           contrib_top=3)
    per_day = p3.filter(pl.col("bias").is_not_null()).group_by("date").len()["len"]
    assert per_day.max() == 3


# ---------------------------------------------------------------- 评分

def _pred_rows() -> pl.DataFrame:
    return pl.DataFrame({
        "date": [date(2024, 5, 6)] * 4, "code": ["600001", "300002", "688003", "000004"],
        "bias": [-1.0, -1.0, -1.0, None], "contrib_sentiment": [0.5, -0.2, 0.0, None],
        "contrib_capital": [0.3, 0.4, -0.5, None], "contrib_fundamental": [0.0, 0.1, 0.2, None],
        "contrib_theme": [0.2, 0.0, 0.1, None], "contrib_technical": [-0.1, 0.3, 0.0, None],
        "prob": [0.0, 0.0, 0.0, 0.2], "close": [5.0, 30.0, 150.0, 1.5], "float_cap": [20.0, 80.0, None, 600.0],
        "board": ["main", "chinext", "star", "main"], "is_st": [True, False, False, False],
        "streak": pl.Series([2, 1, 3, 1], dtype=pl.Int16), "one_word": [False, True, False, False],
    }).with_columns(
        # prob 与贡献一致
        pl.when(pl.col("bias").is_not_null()).then(
            1 / (1 + (-(pl.col("bias") + pl.sum_horizontal(pl.col("^contrib_.*$")))).exp())
        ).otherwise(pl.col("prob")).alias("prob")
    )


def test_apply_weights() -> None:
    df: pl.DataFrame = _pred_rows()
    ones: pl.DataFrame = scoring.apply_weights(df, {g: 1.0 for g in features.GROUPS})
    assert np.allclose(ones["score"].to_numpy(), df["prob"].to_numpy(), atol=1e-9)
    zero_cap: pl.DataFrame = scoring.apply_weights(df, {"capital": 0.0})
    manual = 1 / (1 + math.exp(-(-1.0 + 0.5 + 0.0 + 0.0 + 0.2 - 0.1)))
    assert zero_cap["score"][0] == pytest.approx(manual)
    assert zero_cap["score"][3] == pytest.approx(0.2)            # 没有贡献分解的行用原始概率
    assert ones["dim_sentiment"][0] == pytest.approx(50 + 50 * math.tanh(0.5))
    assert ones["dim_news"].to_list() == [50.0] * 4
    news: pl.DataFrame = pl.DataFrame({"code": ["600001"], "news_count": [3], "policy_count": [1], "news_z": [2.0]})
    wn: pl.DataFrame = scoring.apply_weights(df, None, news, 0.5)
    assert wn["score_logit"][0] == pytest.approx(ones["score_logit"][0] + 1.0)
    assert wn["dim_news"][0] == pytest.approx(50 + 50 * math.tanh(1.0)) and wn["dim_news"][1] == 50
    assert wn["news_count"][0] == 3


def test_filter_candidates() -> None:
    df: pl.DataFrame = scoring.apply_weights(_pred_rows(), None)
    base: dict = {"kind": "streak", "exclude_st": True, "boards": ["main", "chinext", "star"], "min_float_cap": 0,
                  "max_float_cap": 500, "min_price": 2, "max_price": 200, "streak_min": 1, "streak_max": 10,
                  "exclude_one_word": False, "threshold": 0.0}
    assert scoring.filter_candidates(df, base)["code"].to_list() == ["300002", "688003"]
    assert scoring.filter_candidates(df, {**base, "exclude_st": False})["code"].to_list() == \
        ["600001", "300002", "688003"]
    assert scoring.filter_candidates(df, {**base, "streak_min": 2})["code"].to_list() == ["688003"]
    assert scoring.filter_candidates(df, {**base, "exclude_one_word": True})["code"].to_list() == ["688003"]
    assert scoring.filter_candidates(df, {**base, "boards": ["chinext"]})["code"].to_list() == ["300002"]
    thr: float = float(df["score"][1]) + 1e-9
    assert scoring.filter_candidates(df, {**base, "threshold": thr})["code"].to_list() == \
        [c for c, s in zip(["300002", "688003"], df["score"][1:3], strict=True) if s >= thr]
    # 首板模型不按连板数过滤；也接受属性对象（PredictSettings 的替身）
    first = SimpleNamespace(**{**base, "kind": "first", "streak_min": 5})
    assert scoring.filter_candidates(df, first)["code"].to_list() == ["300002", "688003"]


def test_reasons_text() -> None:
    reasons: list[dict] = [
        {"feature": "lb_streak", "label": "连板数", "value": 2.0, "contrib": 0.4},
        {"feature": "turnover", "label": "换手率", "value": 18.5, "contrib": 0.2},
        {"feature": "ind_lu", "label": "所在行业今天涨停家数", "value": 6.0, "contrib": 0.1},
        {"feature": "m_limit_up", "label": "今天全市场涨停家数", "value": 23.0, "contrib": -0.3},
        {"feature": "pe_ttm", "label": "市盈率(TTM)", "value": None, "contrib": -0.05},
    ]
    text: list[str] = scoring.reasons_text(reasons)
    assert text[0] == "今天是第2个涨停（2连板） ↑提高概率"
    assert text[1] == "换手率 18.5%，筹码交换充分 ↑提高概率"
    assert text[2] == "所在行业今天有6只涨停，板块热度高 ↑提高概率"
    assert text[3] == "市场情绪偏冷（涨停仅23家） ↓降低概率"
    assert text[4] == "市盈率(TTM)：暂无数据 ↓降低概率"
    # 每个特征都能生成白话（不抛异常）
    for f in features.ALL_FEATURES:
        for v in (0.0, 0.5, 1.0, 3.0, -0.2, 80.0):
            assert scoring.describe(f, v)


# ---------------------------------------------------------------- 回测

D: list[date] = _bdays(date(2024, 1, 2), 12)


def _stock(code: str, bars: list[tuple[float, float, float, float]], start: int = 0, pre: float = 10.0,
           ratio: float = 0.1) -> list[dict]:
    """bars: (open, high, low, close)，从 D[start] 起逐日；涨跌停价按昨收计算"""
    rows: list[dict] = []
    for i, (o, h, lo, c) in enumerate(bars):
        rows.append({"date": D[start + i], "code": code, "open": o, "high": h, "low": lo, "close": c,
                     "preclose": pre, "limit_up": limit_price(pre, ratio), "limit_down": limit_price(pre, -ratio)})
        pre = c
    return rows


def _filler() -> list[dict]:
    """让日历覆盖全部 D 的一只无关股票"""
    return _stock("999999", [(10.0, 10.0, 10.0, 10.0)] * len(D))


def _oos(signals: list[tuple[date, str, float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "date": [s[0] for s in signals], "code": [s[1] for s in signals], "prob": [s[2] for s in signals],
        "bias": pl.Series([None] * len(signals), dtype=pl.Float64),
        "y": pl.Series([1] * len(signals), dtype=pl.Int8), "close": [10.0] * len(signals),
        "float_cap": [50.0] * len(signals), "board": ["main"] * len(signals), "is_st": [False] * len(signals),
        "streak": pl.Series([1] * len(signals), dtype=pl.Int16), "one_word": [False] * len(signals),
        "name": [f"股{s[1]}" for s in signals],
    })


PS = SimpleNamespace(kind="streak", weights={g: 1.0 for g in features.GROUPS}, news_weight=0.0, exclude_st=False,
                     boards=["main", "chinext", "star", "bj"], min_float_cap=0, max_float_cap=1e9, min_price=0,
                     max_price=1e9, streak_min=0, streak_max=99, exclude_one_word=False, threshold=0.0, top_n=5)


def _ts(**kw) -> SimpleNamespace:
    base: dict = {"capital": 100_000, "position_pct": 0.1, "max_positions": 5, "max_gap_pct": 7.0,
                  "exit_rule": "next_close", "stop_loss_pct": 0.0, "fee_rate": 0.0003, "stamp_duty": 0.0005,
                  "slippage": 0.0}
    return SimpleNamespace(**{**base, **kw})


def _bt(rows: list[dict], signals: list[tuple[date, str, float]], **kw) -> dict:
    panel: pl.DataFrame = pl.DataFrame(rows + _filler())
    return run_backtest(_oos(signals), panel, PS, _ts(**kw))


def test_backtest_fee_math_and_next_close() -> None:
    rows = _stock("600010", [(10.0, 10.2, 9.9, 10.0), (10.0, 10.6, 9.9, 10.5), (10.6, 10.9, 10.5, 10.8)])
    r: dict = _bt(rows, [(D[0], "600010", 0.5)])
    assert r["metrics"]["trades"] == 1
    tr: dict = r["trades"][0]
    assert tr["entry_date"] == D[1].isoformat() and tr["exit_date"] == D[2].isoformat()
    assert tr["entry"] == 10.0 and tr["exit"] == 10.8 and tr["hold_days"] == 1
    # 买 1000 股（10万×10%÷10元），佣金 3 元 → 最低 5 元；卖 10800：佣金 5 元 + 印花税 5.4 元
    cost, net = 10_000 + 5.0, 10_800 - 5.0 - 5.4
    assert tr["ret"] == pytest.approx(net / cost - 1, abs=1e-5)
    assert r["metrics"]["final_equity"] == pytest.approx(100_000 + net - cost, abs=0.01)
    assert r["metrics"]["total_fees"] == pytest.approx(15.4)
    assert r["metrics"]["hit_rate"] == 1.0 and r["metrics"]["base_rate"] == 1.0
    assert tr["name"] == "股600010"
    # 滑点：买价上浮、卖价下浮
    r2: dict = _bt(rows, [(D[0], "600010", 0.5)], slippage=0.001)
    assert r2["trades"][0]["entry"] == pytest.approx(10.01) and r2["trades"][0]["exit"] == pytest.approx(10.7892, abs=1e-3)


def test_backtest_next_open_exit() -> None:
    rows = _stock("600010", [(10.0, 10.2, 9.9, 10.0), (10.0, 10.6, 9.9, 10.5), (10.6, 10.9, 10.5, 10.8)])
    tr: dict = _bt(rows, [(D[0], "600010", 0.5)], exit_rule="next_open")["trades"][0]
    assert tr["exit_date"] == D[2].isoformat() and tr["exit"] == 10.6 and tr["hold_days"] == 1


def test_backtest_unfillable_and_gap() -> None:
    lu = _stock("600020", [(10.0, 10.0, 10.0, 10.0), (11.0, 11.0, 11.0, 11.0), (11.5, 12.1, 11.2, 11.5)])
    gap = _stock("600030", [(10.0, 10.0, 10.0, 10.0), (10.8, 10.9, 10.5, 10.6), (10.6, 10.7, 10.4, 10.5)])
    ok = _stock("600040", [(10.0, 10.0, 10.0, 10.0), (10.5, 10.9, 10.4, 10.6), (10.6, 10.7, 10.4, 10.5)])
    r: dict = _bt(lu + gap + ok, [(D[0], "600020", 0.9), (D[0], "600030", 0.8), (D[0], "600040", 0.7)])
    m: dict = r["metrics"]
    assert m["unfilled"] == 1 and m["skipped_gap"] == 1 and m["trades"] == 1
    assert r["trades"][0]["code"] == "600040"
    # 放宽开盘涨幅上限后可以买
    assert _bt(lu + gap + ok, [(D[0], "600030", 0.8)], max_gap_pct=9.0)["metrics"]["trades"] == 1


def test_backtest_until_break() -> None:
    # D1 买入收涨停 → D2 继续涨停 → D3 收盘没涨停（断板）→ D4 开盘卖
    rows = _stock("600050", [(10.0, 10.0, 10.0, 10.0), (10.2, 11.0, 10.1, 11.0), (11.5, 12.1, 11.4, 12.1),
                             (12.5, 13.0, 12.2, 12.5), (12.0, 12.3, 11.8, 12.1)])
    tr: dict = _bt(rows, [(D[0], "600050", 0.5)], exit_rule="until_break")["trades"][0]
    assert tr["entry_date"] == D[1].isoformat() and tr["exit_date"] == D[4].isoformat()
    assert tr["exit"] == 12.0 and tr["hold_days"] == 3 and "断板" in tr["reason"]
    # 一直涨停：最多持有 10 天
    up: float = limit_price(10.0, 0.1)
    bars: list[tuple[float, float, float, float]] = [(10.0, 10.0, 10.0, 10.0), (10.1, up, 10.0, up)]
    for _ in range(10):                  # 之后一直一字涨停
        up = limit_price(up, 0.1)
        bars.append((up, up, up, up))
    tr2: dict = _bt(_stock("600060", bars), [(D[0], "600060", 0.5)], exit_rule="until_break")["trades"][0]
    assert tr2["hold_days"] == 10 and tr2["exit_date"] == D[11].isoformat() and "满10天" in tr2["reason"]


def test_backtest_stop_loss_open_and_intraday() -> None:
    # 买入当天最低 9.4 也不能止损（T+1）；第二天开盘 9.3 ≤ 止损价 9.5 → 开盘价止损
    at_open = _stock("600070", [(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.4, 9.6), (9.3, 9.7, 9.2, 9.6)])
    tr: dict = _bt(at_open, [(D[0], "600070", 0.5)], stop_loss_pct=0.05)["trades"][0]
    assert tr["exit_date"] == D[2].isoformat() and tr["exit"] == 9.3 and "开盘止损" in tr["reason"]
    # 第二天开盘 9.8，盘中最低 9.4 → 按止损价 9.5 卖
    intraday = _stock("600080", [(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.9, 10.0), (9.8, 9.9, 9.4, 9.6)])
    tr2: dict = _bt(intraday, [(D[0], "600080", 0.5)], stop_loss_pct=0.05)["trades"][0]
    assert tr2["exit"] == pytest.approx(9.5) and "盘中" in tr2["reason"]
    # 不止损时按规则收盘卖
    tr3: dict = _bt(intraday, [(D[0], "600080", 0.5)])["trades"][0]
    assert tr3["exit"] == 9.6


def test_backtest_limit_down_lock_rolls_over() -> None:
    # D2 一字跌停：收盘卖不出 → D3 开盘卖
    rows = _stock("600090", [(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.7, 9.8), (8.82, 8.82, 8.82, 8.82),
                             (8.3, 8.6, 8.1, 8.4)])
    tr: dict = _bt(rows, [(D[0], "600090", 0.5)])["trades"][0]
    assert tr["exit_date"] == D[3].isoformat() and tr["exit"] == 8.3 and "顺延" in tr["reason"]
    # next_open：D2 开盘跌停卖不出 → D3 开盘卖
    tr2: dict = _bt(rows, [(D[0], "600090", 0.5)], exit_rule="next_open")["trades"][0]
    assert tr2["exit_date"] == D[3].isoformat() and "顺延" in tr2["reason"]
    # 止损遇一字跌停也顺延
    tr3: dict = _bt(rows, [(D[0], "600090", 0.5)], stop_loss_pct=0.05, exit_rule="until_break")["trades"][0]
    assert tr3["exit_date"] == D[3].isoformat()


def test_backtest_positions_cash_and_report_shape() -> None:
    rows: list[dict] = []
    signals: list[tuple[date, str, float]] = []
    for k in range(4):
        code: str = f"60011{k}"
        rows += _stock(code, [(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.2), (10.2, 10.4, 10.0, 10.1)])
        signals.append((D[0], code, 0.9 - k * 0.1))
    r: dict = _bt(rows, signals, max_positions=2, position_pct=0.3)
    m: dict = r["metrics"]
    assert m["trades"] == 2 and m["skipped_full"] == 2
    assert sorted(t["code"] for t in r["trades"]) == ["600110", "600111"]          # 按分数高低优先
    assert {"metrics", "equity", "drawdown", "yearly", "calibration", "topn_hit", "trades"} <= set(r)
    for key in ("trades", "win_rate", "avg_return", "median_return", "profit_factor", "total_return", "cagr",
                "max_drawdown", "hit_rate", "base_rate", "unfilled", "avg_hold_days", "sharpe"):
        assert key in m
    assert r["equity"][0]["value"] <= 100_000 and r["drawdown"][0]["value"] <= 0
    assert r["yearly"][0]["year"] == 2024 and r["yearly"][0]["trades"] == 2
    assert [x["n"] for x in r["topn_hit"]] == [1, 3, 5, 10]
    # 每只买入 = 总资产×30% = 30000 → 3000 股
    assert all(t["pnl"] == pytest.approx(3000 * 0.1 - 9.0 - 30300 * 0.0003 - 30300 * 0.0005, abs=0.01)
               for t in r["trades"])


# ---------------------------------------------------------------- 编排（合成工作区）

def test_service_end_to_end(tmp_path, monkeypatch) -> None:
    from quant_web import config
    from quant_web.predict import service

    lab = tmp_path.joinpath("stock_lab")
    daily_dir = lab.joinpath("daily")
    daily_dir.mkdir(parents=True)
    panel: pl.DataFrame = _synth_panel_long()
    for (year,), part in panel.with_columns(pl.col("date").dt.year().alias("_y")).partition_by(
            "_y", as_dict=True).items():
        part.drop("_y").write_parquet(daily_dir.joinpath(f"{year}.parquet"))
    _universe().write_parquet(lab.joinpath("universe.parquet"))
    for name, value in {"WORKSPACE": tmp_path, "STOCK_LAB": lab, "PANEL_DIR": daily_dir,
                        "UNIVERSE_FILE": lab.joinpath("universe.parquet"), "MODEL_DIR": tmp_path.joinpath("models"),
                        "SETTINGS_FILE": tmp_path.joinpath("settings.json"), "POOLS_DIR": lab.joinpath("pools"),
                        "CACHE_DIR": tmp_path.joinpath("cache")}.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(service, "_news_heat", lambda codes: (None, "消息面数据暂时拿不到，消息面评分按中性(50)处理"))
    service.invalidate()

    for kind in ("streak", "first"):
        steps: list[tuple[float, str]] = []
        r: dict = service.train(kind, lambda f, m, steps=steps: steps.append((f, m)))
        assert r["folds"] >= 3 and r["oos"]["auc"] is not None
        fracs: list[float] = [f for f, _ in steps]
        assert fracs == sorted(fracs) and fracs[-1] == 1.0 and "训练完成" in steps[-1][1]
        meta: dict = service.models_meta(kind)
        for key in ("kind", "trained_at", "data_start", "data_end", "n_samples", "base_rate", "feature_cols", "folds",
                    "calibration", "importance"):
            assert key in meta
        assert meta["data_end"] == panel["date"].max().isoformat()
        assert meta["oos_start"] >= "2022-01-01"
        assert model.paths(kind)["oos"].exists() and model.paths(kind)["model"].exists()
    assert model.paths("first")["daily"].exists() and not model.paths("streak")["daily"].exists()
    assert service.models_meta("first")["logit_offset"] == pytest.approx(math.log(service.NEG_SAMPLE))

    settings = SimpleNamespace(predict=SimpleNamespace(**{**vars(PS), "exclude_st": True, "top_n": 2}), trade=_ts())
    for kind in ("streak", "first"):
        res: dict = service.predict_latest(kind, settings)
        assert res["signal_date"] == panel["date"].max().isoformat()
        assert res["model"]["base_rate"] is not None and any("消息面" in w for w in res["warnings"])
        rows: list[dict] = res["rows"]
        assert rows and res["count"] >= len(rows)
        assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
        assert [r["pick"] for r in rows[:3]] == [True, True, False][:len(rows)]
        assert all(r["code"] != ST_CODE for r in rows)
        for key in ("code", "name", "industry", "board", "close", "pct", "streak", "turn", "float_cap", "one_word",
                    "prob", "score", "dims", "reasons", "news_count", "policy_count", "pick"):
            assert key in rows[0]
        assert set(rows[0]["dims"]) == {*features.GROUPS, "news"} and rows[0]["dims"]["news"] == 50
        assert all(0 <= v <= 100 for v in rows[0]["dims"].values())
        assert rows[0]["reasons"] and ("↑" in rows[0]["reasons"][0] or "↓" in rows[0]["reasons"][0])
        if kind == "streak":
            assert all(r["streak"] >= 1 for r in rows) and abs(rows[0]["pct"]) > 4       # pct 单位是 %
        bt: dict = service.backtest(kind, settings)
        assert bt["metrics"]["signals"] > 0 and bt["settings_used"]["predict"]["kind"] == kind
        assert bt["metrics"]["base_rate"] == pytest.approx(service.models_meta(kind)["base_rate"], abs=0.02)

    st: dict = service.dataset_status()
    assert st["panel"]["rows"] == panel.height and st["panel"]["stocks"] == len(CODES)
    assert st["models"]["streak"]["stale"] is False

    # 个股页的预测：名单内给出排名；被筛选条件挡掉时 in_list=False
    top: dict = service.predict_latest("streak", settings)["rows"][0]
    pf: dict | None = service.prediction_for(top["code"], settings)
    assert pf is not None and pf["kind"] == "streak" and pf["in_list"] and pf["rank"] == 1 and pf["pick"]
    assert pf["score"] == pytest.approx(top["score"], abs=1e-3) and 0 < pf["base_prob"] < 1
    strict = SimpleNamespace(predict=SimpleNamespace(**{**vars(settings.predict), "max_price": 0.01}), trade=_ts())
    pf2: dict | None = service.prediction_for(top["code"], strict)
    assert pf2 is not None and pf2["in_list"] is False and pf2["rank"] is None and pf2["pick"] is False
    assert service.prediction_for("999999", settings) is None

    # 数据本来就是最新：更新后保留内存里的面板（不重新读取）；面板文件有变化才丢弃
    from quant_web.market import fundamentals, history, pools

    ctx = service.context()
    monkeypatch.setattr(history, "update_history", lambda progress=None: {"updated": 0, "rows": 0})
    monkeypatch.setattr(pools, "update_lhb", lambda: {"added": 0})
    monkeypatch.setattr(fundamentals, "update_fundamentals", lambda progress=None: {"updated": 0})
    upd: dict = service.update_data()
    assert upd["warnings"] == [] and service._ctx is ctx
    newest: Path = sorted(daily_dir.glob("*.parquet"))[-1]

    def rewrite(progress=None) -> dict:          # 模拟写入了新行情
        pl.read_parquet(newest).write_parquet(newest)
        return {"updated": 1, "rows": 1}

    monkeypatch.setattr(history, "update_history", rewrite)
    service.update_data()
    assert service._ctx is None
    ctx = service.context()

    # 每日流水线：模型新鲜时不重训，只预测
    monkeypatch.setattr(service, "update_data", lambda progress=None: {"warnings": []})
    trained: list[str] = []
    monkeypatch.setattr(service, "train", lambda kind, progress=None: trained.append(kind) or {})
    out: dict = service.daily_pipeline()
    # 波段模型还没训练过 → 流水线会训练它（顺序 streak→first→swing）；三个模型都给出预测结果
    assert trained == ["swing"] and set(out["predictions"]) == {"streak", "first", "swing"}
    assert out["predictions"]["streak"]["signal_date"] == panel["date"].max().isoformat()
    assert out["predictions"]["swing"]["gate"]["trade"] is False

    # 模型超过 30 天没训练（或不存在）→ 流水线自动重训该模型，再预测
    real_summary = service._model_summary
    monkeypatch.setattr(service, "_model_summary",
                        lambda kind: {**real_summary(kind), "stale": True} if kind == "first" else real_summary(kind))
    out = service.daily_pipeline()
    assert trained == ["swing", "first", "swing"] and set(out["predictions"]) == {"streak", "first", "swing"}
    service.invalidate()
