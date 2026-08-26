"""Phase 5 application-layer coordination from signals to broker submissions."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock

from ..risk.engine import KillSwitchReason, RiskDecision, RiskEngine, RiskRejection
from .broker import BrokerAdapter, OrderRequest, OrderStatus, Tick
from .event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
    deterministic_signal_id,
)
from .signal_engine import SignalEvent


class ExecutionResultKind(StrEnum):
    """Stable machine-readable outcomes from the Phase 5 coordinator."""

    SUBMITTED = "submitted"
    EXECUTION_REJECTED = "execution_rejected"
    RISK_REJECTED = "risk_rejected"
    INDETERMINATE = "indeterminate"
    STATUS_UPDATED = "status_updated"
    STATUS_FAILURE = "status_failure"


@dataclass(frozen=True)
class ExecutionIntent:
    """Caller-owned protective prices for one directional market signal."""

    signal: SignalEvent
    sl_price: float
    tp_price: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.signal, SignalEvent):
            raise ValueError("signal must be a SignalEvent")


@dataclass(frozen=True)
class OrderRecord:
    """Immutable public snapshot of one session-local submission."""

    client_order_id: str
    broker_order_id: str | None
    request: OrderRequest
    status: OrderStatus
    reservation_released: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    """Structured result for submission, rejection, and status operations."""

    kind: ExecutionResultKind
    reason: str
    message: str
    record: OrderRecord | None = None
    risk_decision: RiskDecision | None = None
    risk_rejection: RiskRejection | None = None


@dataclass
class OrderManager:
    """Coordinate market intent, risk approval, and broker submission.

    This class does not connect brokers, invent SL/TP, close positions, persist state,
    or infer that a fill is reflected in an account snapshot.
    """

    broker: BrokerAdapter
    risk_engine: RiskEngine
    event_ledger: EventLedger | None = None

    _records: dict[str, OrderRecord] = field(default_factory=dict, init=False)
    _audit_failed: bool = field(default=False, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def submit(
        self, intent: ExecutionIntent, *, current_time: datetime
    ) -> ExecutionResult:
        """Risk-check and submit exactly one market order for an execution intent."""
        if self.audit_failed:
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "audit_unavailable",
                "audit integrity is unavailable; new execution is disabled",
            )
        if not isinstance(intent, ExecutionIntent):
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "invalid_execution_intent",
                "intent must be an ExecutionIntent",
            )
        current_utc = _aware_utc(current_time)
        if current_utc is None:
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "invalid_current_time",
                "current_time must be timezone-aware",
            )

        quote_result = self._current_entry_price(intent.signal, current_utc)
        if isinstance(quote_result, ExecutionResult):
            if not self._audit_execution_failure(
                intent.signal, current_utc, quote_result.reason
            ):
                return _failure(
                    ExecutionResultKind.EXECUTION_REJECTED,
                    "audit_failure_before_submission",
                    "execution rejection could not be audited",
                )
            return quote_result
        entry_price = quote_result

        try:
            account = self.broker.get_account_info()
        except Exception:
            if not self._audit_execution_failure(
                intent.signal, current_utc, "account_unavailable"
            ):
                return _failure(
                    ExecutionResultKind.EXECUTION_REJECTED,
                    "audit_failure_before_submission",
                    "account failure could not be audited",
                )
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "account_unavailable",
                "broker account snapshot could not be obtained",
            )

        risk_result = self.risk_engine.evaluate(
            intent.signal,
            entry_price=entry_price,
            sl_price=intent.sl_price,
            tp_price=intent.tp_price,
            account=account,
            current_time=current_utc,
        )
        if isinstance(risk_result, RiskRejection):
            correlation = _signal_correlation(
                intent.signal, client_order_id=risk_result.order_id
            )
            if not self._audit(
                AuditEventType.RISK_REJECTED,
                occurred_at=current_utc,
                component=AuditComponent.RISK_ENGINE,
                correlation=correlation,
                payload={
                    "reason": risk_result.reason,
                    "message": risk_result.message,
                    "kill_switch_reason": risk_result.kill_switch_reason,
                },
            ):
                return _failure(
                    ExecutionResultKind.EXECUTION_REJECTED,
                    "audit_failure_before_submission",
                    "risk rejection could not be audited",
                )
            return ExecutionResult(
                kind=ExecutionResultKind.RISK_REJECTED,
                reason="risk_rejected",
                message="risk engine rejected the execution intent",
                risk_rejection=risk_result,
            )
        if not isinstance(risk_result, RiskDecision):
            self._audit_execution_failure(
                intent.signal, current_utc, "invalid_risk_result"
            )
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "invalid_risk_result",
                "risk engine returned an unsupported result",
            )

        correlation = _signal_correlation(
            risk_result.signal, client_order_id=risk_result.order_id
        )
        if not self._audit(
            AuditEventType.RISK_APPROVED,
            occurred_at=current_utc,
            component=AuditComponent.RISK_ENGINE,
            correlation=correlation,
            payload={
                "size_lots": risk_result.size_lots,
                "entry_price": risk_result.entry_price,
                "sl_price": risk_result.sl_price,
                "tp_price": risk_result.tp_price,
                "modeled_monetary_risk": risk_result.modeled_monetary_risk,
            },
        ):
            self.risk_engine.release_approval(risk_result.order_id)
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "audit_failure_before_submission",
                "risk approval could not be audited",
            )

        try:
            request = OrderRequest(
                symbol=risk_result.signal.symbol,
                side=risk_result.signal.side,
                size=risk_result.size_lots,
                order_type="market",
                order_id=risk_result.order_id,
                price=None,
                sl_price=risk_result.sl_price,
                tp_price=risk_result.tp_price,
            )
        except Exception:
            self._audit(
                AuditEventType.EXECUTION_FAILED,
                occurred_at=current_utc,
                component=AuditComponent.ORDER_MANAGER,
                correlation=correlation,
                payload={"reason": "order_construction_failed"},
            )
            released = self.risk_engine.release_approval(risk_result.order_id)
            if released:
                self._audit_release(risk_result.order_id, current_utc, correlation)
            return ExecutionResult(
                kind=ExecutionResultKind.EXECUTION_REJECTED,
                reason="order_construction_failed",
                message="approved decision could not be converted to an order request",
                risk_decision=risk_result,
            )

        provisional = OrderRecord(
            client_order_id=risk_result.order_id,
            broker_order_id=None,
            request=request,
            status=OrderStatus.PENDING,
        )
        with self._lock:
            if risk_result.order_id in self._records:
                return ExecutionResult(
                    kind=ExecutionResultKind.EXECUTION_REJECTED,
                    reason="duplicate_manager_order",
                    message="client order ID is already managed",
                    record=self._records[risk_result.order_id],
                    risk_decision=risk_result,
                )
            self._records[risk_result.order_id] = provisional

        if not self._audit(
            AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
            occurred_at=current_utc,
            component=AuditComponent.ORDER_MANAGER,
            correlation=correlation,
            payload=_request_payload(request),
        ):
            with self._lock:
                self._records.pop(risk_result.order_id, None)
            released = self.risk_engine.release_approval(risk_result.order_id)
            if released:
                self._audit_release(risk_result.order_id, current_utc, correlation)
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "audit_failure_before_submission",
                "submission attempt could not be audited",
            )

        try:
            broker_order_id = self.broker.submit_order(request)
        except Exception:
            self._submission_indeterminate(
                current_utc, correlation, "broker_submission_exception"
            )
            return ExecutionResult(
                kind=ExecutionResultKind.INDETERMINATE,
                reason="broker_submission_exception",
                message="broker submission outcome is indeterminate",
                record=provisional,
                risk_decision=risk_result,
            )

        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            self._submission_indeterminate(
                current_utc, correlation, "invalid_broker_order_id"
            )
            return ExecutionResult(
                kind=ExecutionResultKind.INDETERMINATE,
                reason="invalid_broker_order_id",
                message="broker returned no usable order identity",
                record=provisional,
                risk_decision=risk_result,
            )

        submitted = replace(provisional, broker_order_id=broker_order_id)
        with self._lock:
            self._records[risk_result.order_id] = submitted
        submitted_correlation = replace(
            correlation, broker_order_id=broker_order_id.strip()
        )
        if not self._audit(
            AuditEventType.ORDER_SUBMITTED,
            occurred_at=current_utc,
            component=AuditComponent.PAPER_BROKER,
            correlation=submitted_correlation,
            payload={"status": submitted.status},
        ):
            self.risk_engine.trigger_kill_switch(
                KillSwitchReason.POSITION_RECONCILIATION_FAILED
            )
            return ExecutionResult(
                kind=ExecutionResultKind.INDETERMINATE,
                reason="audit_failure_after_submission",
                message="broker acknowledged submission but audit recording failed",
                record=submitted,
                risk_decision=risk_result,
            )
        return ExecutionResult(
            kind=ExecutionResultKind.SUBMITTED,
            reason="submitted",
            message="broker accepted the order submission",
            record=submitted,
            risk_decision=risk_result,
        )

    def refresh_order_status(
        self, client_order_id: str, *, current_time: datetime | None = None
    ) -> ExecutionResult:
        """Refresh one known order without inferring account-position reflection."""
        with self._lock:
            record = self._records.get(client_order_id)
        if record is None:
            return _failure(
                ExecutionResultKind.STATUS_FAILURE,
                "unknown_client_order_id",
                "client order ID is not managed",
            )
        if record.broker_order_id is None:
            return ExecutionResult(
                kind=ExecutionResultKind.STATUS_FAILURE,
                reason="broker_order_id_unavailable",
                message="submission outcome has no usable broker order ID",
                record=record,
            )

        try:
            raw_status = self.broker.get_order_status(record.broker_order_id)
        except Exception:
            self._audit_status_failure(record, current_time, "status_poll_exception")
            return ExecutionResult(
                kind=ExecutionResultKind.STATUS_FAILURE,
                reason="status_poll_exception",
                message="broker order status could not be obtained",
                record=record,
            )
        status = _parse_status(raw_status)
        if status is None:
            self._audit_status_failure(record, current_time, "malformed_broker_status")
            return ExecutionResult(
                kind=ExecutionResultKind.STATUS_FAILURE,
                reason="malformed_broker_status",
                message="broker returned an unsupported order status",
                record=record,
            )

        with self._lock:
            current = self._records.get(client_order_id)
            if current is None:
                return _failure(
                    ExecutionResultKind.STATUS_FAILURE,
                    "unknown_client_order_id",
                    "client order ID is not managed",
                )
            changed = status is not current.status
            terminal_without_position = status in (
                OrderStatus.REJECTED,
                OrderStatus.CANCELLED,
            )
            should_release = terminal_without_position and not current.reservation_released
            updated = replace(
                current,
                status=status,
                reservation_released=current.reservation_released or should_release,
            )
            self._records[client_order_id] = updated

        correlation = _record_correlation(updated)
        occurred_at = self._audit_time(current_time)
        audit_ok = True
        if changed and status in {
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        }:
            event_type = {
                OrderStatus.FILLED: AuditEventType.ORDER_FILLED,
                OrderStatus.REJECTED: AuditEventType.ORDER_REJECTED,
                OrderStatus.CANCELLED: AuditEventType.ORDER_CANCELLED,
            }[status]
            audit_ok = self._audit(
                event_type,
                occurred_at=occurred_at,
                component=AuditComponent.PAPER_BROKER,
                correlation=correlation,
                payload={"previous_status": current.status, "status": status},
            )
        if not audit_ok:
            if should_release:
                with self._lock:
                    latest = self._records.get(client_order_id)
                    if latest is not None:
                        self._records[client_order_id] = replace(
                            latest, reservation_released=False
                        )
            return ExecutionResult(
                kind=ExecutionResultKind.STATUS_FAILURE,
                reason="audit_failure_after_status",
                message="broker status changed but could not be audited",
                record=updated,
            )
        if should_release:
            released = self.risk_engine.release_approval(client_order_id)
            if released:
                self._audit_release(client_order_id, occurred_at, correlation)
            else:
                with self._lock:
                    latest = self._records.get(client_order_id)
                    if latest is not None:
                        updated = replace(latest, reservation_released=False)
                        self._records[client_order_id] = updated
        return ExecutionResult(
            kind=ExecutionResultKind.STATUS_UPDATED,
            reason="status_updated",
            message="broker order status was updated",
            record=updated,
        )

    def confirm_position_reflected(
        self, client_order_id: str, *, current_time: datetime | None = None
    ) -> bool:
        """Release a filled order's reservation after explicit external confirmation."""
        with self._lock:
            record = self._records.get(client_order_id)
            if record is None or record.status is not OrderStatus.FILLED:
                return False
            if record.reservation_released:
                return True
            self._records[client_order_id] = replace(record, reservation_released=True)
        released = self.risk_engine.release_approval(client_order_id)
        if released:
            self._audit_release(
                client_order_id, self._audit_time(current_time), _record_correlation(record)
            )
        return released

    @property
    def audit_failed(self) -> bool:
        with self._lock:
            return self._audit_failed

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        """Return an immutable snapshot for one managed order."""
        with self._lock:
            return self._records.get(client_order_id)

    def snapshot_state(self) -> dict[str, object]:
        """Return primitive order state without polling or submitting."""
        with self._lock:
            return {
                "audit_failed": self._audit_failed,
                "records": [
                    {
                        "client_order_id": record.client_order_id,
                        "broker_order_id": record.broker_order_id,
                        "status": record.status.value,
                        "reservation_released": record.reservation_released,
                        "request": _request_payload(record.request)
                        | {
                            "order_id": record.request.order_id,
                            "price": record.request.price,
                        },
                    }
                    for _, record in sorted(self._records.items())
                ],
            }

    def restore_state(self, state: Mapping[str, object]) -> None:
        """Atomically restore validated records without broker operations."""
        records, audit_failed = _parse_order_manager_state(state)
        with self._lock:
            self._records = records
            self._audit_failed = audit_failed

    def _audit(
        self,
        event_type: AuditEventType,
        *,
        occurred_at: datetime,
        component: AuditComponent,
        correlation: EventCorrelation,
        payload: dict[str, object],
    ) -> bool:
        if self.event_ledger is None:
            return True
        try:
            self.event_ledger.append(
                event_type,
                occurred_at=occurred_at,
                component=component,
                correlation=correlation,
                payload=payload,
            )
            return True
        except Exception:
            with self._lock:
                self._audit_failed = True
            return False

    def _audit_time(self, value: datetime | None) -> datetime:
        normalized = _aware_utc(value) if value is not None else None
        if normalized is not None:
            return normalized
        if self.event_ledger is not None:
            try:
                return self.event_ledger.now()
            except Exception:
                pass
        return datetime.now(UTC)

    def _audit_release(
        self, client_order_id: str, occurred_at: datetime, correlation: EventCorrelation
    ) -> None:
        self._audit(
            AuditEventType.RESERVATION_RELEASED,
            occurred_at=occurred_at,
            component=AuditComponent.RISK_ENGINE,
            correlation=replace(correlation, client_order_id=client_order_id),
            payload={"client_order_id": client_order_id},
        )

    def _audit_status_failure(
        self, record: OrderRecord, current_time: datetime | None, reason: str
    ) -> None:
        self._audit(
            AuditEventType.ORDER_STATUS_FAILED,
            occurred_at=self._audit_time(current_time),
            component=AuditComponent.ORDER_MANAGER,
            correlation=_record_correlation(record),
            payload={"reason": reason},
        )

    def _audit_execution_failure(
        self, signal: SignalEvent, occurred_at: datetime, reason: str
    ) -> bool:
        return self._audit(
            AuditEventType.EXECUTION_FAILED,
            occurred_at=occurred_at,
            component=AuditComponent.ORDER_MANAGER,
            correlation=_signal_correlation(signal),
            payload={"reason": reason},
        )

    def _submission_indeterminate(
        self, occurred_at: datetime, correlation: EventCorrelation, reason: str
    ) -> None:
        activated = self.risk_engine.trigger_kill_switch(
            KillSwitchReason.POSITION_RECONCILIATION_FAILED
        )
        self._audit(
            AuditEventType.ORDER_SUBMISSION_INDETERMINATE,
            occurred_at=occurred_at,
            component=AuditComponent.ORDER_MANAGER,
            correlation=correlation,
            payload={"reason": reason},
        )
        self._audit(
            AuditEventType.RECONCILIATION_FAILED,
            occurred_at=occurred_at,
            component=AuditComponent.ORDER_MANAGER,
            correlation=correlation,
            payload={"reason": reason},
        )
        if activated:
            self._audit(
                AuditEventType.KILL_SWITCH_TRIGGERED,
                occurred_at=occurred_at,
                component=AuditComponent.RISK_ENGINE,
                correlation=correlation,
                payload={
                    "reason": KillSwitchReason.POSITION_RECONCILIATION_FAILED
                },
            )

    def _current_entry_price(
        self, signal: SignalEvent, current_utc: datetime
    ) -> float | ExecutionResult:
        if not isinstance(signal, SignalEvent):
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "invalid_signal",
                "execution intent signal must be a SignalEvent",
            )
        if (
            isinstance(signal.side, bool)
            or not isinstance(signal.side, int)
            or signal.side not in (1, -1)
        ):
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "invalid_signal_side",
                "signal side must be exactly +1 or -1",
            )
        signal_utc = _aware_utc(signal.signal_time)
        if signal_utc is None:
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "invalid_signal_time",
                "signal_time must be timezone-aware",
            )
        try:
            tick = self.broker.get_latest_tick(signal.symbol)
        except Exception:
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "quote_unavailable",
                "broker quote could not be obtained",
            )
        tick_error = _validate_tick(tick, signal, signal_utc, current_utc)
        if tick_error is not None:
            return tick_error
        assert isinstance(tick, Tick)
        return float(tick.ask if signal.side == 1 else tick.bid)


def _validate_tick(
    tick: object,
    signal: SignalEvent,
    signal_utc: datetime,
    current_utc: datetime,
) -> ExecutionResult | None:
    if tick is None:
        return _failure(
            ExecutionResultKind.EXECUTION_REJECTED,
            "missing_quote",
            "broker has no current tick for the signal symbol",
        )
    if not isinstance(tick, Tick):
        return _failure(
            ExecutionResultKind.EXECUTION_REJECTED,
            "invalid_quote",
            "broker quote must be a Tick",
        )
    if _canonical_symbol(tick.symbol) != _canonical_symbol(signal.symbol):
        return _failure(
            ExecutionResultKind.EXECUTION_REJECTED,
            "quote_symbol_mismatch",
            "tick symbol does not match the signal symbol",
        )
    tick_utc = _aware_utc(tick.timestamp)
    if tick_utc is None:
        return _failure(
            ExecutionResultKind.EXECUTION_REJECTED,
            "invalid_quote_time",
            "tick timestamp must be timezone-aware",
        )
    if tick_utc < signal_utc:
        return _failure(
            ExecutionResultKind.EXECUTION_REJECTED,
            "stale_quote",
            "tick timestamp precedes the signal timestamp",
        )
    if tick_utc > current_utc:
        return _failure(
            ExecutionResultKind.EXECUTION_REJECTED,
            "future_quote",
            "tick timestamp is later than current_time",
        )
    if not _positive_finite(tick.bid) or not _positive_finite(tick.ask):
        return _failure(
            ExecutionResultKind.EXECUTION_REJECTED,
            "invalid_quote_price",
            "tick bid and ask must be finite and positive",
        )
    if float(tick.ask) < float(tick.bid):
        return _failure(
            ExecutionResultKind.EXECUTION_REJECTED,
            "invalid_quote_spread",
            "tick ask cannot be below its bid",
        )
    return None


def _parse_status(raw_status: object) -> OrderStatus | None:
    if not isinstance(raw_status, dict) or "status" not in raw_status:
        return None
    value = raw_status["status"]
    if isinstance(value, OrderStatus):
        return value
    if not isinstance(value, str):
        return None
    try:
        return OrderStatus(value)
    except ValueError:
        return None


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except Exception:
        return None


def _positive_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted) and converted > 0


def _canonical_symbol(symbol: object) -> str:
    return symbol.strip().upper() if isinstance(symbol, str) else ""


def _signal_correlation(
    signal: SignalEvent, *, client_order_id: str | None = None
) -> EventCorrelation:
    try:
        signal_id = deterministic_signal_id(signal)
    except ValueError:
        signal_id = None
    return EventCorrelation(signal_id=signal_id, client_order_id=client_order_id)


def _record_correlation(record: OrderRecord) -> EventCorrelation:
    return EventCorrelation(
        signal_id=record.client_order_id,
        client_order_id=record.client_order_id,
        broker_order_id=record.broker_order_id,
    )


def _request_payload(request: OrderRequest) -> dict[str, object]:
    return {
        "symbol": request.symbol,
        "side": request.side,
        "size": request.size,
        "order_type": request.order_type,
        "sl_price": request.sl_price,
        "tp_price": request.tp_price,
    }


def _parse_order_manager_state(
    state: Mapping[str, object],
) -> tuple[dict[str, OrderRecord], bool]:
    if not isinstance(state, Mapping) or not isinstance(state.get("audit_failed"), bool):
        raise ValueError("invalid order-manager state")
    raw_records = state.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("order-manager records must be a list")
    records: dict[str, OrderRecord] = {}
    for raw in raw_records:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("request"), Mapping):
            raise ValueError("invalid order record")
        request_raw = raw["request"]
        if (
            not isinstance(request_raw.get("symbol"), str)
            or not request_raw.get("symbol")
            or isinstance(request_raw.get("side"), bool)
            or not isinstance(request_raw.get("side"), int)
            or request_raw.get("side") not in (1, -1)
            or not _positive_finite(request_raw.get("size"))
            or not isinstance(request_raw.get("order_type"), str)
            or not isinstance(request_raw.get("order_id"), str)
        ):
            raise ValueError("invalid persisted order request")
        try:
            request = OrderRequest(
                symbol=request_raw["symbol"],
                side=request_raw["side"],
                size=request_raw["size"],
                order_type=request_raw["order_type"],
                order_id=request_raw["order_id"],
                price=request_raw.get("price"),
                sl_price=request_raw.get("sl_price"),
                tp_price=request_raw.get("tp_price"),
            )
            client_id = raw["client_order_id"]
            broker_id = raw.get("broker_order_id")
            status = OrderStatus(raw["status"])
            released = raw["reservation_released"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid order record") from exc
        if (
            not isinstance(client_id, str)
            or not client_id.strip()
            or client_id != request.order_id
            or client_id in records
            or (broker_id is not None and (not isinstance(broker_id, str) or not broker_id.strip()))
            or not isinstance(released, bool)
        ):
            raise ValueError("invalid order record identity")
        records[client_id] = OrderRecord(client_id, broker_id, request, status, released)
    return records, bool(state["audit_failed"])


def _failure(kind: ExecutionResultKind, reason: str, message: str) -> ExecutionResult:
    return ExecutionResult(kind=kind, reason=reason, message=message)
