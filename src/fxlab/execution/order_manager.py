"""Phase 5 application-layer coordination from signals to broker submissions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock

from ..risk.engine import KillSwitchReason, RiskDecision, RiskEngine, RiskRejection
from .broker import BrokerAdapter, OrderRequest, OrderStatus, Tick
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

    _records: dict[str, OrderRecord] = field(default_factory=dict, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def submit(
        self, intent: ExecutionIntent, *, current_time: datetime
    ) -> ExecutionResult:
        """Risk-check and submit exactly one market order for an execution intent."""
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
            return quote_result
        entry_price = quote_result

        try:
            account = self.broker.get_account_info()
        except Exception:
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
            return ExecutionResult(
                kind=ExecutionResultKind.RISK_REJECTED,
                reason="risk_rejected",
                message="risk engine rejected the execution intent",
                risk_rejection=risk_result,
            )
        if not isinstance(risk_result, RiskDecision):
            return _failure(
                ExecutionResultKind.EXECUTION_REJECTED,
                "invalid_risk_result",
                "risk engine returned an unsupported result",
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
            self.risk_engine.release_approval(risk_result.order_id)
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

        try:
            broker_order_id = self.broker.submit_order(request)
        except Exception:
            self.risk_engine.trigger_kill_switch(
                KillSwitchReason.POSITION_RECONCILIATION_FAILED
            )
            return ExecutionResult(
                kind=ExecutionResultKind.INDETERMINATE,
                reason="broker_submission_exception",
                message="broker submission outcome is indeterminate",
                record=provisional,
                risk_decision=risk_result,
            )

        if not isinstance(broker_order_id, str) or not broker_order_id.strip():
            self.risk_engine.trigger_kill_switch(
                KillSwitchReason.POSITION_RECONCILIATION_FAILED
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
        return ExecutionResult(
            kind=ExecutionResultKind.SUBMITTED,
            reason="submitted",
            message="broker accepted the order submission",
            record=submitted,
            risk_decision=risk_result,
        )

    def refresh_order_status(self, client_order_id: str) -> ExecutionResult:
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
            return ExecutionResult(
                kind=ExecutionResultKind.STATUS_FAILURE,
                reason="status_poll_exception",
                message="broker order status could not be obtained",
                record=record,
            )
        status = _parse_status(raw_status)
        if status is None:
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

        if should_release:
            self.risk_engine.release_approval(client_order_id)
        return ExecutionResult(
            kind=ExecutionResultKind.STATUS_UPDATED,
            reason="status_updated",
            message="broker order status was updated",
            record=updated,
        )

    def confirm_position_reflected(self, client_order_id: str) -> bool:
        """Release a filled order's reservation after explicit external confirmation."""
        with self._lock:
            record = self._records.get(client_order_id)
            if record is None or record.status is not OrderStatus.FILLED:
                return False
            if record.reservation_released:
                return True
            self._records[client_order_id] = replace(record, reservation_released=True)
        return self.risk_engine.release_approval(client_order_id)

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        """Return an immutable snapshot for one managed order."""
        with self._lock:
            return self._records.get(client_order_id)

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


def _failure(kind: ExecutionResultKind, reason: str, message: str) -> ExecutionResult:
    return ExecutionResult(kind=kind, reason=reason, message=message)
