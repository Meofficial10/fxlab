"""Swing (fractal pivot) detection — the causal foundation for structure & SMC (Phase 3).

A **swing high** at bar ``i`` (span ``left``/``right``) is a bar whose high is *strictly*
greater than the highs of the ``left`` bars before and the ``right`` bars after it; a
**swing low** is the mirror with lows. Strictness (``>`` / ``<``) means flat/equal
neighbours never qualify — no ambiguous "double top" pivots.

The catch that makes this leakage-safe: a pivot at bar ``i`` cannot be *known* until the
``right`` bars after it have closed, i.e. at bar ``i + right``. So the useful primitive is
not "where are the pivots" but **"which pivot is confirmed as of bar t"**:
:func:`confirmed_swings` exposes, at each bar ``t``, only pivots whose confirmation bar
``i + right <= t``. That output is strictly causal and therefore future-invariant
(appending bars to the right never changes a past row) — regression-tested.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SwingConfig:
    left: int = 2
    right: int = 2

    def __post_init__(self) -> None:
        if self.left < 1 or self.right < 1:
            raise ValueError("left and right must both be >= 1")


def pivot_highs_lows(bars: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Strict fractal pivots. Returns ``pivot_high``/``pivot_low`` = the pivot bar's
    price where it qualifies, else NaN. NOTE: a pivot at ``i`` peeks ``right`` bars into
    the future, so it is only *known* at ``i + right`` — use :func:`confirmed_swings` for
    anything a strategy consumes.
    """
    SwingConfig(left, right)  # validate
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)

    lo_i, hi_i = left, n - right
    if hi_i > lo_i:
        ch, cl = high[lo_i:hi_i], low[lo_i:hi_i]
        is_ph = np.ones(hi_i - lo_i, dtype=bool)
        is_pl = np.ones(hi_i - lo_i, dtype=bool)
        for j in range(1, left + 1):
            is_ph &= ch > high[lo_i - j : hi_i - j]
            is_pl &= cl < low[lo_i - j : hi_i - j]
        for j in range(1, right + 1):
            is_ph &= ch > high[lo_i + j : hi_i + j]
            is_pl &= cl < low[lo_i + j : hi_i + j]
        ph[lo_i:hi_i] = np.where(is_ph, ch, np.nan)
        pl[lo_i:hi_i] = np.where(is_pl, cl, np.nan)

    return pd.DataFrame({"pivot_high": ph, "pivot_low": pl}, index=bars.index)


def confirmed_swings(bars: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """As-of-bar-``t`` view of the most recent CONFIRMED swing high and low (causal).

    Columns (indexed like ``bars``):
      * ``last_swing_high`` / ``last_swing_low`` — price of the most recent confirmed pivot,
      * ``last_swing_high_idx`` / ``last_swing_low_idx`` — its integer bar position (-1 if none).

    A pivot at bar ``p`` is folded in exactly at bar ``p + right`` (its confirmation),
    then forward-filled. Row ``t`` therefore depends only on bars ``<= t``.
    """
    piv = pivot_highs_lows(bars, left, right)
    ph = piv["pivot_high"].to_numpy()
    pl = piv["pivot_low"].to_numpy()
    n = len(bars)

    last_sh = np.full(n, np.nan)
    last_sl = np.full(n, np.nan)
    last_sh_idx = np.full(n, -1, dtype=int)
    last_sl_idx = np.full(n, -1, dtype=int)

    cur_sh = np.nan
    cur_sl = np.nan
    cur_sh_i = -1
    cur_sl_i = -1
    for t in range(n):
        p = t - right  # a pivot at p is confirmed now (p + right == t)
        if p >= 0:
            if not np.isnan(ph[p]):
                cur_sh, cur_sh_i = ph[p], p
            if not np.isnan(pl[p]):
                cur_sl, cur_sl_i = pl[p], p
        last_sh[t], last_sh_idx[t] = cur_sh, cur_sh_i
        last_sl[t], last_sl_idx[t] = cur_sl, cur_sl_i

    return pd.DataFrame(
        {
            "last_swing_high": last_sh,
            "last_swing_high_idx": last_sh_idx,
            "last_swing_low": last_sl,
            "last_swing_low_idx": last_sl_idx,
        },
        index=bars.index,
    )
