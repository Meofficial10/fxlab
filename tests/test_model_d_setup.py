"""Model D — FVG-retracement continuation setup.

Hand-crafted candle fixtures pin the objective rules (register-on-close, eligible-from-next-
bar, invalidate-before-tap, one-signal-per-bar, max_age expiry), and a future-invariance guard
proves the stateful loop is still strictly causal. Open prices are irrelevant to Model D
(it reads only high/low/close), so the helper sets open = close.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fxlab.data.schema import ensure_bars
from fxlab.setups.model_d_fvg_retracement import ModelDFvgRetracement


def _bars(highs, lows, closes=None) -> pd.DataFrame:
    highs = np.asarray(highs, dtype="float64")
    lows = np.asarray(lows, dtype="float64")
    closes = (highs + lows) / 2.0 if closes is None else np.asarray(closes, dtype="float64")
    idx = pd.date_range("2020-01-06", periods=len(highs), freq="5min", tz="UTC")
    df = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes}, index=idx)
    return ensure_bars(df, "TEST", "M5")


def test_bullish_fvg_retracement_gives_single_long_at_tap():
    #        t:   0    1    2     3     4     5    6
    highs = [10,  13,  15,   16,   14,   15,  16]
    lows = [8,   9,  11,   13, 10.5,   12,  13]
    # Bullish FVG stamped at t=2: high[0]=10 < low[2]=11 -> band (bottom 10, top 11).
    # It is NOT eligible on its creation bar (t=2); the retracement tap comes at t=4,
    # where low[4]=10.5 <= top=11 (first touch) -> one LONG.
    idx, side = ModelDFvgRetracement().generate(_bars(highs, lows))

    assert idx.tolist() == [4]
    assert side.tolist() == [1]
    assert 2 not in idx.tolist()  # never triggers on the gap's own creation bar


def test_bearish_fvg_retracement_gives_single_short_at_tap():
    #        t:    0    1    2    3    4    5    6
    highs = [8,   6,   4,   3, 6.5,   5,   4]
    lows = [6,   3,   2,   1,   3,   2,   1]
    # Bearish FVG stamped at t=2: low[0]=6 > high[2]=4 -> band (bottom 4, top 6).
    # Retracement up into the gap at t=4: high[4]=6.5 >= bottom=4 (first touch) -> one SHORT.
    idx, side = ModelDFvgRetracement().generate(_bars(highs, lows))

    assert idx.tolist() == [4]
    assert side.tolist() == [-1]


def test_close_through_far_side_invalidates_before_tap():
    #        t:   0    1    2    3    4
    highs = [10,  13,  15,  16,  14]
    lows = [8,   9,  11,  13,   9]
    closes = [9,  12,  14,  15, 9.5]
    # Same bullish gap (bottom 10, top 11) as above. At t=4 the bar dips into the gap
    # (low=9 <= top=11) AND closes below the bottom (close=9.5 < 10). Invalidation takes
    # priority over the tap -> the gap is filled-through and NO trade is taken.
    idx, side = ModelDFvgRetracement().generate(_bars(highs, lows, closes))

    assert idx.tolist() == []
    assert side.tolist() == []


def test_no_signal_on_creation_bar():
    # A bullish gap whose creation bar's own low equals the top edge (low[2]=11=top). The
    # tap predicate low<=top is trivially true there, but gc == t so it can never fire.
    idx, _ = ModelDFvgRetracement().generate(_bars([10, 13, 15], [8, 9, 11]))
    assert idx.tolist() == []


def test_gap_expires_after_max_age():
    # Identical bars to the bullish-tap case (tap would land at t=4), but max_age=1 expires
    # the gap at t=4 (t - created = 2 > 1) BEFORE the tap check -> no signal. Contrast with
    # the default-max_age test above, which fires at t=4 on the very same bars.
    highs = [10, 13, 15, 16, 14]
    lows = [8, 9, 11, 13, 10.5]
    idx, _ = ModelDFvgRetracement(max_age=1).generate(_bars(highs, lows))
    assert idx.tolist() == []


@pytest.mark.parametrize("kwargs", [{"min_gap": -1.0}, {"max_age": 0}])
def test_config_validation_rejects_bad_params(kwargs):
    with pytest.raises(ValueError):
        ModelDFvgRetracement(**kwargs)


def test_future_invariant_on_synthetic(synthetic_bars):
    # The setup is stateful (tracks live gaps) but must still be strictly causal: signals at
    # indices < k depend only on bars <= their own position, so truncating the tail cannot
    # change them.
    setup = ModelDFvgRetracement()
    idx_full, side_full = setup.generate(synthetic_bars)
    assert len(idx_full) > 0  # non-vacuous guard: the mechanism actually fires on this data

    for k in (120, 300, 520):
        idx_k, side_k = setup.generate(synthetic_bars.iloc[:k])
        mask = idx_full < k
        assert np.array_equal(idx_k, idx_full[mask])
        assert np.array_equal(side_k, side_full[mask])
