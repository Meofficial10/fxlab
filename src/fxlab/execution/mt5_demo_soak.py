"""Deterministic offline MT5 demo soak test harness and CLI runner (Phase 2B Gate A)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .event_ledger import EventLedger
from .mt5_demo_broker import MT5_DEMO_SYMBOL, Mt5DemoBroker
from .mt5_demo_session import (
    Mt5DemoSession,
    Mt5SessionCycleKind,
    _issue_soak_execution_permit_internal,
    _Mt5DemoSoakExecutionPermit,
)
from .runtime_control import RuntimeState
from .signal_engine import SignalEvent

MT5_DEMO_SOAK_CONFIRMATION = "I_CONFIRM_MT5_DEMO_SOAK_NO_REAL_MONEY"

# Bounded safety limits for operator protection
MAX_ENTRIES_UPPER_BOUND = 100
MAX_DURATION_SECONDS_UPPER_BOUND = 86400.0  # 24 hours max
MIN_POLL_INTERVAL_SECONDS = 0.05
MAX_POLL_INTERVAL_SECONDS = 60.0
MIN_QUOTE_AGE_SECONDS = 0.1
MAX_QUOTE_AGE_SECONDS = 60.0


def _issue_soak_execution_permit(config: Mt5DemoSoakConfig) -> _Mt5DemoSoakExecutionPermit:
    """Issue the Phase 2B structural authorization strictly after config validation."""
    if not isinstance(config, Mt5DemoSoakConfig):
        raise TypeError("valid_soak_config_required")
    return _issue_soak_execution_permit_internal()


@dataclass(frozen=True)
class Mt5DemoSoakConfig:
    """Validated operational parameters for a bounded MT5 demo soak test run."""

    confirmation: str
    max_loss_usd: float
    max_entries: int
    max_duration_seconds: float
    drain_timeout_seconds: float
    max_quote_age_seconds: float
    poll_interval_seconds: float = 1.0
    cooldown_seconds: float = 30.0
    clock: Callable[[], datetime] | None = None
    sleeper: Callable[[float], None] | None = None

    def __post_init__(self) -> None:
        if self.confirmation != MT5_DEMO_SOAK_CONFIRMATION:
            raise ValueError("invalid_soak_confirmation")

        if (
            not isinstance(self.max_loss_usd, (int, float))
            or isinstance(self.max_loss_usd, bool)
            or not math.isfinite(self.max_loss_usd)
            or self.max_loss_usd <= 0
        ):
            raise ValueError("invalid_max_loss_usd")

        if (
            not isinstance(self.max_entries, int)
            or isinstance(self.max_entries, bool)
            or self.max_entries < 1
            or self.max_entries > MAX_ENTRIES_UPPER_BOUND
        ):
            raise ValueError("invalid_max_entries")

        if (
            not isinstance(self.max_duration_seconds, (int, float))
            or isinstance(self.max_duration_seconds, bool)
            or not math.isfinite(self.max_duration_seconds)
            or self.max_duration_seconds <= 0
            or self.max_duration_seconds > MAX_DURATION_SECONDS_UPPER_BOUND
        ):
            raise ValueError("invalid_max_duration_seconds")

        if (
            not isinstance(self.max_quote_age_seconds, (int, float))
            or isinstance(self.max_quote_age_seconds, bool)
            or not math.isfinite(self.max_quote_age_seconds)
            or self.max_quote_age_seconds < MIN_QUOTE_AGE_SECONDS
            or self.max_quote_age_seconds > MAX_QUOTE_AGE_SECONDS
        ):
            raise ValueError("invalid_max_quote_age_seconds")

        if (
            not isinstance(self.poll_interval_seconds, (int, float))
            or isinstance(self.poll_interval_seconds, bool)
            or not math.isfinite(self.poll_interval_seconds)
            or self.poll_interval_seconds < MIN_POLL_INTERVAL_SECONDS
            or self.poll_interval_seconds > MAX_POLL_INTERVAL_SECONDS
        ):
            raise ValueError("invalid_poll_interval_seconds")

        if (
            not isinstance(self.cooldown_seconds, (int, float))
            or isinstance(self.cooldown_seconds, bool)
            or not math.isfinite(self.cooldown_seconds)
            or self.cooldown_seconds < 0
        ):
            raise ValueError("invalid_cooldown_seconds")

        if (
            not isinstance(self.drain_timeout_seconds, (int, float))
            or isinstance(self.drain_timeout_seconds, bool)
            or not math.isfinite(self.drain_timeout_seconds)
            or self.drain_timeout_seconds <= 0
            or self.drain_timeout_seconds > MAX_DURATION_SECONDS_UPPER_BOUND
        ):
            raise ValueError("invalid_drain_timeout_seconds")


class SyntheticSoakSignalGenerator:
    """Deterministic, non-strategy signal stimulus alternating BUY and SELL."""

    def __init__(self, setup_name: str = "synthetic_soak_test") -> None:
        self._setup_name = setup_name
        self._bar_index = 0
        self._side = 1

    def next_signal(self, current_time: datetime) -> SignalEvent:
        signal = SignalEvent(
            setup_name=self._setup_name,
            symbol=MT5_DEMO_SYMBOL,
            timeframe="M1",
            side=self._side,
            signal_time=current_time,
            signal_bar_index=self._bar_index,
        )
        self._side = -self._side
        self._bar_index += 1
        return signal


@dataclass(frozen=True)
class Mt5DemoSoakResult:
    """Structured execution summary of a completed or stopped MT5 demo soak run."""

    status: str
    stop_reason: str
    entries_completed: int
    duration_seconds: float
    active_position_id: str | None = None
    error_message: str | None = None


class Mt5DemoSoakRunner:
    """Coordinated operator loop managing sequential cycles, holding, and recovery."""

    def __init__(
        self,
        signal_generator: SyntheticSoakSignalGenerator | None = None,
    ) -> None:
        self._signal_generator = signal_generator or SyntheticSoakSignalGenerator()

    def run(
        self,
        config: Mt5DemoSoakConfig,
        session: Mt5DemoSession | None = None,
        *,
        broker: Mt5DemoBroker | None = None,
        ledger: EventLedger | None = None,
    ) -> Mt5DemoSoakResult:
        clock_fn = config.clock or (lambda: datetime.now(UTC))
        sleeper_fn = config.sleeper

        # 1. Initialize or validate session
        if session is None:
            if broker is None or ledger is None:
                raise ValueError("broker_and_ledger_required_when_session_omitted")
            session = Mt5DemoSession(
                broker=broker,
                event_ledger=ledger,
                max_loss_usd=config.max_loss_usd,
                max_quote_age=timedelta(seconds=config.max_quote_age_seconds),
                execution_permit=_issue_soak_execution_permit(config),
                clock=clock_fn,
            )

        start_time = clock_fn()
        entries_count = 0
        last_close_time: datetime | None = None
        stop_reason = "completed"
        status = "completed"
        error_msg: str | None = None
        draining = False
        drain_start_time: datetime | None = None

        # 2. Start session and run startup reconciliation
        session.start()
        start_status = session.runtime_controller.status(
            reconciliation_required=session.reconciliation_required,
            kill_switch_active=session.risk_engine.kill_switch_active,
        )
        if start_status.state is RuntimeState.RECONCILIATION_REQUIRED:
            return Mt5DemoSoakResult(
                status="reconciliation_required",
                stop_reason="startup_reconciliation_required",
                entries_completed=0,
                duration_seconds=0.0,
                active_position_id=session.active_position_id,
                error_message="startup reconciliation required",
            )
        if start_status.state is RuntimeState.KILL_SWITCHED:
            return Mt5DemoSoakResult(
                status="failed",
                stop_reason="kill_switch_triggered",
                entries_completed=0,
                duration_seconds=0.0,
                active_position_id=session.active_position_id,
                error_message="risk engine kill switch is active",
            )
        if start_status.state is RuntimeState.FAILED:
            return Mt5DemoSoakResult(
                status="failed",
                stop_reason="startup_failed",
                entries_completed=0,
                duration_seconds=0.0,
                active_position_id=session.active_position_id,
                error_message="session failed to start",
            )

        # 3. Main Soak Polling Loop
        try:
            while True:
                now = clock_fn()
                elapsed = (now - start_time).total_seconds()

                # Check runtime controller state
                runtime_status = session.runtime_controller.status(
                    reconciliation_required=session.reconciliation_required,
                    kill_switch_active=session.risk_engine.kill_switch_active,
                )
                if runtime_status.state is RuntimeState.RECONCILIATION_REQUIRED:
                    status = "reconciliation_required"
                    stop_reason = "reconciliation_required"
                    error_msg = "session requires manual reconciliation"
                    break
                if runtime_status.state is RuntimeState.KILL_SWITCHED:
                    status = "failed"
                    stop_reason = "kill_switch_triggered"
                    error_msg = "risk engine kill switch is active"
                    break
                if runtime_status.state is RuntimeState.FAILED:
                    status = "failed"
                    stop_reason = "runtime_failed"
                    error_msg = "runtime controller reported fatal failure"
                    break

                # Check duration limit
                if elapsed >= config.max_duration_seconds:
                    if session.active_position_id is None:
                        status = "completed"
                        stop_reason = "max_duration_reached"
                        break
                    if not draining:
                        draining = True
                        drain_start_time = now

                # Check drain timeout
                if draining:
                    assert drain_start_time is not None
                    drain_elapsed = (now - drain_start_time).total_seconds()
                    if drain_elapsed >= config.drain_timeout_seconds:
                        status = "stopped"
                        stop_reason = "drain_timeout_with_open_position"
                        error_msg = (
                            f"position {session.active_position_id} "
                            "remained open after drain timeout"
                        )
                        break

                # Check max entries limit when no position is active
                if (
                    not draining
                    and entries_count >= config.max_entries
                    and session.active_position_id is None
                ):
                    status = "completed"
                    stop_reason = "max_entries_reached"
                    break

                # State-dependent cycle polling
                if session.active_position_id is not None:
                    # Active position is open -> Monitor position only (no new orders)
                    cycle_res = session.poll_cycle(signal=None, current_time=now)
                    if cycle_res.kind == Mt5SessionCycleKind.POSITION_CLOSED:
                        last_close_time = now
                        if draining:
                            status = "completed"
                            stop_reason = "max_duration_reached"
                            break
                    elif cycle_res.kind in (
                        Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                        Mt5SessionCycleKind.FAILED,
                    ):
                        status = (
                            "reconciliation_required"
                            if cycle_res.kind == Mt5SessionCycleKind.RECONCILIATION_REQUIRED
                            else "failed"
                        )
                        stop_reason = cycle_res.reason
                        error_msg = cycle_res.message
                        break
                else:
                    if draining:
                        status = "completed"
                        stop_reason = "max_duration_reached"
                        break

                    # No active position -> Check cooldown before next stimulus
                    in_cooldown = (
                        last_close_time is not None
                        and (now - last_close_time).total_seconds() < config.cooldown_seconds
                    )
                    if in_cooldown or entries_count >= config.max_entries:
                        # Monitor market tick / maintain pause-resume health during cooldown
                        session.poll_cycle(signal=None, current_time=now)
                    else:
                        # Eligible for next synthetic stimulus
                        signal = self._signal_generator.next_signal(now)
                        cycle_res = session.poll_cycle(signal=signal, current_time=now)
                        if cycle_res.kind == Mt5SessionCycleKind.PROCESSED:
                            entries_count += 1
                        elif cycle_res.kind == Mt5SessionCycleKind.POSITION_CLOSED:
                            entries_count += 1
                            last_close_time = now
                        elif cycle_res.kind in (
                            Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                            Mt5SessionCycleKind.FAILED,
                        ):
                            status = (
                                "reconciliation_required"
                                if cycle_res.kind == Mt5SessionCycleKind.RECONCILIATION_REQUIRED
                                else "failed"
                            )
                            stop_reason = cycle_res.reason
                            error_msg = cycle_res.message
                            break

                # Sleep interval
                if sleeper_fn is not None:
                    sleeper_fn(config.poll_interval_seconds)

        except KeyboardInterrupt:
            if session.active_position_id is not None:
                status = "stopped"
                stop_reason = "operator_interrupted_with_open_position"
                error_msg = (
                    f"operator interrupted while position {session.active_position_id} was open"
                )
            else:
                status = "stopped"
                stop_reason = "operator_interrupted"
        finally:
            end_time = clock_fn()
            total_duration = max(0.0, (end_time - start_time).total_seconds())
            try:
                session.stop(current_time=end_time)
            except Exception:
                pass

        return Mt5DemoSoakResult(
            status=status,
            stop_reason=stop_reason,
            entries_completed=entries_count,
            duration_seconds=total_duration,
            active_position_id=session.active_position_id,
            error_message=error_msg,
        )