"""Small deterministic session runner for historical paper-trading replay."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

import pandas as pd

from ..data.schema import OHLCV, timeframe_to_timedelta
from ..risk.engine import KillSwitchReason, RiskEngine
from .broker import AccountInfo, OrderStatus, Tick
from .event_ledger import (
    AuditComponent,
    AuditEventType,
    AuditLedgerError,
    EventCorrelation,
    EventLedger,
    deterministic_signal_id,
)
from .market_data import MarketDataStream
from .order_manager import ExecutionIntent, ExecutionResult, ExecutionResultKind, OrderManager
from .paper_broker import PaperBroker, PositionClose
from .signal_engine import SignalEngine, SignalEvent


@dataclass(frozen=True)
class MarketContext:
    """Market information available when an execution policy sees a signal."""

    tick: Tick
    current_time: datetime
    closed_bars: pd.DataFrame


class ExecutionPolicy(Protocol):
    """Caller-owned SL/TP policy; Phase 6 supplies no default strategy."""

    def __call__(
        self, signal: SignalEvent, market_context: MarketContext
    ) -> ExecutionIntent | None: ...


class CycleKind(StrEnum):
    PROCESSED = "processed"
    NO_SIGNAL = "no_signal"
    POLICY_DECLINED = "policy_declined"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class PaperCycleResult:
    kind: CycleKind
    current_time: datetime | None
    tick: Tick | None = None
    signals: tuple[SignalEvent, ...] = ()
    executions: tuple[ExecutionResult, ...] = ()
    closes: tuple[PositionClose, ...] = ()
    reason: str = ""
    message: str = ""


@dataclass
class HistoricalBarReplay:
    """Emit one zero-spread close quote when each historical bar becomes known.

    Canonical OHLCV does not contain tick microstructure. This replay therefore emits
    exactly one quote at bar close with bid == ask == close; it does not synthesize an
    intrabar path or spread. Future events remain unexposed until ``next_tick`` advances.
    """

    bars_by_symbol: Mapping[str, pd.DataFrame]
    timeframe: str
    _events: tuple[Tick, ...] = field(init=False, repr=False)
    _cursor: int = field(default=0, init=False)
    _stopped: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        delta = timeframe_to_timedelta(self.timeframe)
        events: list[Tick] = []
        for raw_symbol, raw_bars in self.bars_by_symbol.items():
            symbol = raw_symbol.strip().upper()
            if not symbol or not isinstance(raw_bars, pd.DataFrame):
                raise ValueError("replay requires non-empty symbols and DataFrames")
            bars = raw_bars.copy()
            if not isinstance(bars.index, pd.DatetimeIndex):
                raise ValueError("replay bars must use a DatetimeIndex")
            bars.index = (
                bars.index.tz_localize("UTC")
                if bars.index.tz is None
                else bars.index.tz_convert("UTC")
            )
            missing = [column for column in OHLCV if column not in bars.columns]
            if missing:
                raise ValueError(f"replay bars missing columns: {missing}")
            for ts_open, row in bars.sort_index().iterrows():
                close = _positive_float(row["close"])
                if close is None:
                    raise ValueError("replay close prices must be finite and positive")
                timestamp = (ts_open + delta).to_pydatetime()
                events.append(Tick(symbol, timestamp, close, close, close))
        events.sort(key=lambda tick: (tick.timestamp.astimezone(UTC), tick.symbol))
        self._events = tuple(events)

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._events)

    def next_tick(self, *, until: datetime | None = None) -> Tick | None:
        if self._stopped or self.exhausted:
            return None
        boundary = _aware_utc(until) if until is not None else None
        if until is not None and boundary is None:
            raise ValueError("until must be timezone-aware")
        candidate = self._events[self._cursor]
        if boundary is not None and candidate.timestamp.astimezone(UTC) > boundary:
            return None
        self._cursor += 1
        return candidate

    def stop(self) -> None:
        self._stopped = True


@dataclass
class PaperTradingSession:
    """Connect replay, signals, explicit execution policy, and Phase 5 execution."""

    broker: PaperBroker
    replay: HistoricalBarReplay
    market_data: MarketDataStream
    signal_engine: SignalEngine
    order_manager: OrderManager
    risk_engine: RiskEngine
    execution_policy: ExecutionPolicy
    event_ledger: EventLedger

    _started: bool = field(default=False, init=False)
    _stopped: bool = field(default=False, init=False)
    _audit_failed: bool = field(default=False, init=False)
    _tracked_orders: set[str] = field(default_factory=set, init=False)
    _position_correlations: dict[str, EventCorrelation] = field(
        default_factory=dict, init=False
    )
    _reported_reconciliation_failures: set[str] = field(
        default_factory=set, init=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.event_ledger, EventLedger):
            raise ValueError("event_ledger must be an EventLedger")
        if self.order_manager.event_ledger is None:
            self.order_manager.event_ledger = self.event_ledger
        elif self.order_manager.event_ledger is not self.event_ledger:
            raise ValueError("session and order manager must share one EventLedger")

    def start(self) -> None:
        if self._started:
            return
        if self._stopped:
            raise RuntimeError("a stopped paper session cannot be restarted")
        self.broker.connect()
        try:
            self.market_data.start()
        except Exception:
            self.broker.disconnect()
            raise
        try:
            self._append(
                AuditEventType.SESSION_STARTED,
                occurred_at=self.event_ledger.now(),
                component=AuditComponent.PAPER_SESSION,
                payload={},
            )
        except AuditLedgerError:
            self.broker.disconnect()
            self._audit_failed = True
            raise
        self._started = True

    def poll_once(self, *, until: datetime | None = None) -> PaperCycleResult:
        """Advance one replay cycle, failing closed on any audit-integrity loss."""
        try:
            return self._poll_once(until=until)
        except AuditLedgerError:
            self._audit_failed = True
            self.risk_engine.trigger_kill_switch(
                KillSwitchReason.POSITION_RECONCILIATION_FAILED
            )
            return _cycle_failure(
                "audit_unavailable", "required runtime transition could not be audited"
            )

    def _poll_once(self, *, until: datetime | None = None) -> PaperCycleResult:
        if not self._started or self._stopped:
            return _cycle_failure("session_not_running", "paper session is not running")
        if self._audit_failed or self.order_manager.audit_failed:
            return _cycle_failure(
                "audit_unavailable", "audit integrity is unavailable; execution is disabled"
            )
        try:
            tick = self.replay.next_tick(until=until)
        except Exception:
            self._record_runtime_failure("replay_failure")
            return _cycle_failure("replay_failure", "historical replay could not advance")
        if tick is None:
            kind = CycleKind.EXHAUSTED if self.replay.exhausted else CycleKind.NO_SIGNAL
            reason = "replay_exhausted" if kind is CycleKind.EXHAUSTED else "no_event_available"
            return PaperCycleResult(kind, None, reason=reason)

        current_time = tick.timestamp.astimezone(UTC)
        try:
            accepted = self.broker.accept_tick(tick)
            if not accepted:
                return _cycle_failure(
                    "out_of_order_tick",
                    "paper broker rejected an out-of-order replay tick",
                    current_time,
                )
            self._append(
                AuditEventType.MARKET_EVENT,
                occurred_at=current_time,
                component=AuditComponent.REPLAY,
                payload={
                    "symbol": tick.symbol,
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "mid": tick.mid,
                    "timestamp": tick.timestamp,
                },
            )
            self.market_data.on_tick(tick)
            closes = self._drain_close_events(current_time)
            account = self.broker.get_account_info()
            self._record_account(account, current_time)
        except AuditLedgerError:
            self._audit_failed = True
            self.risk_engine.trigger_kill_switch(
                KillSwitchReason.POSITION_RECONCILIATION_FAILED
            )
            return _cycle_failure(
                "audit_unavailable", "market event could not be audited", current_time
            )
        except Exception:
            self._record_runtime_failure("market_data_failure", current_time)
            return _cycle_failure(
                "market_data_failure", "replay market state could not be accepted", current_time
            )

        was_active = self.risk_engine.kill_switch_active
        account_rejection = self.risk_engine.check_account_state(account, current_time)
        try:
            self._record_kill_transition(was_active, current_time)
        except AuditLedgerError:
            return _cycle_failure(
                "audit_unavailable", "kill-switch transition could not be audited"
            )
        if account_rejection is not None:
            return PaperCycleResult(
                kind=CycleKind.FAILED,
                current_time=current_time,
                tick=tick,
                closes=closes,
                reason=account_rejection.reason,
                message=account_rejection.message,
            )

        try:
            signals = tuple(self.signal_engine.process_all_symbols([tick.symbol]))
        except Exception:
            self._record_runtime_failure("signal_processing_failure", current_time)
            return _cycle_failure(
                "signal_processing_failure", "signal processing failed", current_time, tick
            )

        for signal in signals:
            self._append(
                AuditEventType.SIGNAL_EMITTED,
                occurred_at=current_time,
                component=AuditComponent.SIGNAL_ENGINE,
                correlation=_signal_correlation(signal),
                payload=_signal_payload(signal),
            )

        executions: list[ExecutionResult] = []
        declined = False
        for signal in signals:
            try:
                bars = self.market_data.get_closed_bars(
                    signal.symbol, signal.timeframe, count=self.signal_engine.lookback_count
                )
                context = MarketContext(
                    tick=tick,
                    current_time=current_time,
                    closed_bars=bars.copy(),
                )
                intent = self.execution_policy(signal, context)
            except Exception:
                self._append(
                    AuditEventType.EXECUTION_POLICY_FAILED,
                    occurred_at=current_time,
                    component=AuditComponent.EXECUTION_POLICY,
                    correlation=_signal_correlation(signal),
                    payload={"reason": "execution_policy_exception"},
                )
                return _cycle_failure(
                    "execution_policy_failure",
                    "execution policy failed",
                    current_time,
                    tick,
                    signals,
                )
            if intent is None:
                self._append(
                    AuditEventType.SIGNAL_DECLINED,
                    occurred_at=current_time,
                    component=AuditComponent.EXECUTION_POLICY,
                    correlation=_signal_correlation(signal),
                    payload={"reason": "policy_declined"},
                )
                declined = True
                continue
            if not isinstance(intent, ExecutionIntent) or intent.signal != signal:
                self._append(
                    AuditEventType.EXECUTION_FAILED,
                    occurred_at=current_time,
                    component=AuditComponent.PAPER_SESSION,
                    correlation=_signal_correlation(signal),
                    payload={"reason": "invalid_execution_intent"},
                )
                return _cycle_failure(
                    "invalid_execution_intent",
                    "execution policy must return an intent for the supplied signal",
                    current_time,
                    tick,
                    signals,
                )
            self._append(
                AuditEventType.EXECUTION_INTENT_CREATED,
                occurred_at=current_time,
                component=AuditComponent.EXECUTION_POLICY,
                correlation=_signal_correlation(signal),
                payload={"sl_price": intent.sl_price, "tp_price": intent.tp_price},
            )
            result = self.order_manager.submit(intent, current_time=current_time)
            executions.append(result)
            if self.order_manager.audit_failed:
                self._audit_failed = True
                return _cycle_failure(
                    "audit_unavailable",
                    "order audit integrity failed; execution is disabled",
                    current_time,
                    tick,
                    signals,
                )
            if result.kind is ExecutionResultKind.SUBMITTED and result.record is not None:
                self._tracked_orders.add(result.record.client_order_id)

        executions.extend(self._refresh_and_reconcile(current_time))
        if executions:
            return PaperCycleResult(
                CycleKind.PROCESSED,
                current_time,
                tick,
                signals,
                tuple(executions),
                closes,
            )
        if declined:
            return PaperCycleResult(
                CycleKind.POLICY_DECLINED,
                current_time,
                tick,
                signals,
                closes=closes,
            )
        return PaperCycleResult(
            CycleKind.NO_SIGNAL,
            current_time,
            tick,
            signals,
            closes=closes,
        )

    def close_position(self, position_id: str) -> PositionClose | None:
        """Manually close a paper position and notify risk exactly once."""
        if not self._started or self._stopped:
            return None
        close_order_id = self.broker.close_position(position_id)
        if close_order_id is None:
            return None
        events = self._drain_close_events(self.event_ledger.now())
        close = next(
            (event for event in events if event.close_order_id == close_order_id),
            None,
        )
        if close is not None:
            self.risk_engine.check_account_state(
                self.broker.get_account_info(), close.close_time
            )
        return close

    def stop(self) -> None:
        if self._stopped:
            return
        if self._started and not self._audit_failed:
            try:
                self._append(
                    AuditEventType.SESSION_STOPPED,
                    occurred_at=self.event_ledger.now(),
                    component=AuditComponent.PAPER_SESSION,
                    payload={},
                )
            except AuditLedgerError:
                self._audit_failed = True
        self.replay.stop()
        if self.broker.is_connected():
            self.broker.disconnect()
        self._stopped = True

    def _refresh_and_reconcile(self, current_time: datetime) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        for client_id in tuple(sorted(self._tracked_orders)):
            result = self.order_manager.refresh_order_status(
                client_id, current_time=current_time
            )
            results.append(result)
            record = result.record
            if record is None or record.status is not OrderStatus.FILLED:
                continue
            correlation = self.broker.get_correlation(client_id)
            if correlation is None or correlation.broker_order_id != record.broker_order_id:
                self._record_reconciliation_failure(client_id, current_time)
                results.append(_reconciliation_failure(result))
                continue
            try:
                account = self.broker.get_account_info()
            except Exception:
                self._record_reconciliation_failure(client_id, current_time)
                results.append(_reconciliation_failure(result))
                continue
            if any(
                position.position_id == correlation.position_id
                for position in account.open_positions
            ):
                event_correlation = EventCorrelation(
                    signal_id=client_id,
                    client_order_id=client_id,
                    broker_order_id=correlation.broker_order_id,
                    position_id=correlation.position_id,
                )
                self._append(
                    AuditEventType.POSITION_OPENED,
                    occurred_at=current_time,
                    component=AuditComponent.PAPER_BROKER,
                    correlation=event_correlation,
                    payload={"status": record.status},
                )
                self._position_correlations[correlation.position_id] = event_correlation
                self.order_manager.confirm_position_reflected(
                    client_id, current_time=current_time
                )
                self._tracked_orders.discard(client_id)
            else:
                self._record_reconciliation_failure(client_id, current_time)
                results.append(_reconciliation_failure(result))
        return results

    def _drain_close_events(
        self, current_time: datetime
    ) -> tuple[PositionClose, ...]:
        events = self.broker.drain_close_events()
        for event in events:
            self._append(
                AuditEventType.POSITION_CLOSED,
                occurred_at=event.close_time,
                component=AuditComponent.PAPER_BROKER,
                correlation=EventCorrelation(
                    signal_id=event.client_entry_order_id,
                    client_order_id=event.client_entry_order_id,
                    broker_order_id=event.broker_entry_order_id,
                    position_id=event.position_id,
                    close_order_id=event.close_order_id,
                ),
                payload={
                    "symbol": event.symbol,
                    "side": event.side,
                    "size": event.size,
                    "entry_price": event.entry_price,
                    "exit_price": event.exit_price,
                    "gross_pnl": event.gross_pnl,
                    "commission": event.commission,
                    "net_realized_pnl": event.net_realized_pnl,
                    "close_reason": event.reason,
                    "close_timestamp": event.close_time,
                },
            )
            was_active = self.risk_engine.kill_switch_active
            self.risk_engine.on_trade_closed(event.net_realized_pnl)
            self._record_kill_transition(was_active, event.close_time)
            self._position_correlations.pop(event.position_id, None)
        return events

    def _record_account(self, account: AccountInfo, current_time: datetime) -> None:
        self._append(
            AuditEventType.ACCOUNT_OBSERVED,
            occurred_at=current_time,
            component=AuditComponent.PAPER_BROKER,
            payload={
                "balance": account.balance,
                "equity": account.equity,
                "margin_used": account.margin_used,
                "margin_available": account.margin_available,
                "open_position_count": len(account.open_positions),
            },
        )
        for position in account.open_positions:
            correlation = self._position_correlations.get(
                position.position_id,
                EventCorrelation(position_id=position.position_id),
            )
            self._append(
                AuditEventType.POSITION_MARKED,
                occurred_at=current_time,
                component=AuditComponent.PAPER_BROKER,
                correlation=correlation,
                payload={
                    "symbol": position.symbol,
                    "side": position.side,
                    "size": position.size,
                    "entry_price": position.entry_price,
                    "unrealized_pnl": position.unrealized_pnl,
                },
            )

    def _append(
        self,
        event_type: AuditEventType,
        *,
        occurred_at: datetime,
        component: AuditComponent,
        payload: dict[str, object],
        correlation: EventCorrelation | None = None,
    ) -> None:
        try:
            self.event_ledger.append(
                event_type,
                occurred_at=occurred_at,
                component=component,
                correlation=correlation,
                payload=payload,
            )
        except Exception as exc:
            self._audit_failed = True
            raise AuditLedgerError("paper-session audit append failed") from exc

    def _record_reconciliation_failure(
        self, client_id: str, current_time: datetime
    ) -> None:
        if client_id in self._reported_reconciliation_failures:
            return
        self._reported_reconciliation_failures.add(client_id)
        was_active = self.risk_engine.kill_switch_active
        self.risk_engine.trigger_kill_switch(
            KillSwitchReason.POSITION_RECONCILIATION_FAILED
        )
        self._append(
            AuditEventType.RECONCILIATION_FAILED,
            occurred_at=current_time,
            component=AuditComponent.PAPER_SESSION,
            correlation=EventCorrelation(
                signal_id=client_id, client_order_id=client_id
            ),
            payload={"reason": "position_reconciliation_failed"},
        )
        self._record_kill_transition(was_active, current_time)

    def _record_kill_transition(
        self, was_active: bool, current_time: datetime
    ) -> None:
        if was_active or not self.risk_engine.kill_switch_active:
            return
        self._append(
            AuditEventType.KILL_SWITCH_TRIGGERED,
            occurred_at=current_time,
            component=AuditComponent.RISK_ENGINE,
            payload={"reason": self.risk_engine.kill_switch_reason},
        )

    def _record_runtime_failure(
        self, reason: str, current_time: datetime | None = None
    ) -> None:
        try:
            self._append(
                AuditEventType.RUNTIME_FAILURE,
                occurred_at=current_time or self.event_ledger.now(),
                component=AuditComponent.PAPER_SESSION,
                payload={"reason": reason},
            )
        except AuditLedgerError:
            pass


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) and converted > 0 else None


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except Exception:
        return None


def _signal_correlation(signal: SignalEvent) -> EventCorrelation:
    return EventCorrelation(signal_id=deterministic_signal_id(signal))


def _signal_payload(signal: SignalEvent) -> dict[str, object]:
    return {
        "setup_name": signal.setup_name,
        "symbol": signal.symbol,
        "timeframe": signal.timeframe,
        "side": signal.side,
        "signal_time": signal.signal_time,
        "signal_bar_index": signal.signal_bar_index,
    }


def _cycle_failure(
    reason: str,
    message: str,
    current_time: datetime | None = None,
    tick: Tick | None = None,
    signals: tuple[SignalEvent, ...] = (),
) -> PaperCycleResult:
    return PaperCycleResult(
        CycleKind.FAILED,
        current_time,
        tick,
        signals,
        reason=reason,
        message=message,
    )


def _reconciliation_failure(status_result: ExecutionResult) -> ExecutionResult:
    return ExecutionResult(
        kind=ExecutionResultKind.STATUS_FAILURE,
        reason="position_reconciliation_failed",
        message="filled paper order was not exactly correlated to an account position",
        record=status_result.record,
    )
