"""Model B — Trend Pullback (Phase 2 rule baseline).

A deliberately simple, fully objective trend-following pullback. It needs no SMC
detectors (those arrive in P3), so it is the natural *first* baseline: something honest
to measure P3's incremental value against.

Rules (all causal — a signal at bar ``i`` uses only bars ``<= i``):

  * Trend filter: fast EMA vs slow EMA (default 20 / 50) on the trading timeframe.
      - uptrend   := ema_fast[i] > ema_slow[i]
      - downtrend := ema_fast[i] < ema_slow[i]
  * Pullback re-cross (the trigger):
      - LONG at ``i``  iff uptrend AND close[i-1] < ema_fast[i-1] AND close[i] > ema_fast[i]
        (price dipped below the fast EMA and reclaimed it, in an uptrend), and — if
        ``require_momentum`` — close[i] > close[i-1].
      - SHORT is the mirror image.

This is a **HYPOTHESIS**, not a claimed edge: it exists to produce an honest,
leakage-tested baseline metric set. The engine adds ATR barriers, costs, and latency.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class ModelBTrendPullback:
    ema_fast: int = 20
    ema_slow: int = 50
    require_momentum: bool = True
    name: str = "model_b_trend_pullback"

    def __post_init__(self) -> None:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be < ema_slow")

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        close = bars["close"]
        # adjust=False => causal recursive EMA (value at i depends only on bars <= i).
        ema_f = close.ewm(span=self.ema_fast, adjust=False).mean().to_numpy(dtype="float64")
        ema_s = close.ewm(span=self.ema_slow, adjust=False).mean().to_numpy(dtype="float64")
        c = close.to_numpy(dtype="float64")

        uptrend = ema_f > ema_s
        downtrend = ema_f < ema_s

        # Shifted (i-1) views, strictly causal (NO wrap): index 0 gets its own value,
        # so its comparisons collapse to False and it can never fire.
        prev_close = np.empty_like(c)
        prev_close[0], prev_close[1:] = c[0], c[:-1]
        prev_ema_f = np.empty_like(ema_f)
        prev_ema_f[0], prev_ema_f[1:] = ema_f[0], ema_f[:-1]

        below_then_reclaim = (prev_close < prev_ema_f) & (c > ema_f)
        above_then_lose = (prev_close > prev_ema_f) & (c < ema_f)
        mom_up = c > prev_close if self.require_momentum else np.ones_like(c, dtype=bool)
        mom_dn = c < prev_close if self.require_momentum else np.ones_like(c, dtype=bool)

        long_sig = uptrend & below_then_reclaim & mom_up
        short_sig = downtrend & above_then_lose & mom_dn

        # Warm-up: require the slow EMA to have seen >= ema_slow bars, and i >= 1.
        warm = np.arange(len(c)) >= max(self.ema_slow, 1)
        long_sig &= warm
        short_sig &= warm

        idx = np.where(long_sig | short_sig)[0]
        side = np.where(long_sig[idx], 1, -1).astype(int)
        return idx.astype(int), side
