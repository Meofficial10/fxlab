"""Parquet storage for bar frames. Layout: ``{data_dir}/{stage}/{symbol}/{tf}.parquet``."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .schema import ensure_bars

STAGES = ("raw", "interim", "processed")


def bars_path(data_dir: str | Path, symbol: str, timeframe: str, stage: str = "processed") -> Path:
    return Path(data_dir) / stage / symbol / f"{timeframe}.parquet"


def save_bars(
    df: pd.DataFrame, data_dir: str | Path, symbol: str, timeframe: str, stage: str = "processed"
) -> Path:
    path = bars_path(data_dir, symbol, timeframe, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, engine="pyarrow")
    return path


def load_bars(
    data_dir: str | Path, symbol: str, timeframe: str, stage: str = "processed"
) -> pd.DataFrame:
    path = bars_path(data_dir, symbol, timeframe, stage)
    if not path.exists():
        raise FileNotFoundError(f"no bars at {path} — run `fxlab ingest` first")
    return ensure_bars(pd.read_parquet(path, engine="pyarrow"), symbol, timeframe)
