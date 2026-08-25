"""Order blocks — the origin candle of a displacement move (Phase 3, SMC).

An *order block* is the last opposite-colour candle immediately before an impulsive
(displacement) move: the supply/demand candle the move originated from.

  * **Bullish OB** (demand): the last *down* candle before an *up* displacement.
  * **Bearish OB** (supply): the last *up* candle before a *down* displacement.

The block is only *known* once the displacement candle closes, so it is stamped on the
displacement bar ``t`` and its zone is taken from the source candle at ``p < t``. Both
inputs are ``<= t``, so the detector is causal and future-invariant. This is a **detector**,
not a signal; its edge (if any) is a P4 question, measured net of costs against the P2
baseline.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .displacement import displacement


@dataclass(frozen=True)
class OrderBlockConfig:
    atr_window: int = 14
    body_mult: float = 1.5
    max_lookback: int = 3  # how many bars before the displacement to scan for the origin

    def __post_init__(self) -> None:
        if self.max_lookback < 1:
            raise ValueError("max_lookback must be >= 1")


def order_blocks(
    bars: pd.DataFrame, atr_window: int = 14, body_mult: float = 1.5, max_lookback: int = 3
) -> pd.DataFrame:
    """Detect order blocks at each displacement bar. Columns (indexed like ``bars``):

    ``ob_dir`` in {-1, 0, +1} (+1 bullish/demand, -1 bearish/supply, 0 none);
    ``ob_low`` / ``ob_high`` = the source candle's range (NaN when none);
    ``ob_src_idx`` = integer position of the source candle (-1 when none).
    """
    cfg = OrderBlockConfig(atr_window, body_mult, max_lookback)  # validate
    disp = displacement(bars, cfg.atr_window, cfg.body_mult)["disp_dir"].to_numpy()
    open_ = bars["open"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    n = len(bars)
    candle_dir = np.sign(close - open_)  # +1 up, -1 down, 0 doji

    ob_dir = np.zeros(n, dtype="int64")
    ob_low = np.full(n, np.nan)
    ob_high = np.full(n, np.nan)
    ob_src = np.full(n, -1, dtype="int64")

    for t in range(n):
        d = disp[t]
        if d == 0:
            continue
        want = -d  # an up displacement originates from a down candle, and vice versa
        lo = max(0, t - cfg.max_lookback)
        for p in range(t - 1, lo - 1, -1):
            if candle_dir[p] == want:
                ob_dir[t] = d
                ob_low[t] = low[p]
                ob_high[t] = high[p]
                ob_src[t] = p
                break

    return pd.DataFrame(
        {"ob_dir": ob_dir, "ob_low": ob_low, "ob_high": ob_high, "ob_src_idx": ob_src},
        index=bars.index,
    )
