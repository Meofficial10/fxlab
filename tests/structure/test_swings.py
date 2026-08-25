"""Swing/fractal detection — strictness, causal confirmation timing, future-invariance.

Fixtures are hand-crafted OHLC frames with KNOWN pivots so every assertion is checkable
by eye against the definition in :mod:`fxlab.structure.swings`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from fxlab.data.schema import ensure_bars
from fxlab.structure.swings import SwingConfig, confirmed_swings, pivot_highs_lows


def _bars(highs, lows, closes=None) -> pd.DataFrame:
    """Build a canonical bar frame from explicit high/low (close defaults to the mid)."""
    highs = np.asarray(highs, dtype="float64")
    lows = np.asarray(lows, dtype="float64")
    closes = np.asarray(closes, dtype="float64") if closes is not None else (highs + lows) / 2.0
    idx = pd.date_range("2020-01-06", periods=len(highs), freq="5min", tz="UTC")
    df = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes}, index=idx)
    return ensure_bars(df, "TEST", "M5")


def _nonnan_positions(series: pd.Series) -> set[int]:
    arr = series.to_numpy()
    return set(np.flatnonzero(~np.isnan(arr)).tolist())


def test_swing_config_rejects_zero_span():
    with pytest.raises(ValueError):
        SwingConfig(left=0, right=2)
    with pytest.raises(ValueError):
        SwingConfig(left=2, right=0)


def test_strict_fractal_high_positions():
    # highs peak (strictly) at index 2 (=3) and index 6 (=5); nothing else qualifies.
    highs = [1, 2, 3, 2, 1, 2, 5, 2, 1, 2, 3]
    piv = pivot_highs_lows(_bars(highs, highs), left=2, right=2)
    assert _nonnan_positions(piv["pivot_high"]) == {2, 6}
    assert piv["pivot_high"].iloc[2] == 3.0
    assert piv["pivot_high"].iloc[6] == 5.0


def test_strict_fractal_low_position():
    # single strict trough at index 3 (=7); the surrounding plateau of 9s never qualifies.
    lows = [9, 9, 9, 7, 9, 9, 9, 9, 9, 9, 9]
    piv = pivot_highs_lows(_bars(lows, lows), left=2, right=2)
    assert _nonnan_positions(piv["pivot_low"]) == {3}
    assert piv["pivot_low"].iloc[3] == 7.0


def test_flat_top_is_not_a_pivot():
    # A twin-peak plateau (3,3) must NOT count as a swing high: strict '>' rejects equality.
    highs = [1, 3, 3, 1, 0]
    piv = pivot_highs_lows(_bars(highs, [0, 0, 0, 0, 0]), left=1, right=1)
    assert _nonnan_positions(piv["pivot_high"]) == set()


def test_confirmed_swing_timing_is_causal():
    # Pivot high sits at index 2; with right=2 it may only be KNOWN from index 4 onward.
    highs = [1, 2, 3, 2, 1, 1, 1]
    lows = [0, 0, 0, 0, 0, 0, 0]
    cs = confirmed_swings(_bars(highs, lows), left=2, right=2)
    idx = cs["last_swing_high_idx"].to_numpy()
    assert idx.tolist() == [-1, -1, -1, -1, 2, 2, 2]
    val = cs["last_swing_high"].to_numpy()
    assert np.isnan(val[:4]).all()
    assert (val[4:] == 3.0).all()


def test_confirmed_swings_future_invariant(synthetic_bars):
    # Appending future bars must never change a past confirmed-swing row (leakage guard).
    full = confirmed_swings(synthetic_bars, left=2, right=2)
    for k in (50, 200, 371):
        trunc = confirmed_swings(synthetic_bars.iloc[:k], left=2, right=2)
        assert_frame_equal(full.iloc[:k], trunc)
