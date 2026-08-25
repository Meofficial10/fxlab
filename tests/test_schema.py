"""Schema coercion, timeframe helpers, and session tagging."""

from __future__ import annotations

import pandas as pd
import pytest

from fxlab.data.schema import (
    OHLCV,
    _in_window,
    add_session_column,
    bar_close_index,
    ensure_bars,
    tag_sessions,
    timeframe_to_timedelta,
)


def test_ensure_bars_is_idempotent_and_utc(synthetic_bars):
    once = synthetic_bars
    twice = ensure_bars(once, "EURUSD", "M5")
    pd.testing.assert_frame_equal(once, twice)
    assert str(once.index.tz) == "UTC"
    assert once.index.name == "ts_open"
    assert list(once.columns) == OHLCV
    assert once.attrs["symbol"] == "EURUSD"


def test_ensure_bars_localizes_naive_index_and_defaults_volume():
    idx = pd.date_range("2021-03-01", periods=3, freq="h")  # tz-naive
    df = pd.DataFrame({"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.05}, index=idx)
    out = ensure_bars(df, "EURUSD", "H1")
    assert str(out.index.tz) == "UTC"
    assert (out["volume"] == 0.0).all()  # defaulted
    assert out["open"].dtype == "float64"


def test_ensure_bars_rejects_non_datetime_index():
    df = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]}, index=[0])
    with pytest.raises(TypeError):
        ensure_bars(df)


def test_timeframe_helpers():
    assert timeframe_to_timedelta("H1") == pd.Timedelta(hours=1)
    assert timeframe_to_timedelta("M5") == pd.Timedelta(minutes=5)
    with pytest.raises(ValueError):
        timeframe_to_timedelta("W1")


def test_bar_close_index_is_open_plus_one_timeframe():
    idx = pd.date_range("2020-01-06", periods=4, freq="h", tz="UTC")
    closes = bar_close_index(idx, "H1")
    assert (closes == idx + pd.Timedelta(hours=1)).all()


def test_in_window_wraps_past_midnight():
    # Asia 23..8 wraps midnight
    assert _in_window(23, 23, 8)
    assert _in_window(3, 23, 8)
    assert not _in_window(8, 23, 8)  # half-open upper bound
    # London 7..16 no wrap
    assert _in_window(7, 7, 16)
    assert not _in_window(16, 7, 16)


def test_tag_sessions_priority_first_match_then_off():
    sessions = [
        {"name": "Overlap", "start_hour": 12, "end_hour": 16},
        {"name": "London", "start_hour": 7, "end_hour": 16},
    ]
    idx = pd.to_datetime(
        ["2020-01-06 08:00", "2020-01-06 13:00", "2020-01-06 20:00"]
    ).tz_localize("UTC")
    tags = tag_sessions(idx, sessions)
    assert list(tags) == ["London", "Overlap", "Off"]  # 13:00 hits Overlap first (priority)


def test_add_session_column_preserves_rows(m5_hour):
    out = add_session_column(m5_hour, [{"name": "London", "start_hour": 0, "end_hour": 8}])
    assert "session" in out.columns
    assert len(out) == len(m5_hour)
