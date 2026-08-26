"""Safe-point checkpointing and fail-closed recovery for paper replay sessions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .broker import OrderStatus
from .durable_event_store import DurableStoreError, SQLiteEventStore, StoredCheckpoint
from .event_ledger import AuditEventType
from .paper_session import PaperTradingSession


class RecoveryState(StrEnum):
    RECOVERED = "recovered"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED = "failed"


@dataclass(frozen=True)
class RecoveryResult:
    state: RecoveryState
    reason: str
    message: str
    checkpoint_sequence: int | None = None

    @property
    def recovered(self) -> bool:
        return self.state is RecoveryState.RECOVERED


class UnsafeCheckpointError(RuntimeError):
    """The live session is not at a provably safe checkpoint boundary."""


def configuration_fingerprint(
    session: PaperTradingSession, *, execution_policy_id: str
) -> str:
    if not isinstance(execution_policy_id, str) or not execution_policy_id.strip():
        raise ValueError("execution_policy_id must be a non-empty string")
    symbols = sorted(
        key.strip().upper() for key in session.replay.bars_by_symbol if key.strip()
    )
    document = {
        "format": 1,
        "risk": session.risk_engine.configuration_snapshot(symbols),
        "broker": session.broker.configuration_snapshot(symbols),
        "symbols": symbols,
        "timeframe": session.replay.timeframe,
        "execution_policy_id": execution_policy_id.strip(),
        "replay_dataset_fingerprint": session.replay.dataset_fingerprint,
        "market_data_provider": session.replay.provider_compatibility_snapshot(),
    }
    return hashlib.sha256(_canonical_json(document).encode()).hexdigest()


def create_checkpoint(
    session: PaperTradingSession,
    store: SQLiteEventStore,
    *,
    software_version: str,
    execution_policy_id: str,
    created_at: datetime | None = None,
) -> StoredCheckpoint:
    """Persist one complete safe-point snapshot or reject it without best effort."""
    if not isinstance(software_version, str) or not software_version.strip():
        raise ValueError("software_version must be a non-empty string")
    if session.event_ledger.session_id != store.session_id:
        raise UnsafeCheckpointError("session_mismatch")
    if session.event_ledger.durable_store is not store:
        raise UnsafeCheckpointError("durable_store_mismatch")
    _validate_safe_point(session)
    state = {
        "risk": session.risk_engine.snapshot_state(),
        "orders": session.order_manager.snapshot_state(),
        "broker": session.broker.snapshot_state(),
        "replay": session.replay.snapshot_state(),
        "session": session.snapshot_state(),
    }
    checkpoint = StoredCheckpoint(
        session_id=store.session_id,
        created_at=(created_at or datetime.now(UTC)).astimezone(UTC),
        last_event_sequence=store.last_sequence(),
        software_version=software_version.strip(),
        configuration_fingerprint=configuration_fingerprint(
            session, execution_policy_id=execution_policy_id
        ),
        replay_dataset_fingerprint=session.replay.dataset_fingerprint,
        state=state,
    )
    store.store_checkpoint(checkpoint)
    return checkpoint


def create_reconciliation_checkpoint(
    session: PaperTradingSession,
    store: SQLiteEventStore,
    *,
    software_version: str,
    execution_policy_id: str,
    created_at: datetime | None = None,
) -> StoredCheckpoint:
    """Commit a reconciled terminal session while retaining its execution gate.

    This is intentionally narrower than ``create_checkpoint``: the reconciliation
    gate and its latched kill switch remain in the damaged session, while every
    other safe-point invariant must pass.
    """
    if not isinstance(software_version, str) or not software_version.strip():
        raise ValueError("software_version must be a non-empty string")
    if session.event_ledger.session_id != store.session_id:
        raise UnsafeCheckpointError("session_mismatch")
    if session.event_ledger.durable_store is not store:
        raise UnsafeCheckpointError("durable_store_mismatch")
    if not session.recovery_required or not session.risk_engine.kill_switch_active:
        raise UnsafeCheckpointError("reconciliation_gate_not_latched")
    _validate_safe_point(session, allow_reconciliation_gate=True)
    checkpoint = StoredCheckpoint(
        session_id=store.session_id,
        created_at=(created_at or datetime.now(UTC)).astimezone(UTC),
        last_event_sequence=store.last_sequence(),
        software_version=software_version.strip(),
        configuration_fingerprint=configuration_fingerprint(
            session, execution_policy_id=execution_policy_id
        ),
        replay_dataset_fingerprint=session.replay.dataset_fingerprint,
        state=_session_state_snapshot(session),
    )
    store.store_checkpoint(checkpoint)
    return checkpoint


def recover(
    session: PaperTradingSession,
    store: SQLiteEventStore,
    *,
    software_version: str,
    execution_policy_id: str,
) -> RecoveryResult:
    """Restore a PaperBroker session without executing any historical side effect."""
    try:
        if session.event_ledger.session_id != store.session_id:
            return _fail_session(session, "session_mismatch")
        events = store.load_events()
        checkpoint = store.load_latest_checkpoint()
        if checkpoint is None:
            return _fail_session(session, "state_missing")
        if checkpoint.software_version != software_version:
            return _fail_session(session, "software_version_mismatch")
        if checkpoint.replay_dataset_fingerprint != session.replay.dataset_fingerprint:
            return _fail_session(session, "replay_dataset_mismatch")
        expected_config = configuration_fingerprint(
            session, execution_policy_id=execution_policy_id
        )
        if checkpoint.configuration_fingerprint != expected_config:
            return _fail_session(session, "configuration_mismatch")
        if checkpoint.last_event_sequence > len(events):
            return _fail_session(session, "checkpoint_corrupted")
        state = checkpoint.state
        if not isinstance(state, dict):
            # MappingProxyType loaded by the store remains mapping-like, while JSON
            # nested state remains ordinary dictionaries.
            state = dict(state)
        required = {"risk", "orders", "broker", "replay", "session"}
        if set(state) != required:
            return _fail_session(session, "unsupported_recovery_state")
        session.risk_engine.restore_state(state["risk"])
        session.order_manager.restore_state(state["orders"])
        session.broker.restore_state(state["broker"])
        session.replay.restore_state(state["replay"])
        session.restore_state(state["session"])
        tail = events[checkpoint.last_event_sequence :]
        ambiguous = [
            event
            for event in tail
            if event.event_type is not AuditEventType.SESSION_STOPPED
        ]
        if ambiguous:
            session.require_reconciliation()
            return RecoveryResult(
                RecoveryState.RECONCILIATION_REQUIRED,
                "reconciliation_required",
                "durable events exist after the latest safe checkpoint",
                checkpoint.last_event_sequence,
            )
        if session.recovery_required:
            return RecoveryResult(
                RecoveryState.RECONCILIATION_REQUIRED,
                "reconciliation_required",
                "checkpoint belongs to a terminated reconciled session",
                checkpoint.last_event_sequence,
            )
        return RecoveryResult(
            RecoveryState.RECOVERED,
            "recovered",
            "safe checkpoint restored; explicit session start is required",
            checkpoint.last_event_sequence,
        )
    except DurableStoreError as exc:
        reason = str(exc)
        supported = {
            "store_version_incompatible",
            "event_schema_incompatible",
            "checkpoint_schema_incompatible",
            "ledger_corrupted",
            "checkpoint_corrupted",
            "session_mismatch",
        }
        return _fail_session(
            session, reason if reason in supported else "ledger_corrupted"
        )
    except (KeyError, TypeError, ValueError):
        return _fail_session(session, "unsupported_recovery_state")


def validate_reconciliation_safe_point(session: PaperTradingSession) -> None:
    """Validate all safe-point invariants except the intentional recovery gate."""
    if not session.recovery_required or not session.risk_engine.kill_switch_active:
        raise UnsafeCheckpointError("reconciliation_gate_not_latched")
    _validate_safe_point(session, allow_reconciliation_gate=True)


def _validate_safe_point(
    session: PaperTradingSession, *, allow_reconciliation_gate: bool = False
) -> None:
    if session.order_manager.audit_failed or session.snapshot_state()["audit_failed"]:
        raise UnsafeCheckpointError("audit_integrity_lost")
    if session.recovery_required and not allow_reconciliation_gate:
        raise UnsafeCheckpointError("reconciliation_required")
    if session.broker.has_pending_close_events:
        raise UnsafeCheckpointError("undrained_close_events")
    order_state = session.order_manager.snapshot_state()
    records = order_state["records"]
    if not isinstance(records, list):
        raise UnsafeCheckpointError("invalid_order_state")
    for record in records:
        if record["broker_order_id"] is None:
            raise UnsafeCheckpointError("indeterminate_submission")
        if record["status"] == OrderStatus.FILLED.value and not record[
            "reservation_released"
        ]:
            raise UnsafeCheckpointError("filled_position_not_reflected")
    session_state = session.snapshot_state()
    if session_state["capability_failed"]:
        raise UnsafeCheckpointError("broker_capability_failed")
    if session_state["tracked_orders"]:
        raise UnsafeCheckpointError("unresolved_submission")
    risk_state = session.risk_engine.snapshot_state()
    reservations = risk_state["reservations"]
    if reservations:
        raise UnsafeCheckpointError("inconsistent_reservation_state")
    broker_positions = {
        item["position_id"] for item in session.broker.snapshot_state()["positions"]
    }
    session_positions = {
        item["position_id"] for item in session_state["position_correlations"]
    }
    if broker_positions != session_positions:
        raise UnsafeCheckpointError("position_correlation_mismatch")


def _session_state_snapshot(session: PaperTradingSession) -> dict[str, object]:
    return {
        "risk": session.risk_engine.snapshot_state(),
        "orders": session.order_manager.snapshot_state(),
        "broker": session.broker.snapshot_state(),
        "replay": session.replay.snapshot_state(),
        "session": session.snapshot_state(),
    }


def _failed(reason: str) -> RecoveryResult:
    return RecoveryResult(
        RecoveryState.FAILED,
        reason,
        "recovery failed closed; fresh start was not attempted",
    )


def _fail_session(session: PaperTradingSession, reason: str) -> RecoveryResult:
    session.require_reconciliation()
    return _failed(reason)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
