"""Displacement detector — warm-up safety, direction, magnitude, future-invariance."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from fxlab.data.schema import ensure_bars
from fxlab.smc.displacement import DisplacementConfig, displacement


def _bars(open_, high, low, close) -> pd.DataFrame:
    idx = pd.date_range("2020-01-06", periods=len(open_), freq="5min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": np.asarray(open_, "float64"),
            "high": np.asarray(high, "float64"),
            "low": np.asarray(low, "float64"),
            "close": np.asarray(close, "float64"),
        },
        index=idx,
    )
    return ensure_bars(df, "TEST", "M5")


def _series_with_one_big_bearish_candle(n=12):
    # n-1 small bars (body 0.1, range 0.3) then a big bearish candle (body 3.0).
    open_ = [100.0] * n
    close = [100.1] * (n - 1) + [97.0]
    high = [100.2] * (n - 1) + [100.1]
    low = [99.9] * (n - 1) + [96.9]
    return _bars(open_, high, low, close)


def test_config_validation():
    with pytest.raises(ValueError):
        DisplacementConfig(atr_window=0)
    with pytest.raises(ValueError):
        DisplacementConfig(body_mult=0)


def test_warmup_never_displaces_and_small_bodies_are_quiet():
    d = displacement(_series_with_one_big_bearish_candle(), atr_window=5, body_mult=1.5)
    # Wherever ATR is undefined (warm-up), strength is NaN and the bar cannot displace.
    warm = d["disp_strength"].isna()
    assert (d.loc[warm, "disp_dir"] == 0).all()
    assert warm.iloc[0]  # first bar is always warm-up
    # The steady small-body bars (0.1 body vs ~0.3 ATR) never clear 1.5 x ATR.
    assert (d["disp_dir"].iloc[5:11] == 0).all()


def test_big_candle_flagged_with_direction_and_magnitude():
    d = displacement(_series_with_one_big_bearish_candle(), atr_window=5, body_mult=1.5)
    assert d["disp_dir"].iloc[-1] == -1  # close < open
    assert d["body"].iloc[-1] == 3.0
    assert d["disp_strength"].iloc[-1] >= 1.5


def test_direction_is_sign_of_body():
    # A big bullish candle after quiet bars must flag +1.
    open_ = [50.0] * 8 + [50.0]
    close = [50.1] * 8 + [53.0]
    high = [50.2] * 8 + [53.1]
    low = [49.9] * 8 + [49.9]
    d = displacement(_bars(open_, high, low, close), atr_window=4, body_mult=1.5)
    assert d["disp_dir"].iloc[-1] == 1


def test_displacement_future_invariant(synthetic_bars):
    full = displacement(synthetic_bars, atr_window=14, body_mult=1.5)
    for k in (60, 250, 480):
        assert_frame_equal(full.iloc[:k], displacement(synthetic_bars.iloc[:k], 14, 1.5))
