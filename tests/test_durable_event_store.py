"""Focused tests for Phase 9 SQLite audit and checkpoint durability."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from fxlab.execution.durable_event_store import (
    DurableStoreError,
    SQLiteEventStore,
    StoredCheckpoint,
)
from fxlab.execution.event_ledger import AuditComponent, AuditEventType, EventLedger

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)


def append(ledger: EventLedger, value: int = 1) -> None:
    ledger.append(
        AuditEventType.MARKET_EVENT,
        occurred_at=NOW,
        component=AuditComponent.REPLAY,
        payload={"value": value},
    )


def test_create_append_restart_and_sequence_continuation(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path, "session-1")
    ledger = EventLedger("session-1", durable_store=store)
    append(ledger)
    store.close()

    reopened = SQLiteEventStore(path, "session-1")
    resumed = EventLedger("session-1", durable_store=reopened)
    append(resumed, 2)
    assert [event.sequence for event in reopened.load_events()] == [1, 2]
    assert reopened.last_sequence() == 2


def test_same_timestamp_keeps_sequence_order(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite", "session")
    ledger = EventLedger("session", durable_store=store)
    append(ledger, 1)
    append(ledger, 2)
    assert [event.payload["value"] for event in store.load_events()] == [1, 2]


def test_concurrent_durable_append_is_contiguous(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite", "session")
    ledger = EventLedger("session", durable_store=store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda value: append(ledger, value), range(40)))
    assert [event.sequence for event in store.load_events()] == list(range(1, 41))


def test_session_mismatch_fails_closed(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    SQLiteEventStore(path, "one").close()
    with pytest.raises(DurableStoreError, match="session_mismatch"):
        SQLiteEventStore(path, "two")


def test_checksum_corruption_is_detected(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path, "session")
    append(EventLedger("session", durable_store=store))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE events SET payload_json = '{\"value\":9}'")
    with pytest.raises(DurableStoreError, match="ledger_corrupted"):
        store.load_events()


def test_missing_sequence_is_detected(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path, "session")
    ledger = EventLedger("session", durable_store=store)
    append(ledger)
    append(ledger, 2)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM events WHERE sequence = 1")
    with pytest.raises(DurableStoreError, match="ledger_corrupted"):
        store.verify_integrity()


def test_event_schema_mismatch_is_detected(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path, "session")
    append(EventLedger("session", durable_store=store))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE events SET event_schema_version = 99")
    with pytest.raises(DurableStoreError, match="event_schema_incompatible"):
        store.load_events()


def checkpoint(sequence: int = 0) -> StoredCheckpoint:
    return StoredCheckpoint(
        session_id="session",
        created_at=NOW,
        last_event_sequence=sequence,
        software_version="1.0",
        configuration_fingerprint="config",
        replay_dataset_fingerprint="dataset",
        state={"value": 1},
    )


def test_checkpoint_transaction_and_restart(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path, "session")
    store.store_checkpoint(checkpoint())
    store.close()
    loaded = SQLiteEventStore(path, "session").load_latest_checkpoint()
    assert loaded is not None
    assert loaded.state["value"] == 1
    assert loaded.last_event_sequence == 0


def test_checkpoint_checksum_corruption_is_detected(tmp_path) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path, "session")
    store.store_checkpoint(checkpoint())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE checkpoints SET state_json = '{\"value\":2}'")
    with pytest.raises(DurableStoreError, match="checkpoint_corrupted"):
        store.load_latest_checkpoint()


def test_failed_checkpoint_does_not_replace_prior_valid_checkpoint(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite", "session")
    store.store_checkpoint(checkpoint())
    with pytest.raises(DurableStoreError, match="checkpoint_sequence_mismatch"):
        store.store_checkpoint(checkpoint(1))
    assert store.load_latest_checkpoint().state["value"] == 1  # type: ignore[union-attr]


def test_no_mutating_event_api_is_exposed(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite", "session")
    for name in ("update_event", "delete_event", "truncate", "reset_sequence"):
        assert not hasattr(store, name)
