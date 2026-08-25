# Architecture (Phase 1)

## Shape of the system

A local, reproducible Python research platform. No servers, no cloud dependency to run
the pipeline or the tests. Data flows one direction, and every stage is leakage-testable.

```
raw source ──▶ ingest ──▶ validate ──▶ resample ──▶ MTF align ──▶ features* ──▶ setups* ──▶ label ──▶ backtest* ──▶ metrics*
   (dukascopy / synthetic)      │            (closed-candle, no-look-ahead)                (triple-barrier)   (net of costs)
                                ▼
                          parquet store                          * = Phase 2+ (not yet built)
```

Everything above the dashed capabilities exists in P1; `*`-marked stages are stubs/placeholders.

## Module map (`src/fxlab/`)

| Module | Phase | Responsibility |
|---|---|---|
| `config.py` | P1 | Typed, YAML-backed, env-overridable configuration (pydantic-settings). |
| `data/schema.py` | P1 | Canonical bar schema, timeframe helpers, session tagging. |
| `data/validate.py` | P1 | Hard integrity errors vs soft (weekend-gap) warnings. |
| `data/resample.py` | P1 | Right-labelled resampling + **leakage-safe** MTF alignment. |
| `data/ingest_dukascopy.py` | P1 | `synthetic` (offline, deterministic) + `dukascopy` (real, optional) sources. |
| `data/store.py` | P1 | Parquet read/write, `{data_dir}/{stage}/{symbol}/{tf}.parquet`. |
| `labeling/triple_barrier.py` | P1 | `P(TP before SL)` labels; causal Wilder ATR. |
| `costs/model.py` | P1 | Spread/commission/slippage/latency; gross→net; +50% stress. |
| `validation/splits.py` | P1 | Chronological train/val/**untouched** test split. |
| `validation/walkforward.py` | P1 | Purged + embargoed walk-forward (Lopez de Prado, in-house). |
| `features/ structure/ smc/ regime/ setups/` | P2–P3 | Point-in-time features, market structure, SMC detectors, regime, setups. |
| `backtest/ ml/ risk/ execution/ monitoring/ experiment/` | P4+ | Event-driven engine, calibrated filter, risk engine, paper/live, dashboard, run ledger. |
| `cli.py` | P1 | `info · ingest · validate-data · label · split` (grows per phase). |

## Key architectural decisions

- **Custom event-driven backtester (P4), not a vectorized library.** The engine will only
  ever see closed candles; this is the one component we must be able to fully trust and
  leakage-test. Vectorized libraries make look-ahead easy and fight path-dependent SMC.
- **pandas at the analysis boundary; DuckDB for heavy tick→bar aggregation.** Parquet is
  the on-disk format everywhere.
- **In-house purged/embargoed CV.** `mlfinlab` is now commercial; the method is
  re-implemented and unit-tested here.
- **Config is code.** Every knob lives in `config/*.yaml`, validated by `config.py`. A run
  is reproducible from its config plus a data content hash (experiment ledger, P4+).

## Reproducibility

- Pinned interpreter (`.python-version` = 3.12) and a `uv` lockfile.
- Deterministic synthetic data (seeded) so the whole pipeline and test suite run offline.
- Append-only `experiments/registry.jsonl` + per-run artifact dirs with config/data hashes
  (introduced when backtests arrive in P4).

## What P1 deliberately excludes

No strategy, no SMC detectors, no ML, no broker. The `label` CLI command runs the
triple-barrier machinery on **placeholder** signals purely to prove the wiring; it makes
**no** edge claim. Real setups begin at P2, and only after the P1 gate is green.
