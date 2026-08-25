"""Append-only experiment registry (Phase 2 reproducibility layer).

Every backtest appends one JSON line to ``experiments/registry.jsonl`` capturing exactly
what was run and what came out: the setup + params, the data slice (with a content hash
so the *exact* bars can be identified later), the cost/label config, and the full metric
set (gross AND net). No server, Windows-friendly, diff-able, and fully reproducible.

Nothing here interprets results — it only records them, honestly and immutably.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd


def hash_bars(bars: pd.DataFrame) -> str:
    """Stable content hash of a bar frame (index + OHLCV values)."""
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(bars.index.asi8).tobytes())
    for col in ("open", "high", "low", "close", "volume"):
        if col in bars.columns:
            h.update(np.ascontiguousarray(bars[col].to_numpy(dtype="float64")).tobytes())
    return h.hexdigest()[:16]


def _jsonable(obj):
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return str(obj)  # NaN/inf -> "nan"/"inf" so the JSON stays strictly valid
    return obj


def log_experiment(
    registry_path: str | Path,
    *,
    setup: str,
    symbol: str | None,
    timeframe: str | None,
    split: str,
    params: dict,
    metrics,
    data_hash: str,
    n_signals: int,
    n_taken: int,
    stressed: bool = False,
    phase: str = "P2",
    notes: str = "",
) -> dict:
    """Append one experiment record and return it. Creates the file/dir on first write."""
    record = _jsonable(
        {
            "ts_utc": datetime.now(UTC).isoformat(),
            "phase": phase,
            "setup": setup,
            "symbol": symbol,
            "timeframe": timeframe,
            "split": split,
            "stressed": stressed,
            "params": params,
            "data_hash": data_hash,
            "n_signals": n_signals,
            "n_taken": n_taken,
            "metrics": metrics,
            "notes": notes,
        }
    )
    path = Path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record
