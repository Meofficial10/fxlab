"""Fair Value Gaps (FVG) — 3-candle price imbalances (Phase 3, SMC).

An FVG is a gap left by an impulsive move: across three consecutive candles the wicks of
candle 1 and candle 3 fail to overlap, leaving an untraded band around candle 2.

  * **Bullish FVG** at bar ``t``: ``high[t-2] < low[t]`` — gap band ``(high[t-2], low[t])``.
  * **Bearish FVG** at bar ``t``: ``low[t-2] > high[t]`` — gap band ``(high[t], low[t-2])``.

The gap is only *known* once candle 3 closes, so we stamp it on bar ``t`` (which reads only
bars ``t-2, t-1, t`` — all ``<= t``). That makes the detector strictly causal and
future-invariant. Mitigation (price later revisiting the band) is deliberately NOT computed
here — it is stateful and belongs to a setup, not to the pure geometric detector.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def fair_value_gaps(bars: pd.DataFrame, min_size: float = 0.0) -> pd.DataFrame:
    """Detect 3-candle FVGs. Columns (indexed like ``bars``):

    ``fvg_dir`` in {-1, 0, +1}; ``fvg_top`` / ``fvg_bottom`` = the untraded band's edges
    (NaN when no gap); ``fvg_size`` = ``top - bottom`` (NaN when no gap). ``min_size``
    filters out gaps narrower than a price threshold (default: keep any positive gap).
    """
    if min_size < 0:
        raise ValueError("min_size must be >= 0")
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    n = len(high)

    direction = np.zeros(n, dtype="int64")
    top = np.full(n, np.nan)
    bottom = np.full(n, np.nan)

    if n >= 3:
        h2, l2 = high[:-2], low[:-2]  # candle 1 (t-2), aligned to t at position 2..n-1
        h0, l0 = high[2:], low[2:]  # candle 3 (t)
        bull = (h2 < l0) & ((l0 - h2) > min_size)
        bear = (l2 > h0) & ((l2 - h0) > min_size)

        # direction[2:] / top[2:] / bottom[2:] are views, so these write through to the base.
        direction[2:][bull] = 1
        bottom[2:][bull] = h2[bull]
        top[2:][bull] = l0[bull]
        direction[2:][bear] = -1
        bottom[2:][bear] = h0[bear]
        top[2:][bear] = l2[bear]

    return pd.DataFrame(
        {"fvg_dir": direction, "fvg_top": top, "fvg_bottom": bottom, "fvg_size": top - bottom},
        index=bars.index,
    )
