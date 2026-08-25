"""THE leakage regression the charter mandates: future-invariance.

A causal transform computed over ``bars[:k]`` must be byte-identical to the same
transform over the full series then sliced to ``[:k]``. Equivalently: appending
arbitrary FUTURE bars must never change any PAST output. If it does, the feature
peeks ahead and any backtest built on it is invalid.
"""

from __future__ import annotations

import pandas as pd

from fxlab.data.ingest_dukascopy import generate_synthetic_bars
from fxlab.data.resample import mtf_align, resample_ohlcv
from fxlab.labeling.triple_barrier import atr_wilder


def test_atr_is_future_invariant():
    bars = generate_synthetic_bars("EURUSD", "M5", n_bars=500, seed=3)
    k = 300
    full = atr_wilder(bars, window=14)
    partial = atr_wilder(bars.iloc[:k], window=14)
    pd.testing.assert_series_equal(full.iloc[:k], partial, check_names=False)


def test_mtf_alignment_is_future_invariant():
    """Append 200 future M5 bars; the alignment over the first K rows must not move."""
    bars = generate_synthetic_bars("EURUSD", "M5", n_bars=600, seed=11)
    k = 400

    h1_full = resample_ohlcv(bars, "H1")
    aligned_full = mtf_align(bars, h1_full, "H1").iloc[:k]

    past = bars.iloc[:k]
    h1_past = resample_ohlcv(past, "H1")
    aligned_past = mtf_align(past, h1_past, "H1")

    pd.testing.assert_frame_equal(aligned_full, aligned_past)


def test_resample_of_past_matches_full_resample_on_completed_bars():
    bars = generate_synthetic_bars("EURUSD", "M5", n_bars=600, seed=5)
    k = 480  # 480 * 5min = 40h -> boundary lands on a clean H1 close
    full = resample_ohlcv(bars, "H1")
    past = resample_ohlcv(bars.iloc[:k], "H1")
    # Drop past's final H1 bar (it may be mid-formation); every EARLIER, fully-closed
    # H1 bar must equal the same bar computed from the full series.
    common = past.index[:-1].intersection(full.index)
    assert len(common) > 0
    pd.testing.assert_frame_equal(
        past.loc[common], full.loc[common], check_like=True
    )
