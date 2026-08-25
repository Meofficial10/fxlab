"""Market structure — objective BOS / CHoCH events on a hand-verified candle path.

Every pivot, its confirmation bar, and every event below was traced by hand against the
state machine in :mod:`fxlab.structure.market_structure`. See the inline ledger in
``test_bos_up_then_choch_down`` for the full derivation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from fxlab.data.schema import ensure_bars
from fxlab.structure.market_structure import EVENTS, market_structure


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


def test_bos_up_then_choch_down():
    #  t:      0    1    2    3    4    5    6    7    8    9    10    11
    high = [10,  11,  12,  15,  12,  11,  17,  15,  14,  13,  12,   11]
    low = [ 8,   9,  10,  11,  10,   9,  12,  11,   9,  10,  11,    6]
    close = [ 9,  10,  11,  13,  11,  10,  16,  13,  12,  11,  11.5,  7]
    # Pivot highs (left=right=2): idx3(=15) confirm@5, idx6(=17) confirm@8.
    # Pivot lows:                 idx5(=9)  confirm@7, idx8(=9)  confirm@10.
    # t6: close 16 > confirmed high 15 (fresh), state none -> BOS_UP, state->bull.
    # t8: new high 17 > prev 15 -> "HH".
    # t10: new low 9 (not > prev 9) -> "LL".
    # t11: close 7 < confirmed low 9 (fresh) while bull -> CHoCH_DOWN, state->bear.
    ms = market_structure(_bars(high, low, close), left=2, right=2)

    ev = ms["event"].tolist()
    assert ev[6] == "bos_up"
    assert ev[11] == "choch_down"
    assert all(ev[t] == "" for t in range(12) if t not in (6, 11))
    assert set(ev) <= {"", *EVENTS}

    tr = ms["trend_state"].tolist()
    assert tr[:6] == ["none"] * 6
    assert tr[6:11] == ["bull"] * 5
    assert tr[11] == "bear"

    assert ms["new_swing_high"].tolist()[8] == "HH"
    assert ms["new_swing_low"].tolist()[10] == "LL"
    assert ms["last_swing_high"].iloc[6] == 15.0
    assert ms["last_swing_high"].iloc[11] == 17.0
    assert ms["last_swing_low"].iloc[11] == 9.0


def test_first_break_from_none_is_bos_not_choch():
    # A downtrend forms one confirmed swing low (idx3=5, confirm@5); the first close
    # below it (t6) happens from state 'none' -> must be BOS_DOWN, never CHoCH.
    high = [12, 11, 10, 9, 8, 9, 7]
    low = [10, 9, 8, 5, 7, 8, 3]
    close = [11, 10, 9, 6, 7, 8, 4]
    ms = market_structure(_bars(high, low, close), left=2, right=2)

    ev = ms["event"].tolist()
    assert ev[6] == "bos_down"
    assert all(e != "choch_down" and e != "choch_up" for e in ev)
    assert all(ev[t] == "" for t in range(7) if t != 6)

    tr = ms["trend_state"].tolist()
    assert tr[:6] == ["none"] * 6
    assert tr[6] == "bear"


def test_market_structure_future_invariant(synthetic_bars):
    # The whole detector (swings + break state machine) must be future-invariant.
    full = market_structure(synthetic_bars, left=2, right=2)
    for k in (60, 250, 480):
        trunc = market_structure(synthetic_bars.iloc[:k], left=2, right=2)
        assert_frame_equal(full.iloc[:k], trunc)
