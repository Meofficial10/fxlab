"""Focused tests for the Phase 8 immutable in-memory audit ledger."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from math import inf, nan
from threading import Lock

import pytest

from fxlab.execution.broker import Tick
from fxlab.execution.event_ledger import (
    AuditComponent,
    AuditEvent,
    AuditEventType,
    AuditLedgerError,
    EventCorrelation,
    EventLedger,
    deterministic_signal_id,
)
from fxlab.execution.signal_engine import SignalEvent

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)


def ledger(session_id: str = "paper-session-001") -> EventLedger:
    return EventLedger(session_id, time_provider=lambda: NOW)


def append(item: EventLedger, **changes: object):
    values = {
        "event_type": AuditEventType.MARKET_EVENT,
        "occurred_at": NOW,
        "component": AuditComponent.REPLAY,
        "payload": {"symbol": "EURUSD", "bid": 1.1},
    }
    values.update(changes)
    return item.append(**values)  # type: ignore[arg-type]


def test_event_and_correlation_are_frozen_and_event_is_ledger_owned() -> None:
    item = ledger()
    correlation = EventCorrelation(signal_id="signal-1")
    event = append(item, correlation=correlation)
    with pytest.raises(FrozenInstanceError):
        event.sequence = 99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        correlation.signal_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        AuditEvent()  # type: ignore[call-arg]


def test_sequence_ids_and_same_timestamp_ordering() -> None:
    item = ledger()
    first = append(item)
    second = append(item)
    assert (first.sequence, second.sequence) == (1, 2)
    assert first.event_id == "paper-session-001:00000000000000000001"
    assert second.event_id == "paper-session-001:00000000000000000002"
    assert first.occurred_at == second.occurred_at
    assert item.events() == (first, second)


def test_timezone_required_and_normalized() -> None:
    item = ledger()
    with pytest.raises(ValueError, match="timezone-aware"):
        append(item, occurred_at=datetime(2026, 8, 26, 10, 0))
    local = datetime(2026, 8, 26, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert append(item, occurred_at=local).occurred_at == NOW


def test_payload_is_recursively_snapshotted_and_immutable() -> None:
    item = ledger()
    source = {"nested": {"values": [1, 2]}, "when": NOW}
    event = append(item, payload=source)
    source["nested"]["values"].append(3)  # type: ignore[index,union-attr]
    assert event.payload["nested"]["values"] == (1, 2)  # type: ignore[index]
    assert event.payload["when"] == "2026-08-26T10:00:00Z"
    with pytest.raises(TypeError):
        event.payload["new"] = 1  # type: ignore[index]
    assert isinstance(item.events(), tuple)


@pytest.mark.parametrize("bad", [nan, inf, -inf])
def test_invalid_numeric_does_not_consume_sequence(bad: float) -> None:
    item = ledger()
    with pytest.raises(ValueError, match="finite"):
        append(item, payload={"value": bad})
    assert append(item).sequence == 1


@pytest.mark.parametrize(
    "bad",
    [b"secret", lambda: None, object(), Lock(), Tick("EURUSD", NOW, 1.0, 1.0, 1.0)],
)
def test_unsupported_objects_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        append(ledger(), payload={"value": bad})


@pytest.mark.parametrize(
    "key",
    ["password", "Secret", "TOKEN", "api-key", "apikey", "authorization",
     "credential", "credentials", "private key"],
)
def test_sensitive_keys_are_rejected_recursively(key: str) -> None:
    with pytest.raises(ValueError, match="sensitive"):
        append(ledger(), payload={"safe": {key: "value"}})


def test_non_string_mapping_key_rejected() -> None:
    with pytest.raises(ValueError, match="keys"):
        append(ledger(), payload={"nested": {1: "bad"}})


def test_internal_append_failure_raises_and_does_not_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = ledger()
    original = item._store_event

    def fail(event: AuditEvent) -> None:
        raise RuntimeError("storage failed")

    monkeypatch.setattr(item, "_store_event", fail)
    with pytest.raises(AuditLedgerError):
        append(item)
    monkeypatch.setattr(item, "_store_event", original)
    assert append(item).sequence == 1


def test_concurrent_append_is_contiguous_and_unique() -> None:
    item = ledger()
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(lambda _: append(item), range(100)))
    assert sorted(event.sequence for event in events) == list(range(1, 101))
    assert len({event.event_id for event in events}) == 100


def test_ledger_has_no_mutation_or_persistence_api() -> None:
    item = ledger()
    for name in ("update", "delete", "replace", "truncate", "reset", "save", "load"):
        assert not hasattr(item, name)


def test_separate_ledgers_have_independent_session_history() -> None:
    first = ledger("one")
    second = ledger("two")
    assert append(first).sequence == 1
    assert append(second).sequence == 1
    assert first.events()[0].session_id != second.events()[0].session_id


def test_reconciliation_audit_contract_is_stable() -> None:
    assert AuditEventType.RECONCILIATION_STARTED.value == "reconciliation_started"
    assert AuditEventType.RECONCILIATION_RESOLVED.value == "reconciliation_resolved"
    assert AuditEventType.RECONCILIATION_UNRESOLVED.value == "reconciliation_unresolved"
    assert AuditComponent.RECONCILIATION_ENGINE.value == "reconciliation_engine"


def test_runtime_state_audit_contract_is_stable() -> None:
    assert AuditEventType.RUNTIME_STATE_CHANGED.value == "runtime_state_changed"


def test_correlation_validation() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        EventCorrelation(position_id=" ")


def test_signal_identity_matches_risk_format() -> None:
    signal = SignalEvent("model_a", "eurusd", "m5", 1, NOW, 4)
    assert deterministic_signal_id(signal) == (
        "model_a-EURUSD-M5-20260826T100000000000Z-LONG"
    )
