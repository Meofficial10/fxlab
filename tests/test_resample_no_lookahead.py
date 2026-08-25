"""Resampling correctness and leakage-safe multi-timeframe alignment."""

from __future__ import annotations

import pandas as pd

from fxlab.data.resample import mtf_align, resample_ohlcv


def test_resample_aggregates_ohlcv_correctly(m5_hour):
    h1 = resample_ohlcv(m5_hour, "H1")
    assert len(h1) == 1
    row = h1.iloc[0]
    assert row["open"] == m5_hour["open"].iloc[0]
    assert row["close"] == m5_hour["close"].iloc[-1]
    assert row["high"] == m5_hour["high"].max()
    assert row["low"] == m5_hour["low"].min()
    assert row["volume"] == m5_hour["volume"].sum()
    assert h1.index[0] == m5_hour.index[0]  # left-labelled to the open


def test_mtf_align_excludes_the_not_yet_closed_htf_bar():
    """The H1 bar covering the current M5 timestamp must NOT be attached (it hasn't closed)."""
    m5 = _three_hours_of_m5()
    h1 = resample_ohlcv(m5, "H1")
    aligned = mtf_align(m5, h1, "H1")
    assert aligned.index.equals(m5.index)

    t0, t1, t2 = pd.Timestamp("2020-01-06 00:00", tz="UTC"), \
        pd.Timestamp("2020-01-06 01:00", tz="UTC"), pd.Timestamp("2020-01-06 02:00", tz="UTC")

    # During the first hour, no H1 bar has closed yet -> all NaN.
    assert aligned.loc[t0, "H1_close"] != aligned.loc[t0, "H1_close"]  # NaN
    # At 01:00 the 00:00 H1 bar has just closed -> attach it (not the forming 01:00 bar).
    assert aligned.loc[t1, "H1_open"] == h1.loc[t0, "open"]
    assert aligned.loc[t1, "H1_close"] == h1.loc[t0, "close"]
    # At 02:00 we see the 01:00 H1 bar.
    assert aligned.loc[t2, "H1_open"] == h1.loc[t1, "open"]


def test_mtf_align_prefix_is_configurable():
    m5 = _three_hours_of_m5()
    h1 = resample_ohlcv(m5, "H1")
    aligned = mtf_align(m5, h1, "H1", prefix="htf_")
    assert "htf_close" in aligned.columns


def _three_hours_of_m5():
    from fxlab.data.schema import ensure_bars

    idx = pd.date_range("2020-01-06 00:00", periods=36, freq="5min", tz="UTC")
    import numpy as np

    close = 1.10 + np.arange(36) * 0.0001
    open_ = np.r_[1.10, close[:-1]]
    df = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 0.0002,
            "low": np.minimum(open_, close) - 0.0002,
            "close": close,
            "volume": 1.0,
        },
        index=idx,
    )
    return ensure_bars(df, "EURUSD", "M5")
