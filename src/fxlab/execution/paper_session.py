"""Small deterministic session runner for historical paper-trading replay."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

import pandas as pd

if TYPE_CHECKING:
    from .durable_event_store import SQLiteEventStore
    from .monitoring import MonitoringSnapshot

from ..data.provider import (
    ProvenanceQuality,
    ProviderFailure,
    ProviderFailureCategory,
)
from ..data.schema import OHLCV, timeframe_to_timedelta
from ..risk.engine import KillSwitchReason, RiskEngine
from .broker import AccountInfo, OrderStatus, Tick
from .broker_capabilities import (
    CURRENT_PAPER_SESSION_REQUIREMENTS,
    BrokerDescriptor,
    BrokerEnvironment,
    inspect_broker_capabilities,
)
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
from .runtime_control import (
    RuntimeController,
    RuntimeControlReason,
    RuntimeControlResult,
    RuntimeState,
    RuntimeStatus,
)
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
    provider_id: str = "historical-replay"
    provider_version: str = "1"
    normalization_version: str = "1"
    provenance_quality: ProvenanceQuality = ProvenanceQuality.SYNTHETIC
    mapping_identity: str = "canonical-v1"
    _events: tuple[Tick, ...] = field(init=False, repr=False)
    _cursor: int = field(default=0, init=False)
    _stopped: bool = field(default=False, init=False)
    _dataset_fingerprint: str = field(init=False, repr=False)
    _dataset_id: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "provider_version",
            "normalization_version",
            "mapping_identity",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.provenance_quality, ProvenanceQuality):
            raise ValueError("provenance_quality must be a ProvenanceQuality")
        delta = timeframe_to_timedelta(self.timeframe)
        digest = hashlib.sha256(b"fxlab-replay-dataset-v1\0")
        digest.update(self.timeframe.encode("utf-8") + b"\0")
        events: list[Tick] = []
        for raw_symbol, raw_bars in sorted(self.bars_by_symbol.items()):
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
            digest.update(symbol.encode("utf-8") + b"\0")
            digest.update(struct.pack("!Q", len(bars)))
            for timestamp, values in zip(bars.index, bars[OHLCV].to_numpy(), strict=True):
                digest.update(struct.pack("!q", int(timestamp.value)))
                for value in values:
                    digest.update(struct.pack("!d", float(value)))
            for ts_open, row in bars.sort_index().iterrows():
                close = _positive_float(row["close"])
                if close is None:
                    raise ValueError("replay close prices must be finite and positive")
                timestamp = (ts_open + delta).to_pydatetime()
                events.append(Tick(symbol, timestamp, close, close, close))
        events.sort(key=lambda tick: (tick.timestamp.astimezone(UTC), tick.symbol))
        self._events = tuple(events)
        self._dataset_fingerprint = digest.hexdigest()
        identity = {
            "format": 1,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "normalization_version": self.normalization_version,
            "content_hash": self._dataset_fingerprint,
            "timeframe": self.timeframe,
        }
        self._dataset_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._events)

    @property
    def dataset_fingerprint(self) -> str:
        return self._dataset_fingerprint

    @property
    def dataset_id(self) -> str:
        return self._dataset_id

    def provider_compatibility_snapshot(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "capabilities": ["deterministic_replay", "point_in_time", "replay_events"],
            "mapping_identity": self.mapping_identity,
            "normalization_version": self.normalization_version,
            "freshness_policy": "replay-clock-v1",
            "fallback_policy": "none",
            "dataset_id": self.dataset_id,
            "content_hash": self.dataset_fingerprint,
        }

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

    def snapshot_state(self) -> dict[str, object]:
        last = self._events[self._cursor - 1].timestamp if self._cursor else None
        return {
            "cursor": self._cursor,
            "last_consumed_timestamp": last.astimezone(UTC).isoformat() if last else None,
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not isinstance(state, Mapping):
            raise ValueError("replay state must be a mapping")
        cursor, timestamp = state.get("cursor"), state.get("last_consumed_timestamp")
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not 0 <= cursor <= len(self._events)
        ):
            raise ValueError("invalid replay cursor")
        expected = self._events[cursor - 1].timestamp.astimezone(UTC) if cursor else None
        parsed = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
        if parsed is not None:
            parsed = parsed.astimezone(UTC)
        if parsed != expected:
            raise ValueError("replay cursor/timestamp mismatch")
        self._cursor = cursor
        self._stopped = False


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
    runtime_id: str = "runtime-1"

    _started: bool = field(default=False, init=False)
    _stopped: bool = field(default=False, init=False)
    _audit_failed: bool = field(default=False, init=False)
    _recovery_required: bool = field(default=False, init=False)
    _capability_failed: bool = field(default=False, init=False)
    _broker_descriptor_fingerprint: str | None = field(default=None, init=False)
    _tracked_orders: set[str] = field(default_factory=set, init=False)
    _position_correlations: dict[str, EventCorrelation] = field(
        default_factory=dict, init=False
    )
    _reported_reconciliation_failures: set[str] = field(
        default_factory=set, init=False
    )
    _runtime_controller: RuntimeController = field(
        default_factory=RuntimeController, init=False, repr=False
    )
    _last_market_time: datetime | None = field(default=None, init=False)
    _resume_data_ready: bool = field(default=False, init=False)
    _cycle_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.event_ledger, EventLedger):
            raise ValueError("event_ledger must be an EventLedger")
        if self.order_manager.event_ledger is None:
            self.order_manager.event_ledger = self.event_ledger
        elif self.order_manager.event_ledger is not self.event_ledger:
            raise ValueError("session and order manager must share one EventLedger")
        if not isinstance(self.runtime_id, str) or not self.runtime_id.strip():
            raise ValueError("runtime_id must be a non-empty string")

    def start(self) -> None:
        with self._cycle_lock:
            self._start_unlocked()

    def _start_unlocked(self) -> None:
        if self._started:
            return
        if self._recovery_required:
            raise RuntimeError("reconciliation is required before session start")
        if self._stopped:
            raise RuntimeError("a stopped paper session cannot be restarted")
        control_state = self.runtime_status().state
        if control_state is RuntimeState.STOPPING:
            raise RuntimeError("a stopping paper session cannot be started")
        if control_state is RuntimeState.FAILED:
            raise RuntimeError("a failed paper session cannot be started")
        capability_check = inspect_broker_capabilities(
            self.broker,
            CURRENT_PAPER_SESSION_REQUIREMENTS,
            environment=BrokerEnvironment.PAPER,
            deterministic=True,
            require_hedging=True,
        )
        descriptor = capability_check.descriptor
        changed = (
            descriptor is not None
            and self._broker_descriptor_fingerprint is not None
            and descriptor.fingerprint != self._broker_descriptor_fingerprint
        )
        if not capability_check.compatible or changed:
            self._capability_failed = True
            self._record_capability_rejection(
                capability_check.reason if not changed else "broker_descriptor_changed",
                capability_check,
                self.event_ledger.now(),
            )
            raise RuntimeError("broker capabilities are incompatible with paper session")
        assert isinstance(descriptor, BrokerDescriptor)
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
                payload={"runtime_id": self.runtime_id},
            )
            if self._broker_descriptor_fingerprint is None:
                self._append(
                    AuditEventType.BROKER_CAPABILITIES_BOUND,
                    occurred_at=self.event_ledger.now(),
                    component=AuditComponent.BROKER_ADAPTER,
                    payload=descriptor.compatibility_snapshot(),
                )
                self._broker_descriptor_fingerprint = descriptor.fingerprint
            self._append(
                AuditEventType.DATA_PROVIDER_SELECTED,
                occurred_at=self.event_ledger.now(),
                component=AuditComponent.MARKET_DATA_PROVIDER,
                payload={
                    "provider_id": self.replay.provider_id,
                    "provider_version": self.replay.provider_version,
                    "normalization_version": self.replay.normalization_version,
                    "fallback_policy": "none",
                },
            )
            self._append(
                AuditEventType.DATASET_BOUND,
                occurred_at=self.event_ledger.now(),
                component=AuditComponent.MARKET_DATA_PROVIDER,
                payload={
                    "provider_id": self.replay.provider_id,
                    "provider_version": self.replay.provider_version,
                    "dataset_id": self.replay.dataset_id,
                    "content_hash": self.replay.dataset_fingerprint,
                    "normalization_version": self.replay.normalization_version,
                    "symbols": tuple(sorted(self.replay.bars_by_symbol)),
                    "timeframe": self.replay.timeframe,
                    "provenance_quality": self.replay.provenance_quality.value,
                },
            )
        except AuditLedgerError:
            self.broker.disconnect()
            self._audit_failed = True
            raise
        if self._runtime_controller.status().state is RuntimeState.STOPPED:
            self._append_runtime_transition(
                RuntimeState.STOPPED,
                RuntimeState.RUNNING,
                RuntimeControlReason.SESSION_STOPPED,
                operator_requested=False,
                occurred_at=self.event_ledger.now(),
            )
            transition = self._runtime_controller.start()
            if not transition.accepted:
                self.broker.disconnect()
                raise RuntimeError("runtime controller rejected session start")
        self._started = True

    def poll_once(self, *, until: datetime | None = None) -> PaperCycleResult:
        """Advance one replay cycle, failing closed on any audit-integrity loss."""
        with self._cycle_lock:
            try:
                return self._poll_once(until=until)
            except AuditLedgerError:
                self._audit_failed = True
                self._runtime_controller.fail(
                    RuntimeControlReason.AUDIT_INTEGRITY_FAILED
                )
                self.risk_engine.trigger_kill_switch(
                    KillSwitchReason.POSITION_RECONCILIATION_FAILED
                )
                return _cycle_failure(
                    "audit_unavailable", "required runtime transition could not be audited"
                )

    def _poll_once(self, *, until: datetime | None = None) -> PaperCycleResult:
        if not self._started or self._stopped:
            return _cycle_failure("session_not_running", "paper session is not running")
        initial_status = self.runtime_status()
        if initial_status.state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
            return _cycle_failure(initial_status.reason.value, "paper session is stopping")
        if self._audit_failed or self.order_manager.audit_failed or self._recovery_required:
            return _cycle_failure(
                "audit_unavailable", "audit integrity is unavailable; execution is disabled"
            )
        if not self._verify_broker_descriptor():
            self._transition_failed(RuntimeControlReason.BROKER_INCOMPATIBLE)
            return _cycle_failure(
                "broker_capability_unsupported",
                "broker capability declaration changed; execution is disabled",
            )
        try:
            tick = self.replay.next_tick(until=until)
        except Exception:
            self._record_runtime_failure("replay_failure")
            self._transition_failed(RuntimeControlReason.RUNTIME_FAILED)
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
                    "provider_id": self.replay.provider_id,
                    "dataset_id": self.replay.dataset_id,
                    "normalization_version": self.replay.normalization_version,
                },
            )
            self.market_data.on_tick(tick)
            self._last_market_time = current_time
            self._resume_data_ready = True
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
            self._transition_paused(
                RuntimeControlReason.BROKER_UNAVAILABLE, current_time
            )
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

        gate = self.runtime_status()
        if not gate.execution_enabled:
            maintenance = tuple(self._refresh_and_reconcile(current_time))
            return PaperCycleResult(
                CycleKind.PROCESSED if maintenance or closes else CycleKind.NO_SIGNAL,
                current_time,
                tick,
                executions=maintenance,
                closes=closes,
                reason=gate.reason.value,
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

        eligible_signals: list[SignalEvent] = []
        for signal in signals:
            if self._runtime_controller.signal_is_eligible(signal.signal_time):
                eligible_signals.append(signal)
                continue
            self._append(
                AuditEventType.SIGNAL_DECLINED,
                occurred_at=current_time,
                component=AuditComponent.PAPER_SESSION,
                correlation=_signal_correlation(signal),
                payload={"reason": "runtime_entry_watermark"},
            )
        signals = tuple(eligible_signals)

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
            if result.kind is ExecutionResultKind.INDETERMINATE:
                self.require_reconciliation()

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
        with self._cycle_lock:
            return self._close_position_unlocked(position_id)

    def _close_position_unlocked(self, position_id: str) -> PositionClose | None:
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

    def runtime_status(self) -> RuntimeStatus:
        failed_reason = None
        if self._audit_failed or self.order_manager.audit_failed:
            failed_reason = RuntimeControlReason.AUDIT_INTEGRITY_FAILED
        elif self._capability_failed:
            failed_reason = RuntimeControlReason.BROKER_INCOMPATIBLE
        return self._runtime_controller.status(
            reconciliation_required=self._recovery_required,
            failed_reason=failed_reason,
            kill_switch_active=self.risk_engine.kill_switch_active,
            emergency_stop=(
                self.risk_engine.kill_switch_reason is KillSwitchReason.MANUAL
            ),
        )

    def pause(self) -> RuntimeControlResult:
        with self._cycle_lock:
            return self._pause_unlocked()

    def _pause_unlocked(self) -> RuntimeControlResult:
        return self._transition_paused(
            RuntimeControlReason.OPERATOR_PAUSED,
            self._last_market_time or self.event_ledger.now(),
            operator_requested=True,
        )

    def resume(self) -> RuntimeControlResult:
        with self._cycle_lock:
            return self._resume_unlocked()

    def _resume_unlocked(self) -> RuntimeControlResult:
        before = self.runtime_status()
        if self._last_market_time is None or not self._resume_data_ready:
            return RuntimeControlResult(
                False, False, before.state, before.state, before.reason
            )
        failure = None
        if self._audit_failed or self.order_manager.audit_failed:
            failure = RuntimeControlReason.AUDIT_INTEGRITY_FAILED
        elif self._capability_failed:
            failure = RuntimeControlReason.BROKER_INCOMPATIBLE
        if (
            before.state is not RuntimeState.PAUSED
            or self._recovery_required
            or failure is not None
            or self.risk_engine.kill_switch_active
            or self._has_unresolved_execution()
        ):
            return RuntimeControlResult(
                before.state is RuntimeState.RUNNING,
                False,
                before.state,
                before.state,
                before.reason,
            )
        if not self._verify_broker_descriptor():
            self._transition_failed(RuntimeControlReason.BROKER_INCOMPATIBLE)
            after = self.runtime_status()
            return RuntimeControlResult(
                False, True, before.state, after.state, after.reason
            )
        self._append_runtime_transition(
            RuntimeState.PAUSED,
            RuntimeState.RUNNING,
            RuntimeControlReason.OPERATOR_PAUSED,
            operator_requested=True,
            occurred_at=self.event_ledger.now(),
        )
        return self._runtime_controller.resume(self._last_market_time)

    def emergency_stop(self) -> RuntimeControlResult:
        with self._cycle_lock:
            return self._emergency_stop_unlocked()

    def _emergency_stop_unlocked(self) -> RuntimeControlResult:
        before = self.runtime_status()
        was_active = self.risk_engine.kill_switch_active
        self.risk_engine.trigger_kill_switch(KillSwitchReason.MANUAL)
        after = self.runtime_status()
        if not was_active:
            self._record_kill_transition(False, self.event_ledger.now())
        return RuntimeControlResult(
            True,
            before.state is not after.state,
            before.state,
            after.state,
            after.reason,
        )

    def handle_provider_failure(
        self, failure: ProviderFailure, *, occurred_at: datetime | None = None
    ) -> RuntimeControlResult:
        """Apply Phase 11 failure categories without retrying or exposing data."""
        if not isinstance(failure, ProviderFailure):
            raise ValueError("failure must be a ProviderFailure")
        with self._cycle_lock:
            timestamp = occurred_at or self.event_ledger.now()
            temporary = failure.category in {
                ProviderFailureCategory.STALE_DATA,
                ProviderFailureCategory.TRANSIENT,
                ProviderFailureCategory.RATE_LIMIT,
                ProviderFailureCategory.NO_DATA,
            }
            if temporary:
                reason = (
                    RuntimeControlReason.DATA_STALE
                    if failure.category is ProviderFailureCategory.STALE_DATA
                    else RuntimeControlReason.DATA_UNAVAILABLE
                )
                result = self._transition_paused(reason, timestamp)
            else:
                reason = (
                    RuntimeControlReason.DATA_INVALID
                    if failure.category
                    in {
                        ProviderFailureCategory.INVALID_DATA,
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                    }
                    else RuntimeControlReason.RUNTIME_FAILED
                )
                result = self._transition_failed(reason)
            self._append(
                AuditEventType.DATA_STALE
                if failure.category is ProviderFailureCategory.STALE_DATA
                else AuditEventType.DATA_PROVIDER_FAILED,
                occurred_at=timestamp,
                component=AuditComponent.MARKET_DATA_PROVIDER,
                payload={
                    "category": failure.category.value,
                    "reason": failure.reason,
                    "provider_id": failure.provider_id,
                    "retryable": failure.retryable,
                },
            )
            return result

    def request_stop(self) -> RuntimeControlResult:
        with self._cycle_lock:
            return self._request_stop_unlocked()

    def _request_stop_unlocked(self) -> RuntimeControlResult:
        result = self._runtime_controller.request_stop()
        if result.changed:
            self._audit_controller_result(result, operator_requested=True)
        return result

    def apply_operator_control(
        self,
        action: str,
        *,
        actor_id: str,
        request_id: str,
    ) -> RuntimeControlResult:
        """Apply one allow-listed authenticated mutation at a cycle boundary."""
        if action not in {"pause", "resume", "emergency_stop", "stop"}:
            raise ValueError("unsupported operator control action")
        if not isinstance(actor_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", actor_id
        ):
            raise ValueError("actor_id must be a safe identifier")
        try:
            parsed_request_id = uuid.UUID(request_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("request_id must be a canonical UUID4") from exc
        if parsed_request_id.version != 4 or str(parsed_request_id) != request_id:
            raise ValueError("request_id must be a canonical UUID4")
        with self._cycle_lock:
            result = {
                "pause": self._pause_unlocked,
                "resume": self._resume_unlocked,
                "emergency_stop": self._emergency_stop_unlocked,
                "stop": self._request_stop_unlocked,
            }[action]()
            try:
                self._append(
                    AuditEventType.OPERATOR_CONTROL_ACTION,
                    occurred_at=self.event_ledger.now(),
                    component=AuditComponent.CONTROL_SERVICE,
                    payload={
                        "actor_id": actor_id,
                        "request_id": request_id,
                        "action": action,
                        "accepted": result.accepted,
                        "changed": result.changed,
                        "previous_state": result.previous_state.value,
                        "current_state": result.current_state.value,
                        "reason": result.reason.value if result.reason else None,
                    },
                )
            except AuditLedgerError:
                self._audit_failed = True
                self._runtime_controller.fail(RuntimeControlReason.AUDIT_INTEGRITY_FAILED)
                raise
            return result

    def audit_rejected_operator_control(
        self,
        action: str,
        *,
        actor_id: str,
        request_id: str,
        reason: str,
    ) -> None:
        """Attribute a valid authenticated mutation rejected above runtime control."""
        if action not in {"pause", "resume", "emergency_stop", "stop"}:
            raise ValueError("unsupported operator control action")
        if not isinstance(actor_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", actor_id
        ):
            raise ValueError("actor_id must be a safe identifier")
        try:
            parsed_request_id = uuid.UUID(request_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("request_id must be a canonical UUID4") from exc
        if parsed_request_id.version != 4 or str(parsed_request_id) != request_id:
            raise ValueError("request_id must be a canonical UUID4")
        if not isinstance(reason, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,127}", reason
        ):
            raise ValueError("reason must be a stable identifier")
        with self._cycle_lock:
            state = self.runtime_status().state
            try:
                self._append(
                    AuditEventType.OPERATOR_CONTROL_ACTION,
                    occurred_at=self.event_ledger.now(),
                    component=AuditComponent.CONTROL_SERVICE,
                    payload={
                        "actor_id": actor_id,
                        "request_id": request_id,
                        "action": action,
                        "accepted": False,
                        "changed": False,
                        "previous_state": state.value,
                        "current_state": state.value,
                        "reason": reason,
                    },
                )
            except AuditLedgerError:
                self._audit_failed = True
                self._runtime_controller.fail(RuntimeControlReason.AUDIT_INTEGRITY_FAILED)
                raise

    def complete_stop(
        self,
        *,
        checkpoint_store: SQLiteEventStore | None = None,
        software_version: str | None = None,
        execution_policy_id: str | None = None,
    ) -> RuntimeControlResult:
        with self._cycle_lock:
            before = self.runtime_status()
            if before.state is RuntimeState.STOPPED:
                return RuntimeControlResult(
                    True, False, before.state, before.state, before.reason
                )
            if before.state is not RuntimeState.STOPPING:
                return RuntimeControlResult(
                    False, False, before.state, before.state, before.reason
                )
            now = self.event_ledger.now()
            if not self._audit_failed and not self.order_manager.audit_failed:
                self._drain_close_events(now)
                self._refresh_and_reconcile(now)
            if checkpoint_store is not None:
                if software_version is None or execution_policy_id is None:
                    raise ValueError(
                        "durable stop requires software_version and execution_policy_id"
                    )
                from .recovery import create_checkpoint

                create_checkpoint(
                    self,
                    checkpoint_store,
                    software_version=software_version,
                    execution_policy_id=execution_policy_id,
                    created_at=now,
                )
            self.replay.stop()
            if self.broker.is_connected():
                self.broker.disconnect()
            result = self._runtime_controller.complete_stop()
            self._stopped = True
            if not self._audit_failed:
                try:
                    self._audit_controller_result(result, operator_requested=True)
                    self._append(
                        AuditEventType.SESSION_STOPPED,
                        occurred_at=now,
                        component=AuditComponent.PAPER_SESSION,
                        payload={},
                    )
                except AuditLedgerError:
                    self._audit_failed = True
            if checkpoint_store is not None and not self._audit_failed:
                from .recovery import create_checkpoint

                create_checkpoint(
                    self,
                    checkpoint_store,
                    software_version=software_version,
                    execution_policy_id=execution_policy_id,
                    created_at=now,
                )
            return result

    def stop(self) -> None:
        try:
            self.request_stop()
        except AuditLedgerError:
            # Audit loss must block execution, but it must not prevent disconnect.
            pass
        self.complete_stop()

    def account_snapshot(self) -> AccountInfo:
        return self.broker.get_account_info()

    def positions_snapshot(self) -> tuple[object, ...]:
        return tuple(self.broker.get_account_info().open_positions)

    def orders_snapshot(self) -> Mapping[str, object]:
        return MappingProxyType(deepcopy(self.order_manager.snapshot_state()))

    def risk_state_snapshot(self) -> Mapping[str, object]:
        return MappingProxyType(deepcopy(self.risk_engine.snapshot_state()))

    def monitoring_snapshot(self) -> MonitoringSnapshot:
        """Capture one immutable live view at a serialized cycle boundary."""
        from .monitoring import project_live_session

        with self._cycle_lock:
            return project_live_session(self)

    def create_safe_checkpoint(
        self,
        store: SQLiteEventStore,
        *,
        software_version: str,
        execution_policy_id: str,
    ) -> object:
        """Create a checkpoint at the same serialized boundary as runtime controls."""
        from .recovery import create_checkpoint

        with self._cycle_lock:
            return create_checkpoint(
                self,
                store,
                software_version=software_version,
                execution_policy_id=execution_policy_id,
            )

    def activate_recovered_maintenance(self) -> None:
        """Reconnect a recovered blocked PaperBroker without enabling execution."""
        with self._cycle_lock:
            if self._started:
                return
            status = self.runtime_status()
            if self._stopped or status.state in {
                RuntimeState.RUNNING,
                RuntimeState.RECONCILIATION_REQUIRED,
                RuntimeState.FAILED,
                RuntimeState.STOPPING,
                RuntimeState.STOPPED,
            }:
                raise RuntimeError("recovered runtime is not maintenance-attachable")
            if self._broker_descriptor_fingerprint is None or not self._verify_broker_descriptor():
                raise RuntimeError("recovered broker capabilities are incompatible")
            self.broker.connect()
            try:
                self.market_data.start()
                self._append(
                    AuditEventType.SESSION_STARTED,
                    occurred_at=self.event_ledger.now(),
                    component=AuditComponent.PAPER_SESSION,
                    payload={"runtime_id": self.runtime_id, "recovered": True},
                )
            except Exception:
                self.broker.disconnect()
                raise
            self._resume_data_ready = False
            self._started = True

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    def snapshot_state(self) -> dict[str, object]:
        return {
            "audit_failed": self._audit_failed,
            "recovery_required": self._recovery_required,
            "capability_failed": self._capability_failed,
            "broker_descriptor_fingerprint": self._broker_descriptor_fingerprint,
            "tracked_orders": sorted(self._tracked_orders),
            "position_correlations": [
                {
                    "position_id": key,
                    "signal_id": item.signal_id,
                    "client_order_id": item.client_order_id,
                    "broker_order_id": item.broker_order_id,
                    "close_order_id": item.close_order_id,
                }
                for key, item in sorted(self._position_correlations.items())
            ],
            "reported_reconciliation_failures": sorted(
                self._reported_reconciliation_failures
            ),
            "runtime_control": self._runtime_controller.snapshot_state(),
            "last_market_time": (
                self._last_market_time.astimezone(UTC).isoformat()
                if self._last_market_time is not None
                else None
            ),
            "resume_data_ready": self._resume_data_ready,
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        parsed = _parse_session_state(state)
        (
            self._audit_failed,
            self._recovery_required,
            self._capability_failed,
            self._broker_descriptor_fingerprint,
            self._tracked_orders,
            self._position_correlations,
            self._reported_reconciliation_failures,
            runtime_state,
            self._last_market_time,
            self._resume_data_ready,
        ) = parsed
        self._runtime_controller.restore_state(runtime_state)
        self._started = False
        self._stopped = bool(runtime_state["terminally_stopped"])

    def _verify_broker_descriptor(self) -> bool:
        if self._capability_failed:
            return False
        check = inspect_broker_capabilities(
            self.broker,
            CURRENT_PAPER_SESSION_REQUIREMENTS,
            environment=BrokerEnvironment.PAPER,
            deterministic=True,
            require_hedging=True,
        )
        descriptor = check.descriptor
        valid = (
            check.compatible
            and descriptor is not None
            and descriptor.fingerprint == self._broker_descriptor_fingerprint
        )
        if valid:
            return True
        self._capability_failed = True
        reason = check.reason if not check.compatible else "broker_descriptor_changed"
        self._record_capability_rejection(reason, check, self.event_ledger.now())
        return False

    def _record_capability_rejection(
        self, reason: str, check: object, occurred_at: datetime
    ) -> None:
        descriptor = getattr(check, "descriptor", None)
        required = getattr(check, "required", ())
        missing = getattr(check, "missing", ())
        self._append(
            AuditEventType.BROKER_CAPABILITY_REJECTED,
            occurred_at=occurred_at,
            component=AuditComponent.BROKER_ADAPTER,
            payload={
                "reason": reason,
                "required_capabilities": tuple(item.value for item in required),
                "missing_capabilities": tuple(item.value for item in missing),
                "broker_id": descriptor.broker_id if descriptor else None,
            },
        )

    def require_reconciliation(self) -> None:
        self._recovery_required = True
        self.risk_engine.trigger_kill_switch(
            KillSwitchReason.POSITION_RECONCILIATION_FAILED
        )

    def _transition_paused(
        self,
        reason: RuntimeControlReason,
        occurred_at: datetime,
        *,
        operator_requested: bool = False,
    ) -> RuntimeControlResult:
        result = self._runtime_controller.pause(occurred_at, reason=reason)
        if operator_requested:
            self._resume_data_ready = self._last_market_time is not None
        else:
            self._resume_data_ready = False
        if result.changed:
            try:
                self._audit_controller_result(
                    result,
                    operator_requested=operator_requested,
                    occurred_at=occurred_at,
                )
            except AuditLedgerError:
                self._runtime_controller.fail(
                    RuntimeControlReason.AUDIT_INTEGRITY_FAILED
                )
                raise
        return result

    def _has_unresolved_execution(self) -> bool:
        if self._tracked_orders:
            return True
        records = self.order_manager.snapshot_state().get("records")
        return not isinstance(records, list) or any(
            not isinstance(record, Mapping) or record.get("broker_order_id") is None
            for record in records
        )

    def _transition_failed(self, reason: RuntimeControlReason) -> RuntimeControlResult:
        result = self._runtime_controller.fail(reason)
        if result.changed and not self._audit_failed:
            self._audit_controller_result(result, operator_requested=False)
        return result

    def _audit_controller_result(
        self,
        result: RuntimeControlResult,
        *,
        operator_requested: bool,
        occurred_at: datetime | None = None,
    ) -> None:
        if not result.changed:
            return
        self._append_runtime_transition(
            result.previous_state,
            result.current_state,
            result.reason,
            operator_requested=operator_requested,
            occurred_at=occurred_at or self.event_ledger.now(),
        )

    def _append_runtime_transition(
        self,
        previous: RuntimeState,
        current: RuntimeState,
        reason: RuntimeControlReason,
        *,
        operator_requested: bool,
        occurred_at: datetime,
    ) -> None:
        self._append(
            AuditEventType.RUNTIME_STATE_CHANGED,
            occurred_at=occurred_at,
            component=AuditComponent.PAPER_SESSION,
            payload={
                "previous_state": previous.value,
                "current_state": current.value,
                "reason": reason.value,
                "operator_requested": operator_requested,
            },
        )

    def _refresh_and_reconcile(self, current_time: datetime) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        for client_id in tuple(sorted(self._tracked_orders)):
            result = self.order_manager.refresh_order_status(
                client_id, current_time=current_time
            )
            results.append(result)
            if result.kind is ExecutionResultKind.STATUS_FAILURE:
                self._transition_paused(
                    RuntimeControlReason.BROKER_UNAVAILABLE, current_time
                )
                continue
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
            reflected = next(
                (
                    position
                    for position in account.open_positions
                    if position.position_id == correlation.position_id
                ),
                None,
            )
            if reflected is not None:
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
                    payload={
                        "status": record.status,
                        "currency": account.currency,
                        "actual_entry_price": reflected.entry_price,
                    },
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
                    "currency": event.account_currency,
                    "valuation_id": event.valuation_id,
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
        economic = self.broker.economic_monitoring_snapshot()
        self._append(
            AuditEventType.ACCOUNT_OBSERVED,
            occurred_at=current_time,
            component=AuditComponent.PAPER_BROKER,
            payload={
                "balance": account.balance,
                "equity": account.equity,
                "margin_used": account.margin_used,
                "margin_available": account.margin_available,
                "currency": account.currency,
                "margin_model_identity": economic["margin_model_identity"],
                "margin_quality": economic["margin_quality"],
                "open_position_count": len(account.open_positions),
            },
        )
        for position in account.open_positions:
            position_tick = self.broker.get_latest_tick(position.symbol)
            valuation_time = (
                position_tick.timestamp if position_tick is not None else current_time
            )
            valuation = self.broker.pip_valuation(
                position.symbol,
                account.currency,
                valuation_time,
            )
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
                    "currency": account.currency,
                    "valuation_id": valuation.valuation_id,
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
        self._recovery_required = True
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
        prior = self._runtime_controller.status().state
        current = self.runtime_status()
        if prior is not current.state:
            self._append_runtime_transition(
                prior,
                current.state,
                current.reason,
                operator_requested=current.reason
                is RuntimeControlReason.EMERGENCY_STOPPED,
                occurred_at=current_time,
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


def _parse_session_state(state: Mapping[str, object]) -> tuple:
    if not isinstance(state, Mapping):
        raise ValueError("session state must be a mapping")
    audit, recovery = state.get("audit_failed"), state.get("recovery_required")
    capability_failed = state.get("capability_failed")
    descriptor_fingerprint = state.get("broker_descriptor_fingerprint")
    tracked, failures = state.get("tracked_orders"), state.get(
        "reported_reconciliation_failures"
    )
    raw_correlations = state.get("position_correlations")
    runtime_state = state.get("runtime_control")
    raw_last_market_time = state.get("last_market_time")
    resume_data_ready = state.get("resume_data_ready")
    if (
        not isinstance(audit, bool)
        or not isinstance(recovery, bool)
        or not isinstance(capability_failed, bool)
    ):
        raise ValueError("invalid session flags")
    if descriptor_fingerprint is not None and (
        not isinstance(descriptor_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", descriptor_fingerprint)
    ):
        raise ValueError("invalid broker descriptor fingerprint")
    if not isinstance(tracked, list) or not isinstance(failures, list):
        raise ValueError("invalid session order collections")
    if any(not isinstance(item, str) or not item for item in tracked + failures):
        raise ValueError("invalid session order identity")
    if not isinstance(raw_correlations, list):
        raise ValueError("invalid session correlations")
    correlations: dict[str, EventCorrelation] = {}
    for raw in raw_correlations:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid session correlation")
        position_id = raw.get("position_id")
        if not isinstance(position_id, str) or not position_id or position_id in correlations:
            raise ValueError("invalid session position identity")
        correlations[position_id] = EventCorrelation(
            signal_id=raw.get("signal_id"),
            client_order_id=raw.get("client_order_id"),
            broker_order_id=raw.get("broker_order_id"),
            position_id=position_id,
            close_order_id=raw.get("close_order_id"),
        )
    if not isinstance(runtime_state, Mapping):
        raise ValueError("invalid runtime-control state")
    validated_runtime = RuntimeController()
    validated_runtime.restore_state(runtime_state)
    last_market_time = None
    if raw_last_market_time is not None:
        if not isinstance(raw_last_market_time, str):
            raise ValueError("invalid last market time")
        last_market_time = _aware_utc(datetime.fromisoformat(raw_last_market_time))
        if last_market_time is None:
            raise ValueError("invalid last market time")
    if not isinstance(resume_data_ready, bool):
        raise ValueError("invalid resume data state")
    return (
        audit,
        recovery,
        capability_failed,
        descriptor_fingerprint,
        set(tracked),
        correlations,
        set(failures),
        validated_runtime.snapshot_state(),
        last_market_time,
        resume_data_ready,
    )


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
