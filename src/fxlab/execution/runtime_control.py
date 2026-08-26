"""Small thread-safe runtime control state for paper-trading sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock


class RuntimeState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    KILL_SWITCHED = "kill_switched"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class RuntimeControlReason(StrEnum):
    OPERATOR_PAUSED = "operator_paused"
    EMERGENCY_STOPPED = "emergency_stopped"
    RISK_KILL_SWITCH = "risk_kill_switch"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    AUDIT_INTEGRITY_FAILED = "audit_integrity_failed"
    BROKER_INCOMPATIBLE = "broker_incompatible"
    BROKER_UNAVAILABLE = "broker_unavailable"
    DATA_STALE = "data_stale"
    DATA_UNAVAILABLE = "data_unavailable"
    DATA_INVALID = "data_invalid"
    RUNTIME_FAILED = "runtime_failed"
    SHUTDOWN_IN_PROGRESS = "shutdown_in_progress"
    SESSION_STOPPED = "session_stopped"


@dataclass(frozen=True)
class RuntimeStatus:
    state: RuntimeState
    reason: RuntimeControlReason | None
    execution_enabled: bool
    market_maintenance_enabled: bool
    entry_enable_watermark: datetime | None
    started: bool
    stopped: bool
    generation: int


@dataclass(frozen=True)
class RuntimeControlResult:
    accepted: bool
    changed: bool
    previous_state: RuntimeState
    current_state: RuntimeState
    reason: RuntimeControlReason | None


@dataclass
class RuntimeController:
    """Own operator/lifecycle state while projecting external hard blockers."""

    _state: RuntimeState = field(default=RuntimeState.STOPPED, init=False)
    _reason: RuntimeControlReason | None = field(
        default=RuntimeControlReason.SESSION_STOPPED, init=False
    )
    _entry_enable_watermark: datetime | None = field(default=None, init=False)
    _started: bool = field(default=False, init=False)
    _terminally_stopped: bool = field(default=False, init=False)
    _generation: int = field(default=0, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def status(
        self,
        *,
        reconciliation_required: bool = False,
        failed_reason: RuntimeControlReason | str | None = None,
        kill_switch_active: bool = False,
        emergency_stop: bool = False,
    ) -> RuntimeStatus:
        failure = _optional_reason(failed_reason)
        with self._lock:
            state, reason = self._effective_state(
                reconciliation_required=reconciliation_required,
                failed_reason=failure,
                kill_switch_active=kill_switch_active,
                emergency_stop=emergency_stop,
            )
            return RuntimeStatus(
                state=state,
                reason=reason,
                execution_enabled=state is RuntimeState.RUNNING,
                market_maintenance_enabled=state
                in {RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.KILL_SWITCHED},
                entry_enable_watermark=self._entry_enable_watermark,
                started=self._started,
                stopped=state is RuntimeState.STOPPED,
                generation=self._generation,
            )

    def start(self) -> RuntimeControlResult:
        with self._lock:
            previous = self._state
            if self._state is RuntimeState.RUNNING:
                return self._result(False, True, previous)
            if self._state is not RuntimeState.STOPPED or self._terminally_stopped:
                return self._result(False, False, previous)
            self._state = RuntimeState.RUNNING
            self._reason = None
            self._started = True
            self._generation += 1
            return self._result(True, True, previous)

    def pause(
        self,
        watermark: datetime,
        *,
        reason: RuntimeControlReason = RuntimeControlReason.OPERATOR_PAUSED,
    ) -> RuntimeControlResult:
        normalized = _aware_utc(watermark)
        if reason not in {
            RuntimeControlReason.OPERATOR_PAUSED,
            RuntimeControlReason.BROKER_UNAVAILABLE,
            RuntimeControlReason.DATA_STALE,
            RuntimeControlReason.DATA_UNAVAILABLE,
        }:
            raise ValueError("reason is not a temporary pause reason")
        with self._lock:
            previous = self._state
            if self._state is RuntimeState.PAUSED:
                if self._reason is not reason:
                    self._reason = reason
                    self._entry_enable_watermark = normalized
                    self._generation += 1
                    return self._result(True, True, previous)
                return self._result(False, True, previous)
            if self._state is not RuntimeState.RUNNING:
                return self._result(False, False, previous)
            self._state = RuntimeState.PAUSED
            self._reason = reason
            self._entry_enable_watermark = normalized
            self._generation += 1
            return self._result(True, True, previous)

    def resume(
        self,
        watermark: datetime,
        *,
        reconciliation_required: bool = False,
        failed_reason: RuntimeControlReason | str | None = None,
        kill_switch_active: bool = False,
    ) -> RuntimeControlResult:
        normalized = _aware_utc(watermark)
        failure = _optional_reason(failed_reason)
        with self._lock:
            previous = self._state
            blocked = reconciliation_required or failure is not None or kill_switch_active
            if self._state is RuntimeState.RUNNING and not blocked:
                return self._result(False, True, previous)
            if self._state is not RuntimeState.PAUSED or blocked:
                return self._result(False, False, previous)
            self._state = RuntimeState.RUNNING
            self._reason = None
            self._entry_enable_watermark = normalized
            self._generation += 1
            return self._result(True, True, previous)

    def fail(self, reason: RuntimeControlReason) -> RuntimeControlResult:
        if reason not in {
            RuntimeControlReason.AUDIT_INTEGRITY_FAILED,
            RuntimeControlReason.BROKER_INCOMPATIBLE,
            RuntimeControlReason.DATA_INVALID,
            RuntimeControlReason.RUNTIME_FAILED,
        }:
            raise ValueError("reason is not a permanent runtime failure")
        with self._lock:
            previous = self._state
            if self._state is RuntimeState.FAILED and self._reason is reason:
                return self._result(False, True, previous)
            if self._state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
                return self._result(False, False, previous)
            self._state = RuntimeState.FAILED
            self._reason = reason
            self._generation += 1
            return self._result(True, True, previous)

    def request_stop(self) -> RuntimeControlResult:
        with self._lock:
            previous = self._state
            if self._state is RuntimeState.STOPPING:
                return self._result(False, True, previous)
            if self._state is RuntimeState.STOPPED:
                return self._result(False, True, previous)
            self._state = RuntimeState.STOPPING
            self._reason = RuntimeControlReason.SHUTDOWN_IN_PROGRESS
            self._generation += 1
            return self._result(True, True, previous)

    def complete_stop(self) -> RuntimeControlResult:
        with self._lock:
            previous = self._state
            if self._state is RuntimeState.STOPPED:
                return self._result(False, True, previous)
            if self._state is not RuntimeState.STOPPING:
                return self._result(False, False, previous)
            self._state = RuntimeState.STOPPED
            self._reason = RuntimeControlReason.SESSION_STOPPED
            self._terminally_stopped = True
            self._generation += 1
            return self._result(True, True, previous)

    def signal_is_eligible(self, signal_time: datetime) -> bool:
        normalized = _aware_utc(signal_time)
        with self._lock:
            return (
                self._entry_enable_watermark is None
                or normalized > self._entry_enable_watermark
            )

    def snapshot_state(self) -> dict[str, object]:
        with self._lock:
            return {
                "version": 1,
                "state": self._state.value,
                "reason": self._reason.value if self._reason is not None else None,
                "entry_enable_watermark": (
                    self._entry_enable_watermark.isoformat()
                    if self._entry_enable_watermark is not None
                    else None
                ),
                "started": self._started,
                "terminally_stopped": self._terminally_stopped,
                "generation": self._generation,
            }

    def restore_state(self, state: Mapping[str, object]) -> None:
        parsed = _parse_state(state)
        with self._lock:
            (
                self._state,
                self._reason,
                self._entry_enable_watermark,
                self._started,
                self._terminally_stopped,
                self._generation,
            ) = parsed

    def _effective_state(
        self,
        *,
        reconciliation_required: bool,
        failed_reason: RuntimeControlReason | None,
        kill_switch_active: bool,
        emergency_stop: bool,
    ) -> tuple[RuntimeState, RuntimeControlReason | None]:
        if self._state is RuntimeState.STOPPED:
            return RuntimeState.STOPPED, RuntimeControlReason.SESSION_STOPPED
        if self._state is RuntimeState.STOPPING:
            return RuntimeState.STOPPING, RuntimeControlReason.SHUTDOWN_IN_PROGRESS
        if reconciliation_required:
            return (
                RuntimeState.RECONCILIATION_REQUIRED,
                RuntimeControlReason.RECONCILIATION_REQUIRED,
            )
        if failed_reason is not None:
            return RuntimeState.FAILED, failed_reason
        if self._state is RuntimeState.FAILED:
            return RuntimeState.FAILED, self._reason
        if kill_switch_active:
            return (
                RuntimeState.KILL_SWITCHED,
                RuntimeControlReason.EMERGENCY_STOPPED
                if emergency_stop
                else RuntimeControlReason.RISK_KILL_SWITCH,
            )
        return self._state, self._reason

    def _result(
        self, changed: bool, accepted: bool, previous: RuntimeState
    ) -> RuntimeControlResult:
        return RuntimeControlResult(
            accepted=accepted,
            changed=changed,
            previous_state=previous,
            current_state=self._state,
            reason=self._reason,
        )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _optional_reason(value: RuntimeControlReason | str | None) -> RuntimeControlReason | None:
    if value is None:
        return None
    try:
        return RuntimeControlReason(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid runtime failure reason") from exc


def _parse_state(
    state: Mapping[str, object],
) -> tuple[
    RuntimeState,
    RuntimeControlReason | None,
    datetime | None,
    bool,
    bool,
    int,
]:
    if not isinstance(state, Mapping) or set(state) != {
        "version",
        "state",
        "reason",
        "entry_enable_watermark",
        "started",
        "terminally_stopped",
        "generation",
    }:
        raise ValueError("invalid runtime-control state")
    if state["version"] != 1:
        raise ValueError("unsupported runtime-control state version")
    try:
        runtime_state = RuntimeState(state["state"])
        reason = (
            RuntimeControlReason(state["reason"])
            if state["reason"] is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid runtime-control state") from exc
    raw_watermark = state["entry_enable_watermark"]
    watermark = None
    if raw_watermark is not None:
        if not isinstance(raw_watermark, str):
            raise ValueError("invalid runtime-control watermark")
        try:
            watermark = _aware_utc(datetime.fromisoformat(raw_watermark))
        except ValueError as exc:
            raise ValueError("invalid runtime-control watermark") from exc
    started, terminal = state["started"], state["terminally_stopped"]
    generation = state["generation"]
    if not isinstance(started, bool) or not isinstance(terminal, bool):
        raise ValueError("invalid runtime-control lifecycle state")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("invalid runtime-control generation")
    if terminal and runtime_state is not RuntimeState.STOPPED:
        raise ValueError("terminal runtime must be stopped")
    if runtime_state is RuntimeState.RUNNING and reason is not None:
        raise ValueError("running runtime must not carry a block reason")
    if runtime_state is not RuntimeState.RUNNING and reason is None:
        raise ValueError("blocked runtime requires a reason")
    return runtime_state, reason, watermark, started, terminal, generation
