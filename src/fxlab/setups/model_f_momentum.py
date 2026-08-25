"""Model F — Time-Series Momentum (trend-following), a NON-SMC, multi-week-horizon mechanism.

This is the second mechanism deliberately *outside* the Smart-Money-Concepts family (Models A-D)
and the first tested at a **daily / multi-week horizon** rather than intraday. It is not derived
from any prior result; it is chosen for a documented *structural* reason and held to the same P4
gate as everything else.

STRUCTURAL PRIOR (why this class, not just another pattern):
    Time-series momentum -- the tendency of an asset's OWN past return to predict its near-future
    return -- is the single most-replicated cross-asset premium in the literature (Moskowitz, Ooi &
    Pedersen 2012, "Time Series Momentum"; Hurst, Ooi & Pedersen, "A Century of Evidence on
    Trend-Following"). In FX it is attributed to slow diffusion of macro / monetary-policy
    information and investor underreaction -- an *economic* mechanism, not a chart shape. Two things
    make it genuinely different from Models A-E: (1) all of A-E are intraday (hours); momentum lives
    at a multi-week-to-month horizon on DAILY bars; (2) on daily bars the ATR-scaled stop (1R) is an
    order of magnitude larger than intraday, so the fixed per-trade cost (spread + commission) is a
    small fraction of R -- the specific reason the intraday session-breakout (Model E) died net of
    costs. So *if* any gross edge exists, this is the class most likely to survive realistic costs.

HYPOTHESIS (UNPROVEN -- to be measured, never assumed, never tuned toward):
    The sign of a currency pair's trailing ``lookback``-bar return predicts the sign of its next
    move, so entering in the direction of that trailing-return sign carries positive expectancy net
    of costs at the daily horizon.

Objective rules (all causal -- a signal at bar ``t`` reads only bars ``<= t``):

  * **Momentum** at bar ``t`` is the trailing simple return over ``lookback`` bars::

        mom[t] = close[t] - close[t - lookback]

    which depends only on closes at ``t`` and ``t - lookback`` (both ``<= t``) -> strictly causal
    and trivially future-invariant. The first ``lookback`` bars are warm-up and never fire.
  * **State / signal:** an **up-state** bar (``mom[t] > 0``) emits **LONG (+1)**; a **down-state**
    (``mom[t] < 0``) emits **SHORT (-1)**; a flat bar (``mom[t] == 0``) emits nothing. A signal is
    emitted on *every* in-state bar, NOT only on the flip: the backtest engine holds one position at
    a time and ignores signals while in a trade, so continuous in-state signalling makes it re-enter
    with the trend on the next bar after each barrier exit -- the faithful representation of "stay
    positioned in the direction of the trend", chopped into <= ``max_hold`` segments by the engine's
    triple barrier. (Flip-only entry would sample the identical hypothesis with fewer trades.)

The setup emits only ``(signal_idx, side)``. The backtest engine owns entry (next-bar open), the
ATR triple-barrier exit, realistic costs, latency, and one-position-at-a-time gating -- so the same
exit/cost machinery already validated for Models A-E applies unchanged. With the platform's default
barrier (TP = 2xATR, SL = 1xATR, max_hold = 24 bars) a daily trade is an asymmetric 2R:1R bet on the
next ~5 weeks in the trend direction -- reward-skewed, i.e. trend-appropriate, not hostile to it.

Scope note (not tuning): the prior is horizon-specific -- momentum has evidence at the multi-week
horizon. On D1 a ``lookback`` of 126 is ~6 months; the mechanism is intended for D1 (headline) with
H4 reported only as shorter-horizon robustness context. Lower intraday timeframes are out of scope
for this mechanism by construction (they re-ask the intraday question Models B/E already answered).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class ModelFMomentum:
    lookback: int = 126   # trailing bars for the momentum sign (D1: 126 ~= 6 months) -- headline
    name: str = "model_f_momentum"

    def __post_init__(self) -> None:
        if self.lookback < 1:
            raise ValueError("lookback must be >= 1")

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        close = bars["close"].to_numpy(dtype="float64")
        n = len(close)
        if n <= self.lookback:
            return np.empty(0, dtype=int), np.empty(0, dtype=int)

        # Trailing return over `lookback` bars: mom[t] uses close[t] and close[t-lookback].
        # Warm-up bars [0, lookback) have no defined trailing return and can never fire.
        mom = np.full(n, np.nan, dtype="float64")
        mom[self.lookback:] = close[self.lookback:] - close[: n - self.lookback]

        long_sig = mom > 0.0
        short_sig = mom < 0.0   # a flat bar (mom == 0) is neither -> no signal

        idx = np.where(long_sig | short_sig)[0]
        side = np.where(long_sig[idx], 1, -1).astype(int)
        return idx.astype(int), side
