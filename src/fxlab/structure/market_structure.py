"""Objective market structure: HH/HL/LH/LL, BOS, CHoCH (Phase 3).

Built entirely on CONFIRMED swings (see :mod:`fxlab.structure.swings`) plus the current
close, so it is causal by construction. The two structural break events, defined
objectively:

  * **BOS** (Break of Structure) — a close beyond the most recent confirmed swing *in the
    direction of the prevailing trend*: trend continuation.
  * **CHoCH** (Change of Character) — the *first* close beyond structure *against* the
    prevailing trend: the earliest objective hint of a reversal.

A given confirmed swing level can only be broken once (it is then "consumed" until a new
swing of that type confirms), so events fire on the break bar, not repeatedly. Swing
labels HH/HL/LH/LL are stamped on the bar a new swing confirms.

These are **detectors**, not signals — they describe structure. Whether any of it carries
expectancy is measured in P4, net of costs, against the P2 baseline.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .swings import confirmed_swings

EVENTS = ("bos_up", "bos_down", "choch_up", "choch_down")


def market_structure(bars: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Return per-bar structure state. Columns (indexed like ``bars``):

    ``event`` in {"", bos_up, bos_down, choch_up, choch_down};
    ``trend_state`` in {"none","bull","bear"} (state AFTER the bar);
    ``new_swing_high`` in {"","HH","LH"}; ``new_swing_low`` in {"","HL","LL"};
    plus ``last_swing_high`` / ``last_swing_low`` (the confirmed levels used).
    """
    cs = confirmed_swings(bars, left, right)
    sh = cs["last_swing_high"].to_numpy()
    sl = cs["last_swing_low"].to_numpy()
    sh_i = cs["last_swing_high_idx"].to_numpy()
    sl_i = cs["last_swing_low_idx"].to_numpy()
    close = bars["close"].to_numpy(dtype="float64")
    n = len(bars)

    event = np.full(n, "", dtype=object)
    new_sh = np.full(n, "", dtype=object)
    new_sl = np.full(n, "", dtype=object)
    trend = np.full(n, "", dtype=object)

    state = "none"
    prev_sh = np.nan
    prev_sl = np.nan
    seen_sh_i = -1
    seen_sl_i = -1
    broken_sh_i = -1
    broken_sl_i = -1

    for t in range(n):
        # Newly-confirmed swing high/low -> label relative to the previous one of its kind.
        if sh_i[t] != -1 and sh_i[t] != seen_sh_i:
            if not np.isnan(prev_sh):
                new_sh[t] = "HH" if sh[t] > prev_sh else "LH"
            prev_sh, seen_sh_i = sh[t], sh_i[t]
        if sl_i[t] != -1 and sl_i[t] != seen_sl_i:
            if not np.isnan(prev_sl):
                new_sl[t] = "HL" if sl[t] > prev_sl else "LL"
            prev_sl, seen_sl_i = sl[t], sl_i[t]

        # Break detection: close beyond a FRESH (un-consumed) confirmed level.
        # sh > sl, so a single close cannot break both; if/elif is exhaustive.
        if sh_i[t] != -1 and sh_i[t] != broken_sh_i and close[t] > sh[t]:
            event[t] = "choch_up" if state == "bear" else "bos_up"
            state, broken_sh_i = "bull", sh_i[t]
        elif sl_i[t] != -1 and sl_i[t] != broken_sl_i and close[t] < sl[t]:
            event[t] = "choch_down" if state == "bull" else "bos_down"
            state, broken_sl_i = "bear", sl_i[t]

        trend[t] = state

    return pd.DataFrame(
        {
            "event": event,
            "trend_state": trend,
            "new_swing_high": new_sh,
            "new_swing_low": new_sl,
            "last_swing_high": sh,
            "last_swing_low": sl,
        },
        index=bars.index,
    )


def annotate_structure(bars: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Convenience: original bars joined with the market-structure columns."""
    ms = market_structure(bars, left, right)
    out = bars.join(ms)
    out.attrs.update(bars.attrs)
    return out
