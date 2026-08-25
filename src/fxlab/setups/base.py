"""Setup interface (Phase 2).

A *setup* is a pure, objective signal generator. Its one job:

    generate(bars) -> (signal_idx, side)

where ``signal_idx`` are integer positions into ``bars`` at which the setup fires (the
bar CLOSE), and ``side`` is +1 (long) / -1 (short) for each. The contract is strict:

  * **Closed-candle only.** A signal at index ``i`` may read bars ``<= i`` and nothing
    to the right. Appending future bars must never change a past signal
    (future-invariance — regression-tested).
  * **No entry/exit/sizing logic.** The setup only says *when* and *which way*; the
    backtest engine owns fills, barriers, costs, and one-position-at-a-time gating.
  * **Fully specified + reproducible.** Same bars + same params -> same signals.

This keeps setups small and independently testable, and lets the engine stay the single
source of truth for execution.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd


@runtime_checkable
class Setup(Protocol):
    name: str

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Return (signal_idx, side) using only closed-candle information at each index."""
        ...
