"""Shared fixtures. ``fxlab`` is importable after ``python -m uv sync`` (editable install);
a src fallback keeps bare ``pytest`` working too."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fxlab.data.schema import ensure_bars  # noqa: E402


@pytest.fixture
def synthetic_bars():
    from fxlab.data.ingest_dukascopy import generate_synthetic_bars

    return generate_synthetic_bars("EURUSD", "M5", n_bars=600, seed=7)


@pytest.fixture
def m5_hour():
    """Twelve M5 bars spanning exactly one clean H1 window, with known aggregates."""
    idx = pd.date_range("2020-01-06 00:00", periods=12, freq="5min", tz="UTC")
    close = np.linspace(1.1000, 1.1022, 12)
    open_ = np.r_[1.1000, close[:-1]]
    high = np.maximum(open_, close) + 0.0003
    low = np.minimum(open_, close) - 0.0002
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": np.arange(1, 13.0)},
        index=idx,
    )
    return ensure_bars(df, "EURUSD", "M5")
