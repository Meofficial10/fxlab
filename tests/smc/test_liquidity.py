"""Liquidity detectors — sweeps (both directions) and equal-level pools within tolerance."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from fxlab.data.schema import ensure_bars
from fxlab.smc.liquidity import equal_highs_lows, liquidity_sweeps


def _bars(highs, lows, closes) -> pd.DataFrame:
    idx = pd.date_range("2020-01-06", periods=len(highs), freq="5min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": np.asarray(closes, "float64"),
            "high": np.asarray(highs, "float64"),
            "low": np.asarray(lows, "float64"),
            "close": np.asarray(closes, "float64"),
        },
        index=idx,
    )
    return ensure_bars(df, "TEST", "M5")


def test_bearish_sweep_wicks_above_swing_high_then_closes_below():
    # Confirmed swing high = 15 at idx3 (confirm@5). idx6 wicks to 16 but closes at 14.
    highs = [10, 11, 12, 15, 12, 11, 16, 13]
    lows = [8, 9, 10, 11, 10, 9, 10, 9]
    closes = [9, 10, 11, 13, 11, 10, 14, 12]
    sw = liquidity_sweeps(_bars(highs, lows, closes), left=2, right=2)

    assert sw["sweep_dir"].iloc[6] == -1
    assert sw["swept_level"].iloc[6] == 15.0
    assert sw["swept_idx"].iloc[6] == 3
    assert (sw["sweep_dir"].drop(index=sw.index[6]) == 0).all()


def test_bullish_sweep_wicks_below_swing_low_then_closes_above():
    # Confirmed swing low = 7 at idx3 (confirm@5). idx6 wicks to 6 but closes at 10.
    highs = [14, 13, 12, 11, 12, 13, 12, 11]
    lows = [12, 11, 10, 7, 10, 11, 6, 9]
    closes = [13, 12, 11, 9, 11, 12, 10, 10]
    sw = liquidity_sweeps(_bars(highs, lows, closes), left=2, right=2)

    assert sw["sweep_dir"].iloc[6] == 1
    assert sw["swept_level"].iloc[6] == 7.0
    assert sw["swept_idx"].iloc[6] == 3


def test_equal_highs_respect_tolerance():
    # Two swing highs: 15.0 (idx2, confirm@4) and 15.05 (idx6, confirm@8).
    highs = [10, 11, 15.0, 11, 10, 11, 15.05, 11, 10]
    lows = [5] * 9
    closes = [(h + 5) / 2 for h in highs]
    bars = _bars(highs, lows, closes)

    loose = equal_highs_lows(bars, tol=0.1)
    assert bool(loose["equal_high"].iloc[8]) is True  # 0.05 <= 0.1 -> equal pool
    assert loose["equal_high"].sum() == 1  # only the 2nd swing of the pair is flagged

    tight = equal_highs_lows(bars, tol=0.01)
    assert bool(tight["equal_high"].iloc[8]) is False  # 0.05 > 0.01 -> not equal


def test_liquidity_future_invariant(synthetic_bars):
    full_sw = liquidity_sweeps(synthetic_bars)
    full_eq = equal_highs_lows(synthetic_bars, tol=0.0005)
    for k in (60, 250, 480):
        assert_frame_equal(full_sw.iloc[:k], liquidity_sweeps(synthetic_bars.iloc[:k]))
        assert_frame_equal(full_eq.iloc[:k], equal_highs_lows(synthetic_bars.iloc[:k], tol=0.0005))
