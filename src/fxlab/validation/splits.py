"""Chronological data splitting (Phase 1).

The TEST window is defined once (``val_end`` .. end of data) and stays UNTOUCHED until
Phase 4. Everything is time-ordered — no shuffling, ever.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


def to_utc_ts(value: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


@dataclass
class Split:
    train: pd.DatetimeIndex
    val: pd.DatetimeIndex
    test: pd.DatetimeIndex

    def counts(self) -> dict[str, int]:
        return {"train": len(self.train), "val": len(self.val), "test": len(self.test)}


def chronological_split(
    index: pd.DatetimeIndex, train_end: str | pd.Timestamp, val_end: str | pd.Timestamp
) -> Split:
    """Split a time index into train (<= train_end), val (train_end, val_end], test (> val_end)."""
    if not index.is_monotonic_increasing:
        index = index.sort_values()
    te, ve = to_utc_ts(train_end), to_utc_ts(val_end)
    if te >= ve:
        raise ValueError(f"train_end ({te}) must be before val_end ({ve})")
    return Split(
        train=index[index <= te],
        val=index[(index > te) & (index <= ve)],
        test=index[index > ve],
    )
