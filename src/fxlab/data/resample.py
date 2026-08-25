"""Resampling and LEAKAGE-SAFE multi-timeframe alignment (Phase 1).

Two things must be true to avoid look-ahead:
  1. Higher-timeframe (HTF) bars are labelled by their OPEN time and are only
     *known* at their CLOSE time (open + one timeframe).
  2. When attaching HTF context to a lower-timeframe (LTF) bar at open time ``t``,
     we may use only HTF bars whose CLOSE <= ``t``. :func:`mtf_align` enforces this
     with a backward ``merge_asof`` on HTF close time.
"""

from __future__ import annotations

import pandas as pd

from .schema import (
    OHLCV,
    ensure_bars,
    timeframe_to_offset,
    timeframe_to_timedelta,
)

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample_ohlcv(bars: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate bars to a higher ``timeframe``. Left-closed, left-labelled: each output
    bar is stamped with its OPEN time and covers ``[t, t + timeframe)``.
    """
    rule = timeframe_to_offset(timeframe)
    out = (
        bars[OHLCV]
        .resample(rule, closed="left", label="left")
        .agg(_AGG)
        .dropna(subset=["open"])
    )
    out.index.name = "ts_open"
    out.attrs["symbol"] = bars.attrs.get("symbol")
    out.attrs["timeframe"] = timeframe
    return out


def mtf_align(
    ltf: pd.DataFrame,
    htf: pd.DataFrame,
    htf_timeframe: str,
    prefix: str | None = None,
) -> pd.DataFrame:
    """Attach the most recent CLOSED HTF bar to each LTF bar (no look-ahead).

    Returns a frame indexed like ``ltf`` with columns ``{prefix}{open,high,low,close,volume}``.
    The HTF bar covering the current LTF timestamp is intentionally EXCLUDED because it
    has not closed yet.
    """
    prefix = prefix if prefix is not None else f"{htf_timeframe}_"

    right = htf[OHLCV].copy()
    right["_close"] = right.index + timeframe_to_timedelta(htf_timeframe)
    right = right.reset_index(drop=True).sort_values("_close")

    left = pd.DataFrame({"ts_open": ltf.index}).sort_values("ts_open")

    merged = pd.merge_asof(
        left,
        right,
        left_on="ts_open",
        right_on="_close",
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged.set_index("ts_open").drop(columns="_close")
    merged = merged.rename(columns={c: f"{prefix}{c}" for c in OHLCV})
    merged.index.name = "ts_open"
    return merged.reindex(ltf.index)


def build_multitimeframe(base: pd.DataFrame, timeframes: list[str]) -> dict[str, pd.DataFrame]:
    """Resample a base frame into a dict of {timeframe: bars} for every requested TF."""
    base_tf = base.attrs.get("timeframe")
    out: dict[str, pd.DataFrame] = {}
    for tf in timeframes:
        out[tf] = (
            ensure_bars(base, base.attrs.get("symbol"), tf)
            if tf == base_tf
            else resample_ohlcv(base, tf)
        )
    return out
