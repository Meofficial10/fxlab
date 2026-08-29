"""Immutable, deterministic contracts for explicit system-readiness audits."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

READINESS_SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TEST_PATH_PATTERN = re.compile(r"^tests/test_[a-z0-9_]+\.py$")
_TEST_NODE_PATTERN = re.compile(r"^test_[A-Za-z0-9_]+(?:\[[A-Za-z0-9_.-]+\])?$")
_DOC_PATH_PATTERN = re.compile(
    r"^docs/(?:adr/[0-9]{4}-[a-z0-9-]+\.md|AUDIT-[0-9]{4}-[0-9]{2}-[0-9]{2}\.md|"
    r"03-paper-trading-architecture\.md)$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ReadinessDomain(StrEnum):
    RESEARCH = "research"
    DATA = "data"
    EXECUTION_MODEL = "execution_model"
    RISK = "risk"
    BROKER = "broker"
    RECOVERY = "recovery"
    RECONCILIATION = "reconciliation"
    RUNTIME_CONTROL = "runtime_control"
    MONITORING = "monitoring"
    OPERATIONAL_SECURITY = "operational_security"
    SECRET_MANAGEMENT = "secret_management"
    DEPLOYMENT = "deployment"
    CLOCK_TIME = "clock_time"
    NETWORK_FAILURE = "network_failure"
    STORAGE = "storage"


class ReadinessStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"
    NOT_APPLICABLE = "not_applicable"


class ReadinessTarget(StrEnum):
    DETERMINISTIC_PAPER = "deterministic_paper"
    OBSERVATION_SERVICE = "observation_service"
    OANDA_PRACTICE_ADAPTER = "oanda_practice_adapter"
    OANDA_PRACTICE_FORWARD_STRATEGY = "oanda_practice_forward_strategy"
    LIVE_MONEY = "live_money"


class ReadinessVerdict(StrEnum):
    GO = "go"
    NO_GO = "no_go"


class EvidenceKind(StrEnum):
    COMMIT = "commit"
    TEST = "test"
    EVENT_SEQUENCE = "event_sequence"
    CHECKPOINT_FINGERPRINT = "checkpoint_fingerprint"
    DATASET_ID = "dataset_id"
    PROVIDER_DESCRIPTOR = "provider_descriptor"
    BROKER_DESCRIPTOR = "broker_descriptor"
    CONFIGURATION_FINGERPRINT = "configuration_fingerprint"
    EXPERIMENT_ID = "experiment_id"
    ADR = "adr"
    SEALED_TEST_RESULT = "sealed_test_result"
    OPERATIONAL_SERVICE_RESULT = "operational_service_result"


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Strict, canonical reference to evidence named by an approved kind."""

    kind: EvidenceKind
    identifier: str
    sub_identifier: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("invalid evidence reference kind")
        if not isinstance(self.identifier, str) or len(self.identifier) > 160:
            raise ValueError("invalid evidence reference")
        if self.sub_identifier is not None and (
            not isinstance(self.sub_identifier, str) or len(self.sub_identifier) > 128
        ):
            raise ValueError("invalid evidence reference")
        if not _reference_shape_is_valid(self.kind, self.identifier, self.sub_identifier):
            raise ValueError("invalid evidence reference")

    @classmethod
    def from_text(cls, kind: EvidenceKind, value: str) -> EvidenceReference:
        if not isinstance(value, str) or value.count("::") > 1:
            raise ValueError("invalid evidence reference")
        identifier, separator, sub_identifier = value.partition("::")
        return cls(kind, identifier, sub_identifier if separator else None)

    @property
    def canonical_text(self) -> str:
        if self.sub_identifier is None:
            return self.identifier
        return f"{self.identifier}::{self.sub_identifier}"


def _reference_shape_is_valid(
    kind: EvidenceKind, identifier: str, sub_identifier: str | None
) -> bool:
    if kind is EvidenceKind.COMMIT:
        return _COMMIT_PATTERN.fullmatch(identifier) is not None and sub_identifier is None
    if kind is EvidenceKind.TEST:
        return _TEST_PATH_PATTERN.fullmatch(identifier) is not None and (
            sub_identifier is None or _TEST_NODE_PATTERN.fullmatch(sub_identifier) is not None
        )
    if kind in {EvidenceKind.ADR, EvidenceKind.SEALED_TEST_RESULT}:
        return _DOC_PATH_PATTERN.fullmatch(identifier) is not None and (
            sub_identifier is None
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", sub_identifier) is not None
        )
    if kind in {EvidenceKind.CHECKPOINT_FINGERPRINT, EvidenceKind.CONFIGURATION_FINGERPRINT}:
        return _SHA256_PATTERN.fullmatch(identifier) is not None and sub_identifier is None
    if kind in {EvidenceKind.PROVIDER_DESCRIPTOR, EvidenceKind.BROKER_DESCRIPTOR}:
        return _SAFE_TOKEN_PATTERN.fullmatch(identifier) is not None and (
            sub_identifier is not None
            and _SAFE_TOKEN_PATTERN.fullmatch(sub_identifier) is not None
        )
    return _SAFE_TOKEN_PATTERN.fullmatch(identifier) is not None and (
        sub_identifier is None or _SAFE_TOKEN_PATTERN.fullmatch(sub_identifier) is not None
    )


def _require_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _require_commit(value: str | None, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _COMMIT_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _require_utc(value: datetime | None, label: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ReadinessEvidence:
    kind: EvidenceKind
    reference: EvidenceReference
    audited_system_commit: str
    observed_at: datetime | None = None
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("invalid evidence kind")
        if (
            not isinstance(self.reference, EvidenceReference)
            or self.reference.kind is not self.kind
        ):
            raise ValueError("invalid evidence reference")
        _require_commit(self.audited_system_commit, "evidence audited-system commit")
        observed = _require_utc(self.observed_at, "evidence observed_at", optional=True)
        valid = _require_utc(self.valid_until, "evidence valid_until", optional=True)
        if observed is not None and valid is not None and valid < observed:
            raise ValueError("evidence validity ends before observation")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "valid_until", valid)


@dataclass(frozen=True, slots=True)
class EvidenceInventory:
    """Explicit trusted inventory of evidence available to one evaluation."""

    evidence: tuple[ReadinessEvidence, ...]

    def __post_init__(self) -> None:
        evidence = tuple(self.evidence)
        if not all(isinstance(item, ReadinessEvidence) for item in evidence):
            raise ValueError("invalid evidence inventory")
        ordered = tuple(sorted(set(evidence), key=_evidence_sort_key))
        object.__setattr__(self, "evidence", ordered)

    def contains(self, item: ReadinessEvidence) -> bool:
        return item in self.evidence


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    check_id: str
    domain: ReadinessDomain
    status: ReadinessStatus
    reason_code: str
    evidence: tuple[ReadinessEvidence, ...] = ()

    def __post_init__(self) -> None:
        _require_id(self.check_id, "readiness check ID")
        _require_id(self.reason_code, "readiness reason code")
        if not isinstance(self.domain, ReadinessDomain):
            raise ValueError("invalid readiness domain")
        if not isinstance(self.status, ReadinessStatus):
            raise ValueError("invalid readiness status")
        evidence = tuple(self.evidence)
        if not all(isinstance(item, ReadinessEvidence) for item in evidence):
            raise ValueError("invalid readiness evidence")
        object.__setattr__(self, "evidence", tuple(sorted(evidence, key=_evidence_sort_key)))


@dataclass(frozen=True, slots=True)
class TargetGate:
    target: ReadinessTarget
    mandatory_check_ids: tuple[str, ...]
    not_applicable_allowed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.target, ReadinessTarget):
            raise ValueError("invalid readiness target")
        mandatory = tuple(self.mandatory_check_ids)
        allowed = tuple(self.not_applicable_allowed)
        for check_id in (*mandatory, *allowed):
            _require_id(check_id, "readiness check ID")
        if len(set(mandatory)) != len(mandatory):
            raise ValueError("duplicate mandatory check ID")
        if not set(allowed).issubset(mandatory):
            raise ValueError("NOT_APPLICABLE allowance must name a mandatory check")
        object.__setattr__(self, "mandatory_check_ids", tuple(sorted(mandatory)))
        object.__setattr__(self, "not_applicable_allowed", tuple(sorted(set(allowed))))


@dataclass(frozen=True, slots=True)
class ReadinessTargetResult:
    target: ReadinessTarget
    verdict: ReadinessVerdict
    mandatory_check_ids: tuple[str, ...]
    blocking_check_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.target, ReadinessTarget):
            raise ValueError("invalid readiness target")
        if not isinstance(self.verdict, ReadinessVerdict):
            raise ValueError("invalid readiness verdict")
        mandatory = tuple(self.mandatory_check_ids)
        blockers = tuple(self.blocking_check_ids)
        reasons = tuple(self.reason_codes)
        for check_id in (*mandatory, *blockers):
            _require_id(check_id, "readiness check ID")
        for reason in reasons:
            _require_id(reason, "readiness reason code")
        if len(set(mandatory)) != len(mandatory) or len(set(blockers)) != len(blockers):
            raise ValueError("duplicate readiness result check ID")
        if not set(blockers).issubset(mandatory):
            raise ValueError("blocking check is not mandatory")
        if len(blockers) != len(reasons):
            raise ValueError("blocking checks and reasons differ")
        blocker_reasons = tuple(sorted(zip(blockers, reasons, strict=True)))
        object.__setattr__(self, "mandatory_check_ids", tuple(sorted(mandatory)))
        object.__setattr__(
            self, "blocking_check_ids", tuple(item[0] for item in blocker_reasons)
        )
        object.__setattr__(self, "reason_codes", tuple(item[1] for item in blocker_reasons))


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    schema_version: int
    audited_system_commit: str
    report_implementation_commit: str | None
    as_of: datetime
    checks: tuple[ReadinessCheck, ...]
    target_gates: tuple[TargetGate, ...]
    target_results: tuple[ReadinessTargetResult, ...]

    def __post_init__(self) -> None:
        if self.schema_version != READINESS_SCHEMA_VERSION:
            raise ValueError("unsupported readiness schema version")
        _require_commit(self.audited_system_commit, "audited-system commit")
        _require_commit(
            self.report_implementation_commit, "report implementation commit", optional=True
        )
        object.__setattr__(self, "as_of", _require_utc(self.as_of, "report as_of"))
        checks = tuple(self.checks)
        gates = tuple(self.target_gates)
        results = tuple(self.target_results)
        if not all(isinstance(item, ReadinessCheck) for item in checks):
            raise ValueError("invalid readiness report check")
        if not all(isinstance(item, TargetGate) for item in gates):
            raise ValueError("invalid readiness report gate")
        if not all(isinstance(item, ReadinessTargetResult) for item in results):
            raise ValueError("invalid readiness report result")
        if len({item.check_id for item in checks}) != len(checks):
            raise ValueError("duplicate readiness check ID")
        if len({item.target for item in gates}) != len(gates):
            raise ValueError("duplicate readiness target gate")
        if len({item.target for item in results}) != len(results):
            raise ValueError("duplicate readiness target result")
        known_ids = {
            check_id for gate in gates for check_id in gate.mandatory_check_ids
        }
        unknown = sorted({item.check_id for item in checks} - known_ids)
        if unknown:
            raise ValueError(f"unknown readiness check ID: {unknown[0]}")
        checks = tuple(sorted(checks, key=lambda item: item.check_id))
        gates = tuple(sorted(gates, key=lambda item: item.target.value))
        results = tuple(sorted(results, key=lambda item: item.target.value))
        expected = tuple(
            sorted(
                (_evaluate_gate(gate, {item.check_id: item for item in checks}) for gate in gates),
                key=lambda item: item.target.value,
            )
        )
        if results != expected:
            raise ValueError("target result does not match evaluated gate")
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "target_gates", gates)
        object.__setattr__(self, "target_results", results)

    def canonical_json(self) -> bytes:
        return json.dumps(
            _report_payload(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    @property
    def report_fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()

    def result_for(self, target: ReadinessTarget) -> ReadinessTargetResult:
        for result in self.target_results:
            if result.target is target:
                return result
        raise KeyError(target.value)


def evaluate_readiness(
    *,
    audited_system_commit: str,
    as_of: datetime,
    checks: tuple[ReadinessCheck, ...],
    gates: tuple[TargetGate, ...],
    evidence_inventory: EvidenceInventory,
    report_implementation_commit: str | None = None,
) -> ReadinessReport:
    """Evaluate fixed mandatory gates without a manual verdict override."""

    _require_commit(audited_system_commit, "audited-system commit")
    as_of_utc = _require_utc(as_of, "report as_of")
    assert as_of_utc is not None
    supplied_checks = tuple(checks)
    supplied_gates = tuple(gates)
    check_ids = [item.check_id for item in supplied_checks]
    if len(set(check_ids)) != len(check_ids):
        raise ValueError("duplicate readiness check ID")
    targets = [item.target for item in supplied_gates]
    if len(set(targets)) != len(targets):
        raise ValueError("duplicate readiness target gate")
    known_ids = {
        check_id
        for target_gate in supplied_gates
        for check_id in target_gate.mandatory_check_ids
    }
    unknown = sorted(set(check_ids) - known_ids)
    if unknown:
        raise ValueError(f"unknown readiness check ID: {unknown[0]}")

    if not isinstance(evidence_inventory, EvidenceInventory):
        raise ValueError("invalid evidence inventory")
    normalized = tuple(
        _normalize_evidence(item, audited_system_commit, as_of_utc, evidence_inventory)
        for item in supplied_checks
    )
    by_id = {item.check_id: item for item in normalized}
    results = tuple(_evaluate_gate(target_gate, by_id) for target_gate in supplied_gates)
    return ReadinessReport(
        schema_version=READINESS_SCHEMA_VERSION,
        audited_system_commit=audited_system_commit,
        report_implementation_commit=report_implementation_commit,
        as_of=as_of_utc,
        checks=normalized,
        target_gates=supplied_gates,
        target_results=results,
    )


def _normalize_evidence(
    check: ReadinessCheck,
    audited_system_commit: str,
    as_of: datetime,
    inventory: EvidenceInventory,
) -> ReadinessCheck:
    if check.status is ReadinessStatus.NOT_APPLICABLE:
        return check
    if not check.evidence:
        return replace(check, status=ReadinessStatus.UNVERIFIED, reason_code="evidence_missing")
    if any(item.audited_system_commit != audited_system_commit for item in check.evidence):
        return replace(
            check,
            status=ReadinessStatus.UNVERIFIED,
            reason_code="evidence_incompatible",
        )
    if any(item.observed_at is not None and item.observed_at > as_of for item in check.evidence):
        return replace(check, status=ReadinessStatus.UNVERIFIED, reason_code="evidence_from_future")
    if any(item.valid_until is not None and item.valid_until < as_of for item in check.evidence):
        return replace(check, status=ReadinessStatus.UNVERIFIED, reason_code="evidence_stale")
    if any(not inventory.contains(item) for item in check.evidence):
        return replace(
            check,
            status=ReadinessStatus.UNVERIFIED,
            reason_code="evidence_not_verified",
        )
    return check


def _evaluate_gate(
    gate: TargetGate, checks: dict[str, ReadinessCheck]
) -> ReadinessTargetResult:
    blockers: list[str] = []
    reasons: list[str] = []
    for check_id in gate.mandatory_check_ids:
        item = checks.get(check_id)
        if item is None:
            blockers.append(check_id)
            reasons.append("missing_mandatory_check")
        elif item.status is ReadinessStatus.PASS:
            continue
        elif (
            item.status is ReadinessStatus.NOT_APPLICABLE
            and check_id in gate.not_applicable_allowed
        ):
            continue
        else:
            blockers.append(check_id)
            reasons.append(item.reason_code)
    verdict = ReadinessVerdict.GO if not blockers else ReadinessVerdict.NO_GO
    return ReadinessTargetResult(
        target=gate.target,
        verdict=verdict,
        mandatory_check_ids=gate.mandatory_check_ids,
        blocking_check_ids=tuple(blockers),
        reason_codes=tuple(reasons),
    )


def _evidence_sort_key(item: ReadinessEvidence) -> tuple[str, str, str, str, str]:
    return (
        item.kind.value,
        item.reference.canonical_text,
        item.audited_system_commit,
        _datetime_text(item.observed_at),
        _datetime_text(item.valid_until),
    )


def _datetime_text(value: datetime | None) -> str:
    return "" if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _report_payload(report: ReadinessReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "audited_system_commit": report.audited_system_commit,
        "report_implementation_commit": report.report_implementation_commit,
        "as_of": _datetime_text(report.as_of),
        "checks": [
            {
                "check_id": item.check_id,
                "domain": item.domain.value,
                "status": item.status.value,
                "reason_code": item.reason_code,
                "evidence": [
                    {
                        "kind": evidence.kind.value,
                        "reference": {
                            "kind": evidence.reference.kind.value,
                            "identifier": evidence.reference.identifier,
                            "sub_identifier": evidence.reference.sub_identifier,
                        },
                        "audited_system_commit": evidence.audited_system_commit,
                        "observed_at": _datetime_text(evidence.observed_at) or None,
                        "valid_until": _datetime_text(evidence.valid_until) or None,
                    }
                    for evidence in item.evidence
                ],
            }
            for item in report.checks
        ],
        "target_gates": [
            {
                "target": item.target.value,
                "mandatory_check_ids": list(item.mandatory_check_ids),
                "not_applicable_allowed": list(item.not_applicable_allowed),
            }
            for item in report.target_gates
        ],
        "target_results": [
            {
                "target": item.target.value,
                "verdict": item.verdict.value,
                "mandatory_check_ids": list(item.mandatory_check_ids),
                "blocking_check_ids": list(item.blocking_check_ids),
                "reason_codes": list(item.reason_codes),
            }
            for item in report.target_results
        ],
    }
