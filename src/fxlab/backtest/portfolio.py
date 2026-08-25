"""Portfolio accounting: turn per-trade returns into equity curves + drawdown (Phase 2).

Two deliberately separate views, because conflating them hides risk:

  * **R-based** (sizing-agnostic) — the primary P2 view. Each trade's net return is
    expressed in R multiples (net ÷ stop distance) and simply *summed*. The cumulative-R
    curve and its max drawdown describe the setup's edge independently of position size.
  * **Fixed-fractional** (illustrative) — compound a starting equity by risking a fixed
    fraction ``f`` of it per trade: ``equity *= (1 + f * R)``. This shows path/compounding
    effects but is only a preview; real sizing + kill-switches live in the risk engine (P6).

Drawdown is reported as the max peak-to-trough decline of the cumulative curve.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class DrawdownStats:
    max_drawdown: float          # magnitude (>= 0), in the curve's own units
    peak_value: float            # curve value at the peak preceding the worst trough
    trough_value: float


def drawdown_curve(cumulative: np.ndarray) -> np.ndarray:
    """Drawdown at each point = running_peak - value (>= 0)."""
    if len(cumulative) == 0:
        return np.asarray([], dtype="float64")
    running_peak = np.maximum.accumulate(cumulative)
    return running_peak - cumulative


def max_drawdown(cumulative: np.ndarray) -> DrawdownStats:
    dd = drawdown_curve(cumulative)
    if len(dd) == 0:
        return DrawdownStats(0.0, 0.0, 0.0)
    j = int(np.argmax(dd))
    peak = float(np.maximum.accumulate(cumulative)[j])
    return DrawdownStats(
        max_drawdown=float(dd[j]), peak_value=peak, trough_value=float(cumulative[j])
    )


def cumulative_R(net_R: pd.Series | np.ndarray) -> np.ndarray:
    """Additive cumulative R curve (starts at 0, one point per closed trade)."""
    r = np.asarray(net_R, dtype="float64")
    return np.cumsum(r)


def fixed_fractional_equity(
    net_R: pd.Series | np.ndarray, risk_fraction: float, starting_equity: float = 1.0
) -> np.ndarray:
    """Compound equity by risking ``risk_fraction`` of current equity per trade.

    ``equity_{t+1} = equity_t * (1 + risk_fraction * R_t)``. Illustrative only — the risk
    engine (P6) owns real sizing, caps, and kill-switches. Equity is floored at 0 (ruin).
    """
    r = np.asarray(net_R, dtype="float64")
    equity = float(starting_equity)
    out = np.empty(len(r), dtype="float64")
    for i, ri in enumerate(r):
        equity = max(0.0, equity * (1.0 + risk_fraction * ri))
        out[i] = equity
    return out
