"""Premium / discount — where price sits in the current dealing range (Phase 3, SMC).

The *dealing range* is spanned by the most recent confirmed swing low and swing high. Its
midpoint is *equilibrium*; the upper half is *premium* (favour selling), the lower half is
*discount* (favour buying). We report the fractional position

    pos(t) = (close(t) - range_low) / (range_high - range_low)

which is 0 at the low, 1 at the high, and may fall outside ``[0, 1]`` once price extends
beyond the range. Range edges come from :func:`fxlab.structure.swings.confirmed_swings`, so
row ``t`` depends only on bars ``<= t`` — causal and future-invariant. Before both a swing
high and a swing low have confirmed, the zone is undefined (empty string, ``pos`` NaN).

A **detector**, not a signal: premium/discount only contextualises other setups; its edge is
a P4 question, net of costs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..structure.swings import confirmed_swings


def premium_discount(
    bars: pd.DataFrame, left: int = 2, right: int = 2, eq_band: float = 0.0
) -> pd.DataFrame:
    """Classify each bar's close within the current dealing range. Columns:

    ``pd_zone`` in {"", "premium", "equilibrium", "discount"}; ``pd_pos`` = fractional
    position in the range (NaN when undefined); ``range_low`` / ``range_high`` / ``eq`` =
    the confirmed swing low, swing high, and their midpoint. ``eq_band`` widens the
    equilibrium band symmetrically around 0.5 (e.g. 0.1 -> [0.4, 0.6] counts as equilibrium).
    """
    if not 0.0 <= eq_band < 0.5:
        raise ValueError("eq_band must be in [0, 0.5)")
    cs = confirmed_swings(bars, left, right)
    sh = cs["last_swing_high"].to_numpy()
    sl = cs["last_swing_low"].to_numpy()
    sh_i = cs["last_swing_high_idx"].to_numpy()
    sl_i = cs["last_swing_low_idx"].to_numpy()
    close = bars["close"].to_numpy(dtype="float64")
    n = len(bars)

    valid = (sh_i != -1) & (sl_i != -1) & (sh > sl)
    rng = np.where(valid, sh - sl, np.nan)
    with np.errstate(invalid="ignore"):
        pos = np.where(valid, (close - sl) / rng, np.nan)

    zone = np.full(n, "", dtype=object)
    hi_thr, lo_thr = 0.5 + eq_band, 0.5 - eq_band
    zone[valid & (pos > hi_thr)] = "premium"
    zone[valid & (pos < lo_thr)] = "discount"
    zone[valid & (pos >= lo_thr) & (pos <= hi_thr)] = "equilibrium"

    return pd.DataFrame(
        {
            "pd_zone": zone,
            "pd_pos": pos,
            "range_low": np.where(valid, sl, np.nan),
            "range_high": np.where(valid, sh, np.nan),
            "eq": np.where(valid, (sh + sl) / 2.0, np.nan),
        },
        index=bars.index,
    )
