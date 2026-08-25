"""Order block detector — origin candle selection, direction, no-origin case, invariance."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from fxlab.data.schema import ensure_bars
from fxlab.smc.order_blocks import OrderBlockConfig, order_blocks


def _bars(open_, high, low, close) -> pd.DataFrame:
    idx = pd.date_range("2020-01-06", periods=len(open_), freq="5min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": np.asarray(open_, "float64"),
            "high": np.asarray(high, "float64"),
            "low": np.asarray(low, "float64"),
            "close": np.asarray(close, "float64"),
        },
        index=idx,
    )
    return ensure_bars(df, "TEST", "M5")


def test_config_validation():
    with pytest.raises(ValueError):
        OrderBlockConfig(max_lookback=0)


def test_bullish_ob_is_last_down_candle_before_up_displacement():
    # bars 0..5 quiet-up; bar 6 a small DOWN candle; bar 7 a big UP displacement.
    open_ = [100.0] * 6 + [100.1, 100.0]
    close = [100.1] * 6 + [100.0, 103.0]
    high = [100.2] * 6 + [100.2, 103.1]
    low = [99.9] * 6 + [99.9, 99.9]
    ob = order_blocks(_bars(open_, high, low, close), atr_window=3, body_mult=1.5)

    assert ob["ob_dir"].iloc[7] == 1  # bullish/demand
    assert ob["ob_src_idx"].iloc[7] == 6  # the down candle
    assert ob["ob_low"].iloc[7] == 99.9
    assert ob["ob_high"].iloc[7] == 100.2
    assert (ob["ob_dir"].iloc[:7] == 0).all()  # nothing stamped before the displacement


def test_bearish_ob_is_last_up_candle_before_down_displacement():
    # bar 6 a small UP candle; bar 7 a big DOWN displacement -> bearish/supply OB at 7.
    open_ = [100.0] * 6 + [100.0, 100.0]
    close = [99.9] * 6 + [100.1, 97.0]
    high = [100.1] * 6 + [100.2, 100.1]
    low = [99.8] * 6 + [99.9, 96.9]
    ob = order_blocks(_bars(open_, high, low, close), atr_window=3, body_mult=1.5)

    assert ob["ob_dir"].iloc[7] == -1
    assert ob["ob_src_idx"].iloc[7] == 6
    assert ob["ob_low"].iloc[7] == 99.9
    assert ob["ob_high"].iloc[7] == 100.2


def test_no_opposite_candle_in_lookback_means_no_block():
    # Every candle before the up-displacement is itself UP -> no down candle to anchor to.
    open_ = [100.0] * 8
    close = [100.1] * 7 + [103.0]
    high = [100.2] * 7 + [103.1]
    low = [99.9] * 8
    ob = order_blocks(_bars(open_, high, low, close), atr_window=3, body_mult=1.5, max_lookback=3)
    assert ob["ob_dir"].iloc[-1] == 0  # displacement fired, but no valid origin candle
    assert ob["ob_src_idx"].iloc[-1] == -1


def test_order_blocks_future_invariant(synthetic_bars):
    full = order_blocks(synthetic_bars, atr_window=14, body_mult=1.5)
    for k in (60, 250, 480):
        assert_frame_equal(full.iloc[:k], order_blocks(synthetic_bars.iloc[:k], 14, 1.5))
