"""Fair Value Gap detector — 3-candle geometry, min-size filter, future-invariance."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from fxlab.data.schema import ensure_bars
from fxlab.smc.fvg import fair_value_gaps


def _bars(highs, lows) -> pd.DataFrame:
    highs = np.asarray(highs, dtype="float64")
    lows = np.asarray(lows, dtype="float64")
    mid = (highs + lows) / 2.0
    idx = pd.date_range("2020-01-06", periods=len(highs), freq="5min", tz="UTC")
    df = pd.DataFrame({"open": mid, "high": highs, "low": lows, "close": mid}, index=idx)
    return ensure_bars(df, "TEST", "M5")


def test_bullish_and_bearish_gaps_detected_at_candle3():
    #        t:  0    1    2    3    4    5    6
    highs = [10,  13,  15,  14,  12,   6,   7]
    lows = [ 8,   9,  11,  10,   7,   3,   5]
    # t2 bullish: high[0]=10 < low[2]=11  -> band (10, 11), size 1
    # t5 bearish: low[3]=10 > high[5]=6   -> band (6, 10),  size 4
    fvg = fair_value_gaps(_bars(highs, lows))

    assert fvg["fvg_dir"].tolist() == [0, 0, 1, 0, 0, -1, 0]
    assert fvg["fvg_bottom"].iloc[2] == 10.0
    assert fvg["fvg_top"].iloc[2] == 11.0
    assert fvg["fvg_size"].iloc[2] == 1.0
    assert fvg["fvg_bottom"].iloc[5] == 6.0
    assert fvg["fvg_top"].iloc[5] == 10.0
    assert fvg["fvg_size"].iloc[5] == 4.0
    # No-gap bars carry NaN band edges, not stale numbers.
    assert np.isnan(fvg["fvg_top"].iloc[0])
    assert np.isnan(fvg["fvg_size"].iloc[6])


def test_min_size_filters_small_gaps_strictly():
    highs = [10, 13, 15, 14, 12, 6, 7]
    lows = [8, 9, 11, 10, 7, 3, 5]
    # min_size 1.5 drops the size-1 bullish gap; the strict '>' also means a gap exactly
    # equal to the threshold would be dropped. The size-4 bearish gap survives.
    fvg = fair_value_gaps(_bars(highs, lows), min_size=1.5)
    assert fvg["fvg_dir"].tolist() == [0, 0, 0, 0, 0, -1, 0]


def test_fewer_than_three_bars_is_empty_not_error():
    fvg = fair_value_gaps(_bars([2, 3], [0, 1]))
    assert fvg["fvg_dir"].tolist() == [0, 0]
    assert fvg["fvg_top"].isna().all()


def test_fvg_future_invariant(synthetic_bars):
    full = fair_value_gaps(synthetic_bars)
    for k in (40, 200, 511):
        assert_frame_equal(full.iloc[:k], fair_value_gaps(synthetic_bars.iloc[:k]))
