"""Bar-integrity validation: hard errors vs soft (weekend-gap) warnings."""

from __future__ import annotations

import numpy as np
import pandas as pd

from fxlab.data.validate import validate_bars


def test_clean_bars_validate_ok(synthetic_bars):
    rep = validate_bars(synthetic_bars, "M5", "EURUSD")
    assert rep.ok, rep.summary()
    assert rep.n_rows == len(synthetic_bars)


def test_duplicate_timestamps_are_hard_error(synthetic_bars):
    dup = pd.concat([synthetic_bars, synthetic_bars.iloc[[0]]]).sort_index()
    rep = validate_bars(dup, "M5", "EURUSD")
    assert not rep.ok
    assert any("duplicate" in e for e in rep.errors)


def test_non_monotonic_index_is_hard_error(synthetic_bars):
    rev = synthetic_bars.iloc[::-1]
    rep = validate_bars(rev, "M5", "EURUSD")
    assert not rep.ok
    assert any("monoton" in e for e in rep.errors)


def test_nan_and_nonpositive_and_high_lt_low(synthetic_bars):
    bad = synthetic_bars.copy()
    bad.iloc[0, bad.columns.get_loc("close")] = np.nan
    bad.iloc[1, bad.columns.get_loc("open")] = -1.0
    # force high < low on row 2
    bad.iloc[2, bad.columns.get_loc("high")] = 0.5
    bad.iloc[2, bad.columns.get_loc("low")] = 0.9
    rep = validate_bars(bad, "M5", "EURUSD")
    assert not rep.ok
    joined = " ".join(rep.errors)
    assert "NaN" in joined and "non-positive" in joined and "high < low" in joined


def test_weekend_gaps_are_warnings_not_errors():
    # Fri 22:00 -> Mon 00:00 gap should be classified weekend, not an error.
    idx = pd.to_datetime(
        ["2020-01-03 21:55", "2020-01-06 00:00", "2020-01-06 00:05"]
    ).tz_localize("UTC")
    df = pd.DataFrame(
        {"open": 1.1, "high": 1.11, "low": 1.09, "close": 1.10, "volume": 10.0}, index=idx
    )
    rep = validate_bars(df, "M5", "EURUSD")
    assert rep.ok  # gaps never fail validation
    assert rep.n_gaps >= 1
    assert rep.n_weekend_gaps >= 1


def test_empty_frame_is_error():
    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df.index = pd.DatetimeIndex([], tz="UTC")
    rep = validate_bars(df, "M5", "EURUSD")
    assert not rep.ok
