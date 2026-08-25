"""Model A — Liquidity-Sweep Reversal (Phase 3 SMC setup).

The first setup built from SMC detectors, and the one used to measure P3's incremental
value over the P2 baseline (Model B). It trades the classic stop-run reversal:

  * A **liquidity sweep** fires (``smc.liquidity.liquidity_sweeps``): price traded beyond
    the most recent confirmed swing and *closed back through* it — stops taken, then
    rejected.
      - bullish sweep (swept lows, closed above)  -> LONG  (+1)
      - bearish sweep (swept highs, closed below) -> SHORT (-1)

By default it fires on **every** sweep. That is deliberately un-selective, and the P3
measurement showed the resulting per-trade gross edge is too small to clear costs. The
optional **confirmation filters** below exist to test one hypothesis: *fewer, higher-
conviction trades carry more edge per trade, so the fixed per-trade cost stops swamping
the signal* (i.e. lift the cost-to-edge ratio). Each is same-bar and causal — it reads
only detectors evaluated at the signal bar ``i`` (all of which read only bars ``<= i``):

  * ``require_displacement`` — the sweep bar is itself a displacement candle in the
    sweep/reversal direction (``disp_dir[i] == sweep_dir[i]``): a forceful rejection, not
    a quiet poke-through.
  * ``require_fvg`` — a 3-candle fair-value gap completes on the sweep bar in the
    reversal direction (``fvg_dir[i] == sweep_dir[i]``): visible imbalance behind the move.
  * ``align_structure`` — the market-structure trend matches the sweep direction (long only
    while ``trend_state == "bull"``, short only while ``"bear"``): a liquidity grab *with*
    the prevailing structure rather than against it.
  * ``align_pd`` — only take longs in **discount** and shorts in **premium**
    (``smc.premium_discount``): don't buy expensive or sell cheap.

Filters compose by logical AND; with all off this is the original every-sweep setup.
Every filter is a **HYPOTHESIS** — it exists to produce an honest, leakage-tested metric
set to compare against the baseline, never an assumed edge. The engine owns fills, ATR
barriers, costs, latency, and one-position gating; whether any filter improves expectancy
*net of costs* is a measured question, and a filter that helps gross but not net is a
finding, not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..smc.displacement import displacement
from ..smc.fvg import fair_value_gaps
from ..smc.liquidity import liquidity_sweeps
from ..smc.premium_discount import premium_discount
from ..structure.market_structure import market_structure


@dataclass
class ModelASweepReversal:
    left: int = 2
    right: int = 2
    align_pd: bool = False  # require discount for longs / premium for shorts
    require_displacement: bool = False  # sweep bar displaces in the reversal direction
    require_fvg: bool = False  # FVG completes on the sweep bar in the reversal direction
    align_structure: bool = False  # market-structure trend matches the sweep direction
    atr_window: int = 14  # displacement filter ATR window
    body_mult: float = 1.5  # displacement filter body/ATR threshold
    name: str = "model_a_sweep_reversal"

    def __post_init__(self) -> None:
        if self.left < 1 or self.right < 1:
            raise ValueError("left and right must both be >= 1")
        if self.atr_window < 1:
            raise ValueError("atr_window must be >= 1")
        if self.body_mult <= 0:
            raise ValueError("body_mult must be > 0")

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        sweep = liquidity_sweeps(bars, self.left, self.right)["sweep_dir"].to_numpy()
        take = sweep != 0  # base: every sweep

        # Each filter narrows `take`; all are same-bar and causal (read only bars <= i).
        if self.require_displacement:
            disp = displacement(bars, self.atr_window, self.body_mult)["disp_dir"].to_numpy()
            take &= disp == sweep  # reversal-direction displacement on the sweep bar

        if self.require_fvg:
            fvg = fair_value_gaps(bars)["fvg_dir"].to_numpy()
            take &= fvg == sweep  # reversal-direction imbalance completing on the sweep bar

        if self.align_structure:
            trend = market_structure(bars, self.left, self.right)["trend_state"].to_numpy()
            aligned = ((sweep == 1) & (trend == "bull")) | ((sweep == -1) & (trend == "bear"))
            take &= aligned

        if self.align_pd:
            zone = premium_discount(bars, self.left, self.right)["pd_zone"].to_numpy()
            in_zone = ((sweep == 1) & (zone == "discount")) | ((sweep == -1) & (zone == "premium"))
            take &= in_zone

        idx = np.flatnonzero(take).astype(int)
        side = sweep[idx].astype(int)  # +1 bullish sweep -> long, -1 bearish -> short
        return idx, side
