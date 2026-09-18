"""Gate A MT5 DEMO Observation Reliability Orchestrator and Runner.

Non-production runner that orchestrates the committed Mt5DemoObservationService
against real MT5 DEMO terminal without modifying production code and with
structural zero trade mutations.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from fxlab.execution.app import AppExitCode
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import AuditEventType
from fxlab.execution.mt5_demo_broker import MT5_DEMO_MAGIC, MT5_DEMO_SYMBOL, Mt5DemoBroker
from fxlab.execution.mt5_demo_preflight import Mt5DemoPreflight, Mt5PreflightResult
from fxlab.execution.runtime_control import RuntimeState
from fxlab.operations.control import (
    CONTROL_PROTOCOL_VERSION,
    ControlAction,
    ControlRequest,
    ControlResponse,
    ServiceState,
    send_control_request,
)
from fxlab.operations.mt5_service import (
    Mt5DemoObservationService,
    Mt5ObservationConfig,
    Mt5ObservationResult,
)
from fxlab.operations.security import ControlSecret
from fxlab.operations.service import InstanceLock

# Event types that represent trade/execution mutations
TRADE_MUTATION_EVENT_TYPES = frozenset(
    {
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.ORDER_SUBMISSION_INDETERMINATE,
        AuditEventType.ORDER_FILLED,
        AuditEventType.ORDER_REJECTED,
        AuditEventType.ORDER_CANCELLED,
        AuditEventType.ORDER_STATUS_FAILED,
        AuditEventType.POSITION_OPENED,
        AuditEventType.POSITION_CLOSED,
        AuditEventType.EXECUTION_INTENT_CREATED,
        AuditEventType.EXECUTION_POLICY_FAILED,
        AuditEventType.EXECUTION_FAILED,
        AuditEventType.RECONCILIATION_FAILED,
    }
)


@dataclass(frozen=True)
class GateAConfig:
    """Configuration for Gate A Observation Reliability Runner."""

    state_directory: Path
    runtime_id: str
    session_id: str
    symbol: str = MT5_DEMO_SYMBOL
    observe_phase1_seconds: float = 30.0
    pause_phase_seconds: float = 25.0
    observe_phase2_seconds: float = 30.0
    poll_interval_seconds: float = 1.0
    max_quote_age_seconds: float = 5.0
    control_timeout_seconds: float = 5.0
    service_start_timeout_seconds: float = 10.0
    service_stop_timeout_seconds: float = 15.0
    api: object | None = None
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class GateAResult:
    """Structured, serializable verdict of Gate A observation execution."""

    status: str  # "PASS", "FAIL", "INTERRUPTED"
    failure_reason: str | None
    runtime_id: str
    session_id: str
    masked_account: str
    server: str
    company: str
    service_exit_code: int | None
    service_reason: str | None
    initial_positions: int
    initial_pending_orders: int
    final_positions: int
    final_pending_orders: int
    initial_ticks: int
    pre_pause_ticks: int
    paused_ticks: int
    post_resume_ticks: int
    status_ok: bool
    pause_ok: bool
    paused_ticks_frozen: bool
    resume_ok: bool
    post_resume_ticks_increased: bool
    stop_ok: bool
    audit_trade_mutation_events: tuple[str, ...]
    mt5_attributable_orders: int
    mt5_attributable_deals: int
    mt5_history_attribution_provable: bool
    control_server_stopped: bool
    instance_lock_released: bool
    zero_trade_mutation_proven: bool


class Mt5ObservationGateARunner:
    """Non-production orchestrator for MT5 DEMO Observation Gate A."""

    def run(self, config: GateAConfig) -> GateAResult:
        config.state_directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(config.state_directory, 0o700)
            except OSError:
                pass

        secret_file = config.state_directory / f"{config.runtime_id}.secret"
        secret_bytes = os.urandom(32)
        secret_file.write_bytes(secret_bytes)
        if os.name != "nt":
            try:
                os.chmod(secret_file, 0o600)
            except OSError:
                pass
        control_secret = ControlSecret(secret_bytes)

        masked_account = "unknown"
        server = "unknown"
        company = "unknown"
        initial_positions = -1
        initial_pending_orders = -1
        final_positions = -1
        final_pending_orders = -1

        initial_ticks = 0
        pre_pause_ticks = 0
        paused_ticks = 0
        post_resume_ticks = 0

        status_ok = False
        pause_ok = False
        paused_ticks_frozen = False
        resume_ok = False
        post_resume_ticks_increased = False
        stop_ok = False

        audit_trade_mutation_events: list[str] = []
        mt5_attributable_orders = 0
        mt5_attributable_deals = 0
        mt5_history_attribution_provable = True

        service_exit_code: int | None = None
        service_reason: str | None = None
        control_server_stopped = False
        instance_lock_released = False
        service_thread: threading.Thread | None = None
        service_result: Mt5ObservationResult | None = None
        service_exception: Exception | None = None

        primary_failure_reason: str | None = None
        is_interrupted = False

        # Step 1: Pre-run read-only MT5 verification & exposure check
        broker = Mt5DemoBroker(api=config.api) if config.api is not None else Mt5DemoBroker()
        preflight = (
            Mt5DemoPreflight(api=config.api, shutdown_after=False)
            if config.api is not None
            else Mt5DemoPreflight(shutdown_after=False)
        )

        try:
            preflight_res: Mt5PreflightResult = preflight.run(quote=config.symbol)
            masked_account = preflight_res.account
            server = preflight_res.server
            company = preflight_res.company
            if preflight_res.environment != "demo":
                return self._build_result(
                    status="FAIL",
                    failure_reason="non_demo_account_rejected",
                    config=config,
                    masked_account=masked_account,
                    server=server,
                    company=company,
                    initial_positions=initial_positions,
                    initial_pending_orders=initial_pending_orders,
                    final_positions=final_positions,
                    final_pending_orders=final_pending_orders,
                    initial_ticks=initial_ticks,
                    pre_pause_ticks=pre_pause_ticks,
                    paused_ticks=paused_ticks,
                    post_resume_ticks=post_resume_ticks,
                    status_ok=status_ok,
                    pause_ok=pause_ok,
                    paused_ticks_frozen=paused_ticks_frozen,
                    resume_ok=resume_ok,
                    post_resume_ticks_increased=post_resume_ticks_increased,
                    stop_ok=stop_ok,
                    audit_trade_mutation_events=audit_trade_mutation_events,
                    mt5_attributable_orders=mt5_attributable_orders,
                    mt5_attributable_deals=mt5_attributable_deals,
                    mt5_history_attribution_provable=mt5_history_attribution_provable,
                    service_exit_code=service_exit_code,
                    service_reason=service_reason,
                    control_server_stopped=control_server_stopped,
                    instance_lock_released=instance_lock_released,
                )
        except Exception as exc:
            return self._build_result(
                status="FAIL",
                failure_reason=f"preflight_failed: {exc}",
                config=config,
                masked_account=masked_account,
                server=server,
                company=company,
                initial_positions=initial_positions,
                initial_pending_orders=initial_pending_orders,
                final_positions=final_positions,
                final_pending_orders=final_pending_orders,
                initial_ticks=initial_ticks,
                pre_pause_ticks=pre_pause_ticks,
                paused_ticks=paused_ticks,
                post_resume_ticks=post_resume_ticks,
                status_ok=status_ok,
                pause_ok=pause_ok,
                paused_ticks_frozen=paused_ticks_frozen,
                resume_ok=resume_ok,
                post_resume_ticks_increased=post_resume_ticks_increased,
                stop_ok=stop_ok,
                audit_trade_mutation_events=audit_trade_mutation_events,
                mt5_attributable_orders=mt5_attributable_orders,
                mt5_attributable_deals=mt5_attributable_deals,
                mt5_history_attribution_provable=mt5_history_attribution_provable,
                service_exit_code=service_exit_code,
                service_reason=service_reason,
                control_server_stopped=control_server_stopped,
                instance_lock_released=instance_lock_released,
            )

        try:
            broker.connect()
            initial_exposure = broker.get_account_exposure()
            initial_positions = len(initial_exposure.open_positions)
            initial_pending_orders = len(initial_exposure.pending_orders)
            broker.disconnect()

            if not initial_exposure.is_flat:
                return self._build_result(
                    status="FAIL",
                    failure_reason="initial_exposure_not_flat",
                    config=config,
                    masked_account=masked_account,
                    server=server,
                    company=company,
                    initial_positions=initial_positions,
                    initial_pending_orders=initial_pending_orders,
                    final_positions=final_positions,
                    final_pending_orders=final_pending_orders,
                    initial_ticks=initial_ticks,
                    pre_pause_ticks=pre_pause_ticks,
                    paused_ticks=paused_ticks,
                    post_resume_ticks=post_resume_ticks,
                    status_ok=status_ok,
                    pause_ok=pause_ok,
                    paused_ticks_frozen=paused_ticks_frozen,
                    resume_ok=resume_ok,
                    post_resume_ticks_increased=post_resume_ticks_increased,
                    stop_ok=stop_ok,
                    audit_trade_mutation_events=audit_trade_mutation_events,
                    mt5_attributable_orders=mt5_attributable_orders,
                    mt5_attributable_deals=mt5_attributable_deals,
                    mt5_history_attribution_provable=mt5_history_attribution_provable,
                    service_exit_code=service_exit_code,
                    service_reason=service_reason,
                    control_server_stopped=control_server_stopped,
                    instance_lock_released=instance_lock_released,
                )
        except Exception as exc:
            try:
                broker.disconnect()
            except Exception:
                pass
            return self._build_result(
                status="FAIL",
                failure_reason=f"initial_exposure_check_failed: {exc}",
                config=config,
                masked_account=masked_account,
                server=server,
                company=company,
                initial_positions=initial_positions,
                initial_pending_orders=initial_pending_orders,
                final_positions=final_positions,
                final_pending_orders=final_pending_orders,
                initial_ticks=initial_ticks,
                pre_pause_ticks=pre_pause_ticks,
                paused_ticks=paused_ticks,
                post_resume_ticks=post_resume_ticks,
                status_ok=status_ok,
                pause_ok=pause_ok,
                paused_ticks_frozen=paused_ticks_frozen,
                resume_ok=resume_ok,
                post_resume_ticks_increased=post_resume_ticks_increased,
                stop_ok=stop_ok,
                audit_trade_mutation_events=audit_trade_mutation_events,
                mt5_attributable_orders=mt5_attributable_orders,
                mt5_attributable_deals=mt5_attributable_deals,
                mt5_history_attribution_provable=mt5_history_attribution_provable,
                service_exit_code=service_exit_code,
                service_reason=service_reason,
                control_server_stopped=control_server_stopped,
                instance_lock_released=instance_lock_released,
            )

        # Step 2: Initialize and run production observation service in background thread
        obs_config = Mt5ObservationConfig(
            runtime_id=config.runtime_id,
            session_id=config.session_id,
            symbol=config.symbol,
            state_directory=config.state_directory,
            poll_interval_seconds=config.poll_interval_seconds,
            max_quote_age_seconds=config.max_quote_age_seconds,
            control_secret_file=secret_file,
            clock=config.clock,
            sleeper=config.sleeper,
        )
        service = (
            Mt5DemoObservationService(obs_config, api=config.api)
            if config.api is not None
            else Mt5DemoObservationService(obs_config)
        )

        def _service_worker() -> None:
            nonlocal service_result, service_exception
            try:
                service_result = service.run()
            except Exception as exc:
                service_exception = exc

        service_thread = threading.Thread(
            target=_service_worker,
            name=f"gate-a-service-{config.runtime_id}",
            daemon=False,
        )
        service_thread.start()

        operational_cfg = obs_config.operational_config
        assert operational_cfg is not None

        def _send_control(action: ControlAction) -> tuple[ControlResponse | None, str | None]:
            """Send control request and return (response, error_str_if_exception)."""
            try:
                resp = send_control_request(
                    operational_cfg,
                    control_secret,
                    ControlRequest(
                        CONTROL_PROTOCOL_VERSION,
                        str(uuid.uuid4()),
                        action,
                    ),
                )
                return resp, None
            except Exception as exc:
                err_msg = type(exc).__name__ + (f": {exc}" if str(exc) else "")
                return None, err_msg

        def _terminate_service_and_join(timeout: float) -> bool:
            """Safe bounded lifecycle cleanup without daemon threads."""
            if service_thread is None or not service_thread.is_alive():
                return True
            try:
                _send_control(ControlAction.STOP)
            except Exception:
                pass
            service_thread.join(timeout=timeout)
            if not service_thread.is_alive():
                return True
            try:
                service.request_stop()
            except Exception:
                pass
            service_thread.join(timeout=timeout)
            return not service_thread.is_alive()

        def _validate_status(
            resp: ControlResponse | None,
            err: str | None,
            *,
            expected_runtime: RuntimeState | None = RuntimeState.RUNNING,
        ) -> tuple[bool, str | None, int]:
            """Deterministic fail-closed validation of STATUS response."""
            if resp is None:
                reason = f"status_response_missing ({err})" if err else "status_response_missing"
                return False, reason, 0
            if not resp.accepted:
                reason = (
                    f"status_request_rejected: {resp.reason}"
                    if resp.reason
                    else "status_request_rejected"
                )
                return False, reason, 0
            if resp.service_state is not ServiceState.RUNNING:
                return False, f"unexpected_service_state: {resp.service_state.value}", 0
            if expected_runtime is not None and resp.runtime_state is not expected_runtime:
                st_str = resp.runtime_state.value if resp.runtime_state is not None else "None"
                return False, f"unexpected_runtime_state: {st_str}", 0
            if not isinstance(resp.payload, tuple):
                return False, "invalid_status_payload: payload_not_tuple", 0
            try:
                p_dict = dict(resp.payload)
            except Exception:
                return False, "invalid_status_payload: payload_not_dict_convertible", 0
            ticks_val = p_dict.get("ticks_observed")
            if ticks_val is None or not isinstance(ticks_val, int) or ticks_val < 0:
                return False, "invalid_status_payload: ticks_observed_missing_or_invalid", 0
            return True, None, ticks_val

        try:
            # Step 3: Wait for control server to become healthy and reachable
            deadline = time.monotonic() + config.service_start_timeout_seconds
            started = False
            while time.monotonic() < deadline:
                if not service_thread.is_alive():
                    break
                resp, _ = _send_control(ControlAction.STATUS)
                if (
                    resp is not None
                    and resp.accepted
                    and resp.service_state is ServiceState.RUNNING
                ):
                    started = True
                    break
                time.sleep(0.1)

            if not started:
                primary_failure_reason = "service_failed_to_start_or_control_unavailable"

            # Step 4: Phase 1 Active Observation
            if primary_failure_reason is None:
                config.sleeper(config.observe_phase1_seconds)

            # Step 5: Operator STATUS
            if primary_failure_reason is None:
                status_resp, status_err = _send_control(ControlAction.STATUS)
                ok, diag, ticks = _validate_status(
                    status_resp, status_err, expected_runtime=RuntimeState.RUNNING
                )
                if ok:
                    status_ok = True
                    initial_ticks = ticks
                    pre_pause_ticks = ticks
                else:
                    primary_failure_reason = diag

            # Step 6: Operator PAUSE
            if primary_failure_reason is None:
                pause_resp, pause_err = _send_control(ControlAction.PAUSE)
                if (
                    pause_resp is not None
                    and pause_resp.accepted
                    and pause_resp.runtime_state is RuntimeState.PAUSED
                ):
                    pause_ok = True
                else:
                    if pause_resp is None:
                        primary_failure_reason = (
                            f"pause_response_missing ({pause_err})"
                            if pause_err
                            else "pause_response_missing"
                        )
                    elif not pause_resp.accepted:
                        primary_failure_reason = "pause_request_rejected"
                    else:
                        st = (
                            pause_resp.runtime_state.value
                            if pause_resp.runtime_state
                            else "None"
                        )
                        primary_failure_reason = f"pause_unexpected_runtime_state: {st}"

            # Step 7: Record pause settling baseline
            if primary_failure_reason is None:
                time.sleep(min(0.2, config.poll_interval_seconds))
                p_resp, p_err = _send_control(ControlAction.STATUS)
                p_ok, p_diag, p_ticks = _validate_status(
                    p_resp, p_err, expected_runtime=RuntimeState.PAUSED
                )
                if p_ok:
                    paused_ticks = p_ticks
                else:
                    primary_failure_reason = p_diag

            # Step 8: Pause Observation Window & Freeze Check
            if primary_failure_reason is None:
                config.sleeper(config.pause_phase_seconds)
                f_resp, f_err = _send_control(ControlAction.STATUS)
                f_ok, f_diag, f_ticks = _validate_status(
                    f_resp, f_err, expected_runtime=RuntimeState.PAUSED
                )
                if not f_ok:
                    primary_failure_reason = f_diag
                elif f_ticks != paused_ticks:
                    primary_failure_reason = "ticks_observed_changed_while_paused"
                else:
                    paused_ticks_frozen = True

            # Step 9: Operator RESUME
            if primary_failure_reason is None:
                resume_resp, resume_err = _send_control(ControlAction.RESUME)
                if (
                    resume_resp is not None
                    and resume_resp.accepted
                    and resume_resp.runtime_state is RuntimeState.RUNNING
                ):
                    resume_ok = True
                else:
                    if resume_resp is None:
                        primary_failure_reason = (
                            f"resume_response_missing ({resume_err})"
                            if resume_err
                            else "resume_response_missing"
                        )
                    elif not resume_resp.accepted:
                        primary_failure_reason = "resume_request_rejected"
                    else:
                        st = (
                            resume_resp.runtime_state.value
                            if resume_resp.runtime_state
                            else "None"
                        )
                        primary_failure_reason = f"resume_unexpected_runtime_state: {st}"

            # Step 10: Phase 2 Active Observation
            if primary_failure_reason is None:
                config.sleeper(config.observe_phase2_seconds)

            # Step 11: Operator STATUS after Resume
            if primary_failure_reason is None:
                post_resp, post_err = _send_control(ControlAction.STATUS)
                post_ok, post_diag, post_ticks = _validate_status(
                    post_resp, post_err, expected_runtime=RuntimeState.RUNNING
                )
                if not post_ok:
                    primary_failure_reason = post_diag
                else:
                    post_resume_ticks = post_ticks
                    if post_resume_ticks <= paused_ticks:
                        primary_failure_reason = (
                            "ticks_observed_did_not_increase_after_resume"
                        )
                    else:
                        post_resume_ticks_increased = True

            # Step 12: Operator STOP
            if primary_failure_reason is None:
                stop_resp, stop_err = _send_control(ControlAction.STOP)
                if (
                    stop_resp is not None
                    and stop_resp.accepted
                    and stop_resp.service_state is ServiceState.STOPPING
                ):
                    stop_ok = True
                else:
                    primary_failure_reason = "stop_request_rejected"

            # Step 13: Normal wait for service thread to terminate
            if primary_failure_reason is None:
                service_thread.join(timeout=config.service_stop_timeout_seconds)
                if service_thread.is_alive():
                    primary_failure_reason = "service_thread_did_not_terminate_on_stop"

        except KeyboardInterrupt:
            is_interrupted = True
            primary_failure_reason = "operator_interrupted"
        except Exception as exc:
            if primary_failure_reason is None:
                primary_failure_reason = (
                    f"unexpected_runner_exception: {type(exc).__name__}: {exc}"
                )

        # -------------------------------------------------------------------
        # UNIFIED FINALIZATION & SAFETY EVIDENCE COLLECTION (ALWAYS RUNS)
        # -------------------------------------------------------------------
        # 1. Bounded lifecycle termination if thread is still running
        thread_joined = _terminate_service_and_join(timeout=config.service_stop_timeout_seconds)
        if not thread_joined and primary_failure_reason is None:
            primary_failure_reason = "service_thread_did_not_terminate"

        # 2. Collect service result and reason
        if service_result is not None:
            service_exit_code = service_result.exit_code
            service_reason = service_result.reason
        elif service_exception is not None:
            service_reason = str(service_exception)

        if (
            service_exit_code is not None
            and service_exit_code != int(AppExitCode.SUCCESS)
            and primary_failure_reason is None
        ):
            primary_failure_reason = (
                f"service_non_zero_exit_code: {service_exit_code} ({service_reason})"
            )

        # 3. Post-Run Read-Only MT5 Verification & Exposure Check
        try:
            broker.connect()
            final_exposure = broker.get_account_exposure()
            final_positions = len(final_exposure.open_positions)
            final_pending_orders = len(final_exposure.pending_orders)
            broker.disconnect()

            if not final_exposure.is_flat and primary_failure_reason is None:
                primary_failure_reason = "final_exposure_not_flat"
        except Exception as exc:
            try:
                broker.disconnect()
            except Exception:
                pass
            if primary_failure_reason is None:
                primary_failure_reason = f"final_exposure_check_failed: {exc}"

        # 4. Validate SQLite Audit Ledger
        store: SQLiteEventStore | None = None
        try:
            if obs_config.store_path.exists():
                store = SQLiteEventStore(obs_config.store_path, config.session_id)
                events = store.load_events()
                if not events and primary_failure_reason is None:
                    primary_failure_reason = "audit_store_empty"

                for ev in events:
                    if ev.event_type in TRADE_MUTATION_EVENT_TYPES:
                        audit_trade_mutation_events.append(ev.event_type.value)

                if audit_trade_mutation_events and primary_failure_reason is None:
                    primary_failure_reason = (
                        f"audit_contains_trade_mutation_events: {audit_trade_mutation_events}"
                    )
        except Exception as exc:
            if primary_failure_reason is None:
                primary_failure_reason = f"audit_store_validation_failed: {exc}"
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

        # 5. Check MT5 Deal/Order History Attribution
        api_obj = config.api
        if api_obj is not None:
            deals_fn = getattr(api_obj, "history_deals_get", None)
            if callable(deals_fn):
                try:
                    deals_raw = deals_fn() or ()
                    for d in deals_raw:
                        if getattr(d, "magic", None) == MT5_DEMO_MAGIC:
                            mt5_attributable_deals += 1
                except Exception:
                    mt5_history_attribution_provable = False

            orders_fn = getattr(api_obj, "history_orders_get", None)
            if callable(orders_fn):
                try:
                    orders_raw = orders_fn() or ()
                    for o in orders_raw:
                        if getattr(o, "magic", None) == MT5_DEMO_MAGIC:
                            mt5_attributable_orders += 1
                except Exception:
                    mt5_history_attribution_provable = False

        if (
            mt5_attributable_deals > 0 or mt5_attributable_orders > 0
        ) and primary_failure_reason is None:
            primary_failure_reason = "attributable_mt5_history_mutation_detected"

        # 6. Verify Lock and Server Cleanup
        try:
            test_lock = InstanceLock(obs_config.lock_path, config.runtime_id)
            test_lock.acquire()
            test_lock.release()
            instance_lock_released = True
        except Exception:
            instance_lock_released = False

        control_server_stopped = (
            not service.control_server
            or not service.control_server._thread
            or not service.control_server._thread.is_alive()
        )

        if (
            not instance_lock_released or not control_server_stopped
        ) and primary_failure_reason is None:
            primary_failure_reason = "resource_cleanup_incomplete"

        # 7. Determine Final Status
        if is_interrupted:
            final_status = "INTERRUPTED"
        elif primary_failure_reason is not None:
            final_status = "FAIL"
        else:
            final_status = "PASS"

        return self._build_result(
            status=final_status,
            failure_reason=primary_failure_reason,
            config=config,
            masked_account=masked_account,
            server=server,
            company=company,
            initial_positions=initial_positions,
            initial_pending_orders=initial_pending_orders,
            final_positions=final_positions,
            final_pending_orders=final_pending_orders,
            initial_ticks=initial_ticks,
            pre_pause_ticks=pre_pause_ticks,
            paused_ticks=paused_ticks,
            post_resume_ticks=post_resume_ticks,
            status_ok=status_ok,
            pause_ok=pause_ok,
            paused_ticks_frozen=paused_ticks_frozen,
            resume_ok=resume_ok,
            post_resume_ticks_increased=post_resume_ticks_increased,
            stop_ok=stop_ok,
            audit_trade_mutation_events=audit_trade_mutation_events,
            mt5_attributable_orders=mt5_attributable_orders,
            mt5_attributable_deals=mt5_attributable_deals,
            mt5_history_attribution_provable=mt5_history_attribution_provable,
            service_exit_code=service_exit_code,
            service_reason=service_reason,
            control_server_stopped=control_server_stopped,
            instance_lock_released=instance_lock_released,
        )

    def _build_result(
        self,
        *,
        status: str,
        failure_reason: str | None,
        config: GateAConfig,
        masked_account: str,
        server: str,
        company: str,
        initial_positions: int,
        initial_pending_orders: int,
        final_positions: int,
        final_pending_orders: int,
        initial_ticks: int,
        pre_pause_ticks: int,
        paused_ticks: int,
        post_resume_ticks: int,
        status_ok: bool,
        pause_ok: bool,
        paused_ticks_frozen: bool,
        resume_ok: bool,
        post_resume_ticks_increased: bool,
        stop_ok: bool,
        audit_trade_mutation_events: list[str],
        mt5_attributable_orders: int,
        mt5_attributable_deals: int,
        mt5_history_attribution_provable: bool,
        service_exit_code: int | None,
        service_reason: str | None,
        control_server_stopped: bool,
        instance_lock_released: bool,
    ) -> GateAResult:
        zero_trade_mutation_proven = (
            initial_positions == 0
            and initial_pending_orders == 0
            and final_positions == 0
            and final_pending_orders == 0
            and len(audit_trade_mutation_events) == 0
            and mt5_attributable_orders == 0
            and mt5_attributable_deals == 0
            and mt5_history_attribution_provable is True
            and control_server_stopped is True
            and instance_lock_released is True
        )

        res = GateAResult(
            status=status,
            failure_reason=failure_reason,
            runtime_id=config.runtime_id,
            session_id=config.session_id,
            masked_account=masked_account,
            server=server,
            company=company,
            service_exit_code=service_exit_code,
            service_reason=service_reason,
            initial_positions=initial_positions,
            initial_pending_orders=initial_pending_orders,
            final_positions=final_positions,
            final_pending_orders=final_pending_orders,
            initial_ticks=initial_ticks,
            pre_pause_ticks=pre_pause_ticks,
            paused_ticks=paused_ticks,
            post_resume_ticks=post_resume_ticks,
            status_ok=status_ok,
            pause_ok=pause_ok,
            paused_ticks_frozen=paused_ticks_frozen,
            resume_ok=resume_ok,
            post_resume_ticks_increased=post_resume_ticks_increased,
            stop_ok=stop_ok,
            audit_trade_mutation_events=tuple(audit_trade_mutation_events),
            mt5_attributable_orders=mt5_attributable_orders,
            mt5_attributable_deals=mt5_attributable_deals,
            mt5_history_attribution_provable=mt5_history_attribution_provable,
            control_server_stopped=control_server_stopped,
            instance_lock_released=instance_lock_released,
            zero_trade_mutation_proven=zero_trade_mutation_proven,
        )

        result_path = config.state_directory / "gate_a_result.json"
        try:
            result_dict = asdict(res)
            result_path.write_text(json.dumps(result_dict, indent=2), encoding="utf-8")
        except Exception:
            pass

        return res


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for running Gate A against real MT5."""
    parser = argparse.ArgumentParser(
        prog="run_mt5_observation_gate_a",
        description="MT5 DEMO Observation Gate A Reliability Runner (Non-production, Read-only).",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Explicitly execute Gate A observation run against MT5 DEMO terminal.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Optional base directory for audit and state persistence.",
    )

    args = parser.parse_args(argv)

    if not args.run:
        parser.print_help()
        print("\nNotice: Pass '--run' to explicitly execute Gate A against MT5 DEMO terminal.")
        return 0

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    runtime_id = f"gate_a_{timestamp}"
    session_id = f"gate_a_session_{timestamp}"

    data_dir = args.data_dir or Path("E:/jarvis-data/fxlab-demo-audit")
    if not data_dir.exists():
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            data_dir = Path("state/gate_a_audit")

    state_dir = data_dir / f"observation-gate-a-{timestamp}"
    config = GateAConfig(
        state_directory=state_dir.resolve(),
        runtime_id=runtime_id,
        session_id=session_id,
    )

    runner = Mt5ObservationGateARunner()
    print("=== Starting MT5 DEMO Observation Gate A ===")
    print(f"State Directory: {config.state_directory}")
    print(f"Runtime ID:      {config.runtime_id}")
    print(f"Session ID:      {config.session_id}")

    res = runner.run(config)
    print("\n=== Gate A Completed ===")
    print(f"Status:                      {res.status}")
    print(f"Failure Reason:              {res.failure_reason}")
    print(f"Account:                     {res.masked_account} ({res.server} / {res.company})")
    print(f"Initial Positions / Orders:  {res.initial_positions} / {res.initial_pending_orders}")
    print(f"Final Positions / Orders:    {res.final_positions} / {res.final_pending_orders}")
    print(
        f"Initial / Paused / Resume:   {res.initial_ticks} / "
        f"{res.paused_ticks} / {res.post_resume_ticks}"
    )
    print(f"Zero Mutation Proven:        {res.zero_trade_mutation_proven}")
    print(f"Evidence Directory:          {config.state_directory}")

    return 0 if res.status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
