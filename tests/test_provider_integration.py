"""Integration tests for provider-backed history, replay audit, and recovery identity."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from fxlab.data import (
    InMemoryBarProvider,
    ProviderCapability,
    ProviderDescriptor,
    ProviderGateway,
    ProviderRegistry,
    ProviderRoute,
)
from fxlab.execution.event_ledger import AuditComponent, AuditEventType
from fxlab.execution.market_data import MarketDataStream
from fxlab.execution.paper_session import HistoricalBarReplay
from test_market_data import MockBroker
from test_paper_session import NoSignalSetup, make_session


def bars() -> pd.DataFrame:
    index = pd.date_range("2026-08-25 10:00", periods=3, freq="5min", tz="UTC")
    close = np.array([1.1, 1.101, 1.102], dtype="float64")
    result = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.0005,
            "low": close - 0.0005,
            "close": close,
            "volume": np.ones(3, dtype="float64"),
        },
        index=index,
        dtype="float64",
    )
    result.index.name = "ts_open"
    result.attrs.update(symbol="EURUSD", timeframe="M5")
    return result


def test_market_data_stream_uses_validated_provider_history() -> None:
    registry = ProviderRegistry()
    registry.register(
        InMemoryBarProvider(
            ProviderDescriptor(
                "memory",
                "1",
                frozenset(
                    {
                        ProviderCapability.HISTORICAL_BARS,
                        ProviderCapability.POINT_IN_TIME,
                    }
                ),
            ),
            {("EURUSD", "M5"): bars()},
        )
    )
    registry.freeze()
    broker = MockBroker()
    stream = MarketDataStream(
        broker,
        ["EURUSD"],
        time_provider=lambda: datetime(2026, 8, 25, 10, 15, tzinfo=UTC),
        historical_gateway=ProviderGateway(registry),
        historical_route=ProviderRoute("memory", ProviderCapability.HISTORICAL_BARS),
    )
    result = stream.get_closed_bars("EURUSD", "M5", 3)
    assert len(result) == 3
    assert result.attrs["provider_id"] == "memory"
    assert broker.historical_bars_mock.empty


def test_session_records_dataset_binding_and_market_provenance() -> None:
    session, _, _ = make_session(setup=NoSignalSetup())
    session.start()
    session.poll_once()
    events = session.event_ledger.events()
    bound = next(event for event in events if event.event_type is AuditEventType.DATASET_BOUND)
    market = next(event for event in events if event.event_type is AuditEventType.MARKET_EVENT)
    assert bound.component is AuditComponent.MARKET_DATA_PROVIDER
    assert market.payload["provider_id"] == bound.payload["provider_id"]
    assert market.payload["dataset_id"] == bound.payload["dataset_id"]
    assert market.payload["normalization_version"] == "1"


def test_replay_provider_identity_changes_dataset_identity() -> None:
    first, _, _ = make_session(setup=NoSignalSetup())
    second, _, _ = make_session(setup=NoSignalSetup())
    second.replay.provider_version = "2"
    assert first.replay.provider_compatibility_snapshot() != (
        second.replay.provider_compatibility_snapshot()
    )


def test_external_provider_mapping_identity_is_bound_into_replay_compatibility() -> None:
    replay = HistoricalBarReplay(
        {"EURUSD": bars()},
        "M5",
        provider_id="dukascopy",
        provider_version="1",
        normalization_version="dukascopy_bid_v1",
        mapping_identity="a" * 64,
    )
    snapshot = replay.provider_compatibility_snapshot()
    assert snapshot["mapping_identity"] == "a" * 64
    assert snapshot["fallback_policy"] == "none"
