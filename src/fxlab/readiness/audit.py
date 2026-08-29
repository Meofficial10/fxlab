"""Evidence-backed Phase 20 readiness policy for the Phase 1-19 baseline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .model import (
    EvidenceInventory,
    EvidenceKind,
    EvidenceReference,
    ReadinessCheck,
    ReadinessDomain,
    ReadinessEvidence,
    ReadinessReport,
    ReadinessStatus,
    ReadinessTarget,
    TargetGate,
    evaluate_readiness,
)

AUDITED_SYSTEM_COMMIT = "f3ce10f32f17b6f7dbe793a40967ad8c4e2e3143"

_VERIFIED_EVIDENCE_REFERENCES = (
    (EvidenceKind.ADR, "docs/03-paper-trading-architecture.md::research-r7"),
    (EvidenceKind.ADR, "docs/03-paper-trading-architecture.md::research-r8"),
    (EvidenceKind.ADR, "docs/adr/0001-p4-no-go-smc-rule-baseline.md"),
    (EvidenceKind.BROKER_DESCRIPTOR, "oanda-v20::2"),
    (EvidenceKind.COMMIT, AUDITED_SYSTEM_COMMIT),
    (EvidenceKind.SEALED_TEST_RESULT, "docs/AUDIT-2026-08-25.md::p4-no-go"),
    (EvidenceKind.TEST, "tests/test_control_channel.py"),
    (
        EvidenceKind.TEST,
        "tests/test_data_provider.py::"
        "test_exact_close_boundary_is_available_and_future_bar_is_excluded",
    ),
    (EvidenceKind.TEST, "tests/test_dukascopy_provider.py"),
    (
        EvidenceKind.TEST,
        "tests/test_dukascopy_provider.py::"
        "test_forming_candle_is_excluded_at_exact_point_in_time_boundary",
    ),
    (EvidenceKind.TEST, "tests/test_durable_event_store.py"),
    (EvidenceKind.TEST, "tests/test_monitoring.py"),
    (EvidenceKind.TEST, "tests/test_oanda_demo_broker.py"),
    (
        EvidenceKind.TEST,
        "tests/test_oanda_demo_broker.py::"
        "test_verified_practice_metadata_provides_explicit_usd_pip_valuation",
    ),
    (EvidenceKind.TEST, "tests/test_operational_security.py"),
    (EvidenceKind.TEST, "tests/test_order_manager.py"),
    (EvidenceKind.TEST, "tests/test_paper_app.py"),
    (EvidenceKind.TEST, "tests/test_paper_broker.py"),
    (
        EvidenceKind.TEST,
        "tests/test_paper_broker.py::test_cost_model_does_not_double_count_real_spread",
    ),
    (EvidenceKind.TEST, "tests/test_reconciliation.py"),
    (
        EvidenceKind.TEST,
        "tests/test_reconciliation.py::"
        "test_exact_identity_is_required_and_heuristic_similarity_is_ignored",
    ),
    (
        EvidenceKind.TEST,
        "tests/test_reconciliation.py::"
        "test_filled_order_repairs_exact_record_reflection_and_reservation",
    ),
    (
        EvidenceKind.TEST,
        "tests/test_reconciliation.py::"
        "test_reconcile_does_not_execute_trading_side_effects",
    ),
    (EvidenceKind.TEST, "tests/test_recovery.py"),
    (EvidenceKind.TEST, "tests/test_risk_engine.py"),
    (EvidenceKind.TEST, "tests/test_runtime_control.py"),
    (EvidenceKind.TEST, "tests/test_service.py"),
    (
        EvidenceKind.TEST,
        "tests/test_service.py::test_pause_resume_service_remains_structurally_observation_only",
    ),
    (EvidenceKind.TEST, "tests/test_valuation.py"),
)


class FaultEvidenceClassification(StrEnum):
    DIRECTLY_TESTED = "directly_tested"
    PREEXISTING_TEST_EVIDENCE = "preexisting_test_evidence"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class FaultInjectionEvidence:
    """Named deterministic fault evidence; never a substitute for the referenced test."""

    scenario_id: str
    classification: FaultEvidenceClassification
    reason_code: str
    evidence_reference: EvidenceReference

    def __post_init__(self) -> None:
        if not isinstance(self.classification, FaultEvidenceClassification):
            raise ValueError("invalid fault evidence classification")
        if not self.scenario_id or not self.reason_code:
            raise ValueError("invalid fault evidence identifier")
        if (
            not isinstance(self.evidence_reference, EvidenceReference)
            or self.evidence_reference.kind is not EvidenceKind.TEST
        ):
            raise ValueError("invalid fault evidence reference")

    @property
    def status(self) -> ReadinessStatus:
        return (
            ReadinessStatus.UNVERIFIED
            if self.classification is FaultEvidenceClassification.UNVERIFIED
            else ReadinessStatus.PASS
        )


def _fault_test_reference(value: str) -> EvidenceReference:
    return EvidenceReference.from_text(EvidenceKind.TEST, value)


FAULT_INJECTION_EVIDENCE = (
    FaultInjectionEvidence(
        "submission_before_acknowledgement",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "reservation_retained_reconciliation_required",
        _fault_test_reference(
            "tests/test_order_manager.py::"
            "test_submission_exception_is_indeterminate_retains_reservation_and_latches_kill"
        ),
    ),
    FaultInjectionEvidence(
        "fill_before_reflection",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "preexisting_exact_reflection_repair_verified",
        _fault_test_reference(
            "tests/test_reconciliation.py::"
            "test_filled_order_repairs_exact_record_reflection_and_reservation"
        ),
    ),
    FaultInjectionEvidence(
        "close_accounting_interruption",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "close_accounting_uncertain",
        _fault_test_reference("tests/test_reconciliation.py::test_close_tail_is_never_replayed"),
    ),
    FaultInjectionEvidence(
        "audit_failure",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "post_dispatch_audit_failure_indeterminate",
        _fault_test_reference(
            "tests/test_order_manager.py::test_audit_failure_after_broker_call_is_indeterminate"
        ),
    ),
    FaultInjectionEvidence(
        "checkpoint_failure",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "checkpoint_failure_rolls_back",
        _fault_test_reference(
            "tests/test_reconciliation.py::test_checkpoint_failure_rolls_back_component_state"
        ),
    ),
    FaultInjectionEvidence(
        "sqlite_corruption",
        FaultEvidenceClassification.DIRECTLY_TESTED,
        "sqlite_corruption_detected",
        _fault_test_reference(
            "tests/test_readiness_fault_injection.py::"
            "test_sqlite_corruption_is_detected_without_fresh_fallback"
        ),
    ),
    FaultInjectionEvidence(
        "disk_store_failure",
        FaultEvidenceClassification.UNVERIFIED,
        "physical_disk_full_not_validated",
        _fault_test_reference(
            "tests/test_readiness_fault_injection.py::"
            "test_physical_disk_full_remains_honestly_unverified"
        ),
    ),
    FaultInjectionEvidence(
        "stale_future_corrupt_data",
        FaultEvidenceClassification.UNVERIFIED,
        "combined_corrupt_data_fault_unverified",
        _fault_test_reference(
            "tests/test_readiness_fault_injection.py::"
            "test_future_and_stale_conversion_data_fail_closed"
        ),
    ),
    FaultInjectionEvidence(
        "provider_failure",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "provider_partial_query_discarded_no_retry",
        _fault_test_reference(
            "tests/test_dukascopy_provider.py::"
            "test_failed_page_discards_accumulated_rows_and_does_not_retry"
        ),
    ),
    FaultInjectionEvidence(
        "broker_timeout_uncertainty",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "broker_timeout_indeterminate_no_retry",
        _fault_test_reference(
            "tests/test_oanda_demo_broker.py::test_submission_uncertainty_is_not_retried"
        ),
    ),
    FaultInjectionEvidence(
        "control_failure",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "control_failure_requests_shutdown",
        _fault_test_reference(
            "tests/test_service.py::test_listener_failure_requests_fail_closed_shutdown"
        ),
    ),
    FaultInjectionEvidence(
        "logging_failure",
        FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE,
        "preexisting_logging_failure_shutdown_verified",
        _fault_test_reference(
            "tests/test_service.py::test_runtime_logging_failure_requests_fail_closed_shutdown"
        ),
    ),
    FaultInjectionEvidence(
        "secret_read_failure",
        FaultEvidenceClassification.DIRECTLY_TESTED,
        "secret_preflight_failed",
        _fault_test_reference(
            "tests/test_readiness_fault_injection.py::"
            "test_secret_read_failure_prevents_credential_material_creation"
        ),
    ),
    FaultInjectionEvidence(
        "instance_lock_contention",
        FaultEvidenceClassification.DIRECTLY_TESTED,
        "second_instance_rejected",
        _fault_test_reference(
            "tests/test_readiness_fault_injection.py::"
            "test_instance_lock_contention_fails_before_second_owner"
        ),
    ),
    FaultInjectionEvidence(
        "shutdown_during_serialized_cycle",
        FaultEvidenceClassification.UNVERIFIED,
        "full_active_cycle_shutdown_unverified",
        _fault_test_reference(
            "tests/test_service.py::test_signal_handler_never_enters_session_cycle_lock"
        ),
    ),
    FaultInjectionEvidence(
        "stalled_authentication",
        FaultEvidenceClassification.DIRECTLY_TESTED,
        "authentication_stall_shutdown_bounded",
        _fault_test_reference(
            "tests/test_readiness_fault_injection.py::"
            "test_stalled_authentication_does_not_prevent_bounded_control_shutdown"
        ),
    ),
)

CURRENT_TARGET_GATES = (
    TargetGate(
        ReadinessTarget.DETERMINISTIC_PAPER,
        (
            "data_integrity",
            "execution_model_deterministic",
            "risk_controls",
            "paper_broker_contract",
            "paper_recovery",
            "paper_reconciliation",
            "runtime_controls",
            "monitoring_contract",
            "network_fail_closed",
            "storage_integrity",
        ),
    ),
    TargetGate(
        ReadinessTarget.OBSERVATION_SERVICE,
        (
            "observation_only_service",
            "operational_security_local",
            "secret_file_local",
            "paper_recovery",
            "runtime_controls",
            "monitoring_contract",
            "storage_integrity",
        ),
    ),
    TargetGate(
        ReadinessTarget.OANDA_PRACTICE_ADAPTER,
        (
            "oanda_practice_contract",
            "risk_controls",
            "execution_valuation",
            "network_fail_closed",
        ),
    ),
    TargetGate(
        ReadinessTarget.OANDA_PRACTICE_FORWARD_STRATEGY,
        (
            "research_edge",
            "research_r7",
            "research_r8",
            "oanda_practice_contract",
            "external_reconciliation",
            "execution_model_broker_forward",
            "risk_controls",
        ),
    ),
    TargetGate(
        ReadinessTarget.LIVE_MONEY,
        (
            "research_edge",
            "research_r7",
            "research_r8",
            "live_broker",
            "external_reconciliation",
            "execution_model_live",
            "risk_controls",
            "secret_management_live",
            "deployment_production",
            "clock_deployment",
            "storage_production",
            "network_fail_closed",
        ),
    ),
)


def build_current_readiness_report(
    *, as_of: datetime, report_implementation_commit: str | None = None
) -> ReadinessReport:
    """Evaluate named committed evidence; it does not authorize any execution."""

    checks = _current_checks()
    inventory = _current_evidence_inventory()
    return evaluate_readiness(
        audited_system_commit=AUDITED_SYSTEM_COMMIT,
        report_implementation_commit=report_implementation_commit,
        as_of=as_of,
        checks=checks,
        gates=CURRENT_TARGET_GATES,
        evidence_inventory=inventory,
    )


def _evidence(kind: EvidenceKind, reference: str) -> ReadinessEvidence:
    return ReadinessEvidence(
        kind,
        EvidenceReference.from_text(kind, reference),
        AUDITED_SYSTEM_COMMIT,
    )


def _current_evidence_inventory() -> EvidenceInventory:
    return EvidenceInventory(
        tuple(_evidence(kind, reference) for kind, reference in _VERIFIED_EVIDENCE_REFERENCES)
    )


def _check(
    check_id: str,
    domain: ReadinessDomain,
    status: ReadinessStatus,
    reason: str,
    *evidence: ReadinessEvidence,
) -> ReadinessCheck:
    return ReadinessCheck(check_id, domain, status, reason, tuple(evidence))


def _current_checks() -> tuple[ReadinessCheck, ...]:
    test = EvidenceKind.TEST
    return (
        _check(
            "data_integrity",
            ReadinessDomain.DATA,
            ReadinessStatus.PASS,
            "provider_point_in_time_verified",
            _evidence(
                test,
                "tests/test_data_provider.py::"
                "test_exact_close_boundary_is_available_and_future_bar_is_excluded",
            ),
            _evidence(
                test,
                "tests/test_dukascopy_provider.py::"
                "test_forming_candle_is_excluded_at_exact_point_in_time_boundary",
            ),
        ),
        _check(
            "execution_model_deterministic",
            ReadinessDomain.EXECUTION_MODEL,
            ReadinessStatus.PASS,
            "deterministic_model_contract_verified",
            _evidence(
                test,
                "tests/test_paper_broker.py::test_cost_model_does_not_double_count_real_spread",
            ),
            _evidence(test, "tests/test_valuation.py"),
        ),
        _check(
            "execution_valuation",
            ReadinessDomain.EXECUTION_MODEL,
            ReadinessStatus.PASS,
            "dynamic_valuation_verified",
            _evidence(test, "tests/test_valuation.py"),
            _evidence(
                test,
                "tests/test_oanda_demo_broker.py::"
                "test_verified_practice_metadata_provides_explicit_usd_pip_valuation",
            ),
        ),
        _check(
            "execution_model_broker_forward",
            ReadinessDomain.EXECUTION_MODEL,
            ReadinessStatus.UNVERIFIED,
            "broker_forward_evidence_missing",
            _evidence(EvidenceKind.COMMIT, AUDITED_SYSTEM_COMMIT),
        ),
        _check(
            "execution_model_live",
            ReadinessDomain.EXECUTION_MODEL,
            ReadinessStatus.BLOCKED,
            "live_execution_realism_incomplete",
            _evidence(EvidenceKind.COMMIT, AUDITED_SYSTEM_COMMIT),
        ),
        _check(
            "risk_controls",
            ReadinessDomain.RISK,
            ReadinessStatus.PASS,
            "risk_invariants_verified",
            _evidence(test, "tests/test_risk_engine.py"),
            _evidence(test, "tests/test_order_manager.py"),
        ),
        _check(
            "paper_broker_contract",
            ReadinessDomain.BROKER,
            ReadinessStatus.PASS,
            "paper_broker_verified",
            _evidence(test, "tests/test_paper_broker.py"),
        ),
        _check(
            "oanda_practice_contract",
            ReadinessDomain.BROKER,
            ReadinessStatus.PASS,
            "practice_adapter_contract_verified",
            _evidence(EvidenceKind.BROKER_DESCRIPTOR, "oanda-v20::2"),
            _evidence(test, "tests/test_oanda_demo_broker.py"),
        ),
        _check(
            "live_broker",
            ReadinessDomain.BROKER,
            ReadinessStatus.BLOCKED,
            "live_broker_not_implemented",
            _evidence(EvidenceKind.BROKER_DESCRIPTOR, "oanda-v20::2"),
        ),
        _check(
            "paper_recovery",
            ReadinessDomain.RECOVERY,
            ReadinessStatus.PASS,
            "paper_recovery_verified",
            _evidence(test, "tests/test_recovery.py"),
        ),
        _check(
            "paper_reconciliation",
            ReadinessDomain.RECONCILIATION,
            ReadinessStatus.PASS,
            "paper_reconciliation_verified",
            _evidence(
                test,
                "tests/test_reconciliation.py::"
                "test_filled_order_repairs_exact_record_reflection_and_reservation",
            ),
            _evidence(
                test,
                "tests/test_reconciliation.py::"
                "test_exact_identity_is_required_and_heuristic_similarity_is_ignored",
            ),
            _evidence(
                test,
                "tests/test_reconciliation.py::"
                "test_reconcile_does_not_execute_trading_side_effects",
            ),
        ),
        _check(
            "external_reconciliation",
            ReadinessDomain.RECONCILIATION,
            ReadinessStatus.BLOCKED,
            "external_reconciliation_not_implemented",
            _evidence(test, "tests/test_reconciliation.py"),
            _evidence(EvidenceKind.COMMIT, AUDITED_SYSTEM_COMMIT),
        ),
        _check(
            "runtime_controls",
            ReadinessDomain.RUNTIME_CONTROL,
            ReadinessStatus.PASS,
            "runtime_controls_verified",
            _evidence(test, "tests/test_runtime_control.py"),
            _evidence(test, "tests/test_service.py"),
        ),
        _check(
            "monitoring_contract",
            ReadinessDomain.MONITORING,
            ReadinessStatus.PASS,
            "monitoring_sources_verified",
            _evidence(test, "tests/test_monitoring.py"),
        ),
        _check(
            "observation_only_service",
            ReadinessDomain.OPERATIONAL_SECURITY,
            ReadinessStatus.PASS,
            "observation_only_barriers_verified",
            _evidence(
                test,
                "tests/test_service.py::"
                "test_pause_resume_service_remains_structurally_observation_only",
            ),
            _evidence(test, "tests/test_paper_app.py"),
        ),
        _check(
            "operational_security_local",
            ReadinessDomain.OPERATIONAL_SECURITY,
            ReadinessStatus.PASS,
            "local_control_boundary_verified",
            _evidence(test, "tests/test_control_channel.py"),
            _evidence(test, "tests/test_operational_security.py"),
        ),
        _check(
            "secret_file_local",
            ReadinessDomain.SECRET_MANAGEMENT,
            ReadinessStatus.PASS,
            "local_secret_file_contract_verified",
            _evidence(test, "tests/test_operational_security.py"),
        ),
        _check(
            "secret_management_live",
            ReadinessDomain.SECRET_MANAGEMENT,
            ReadinessStatus.UNVERIFIED,
            "live_secret_controls_unverified",
            _evidence(EvidenceKind.COMMIT, AUDITED_SYSTEM_COMMIT),
        ),
        _check(
            "deployment_production",
            ReadinessDomain.DEPLOYMENT,
            ReadinessStatus.BLOCKED,
            "production_deployment_evidence_missing",
            _evidence(EvidenceKind.COMMIT, AUDITED_SYSTEM_COMMIT),
        ),
        _check(
            "clock_deployment",
            ReadinessDomain.CLOCK_TIME,
            ReadinessStatus.UNVERIFIED,
            "clock_synchronization_unverified",
            _evidence(EvidenceKind.COMMIT, AUDITED_SYSTEM_COMMIT),
        ),
        _check(
            "network_fail_closed",
            ReadinessDomain.NETWORK_FAILURE,
            ReadinessStatus.PASS,
            "network_failures_verified",
            _evidence(test, "tests/test_dukascopy_provider.py"),
            _evidence(test, "tests/test_oanda_demo_broker.py"),
        ),
        _check(
            "storage_integrity",
            ReadinessDomain.STORAGE,
            ReadinessStatus.PASS,
            "durable_store_integrity_verified",
            _evidence(test, "tests/test_durable_event_store.py"),
            _evidence(test, "tests/test_recovery.py"),
        ),
        _check(
            "storage_production",
            ReadinessDomain.STORAGE,
            ReadinessStatus.UNVERIFIED,
            "disk_full_backup_restore_unverified",
            _evidence(EvidenceKind.COMMIT, AUDITED_SYSTEM_COMMIT),
        ),
        _check(
            "research_edge",
            ReadinessDomain.RESEARCH,
            ReadinessStatus.BLOCKED,
            "research_p4_no_go",
            _evidence(
                EvidenceKind.SEALED_TEST_RESULT,
                "docs/AUDIT-2026-08-25.md::p4-no-go",
            ),
            _evidence(
                EvidenceKind.ADR,
                "docs/adr/0001-p4-no-go-smc-rule-baseline.md",
            ),
        ),
        _check(
            "research_r7",
            ReadinessDomain.RESEARCH,
            ReadinessStatus.BLOCKED,
            "r7_not_eligible",
            _evidence(EvidenceKind.ADR, "docs/03-paper-trading-architecture.md::research-r7"),
        ),
        _check(
            "research_r8",
            ReadinessDomain.RESEARCH,
            ReadinessStatus.BLOCKED,
            "r8_not_eligible",
            _evidence(EvidenceKind.ADR, "docs/03-paper-trading-architecture.md::research-r8"),
        ),
    )
