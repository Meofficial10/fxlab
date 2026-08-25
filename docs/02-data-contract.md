# Data contract (Phase 1)

The single source of truth for what a "bar" is. Enforced by `data/schema.py` and
`data/validate.py`; tested by `tests/test_schema.py` and `tests/test_validate.py`.

## Canonical bar frame

A pandas `DataFrame` with:

- **Index**: a timezone-aware **UTC** `DatetimeIndex` named `ts_open` — the bar's **OPEN**
  time. Monotonic increasing, no duplicates.
- **Columns** (all `float64`): `open`, `high`, `low`, `close`, `volume`.
- **Metadata**: `df.attrs["symbol"]` and `df.attrs["timeframe"]` — kept out of the numeric
  frame so it stays clean for math.

`ensure_bars()` coerces any OHLC(V) frame into this shape (localises/convert to UTC,
lowercases columns, defaults missing `volume` to 0, sorts). It is idempotent.

## Timeframe semantics

- A bar labelled at open time `t` on timeframe `TF` covers the half-open interval
  `[t, t + TF)`. Its **close time** is `t + TF`.
- **A bar is only *known* at its close.** Any cross-timeframe or forward logic keys off the
  close time, never the open time. This is the core no-look-ahead rule
  (see `resample.mtf_align`).
- Supported: `M1, M5, M15, M30, H1, H4, D1`.

## Sessions

Each timestamp is tagged with the **first matching** session window (list order = priority),
else `Off`. Windows are `[start_hour, end_hour)` in UTC and may wrap past midnight
(e.g. Asia `23→8`). Configured in `config/data.yaml`. Default priority order puts the
London/NY **Overlap** ahead of the individual sessions.

## Validation rules

**Hard errors** (`report.ok == False`, run must not proceed):

- index not a `DatetimeIndex`, timezone-naive, non-monotonic, or duplicated;
- empty frame; missing OHLCV columns;
- any `NaN` in OHLCV; non-positive prices; `high < low`;
- `high < max(open, close)` or `low > min(open, close)`; negative volume.

**Soft warnings** (never fail a run):

- time gaps larger than one timeframe step. Gaps whose preceding bar is a Friday are
  counted as **weekend gaps** (expected in FX) and reported separately.

## Storage layout

```
{data_dir}/{stage}/{symbol}/{timeframe}.parquet     stage ∈ {raw, interim, processed}
```

Parquet via pyarrow. `data/` is git-ignored.

## Sources

- **`synthetic`** — deterministic, seeded, offline geometric-random-walk generator
  (weekends removed, positive by construction). Used by tests and for running the pipeline
  without a network. **Never** used for any performance claim.
- **`dukascopy`** — real free tick/OHLC via the optional `dukascopy-python` extra
  (`python -m uv sync --extra data`). Classified **UNPROVEN** until exercised live.

## Split policy

Chronological only — **never shuffled**. `train ≤ train_end < val ≤ val_end < test`.
The **test window is opened once** (at P4) and is otherwise untouched. Purge + embargo
(sized to the label horizon) are applied at every train/test boundary so label windows
cannot straddle it.
