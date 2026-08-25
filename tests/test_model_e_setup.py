"""Model E — session opening-range breakout setup.

Hand-crafted HOURLY fixtures (open irrelevant to Model E — it reads high/low/close + the bar's
UTC hour — so the helper sets open = close) pin the objective rules against a compact test session
window ``[7, 10)`` (three in-session hours 7,8,9). A future-invariance guard proves the stateful
session/opening-range tracking is still strictly causal.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fxlab.data.schema import _in_window
from fxlab.setups.model_e_session_breakout import ModelESessionBreakout


def _hourly(start: str, highs, lows, closes) -> pd.DataFrame:
    highs = np.asarray(highs, dtype="float64")
    lows = np.asarray(lows, dtype="float64")
    closes = np.asarray(closes, dtype="float64")
    idx = pd.date_range(start, periods=len(highs), freq="1h", tz="UTC")
    df = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes}, index=idx)
    return _ensure(df)


def _ensure(df):
    from fxlab.data.schema import ensure_bars

    return ensure_bars(df, "TEST", "H1")


# Session [7,10): OR (or_bars=1) is the hour-07 bar; hours 08,09 watch for the first close-break.
# Bars start 06:00 -> indices 0..5 = hours 06,07,08,09,10,11.
def test_long_breakout_fires_once_at_first_close_above_or():
    bars = _hourly(
        "2020-01-06 06:00",
        highs=[9.6, 10.0, 9.6, 10.6, 12, 12],
        lows=[9.4, 9.0, 9.4, 9.4, 11, 11],
        closes=[9.5, 9.5, 9.5, 10.5, 11.5, 11.5],  # idx3 (h09) is the first close > OR high 10.0
    )
    idx, side = ModelESessionBreakout(start_hour=7, end_hour=10, or_bars=1).generate(bars)
    assert idx.tolist() == [3]
    assert side.tolist() == [1]
    assert 1 not in idx.tolist()  # the OR bar itself never fires


def test_short_breakout_mirror():
    bars = _hourly(
        "2020-01-06 06:00",
        highs=[9.6, 10.0, 9.6, 9.0, 9, 9],
        lows=[9.4, 9.0, 9.4, 8.4, 7, 7],
        closes=[9.5, 9.5, 9.5, 8.5, 8, 8],  # idx3 (h09) is the first close < OR low 9.0
    )
    idx, side = ModelESessionBreakout(start_hour=7, end_hour=10, or_bars=1).generate(bars)
    assert idx.tolist() == [3]
    assert side.tolist() == [-1]


def test_range_held_all_session_gives_no_signal():
    bars = _hourly(
        "2020-01-06 06:00",
        highs=[9.8, 10.0, 9.9, 9.95, 9.9, 9.9],
        lows=[9.2, 9.0, 9.3, 9.4, 9.3, 9.3],
        closes=[9.5, 9.5, 9.6, 9.7, 9.5, 9.5],  # every in-session close stays within OR [9,10]
    )
    idx, _ = ModelESessionBreakout(start_hour=7, end_hour=10, or_bars=1).generate(bars)
    assert idx.tolist() == []


def test_breakout_after_session_end_is_ignored():
    # Closes stay inside the OR through hours 08,09; the break to 11.0 only happens at hour 10,
    # which is OUTSIDE [7,10) -> no signal (the session's watch window has closed).
    bars = _hourly(
        "2020-01-06 06:00",
        highs=[9.6, 10.0, 9.6, 9.6, 12.0, 12.0, 12.0],
        lows=[9.4, 9.0, 9.4, 9.4, 11.0, 11.0, 11.0],
        closes=[9.5, 9.5, 9.5, 9.6, 11.0, 11.0, 11.0],
    )
    idx, _ = ModelESessionBreakout(start_hour=7, end_hour=10, or_bars=1).generate(bars)
    assert idx.tolist() == []


def test_new_session_resets_the_range():
    # Two sessions (day1 h7-9 at idx 1-3, day2 h7-9 at idx 25-27). Day1 breaks up, day2 breaks down;
    # each uses its OWN opening range -> proves the range/fired state resets per session.
    n = 31
    highs = np.full(n, 9.8)
    lows = np.full(n, 9.2)
    closes = np.full(n, 9.5)
    highs[1], lows[1] = 10.0, 9.0          # session 1 OR = [9,10]
    closes[3] = 10.5                       # session 1: LONG at idx 3
    highs[25], lows[25] = 10.0, 9.0        # session 2 OR = [9,10]
    closes[27] = 8.5                       # session 2: SHORT at idx 27
    bars = _hourly("2020-01-06 06:00", highs=highs, lows=lows, closes=closes)
    # sanity: the two starts really are the only in-session hour-7 bars
    assert bars.index[1].hour == 7 and bars.index[25].hour == 7

    idx, side = ModelESessionBreakout(start_hour=7, end_hour=10, or_bars=1).generate(bars)
    assert idx.tolist() == [3, 27]
    assert side.tolist() == [1, -1]


def test_first_breakout_in_a_session_wins():
    # Up-break at idx2 then a down-break at idx3; only the first (LONG) is taken.
    bars = _hourly(
        "2020-01-06 06:00",
        highs=[9.6, 10.0, 10.6, 9.0, 9, 9],
        lows=[9.4, 9.0, 9.4, 8.4, 8, 8],
        closes=[9.5, 9.5, 10.5, 8.5, 8.5, 8.5],
    )
    idx, side = ModelESessionBreakout(start_hour=7, end_hour=10, or_bars=1).generate(bars)
    assert idx.tolist() == [2]
    assert side.tolist() == [1]


def test_or_bars_two_widens_the_opening_range():
    # or_bars=2 -> OR spans hours 07 AND 08; a close above hour-07's high but below the
    # 2-bar range high must NOT fire until a close clears the wider range.
    bars = _hourly(
        "2020-01-06 06:00",
        highs=[9.6, 10.0, 10.5, 10.4, 10.8, 10.8],  # 2-bar OR high = max(10.0, 10.5) = 10.5
        lows=[9.4, 9.0, 9.5, 9.5, 9.5, 9.5],
        closes=[9.5, 9.5, 10.2, 10.3, 10.7, 10.7],  # idx3 close 10.3 < 10.5 (no); idx4 10.7 > 10.5
    )
    # NOTE session must be long enough: widen window to [7,12) so hours 7..11 are in-session.
    idx, side = ModelESessionBreakout(start_hour=7, end_hour=12, or_bars=2).generate(bars)
    assert idx.tolist() == [4]
    assert side.tolist() == [1]


def test_future_invariant_on_synthetic(synthetic_bars):
    # Stateful (session runs, opening ranges, fired flags) but strictly causal: signals at indices
    # < k depend only on bars <= their own position, so a prefix reproduces the full run's prefix.
    setup = ModelESessionBreakout()  # default London 07-16 UTC
    idx_full, side_full = setup.generate(synthetic_bars)
    assert len(idx_full) > 0  # non-vacuous: the mechanism actually fires on this data

    for k in (150, 350, 550):
        idx_k, side_k = setup.generate(synthetic_bars.iloc[:k])
        mask = idx_full < k
        assert np.array_equal(idx_k, idx_full[mask])
        assert np.array_equal(side_k, side_full[mask])


def test_window_matches_schema_in_window_semantics():
    # The setup's vectorised membership must agree with the platform's scalar _in_window, incl.
    # the midnight-wrapping Asia-style window (start > end).
    for start, end in [(7, 16), (23, 8), (0, 24)]:
        setup = ModelESessionBreakout(start_hour=start, end_hour=end) if start != end else None
        if setup is None:
            continue
        hours = np.arange(24)
        vec = setup._in_session(hours)
        scalar = np.array([_in_window(h, start, end) for h in range(24)])
        assert np.array_equal(vec, scalar)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"or_bars": 0},
        {"max_watch": 0},
        {"start_hour": 24},
        {"end_hour": 25},
        {"start_hour": 8, "end_hour": 8},
    ],
)
def test_config_validation_rejects_bad_params(kwargs):
    with pytest.raises(ValueError):
        ModelESessionBreakout(**kwargs)
