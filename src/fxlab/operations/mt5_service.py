"""Observation-only MT5 DEMO service composition kernel (Autonomy V1A).

Runs an unattended, read-only observation lifecycle against MT5 DEMO terminal.
Enforces structural zero trade mutations (no execution permits, no order submission,
no position closing).
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock

from ..execution.app import AppExitCode
from ..execution.durable_event_store import SQLiteEventStore
from ..execution.event_ledger import EventLedger
from ..execution.mt5_demo_broker import MT5_DEMO_SYMBOL, Mt5DemoBroker
from ..execution.mt5_demo_preflight import Mt5DemoPreflight, _load_mt5
from ..execution.mt5_demo_session import Mt5DemoSession, Mt5SessionCycleKind
from ..execution.runtime_control import RuntimeState
from .control import ServiceState
from .security import is_safe_local_absolute_path
from .service import InstanceLock, OperationalLogger

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_LOG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.jsonl$")


@dataclass(frozen=True, slots=True)
class Mt5ObservationConfig:
    """Validated operational parameters for MT5 DEMO observation-only service."""

    state_directory: Path
    runtime_id: str
    session_id: str
    symbol: str = MT5_DEMO_SYMBOL
    poll_interval_seconds: float = 1.0
    max_quote_age_seconds: float = 5.0
    max_loss_usd: float = 10.0
    log_filename: str | None = None
    clock: Callable[[], datetime] | None = None
    sleeper: Callable[[float], None] | None = None
    stop_predicate: Callable[[], bool] | None = None
    max_cycles: int | None = None

    def __post_init__(self) -> None:
        state = Path(self.state_directory)
        if not is_safe_local_absolute_path(state):
            raise ValueError("state_directory must be an absolute local path without aliases")
        for name in ("runtime_id", "session_id"):
            val = getattr(self, name)
            if not isinstance(val, str) or not _SAFE_ID.fullmatch(val):
                raise ValueError(f"{name} must be a safe identifier")
        if self.symbol != MT5_DEMO_SYMBOL:
            raise ValueError(f"unsupported symbol {self.symbol}; must be {MT5_DEMO_SYMBOL}")
        if (
            isinstance(self.poll_interval_seconds, bool)
            or not isinstance(self.poll_interval_seconds, (int, float))
            or not math.isfinite(self.poll_interval_seconds)
            or self.poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be a positive finite number")
        if (
            isinstance(self.max_quote_age_seconds, bool)
            or not isinstance(self.max_quote_age_seconds, (int, float))
            or not math.isfinite(self.max_quote_age_seconds)
            or self.max_quote_age_seconds <= 0
        ):
            raise ValueError("max_quote_age_seconds must be a positive finite number")
        if (
            isinstance(self.max_loss_usd, bool)
            or not isinstance(self.max_loss_usd, (int, float))
            or not math.isfinite(self.max_loss_usd)
            or self.max_loss_usd <= 0
        ):
            raise ValueError("max_loss_usd must be a positive finite number")
        if self.log_filename is not None:
            if not isinstance(self.log_filename, str) or not _SAFE_LOG.fullmatch(self.log_filename):
                raise ValueError("log_filename must be a safe .jsonl filename")
        if self.max_cycles is not None:
            if (
                isinstance(self.max_cycles, bool)
                or not isinstance(self.max_cycles, int)
                or self.max_cycles < 1
            ):
                raise ValueError("max_cycles must be an integer >= 1")
        object.__setattr__(self, "state_directory", state)

    @property
    def store_path(self) -> Path:
        return self.state_directory / f"{self.runtime_id}.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_directory / f"{self.runtime_id}.lock"

    @property
    def log_path(self) -> Path:
        return self.state_directory / (self.log_filename or f"{self.runtime_id}.jsonl")


@dataclass(frozen=True, slots=True)
class Mt5ObservationResult:
    """Structured result of an MT5 observation service execution."""

    exit_code: int
    service_state: ServiceState
    reason: str
    session_id: str
    cycles_completed: int = 0
    ticks_observed: int = 0
    active_position_id: str | None = None
    error_message: str | None = None


class Mt5DemoObservationService:
    """Foreground, unattended MT5 DEMO observation-only service."""

    def __init__(
        self,
        config: Mt5ObservationConfig,
        *,
        api: object | None = None,
        broker: Mt5DemoBroker | None = None,
        preflight: Mt5DemoPreflight | None = None,
    ) -> None:
        if not isinstance(config, Mt5ObservationConfig):
            raise ValueError("validated_observation_config_required")
        self.config = config
        self.api = api
        self.broker = broker
        self.preflight = preflight
        self._state = ServiceState.STARTING
        self._state_lock = Lock()
        self._stop_requested = Event()
        self._session: Mt5DemoSession | None = None
        self._logger: OperationalLogger | None = None
        self._ticks_observed = 0

    @property
    def state(self) -> ServiceState:
        with self._state_lock:
            return self._state

    @property
    def session(self) -> Mt5DemoSession | None:
        return self._session

    @property
    def logger(self) -> OperationalLogger | None:
        return self._logger

    def request_stop(self) -> None:
        self._stop_requested.set()

    def _set_state(self, state: ServiceState) -> None:
        with self._state_lock:
            self._state = state

    def run(self) -> Mt5ObservationResult:
        lock = InstanceLock(self.config.lock_path, self.config.runtime_id)
        cycles = 0
        self._ticks_observed = 0
        exit_code = AppExitCode.RUNTIME_FAILURE
        reason = "service_failed"
        error_msg: str | None = None
        session: Mt5DemoSession | None = None
        broker: Mt5DemoBroker | None = self.broker
        logger: OperationalLogger | None = None
        clock_fn = self.config.clock or (lambda: datetime.now(UTC))
        sleeper_fn = self.config.sleeper or time.sleep

        # Phase 1: Lock acquisition BEFORE any broker or service activity
        self._set_state(ServiceState.STARTING)
        try:
            self.config.state_directory.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.config.state_directory, 0o700)
            lock.acquire()
        except Exception as exc:
            self._set_state(ServiceState.FAILED)
            return Mt5ObservationResult(
                exit_code=int(AppExitCode.RUNTIME_FAILURE),
                service_state=ServiceState.FAILED,
                reason="lock_acquisition_failed",
                session_id=self.config.session_id,
                error_message=str(exc),
            )

        try:
            # Phase 2: Operational Logger initialization
            logger = OperationalLogger(
                self.config.log_path,
                runtime_id=self.config.runtime_id,
                session_id=self.config.session_id,
            )
            logger.open()
            self._logger = logger
            logger.write(
                severity="info",
                reason_code="service_starting",
                service_state=self.state.value,
            )

            # Phase 3: Preflight Verification
            self._set_state(ServiceState.PREFLIGHT)
            api = self.api
            if api is None:
                if broker is not None:
                    api = broker.api
                else:
                    api = _load_mt5()

            preflight = self.preflight or Mt5DemoPreflight(api=api, shutdown_after=False)
            logger.write(
                severity="info",
                reason_code="preflight_started",
                service_state=self.state.value,
            )
            try:
                preflight_result = preflight.run(quote=self.config.symbol)
            except Exception as exc:
                logger.write(
                    severity="error",
                    reason_code="preflight_failed",
                    service_state=self.state.value,
                    result="failed",
                    failure_category="preflight",
                )
                self._set_state(ServiceState.FAILED)
                return Mt5ObservationResult(
                    exit_code=int(AppExitCode.RUNTIME_FAILURE),
                    service_state=ServiceState.FAILED,
                    reason="preflight_failed",
                    session_id=self.config.session_id,
                    error_message=str(exc),
                )

            if preflight_result.environment != "demo":
                logger.write(
                    severity="critical",
                    reason_code="non_demo_environment_rejected",
                    service_state=self.state.value,
                    result="rejected",
                    failure_category="environment",
                )
                self._set_state(ServiceState.FAILED)
                return Mt5ObservationResult(
                    exit_code=int(AppExitCode.RUNTIME_FAILURE),
                    service_state=ServiceState.FAILED,
                    reason="mt5_demo_account_required",
                    session_id=self.config.session_id,
                    error_message="non-demo environment detected",
                )

            logger.write(
                severity="info",
                reason_code="preflight_passed",
                service_state=self.state.value,
            )

            # Phase 4: Broker & Durable Session assembly (STRUCTURAL ZERO EXECUTION PERMIT)
            if broker is None:
                broker = Mt5DemoBroker(
                    api=api,
                    max_quote_age=timedelta(seconds=self.config.max_quote_age_seconds),
                    poll_interval=timedelta(
                        seconds=min(0.05, self.config.poll_interval_seconds)
                    ),
                    clock=self.config.clock,
                    sleeper=self.config.sleeper,
                )
                self.broker = broker

            store = SQLiteEventStore(
                self.config.store_path, session_id=self.config.session_id
            )
            ledger = EventLedger(session_id=self.config.session_id, durable_store=store)

            session = Mt5DemoSession(
                broker=broker,
                event_ledger=ledger,
                max_loss_usd=self.config.max_loss_usd,
                max_quote_age=timedelta(seconds=self.config.max_quote_age_seconds),
                execution_permit=None,  # STRUCTURAL ZERO-MUTATION GUARANTEE: NO PERMIT
                clock=self.config.clock,
                disconnect_on_stop=True,
            )
            self._session = session

            # Phase 5: Startup Reconciliation
            session.start()
            start_status = session.runtime_controller.status(
                reconciliation_required=session.reconciliation_required,
                kill_switch_active=session.risk_engine.kill_switch_active,
            )

            if (
                start_status.state is RuntimeState.RECONCILIATION_REQUIRED
                or session.reconciliation_required
            ):
                logger.write(
                    severity="critical",
                    reason_code="startup_reconciliation_required",
                    service_state=self.state.value,
                    result="reconciliation_required",
                )
                self._set_state(ServiceState.FAILED)
                return Mt5ObservationResult(
                    exit_code=int(AppExitCode.RECONCILIATION_REQUIRED),
                    service_state=ServiceState.FAILED,
                    reason="reconciliation_required",
                    session_id=self.config.session_id,
                    active_position_id=session.active_position_id,
                    error_message="session requires reconciliation upon startup",
                )

            if start_status.state is RuntimeState.KILL_SWITCHED:
                logger.write(
                    severity="error",
                    reason_code="kill_switch_active",
                    service_state=self.state.value,
                )
                self._set_state(ServiceState.FAILED)
                return Mt5ObservationResult(
                    exit_code=int(AppExitCode.RUNTIME_FAILURE),
                    service_state=ServiceState.FAILED,
                    reason="kill_switch_triggered",
                    session_id=self.config.session_id,
                    active_position_id=session.active_position_id,
                    error_message="risk engine kill switch is active",
                )

            if start_status.state is RuntimeState.FAILED:
                logger.write(
                    severity="error",
                    reason_code="startup_failed",
                    service_state=self.state.value,
                )
                self._set_state(ServiceState.FAILED)
                return Mt5ObservationResult(
                    exit_code=int(AppExitCode.RUNTIME_FAILURE),
                    service_state=ServiceState.FAILED,
                    reason="startup_failed",
                    session_id=self.config.session_id,
                    error_message="session failed to start",
                )

            # Phase 6: Observation Polling Loop
            self._set_state(ServiceState.RUNNING)
            logger.write(
                severity="info",
                reason_code="observation_running",
                service_state=self.state.value,
            )

            while not self._stop_requested.is_set():
                if self.config.stop_predicate and self.config.stop_predicate():
                    reason = "stop_predicate_satisfied"
                    exit_code = AppExitCode.SUCCESS
                    break

                if self.config.max_cycles is not None and cycles >= self.config.max_cycles:
                    reason = "max_cycles_reached"
                    exit_code = AppExitCode.SUCCESS
                    break

                now = clock_fn()

                # Poll cycle strictly in observation mode:
                # signal=None, synthetic_signal_factory=None, force_close=False
                cycle_res = session.poll_cycle(
                    signal=None,
                    synthetic_signal_factory=None,
                    force_close=False,
                    current_time=now,
                )
                cycles += 1
                if cycle_res.tick is not None:
                    self._ticks_observed += 1

                if cycle_res.kind is Mt5SessionCycleKind.RECONCILIATION_REQUIRED:
                    logger.write(
                        severity="critical",
                        reason_code="reconciliation_required",
                        service_state=self.state.value,
                        result="reconciliation_required",
                    )
                    reason = "reconciliation_required"
                    exit_code = AppExitCode.RECONCILIATION_REQUIRED
                    error_msg = cycle_res.message
                    break

                if cycle_res.kind is Mt5SessionCycleKind.FAILED:
                    logger.write(
                        severity="error",
                        reason_code="cycle_failed",
                        service_state=self.state.value,
                        result=cycle_res.reason,
                    )
                    reason = cycle_res.reason or "cycle_failed"
                    exit_code = AppExitCode.RUNTIME_FAILURE
                    error_msg = cycle_res.message
                    break

                if sleeper_fn is not None:
                    sleeper_fn(self.config.poll_interval_seconds)

            if self._stop_requested.is_set():
                reason = "operator_stopped"
                exit_code = AppExitCode.SUCCESS

        except KeyboardInterrupt:
            reason = "operator_interrupted"
            exit_code = AppExitCode.INTERRUPTED
        except Exception as exc:
            reason = "runtime_exception"
            exit_code = AppExitCode.RUNTIME_FAILURE
            error_msg = str(exc)
            if logger is not None:
                try:
                    logger.write(
                        severity="critical",
                        reason_code="unhandled_exception",
                        service_state=self.state.value,
                        result="exception",
                        failure_category="runtime",
                    )
                except Exception:
                    pass
        finally:
            self._set_state(ServiceState.STOPPING)
            if session is not None:
                try:
                    now = None
                    try:
                        now = clock_fn()
                    except Exception:
                        pass
                    session.stop(current_time=now)
                except Exception:
                    try:
                        session.broker.disconnect()
                    except Exception:
                        pass
            elif broker is not None and broker.is_connected():
                try:
                    broker.disconnect()
                except Exception:
                    pass

            final_service_state = (
                ServiceState.STOPPED
                if exit_code in {AppExitCode.SUCCESS, AppExitCode.INTERRUPTED}
                else ServiceState.FAILED
            )
            self._set_state(final_service_state)

            if logger is not None:
                try:
                    logger.write(
                        severity="info" if exit_code == AppExitCode.SUCCESS else "warning",
                        reason_code="service_stopped",
                        service_state=final_service_state.value,
                        result=reason,
                    )
                    logger.close()
                except Exception:
                    pass

            lock.release()

        return Mt5ObservationResult(
            exit_code=int(exit_code),
            service_state=self.state,
            reason=reason,
            session_id=self.config.session_id,
            cycles_completed=cycles,
            ticks_observed=self._ticks_observed,
            active_position_id=session.active_position_id if session else None,
            error_message=error_msg,
        )
