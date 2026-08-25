"""Model C — breakout-failure (fakeout) reversal setup.

Hand-crafted candle fixtures (span left=1/right=1, so a swing confirms one bar after it forms)
pin the objective rules: a close-based breakout of a confirmed swing that is later *reclaimed*
fires the fade at the reclaim bar; a breakout that holds fires nothing; an over-late reclaim is
blocked by ``max_wait``. A future-invariance guard proves the stateful watch is still causal.
Open prices are irrelevant to Model C (market_structure reads only close + confirmed swings from
high/low), so the helper sets open = close.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fxlab.data.schema import ensure_bars
from fxlab.setups.model_c_breakout_failure import ModelCBreakoutFailure


def _bars(highs, lows, closes) -> pd.DataFrame:
    highs = np.asarray(highs, dtype="float64")
    lows = np.asarray(lows, dtype="float64")
    closes = np.asarray(closes, dtype="float64")
    idx = pd.date_range("2020-01-06", periods=len(highs), freq="5min", tz="UTC")
    df = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes}, index=idx)
    return ensure_bars(df, "TEST", "M5")


# A confirmed swing high of 13 forms at t=1; price closes above it (13.5) at t=4 -> up-breakout
# armed at level 13; the close falls back below 13 (to 12.5) at t=6 -> failed breakout -> SHORT.
_BEAR = dict(
    highs=[11, 13, 11, 11.5, 14, 14.5, 13, 12],
    lows=[9, 11, 9, 9.5, 12, 12.5, 10, 9],
    closes=[10, 12, 10, 11, 13.5, 14, 12.5, 10],
)
# Mirror image (reflected about 24): swing low 11 at t=1, down-breakout at t=4, reclaim -> LONG.
_BULL = dict(
    highs=[15, 13, 15, 14.5, 12, 11.5, 14, 15],
    lows=[13, 11, 13, 12.5, 10, 9.5, 11, 12],
    closes=[14, 12, 14, 13, 10.5, 10, 11.5, 14],
)


def test_failed_up_breakout_gives_single_short_at_reclaim():
    idx, side = ModelCBreakoutFailure(left=1, right=1).generate(_bars(**_BEAR))
    assert idx.tolist() == [6]
    assert side.tolist() == [-1]
    assert 4 not in idx.tolist()  # the breakout bar itself is never a signal


def test_failed_down_breakout_gives_single_long_at_reclaim():
    idx, side = ModelCBreakoutFailure(left=1, right=1).generate(_bars(**_BULL))
    assert idx.tolist() == [6]
    assert side.tolist() == [1]


def test_breakout_that_holds_gives_no_signal():
    # Same structure/breakout as _BEAR, but every close after the break stays above the level
    # (13) -> the breakout never fails -> no fade.
    held = dict(_BEAR, closes=[10, 12, 10, 11, 13.5, 14, 13.5, 14])
    idx, _ = ModelCBreakoutFailure(left=1, right=1).generate(_bars(**held))
    assert idx.tolist() == []


def test_max_wait_expires_a_late_reclaim():
    # Breakout at t=4, reclaim not until t=7 (three bars later). With max_wait=1 the watch
    # expires first -> no signal; with a generous window the very same bars fire at t=7.
    highs = [11, 13, 11, 11.5, 14, 14.5, 15, 13.5]
    lows = [9, 11, 9, 9.5, 12, 12.5, 13, 10]
    closes = [10, 12, 10, 11, 13.5, 14, 14.5, 12]
    bars = _bars(highs, lows, closes)

    idx_short, _ = ModelCBreakoutFailure(left=1, right=1, max_wait=1).generate(bars)
    assert idx_short.tolist() == []

    idx_long, side_long = ModelCBreakoutFailure(left=1, right=1, max_wait=10).generate(bars)
    assert idx_long.tolist() == [7]
    assert side_long.tolist() == [-1]


@pytest.mark.parametrize("kwargs", [{"max_wait": 0}, {"left": 0}, {"right": 0}])
def test_config_validation_rejects_bad_params(kwargs):
    with pytest.raises(ValueError):
        ModelCBreakoutFailure(**kwargs)


def test_future_invariant_on_synthetic(synthetic_bars):
    # Stateful (latched breakout levels + ageing watches) but strictly causal: signals at
    # indices < k depend only on bars <= their own position.
    setup = ModelCBreakoutFailure()
    idx_full, side_full = setup.generate(synthetic_bars)
    assert len(idx_full) > 0  # non-vacuous: the mechanism actually fires on this data

    for k in (120, 300, 520):
        idx_k, side_k = setup.generate(synthetic_bars.iloc[:k])
        mask = idx_full < k
        assert np.array_equal(idx_k, idx_full[mask])
        assert np.array_equal(side_k, side_full[mask])
