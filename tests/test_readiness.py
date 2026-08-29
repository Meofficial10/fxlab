from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fxlab.readiness.audit import CURRENT_TARGET_GATES, build_current_readiness_report
from fxlab.readiness.model import (
    EvidenceInventory,
    EvidenceKind,
    EvidenceReference,
    ReadinessCheck,
    ReadinessDomain,
    ReadinessEvidence,
    ReadinessStatus,
    ReadinessTarget,
    ReadinessTargetResult,
    ReadinessVerdict,
    TargetGate,
    evaluate_readiness,
)

AUDITED_COMMIT = "f3ce10f32f17b6f7dbe793a40967ad8c4e2e3143"
AS_OF = datetime(2026, 8, 29, tzinfo=UTC)


def evidence(
    reference: str = "tests/test_readiness.py::test_evidence",
    *,
    commit: str = AUDITED_COMMIT,
    observed_at: datetime | None = None,
    valid_until: datetime | None = None,
) -> ReadinessEvidence:
    return ReadinessEvidence(
        kind=EvidenceKind.TEST,
        reference=EvidenceReference.from_text(EvidenceKind.TEST, reference),
        audited_system_commit=commit,
        observed_at=observed_at,
        valid_until=valid_until,
    )


def check(
    check_id: str,
    status: ReadinessStatus = ReadinessStatus.PASS,
    *,
    items: tuple[ReadinessEvidence, ...] | None = None,
) -> ReadinessCheck:
    return ReadinessCheck(
        check_id=check_id,
        domain=ReadinessDomain.RISK,
        status=status,
        reason_code=f"{check_id}_reason",
        evidence=(evidence() if items is None else None,) if items is None else items,
    )


def gate(*ids: str, allow_na: tuple[str, ...] = ()) -> TargetGate:
    return TargetGate(
        target=ReadinessTarget.DETERMINISTIC_PAPER,
        mandatory_check_ids=ids,
        not_applicable_allowed=allow_na,
    )


def evaluate(
    checks: tuple[ReadinessCheck, ...],
    target_gate: TargetGate,
    *,
    implementation_commit: str | None = None,
):
    inventory = EvidenceInventory(
        tuple(item for check_item in checks for item in check_item.evidence)
    )
    return evaluate_readiness(
        audited_system_commit=AUDITED_COMMIT,
        report_implementation_commit=implementation_commit,
        as_of=AS_OF,
        checks=checks,
        gates=(target_gate,),
        evidence_inventory=inventory,
    )


def test_contracts_are_frozen_and_collections_do_not_escape_mutably() -> None:
    report = evaluate((check("risk"),), gate("risk"))

    with pytest.raises(dataclasses.FrozenInstanceError):
        report.schema_version = 2  # type: ignore[misc]
    assert isinstance(report.checks, tuple)
    assert isinstance(report.target_results, tuple)
    assert isinstance(report.checks[0].evidence, tuple)


def test_target_result_normalizes_mutable_inputs_without_aliasing() -> None:
    mandatory = ["risk"]
    blockers = ["risk"]
    reasons = ["risk_blocked"]
    result = ReadinessTargetResult(
        ReadinessTarget.LIVE_MONEY,
        ReadinessVerdict.NO_GO,
        mandatory,  # type: ignore[arg-type]
        blockers,  # type: ignore[arg-type]
        reasons,  # type: ignore[arg-type]
    )

    mandatory.append("later")
    blockers.clear()
    reasons[0] = "changed"

    assert result.mandatory_check_ids == ("risk",)
    assert result.blocking_check_ids == ("risk",)
    assert result.reason_codes == ("risk_blocked",)


def test_evidence_inventory_normalizes_mutable_input_without_aliasing() -> None:
    items = [evidence()]
    inventory = EvidenceInventory(items)  # type: ignore[arg-type]
    items.clear()

    assert len(inventory.evidence) == 1


def test_enum_values_are_stable() -> None:
    assert [status.value for status in ReadinessStatus] == [
        "pass",
        "fail",
        "blocked",
        "unverified",
        "not_applicable",
    ]
    assert ReadinessTarget.LIVE_MONEY.value == "live_money"
    assert ReadinessDomain.OPERATIONAL_SECURITY.value == "operational_security"
    assert EvidenceKind.SEALED_TEST_RESULT.value == "sealed_test_result"


def test_canonical_serialization_and_sha256_identity_are_order_invariant() -> None:
    first = evaluate((check("b"), check("a")), gate("b", "a"))
    second = evaluate((check("a"), check("b")), gate("a", "b"))

    assert first.canonical_json() == second.canonical_json()
    assert first.report_fingerprint == second.report_fingerprint
    assert first.report_fingerprint == hashlib.sha256(first.canonical_json()).hexdigest()
    assert json.loads(first.canonical_json())["checks"][0]["check_id"] == "a"


def test_evidence_order_is_canonical_when_only_commits_differ() -> None:
    first_item = evidence(commit="0" * 40)
    second_item = evidence(commit="1" * 40)
    left = ReadinessCheck(
        "risk",
        ReadinessDomain.RISK,
        ReadinessStatus.BLOCKED,
        "risk_blocked",
        (first_item, second_item),
    )
    right = ReadinessCheck(
        "risk",
        ReadinessDomain.RISK,
        ReadinessStatus.BLOCKED,
        "risk_blocked",
        (second_item, first_item),
    )

    assert left.evidence == right.evidence

    target_gate = gate("risk")
    inventory = EvidenceInventory((first_item, second_item))
    first_report = evaluate_readiness(
        audited_system_commit=AUDITED_COMMIT,
        as_of=AS_OF,
        checks=(left,),
        gates=(target_gate,),
        evidence_inventory=inventory,
    )
    second_report = evaluate_readiness(
        audited_system_commit=AUDITED_COMMIT,
        as_of=AS_OF,
        checks=(right,),
        gates=(target_gate,),
        evidence_inventory=inventory,
    )
    assert first_report.report_fingerprint == second_report.report_fingerprint


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ReadinessStatus.PASS, ReadinessVerdict.GO),
        (ReadinessStatus.FAIL, ReadinessVerdict.NO_GO),
        (ReadinessStatus.BLOCKED, ReadinessVerdict.NO_GO),
        (ReadinessStatus.UNVERIFIED, ReadinessVerdict.NO_GO),
    ],
)
def test_mandatory_status_propagates_to_target_verdict(
    status: ReadinessStatus, expected: ReadinessVerdict
) -> None:
    report = evaluate((check("risk", status),), gate("risk"))

    assert report.target_results[0].verdict is expected


def test_missing_mandatory_check_fails_closed() -> None:
    result = evaluate((), gate("required")).target_results[0]

    assert result.verdict is ReadinessVerdict.NO_GO
    assert result.blocking_check_ids == ("required",)
    assert result.reason_codes == ("missing_mandatory_check",)


def test_not_applicable_only_satisfies_explicit_check_target_pair() -> None:
    item = check("broker", ReadinessStatus.NOT_APPLICABLE, items=())

    assert evaluate((item,), gate("broker")).target_results[0].verdict is ReadinessVerdict.NO_GO
    assert (
        evaluate((item,), gate("broker", allow_na=("broker",))).target_results[0].verdict
        is ReadinessVerdict.GO
    )


def test_duplicate_checks_and_conflicting_gate_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate readiness check ID"):
        evaluate((check("risk"), check("risk")), gate("risk"))
    with pytest.raises(ValueError, match="duplicate mandatory check ID"):
        evaluate((check("risk"),), gate("risk", "risk"))


def test_unknown_check_is_rejected_instead_of_satisfying_a_gate() -> None:
    with pytest.raises(ValueError, match="unknown readiness check ID"):
        evaluate((check("risk"), check("invented")), gate("risk"))


@pytest.mark.parametrize(
    ("items", "reason"),
    [
        ((), "evidence_missing"),
        ((evidence(commit="0" * 40),), "evidence_incompatible"),
        (
            (
                evidence(
                    observed_at=AS_OF - timedelta(days=2),
                    valid_until=AS_OF - timedelta(seconds=1),
                ),
            ),
            "evidence_stale",
        ),
        ((evidence(observed_at=AS_OF + timedelta(microseconds=1)),), "evidence_from_future"),
    ],
)
def test_invalid_pass_evidence_is_downgraded_to_unverified(
    items: tuple[ReadinessEvidence, ...], reason: str
) -> None:
    report = evaluate((check("risk", items=items),), gate("risk"))

    assert report.checks[0].status is ReadinessStatus.UNVERIFIED
    assert report.checks[0].reason_code == reason
    assert report.target_results[0].verdict is ReadinessVerdict.NO_GO


def test_audited_system_commit_is_distinct_from_report_implementation_commit() -> None:
    implementation = "a" * 40
    report = evaluate((check("risk"),), gate("risk"), implementation_commit=implementation)

    assert report.audited_system_commit == AUDITED_COMMIT
    assert report.report_implementation_commit == implementation
    assert report.audited_system_commit != report.report_implementation_commit


def test_report_serialization_has_no_generic_secret_or_runtime_object_channel() -> None:
    report = evaluate((check("risk"),), gate("risk"))
    rendered = report.canonical_json().decode("utf-8")

    assert "secret" not in rendered.lower()
    assert "token" not in rendered.lower()
    assert "object at 0x" not in rendered


@pytest.mark.parametrize(
    "unsafe",
    [
        "token=abc123",
        "Authorization: Bearer abc123",
        "https://user:pass@example.com",
        "password=abc123",
        "api_key=abc123",
        "../tests/test_readiness.py::test_evidence",
    ],
)
def test_structured_evidence_reference_rejects_free_form_or_secret_shapes(
    unsafe: str,
) -> None:
    with pytest.raises(ValueError, match="invalid evidence reference"):
        EvidenceReference.from_text(EvidenceKind.TEST, unsafe)


def test_structurally_valid_but_absent_inventory_evidence_is_unverified() -> None:
    item = check("risk")
    report = evaluate_readiness(
        audited_system_commit=AUDITED_COMMIT,
        as_of=AS_OF,
        checks=(item,),
        gates=(gate("risk"),),
        evidence_inventory=EvidenceInventory(()),
    )

    assert report.checks[0].status is ReadinessStatus.UNVERIFIED
    assert report.checks[0].reason_code == "evidence_not_verified"


def test_nonexistent_adr_reference_cannot_support_a_check() -> None:
    absent = ReadinessEvidence(
        EvidenceKind.ADR,
        EvidenceReference(
            EvidenceKind.ADR,
            "docs/adr/9999-nonexistent-decision.md",
        ),
        AUDITED_COMMIT,
    )
    report = evaluate_readiness(
        audited_system_commit=AUDITED_COMMIT,
        as_of=AS_OF,
        checks=(check("research", items=(absent,)),),
        gates=(gate("research"),),
        evidence_inventory=EvidenceInventory(()),
    )
    assert report.checks[0].status is ReadinessStatus.UNVERIFIED
    assert report.checks[0].reason_code == "evidence_not_verified"


def test_manual_go_report_inconsistent_with_gate_is_rejected() -> None:
    blocked = check("risk", ReadinessStatus.BLOCKED)
    target_gate = gate("risk")
    forged = ReadinessTargetResult(
        ReadinessTarget.DETERMINISTIC_PAPER,
        ReadinessVerdict.GO,
        ("risk",),
        (),
        (),
    )

    from fxlab.readiness.model import ReadinessReport

    with pytest.raises(ValueError, match="target result does not match evaluated gate"):
        ReadinessReport(
            schema_version=1,
            audited_system_commit=AUDITED_COMMIT,
            report_implementation_commit=None,
            as_of=AS_OF,
            checks=(blocked,),
            target_gates=(target_gate,),
            target_results=(forged,),
        )


def test_direct_report_rejects_checks_outside_its_gates() -> None:
    from fxlab.readiness.model import ReadinessReport

    with pytest.raises(ValueError, match="unknown readiness check ID"):
        ReadinessReport(
            schema_version=1,
            audited_system_commit=AUDITED_COMMIT,
            report_implementation_commit=None,
            as_of=AS_OF,
            checks=(check("invented"),),
            target_gates=(gate("risk"),),
            target_results=(
                ReadinessTargetResult(
                    ReadinessTarget.DETERMINISTIC_PAPER,
                    ReadinessVerdict.NO_GO,
                    ("risk",),
                    ("risk",),
                    ("missing_mandatory_check",),
                ),
            ),
        )


def test_current_oanda_evidence_uses_actual_descriptor_identity() -> None:
    report = build_current_readiness_report(as_of=AS_OF)
    check_item = next(item for item in report.checks if item.check_id == "oanda_practice_contract")
    descriptor = next(
        item.reference
        for item in check_item.evidence
        if item.kind is EvidenceKind.BROKER_DESCRIPTOR
    )

    assert descriptor.identifier == "oanda-v20"
    assert descriptor.sub_identifier == "2"


def test_current_research_evidence_uses_existing_p4_decision() -> None:
    report = build_current_readiness_report(as_of=AS_OF)
    research = next(item for item in report.checks if item.check_id == "research_edge")
    references = {item.reference.canonical_text for item in research.evidence}

    assert "docs/adr/0001-p4-no-go-smc-rule-baseline.md" in references
    assert "docs/adr/0005-p4-final-decision.md" not in references
    assert Path("docs/adr/0001-p4-no-go-smc-rule-baseline.md").is_file()


def test_oanda_descriptor_mismatch_is_not_verified() -> None:
    actual = ReadinessEvidence(
        EvidenceKind.BROKER_DESCRIPTOR,
        EvidenceReference(EvidenceKind.BROKER_DESCRIPTOR, "oanda-v20", "2"),
        AUDITED_COMMIT,
    )
    mismatched = ReadinessEvidence(
        EvidenceKind.BROKER_DESCRIPTOR,
        EvidenceReference(EvidenceKind.BROKER_DESCRIPTOR, "oanda-v20", "1"),
        AUDITED_COMMIT,
    )
    report = evaluate_readiness(
        audited_system_commit=AUDITED_COMMIT,
        as_of=AS_OF,
        checks=(check("broker", items=(mismatched,)),),
        gates=(gate("broker"),),
        evidence_inventory=EvidenceInventory((actual,)),
    )
    assert report.checks[0].status is ReadinessStatus.UNVERIFIED
    assert report.checks[0].reason_code == "evidence_not_verified"


def test_deterministic_paper_requires_paper_reconciliation() -> None:
    gate_item = next(
        item for item in CURRENT_TARGET_GATES if item.target is ReadinessTarget.DETERMINISTIC_PAPER
    )
    assert "paper_reconciliation" in gate_item.mandatory_check_ids

    checks = tuple(
        check(check_id)
        for check_id in gate_item.mandatory_check_ids
        if check_id != "paper_reconciliation"
    )
    report = evaluate_readiness(
        audited_system_commit=AUDITED_COMMIT,
        as_of=AS_OF,
        checks=checks,
        gates=(gate_item,),
        evidence_inventory=EvidenceInventory(
            tuple(item for check_item in checks for item in check_item.evidence)
        ),
    )
    assert report.target_results[0].verdict is ReadinessVerdict.NO_GO
    assert "paper_reconciliation" in report.target_results[0].blocking_check_ids


def test_current_evidence_calculates_separate_target_verdicts() -> None:
    report = build_current_readiness_report(as_of=AS_OF)
    results = {item.target: item.verdict for item in report.target_results}

    assert results == {
        ReadinessTarget.DETERMINISTIC_PAPER: ReadinessVerdict.GO,
        ReadinessTarget.OBSERVATION_SERVICE: ReadinessVerdict.GO,
        ReadinessTarget.OANDA_PRACTICE_ADAPTER: ReadinessVerdict.GO,
        ReadinessTarget.OANDA_PRACTICE_FORWARD_STRATEGY: ReadinessVerdict.NO_GO,
        ReadinessTarget.LIVE_MONEY: ReadinessVerdict.NO_GO,
    }
    forward = report.result_for(ReadinessTarget.OANDA_PRACTICE_FORWARD_STRATEGY)
    live = report.result_for(ReadinessTarget.LIVE_MONEY)
    assert "research_edge" in forward.blocking_check_ids
    assert "external_reconciliation" in forward.blocking_check_ids
    assert "research_edge" in live.blocking_check_ids
    assert "live_broker" in live.blocking_check_ids
    assert len(CURRENT_TARGET_GATES) == 5


def test_no_infrastructure_pass_can_promote_live_money() -> None:
    report = build_current_readiness_report(as_of=AS_OF)

    assert report.result_for(ReadinessTarget.DETERMINISTIC_PAPER).verdict is ReadinessVerdict.GO
    assert report.result_for(ReadinessTarget.OBSERVATION_SERVICE).verdict is ReadinessVerdict.GO
    assert report.result_for(ReadinessTarget.OANDA_PRACTICE_ADAPTER).verdict is ReadinessVerdict.GO
    assert report.result_for(ReadinessTarget.LIVE_MONEY).verdict is ReadinessVerdict.NO_GO


def test_current_report_fingerprint_matches_documented_identity() -> None:
    report = build_current_readiness_report(as_of=AS_OF)
    documented = Path("docs/05-live-readiness-audit.md").read_text(encoding="utf-8")

    assert report.report_fingerprint == (
        "cc02258939bc76545fb4cd94bfffdc32e52af94385e6e4d638ccf9ba966feca3"
    )
    assert report.report_fingerprint in documented


def test_secret_rotation_runbook_is_provisional_not_validated() -> None:
    runbook = Path("docs/runbooks/phase20-incidents-and-evidence.md").read_text(
        encoding="utf-8"
    )
    section = runbook.split("## Control secret rotation", 1)[1].split("## Backup", 1)[0]

    assert "**PROVISIONAL**" in section
    assert "is validated" not in section
