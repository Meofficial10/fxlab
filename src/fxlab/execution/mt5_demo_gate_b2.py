"""Gate B2 Repeated MT5 DEMO Reliability Orchestrator.

Orchestrates sequential, bounded, independent MT5 DEMO soak runs (Gate B2)
using Mt5DemoSoakRunner without bypassing risk, preflight, reconciliation, or exposure checks.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .durable_event_store import SQLiteEventStore
from .event_ledger import AuditEventType, EventLedger
from .mt5_demo_broker import Mt5AccountExposure, Mt5DemoBroker
from .mt5_demo_preflight import Mt5DemoPreflight, Mt5PreflightResult
from .mt5_demo_soak import Mt5DemoSoakConfig, Mt5DemoSoakRunner

GATE_B2_CONFIRM_TEXT = "I_CONFIRM_MT5_DEMO_GATE_B2_NO_REAL_MONEY"


class GateB2Error(Exception):
    """Base exception for Gate B2 orchestration errors."""


class GateB2AuditValidationError(GateB2Error):
    """Raised when post-run audit trail validation fails."""


@dataclass(frozen=True)
class Mt5DemoGateB2Config:
    confirm_text: str
    account_id: str
    account_server: str
    audit_db_dir: Path
    account_company: str | None = None
    total_runs: int = 5
    max_loss_usd: float = 1.0
    max_entries_per_run: int = 2
    max_duration_seconds: float = 300.0
    drain_timeout_seconds: float = 300.0
    max_quote_age_seconds: float = 5.0
    poll_interval_seconds: float = 1.0
    cooldown_seconds: float = 30.0
    inter_run_cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.confirm_text != GATE_B2_CONFIRM_TEXT:
            raise ValueError(f"invalid_confirmation: must match {GATE_B2_CONFIRM_TEXT}")
        if self.total_runs != 5:
            raise ValueError("total_runs must be exactly 5")
        if not self.account_id or not self.account_id.strip():
            raise ValueError("account_id must be non-empty")
        if not self.account_server or not self.account_server.strip():
            raise ValueError("account_server must be non-empty")
        if self.account_company is not None and not self.account_company.strip():
            raise ValueError("account_company must be non-empty when provided")
        if not isinstance(self.audit_db_dir, Path):
            raise ValueError("audit_db_dir must be a Path")

        for name, val in [
            ("max_loss_usd", self.max_loss_usd),
            ("max_duration_seconds", self.max_duration_seconds),
            ("drain_timeout_seconds", self.drain_timeout_seconds),
            ("max_quote_age_seconds", self.max_quote_age_seconds),
            ("poll_interval_seconds", self.poll_interval_seconds),
            ("cooldown_seconds", self.cooldown_seconds),
            ("inter_run_cooldown_seconds", self.inter_run_cooldown_seconds),
        ]:
            if (
                not isinstance(val, (int, float))
                or isinstance(val, bool)
                or not math.isfinite(val)
                or val <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")

        if (
            not isinstance(self.max_entries_per_run, int)
            or isinstance(self.max_entries_per_run, bool)
            or self.max_entries_per_run <= 0
        ):
            raise ValueError("max_entries_per_run must be a positive integer")


@dataclass(frozen=True)
class Mt5DemoGateB2RunSummary:
    run_index: int
    session_id: str
    audit_db_path: Path
    status: str
    entries_completed: int
    realized_pnl_usd: float
    reason: str
    duration_seconds: float
    failure_detail: str | None = None


@dataclass(frozen=True)
class Mt5DemoGateB2BatchResult:
    status: str
    runs_completed: int
    runs_passed: int
    total_realized_pnl_usd: float
    failure_reason: str | None
    runs: tuple[Mt5DemoGateB2RunSummary, ...]
    account_exposure_at_end: Mt5AccountExposure | None


def validate_run_audit(
    audit_db_path: Path, expected_session_id: str, entries_completed: int
) -> float:
    """Validate that the audit log for a single run is strictly valid and complete.

    Returns the sum of realized PnL across all closed positions in the run.
    """
    if not audit_db_path.exists():
        raise GateB2AuditValidationError(f"audit_db_missing: {audit_db_path}")

    store: SQLiteEventStore | None = None
    try:
        store = SQLiteEventStore(audit_db_path, expected_session_id)
        events = store.load_events()
    except Exception as exc:
        raise GateB2AuditValidationError(f"audit_store_load_error: {exc}") from exc
    finally:
        if store is not None:
            store.close()

    if not events:
        raise GateB2AuditValidationError("audit_db_empty")

    # Contiguous 1-indexed sequence check
    for idx, event in enumerate(events, start=1):
        if event.sequence != idx:
            raise GateB2AuditValidationError(
                f"audit_sequence_gap: expected {idx}, got {event.sequence}"
            )
        if event.session_id != expected_session_id:
            raise GateB2AuditValidationError(
                f"audit_session_mismatch: expected {expected_session_id}, got {event.session_id}"
            )

    # First event must be SESSION_STARTED
    if events[0].event_type != AuditEventType.SESSION_STARTED:
        raise GateB2AuditValidationError(f"first_event_not_session_started: {events[0].event_type}")

    # Last event must be SESSION_STOPPED
    if events[-1].event_type != AuditEventType.SESSION_STOPPED:
        raise GateB2AuditValidationError(f"last_event_not_session_stopped: {events[-1].event_type}")

    risk_approved_clients: dict[str, int] = {}  # client_order_id -> sequence
    opened_positions: dict[str, int] = {}       # position_id -> sequence
    closed_positions: dict[str, int] = {}       # position_id -> sequence
    total_pnl = 0.0

    for event in events:
        corr = event.correlation

        if event.event_type == AuditEventType.RISK_APPROVED:
            client_id = corr.client_order_id if corr else None
            if not client_id or not str(client_id).strip():
                raise GateB2AuditValidationError(
                    f"risk_approved_missing_client_order_id: seq={event.sequence}"
                )
            risk_approved_clients[str(client_id).strip()] = event.sequence

        elif event.event_type == AuditEventType.ORDER_SUBMITTED:
            client_id = corr.client_order_id if corr else None
            if not client_id or not str(client_id).strip():
                raise GateB2AuditValidationError(
                    f"order_submitted_missing_client_order_id: seq={event.sequence}"
                )
            clean_client_id = str(client_id).strip()
            prior_risk_seq = risk_approved_clients.get(clean_client_id)
            if prior_risk_seq is None or prior_risk_seq >= event.sequence:
                raise GateB2AuditValidationError(
                    f"order_submitted_without_matching_risk_approval: "
                    f"client_order_id={clean_client_id} seq={event.sequence}"
                )

        elif event.event_type == AuditEventType.POSITION_OPENED:
            pos_id = (
                (corr.position_id if corr else None)
                or event.payload.get("position_id")
            )
            if not pos_id or not str(pos_id).strip():
                raise GateB2AuditValidationError(
                    f"position_opened_missing_position_id: seq={event.sequence}"
                )
            clean_pos_id = str(pos_id).strip()
            if clean_pos_id in opened_positions:
                raise GateB2AuditValidationError(
                    f"duplicate_position_opened: {clean_pos_id} seq={event.sequence}"
                )
            opened_positions[clean_pos_id] = event.sequence

        elif event.event_type == AuditEventType.POSITION_CLOSED:
            pos_id = (
                (corr.position_id if corr else None)
                or event.payload.get("position_id")
            )
            if not pos_id or not str(pos_id).strip():
                raise GateB2AuditValidationError(
                    f"position_closed_missing_position_id: seq={event.sequence}"
                )
            clean_pos_id = str(pos_id).strip()
            if clean_pos_id in closed_positions:
                raise GateB2AuditValidationError(
                    f"duplicate_position_closed: {clean_pos_id} seq={event.sequence}"
                )
            if clean_pos_id not in opened_positions:
                raise GateB2AuditValidationError(
                    f"close_without_matching_open: {clean_pos_id} seq={event.sequence}"
                )

            payload = event.payload
            close_order_id = payload.get("close_order_id")
            close_deal_id = payload.get("close_deal_id")
            exit_reason = payload.get("exit_reason")
            realized_pnl = payload.get("realized_pnl")

            if not close_order_id or not str(close_order_id).strip():
                raise GateB2AuditValidationError("missing_close_order_id_in_audit")
            if not close_deal_id or not str(close_deal_id).strip():
                raise GateB2AuditValidationError("missing_close_deal_id_in_audit")
            if not exit_reason or not str(exit_reason).strip():
                raise GateB2AuditValidationError("missing_exit_reason_in_audit")
            if (
                realized_pnl is None
                or not isinstance(realized_pnl, (int, float))
                or isinstance(realized_pnl, bool)
                or not math.isfinite(realized_pnl)
            ):
                raise GateB2AuditValidationError("invalid_realized_pnl_in_audit")

            total_pnl += float(realized_pnl)
            closed_positions[clean_pos_id] = event.sequence

    if set(opened_positions.keys()) != set(closed_positions.keys()):
        raise GateB2AuditValidationError(
            f"open_closed_correlation_mismatch: opened={sorted(opened_positions.keys())}, "
            f"closed={sorted(closed_positions.keys())}"
        )

    if len(closed_positions) != entries_completed:
        raise GateB2AuditValidationError(
            f"entries_completed_mismatch: audit_closed={len(closed_positions)}, "
            f"expected={entries_completed}"
        )

    return total_pnl


class Mt5DemoGateB2Orchestrator:
    """Orchestrator for Gate B2 reliability runs."""

    def __init__(
        self,
        broker: Mt5DemoBroker,
        config: Mt5DemoGateB2Config,
        preflight: Mt5DemoPreflight,
        runner_factory: Callable[[], Mt5DemoSoakRunner] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._broker = broker
        self._config = config
        self._preflight = preflight
        self._runner_factory = runner_factory or (lambda: Mt5DemoSoakRunner())
        self._sleep_fn = sleep_fn or time.sleep

    def run_batch(self) -> Mt5DemoGateB2BatchResult:
        self._config.audit_db_dir.mkdir(parents=True, exist_ok=True)

        expected_company: str | None = self._config.account_company

        summaries: list[Mt5DemoGateB2RunSummary] = []
        total_pnl = 0.0
        batch_failure_reason: str | None = None
        batch_status = "success"

        for run_idx in range(1, self._config.total_runs + 1):
            session_id = f"gate_b2_run_{run_idx}_{uuid.uuid4().hex[:8]}"
            audit_db_path = self._config.audit_db_dir / f"audit_run_{run_idx}_{session_id}.db"

            # 1. Authoritative DEMO Preflight before EACH run
            try:
                preflight_res: Mt5PreflightResult = self._preflight.run()
            except Exception as exc:
                batch_status = "failed"
                batch_failure_reason = f"preflight_failed_run_{run_idx}: {exc}"
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="preflight_failed",
                        duration_seconds=0.0,
                        failure_detail=str(exc),
                    )
                )
                break

            expected_masked_account = (
                self._config.account_id
                if self._config.account_id.startswith("****")
                else f"****{self._config.account_id[-4:]}"
            )
            if (
                preflight_res.account != expected_masked_account
                and preflight_res.account != self._config.account_id
            ):
                batch_status = "failed"
                batch_failure_reason = (
                    f"preflight_account_mismatch_run_{run_idx}: "
                    f"preflight={preflight_res.account} config={self._config.account_id}"
                )
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="preflight_account_mismatch",
                        duration_seconds=0.0,
                        failure_detail=batch_failure_reason,
                    )
                )
                break

            if preflight_res.server != self._config.account_server:
                batch_status = "failed"
                batch_failure_reason = (
                    f"preflight_server_mismatch_run_{run_idx}: "
                    f"preflight={preflight_res.server} config={self._config.account_server}"
                )
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="preflight_server_mismatch",
                        duration_seconds=0.0,
                        failure_detail=batch_failure_reason,
                    )
                )
                break

            if preflight_res.environment != "demo":
                batch_status = "failed"
                batch_failure_reason = (
                    f"preflight_non_demo_environment_run_{run_idx}: {preflight_res.environment}"
                )
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="preflight_non_demo_environment",
                        duration_seconds=0.0,
                        failure_detail=batch_failure_reason,
                    )
                )
                break

            if not (
                preflight_res.hedging_enabled
                and preflight_res.account_trading_enabled
                and preflight_res.expert_trading_enabled
                and preflight_res.terminal_trading_enabled
            ):
                batch_status = "failed"
                batch_failure_reason = (
                    f"preflight_trading_disabled_run_{run_idx}: "
                    f"hedging={preflight_res.hedging_enabled} "
                    f"account={preflight_res.account_trading_enabled} "
                    f"expert={preflight_res.expert_trading_enabled} "
                    f"terminal={preflight_res.terminal_trading_enabled}"
                )
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="preflight_trading_disabled",
                        duration_seconds=0.0,
                        failure_detail=batch_failure_reason,
                    )
                )
                break

            # Freeze authoritative company identity on first run
            if expected_company is None:
                expected_company = preflight_res.company
            elif preflight_res.company != expected_company:
                batch_status = "failed"
                batch_failure_reason = (
                    f"preflight_company_mismatch_run_{run_idx}: "
                    f"preflight={preflight_res.company} expected={expected_company}"
                )
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="preflight_company_mismatch",
                        duration_seconds=0.0,
                        failure_detail=batch_failure_reason,
                    )
                )
                break

            # 2. Verify flat whole-account exposure before each run
            try:
                pre_run_exposure = self._broker.get_account_exposure()
            except Exception as exc:
                batch_status = "failed"
                batch_failure_reason = f"pre_run_exposure_check_error_run_{run_idx}: {exc}"
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="pre_run_exposure_check_error",
                        duration_seconds=0.0,
                        failure_detail=str(exc),
                    )
                )
                break

            if pre_run_exposure.account_id != self._config.account_id:
                batch_status = "failed"
                batch_failure_reason = (
                    f"exposure_account_mismatch_run_{run_idx}: "
                    f"broker={pre_run_exposure.account_id} config={self._config.account_id}"
                )
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="exposure_account_mismatch",
                        duration_seconds=0.0,
                        failure_detail=batch_failure_reason,
                    )
                )
                break

            if pre_run_exposure.server != self._config.account_server:
                batch_status = "failed"
                batch_failure_reason = (
                    f"exposure_server_mismatch_run_{run_idx}: "
                    f"broker={pre_run_exposure.server} config={self._config.account_server}"
                )
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="exposure_server_mismatch",
                        duration_seconds=0.0,
                        failure_detail=batch_failure_reason,
                    )
                )
                break

            if pre_run_exposure.company != expected_company:
                batch_status = "failed"
                batch_failure_reason = (
                    f"exposure_company_mismatch_run_{run_idx}: "
                    f"broker={pre_run_exposure.company} expected={expected_company}"
                )
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="exposure_company_mismatch",
                        duration_seconds=0.0,
                        failure_detail=batch_failure_reason,
                    )
                )
                break

            if not pre_run_exposure.is_flat:
                batch_status = "failed"
                batch_failure_reason = f"pre_run_exposure_not_flat_run_{run_idx}"
                open_ct = len(pre_run_exposure.open_positions)
                pending_ct = len(pre_run_exposure.pending_orders)
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="pre_run_exposure_not_flat",
                        duration_seconds=0.0,
                        failure_detail=f"open_positions={open_ct}, pending_orders={pending_ct}",
                    )
                )
                break

            # 3. Build single soak config with preserved Gate B1 policy
            soak_config = Mt5DemoSoakConfig(
                confirmation="I_CONFIRM_MT5_DEMO_SOAK_NO_REAL_MONEY",
                max_loss_usd=self._config.max_loss_usd,
                max_entries=self._config.max_entries_per_run,
                max_duration_seconds=self._config.max_duration_seconds,
                drain_timeout_seconds=self._config.drain_timeout_seconds,
                max_quote_age_seconds=self._config.max_quote_age_seconds,
                poll_interval_seconds=self._config.poll_interval_seconds,
                cooldown_seconds=self._config.cooldown_seconds,
            )

            run_start = time.perf_counter()
            runner = self._runner_factory()
            store: SQLiteEventStore | None = None

            try:
                store = SQLiteEventStore(audit_db_path, session_id)
                ledger = EventLedger(session_id, durable_store=store)
                soak_result = runner.run(soak_config, broker=self._broker, ledger=ledger)
            except KeyboardInterrupt:
                run_dur = time.perf_counter() - run_start
                batch_status = "aborted"
                batch_failure_reason = "interrupted_by_user"
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="aborted",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="interrupted_by_user",
                        duration_seconds=run_dur,
                        failure_detail="KeyboardInterrupt",
                    )
                )
                break
            except Exception as exc:
                run_dur = time.perf_counter() - run_start
                batch_status = "failed"
                batch_failure_reason = f"soak_runner_exception_run_{run_idx}: {exc}"
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=0,
                        realized_pnl_usd=0.0,
                        reason="runner_exception",
                        duration_seconds=run_dur,
                        failure_detail=str(exc),
                    )
                )
                break
            finally:
                if store is not None:
                    store.close()

            run_dur = time.perf_counter() - run_start

            if soak_result.status != "completed":
                batch_status = "failed"
                batch_failure_reason = f"soak_run_{run_idx}_failed: {soak_result.stop_reason}"
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status=soak_result.status,
                        entries_completed=soak_result.entries_completed,
                        realized_pnl_usd=0.0,
                        reason=soak_result.stop_reason,
                        duration_seconds=run_dur,
                        failure_detail=soak_result.error_message or soak_result.stop_reason,
                    )
                )
                break

            # 4. Verify post-run exposure is flat
            try:
                post_run_exposure = self._broker.get_account_exposure()
                if not post_run_exposure.is_flat:
                    batch_status = "failed"
                    batch_failure_reason = f"post_run_exposure_not_flat_run_{run_idx}"
                    open_ct = len(post_run_exposure.open_positions)
                    pending_ct = len(post_run_exposure.pending_orders)
                    summaries.append(
                        Mt5DemoGateB2RunSummary(
                            run_index=run_idx,
                            session_id=session_id,
                            audit_db_path=audit_db_path,
                            status="failed",
                            entries_completed=soak_result.entries_completed,
                            realized_pnl_usd=0.0,
                            reason="post_run_exposure_not_flat",
                            duration_seconds=run_dur,
                            failure_detail=f"open_positions={open_ct}, pending_orders={pending_ct}",
                        )
                    )
                    break
            except Exception as exc:
                batch_status = "failed"
                batch_failure_reason = f"post_run_exposure_check_error_run_{run_idx}: {exc}"
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=soak_result.entries_completed,
                        realized_pnl_usd=0.0,
                        reason="post_run_exposure_check_error",
                        duration_seconds=run_dur,
                        failure_detail=str(exc),
                    )
                )
                break

            # 5. Validate audit trail for completed run
            try:
                run_pnl = validate_run_audit(
                    audit_db_path, session_id, soak_result.entries_completed
                )
            except Exception as exc:
                batch_status = "failed"
                batch_failure_reason = f"audit_validation_failed_run_{run_idx}: {exc}"
                summaries.append(
                    Mt5DemoGateB2RunSummary(
                        run_index=run_idx,
                        session_id=session_id,
                        audit_db_path=audit_db_path,
                        status="failed",
                        entries_completed=soak_result.entries_completed,
                        realized_pnl_usd=0.0,
                        reason="audit_validation_failed",
                        duration_seconds=run_dur,
                        failure_detail=str(exc),
                    )
                )
                break

            total_pnl += run_pnl
            summaries.append(
                Mt5DemoGateB2RunSummary(
                    run_index=run_idx,
                    session_id=session_id,
                    audit_db_path=audit_db_path,
                    status="success",
                    entries_completed=soak_result.entries_completed,
                    realized_pnl_usd=run_pnl,
                    reason=soak_result.stop_reason,
                    duration_seconds=run_dur,
                    failure_detail=None,
                )
            )

            # Inter-run cooldown between runs if more runs remain
            if run_idx < self._config.total_runs:
                self._sleep_fn(self._config.inter_run_cooldown_seconds)

        if batch_status != "success":
            latest_exposure: Mt5AccountExposure | None = None
            try:
                latest_exposure = self._broker.get_account_exposure()
            except Exception:
                pass
            return Mt5DemoGateB2BatchResult(
                status=batch_status,
                runs_completed=len(summaries),
                runs_passed=sum(1 for s in summaries if s.status == "success"),
                total_realized_pnl_usd=total_pnl,
                failure_reason=batch_failure_reason,
                runs=tuple(summaries),
                account_exposure_at_end=latest_exposure,
            )

        # 6. Final independent whole-account exposure check (certifies completed batch)
        final_exposure: Mt5AccountExposure | None = None
        try:
            final_exposure = self._broker.get_account_exposure()
        except Exception as exc:
            return Mt5DemoGateB2BatchResult(
                status="failed",
                runs_completed=len(summaries),
                runs_passed=sum(1 for s in summaries if s.status == "success"),
                total_realized_pnl_usd=total_pnl,
                failure_reason=f"final_exposure_check_failed: {exc}",
                runs=tuple(summaries),
                account_exposure_at_end=None,
            )

        if not final_exposure.is_flat:
            open_ct = len(final_exposure.open_positions)
            pending_ct = len(final_exposure.pending_orders)
            return Mt5DemoGateB2BatchResult(
                status="failed",
                runs_completed=len(summaries),
                runs_passed=sum(1 for s in summaries if s.status == "success"),
                total_realized_pnl_usd=total_pnl,
                failure_reason=(
                    f"final_exposure_not_flat: "
                    f"open_positions={open_ct}, pending_orders={pending_ct}"
                ),
                runs=tuple(summaries),
                account_exposure_at_end=final_exposure,
            )

        runs_passed = sum(1 for s in summaries if s.status == "success")

        return Mt5DemoGateB2BatchResult(
            status=batch_status,
            runs_completed=len(summaries),
            runs_passed=runs_passed,
            total_realized_pnl_usd=total_pnl,
            failure_reason=batch_failure_reason,
            runs=tuple(summaries),
            account_exposure_at_end=final_exposure,
        )