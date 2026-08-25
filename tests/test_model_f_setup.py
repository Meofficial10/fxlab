"""Model F — time-series momentum setup.

Hand-built close series pin the objective rule (sign of the trailing ``lookback``-bar return);
Model F reads only ``close``, so the helper sets ``open = high = low = close``. A future-invariance
guard proves the trailing-return signal is strictly causal, and a count check proves signalling is
continuous (one signal per in-state bar), not flip-only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fxlab.data.schema import ensure_bars
from fxlab.setups.model_f_momentum import ModelFMomentum


def _daily(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype="float64")
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="1D", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes}, index=idx
    )
    return ensure_bars(df, "TEST", "D1")


def test_up_state_emits_long_and_respects_warmup():
    # Monotonic rise: every post-warmup bar is in an up-state (mom = close[t]-close[t-2] > 0).
    bars = _daily([1, 2, 3, 4, 5])
    idx, side = ModelFMomentum(lookback=2).generate(bars)
    assert idx.tolist() == [2, 3, 4]          # warm-up bars 0,1 never fire
    assert side.tolist() == [1, 1, 1]
    assert idx.min() >= 2                      # warm-up guard


def test_down_state_emits_short():
    bars = _daily([5, 4, 3, 2, 1])
    idx, side = ModelFMomentum(lookback=2).generate(bars)
    assert idx.tolist() == [2, 3, 4]
    assert side.tolist() == [-1, -1, -1]


def test_flat_vs_lookback_ago_emits_no_signal():
    # Each bar equals the bar `lookback` ago -> trailing return is exactly 0 -> no signal.
    bars = _daily([1, 2, 1, 2, 1])
    idx, _ = ModelFMomentum(lookback=2).generate(bars)
    assert idx.tolist() == []


def test_state_flip_tracks_the_sign_of_trailing_return():
    # mom (lookback=2) sign at t=2..6 is +,0,-,0,+ -> long, skip, short, skip, long.
    bars = _daily([1, 2, 3, 2, 1, 2, 3])
    idx, side = ModelFMomentum(lookback=2).generate(bars)
    assert idx.tolist() == [2, 4, 6]
    assert side.tolist() == [1, -1, 1]


def test_signalling_is_continuous_one_per_in_state_bar():
    # A pure rise: every one of the n-lookback post-warmup bars fires (continuous state, not flip).
    closes = np.arange(1.0, 11.0)  # 10 strictly increasing bars
    bars = _daily(closes)
    idx, side = ModelFMomentum(lookback=3).generate(bars)
    assert len(idx) == len(closes) - 3
    assert set(side.tolist()) == {1}


def test_too_few_bars_returns_empty():
    bars = _daily([1, 2, 3])
    idx, side = ModelFMomentum(lookback=5).generate(bars)
    assert idx.tolist() == [] and side.tolist() == []


def test_future_invariant_on_synthetic(synthetic_bars):
    # mom[t] reads only close[t] and close[t-lookback], both <= t -> appending future bars can never
    # change a past signal. A small lookback ensures the mechanism fires within the 600-bar fixture.
    setup = ModelFMomentum(lookback=20)
    idx_full, side_full = setup.generate(synthetic_bars)
    assert len(idx_full) > 0  # non-vacuous

    for k in (150, 350, 550):
        idx_k, side_k = setup.generate(synthetic_bars.iloc[:k])
        mask = idx_full < k
        assert np.array_equal(idx_k, idx_full[mask])
        assert np.array_equal(side_k, side_full[mask])


@pytest.mark.parametrize("kwargs", [{"lookback": 0}, {"lookback": -5}])
def test_config_validation_rejects_bad_params(kwargs):
    with pytest.raises(ValueError):
        ModelFMomentum(**kwargs)
