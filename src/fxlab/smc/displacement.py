"""Displacement — impulsive candles relative to recent volatility (Phase 3, SMC).

A *displacement* is a candle whose body is large compared with the prevailing volatility:
the market moving with intent rather than drifting. We define it objectively as

    body(t) = |close(t) - open(t)|   and   strength(t) = body(t) / ATR(t)

with a displacement flagged when ``strength >= body_mult``. ATR here is the causal Wilder
ATR (:func:`fxlab.labeling.triple_barrier.atr_wilder`), so ``strength(t)`` reads only bars
``<= t`` and the detector is future-invariant by construction.

This is a **detector**, not a signal. Displacement underpins order blocks and gives context
to fair-value gaps; whether it carries expectancy is a P4 question, measured net of costs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..labeling.triple_barrier import atr_wilder


@dataclass(frozen=True)
class DisplacementConfig:
    atr_window: int = 14
    body_mult: float = 1.5  # body must be >= this many ATRs to count as displacement

    def __post_init__(self) -> None:
        if self.atr_window < 1:
            raise ValueError("atr_window must be >= 1")
        if self.body_mult <= 0:
            raise ValueError("body_mult must be > 0")


def displacement(
    bars: pd.DataFrame, atr_window: int = 14, body_mult: float = 1.5
) -> pd.DataFrame:
    """Per-bar displacement. Columns (indexed like ``bars``):

    ``disp_dir`` in {-1, 0, +1} (sign of the candle body when it displaces, else 0);
    ``disp_strength`` = body / ATR (NaN during ATR warm-up); ``body`` = |close - open|.
    """
    cfg = DisplacementConfig(atr_window, body_mult)  # validate
    open_ = bars["open"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")
    atr = atr_wilder(bars, cfg.atr_window).to_numpy()

    body = np.abs(close - open_)
    with np.errstate(invalid="ignore", divide="ignore"):
        strength = np.where((atr > 0), body / atr, np.nan)

    is_disp = strength >= cfg.body_mult  # NaN >= x is False -> warm-up never displaces
    direction = np.where(is_disp, np.sign(close - open_).astype("int64"), 0).astype("int64")

    return pd.DataFrame(
        {"disp_dir": direction, "disp_strength": strength, "body": body},
        index=bars.index,
    )
