"""Model C — Breakout-Failure (fakeout) reversal (Phase 3 SMC setup).

A third, mechanistically *independent* setup. Where Model A fades a single-bar wick sweep of a
swing and Model D trades continuation into a fair-value gap, Model C fades a **trapped
breakout**: price *closes* beyond a confirmed structural level (a real break, not just a wick),
then *closes back* through it within a short window — the breakout has failed and the traders who
chased it are offside. So:

  * an **up-breakout** (close above the last confirmed swing high) that is later **reclaimed**
    (a close back below that level) -> go **SHORT** (-1): trapped breakout longs;
  * a **down-breakout** (close below the last confirmed swing low) later reclaimed (close back
    above) -> go **LONG** (+1): trapped breakdown shorts.

The breakout itself is exactly the objective, close-based, consumed-once event that
:func:`fxlab.structure.market_structure.market_structure` already emits (``bos_up``/``choch_up``
above the last confirmed swing high, ``bos_down``/``choch_down`` below the last confirmed swing
low). This setup is **stateful** — it latches the broken level and watches subsequent closes —
but strictly causal: a signal at bar ``t`` uses only market-structure output through ``t`` (built
on confirmed swings, itself future-invariant) and closes ``<= t``. Future-invariance is
regression-tested.

Objective rules, evaluated bar-by-bar on the CLOSED bar ``t``:

  * **Arm** an up-breakout watch at the broken level on a ``bos_up``/``choch_up`` bar (mirror for
    down). A fresh breakout on the same side supersedes an unresolved earlier one (the most recent
    trap is the relevant one).
  * **Fire** the fade on the first later bar that *closes back* through the latched level
    (up-watch: ``close < level`` -> SHORT; down-watch: ``close > level`` -> LONG); the watch is
    then consumed. The breakout bar can never be its own reclaim (its close is beyond the level).
  * At most **one signal per bar**; if an up-watch and a down-watch reclaim on the same bar the
    signals conflict and none is taken (both are still consumed).
  * A watch unreclaimed for ``max_wait`` bars **expires** — a failed breakout is by definition a
    *prompt* reclaim; ``max_wait`` is that behavioural window, not a tuned edge parameter.

This is a **HYPOTHESIS** — it produces an honest, leakage-tested metric set for the fakeout
mechanism, never an assumed edge. The engine owns fills, ATR barriers, costs, latency, and
one-position gating.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..structure.market_structure import market_structure

_UP_EVENTS = ("bos_up", "choch_up")  # close above last confirmed swing high = up-breakout
_DOWN_EVENTS = ("bos_down", "choch_down")  # close below last confirmed swing low = down-breakout


@dataclass
class ModelCBreakoutFailure:
    left: int = 2  # swing-detection spans (passed to market_structure -> confirmed_swings)
    right: int = 2
    max_wait: int = 10  # bars a breakout stays watched for a reclaim before expiring
    name: str = "model_c_breakout_failure"

    def __post_init__(self) -> None:
        if self.left < 1 or self.right < 1:
            raise ValueError("left and right must both be >= 1")
        if self.max_wait < 1:
            raise ValueError("max_wait must be >= 1")

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        ms = market_structure(bars, self.left, self.right)
        event = ms["event"].to_numpy()
        sh = ms["last_swing_high"].to_numpy()
        sl = ms["last_swing_low"].to_numpy()
        close = bars["close"].to_numpy(dtype="float64")
        n = len(bars)

        up_level: float | None = None  # broken swing high awaiting a close back below (-> SHORT)
        up_age = 0
        down_level: float | None = None  # broken swing low awaiting a close back above (-> LONG)
        down_age = 0
        out_idx: list[int] = []
        out_side: list[int] = []

        for t in range(n):
            c = close[t]
            fired_long = fired_short = False

            # Resolve open watches first (reclaim beats expiry); a reclaim consumes the watch.
            if up_level is not None:
                if c < up_level:
                    fired_short = True
                    up_level = None
                elif up_age >= self.max_wait:
                    up_level = None
                else:
                    up_age += 1
            if down_level is not None:
                if c > down_level:
                    fired_long = True
                    down_level = None
                elif down_age >= self.max_wait:
                    down_level = None
                else:
                    down_age += 1

            if fired_short and not fired_long:
                out_idx.append(t)
                out_side.append(-1)
            elif fired_long and not fired_short:
                out_idx.append(t)
                out_side.append(1)
            # both or neither -> no signal (any reclaimed watches already consumed)

            # Arm a new watch from this bar's structural breakout (after resolution, so the
            # breakout bar cannot be counted as its own reclaim). A fresh break supersedes.
            ev = event[t]
            if ev in _UP_EVENTS:
                up_level, up_age = float(sh[t]), 0
            elif ev in _DOWN_EVENTS:
                down_level, down_age = float(sl[t]), 0

        return np.asarray(out_idx, dtype=int), np.asarray(out_side, dtype=int)
