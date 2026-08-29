"""Session-local immutable execution audit ledger (Phase 8)."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from threading import Lock
from types import MappingProxyType
from typing import Protocol

from .signal_engine import SignalEvent


class AuditEventType(StrEnum):
    SESSION_STARTED = "session_started"
    SESSION_STOPPED = "session_stopped"
    MARKET_EVENT = "market_event"
    ACCOUNT_OBSERVED = "account_observed"
    SIGNAL_EMITTED = "signal_emitted"
    SIGNAL_DECLINED = "signal_declined"
    EXECUTION_INTENT_CREATED = "execution_intent_created"
    EXECUTION_POLICY_FAILED = "execution_policy_failed"
    EXECUTION_FAILED = "execution_failed"
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
    ORDER_SUBMISSION_ATTEMPTED = "order_submission_attempted"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_SUBMISSION_INDETERMINATE = "order_submission_indeterminate"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_STATUS_FAILED = "order_status_failed"
    POSITION_OPENED = "position_opened"
    POSITION_MARKED = "position_marked"
    POSITION_CLOSED = "position_closed"
    RESERVATION_RELEASED = "reservation_released"
    RECONCILIATION_FAILED = "reconciliation_failed"
    RECONCILIATION_STARTED = "reconciliation_started"
    RECONCILIATION_RESOLVED = "reconciliation_resolved"
    RECONCILIATION_UNRESOLVED = "reconciliation_unresolved"
    DATA_PROVIDER_SELECTED = "data_provider_selected"
    DATA_PROVIDER_FAILED = "data_provider_failed"
    DATA_PROVIDER_FALLBACK = "data_provider_fallback"
    DATA_STALE = "data_stale"
    DATASET_BOUND = "dataset_bound"
    BROKER_CAPABILITIES_BOUND = "broker_capabilities_bound"
    BROKER_CAPABILITY_REJECTED = "broker_capability_rejected"
    RUNTIME_STATE_CHANGED = "runtime_state_changed"
    OPERATOR_CONTROL_ACTION = "operator_control_action"
    RUNTIME_FAILURE = "runtime_failure"


class AuditComponent(StrEnum):
    PAPER_SESSION = "paper_session"
    REPLAY = "replay"
    SIGNAL_ENGINE = "signal_engine"
    EXECUTION_POLICY = "execution_policy"
    RISK_ENGINE = "risk_engine"
    ORDER_MANAGER = "order_manager"
    PAPER_BROKER = "paper_broker"
    RECONCILIATION_ENGINE = "reconciliation_engine"
    MARKET_DATA_PROVIDER = "market_data_provider"
    BROKER_ADAPTER = "broker_adapter"
    CONTROL_SERVICE = "control_service"


class AuditLedgerError(RuntimeError):
    """Raised when a validated event cannot be appended to the ledger."""


class DurableEventBackend(Protocol):
    session_id: str

    def append(self, event: AuditEvent) -> None: ...

    def load_events(self) -> tuple[AuditEvent, ...]: ...


@dataclass(frozen=True)
class EventCorrelation:
    signal_id: str | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    position_id: str | None = None
    close_order_id: str | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{item.name} must be a non-empty string when supplied")
            if isinstance(value, str):
                stripped = value.strip()
                if not stripped:
                    raise ValueError(
                        f"{item.name} must be a non-empty string when supplied"
                    )
                object.__setattr__(self, item.name, stripped)


@dataclass(frozen=True, init=False)
class AuditEvent:
    event_id: str
    session_id: str
    sequence: int
    event_type: AuditEventType
    occurred_at: datetime
    component: AuditComponent
    correlation: EventCorrelation
    payload: Mapping[str, object]

    def __new__(cls) -> AuditEvent:
        raise TypeError("AuditEvent instances are created only by EventLedger")

    @classmethod
    def _create(
        cls,
        *,
        event_id: str,
        session_id: str,
        sequence: int,
        event_type: AuditEventType,
        occurred_at: datetime,
        component: AuditComponent,
        correlation: EventCorrelation,
        payload: Mapping[str, object],
    ) -> AuditEvent:
        event = object.__new__(cls)
        object.__setattr__(event, "event_id", event_id)
        object.__setattr__(event, "session_id", session_id)
        object.__setattr__(event, "sequence", sequence)
        object.__setattr__(event, "event_type", event_type)
        object.__setattr__(event, "occurred_at", occurred_at)
        object.__setattr__(event, "component", component)
        object.__setattr__(event, "correlation", correlation)
        object.__setattr__(event, "payload", payload)
        return event


@dataclass
class EventLedger:
    """Thread-safe append-only audit history for one in-memory session.

    Restarting the process loses this ledger. Persistence and restoration belong to
    Phase 9 and are intentionally absent here.
    """

    session_id: str
    time_provider: Callable[[], datetime] = lambda: datetime.now(UTC)
    durable_store: DurableEventBackend | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if ":" in self.session_id:
            raise ValueError("session_id cannot contain ':'")
        if not callable(self.time_provider):
            raise ValueError("time_provider must be callable")
        self.session_id = self.session_id.strip()
        if self.durable_store is not None:
            if self.durable_store.session_id != self.session_id:
                raise ValueError("durable store session_id must match the ledger")
            loaded = self.durable_store.load_events()
            self._events = list(loaded)
            self._next_sequence = len(loaded) + 1
        else:
            self._events = []
            self._next_sequence = 1
        self._lock = Lock()

    def append(
        self,
        event_type: AuditEventType,
        *,
        occurred_at: datetime,
        component: AuditComponent,
        correlation: EventCorrelation | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> AuditEvent:
        if not isinstance(event_type, AuditEventType):
            raise ValueError("event_type must be an AuditEventType")
        if not isinstance(component, AuditComponent):
            raise ValueError("component must be an AuditComponent")
        occurred_utc = _aware_utc(occurred_at)
        if occurred_utc is None:
            raise ValueError("occurred_at must be timezone-aware")
        if correlation is None:
            correlation = EventCorrelation()
        if not isinstance(correlation, EventCorrelation):
            raise ValueError("correlation must be an EventCorrelation")
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a string-keyed mapping")
        snapshot = _snapshot_mapping(payload)

        with self._lock:
            sequence = self._next_sequence
            event_id = f"{self.session_id}:{sequence:020d}"
            event = AuditEvent._create(
                event_id=event_id,
                session_id=self.session_id,
                sequence=sequence,
                event_type=event_type,
                occurred_at=occurred_utc,
                component=component,
                correlation=correlation,
                payload=snapshot,
            )
            try:
                self._store_event(event)
            except Exception as exc:
                raise AuditLedgerError("validated audit event could not be appended") from exc
            self._next_sequence += 1
            return event

    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def last_event(self) -> AuditEvent | None:
        with self._lock:
            return self._events[-1] if self._events else None

    def now(self) -> datetime:
        value = self.time_provider()
        normalized = _aware_utc(value)
        if normalized is None:
            raise ValueError("time_provider must return a timezone-aware datetime")
        return normalized

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def _store_event(self, event: AuditEvent) -> None:
        if self.durable_store is not None:
            self.durable_store.append(event)
        self._events.append(event)


def deterministic_signal_id(signal: SignalEvent) -> str:
    """Reproduce the existing RiskEngine deterministic order/signal identity."""
    if not isinstance(signal, SignalEvent):
        raise ValueError("signal must be a SignalEvent")
    signal_utc = _aware_utc(signal.signal_time)
    if signal_utc is None:
        raise ValueError("signal_time must be timezone-aware")
    symbol = signal.symbol.strip().upper() if isinstance(signal.symbol, str) else ""
    timeframe = signal.timeframe.upper() if isinstance(signal.timeframe, str) else ""
    setup = signal.setup_name if isinstance(signal.setup_name, str) else ""
    if not symbol or not timeframe or not setup:
        raise ValueError("signal identity fields must be non-empty strings")
    if signal.side not in (1, -1) or isinstance(signal.side, bool):
        raise ValueError("signal side must be exactly +1 or -1")
    timestamp = signal_utc.strftime("%Y%m%dT%H%M%S%fZ")
    side = "LONG" if signal.side == 1 else "SHORT"
    return f"{setup}-{symbol}-{timeframe}-{timestamp}-{side}"


_SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "private_key",
}


def _snapshot_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    snapshot: dict[str, object] = {}
    if any(not isinstance(key, str) for key in value):
        raise ValueError("payload mapping keys must be strings")
    for key in sorted(value):
        normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in _SENSITIVE_KEYS:
            raise ValueError(f"sensitive payload key is not permitted: {key!r}")
        snapshot[key] = _snapshot_value(value[key])
    return MappingProxyType(snapshot)


def _snapshot_value(value: object) -> object:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload numeric values must be finite")
        return value
    if isinstance(value, datetime):
        normalized = _aware_utc(value)
        if normalized is None:
            raise ValueError("payload datetimes must be timezone-aware")
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _snapshot_value(value.value)
    if isinstance(value, Mapping):
        return _snapshot_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_snapshot_value(item) for item in value)
    if is_dataclass(value):
        raise ValueError("dataclass/DTO payloads must be flattened explicitly")
    if callable(value):
        raise ValueError("callables are not valid audit payload values")
    raise ValueError(f"unsupported audit payload value type: {type(value).__name__}")


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except Exception:
        return None
