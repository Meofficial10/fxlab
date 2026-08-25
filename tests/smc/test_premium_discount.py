"""Premium/discount detector — zone classification, equilibrium band, undefined-before-range."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from fxlab.data.schema import ensure_bars
from fxlab.smc.premium_discount import premium_discount


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


# Range built from: swing low = 5 at idx2 (confirm@4), swing high = 20 at idx6 (confirm@8).
# From idx8 on the dealing range is [5, 20], equilibrium 12.5. Closes at 8/9/10 pick zones.
_HIGHS = [12, 12, 11, 12, 13, 14, 20, 15, 19, 13, 13]
_LOWS = [8, 8, 5, 9, 9, 9, 10, 10, 10, 6, 6]
_CLOSES = [10, 10, 7, 10, 11, 12, 15, 12, 18, 7, 12.5]


def test_zone_classification_premium_discount_equilibrium():
    pd_ = premium_discount(_bars(_HIGHS, _LOWS, _CLOSES), left=2, right=2, eq_band=0.0)
    assert pd_["pd_zone"].iloc[8] == "premium"  # pos = 13/15 ≈ 0.867
    assert pd_["pd_zone"].iloc[9] == "discount"  # pos = 2/15 ≈ 0.133
    assert pd_["pd_zone"].iloc[10] == "equilibrium"  # pos = 7.5/15 = 0.5
    assert pd_["range_low"].iloc[8] == 5.0
    assert pd_["range_high"].iloc[8] == 20.0
    assert pd_["eq"].iloc[8] == 12.5
    assert abs(pd_["pd_pos"].iloc[8] - 13.0 / 15.0) < 1e-12


def test_zone_is_undefined_before_both_swings_confirm():
    pd_ = premium_discount(_bars(_HIGHS, _LOWS, _CLOSES), left=2, right=2)
    # The swing high only confirms at idx8, so no dealing range exists before then.
    assert (pd_["pd_zone"].iloc[:8] == "").all()
    assert pd_["pd_pos"].iloc[:8].isna().all()


def test_equilibrium_band_widens_the_neutral_zone():
    # With a wide band, pos 0.867 no longer clears the 0.9 premium threshold.
    pd_ = premium_discount(_bars(_HIGHS, _LOWS, _CLOSES), left=2, right=2, eq_band=0.4)
    assert pd_["pd_zone"].iloc[8] == "equilibrium"


def test_eq_band_validation():
    with pytest.raises(ValueError):
        premium_discount(_bars(_HIGHS, _LOWS, _CLOSES), eq_band=0.5)


def test_premium_discount_future_invariant(synthetic_bars):
    full = premium_discount(synthetic_bars, left=2, right=2)
    for k in (60, 250, 480):
        assert_frame_equal(full.iloc[:k], premium_discount(synthetic_bars.iloc[:k], 2, 2))
