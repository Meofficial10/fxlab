"""Model D — Fair-Value-Gap Retracement Continuation (Phase 3 SMC setup).

A deliberately *independent* SMC expression from Model A. Where Model A **fades** a liquidity
grab at a swing (reversal), Model D trades **continuation**: an impulsive move leaves a
fair-value gap (an imbalance); price often pulls back *into* that gap and then resumes in the
original direction. So:

  * a **bullish FVG** (up-impulse imbalance) is a demand zone — when price later retraces down
    and taps it, go **LONG** (+1), betting the up-move continues;
  * a **bearish FVG** (down-impulse imbalance) is a supply zone — when price retraces up into
    it, go **SHORT** (-1).

Unlike the pure detectors this setup is **stateful** (it tracks unfilled gaps across bars),
but it is still strictly causal: a signal at bar ``t`` uses only gaps *created before* ``t``
(``fair_value_gaps`` stamps a gap on its 3rd candle, reading bars ``<= that candle``) and bar
``t``'s own high/low/close — never anything after ``t``. Future-invariance is regression-tested.

Objective rules, evaluated bar-by-bar (all prices are of the closed bar ``t``):

  * **Register** a gap on its completion bar; it becomes eligible to trigger from the *next*
    bar (a gap never triggers on its own creation bar — where ``low == top`` trivially).
  * **Invalidate first** (checked before any tap): a bullish gap whose bar *closes below* its
    bottom edge, or a bearish gap that closes above its top edge, is filled-through — dropped,
    no trade. Conservative: a bar that both dips into a bullish gap and closes below it is an
    invalidation, not an entry.
  * **Tap** = first touch of the gap: bullish when ``low[t] <= top``, bearish when
    ``high[t] >= bottom``. A tap fires the continuation entry and consumes the gap.
  * At most **one signal per bar**; if both a bullish and a bearish gap tap on the same bar the
    signals conflict and none is taken (both are still consumed).
  * A gap untapped for ``max_age`` bars **expires** (a computational bound and a mild "stale
    zone" prior; it is not a tuned edge parameter).

This is a **HYPOTHESIS** — it exists to produce an honest, leakage-tested metric set for a
mechanism distinct from the sweep reversal, never an assumed edge. The engine owns fills, ATR
barriers, costs, latency, and one-position gating.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..smc.fvg import fair_value_gaps


@dataclass
class ModelDFvgRetracement:
    min_gap: float = 0.0  # ignore gaps narrower than this (absolute price); 0 keeps all
    max_age: int = 500  # bars an unfilled gap stays active before expiring
    name: str = "model_d_fvg_retracement"

    def __post_init__(self) -> None:
        if self.min_gap < 0:
            raise ValueError("min_gap must be >= 0")
        if self.max_age < 1:
            raise ValueError("max_age must be >= 1")

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        fvg = fair_value_gaps(bars, min_size=self.min_gap)
        fdir = fvg["fvg_dir"].to_numpy()
        ftop = fvg["fvg_top"].to_numpy()
        fbot = fvg["fvg_bottom"].to_numpy()
        high = bars["high"].to_numpy(dtype="float64")
        low = bars["low"].to_numpy(dtype="float64")
        close = bars["close"].to_numpy(dtype="float64")
        n = len(bars)

        active: list[tuple[int, float, float, int]] = []  # (dir, bottom, top, created_idx)
        out_idx: list[int] = []
        out_side: list[int] = []

        for t in range(n):
            survivors: list[tuple[int, float, float, int]] = []
            long_tap = short_tap = False

            for g in active:
                gdir, gbot, gtop, gc = g
                if t - gc > self.max_age:
                    continue  # expired -> drop
                # Invalidation (far side closed through) takes priority over a tap.
                if gdir == 1 and close[t] < gbot:
                    continue
                if gdir == -1 and close[t] > gtop:
                    continue
                # Tap = first touch, only from a bar strictly after creation.
                if gc < t:
                    if gdir == 1 and low[t] <= gtop:
                        long_tap = True
                        continue  # consumed
                    if gdir == -1 and high[t] >= gbot:
                        short_tap = True
                        continue  # consumed
                survivors.append(g)
            active = survivors

            if long_tap and not short_tap:
                out_idx.append(t)
                out_side.append(1)
            elif short_tap and not long_tap:
                out_idx.append(t)
                out_side.append(-1)
            # both or neither -> no signal this bar (any tapped gaps already consumed)

            if fdir[t] != 0:  # register a gap created on this bar (eligible from t+1)
                active.append((int(fdir[t]), float(fbot[t]), float(ftop[t]), t))

        return np.asarray(out_idx, dtype=int), np.asarray(out_side, dtype=int)
