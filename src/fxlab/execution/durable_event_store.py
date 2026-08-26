"""Small SQLite-backed durable store for audit events and safe checkpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from types import MappingProxyType

from .event_ledger import (
    AuditComponent,
    AuditEvent,
    AuditEventType,
    EventCorrelation,
)

STORE_FORMAT_VERSION = 1
EVENT_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 1


class DurableStoreError(RuntimeError):
    """A durable-store compatibility or integrity failure."""


@dataclass(frozen=True)
class StoredCheckpoint:
    session_id: str
    created_at: datetime
    last_event_sequence: int
    software_version: str
    configuration_fingerprint: str
    replay_dataset_fingerprint: str
    state: Mapping[str, object]
    checkpoint_schema_version: int = CHECKPOINT_SCHEMA_VERSION


class SQLiteEventStore:
    """Synchronous SQLite persistence for one logical paper session.

    WAL plus ``synchronous=FULL`` protects committed SQLite transactions against
    ordinary process interruption. It does not claim durability beyond the filesystem
    and hardware guarantees SQLite receives.
    """

    def __init__(self, path: Path | str, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        self.path = Path(path)
        self.session_id = session_id.strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append(self, event: AuditEvent) -> None:
        if not isinstance(event, AuditEvent):
            raise ValueError("event must be an AuditEvent")
        document = _event_document(event)
        checksum = _checksum(document)
        with self._lock, self._connection:
            last = self._metadata_int("last_sequence")
            if event.session_id != self.session_id:
                raise DurableStoreError("session_mismatch")
            if event.sequence != last + 1:
                raise DurableStoreError("non_contiguous_sequence")
            self._connection.execute(
                """INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.session_id,
                    event.sequence,
                    event.event_id,
                    event.event_type.value,
                    _utc_text(event.occurred_at),
                    event.component.value,
                    _canonical_json(document["correlation"]),
                    _canonical_json(document["payload"]),
                    checksum,
                    EVENT_SCHEMA_VERSION,
                ),
            )
            self._set_metadata("last_sequence", str(event.sequence))

    def load_events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT session_id, sequence, event_id, event_type, occurred_at,
                component, correlation_json, payload_json, checksum,
                event_schema_version FROM events ORDER BY sequence"""
            ).fetchall()
            expected_last = self._metadata_int("last_sequence")
        events: list[AuditEvent] = []
        for expected, row in enumerate(rows, start=1):
            event = self._decode_event(row, expected)
            events.append(event)
        if len(events) != expected_last:
            raise DurableStoreError("ledger_corrupted")
        return tuple(events)

    def last_sequence(self) -> int:
        with self._lock:
            return self._metadata_int("last_sequence")

    def verify_integrity(self) -> None:
        self.load_events()

    def store_checkpoint(self, checkpoint: StoredCheckpoint) -> None:
        _validate_checkpoint(checkpoint, self.session_id)
        state_json = _canonical_json(checkpoint.state)
        document = {
            "checkpoint_schema_version": checkpoint.checkpoint_schema_version,
            "session_id": checkpoint.session_id,
            "created_at": _utc_text(checkpoint.created_at),
            "last_event_sequence": checkpoint.last_event_sequence,
            "software_version": checkpoint.software_version,
            "configuration_fingerprint": checkpoint.configuration_fingerprint,
            "replay_dataset_fingerprint": checkpoint.replay_dataset_fingerprint,
            "state": json.loads(state_json),
        }
        checksum = _checksum(document)
        with self._lock, self._connection:
            if checkpoint.last_event_sequence != self._metadata_int("last_sequence"):
                raise DurableStoreError("checkpoint_sequence_mismatch")
            self._connection.execute(
                """INSERT INTO checkpoints
                (session_id, created_at, last_event_sequence, software_version,
                 configuration_fingerprint, replay_dataset_fingerprint, state_json,
                 checksum, checkpoint_schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint.session_id,
                    _utc_text(checkpoint.created_at),
                    checkpoint.last_event_sequence,
                    checkpoint.software_version,
                    checkpoint.configuration_fingerprint,
                    checkpoint.replay_dataset_fingerprint,
                    state_json,
                    checksum,
                    checkpoint.checkpoint_schema_version,
                ),
            )

    def load_latest_checkpoint(self) -> StoredCheckpoint | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT session_id, created_at, last_event_sequence,
                software_version, configuration_fingerprint,
                replay_dataset_fingerprint, state_json, checksum,
                checkpoint_schema_version FROM checkpoints
                ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        try:
            version = int(row[8])
            if version != CHECKPOINT_SCHEMA_VERSION:
                raise DurableStoreError("checkpoint_schema_incompatible")
            state = json.loads(row[6])
            checkpoint = StoredCheckpoint(
                session_id=row[0],
                created_at=_parse_utc(row[1]),
                last_event_sequence=int(row[2]),
                software_version=row[3],
                configuration_fingerprint=row[4],
                replay_dataset_fingerprint=row[5],
                state=MappingProxyType(state),
                checkpoint_schema_version=version,
            )
            _validate_checkpoint(checkpoint, self.session_id)
            document = {
                "checkpoint_schema_version": version,
                "session_id": row[0],
                "created_at": row[1],
                "last_event_sequence": int(row[2]),
                "software_version": row[3],
                "configuration_fingerprint": row[4],
                "replay_dataset_fingerprint": row[5],
                "state": state,
            }
            if _checksum(document) != row[7]:
                raise DurableStoreError("checkpoint_corrupted")
            return checkpoint
        except DurableStoreError:
            raise
        except Exception as exc:
            raise DurableStoreError("checkpoint_corrupted") from exc

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, component TEXT NOT NULL,
                    correlation_json TEXT NOT NULL, payload_json TEXT NOT NULL,
                    checksum TEXT NOT NULL, event_schema_version INTEGER NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, last_event_sequence INTEGER NOT NULL,
                    software_version TEXT NOT NULL,
                    configuration_fingerprint TEXT NOT NULL,
                    replay_dataset_fingerprint TEXT NOT NULL,
                    state_json TEXT NOT NULL, checksum TEXT NOT NULL,
                    checkpoint_schema_version INTEGER NOT NULL
                );
                """
            )
            existing = dict(self._connection.execute("SELECT key, value FROM metadata"))
            if not existing:
                self._set_metadata("store_format_version", str(STORE_FORMAT_VERSION))
                self._set_metadata("event_schema_version", str(EVENT_SCHEMA_VERSION))
                self._set_metadata("session_id", self.session_id)
                self._set_metadata("last_sequence", "0")
            elif existing.get("session_id") != self.session_id:
                raise DurableStoreError("session_mismatch")
            elif existing.get("store_format_version") != str(STORE_FORMAT_VERSION):
                raise DurableStoreError("store_version_incompatible")
            elif existing.get("event_schema_version") != str(EVENT_SCHEMA_VERSION):
                raise DurableStoreError("event_schema_incompatible")

    def _decode_event(self, row: tuple[object, ...], expected: int) -> AuditEvent:
        try:
            session_id, sequence, event_id = str(row[0]), int(row[1]), str(row[2])
            if int(row[9]) != EVENT_SCHEMA_VERSION:
                raise DurableStoreError("event_schema_incompatible")
            if session_id != self.session_id or sequence != expected:
                raise DurableStoreError("ledger_corrupted")
            if event_id != f"{self.session_id}:{sequence:020d}":
                raise DurableStoreError("ledger_corrupted")
            correlation_data = json.loads(str(row[6]))
            payload = json.loads(str(row[7]))
            document = {
                "session_id": session_id,
                "sequence": sequence,
                "event_id": event_id,
                "event_type": str(row[3]),
                "occurred_at": str(row[4]),
                "component": str(row[5]),
                "correlation": correlation_data,
                "payload": payload,
                "event_schema_version": int(row[9]),
            }
            if _checksum(document) != row[8]:
                raise DurableStoreError("ledger_corrupted")
            return AuditEvent._create(
                event_id=event_id,
                session_id=session_id,
                sequence=sequence,
                event_type=AuditEventType(str(row[3])),
                occurred_at=_parse_utc(str(row[4])),
                component=AuditComponent(str(row[5])),
                correlation=EventCorrelation(**correlation_data),
                payload=_freeze_json(payload),
            )
        except DurableStoreError:
            raise
        except Exception as exc:
            raise DurableStoreError("ledger_corrupted") from exc

    def _metadata_int(self, key: str) -> int:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise DurableStoreError("store_metadata_missing")
        return int(row[0])

    def _set_metadata(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value)
        )


def _event_document(event: AuditEvent) -> dict[str, object]:
    return {
        "session_id": event.session_id,
        "sequence": event.sequence,
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "occurred_at": _utc_text(event.occurred_at),
        "component": event.component.value,
        "correlation": {
            "signal_id": event.correlation.signal_id,
            "client_order_id": event.correlation.client_order_id,
            "broker_order_id": event.correlation.broker_order_id,
            "position_id": event.correlation.position_id,
            "close_order_id": event.correlation.close_order_id,
        },
        "payload": _plain_json(event.payload),
        "event_schema_version": EVENT_SCHEMA_VERSION,
    }


def _validate_checkpoint(checkpoint: StoredCheckpoint, session_id: str) -> None:
    if not isinstance(checkpoint, StoredCheckpoint):
        raise ValueError("checkpoint must be a StoredCheckpoint")
    if checkpoint.session_id != session_id:
        raise DurableStoreError("session_mismatch")
    if checkpoint.checkpoint_schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise DurableStoreError("checkpoint_schema_incompatible")
    if checkpoint.last_event_sequence < 0:
        raise ValueError("last_event_sequence must be non-negative")
    _utc_text(checkpoint.created_at)
    for value in (
        checkpoint.software_version,
        checkpoint.configuration_fingerprint,
        checkpoint.replay_dataset_fingerprint,
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("checkpoint identities must be non-empty strings")
    _canonical_json(checkpoint.state)


def _canonical_json(value: object) -> str:
    return json.dumps(_plain_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON mappings require string keys")
        return {key: _plain_json(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _checksum(document: object) -> str:
    return hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
