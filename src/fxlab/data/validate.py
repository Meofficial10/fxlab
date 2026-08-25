"""Bar-integrity validation (Phase 1).

Separates HARD errors (duplicate/non-monotonic index, NaNs, non-positive prices,
high<low, OHLC out of range) from SOFT warnings (time gaps, which are normal in FX
around weekends). ``report.ok`` is True iff there are no hard errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .schema import OHLCV, timeframe_to_timedelta


@dataclass
class ValidationReport:
    symbol: str | None
    timeframe: str
    n_rows: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    n_gaps: int = 0
    n_weekend_gaps: int = 0
    max_gap: pd.Timedelta | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise ValueError(
                f"Bar validation failed for {self.symbol}/{self.timeframe}:\n  - "
                + "\n  - ".join(self.errors)
            )

    def summary(self) -> str:
        status = "OK" if self.ok else "FAILED"
        lines = [
            f"[{status}] {self.symbol}/{self.timeframe}  rows={self.n_rows}",
            f"  gaps={self.n_gaps} (weekend={self.n_weekend_gaps}) max_gap={self.max_gap}",
        ]
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  warn:  {w}")
        return "\n".join(lines)


def validate_bars(df: pd.DataFrame, timeframe: str, symbol: str | None = None) -> ValidationReport:
    symbol = symbol or df.attrs.get("symbol")
    rep = ValidationReport(symbol=symbol, timeframe=timeframe, n_rows=len(df))

    if not isinstance(df.index, pd.DatetimeIndex):
        rep.errors.append("index is not a DatetimeIndex")
        return rep
    if df.index.tz is None:
        rep.errors.append("index is timezone-naive (must be UTC)")
    if len(df) == 0:
        rep.errors.append("empty frame")
        return rep

    if not df.index.is_monotonic_increasing:
        rep.errors.append("index is not monotonically increasing")
    n_dupes = int(df.index.duplicated().sum())
    if n_dupes:
        rep.errors.append(f"{n_dupes} duplicate timestamps")

    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        rep.errors.append(f"missing columns: {missing}")
        return rep

    ohlc = df[["open", "high", "low", "close"]]
    n_nan = int(df[OHLCV].isna().to_numpy().sum())
    if n_nan:
        rep.errors.append(f"{n_nan} NaN values in OHLCV")
    if (ohlc <= 0).to_numpy().any():
        rep.errors.append("non-positive prices present")
    hi, lo = df["high"], df["low"]
    if (hi < lo).any():
        rep.errors.append(f"{int((hi < lo).sum())} bars with high < low")
    body_hi = df[["open", "close"]].max(axis=1)
    body_lo = df[["open", "close"]].min(axis=1)
    if (hi < body_hi - 1e-12).any():
        rep.errors.append("high below max(open, close) on some bars")
    if (lo > body_lo + 1e-12).any():
        rep.errors.append("low above min(open, close) on some bars")
    if (df["volume"] < 0).any():
        rep.errors.append("negative volume present")

    # Gaps (warnings only). A gap larger than one step is expected across weekends.
    step = timeframe_to_timedelta(timeframe)
    diffs = df.index.to_series().diff().dropna()
    gap_mask = diffs > step
    rep.n_gaps = int(gap_mask.sum())
    if rep.n_gaps:
        prev_dow = df.index.to_series().shift(1).dt.dayofweek  # 4 = Friday
        weekend = gap_mask & (prev_dow >= 4)
        rep.n_weekend_gaps = int(weekend.sum())
        rep.max_gap = diffs.max()
        non_weekend = rep.n_gaps - rep.n_weekend_gaps
        if non_weekend:
            rep.warnings.append(f"{non_weekend} non-weekend gaps > {step}")

    return rep
