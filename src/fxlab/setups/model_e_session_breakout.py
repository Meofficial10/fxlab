"""Model E — Session Opening-Range Breakout (a NON-SMC, time-of-day volatility mechanism).

This is the first mechanism deliberately *outside* the Smart-Money-Concepts family (Models A–D).
It is not derived from any prior result and does not lean on them; it is a fresh, classic
market-microstructure idea, tested with the same discipline and held to the same P4 gate.

HYPOTHESIS (UNPROVEN — to be measured, never assumed, never tuned toward):
    Intraday FX has a time-of-day volatility structure: when a major session opens (canonically
    London, 07:00–16:00 UTC), participation and volatility rise, and the price level at which the
    session's first *range* is broken tends to mark the direction institutional flow commits to for
    the session. So a breakout of the session's OPENING RANGE, taken in the breakout direction,
    *may* carry positive expectancy net of costs. This is a directional-continuation, volatility
    hypothesis — mechanistically distinct from the SMC reversal/continuation setups.

Objective rules (all causal — a signal at bar ``t`` reads only bars ``<= t``):

  * **Session** = the fixed-UTC half-open window ``[start_hour, end_hour)`` (same convention as
    ``fxlab.data.schema._in_window``; a window with ``start_hour > end_hour`` wraps past midnight).
    A **session start** is the first in-window bar after an out-of-window bar (bar 0, if in-window,
    counts as a start — conservative and it is identical under any right-truncation, so causal).
  * **Opening range (OR)** = the highest high and lowest low of the first ``or_bars`` bars of the
    session (bars ``[start, start + or_bars)``). The OR is only *known* at the close of its last
    bar, so breakouts are watched strictly from bar ``start + or_bars`` onward.
  * **Breakout trigger (close-based, conservative):** the first in-session bar after the OR whose
    **close** is strictly above the OR high fires **LONG (+1)**; strictly below the OR low fires
    **SHORT (-1)**. Close-based (not intrabar-touch) so the trigger is decided only on closed-candle
    information and avoids wick whipsaw.
  * **At most one signal per session** (the first breakout wins). Once fired, that session is done.
  * The breakout is watched for at most ``max_watch`` bars after the OR completes AND only while the
    bar is still inside the session window — whichever ends first. No breakout in that span → no
    trade for that session. ``max_watch`` is a computational bound, not a tuned edge parameter.
  * A new session start resets the OR and the fired flag.

The setup emits only ``(signal_idx, side)``. The backtest engine owns entry (next-bar open), the
ATR triple-barrier exit, realistic costs, latency, and one-position-at-a-time gating — so the same
exit/cost machinery already validated for Models A–D applies unchanged here.

Scope note (not tuning): a session opening range is only meaningful intraday. On H1 the London
window is 9 bars (``or_bars=1`` = the first hour); on M15 it is 36 bars. On H4 a whole session is
~2 bars, so there is no opening range to speak of — H4 is therefore out of scope for this mechanism
by construction, not by result-shopping.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class ModelESessionBreakout:
    start_hour: int = 7   # London open (UTC); matches config sessions "London"
    end_hour: int = 16    # London close (UTC)
    or_bars: int = 1      # bars forming the opening range (H1: 1 = first hour)
    max_watch: int = 24   # max bars after the OR to watch for a breakout (computational bound)
    name: str = "model_e_session_breakout"

    def __post_init__(self) -> None:
        if not (0 <= self.start_hour <= 23):
            raise ValueError("start_hour must be in [0, 23]")
        if not (0 <= self.end_hour <= 24):
            raise ValueError("end_hour must be in [0, 24]")
        if self.start_hour == self.end_hour:
            raise ValueError("start_hour == end_hour is an empty session window")
        if self.or_bars < 1:
            raise ValueError("or_bars must be >= 1")
        if self.max_watch < 1:
            raise ValueError("max_watch must be >= 1")

    def _in_session(self, hours: np.ndarray) -> np.ndarray:
        """Vectorised half-open [start, end) membership; wraps past midnight if start > end.

        Same semantics as ``fxlab.data.schema._in_window`` (kept in lock-step by the unit tests).
        """
        if self.start_hour <= self.end_hour:
            return (hours >= self.start_hour) & (hours < self.end_hour)
        return (hours >= self.start_hour) | (hours < self.end_hour)

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        high = bars["high"].to_numpy(dtype="float64")
        low = bars["low"].to_numpy(dtype="float64")
        close = bars["close"].to_numpy(dtype="float64")
        hours = bars.index.hour.to_numpy()
        in_sess = self._in_session(hours)
        n = len(bars)

        out_idx: list[int] = []
        out_side: list[int] = []

        active = False        # inside a session run?
        fired = False         # has this session already produced its one signal?
        k = 0                 # bars since this session's start (0-based)
        or_hi = -np.inf
        or_lo = np.inf
        prev_in = False

        for t in range(n):
            here = bool(in_sess[t])
            if here and not prev_in:      # session just started -> (re)initialise the range
                active = True
                fired = False
                k = 0
                or_hi = -np.inf
                or_lo = np.inf

            if here and active:
                if k < self.or_bars:      # still building the opening range
                    or_hi = max(or_hi, high[t])
                    or_lo = min(or_lo, low[t])
                elif not fired and (k - self.or_bars) < self.max_watch:
                    # OR is complete (from bars strictly before t); watch for the first close-break
                    if close[t] > or_hi:
                        out_idx.append(t)
                        out_side.append(1)
                        fired = True
                    elif close[t] < or_lo:
                        out_idx.append(t)
                        out_side.append(-1)
                        fired = True
                k += 1

            if not here:                  # left the window -> session over, await next start
                active = False
            prev_in = here

        return np.asarray(out_idx, dtype=int), np.asarray(out_side, dtype=int)
