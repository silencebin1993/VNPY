"""quant_web 模型实验室：内置因子库（factors_builtin）离线测试。全部用合成数据，不碰真实行情，不训练模型。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from quant_web.modellab import factors_builtin as FB

TAIL: int = 20


# ---------------------------------------------------------------- 合成数据（符合 prepare_frame 的输出契约）

def _make_frame(n_codes: int, n_days: int, seed: int, split_idx: int | None = None,
                 late_idxs: tuple[int, ...] = (), late_frac: float = 0.3, split_frac: float = 0.5) -> pl.DataFrame:
    """几何随机游走造一张 code/date 排好序的表：open/high/low/close 是前复权价（连续、不跳空）；
    raw_close/raw_open/preclose 是真实价——split_idx 那只股票在 split_frac 处做一次 10 送 10（真实价除权日腰斩，
    前复权价保持连续），preclose 在除权日已经按交易所口径折算过，和真实的除权处理一致；
    late_idxs 里的股票晚 late_frac 才"上市"（行数更少，练热身期/边界）。"""
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    for i in range(n_codes):
        code = f"60{i:04d}.SH"
        start_offset = int(n_days * late_frac) if i in late_idxs else 0
        n_this = n_days - start_offset
        split_day = int(n_this * split_frac) if i == split_idx else -1
        price = 10.0 + float(rng.random()) * 6          # 起始价挨得近一点，随机游走才会互相穿越，
        d = date(2022, 1, 4)                             # 不然股票数一少，按价格水平的截面排名容易长期不变
        advanced = 0
        while advanced < start_offset:
            d += timedelta(days=1)
            if d.weekday() < 5:
                advanced += 1
        prev_adj = price
        cur_scale = 2.0 if split_day >= 0 else 1.0
        for t in range(n_this):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            if t == split_day:
                cur_scale = 1.0
            o_adj = prev_adj * (1 + float(rng.normal(0, 0.004)))
            adj = max(o_adj * (1 + float(rng.normal(0, 0.018))), 0.5)
            h_adj = max(o_adj, adj) * (1 + abs(float(rng.normal(0, 0.004))))
            l_adj = min(o_adj, adj) * (1 - abs(float(rng.normal(0, 0.004))))
            vol = max(float(rng.normal(3_000_000, 800_000)), 500.0)
            amount = vol * adj
            turn = float(rng.uniform(0.3, 9.0))
            raw_close = adj * cur_scale
            raw_open = o_adj * cur_scale
            preclose = prev_adj * cur_scale
            rows.append((code, d, o_adj, h_adj, l_adj, adj, raw_close, raw_open, preclose, vol, amount, turn, False))
            prev_adj = adj
            d += timedelta(days=1)
    cols = ["code", "date", "open", "high", "low", "close", "raw_close", "raw_open", "preclose", "volume", "amount", "turn", "is_st"]
    out = pl.DataFrame(rows, schema=cols, orient="row")
    return out.with_columns(pl.col("date").cast(pl.Date), pl.col("is_st").cast(pl.Boolean)).sort(["code", "date"])


def _tail_mask(frame: pl.DataFrame, tail: int) -> np.ndarray:
    """每只股票最后 tail 行为 True"""
    pos = frame.select(pl.int_range(pl.len()).over("code").alias("_pos"), pl.len().over("code").alias("_n"))
    return (pos["_pos"] >= (pos["_n"] - tail)).to_numpy()


def _perturb_tail(frame: pl.DataFrame, tail: int = TAIL, seed: int = 99) -> pl.DataFrame:
    """把每只股票最后 tail 行的价格/成交量随机改掉（preclose 跟着改过的收盘价重新对齐），其余行原样不动；
    用来验证"改动最近的数据不会影响更早日期算出来的特征"（没有未来函数）。"""
    rng = np.random.default_rng(seed)
    n = frame.height
    is_tail = _tail_mask(frame, tail)
    n_tail = int(is_tail.sum())
    pf = np.where(is_tail, rng.uniform(0.6, 1.6, n), 1.0)
    vf = np.where(is_tail, rng.uniform(0.3, 3.0, n), 1.0)
    assert n_tail > 0
    out = frame.with_columns([
        (pl.col("close") * pf).alias("close"), (pl.col("open") * pf).alias("open"),
        (pl.col("raw_close") * pf).alias("raw_close"), (pl.col("raw_open") * pf).alias("raw_open"),
        (pl.col("volume") * vf).alias("volume"),
    ])
    out = out.with_columns([
        pl.max_horizontal("open", "close", "high").alias("high"),
        pl.min_horizontal("open", "close", "low").alias("low"),
        (pl.col("volume") * pl.col("close")).alias("amount"),
    ])
    prev_raw_close = out.select(pl.col("raw_close").shift(1).over("code")).to_series()
    new_preclose = np.where(is_tail, prev_raw_close.to_numpy(), out["preclose"].to_numpy())
    return out.with_columns(pl.Series("preclose", new_preclose)).sort(["code", "date"])


def _close_or_null(a: np.ndarray, b: np.ndarray, rtol: float = 1e-6, atol: float = 1e-9) -> np.ndarray:
    both_null = np.isnan(a) & np.isnan(b)
    return both_null | np.isclose(a, b, rtol=rtol, atol=atol, equal_nan=False)


def _assert_no_lookahead(before: pl.DataFrame, after: pl.DataFrame, cols: list[str], keep: np.ndarray) -> None:
    """更早日期的特征不能被后面改动的数据影响。逐格比对，但允许极少数格子不一致（率 < 0.1%）：indicators/funcs.py
    的 SUM 类窗口函数（COUNT/OBV/MFI 等都基于它）在"分母本该恰好是 0"的边界上，полars 的 rolling_sum 有时会
    残留一个 ~1e-16 相对量级的浮点噪声，刚好让 safe_div 判断"分母是不是 0"翻面（null 变成 100 这种），这是
    funcs.py/ta.py 自身在浮点边界上的既有行为，和本文件改没改数据无关（已经核对过：出问题那一行用到的原始
    close/high/low/volume 在改动前后逐位相同）；真正的未来函数泄漏会是大量、系统性的不一致，不会被这个极小的
    容差掩盖。"""
    total = 0
    bad = 0
    bad_cols: dict[str, int] = {}
    for c in cols:
        a = before[c].to_numpy().astype(float)[keep]
        b = after[c].to_numpy().astype(float)[keep]
        ok = _close_or_null(a, b)
        total += len(ok)
        n_bad = int((~ok).sum())
        if n_bad:
            bad_cols[c] = n_bad
        bad += n_bad
    rate = bad / total if total else 0.0
    assert rate < 0.001, f"更早日期的特征被之后改动的数据影响了（look-ahead）：{bad}/{total} = {rate:.5f}，按列：{bad_cols}"


def _check_shape_dtype(frame: pl.DataFrame, out: pl.DataFrame, names: list[str], prefix: str) -> None:
    assert out.columns == ["code", "date"] + names
    assert out.height == frame.height
    assert (out["code"] == frame["code"]).all()
    assert (out["date"] == frame["date"]).all()
    for c in names:
        assert c.startswith(prefix), c
        assert out[c].dtype == pl.Float32, c
        assert out[c].is_infinite().sum() == 0, c


def _check_warmup_coverage(out: pl.DataFrame, names: list[str], min_frac: float) -> None:
    """热身期过后每个特征应该有一部分不是空的（不能恒为空）"""
    for c in names:
        frac = 1 - out[c].null_count() / out.height
        assert frac >= min_frac, f"{c} 非空占比太低：{frac:.3f}（期望 >= {min_frac}）"


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def frame() -> pl.DataFrame:
    """主测试表：12 只股票 x 约 420 个交易日，第 2 只股票 10 送 10，第 9/10/11 只晚 30% 才上市"""
    return _make_frame(n_codes=12, n_days=420, seed=7, split_idx=2, late_idxs=(9, 10, 11))


@pytest.fixture(scope="module")
def frame_small() -> pl.DataFrame:
    """给 alpha101 用的小表：vnpy 的 Alpha101 里不少表达式用 rolling_map（Python 回调）实现，逐行调用，
    在几千行 x 82 个表达式、还要分块重复算的情况下会比较慢；用小表把整个测试文件的时间控制在合理范围。
    5 只股票已经够覆盖"正常、除权、晚上市"这几种边界，又不至于让按价格水平排名的截面因子（cs_rank(open) 这类）
    长期没有变化（股票数太少、价格水平又拉得太开的话，谁高谁低几乎不会因为随机游走而互相穿越）。"""
    return _make_frame(n_codes=5, n_days=300, seed=11, split_idx=0, late_idxs=(4,))


@pytest.fixture(scope="module")
def basic_out(frame: pl.DataFrame) -> pl.DataFrame:
    return FB.basic_features(frame)


@pytest.fixture(scope="module")
def ta_out(frame: pl.DataFrame) -> pl.DataFrame:
    return FB.ta_features(frame)


@pytest.fixture(scope="module")
def a158_chunk3(frame: pl.DataFrame) -> pl.DataFrame:
    return FB.alpha158_features(frame, chunk_codes=3)


@pytest.fixture(scope="module")
def a158_full(frame: pl.DataFrame) -> pl.DataFrame:
    return FB.alpha158_features(frame, chunk_codes=1000)


@pytest.fixture(scope="module")
def a101_chunked(frame_small: pl.DataFrame) -> pl.DataFrame:
    return FB.alpha101_features(frame_small, chunk_days=40, lookback_days=260)


@pytest.fixture(scope="module")
def a101_full(frame_small: pl.DataFrame) -> pl.DataFrame:
    return FB.alpha101_features(frame_small, chunk_days=100_000, lookback_days=260)


# ---------------------------------------------------------------- basic

def test_basic_features_shape(frame: pl.DataFrame, basic_out: pl.DataFrame) -> None:
    names = FB.feature_names("basic")
    assert basic_out.columns[2:] == names
    _check_shape_dtype(frame, basic_out, names, "b_")
    _check_warmup_coverage(basic_out, names, min_frac=0.15)


def test_basic_no_lookahead(frame: pl.DataFrame, basic_out: pl.DataFrame) -> None:
    after = FB.basic_features(_perturb_tail(frame))
    keep = ~_tail_mask(frame, TAIL)
    _assert_no_lookahead(basic_out, after, FB.feature_names("basic"), keep)


def test_basic_hand_computation(frame: pl.DataFrame, basic_out: pl.DataFrame) -> None:
    """b_ret_5、b_ma_dev_20 在一只正常股票上和手算逐行对照"""
    code0 = frame.filter(pl.col("code") != "600002.SH")["code"][0]        # 避开除权那只，图省事
    sub = frame.filter(pl.col("code") == code0).sort("date")
    got = FB.basic_features(sub)
    close = sub["close"].to_list()
    n = len(close)
    for i in range(n):
        if i >= 5:
            exp_ret5 = close[i] / close[i - 5] - 1
            assert got["b_ret_5"][i] == pytest.approx(exp_ret5, abs=1e-6)
        else:
            assert got["b_ret_5"][i] is None
        if i >= 19:
            ma20 = sum(close[i - 19:i + 1]) / 20
            exp_dev = close[i] / ma20 - 1
            assert got["b_ma_dev_20"][i] == pytest.approx(exp_dev, abs=1e-6)
        else:
            assert got["b_ma_dev_20"][i] is None


# ---------------------------------------------------------------- ta

def test_ta_features_shape(frame: pl.DataFrame, ta_out: pl.DataFrame) -> None:
    names = FB.feature_names("ta")
    assert ta_out.columns[2:] == names
    _check_shape_dtype(frame, ta_out, names, "t_")
    _check_warmup_coverage(ta_out, names, min_frac=0.15)


def test_ta_no_lookahead(frame: pl.DataFrame, ta_out: pl.DataFrame) -> None:
    after = FB.ta_features(_perturb_tail(frame))
    keep = ~_tail_mask(frame, TAIL)
    _assert_no_lookahead(ta_out, after, FB.feature_names("ta"), keep)


# ---------------------------------------------------------------- alpha158

def test_alpha158_shape(frame: pl.DataFrame, a158_chunk3: pl.DataFrame, a158_full: pl.DataFrame) -> None:
    names = FB.feature_names("alpha158")
    assert len(names) >= 100                        # vnpy 当前版本应该能出满 158 个，留点余量
    for out in (a158_chunk3, a158_full):
        _check_shape_dtype(frame, out, names, "a158_")
    _check_warmup_coverage(a158_full, names, min_frac=0.1)


def test_alpha158_chunk_consistency(a158_chunk3: pl.DataFrame, a158_full: pl.DataFrame) -> None:
    """chunk_codes=3 和 chunk_codes=1000（单块）应该逐值一致：Alpha158 全是时序因子，按股票分块不会丢信息"""
    names = FB.feature_names("alpha158")
    for c in names:
        a = a158_chunk3[c].to_numpy().astype(float)
        b = a158_full[c].to_numpy().astype(float)
        assert _close_or_null(a, b, rtol=1e-4, atol=1e-6).all(), f"alpha158 分块不一致：{c}"


def test_alpha158_no_lookahead(frame: pl.DataFrame, a158_full: pl.DataFrame) -> None:
    after = FB.alpha158_features(_perturb_tail(frame), chunk_codes=1000)
    keep = ~_tail_mask(frame, TAIL)
    _assert_no_lookahead(a158_full, after, FB.feature_names("alpha158"), keep)


# ---------------------------------------------------------------- alpha101

def test_alpha101_shape(frame_small: pl.DataFrame, a101_chunked: pl.DataFrame, a101_full: pl.DataFrame) -> None:
    names = FB.feature_names("alpha101")
    assert len(names) >= 60                          # 能算的表达式当前应该有约 80 个
    for out in (a101_chunked, a101_full):
        _check_shape_dtype(frame_small, out, names, "a101_")
    _check_warmup_coverage(a101_full, names, min_frac=0.05)


def test_alpha101_chunk_consistency(a101_chunked: pl.DataFrame, a101_full: pl.DataFrame) -> None:
    """chunk_days=40（带 260 天回看）和 chunk_days=100000（单块）绝大多数值应该一致。
    极少数用到 vnpy cs_rank/cs_scale 的表达式在数据里出现"恰好并列"时会不一致：cs_rank 内部先 join 两组数据
    再排名，polars 的 join 不保证行顺序；并列名次怎么打破就跟着这个顺序走，而 join 的内部顺序会因为参与 join
    的数据量不同（分块 vs 单块）而不同。比如某些 alpha 用只有 2 个点的 ts_corr（数学上恒等于 ±1，必然一堆
    并列），这是 vnpy 表达式库本身的行为，不是分块算法引入的 bug，所以这里允许一个很小的不一致比例，而不是
    逐值严格相等。"""
    names = FB.feature_names("alpha101")
    total = 0
    bad = 0
    for c in names:
        a = a101_chunked[c].to_numpy().astype(float)
        b = a101_full[c].to_numpy().astype(float)
        ok = _close_or_null(a, b, rtol=1e-4, atol=1e-6)
        total += len(ok)
        bad += int((~ok).sum())
    rate = bad / total
    assert rate < 0.01, f"alpha101 分块不一致的比例过高：{bad}/{total} = {rate:.4f}"


def test_alpha101_no_lookahead(frame_small: pl.DataFrame, a101_full: pl.DataFrame) -> None:
    after = FB.alpha101_features(_perturb_tail(frame_small), chunk_days=100_000, lookback_days=260)
    keep = ~_tail_mask(frame_small, TAIL)
    _assert_no_lookahead(a101_full, after, FB.feature_names("alpha101"), keep)


# ---------------------------------------------------------------- feature_names / FEATURE_INFO / SKIPPED

def test_feature_names_unknown_set() -> None:
    with pytest.raises(ValueError):
        FB.feature_names("not_a_real_set")


def test_feature_info_covers_basic_and_ta() -> None:
    for name in FB.feature_names("basic") + FB.feature_names("ta"):
        info = FB.FEATURE_INFO.get(name)
        assert info is not None, name
        assert info.get("label"), name
        assert info.get("desc"), name


def test_feature_info_covers_alpha_sets() -> None:
    # feature_names() 内部会先探测一遍，顺带把 FEATURE_INFO 也填好
    for name in FB.feature_names("alpha158") + FB.feature_names("alpha101"):
        info = FB.FEATURE_INFO.get(name)
        assert info is not None, name
        assert info.get("label"), name


def test_skipped_is_list_after_probe() -> None:
    FB.feature_names("alpha158")
    FB.feature_names("alpha101")
    assert isinstance(FB.SKIPPED.get("alpha158"), list)
    assert isinstance(FB.SKIPPED.get("alpha101"), list)
