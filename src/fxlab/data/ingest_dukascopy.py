"""Data ingestion (Phase 1).

Two sources:
  * ``synthetic`` — a deterministic, offline, geometric-random-walk generator used by
    the test suite and for running the pipeline without a network. It is NOT market
    data and must never be used to make any performance claim.
  * ``dukascopy`` — external historical BID bars through the bounded Phase 16
    provider boundary.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from .dukascopy_provider import (
    DUKASCOPY_MAPPING_FINGERPRINT,
    DukascopyConnectorSettings,
    DukascopyHistoricalBarsProvider,
    DukascopyHttpTransport,
    DukascopyTransport,
)
from .provider import BarQuery, CanonicalInstrument, ProviderCapability, ProviderRoute
from .providers import ProviderGateway, ProviderRegistry
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


def fetch_dukascopy(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    offer_side: str = "bid",
    *,
    transport: DukascopyTransport | None = None,
    settings: DukascopyConnectorSettings | None = None,
) -> pd.DataFrame:
    """Fetch validated historical BID bars through the Phase 11 provider gateway."""
    if offer_side.strip().lower() != "bid":
        raise ValueError("Phase 16 supports Dukascopy BID bars only")
    start_at, end_at = _explicit_utc(start, "start"), _explicit_utc(end, "end")
    query = BarQuery(
        CanonicalInstrument(symbol),
        timeframe,
        start_at,
        end_at,
        end_at,
    )
    provider = DukascopyHistoricalBarsProvider(
        transport or DukascopyHttpTransport(),
        settings=settings or DukascopyConnectorSettings(),
    )
    registry = ProviderRegistry()
    registry.register(provider)
    registry.freeze()
    route = ProviderRoute(
        provider.descriptor.provider_id,
        ProviderCapability.HISTORICAL_BARS,
        mapping_identity=DUKASCOPY_MAPPING_FINGERPRINT,
        normalization_version=provider.descriptor.normalization_version,
    )
    dataset = ProviderGateway(registry).fetch_bars(route, query)
    return dataset.frame


def _explicit_utc(value: str, field_name: str) -> datetime:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return parsed.tz_convert("UTC").to_pydatetime()


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
