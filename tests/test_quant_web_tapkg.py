"""quant_web 模型实验室：ta 库扩展指标（factors_tapkg）离线测试。合成数据，不碰真实行情。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

ta = pytest.importorskip("ta")

from quant_web.modellab import factors as F  # noqa: E402
from quant_web.modellab import factors_tapkg as X  # noqa: E402


def _frame(n_codes: int = 4, n_days: int = 400, seed: int = 1) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    days = [date(2022, 1, 3) + timedelta(days=i) for i in range(n_days)]
    parts: list[pl.DataFrame] = []
    for i in range(n_codes):
        n = n_days - (100 if i == 1 else 0)                 # 第 2 只晚上市：行数不同
        close = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        parts.append(pl.DataFrame({
            "code": [f"60{i:04d}"] * n, "date": days[n_days - n:],
            "open": close * (1 + rng.normal(0, 0.005, n)), "high": close * (1 + np.abs(rng.normal(0, 0.01, n))),
            "low": close * (1 - np.abs(rng.normal(0, 0.01, n))), "close": close,
            "volume": rng.integers(100_000, 10_000_000, n).astype(float),
        }))
    return pl.concat(parts)


@pytest.fixture(scope="module")
def frame() -> pl.DataFrame:
    return _frame()


@pytest.fixture(scope="module")
def out(frame: pl.DataFrame) -> pl.DataFrame:
    return X.tapkg_features(frame)


def test_shape_and_order(frame: pl.DataFrame, out: pl.DataFrame) -> None:
    assert out.height == frame.height
    assert out.columns == ["code", "date", *X.TAPKG_NAMES]
    assert out["code"].to_list() == frame["code"].to_list()
    assert out["date"].to_list() == frame["date"].to_list()
    assert all(out.schema[n] == pl.Float32 for n in X.TAPKG_NAMES)


def test_every_factor_has_values(out: pl.DataFrame) -> None:
    tail = out.group_by("code").tail(50)
    for n in X.TAPKG_NAMES:
        assert tail[n].null_count() < tail.height, n


def test_no_lookahead(frame: pl.DataFrame, out: pl.DataFrame) -> None:
    """只保留前 250 天重新算，重叠部分必须和全量一致（ta 的 KST 原本会拿整段均值补热身期，这里已置空）"""
    cut_day = frame["date"].min() + timedelta(days=250)
    part = X.tapkg_features(frame.filter(pl.col("date") <= cut_day))
    j = out.join(part, on=["code", "date"], how="inner", suffix="_cut")
    for n in X.TAPKG_NAMES:
        diff = (j[n] - j[f"{n}_cut"]).abs().max()
        assert diff is None or diff < 1e-4, n


def test_fast_versions_match_ta(frame: pl.DataFrame) -> None:
    df = frame.filter(pl.col("code") == "600000").to_pandas()
    ref = ta.trend.AroonIndicator(high=df["high"], low=df["low"], window=25)
    up, down = X.aroon(df["high"], df["low"], 25)
    assert np.allclose(ref.aroon_up(), up, equal_nan=True)
    assert np.allclose(ref.aroon_down(), down, equal_nan=True)
    nvi = ta.volume.NegativeVolumeIndexIndicator(close=df["close"], volume=df["volume"]).negative_volume_index()
    assert np.allclose(nvi, X.negative_volume_index(df["close"], df["volume"]))


def test_registered_in_factor_sets() -> None:
    assert F.feature_names("tapkg") == X.TAPKG_NAMES
    assert F.set_of("x_cmf") == "tapkg"
    assert F.feature_label("x_cmf").startswith("蔡金资金流")
    assert F.frame_builder("tapkg") is X.tapkg_features
    item = next(i for i in F.listing() if i["key"] == "tapkg")
    assert item["available"] and item["n"] == len(X.TAPKG_NAMES)


def test_missing_library_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(X.importlib.util, "find_spec", lambda name: None)
    item = next(i for i in F.listing() if i["key"] == "tapkg")
    assert not item["available"] and "pip install ta" in item["reason"]
