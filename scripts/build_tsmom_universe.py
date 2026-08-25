"""Build the CORE CROSS-ASSET DAILY universe for the multi-asset TSMOM portfolio.

Fetches real Dukascopy **D1** bars for the panel instruments (FX majors + metals + energy + liquid
equity indices) and stores them under a DEDICATED ``stage="panel"`` namespace
(``data/panel/{symbol}/D1.parquet``). Using a separate stage keeps this uniform, directly-fetched
D1 source isolated from the ``processed`` D1 that Model F resampled from H1 -- so ADR 0004's numbers
stay reproducible and the panel has one consistent bar source.

Deliberately restricted to LIQUID instruments with a uniform, well-understood cost regime; crypto
and thin/short-history instruments are excluded on purpose (user-chosen scope). Each instrument is
validated on ingest: it must start early enough and carry enough D1 bars to contribute a causal
252-day momentum + 60-day vol signal across the train/val window, else it is DROPPED and logged
(never silently kept). Idempotent: an instrument already on disk with a sufficient span is skipped
unless ``--force`` is passed. Network required (``uv sync --extra data``); the TEST window is not a
concern here -- ingest stores full history and the research driver seals 2024 via the split.

Run: ``.venv/Scripts/python.exe scripts/build_tsmom_universe.py [--force]``
"""

from __future__ import annotations

import sys

import pandas as pd

from fxlab.config import load_config
from fxlab.data.ingest_dukascopy import _DUKA_INSTRUMENT, fetch_dukascopy
from fxlab.data.store import bars_path, load_bars, save_bars

PANEL_STAGE = "panel"

# The user-chosen core cross-asset panel (~13-14 liquid instruments), by asset class.
PANEL: dict[str, list[str]] = {
    "fx": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"],
    "metal": ["XAUUSD", "XAGUSD"],
    "energy": ["BRENT", "WTI"],
    "index": ["SPX500", "NAS100", "GER40"],
}
UNIVERSE: list[str] = [s for group in PANEL.values() for s in group]

# Validation gate (documented, not tuned): to contribute a causal 252-day momentum + 60-day vol
# signal with usable train/val coverage, an instrument must start by end-2018 and carry >= 800 D1
# bars. Anything short of that is dropped and logged so a late/thin series never distorts the panel.
MIN_ROWS = 800
MUST_START_BY = pd.Timestamp("2018-12-31", tz="UTC")


def _asset_class(symbol: str) -> str:
    return next((cls for cls, syms in PANEL.items() if symbol in syms), "?")


def _validate(bars: pd.DataFrame) -> tuple[bool, str]:
    if bars is None or bars.empty:
        return False, "no bars returned"
    n = len(bars)
    start = bars.index.min()
    if n < MIN_ROWS:
        return False, f"only {n} D1 bars (< {MIN_ROWS})"
    if start > MUST_START_BY:
        return False, f"starts {start.date()} (after {MUST_START_BY.date()})"
    return True, "ok"


def _already_good(cfg, symbol: str) -> bool:
    path = bars_path(cfg.data_dir, symbol, "D1", stage=PANEL_STAGE)
    if not path.exists():
        return False
    try:
        ok, _ = _validate(load_bars(cfg.data_dir, symbol, "D1", stage=PANEL_STAGE))
    except Exception:
        return False
    return ok


def main() -> None:
    force = "--force" in sys.argv
    cfg = load_config()
    dr = cfg.data.date_range
    start, end = dr.get("start", "2015-01-01"), dr.get("end", "2025-12-31")

    print("\nBuilding core cross-asset TSMOM universe (real Dukascopy D1)")
    print(f"date range {start}..{end}; stage='{PANEL_STAGE}'; force={force}")
    print(f"{'symbol':<8}{'class':<7}{'rows':>7}  {'span':<26}{'status'}")
    print("-" * 72)

    kept, dropped = [], []
    for symbol in UNIVERSE:
        cls = _asset_class(symbol)
        if symbol not in _DUKA_INSTRUMENT:
            print(f"{symbol:<8}{cls:<7}{'-':>7}  {'':<26}NO MAPPING — skipped")
            dropped.append(symbol)
            continue
        if not force and _already_good(cfg, symbol):
            bars = load_bars(cfg.data_dir, symbol, "D1", stage=PANEL_STAGE)
            span = f"{bars.index.min().date()}..{bars.index.max().date()}"
            print(f"{symbol:<8}{cls:<7}{len(bars):>7}  {span:<26}cached")
            kept.append(symbol)
            continue
        try:
            bars = fetch_dukascopy(symbol, "D1", start, end)
        except Exception as exc:  # network / mapping / API failure -> drop-and-log, keep going
            print(f"{symbol:<8}{cls:<7}{'-':>7}  {'':<26}FETCH FAILED: {type(exc).__name__}")
            dropped.append(symbol)
            continue
        ok, why = _validate(bars)
        span = (
            f"{bars.index.min().date()}..{bars.index.max().date()}"
            if bars is not None and not bars.empty else ""
        )
        if not ok:
            print(f"{symbol:<8}{cls:<7}{len(bars) if bars is not None else 0:>7}  "
                  f"{span:<26}DROPPED: {why}")
            dropped.append(symbol)
            continue
        save_bars(bars, cfg.data_dir, symbol, "D1", stage=PANEL_STAGE)
        print(f"{symbol:<8}{cls:<7}{len(bars):>7}  {span:<26}saved")
        kept.append(symbol)

    print("-" * 72)
    print(f"kept {len(kept)}/{len(UNIVERSE)}: {', '.join(kept)}")
    if dropped:
        print(f"dropped: {', '.join(dropped)}")
    classes_kept = sorted({_asset_class(s) for s in kept})
    print(f"asset classes represented: {', '.join(classes_kept)}")


if __name__ == "__main__":
    main()
