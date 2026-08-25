"""Build DAILY (D1) bars for the momentum mechanism (Model F) by resampling stored H1.

Model F is a daily / multi-week-horizon mechanism, but the pipeline so far ingested only
M5/M15/H1/H4. Rather than re-download, D1 is a *deterministic* function of the H1 bars already on
disk: resample_ohlcv aggregates H1 -> D1 (left-closed, left-labelled, UTC-midnight boundaries;
empty weekend days dropped). This keeps D1 exactly consistent with the H1 data every other model
used, adds no network dependency, and cannot introduce look-ahead (aggregation reads only bars
within each day). Idempotent -- safe to re-run.

Run: ``.venv/Scripts/python.exe scripts/build_daily_bars.py``
"""

from __future__ import annotations

from fxlab.config import load_config
from fxlab.data.resample import resample_ohlcv
from fxlab.data.store import load_bars, save_bars


def main() -> None:
    cfg = load_config()
    for pair in cfg.data.symbols:
        try:
            h1 = load_bars(cfg.data_dir, pair, "H1")
        except FileNotFoundError:
            print(f"[skip] {pair}: no H1 bars on disk (ingest H1 first)")
            continue
        d1 = resample_ohlcv(h1, "D1")
        path = save_bars(d1, cfg.data_dir, pair, "D1")
        span = f"{d1.index.min().date()}..{d1.index.max().date()}"
        print(f"[ok] {pair}: {len(h1)} H1 -> {len(d1)} D1 bars  ({span})  -> {path}")


if __name__ == "__main__":
    main()
