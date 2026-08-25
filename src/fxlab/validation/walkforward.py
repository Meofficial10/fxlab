"""Purged + embargoed walk-forward cross-validation (Phase 1 primitive; used from P4/P7).

Method after Lopez de Prado, *Advances in Financial Machine Learning* (re-implemented
in-house because ``mlfinlab`` is now commercial). The two leakage defences:

  * **Purge** — drop any TRAIN event whose label window ``[t0, t1]`` reaches into the
    test period (``t1 >= test_start``). A label that resolves inside the test window
    would otherwise leak test information into training.
  * **Embargo** — additionally drop train events starting in a short window immediately
    before the test block, since their labels can still overlap it.

Walk-forward means train is drawn ONLY from the past relative to each test block.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pandas as pd

_NO_EMBARGO = pd.Timedelta(0)


def purge_train_times(
    t1: pd.Series, test_start: pd.Timestamp, embargo: pd.Timedelta = _NO_EMBARGO
) -> pd.DatetimeIndex:
    """Return train event start-times whose labels do not leak into/near the test block.

    ``t1`` is indexed by event start-time; its values are the label-end times.
    """
    starts = t1.index
    test_start = pd.Timestamp(test_start)
    # Purge: label ends at/after test_start -> would peek into the test window.
    leaks = (t1 >= test_start).to_numpy()
    # Embargo: starts within [test_start - embargo, test_start).
    embargoed = (starts >= (test_start - embargo)) & (starts < test_start)
    keep = ~(leaks | np.asarray(embargoed))
    return starts[keep]


class PurgedWalkForward:
    """Yield (train_times, test_times) folds over sequential, contiguous test blocks.

    The earliest block is used only as warm-up training history (never as a test set).
    """

    def __init__(self, n_splits: int = 5, embargo: pd.Timedelta = _NO_EMBARGO):
        if n_splits < 2:
            raise ValueError("n_splits must be >= 2")
        self.n_splits = n_splits
        self.embargo = embargo

    def split(self, t1: pd.Series) -> Iterator[tuple[pd.DatetimeIndex, pd.DatetimeIndex]]:
        times = t1.index.sort_values()
        t1 = t1.reindex(times)
        blocks = np.array_split(np.arange(len(times)), self.n_splits)
        for block in blocks[1:]:
            if len(block) == 0:
                continue
            test_times = times[block]
            test_start = test_times.min()
            past = times[times < test_start]
            if len(past) == 0:
                continue
            train_times = purge_train_times(t1.loc[past], test_start, self.embargo)
            if len(train_times) == 0:
                continue
            yield train_times, test_times
