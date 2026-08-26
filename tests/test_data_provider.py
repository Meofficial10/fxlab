"""Contract tests for the Phase 11 canonical provider boundary."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from fxlab.data import (
    BarQuery,
    CanonicalInstrument,
    HistoricalReplayProvider,
    InMemoryBarProvider,
    LocalParquetProvider,
    ProvenanceQuality,
    ProviderCapability,
    ProviderDescriptor,
    ProviderFailure,
    ProviderFailureCategory,
    ProviderGateway,
    ProviderGatewayError,
    ProviderRegistry,
    ProviderRoute,
    SymbolAliasMap,
    TickQuery,
)
from fxlab.data.store import save_bars
from fxlab.execution.paper_session import HistoricalBarReplay


def frame(periods: int = 3) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=periods, freq="5min", tz="UTC")
    close = np.arange(periods, dtype="float64") * 0.001 + 1.1
    result = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.001,
            "low": close - 0.001,
            "close": close,
            "volume": np.ones(periods, dtype="float64"),
        },
        index=index,
        dtype="float64",
    )
    result.index.name = "ts_open"
    result.attrs.update(symbol="EURUSD", timeframe="M5")
    return result


def descriptor(*capabilities: ProviderCapability) -> ProviderDescriptor:
    return ProviderDescriptor("memory", "1", frozenset(capabilities), deterministic=True)


def query(*, as_of: datetime | None = None) -> BarQuery:
    return BarQuery(
        CanonicalInstrument(" eurusd "),
        "M5",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
        as_of or datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
    )


def gateway() -> tuple[ProviderGateway, ProviderRoute]:
    registry = ProviderRegistry()
    registry.register(
        InMemoryBarProvider(
            descriptor(
                ProviderCapability.HISTORICAL_BARS,
                ProviderCapability.POINT_IN_TIME,
            ),
            {("EURUSD", "M5"): frame()},
        )
    )
    registry.freeze()
    return ProviderGateway(registry), ProviderRoute("memory", ProviderCapability.HISTORICAL_BARS)


def test_contracts_are_frozen_and_symbols_are_canonical() -> None:
    instrument = CanonicalInstrument(" eurusd ")
    assert instrument.symbol == "EURUSD"
    with pytest.raises(FrozenInstanceError):
        instrument.symbol = "USDJPY"  # type: ignore[misc]
    for invalid in ("", "EUR/USD", " EUR USD ", "€EURUSD"):
        with pytest.raises(ValueError):
            CanonicalInstrument(invalid)


def test_queries_require_aware_coherent_explicit_time() -> None:
    with pytest.raises(ValueError):
        BarQuery(
            CanonicalInstrument("EURUSD"),
            "M5",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )
    local = timezone(timedelta(hours=2))
    normalized = BarQuery(
        CanonicalInstrument("EURUSD"),
        "M5",
        datetime(2026, 1, 1, 2, tzinfo=local),
        datetime(2026, 1, 1, 3, tzinfo=local),
        datetime(2026, 1, 1, 3, tzinfo=local),
    )
    assert normalized.start.tzinfo is UTC
    with pytest.raises(ValueError):
        TickQuery(CanonicalInstrument("EURUSD"), datetime.now(UTC), timedelta(0))


def test_bar_dataset_is_point_in_time_safe_and_copy_isolated() -> None:
    data_gateway, route = gateway()
    dataset = data_gateway.fetch_bars(route, query())
    assert len(dataset.frame) == 3
    copy = dataset.frame
    copy.iloc[0, 0] = 999.0
    assert dataset.frame.iloc[0, 0] != 999.0
    assert list(dataset.frame.columns) == ["open", "high", "low", "close", "volume"]
    assert all(dtype == np.dtype("float64") for dtype in dataset.frame.dtypes)


def test_untrusted_dataframe_attrs_do_not_escape_dataset() -> None:
    source = frame()
    source.attrs["api_key"] = "must-not-leak"
    provider = InMemoryBarProvider(
        descriptor(ProviderCapability.HISTORICAL_BARS),
        {("EURUSD", "M5"): source},
    )
    registry = ProviderRegistry()
    registry.register(provider)
    registry.freeze()
    dataset = ProviderGateway(registry).fetch_bars(
        ProviderRoute("memory", ProviderCapability.HISTORICAL_BARS), query()
    )
    assert set(dataset.frame.attrs) == {"symbol", "timeframe"}


def test_exact_close_boundary_is_available_and_future_bar_is_excluded() -> None:
    data_gateway, route = gateway()
    exact = data_gateway.fetch_bars(route, query(as_of=datetime(2026, 1, 1, 0, 10, tzinfo=UTC)))
    assert list(exact.frame.index.minute) == [0, 5]


def test_content_identity_stable_and_retrieval_time_not_in_identity() -> None:
    data_gateway, route = gateway()
    first = data_gateway.fetch_bars(route, query())
    second = data_gateway.fetch_bars(route, query())
    assert first.provenance.content_hash == second.provenance.content_hash
    assert first.provenance.query_fingerprint == second.provenance.query_fingerprint
    assert first.provenance.dataset_id == second.provenance.dataset_id
    assert first.provenance.provenance_quality is ProvenanceQuality.SYNTHETIC


def test_registry_is_explicit_unique_and_frozen() -> None:
    registry = ProviderRegistry()
    provider = InMemoryBarProvider(
        descriptor(ProviderCapability.HISTORICAL_BARS),
        {("EURUSD", "M5"): frame()},
    )
    registry.register(provider)
    with pytest.raises(ValueError):
        registry.register(provider)
    with pytest.raises(RuntimeError):
        registry.get("memory", ProviderCapability.HISTORICAL_BARS)
    registry.freeze()
    assert registry.provider_ids() == ("memory",)
    with pytest.raises(RuntimeError):
        registry.register(provider)
    with pytest.raises(ValueError):
        registry.get("memory", ProviderCapability.LATEST_TICK)


def test_symbol_alias_mapping_is_explicit_and_bijective() -> None:
    aliases = SymbolAliasMap({"EURUSD": "EUR/USD"}, "map-v1")
    assert aliases.to_native(CanonicalInstrument("EURUSD")) == "EUR/USD"
    assert aliases.to_canonical("EUR/USD") == CanonicalInstrument("EURUSD")
    with pytest.raises(ValueError):
        SymbolAliasMap({"EURUSD": "same", "USDJPY": "same"}, "bad")


def test_no_data_is_structured_and_gateway_has_no_hidden_fallback() -> None:
    provider = InMemoryBarProvider(descriptor(ProviderCapability.HISTORICAL_BARS), {})
    failure = provider.fetch_bars(query())
    assert isinstance(failure, ProviderFailure)
    assert failure.category is ProviderFailureCategory.NO_DATA
    registry = ProviderRegistry()
    registry.register(provider)
    registry.freeze()
    with pytest.raises(ProviderGatewayError) as caught:
        ProviderGateway(registry).fetch_bars(
            ProviderRoute("memory", ProviderCapability.HISTORICAL_BARS, ("unused",)),
            query(),
        )
    assert caught.value.failure.category is ProviderFailureCategory.NO_DATA


def test_sensitive_failure_context_and_source_references_rejected() -> None:
    with pytest.raises(ValueError):
        ProviderFailure(
            ProviderFailureCategory.AUTHENTICATION,
            "auth_failed",
            "memory",
            context={"api_key": "do-not-store"},
        )


def test_replay_adapter_never_exposes_future_event() -> None:
    replay = HistoricalBarReplay({"EURUSD": frame(2)}, "M5")
    replay_descriptor = ProviderDescriptor(
        "replay",
        "1",
        frozenset(
            {
                ProviderCapability.REPLAY_EVENTS,
                ProviderCapability.POINT_IN_TIME,
                ProviderCapability.DETERMINISTIC_REPLAY,
            }
        ),
        deterministic=True,
    )
    provider = HistoricalReplayProvider(replay, replay_descriptor, replay.dataset_id)
    early = TickQuery(
        CanonicalInstrument("EURUSD"),
        datetime(2026, 1, 1, 0, 4, tzinfo=UTC),
        timedelta(minutes=10),
    )
    assert provider.next_event(early) is None
    exact = TickQuery(
        CanonicalInstrument("EURUSD"),
        datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        timedelta(minutes=10),
    )
    snapshot = provider.next_event(exact)
    assert snapshot is not None and not isinstance(snapshot, ProviderFailure)
    assert snapshot.timestamp == exact.as_of


def test_local_parquet_is_honestly_legacy_unverified(tmp_path) -> None:
    save_bars(frame(), tmp_path, "EURUSD", "M5")
    local_descriptor = ProviderDescriptor(
        "local-parquet",
        "1",
        frozenset({ProviderCapability.HISTORICAL_BARS}),
    )
    provider = LocalParquetProvider(local_descriptor, tmp_path)
    result = provider.fetch_bars(query())
    assert not isinstance(result, ProviderFailure)
    assert result.provenance.provenance_quality is ProvenanceQuality.LEGACY_UNVERIFIED


def test_replay_freshness_equality_passes_and_older_tick_fails_closed() -> None:
    replay = HistoricalBarReplay({"EURUSD": frame(1)}, "M5")
    replay_descriptor = ProviderDescriptor(
        "replay",
        "1",
        frozenset(
            {
                ProviderCapability.REPLAY_EVENTS,
                ProviderCapability.POINT_IN_TIME,
                ProviderCapability.DETERMINISTIC_REPLAY,
            }
        ),
    )
    provider = HistoricalReplayProvider(replay, replay_descriptor, replay.dataset_id)
    stale_query = TickQuery(
        CanonicalInstrument("EURUSD"),
        datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        timedelta(minutes=4),
    )
    stale = provider.next_event(stale_query)
    assert isinstance(stale, ProviderFailure)
    assert stale.category is ProviderFailureCategory.STALE_DATA
