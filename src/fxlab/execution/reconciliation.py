"""Controlled, exact-identity reconciliation for durable PaperBroker sessions."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock

from fxlab.risk import KillSwitchReason

from .broker import OrderStatus
from .durable_event_store import DurableStoreError, SQLiteEventStore, StoredCheckpoint
from .event_ledger import (
    AuditComponent,
    AuditEvent,
    AuditEventType,
    AuditLedgerError,
    EventCorrelation,
)
from .paper_session import PaperTradingSession
from .recovery import (
    UnsafeCheckpointError,
    configuration_fingerprint,
    create_checkpoint,
    create_reconciliation_checkpoint,
    validate_reconciliation_safe_point,
)


class ReconciliationStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


@dataclass(frozen=True)
class ReconciliationResult:
    status: ReconciliationStatus
    reason: str
    message: str
    reconciliation_id: str
    base_checkpoint_sequence: int
    tail_start_sequence: int
    tail_end_sequence: int
    applied_actions: tuple[str, ...] = ()
    new_session_id: str | None = None


@dataclass(frozen=True)
class ReconciliationPlan:
    reconciliation_id: str
    base_checkpoint_sequence: int
    tail_start_sequence: int
    tail_end_sequence: int
    resolvable: bool
    reason: str
    message: str
    correlations: tuple[EventCorrelation, ...] = ()
    proposed_actions: tuple[str, ...] = ()
    evidence_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class _Evidence:
    checkpoint: StoredCheckpoint
    events: tuple[AuditEvent, ...]
    tail: tuple[AuditEvent, ...]
    operational_tail: tuple[AuditEvent, ...]
    reconciliation_id: str
    tail_start: int
    tail_end: int


_RECONCILIATION_EVENTS = {
    AuditEventType.RECONCILIATION_STARTED,
    AuditEventType.RECONCILIATION_RESOLVED,
    AuditEventType.RECONCILIATION_UNRESOLVED,
}


@dataclass
class ReconciliationEngine:
    """Inspect and resolve only exact, authoritative PaperBroker discrepancies."""

    session: PaperTradingSession
    store: SQLiteEventStore
    software_version: str
    execution_policy_id: str
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.software_version, str) or not self.software_version.strip():
            raise ValueError("software_version must be a non-empty string")
        if (
            not isinstance(self.execution_policy_id, str)
            or not self.execution_policy_id.strip()
        ):
            raise ValueError("execution_policy_id must be a non-empty string")
        if self.session.event_ledger.session_id != self.store.session_id:
            raise ValueError("session and store IDs must match")

    def inspect(self, *, authoritative_broker_state: bool = False) -> ReconciliationPlan:
        """Return an immutable plan without mutating any component."""
        with self._lock:
            evidence = self._load_evidence()
            return self._build_plan(evidence, authoritative_broker_state)

    def reconcile(
        self,
        *,
        new_session: PaperTradingSession | None = None,
        new_store: SQLiteEventStore | None = None,
        authoritative_broker_state: bool = False,
        occurred_at: datetime | None = None,
    ) -> ReconciliationResult:
        """Apply a supported plan, durably close the old session, and stage a new one."""
        with self._lock:
            try:
                evidence = self._load_evidence()
                committed = _committed_resolution(evidence.events, evidence.checkpoint)
                if committed is not None:
                    if evidence.operational_tail:
                        raise ValueError("events exist after terminal reconciliation")
                    return self._resume_committed_handoff(
                        evidence.checkpoint, committed, new_session, new_store
                    )
                now = _utc_now(occurred_at, self.session)
                self._append_started(evidence, now)
                plan = self._build_plan(evidence, authoritative_broker_state)
                if not plan.resolvable:
                    self._append_unresolved(plan, now)
                    return _plan_result(plan, ReconciliationStatus.UNRESOLVED)
                if new_session is None or new_store is None:
                    return self._failed(plan, "unsupported_state", "a new session is required")
                self._validate_new_target(new_session, new_store)
                # The target cannot become runnable until its baseline checkpoint commits.
                new_session.require_reconciliation()
                snapshots = _component_snapshots(self.session)
                released: tuple[str, ...] = ()
                try:
                    released = self._apply_plan(plan, evidence)
                    validate_reconciliation_safe_point(self.session)
                    for client_id in released:
                        self.session.event_ledger.append(
                            AuditEventType.RESERVATION_RELEASED,
                            occurred_at=now,
                            component=AuditComponent.RECONCILIATION_ENGINE,
                            correlation=EventCorrelation(
                                signal_id=client_id, client_order_id=client_id
                            ),
                            payload={"reason": "reconciliation"},
                        )
                    self.session.event_ledger.append(
                        AuditEventType.RECONCILIATION_RESOLVED,
                        occurred_at=now,
                        component=AuditComponent.RECONCILIATION_ENGINE,
                        payload={
                            "reconciliation_id": plan.reconciliation_id,
                            "reason": plan.reason,
                            "applied_actions": plan.proposed_actions,
                            "new_session_id": new_session.event_ledger.session_id,
                        },
                    )
                    self.session.stop()
                    final_checkpoint = create_reconciliation_checkpoint(
                        self.session,
                        self.store,
                        software_version=self.software_version,
                        execution_policy_id=self.execution_policy_id,
                        created_at=now,
                    )
                except Exception:
                    _restore_components(self.session, snapshots)
                    raise
                self._install_new_baseline(
                    final_checkpoint, new_session, new_store, created_at=now
                )
                return ReconciliationResult(
                    ReconciliationStatus.RESOLVED,
                    plan.reason,
                    "reconciliation committed; caller may explicitly start the new session",
                    plan.reconciliation_id,
                    plan.base_checkpoint_sequence,
                    plan.tail_start_sequence,
                    plan.tail_end_sequence,
                    plan.proposed_actions,
                    new_session.event_ledger.session_id,
                )
            except AuditLedgerError:
                return self._failure_from_store("audit_failed")
            except (DurableStoreError, UnsafeCheckpointError):
                return self._failure_from_store("checkpoint_failed")
            except (KeyError, TypeError, ValueError):
                return self._failure_from_store("evidence_corrupted")
            except Exception:
                return self._failure_from_store("unsupported_state")

    def _load_evidence(self) -> _Evidence:
        events = self.store.load_events()
        checkpoint = self.store.load_latest_checkpoint()
        if checkpoint is None:
            raise ValueError("checkpoint is required")
        if checkpoint.last_event_sequence > len(events):
            raise ValueError("checkpoint exceeds the ledger")
        if checkpoint.software_version != self.software_version.strip():
            raise ValueError("software version mismatch")
        if checkpoint.replay_dataset_fingerprint != self.session.replay.dataset_fingerprint:
            raise ValueError("replay dataset mismatch")
        if checkpoint.configuration_fingerprint != configuration_fingerprint(
            self.session, execution_policy_id=self.execution_policy_id
        ):
            raise ValueError("configuration mismatch")
        tail = events[checkpoint.last_event_sequence :]
        operational = tuple(
            event
            for event in tail
            if event.event_type not in _RECONCILIATION_EVENTS
            and event.event_type is not AuditEventType.SESSION_STOPPED
        )
        start = operational[0].sequence if operational else checkpoint.last_event_sequence + 1
        end = operational[-1].sequence if operational else checkpoint.last_event_sequence
        identity = _reconciliation_id(
            self.store.session_id, checkpoint.last_event_sequence, start, end
        )
        return _Evidence(checkpoint, events, tail, operational, identity, start, end)

    def _build_plan(
        self, evidence: _Evidence, authoritative_broker_state: bool
    ) -> ReconciliationPlan:
        counts = tuple(
            sorted(Counter(event.event_type.value for event in evidence.operational_tail).items())
        )
        base = dict(
            reconciliation_id=evidence.reconciliation_id,
            base_checkpoint_sequence=evidence.checkpoint.last_event_sequence,
            tail_start_sequence=evidence.tail_start,
            tail_end_sequence=evidence.tail_end,
            evidence_counts=counts,
        )
        if not evidence.operational_tail:
            return ReconciliationPlan(
                **base,
                resolvable=False,
                reason="unsupported_state",
                message="no unresolved operational evidence exists",
            )
        if any(
            event.event_type is AuditEventType.POSITION_CLOSED
            for event in evidence.operational_tail
        ):
            return ReconciliationPlan(
                **base,
                resolvable=False,
                reason="close_accounting_uncertain",
                message="trade-close risk accounting cannot be proven",
            )
        by_client = _events_by_client(evidence.operational_tail)
        if not by_client:
            return ReconciliationPlan(
                **base,
                resolvable=False,
                reason="audit_evidence_incomplete",
                message="tail has no exact client-order correlation",
            )
        actions: list[str] = []
        correlations: list[EventCorrelation] = []
        for client_id, events in sorted(by_client.items()):
            outcome = self._plan_client(client_id, events, authoritative_broker_state)
            if isinstance(outcome, tuple) and len(outcome) == 2 and isinstance(outcome[0], str):
                reason, message = outcome
                return ReconciliationPlan(
                    **base,
                    resolvable=False,
                    reason=reason,
                    message=message,
                    correlations=tuple(correlations),
                )
            client_actions, correlation = outcome
            actions.extend(client_actions)
            correlations.append(correlation)
        return ReconciliationPlan(
            **base,
            resolvable=True,
            reason="state_reconciled",
            message="all proposed corrections are supported by exact evidence",
            correlations=tuple(correlations),
            proposed_actions=tuple(actions),
        )

    def _plan_client(
        self,
        client_id: str,
        events: tuple[AuditEvent, ...],
        authoritative: bool,
    ) -> tuple[list[str], EventCorrelation] | tuple[str, str]:
        types = {event.event_type for event in events}
        if AuditEventType.ORDER_SUBMISSION_INDETERMINATE in types:
            return "submission_outcome_unknown", "submission is explicitly indeterminate"
        attempted = _last(events, AuditEventType.ORDER_SUBMISSION_ATTEMPTED)
        submitted = _last(events, AuditEventType.ORDER_SUBMITTED)
        approved = _last(events, AuditEventType.RISK_APPROVED)
        if attempted is not None and submitted is None:
            return "submission_outcome_unknown", "submission attempt has no acknowledgement"
        risk_state = self.session.risk_engine.snapshot_state()
        approved_ids = set(risk_state["approved_order_ids"])
        reservations = {
            item["order_id"] for item in risk_state["reservations"]  # type: ignore[index]
        }
        if approved is not None and attempted is None:
            if not authoritative or client_id not in reservations or client_id not in approved_ids:
                return (
                    "audit_evidence_incomplete",
                    "approval state is not independently available in the live process",
                )
            return [f"release_reservation:{client_id}"], approved.correlation
        if submitted is None or attempted is None:
            return "audit_evidence_incomplete", "order transition trace is incomplete"
        if not authoritative:
            return (
                "audit_evidence_incomplete",
                "checkpoint-restored broker state cannot prove post-checkpoint actions",
            )
        broker_id = submitted.correlation.broker_order_id
        if broker_id is None:
            return "audit_evidence_incomplete", "acknowledgement lacks broker identity"
        correlation = self.session.broker.get_correlation(client_id)
        if (
            correlation is None
            or correlation.broker_order_id != broker_id
            or submitted.correlation.client_order_id != client_id
        ):
            return "order_not_found", "authoritative broker correlation is absent"
        try:
            raw_status = self.session.broker.get_order_status(broker_id)
            status = OrderStatus(raw_status["status"])
        except (KeyError, TypeError, ValueError):
            return "order_not_found", "authoritative broker status is unavailable"
        if (
            raw_status.get("client_order_id") != client_id
            or raw_status.get("broker_order_id") != broker_id
            or raw_status.get("position_id") != correlation.position_id
        ):
            return "state_mismatch", "broker status correlation conflicts"
        if client_id not in approved_ids:
            return "reservation_mismatch", "approved-ID history is missing"
        account = self.session.broker.get_account_info()
        exact_positions = {
            position.position_id: position for position in account.open_positions
        }
        position_present = correlation.position_id in exact_positions
        actions: list[str] = []
        manager_record = self.session.order_manager.get_order(client_id)
        if manager_record is not None and (
            manager_record.broker_order_id not in (None, broker_id)
            or manager_record.client_order_id != client_id
        ):
            return "state_mismatch", "local order record conflicts with broker identity"
        if manager_record is None:
            actions.append(f"repair_order_record:{client_id}")
        elif manager_record.status is not status or manager_record.broker_order_id is None:
            actions.append(f"repair_order_status:{client_id}")
        terminal_event = _terminal_status_event(events)
        if terminal_event is not None and terminal_event is not status:
            return "state_mismatch", "audit terminal status conflicts with broker status"
        if status is OrderStatus.PENDING:
            return "unsupported_state", "PaperBroker pending state is unsupported"
        if status is OrderStatus.FILLED:
            if not position_present:
                return "position_not_found", "filled order has no exact broker position"
            actions.append(f"repair_session_correlation:{client_id}")
        elif position_present:
            return "account_mismatch", "terminal non-fill unexpectedly has a position"
        if client_id in reservations:
            actions.append(f"release_reservation:{client_id}")
        actions.append(f"finalize_order_record:{client_id}")
        return actions, EventCorrelation(
            signal_id=client_id,
            client_order_id=client_id,
            broker_order_id=broker_id,
            position_id=correlation.position_id if status is OrderStatus.FILLED else None,
        )

    def _apply_plan(
        self, plan: ReconciliationPlan, evidence: _Evidence
    ) -> tuple[str, ...]:
        actions = set(plan.proposed_actions)
        by_client = _events_by_client(evidence.operational_tail)
        order_state = self.session.order_manager.snapshot_state()
        session_state = self.session.snapshot_state()
        records = {item["client_order_id"]: dict(item) for item in order_state["records"]}
        correlations = {
            item["position_id"]: dict(item)
            for item in session_state["position_correlations"]
        }
        tracked = set(session_state["tracked_orders"])
        released: list[str] = []
        for client_id, events in sorted(by_client.items()):
            submitted = _last(events, AuditEventType.ORDER_SUBMITTED)
            attempted = _last(events, AuditEventType.ORDER_SUBMISSION_ATTEMPTED)
            broker_correlation = self.session.broker.get_correlation(client_id)
            if submitted is not None and attempted is not None and broker_correlation is not None:
                broker_id = broker_correlation.broker_order_id
                status = OrderStatus(
                    self.session.broker.get_order_status(broker_id)["status"]
                )
                if f"repair_order_record:{client_id}" in actions:
                    records[client_id] = _record_from_attempt(
                        client_id, broker_id, status, attempted
                    )
                if (
                    f"repair_order_status:{client_id}" in actions
                    or f"finalize_order_record:{client_id}" in actions
                ):
                    record = records[client_id]
                    record["broker_order_id"] = broker_id
                    record["status"] = status.value
                    record["reservation_released"] = True
                if f"repair_session_correlation:{client_id}" in actions:
                    position_id = broker_correlation.position_id
                    correlations[position_id] = {
                        "position_id": position_id,
                        "signal_id": client_id,
                        "client_order_id": client_id,
                        "broker_order_id": broker_id,
                        "close_order_id": None,
                    }
                tracked.discard(client_id)
            if f"release_reservation:{client_id}" in actions:
                if not self.session.risk_engine.release_approval(client_id):
                    raise ValueError("planned reservation is no longer active")
                released.append(client_id)
        repaired_order_state = {
            "audit_failed": order_state["audit_failed"],
            "records": [records[key] for key in sorted(records)],
        }
        repaired_session_state = dict(session_state)
        repaired_session_state["tracked_orders"] = sorted(tracked)
        repaired_session_state["position_correlations"] = [
            correlations[key] for key in sorted(correlations)
        ]
        self.session.order_manager.restore_state(repaired_order_state)
        self.session.restore_state(repaired_session_state)
        self.session.require_reconciliation()
        return tuple(released)

    def _append_started(self, evidence: _Evidence, now: datetime) -> None:
        self.session.event_ledger.append(
            AuditEventType.RECONCILIATION_STARTED,
            occurred_at=now,
            component=AuditComponent.RECONCILIATION_ENGINE,
            payload={
                "reconciliation_id": evidence.reconciliation_id,
                "base_checkpoint_sequence": evidence.checkpoint.last_event_sequence,
                "tail_start_sequence": evidence.tail_start,
                "tail_end_sequence": evidence.tail_end,
            },
        )

    def _append_unresolved(self, plan: ReconciliationPlan, now: datetime) -> None:
        self.session.event_ledger.append(
            AuditEventType.RECONCILIATION_UNRESOLVED,
            occurred_at=now,
            component=AuditComponent.RECONCILIATION_ENGINE,
            payload={
                "reconciliation_id": plan.reconciliation_id,
                "reason": plan.reason,
                "evidence_counts": dict(plan.evidence_counts),
            },
        )

    def _validate_new_target(
        self, new_session: PaperTradingSession, new_store: SQLiteEventStore
    ) -> None:
        new_id = new_session.event_ledger.session_id
        if new_id == self.store.session_id or new_store.session_id != new_id:
            raise ValueError("new session must use a distinct matching store ID")
        if new_session.event_ledger.durable_store is not new_store:
            raise ValueError("new session ledger must use the supplied durable store")
        if new_store.last_sequence() or new_store.load_latest_checkpoint() is not None:
            raise ValueError("new session store must be empty")

    def _install_new_baseline(
        self,
        old_checkpoint: StoredCheckpoint,
        new_session: PaperTradingSession,
        new_store: SQLiteEventStore,
        *,
        created_at: datetime,
    ) -> None:
        original = _component_snapshots(new_session)
        state = dict(old_checkpoint.state)
        risk_state = dict(state["risk"])
        if risk_state.get("kill_switch_reason") == (
            KillSwitchReason.POSITION_RECONCILIATION_FAILED.value
        ):
            risk_state["kill_switch_active"] = False
            risk_state["kill_switch_reason"] = None
        order_state = dict(state["orders"])
        order_state["audit_failed"] = False
        session_state = dict(state["session"])
        session_state["audit_failed"] = False
        session_state["recovery_required"] = False
        session_state["reported_reconciliation_failures"] = []
        try:
            new_session.risk_engine.restore_state(risk_state)
            new_session.order_manager.restore_state(order_state)
            new_session.broker.restore_state(state["broker"])
            new_session.replay.restore_state(state["replay"])
            new_session.restore_state(session_state)
            if configuration_fingerprint(
                new_session, execution_policy_id=self.execution_policy_id
            ) != old_checkpoint.configuration_fingerprint:
                raise ValueError(
                    "new session configuration differs from reconciled state"
                )
            create_checkpoint(
                new_session,
                new_store,
                software_version=self.software_version,
                execution_policy_id=self.execution_policy_id,
                created_at=created_at,
            )
        except Exception:
            _restore_components(new_session, original)
            raise

    def _resume_committed_handoff(
        self,
        checkpoint: StoredCheckpoint,
        resolved: AuditEvent,
        new_session: PaperTradingSession | None,
        new_store: SQLiteEventStore | None,
    ) -> ReconciliationResult:
        new_id = resolved.payload.get("new_session_id")
        reconciliation_id = resolved.payload.get("reconciliation_id")
        if not isinstance(new_id, str) or not isinstance(reconciliation_id, str):
            raise ValueError("committed resolution payload is invalid")
        if new_session is not None and new_store is not None:
            if new_session.event_ledger.session_id != new_id:
                raise ValueError("new session ID differs from committed resolution")
            new_checkpoint = new_store.load_latest_checkpoint()
            if new_checkpoint is None:
                self._validate_new_target(new_session, new_store)
                new_session.require_reconciliation()
                self._install_new_baseline(
                    checkpoint,
                    new_session,
                    new_store,
                    created_at=self.session.event_ledger.now(),
                )
            elif (
                new_checkpoint.configuration_fingerprint
                != checkpoint.configuration_fingerprint
                or new_checkpoint.replay_dataset_fingerprint
                != checkpoint.replay_dataset_fingerprint
            ):
                raise ValueError("existing new-session baseline is incompatible")
        started = _started_event(self.store.load_events(), reconciliation_id)
        base = int(started.payload["base_checkpoint_sequence"]) if started else 0
        start = int(started.payload["tail_start_sequence"]) if started else 0
        end = int(started.payload["tail_end_sequence"]) if started else 0
        return ReconciliationResult(
            ReconciliationStatus.RESOLVED,
            "already_reconciled",
            "the reconciliation commit already exists",
            reconciliation_id,
            base,
            start,
            end,
            (),
            new_id,
        )

    def _failed(
        self, plan: ReconciliationPlan, reason: str, message: str
    ) -> ReconciliationResult:
        return ReconciliationResult(
            ReconciliationStatus.FAILED,
            reason,
            message,
            plan.reconciliation_id,
            plan.base_checkpoint_sequence,
            plan.tail_start_sequence,
            plan.tail_end_sequence,
        )

    def _failure_from_store(self, reason: str) -> ReconciliationResult:
        self.session.require_reconciliation()
        try:
            checkpoint = self.store.load_latest_checkpoint()
            base = checkpoint.last_event_sequence if checkpoint else 0
        except DurableStoreError:
            base = 0
        identity = _reconciliation_id(self.store.session_id, base, base + 1, base)
        return ReconciliationResult(
            ReconciliationStatus.FAILED,
            reason,
            "reconciliation failed closed",
            identity,
            base,
            base + 1,
            base,
        )


def _events_by_client(events: tuple[AuditEvent, ...]) -> dict[str, tuple[AuditEvent, ...]]:
    grouped: dict[str, list[AuditEvent]] = {}
    relevant = {
        AuditEventType.RISK_APPROVED,
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        AuditEventType.ORDER_SUBMISSION_INDETERMINATE,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.ORDER_FILLED,
        AuditEventType.ORDER_REJECTED,
        AuditEventType.ORDER_CANCELLED,
        AuditEventType.POSITION_OPENED,
        AuditEventType.RESERVATION_RELEASED,
    }
    for event in events:
        if event.event_type not in relevant:
            continue
        client_id = event.correlation.client_order_id
        if client_id is None:
            continue
        grouped.setdefault(client_id, []).append(event)
    return {key: tuple(value) for key, value in grouped.items()}


def _last(events: tuple[AuditEvent, ...], event_type: AuditEventType) -> AuditEvent | None:
    return next((item for item in reversed(events) if item.event_type is event_type), None)


def _terminal_status_event(events: tuple[AuditEvent, ...]) -> OrderStatus | None:
    statuses = {
        AuditEventType.ORDER_FILLED: OrderStatus.FILLED,
        AuditEventType.ORDER_REJECTED: OrderStatus.REJECTED,
        AuditEventType.ORDER_CANCELLED: OrderStatus.CANCELLED,
    }
    found = [statuses[item.event_type] for item in events if item.event_type in statuses]
    return found[-1] if found else None


def _record_from_attempt(
    client_id: str, broker_id: str, status: OrderStatus, event: AuditEvent
) -> dict[str, object]:
    payload = event.payload
    required = ("symbol", "side", "size", "order_type")
    if any(key not in payload for key in required):
        raise ValueError("submission attempt payload is incomplete")
    return {
        "client_order_id": client_id,
        "broker_order_id": broker_id,
        "status": status.value,
        "reservation_released": True,
        "request": {
            "symbol": payload["symbol"],
            "side": payload["side"],
            "size": payload["size"],
            "order_type": payload["order_type"],
            "order_id": client_id,
            "price": None,
            "sl_price": payload.get("sl_price"),
            "tp_price": payload.get("tp_price"),
        },
    }


def _component_snapshots(session: PaperTradingSession) -> dict[str, object]:
    return {
        "risk": session.risk_engine.snapshot_state(),
        "orders": session.order_manager.snapshot_state(),
        "broker": session.broker.snapshot_state(),
        "replay": session.replay.snapshot_state(),
        "session": session.snapshot_state(),
    }


def _restore_components(session: PaperTradingSession, state: Mapping[str, object]) -> None:
    session.risk_engine.restore_state(state["risk"])
    session.order_manager.restore_state(state["orders"])
    session.broker.restore_state(state["broker"])
    session.replay.restore_state(state["replay"])
    session.restore_state(state["session"])


def _reconciliation_id(session_id: str, base: int, start: int, end: int) -> str:
    raw = f"{session_id}|{base}|{start}|{end}".encode()
    return f"reconcile-{hashlib.sha256(raw).hexdigest()[:24]}"


def _committed_resolution(
    events: tuple[AuditEvent, ...], checkpoint: StoredCheckpoint
) -> AuditEvent | None:
    return next(
        (
            event
            for event in reversed(events[: checkpoint.last_event_sequence])
            if event.event_type is AuditEventType.RECONCILIATION_RESOLVED
        ),
        None,
    )


def _started_event(
    events: tuple[AuditEvent, ...], reconciliation_id: str
) -> AuditEvent | None:
    return next(
        (
            event
            for event in events
            if event.event_type is AuditEventType.RECONCILIATION_STARTED
            and event.payload.get("reconciliation_id") == reconciliation_id
        ),
        None,
    )


def _plan_result(
    plan: ReconciliationPlan, status: ReconciliationStatus
) -> ReconciliationResult:
    return ReconciliationResult(
        status,
        plan.reason,
        plan.message,
        plan.reconciliation_id,
        plan.base_checkpoint_sequence,
        plan.tail_start_sequence,
        plan.tail_end_sequence,
        plan.proposed_actions,
    )


def _utc_now(value: datetime | None, session: PaperTradingSession) -> datetime:
    candidate = value if value is not None else session.event_ledger.now()
    if not isinstance(candidate, datetime) or candidate.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    if candidate.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return candidate.astimezone(UTC)
