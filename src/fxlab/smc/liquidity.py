"""Liquidity — equal highs/lows (pools) and sweeps/stop-runs (Phase 3, SMC).

Resting stop orders cluster just beyond obvious swing points; SMC calls that *liquidity*.
Two objective detectors, both built on CONFIRMED swings so they are causal:

  * **Equal highs / equal lows** — a newly confirmed swing high (low) within ``tol`` of the
    previous confirmed swing high (low): a buy-side (sell-side) liquidity pool.
  * **Sweep / stop-run** — a bar that trades *beyond* the most recent confirmed swing but
    *closes back through* it: liquidity taken, then rejected. A wick above a swing high that
    closes below it is a bearish sweep (``-1``); a wick below a swing low that closes above
    it is a bullish sweep (``+1``).

A sweep at bar ``t`` uses the swing confirmed as-of ``t`` and bar ``t``'s own high/low/close,
so it reads only bars ``<= t`` — causal and future-invariant. These are **detectors**; any
edge is measured in P4, net of costs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..structure.swings import confirmed_swings


def liquidity_sweeps(bars: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Detect stop-runs against the most recent confirmed swing. Columns:

    ``sweep_dir`` in {-1, 0, +1} (-1 swept buy-side above a swing high then closed below;
    +1 swept sell-side below a swing low then closed above); ``swept_level`` = the swing
    price taken (NaN when none); ``swept_idx`` = that swing's bar position (-1 when none).
    """
    cs = confirmed_swings(bars, left, right)
    sh = cs["last_swing_high"].to_numpy()
    sl = cs["last_swing_low"].to_numpy()
    sh_i = cs["last_swing_high_idx"].to_numpy()
    sl_i = cs["last_swing_low_idx"].to_numpy()
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")
    n = len(bars)

    sweep_dir = np.zeros(n, dtype="int64")
    swept_level = np.full(n, np.nan)
    swept_idx = np.full(n, -1, dtype="int64")

    for t in range(n):
        # Buy-side sweep above a swing high takes priority on a rare both-sides outside bar.
        if sh_i[t] != -1 and high[t] > sh[t] and close[t] < sh[t]:
            sweep_dir[t] = -1
            swept_level[t] = sh[t]
            swept_idx[t] = sh_i[t]
        elif sl_i[t] != -1 and low[t] < sl[t] and close[t] > sl[t]:
            sweep_dir[t] = 1
            swept_level[t] = sl[t]
            swept_idx[t] = sl_i[t]

    return pd.DataFrame(
        {"sweep_dir": sweep_dir, "swept_level": swept_level, "swept_idx": swept_idx},
        index=bars.index,
    )


def equal_highs_lows(
    bars: pd.DataFrame, left: int = 2, right: int = 2, tol: float = 0.0
) -> pd.DataFrame:
    """Flag liquidity pools where a newly confirmed swing matches the previous one within
    ``tol`` (absolute price). Columns ``equal_high`` / ``equal_low`` (bool), stamped on the
    confirmation bar of the second swing of the pair.
    """
    if tol < 0:
        raise ValueError("tol must be >= 0")
    cs = confirmed_swings(bars, left, right)
    sh = cs["last_swing_high"].to_numpy()
    sl = cs["last_swing_low"].to_numpy()
    sh_i = cs["last_swing_high_idx"].to_numpy()
    sl_i = cs["last_swing_low_idx"].to_numpy()
    n = len(bars)

    equal_high = np.zeros(n, dtype=bool)
    equal_low = np.zeros(n, dtype=bool)

    prev_sh, seen_sh = np.nan, -1
    prev_sl, seen_sl = np.nan, -1
    for t in range(n):
        if sh_i[t] != -1 and sh_i[t] != seen_sh:
            if not np.isnan(prev_sh) and abs(sh[t] - prev_sh) <= tol:
                equal_high[t] = True
            prev_sh, seen_sh = sh[t], sh_i[t]
        if sl_i[t] != -1 and sl_i[t] != seen_sl:
            if not np.isnan(prev_sl) and abs(sl[t] - prev_sl) <= tol:
                equal_low[t] = True
            prev_sl, seen_sl = sl[t], sl_i[t]

    return pd.DataFrame({"equal_high": equal_high, "equal_low": equal_low}, index=bars.index)
