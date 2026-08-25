"""Canonical candle schema, timeframe helpers, and session tagging (Phase 1).

Bars are a pandas DataFrame with:
  * a tz-aware UTC ``DatetimeIndex`` named ``ts_open`` (the bar's OPEN time), and
  * float64 columns ``open, high, low, close, volume``.

``symbol`` and ``timeframe`` travel in ``df.attrs`` so the numeric frame stays clean.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

OHLCV: list[str] = ["open", "high", "low", "close", "volume"]

_TF_MINUTES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}
_TF_OFFSET: dict[str, str] = {
    "M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
    "H1": "1h", "H4": "4h", "D1": "1D",
}


def timeframe_to_timedelta(tf: str) -> pd.Timedelta:
    if tf not in _TF_MINUTES:
        raise ValueError(f"Unknown timeframe {tf!r}; known: {sorted(_TF_MINUTES)}")
    return pd.Timedelta(minutes=_TF_MINUTES[tf])


def timeframe_to_offset(tf: str) -> str:
    if tf not in _TF_OFFSET:
        raise ValueError(f"Unknown timeframe {tf!r}; known: {sorted(_TF_OFFSET)}")
    return _TF_OFFSET[tf]


def ensure_bars(
    df: pd.DataFrame, symbol: str | None = None, timeframe: str | None = None
) -> pd.DataFrame:
    """Coerce an arbitrary OHLC(V) frame into the canonical bar schema.

    Idempotent. Localises/converts the index to UTC, lowercases columns, fills a
    missing ``volume`` with 0.0, and sorts by time. Does NOT validate integrity
    (see :func:`fxlab.data.validate.validate_bars`).
    """
    df = df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("bars must be indexed by a DatetimeIndex (the bar open time)")

    idx = df.index
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index = idx
    df.index.name = "ts_open"

    df.columns = [str(c).lower() for c in df.columns]
    if "volume" not in df.columns:
        df["volume"] = 0.0
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        raise ValueError(f"bars missing required columns: {missing}")

    df = df[OHLCV].astype("float64").sort_index()
    if symbol is not None:
        df.attrs["symbol"] = symbol
    if timeframe is not None:
        df.attrs["timeframe"] = timeframe
    return df


def bar_close_index(index: pd.DatetimeIndex, timeframe: str) -> pd.DatetimeIndex:
    """Close time of each bar = open time + one timeframe. A bar is only *known* at its close."""
    return index + timeframe_to_timedelta(timeframe)


def _in_window(hour: int, start: int, end: int) -> bool:
    """Half-open [start, end); if start > end the window wraps past midnight."""
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _session_fields(s) -> tuple[str, int, int]:
    if isinstance(s, dict):
        return s["name"], int(s["start_hour"]), int(s["end_hour"])
    return s.name, int(s.start_hour), int(s.end_hour)


def tag_sessions(index: pd.DatetimeIndex, sessions: Sequence) -> pd.Categorical:
    """Tag each timestamp with the first matching session (priority = list order), else 'Off'."""
    spec = [_session_fields(s) for s in sessions]
    names = [name for name, _, _ in spec] + ["Off"]
    hour_map: dict[int, str] = {}
    for h in range(24):
        hour_map[h] = next((name for name, a, b in spec if _in_window(h, a, b)), "Off")
    tags = [hour_map[h] for h in index.hour]
    return pd.Categorical(tags, categories=list(dict.fromkeys(names)))


def add_session_column(df: pd.DataFrame, sessions: Sequence) -> pd.DataFrame:
    df = df.copy()
    df["session"] = tag_sessions(df.index, sessions)
    return df
