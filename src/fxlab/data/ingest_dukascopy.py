"""Data ingestion (Phase 1).

Two sources:
  * ``synthetic`` — a deterministic, offline, geometric-random-walk generator used by
    the test suite and for running the pipeline without a network. It is NOT market
    data and must never be used to make any performance claim.
  * ``dukascopy`` — real free tick/OHLC data via the optional ``dukascopy-python`` extra.

The dukascopy adapter is classified UNPROVEN until exercised against the live API
(constant names are resolved defensively at call time).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .schema import ensure_bars, timeframe_to_offset

# --------------------------------------------------------------------------- synthetic


def generate_synthetic_bars(
    symbol: str = "EURUSD",
    timeframe: str = "M5",
    n_bars: int = 20_000,
    start: str = "2018-01-01",
    seed: int = 7,
    base_price: float | None = None,
    vol_pips: float = 8.0,
) -> pd.DataFrame:
    """Deterministic synthetic OHLCV (weekends removed). Positive by construction."""
    pip = 0.01 if symbol.upper().endswith("JPY") else 0.0001
    if base_price is None:
        base_price = 110.0 if symbol.upper().endswith("JPY") else 1.10

    # Generate a surplus of timestamps, drop weekends, keep the first n_bars.
    candidates = pd.date_range(
        pd.Timestamp(start, tz="UTC"),
        periods=int(n_bars * 1.45) + 10,
        freq=timeframe_to_offset(timeframe),
        tz="UTC",
    )
    idx = candidates[candidates.dayofweek < 5][:n_bars]
    n = len(idx)

    rng = np.random.default_rng(seed)
    rel_sigma = (vol_pips * pip) / base_price
    log_rets = rng.normal(0.0, rel_sigma, size=n)
    close = base_price * np.exp(np.cumsum(log_rets))
    open_ = np.empty(n)
    open_[0] = base_price
    open_[1:] = close[:-1]

    u_hi = np.abs(rng.normal(0.0, rel_sigma, size=n))
    u_lo = np.abs(rng.normal(0.0, rel_sigma, size=n))
    high = np.maximum(open_, close) * (1.0 + u_hi)
    low = np.minimum(open_, close) * (1.0 - u_lo)
    volume = rng.integers(50, 500, size=n).astype("float64")

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    df.index.name = "ts_open"
    return ensure_bars(df, symbol, timeframe)


# --------------------------------------------------------------------------- dukascopy

_DUKA_INTERVAL = {
    "M1": "INTERVAL_MIN_1", "M5": "INTERVAL_MIN_5", "M15": "INTERVAL_MIN_15",
    "M30": "INTERVAL_MIN_30", "H1": "INTERVAL_HOUR_1", "H4": "INTERVAL_HOUR_4",
    "D1": "INTERVAL_DAY_1",
}
# Core cross-asset panel for the multi-asset TSMOM build (return-space, vol-scaled portfolio).
# Deliberately restricted to LIQUID instruments with a uniform, well-understood cost regime
# (crypto and thin/short-history instruments are excluded on purpose). Constant names verified
# against dukascopy-python 4.0.1's ``instruments`` module.
_DUKA_INSTRUMENT = {
    # FX majors
    "EURUSD": "INSTRUMENT_FX_MAJORS_EUR_USD",
    "GBPUSD": "INSTRUMENT_FX_MAJORS_GBP_USD",
    "USDJPY": "INSTRUMENT_FX_MAJORS_USD_JPY",
    "AUDUSD": "INSTRUMENT_FX_MAJORS_AUD_USD",
    "USDCAD": "INSTRUMENT_FX_MAJORS_USD_CAD",
    "USDCHF": "INSTRUMENT_FX_MAJORS_USD_CHF",
    "NZDUSD": "INSTRUMENT_FX_MAJORS_NZD_USD",
    # Metals
    "XAUUSD": "INSTRUMENT_FX_METALS_XAU_USD",
    "XAGUSD": "INSTRUMENT_FX_METALS_XAG_USD",
    # Energy (E_Brent = Brent crude, E_Light = WTI/light crude CFDs)
    "BRENT": "INSTRUMENT_CMD_ENERGY_E_BRENT",
    "WTI": "INSTRUMENT_CMD_ENERGY_E_LIGHT",
    # Equity indices (CFDs)
    "SPX500": "INSTRUMENT_IDX_AMERICA_E_SANDP_500",
    "NAS100": "INSTRUMENT_IDX_AMERICA_E_NQ_100",
    "GER40": "INSTRUMENT_IDX_EUROPE_E_DAAX",
}


def fetch_dukascopy(
    symbol: str, timeframe: str, start: str, end: str, offer_side: str = "bid"
) -> pd.DataFrame:
    """Fetch real bars from Dukascopy. Requires the optional extra: ``uv sync --extra data``."""
    try:
        import dukascopy_python
        from dukascopy_python import instruments as duka_instruments
    except ImportError as exc:  # pragma: no cover - network/optional path
        raise RuntimeError(
            "dukascopy-python is not installed. Run: python -m uv sync --extra data"
        ) from exc

    if symbol not in _DUKA_INSTRUMENT:
        raise ValueError(f"no Dukascopy instrument mapping for {symbol!r}")
    interval = getattr(dukascopy_python, _DUKA_INTERVAL[timeframe])
    instrument = getattr(duka_instruments, _DUKA_INSTRUMENT[symbol])
    side = (
        dukascopy_python.OFFER_SIDE_BID
        if offer_side == "bid"
        else dukascopy_python.OFFER_SIDE_ASK
    )
    raw = dukascopy_python.fetch(
        instrument,
        interval,
        side,
        pd.Timestamp(start).to_pydatetime(),
        pd.Timestamp(end).to_pydatetime(),
    )
    return ensure_bars(raw, symbol, timeframe)


# --------------------------------------------------------------------------- dispatch


def ingest(
    symbol: str,
    timeframe: str,
    source: str = "synthetic",
    start: str | None = None,
    end: str | None = None,
    n_bars: int = 20_000,
    seed: int = 7,
) -> pd.DataFrame:
    if source == "synthetic":
        return generate_synthetic_bars(
            symbol, timeframe, n_bars=n_bars, start=start or "2018-01-01", seed=seed
        )
    if source == "dukascopy":
        if not (start and end):
            raise ValueError("dukascopy ingest requires --from and --to")
        return fetch_dukascopy(symbol, timeframe, start, end)
    raise ValueError(f"unknown source {source!r} (expected 'synthetic' or 'dukascopy')")
