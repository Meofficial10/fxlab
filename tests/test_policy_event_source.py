"""Synthetic offline tests for the Candidate B policy-event *source-artifact*
acquisition, immutable persistence, and independent verification boundary.

Every fixture in this module is synthetic. No test performs network I/O, reads
real central-bank documents, inspects 2024+ evidence, constructs a
``PolicyRateEvent``, or invokes qualification. The real artifact inventory
(``OFFICIAL_POLICY_ARTIFACT_SPECS``) is expected to be empty; tests drive the
machinery with locally constructed synthetic specs and responses.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

import pytest
import scripts.acquire_policy_event_source as acquire
from scripts.acquire_policy_event_source import (
    AUTHORITATIVE_MAX_RESPONSE_BYTES,
    AUTHORITATIVE_TIMEOUT_SECONDS,
    acquire_and_publish_policy_event_source,
    fetch_policy_event_source_response,
)

import fxlab.data.policy_event_source as pes
from fxlab.data.policy_event_source import (
    OFFICIAL_POLICY_ARTIFACT_SPECS,
    POLICY_EVENT_SOURCE_ARTIFACT_SCHEMA,
    RETAINED_RESPONSE_HEADERS,
    OfficialPolicyArtifactSpec,
    PolicyEventSourceBodyFormat,
    PolicyEventSourceHttpResponse,
    PolicyEventSourcePublication,
    VerifiedPolicyEventSourceArtifact,
    persist_policy_event_source_artifact,
    resolve_official_policy_artifact_spec,
    verify_policy_event_source_artifact,
)
from fxlab.data.policy_rates import (
    MAX_OBSERVATION_DATE,
    EvidenceClassification,
    PolicyRateEvent,
    PolicyRateQualificationError,
    PolicySourceEvidence,
)

RETRIEVED = datetime(2026, 9, 2, 15, 30, tzinfo=UTC)

_PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
_OPAQUE = b"synthetic-opaque-policy-notice-bytes"
_XML = b"<?xml version='1.0' encoding='utf-8'?><notice><segment>x</segment></notice>"
_XML_DTD = b"<?xml version='1.0'?><!DOCTYPE n [<!ENTITY e 'v'>]><notice>&e;</notice>"
_XML_MALFORMED = b"<notice><segment>"
_PDF_MALFORMED = b"this is plainly not a pdf document"


def _official_spec(**overrides: object) -> OfficialPolicyArtifactSpec:
    """Synthetic spec whose host is an already-frozen official domain, so a
    verified artifact can legally bridge to ``PolicySourceEvidence``."""

    base: dict[str, object] = dict(
        artifact_key="fed-fixture-a",
        currency="USD",
        authority="FED",
        source_kind=EvidenceClassification.OFFICIAL_ANNOUNCEMENT,
        body_format=PolicyEventSourceBodyFormat.PDF,
        event_date=date(2023, 3, 15),
        approved_url="https://www.federalreserve.gov/synthetic/fixture-a.pdf",
        authority_host="www.federalreserve.gov",
        accept_media_type="application/pdf",
        response_media_type="application/pdf",
    )
    base.update(overrides)
    return OfficialPolicyArtifactSpec(**base)  # type: ignore[arg-type]


def _opaque_spec(**overrides: object) -> OfficialPolicyArtifactSpec:
    """Synthetic spec on a NON-official host: the acquisition machinery accepts
    it (spec-scoped domain), but the frozen evidence gate must refuse it."""

    base: dict[str, object] = dict(
        artifact_key="authority-fixture-b",
        currency="EUR",
        authority="SYNTH",
        source_kind=EvidenceClassification.OFFICIAL_INSTRUMENT_NOTICE,
        body_format=PolicyEventSourceBodyFormat.OPAQUE,
        event_date=date(2023, 6, 1),
        approved_url="https://docs.authority.example/notice/b",
        authority_host="docs.authority.example",
        accept_media_type="application/octet-stream",
        response_media_type="application/octet-stream",
    )
    base.update(overrides)
    return OfficialPolicyArtifactSpec(**base)  # type: ignore[arg-type]


def _response(
    spec: OfficialPolicyArtifactSpec,
    raw: bytes,
    *,
    status: int = 200,
    final_url: str | None = None,
    media_type: str | None = None,
    headers: dict[str, str] | None = None,
    redirect_chain: tuple[str, ...] = (),
) -> PolicyEventSourceHttpResponse:
    return PolicyEventSourceHttpResponse(
        status_code=status,
        final_url=spec.approved_url if final_url is None else final_url,
        media_type=spec.response_media_type if media_type is None else media_type,
        headers=headers
        if headers is not None
        else {
            "Content-Type": spec.response_media_type,
            "ETag": '"tag"',
            "Set-Cookie": "session=drop-me",
        },
        raw_bytes=raw,
        redirect_chain=redirect_chain,
    )


class FakePolicyEventSourceTransport:
    def __init__(
        self,
        response: PolicyEventSourceHttpResponse | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[object, str, str, int, int]] = []

    def fetch(
        self,
        spec: object,
        *,
        exact_url: str,
        accept: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> PolicyEventSourceHttpResponse:
        self.calls.append((spec, exact_url, accept, timeout_seconds, max_response_bytes))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _transport(spec: OfficialPolicyArtifactSpec, raw: bytes) -> FakePolicyEventSourceTransport:
    return FakePolicyEventSourceTransport(_response(spec, raw))


# --------------------------------------------------------------------------- #
# A. Frozen registry and OfficialPolicyArtifactSpec contract
# --------------------------------------------------------------------------- #
def test_official_policy_artifact_inventory_is_empty_until_discovery() -> None:
    assert dict(OFFICIAL_POLICY_ARTIFACT_SPECS) == {}
    with pytest.raises(
        PolicyRateQualificationError, match="unknown_policy_event_source_artifact"
    ):
        resolve_official_policy_artifact_spec("fed-fixture-a")


def test_spec_enforces_https_and_forbids_userinfo_fragment_port_query() -> None:
    good = _official_spec()
    for bad_url in (
        good.approved_url.replace("https://", "http://"),
        good.approved_url.replace(
            "www.federalreserve.gov", "user:pass@www.federalreserve.gov"
        ),
        good.approved_url + "#section",
        good.approved_url.replace(
            "www.federalreserve.gov", "www.federalreserve.gov:8443"
        ),
        good.approved_url + "?download=1",
    ):
        with pytest.raises(ValueError):
            _official_spec(approved_url=bad_url)


def test_spec_binds_host_to_authority_host() -> None:
    with pytest.raises(ValueError):
        _official_spec(authority_host="www.bankofengland.co.uk")


def test_spec_supports_port_and_query_only_when_explicitly_frozen() -> None:
    spec = _official_spec(
        approved_url="https://www.federalreserve.gov:8443/synthetic/fixture-a.pdf?ref=a",
        approved_port=8443,
        approved_query="ref=a",
    )
    assert spec.approved_port == 8443
    assert spec.approved_query == "ref=a"
    # A mismatching declared query is still rejected.
    with pytest.raises(ValueError):
        _official_spec(
            approved_url="https://www.federalreserve.gov/synthetic/fixture-a.pdf?ref=b",
            approved_query="ref=a",
        )


# --------------------------------------------------------------------------- #
# B. Acquisition boundary: bounded, one-attempt, fail-closed transport
# --------------------------------------------------------------------------- #
def test_fetch_uses_exact_frozen_contract_exactly_once() -> None:
    spec = _official_spec()
    transport = _transport(spec, _PDF)
    result = fetch_policy_event_source_response(spec, transport)
    assert result is transport.response
    assert transport.calls == [
        (
            spec,
            spec.approved_url,
            spec.accept_media_type,
            15,
            4 * 1024 * 1024,
        )
    ]
    assert AUTHORITATIVE_TIMEOUT_SECONDS == 15
    assert AUTHORITATIVE_MAX_RESPONSE_BYTES == 4 * 1024 * 1024


@pytest.mark.parametrize(
    ("status", "final_url", "media_type", "reason"),
    [
        (302, "https://www.federalreserve.gov/elsewhere", "application/pdf", "redirect_rejected"),
        (
            200,
            "https://www.federalreserve.gov/synthetic/fixture-a-moved.pdf",
            "application/pdf",
            "redirect_rejected",
        ),
        (
            500,
            "https://www.federalreserve.gov/synthetic/fixture-a.pdf",
            "application/pdf",
            "http_status_not_success",
        ),
        (
            200,
            "https://www.federalreserve.gov/synthetic/fixture-a.pdf",
            "text/plain",
            "media_type_not_approved",
        ),
    ],
)
def test_fetch_rejects_redirect_status_url_or_media(
    status: int, final_url: str, media_type: str, reason: str
) -> None:
    spec = _official_spec()
    transport = FakePolicyEventSourceTransport(
        _response(spec, _PDF, status=status, final_url=final_url, media_type=media_type)
    )
    with pytest.raises(PolicyRateQualificationError, match=reason):
        fetch_policy_event_source_response(spec, transport)
    assert len(transport.calls) == 1


def test_fetch_timeout_is_one_attempt_without_retry() -> None:
    spec = _official_spec()
    transport = FakePolicyEventSourceTransport(error=TimeoutError("synthetic timeout"))
    with pytest.raises(PolicyRateQualificationError, match="acquisition_timeout"):
        fetch_policy_event_source_response(spec, transport)
    assert len(transport.calls) == 1


def test_fetch_transport_failure_is_one_attempt_without_fallback() -> None:
    spec = _official_spec()
    transport = FakePolicyEventSourceTransport(error=ValueError("synthetic wire error"))
    with pytest.raises(PolicyRateQualificationError, match="transport_failure"):
        fetch_policy_event_source_response(spec, transport)
    assert len(transport.calls) == 1


def test_fetch_rejects_oversized_response_without_truncation() -> None:
    spec = _official_spec()
    oversized = b"%PDF-1.7\n" + b"x" * AUTHORITATIVE_MAX_RESPONSE_BYTES
    transport = _transport(spec, oversized)
    with pytest.raises(PolicyRateQualificationError, match="response_too_large"):
        fetch_policy_event_source_response(spec, transport)
    assert len(transport.calls) == 1


def test_fetch_enforces_exact_frozen_redirect_chain_when_specified() -> None:
    chain = (
        "https://www.federalreserve.gov/hop-1",
        "https://www.federalreserve.gov/synthetic/fixture-a.pdf",
    )
    spec = _official_spec(approved_redirect_chain=chain)
    ok = FakePolicyEventSourceTransport(
        _response(spec, _PDF, final_url=chain[-1], redirect_chain=chain)
    )
    assert fetch_policy_event_source_response(spec, ok).redirect_chain == chain

    wrong = FakePolicyEventSourceTransport(
        _response(
            spec,
            _PDF,
            final_url=chain[-1],
            redirect_chain=("https://www.federalreserve.gov/other-hop", chain[-1]),
        )
    )
    with pytest.raises(PolicyRateQualificationError, match="redirect_rejected"):
        fetch_policy_event_source_response(spec, wrong)


def test_fetch_rejects_redirect_hop_outside_authority_domain() -> None:
    chain = (
        "https://cdn.elsewhere.example/hop",
        "https://www.federalreserve.gov/synthetic/fixture-a.pdf",
    )
    spec = _official_spec(approved_redirect_chain=chain)
    transport = FakePolicyEventSourceTransport(
        _response(spec, _PDF, final_url=chain[-1], redirect_chain=chain)
    )
    with pytest.raises(
        PolicyRateQualificationError, match="redirect_outside_authority_domain"
    ):
        fetch_policy_event_source_response(spec, transport)


def test_fetch_rejects_sealed_window_event_date_before_any_transport_call() -> None:
    spec = _official_spec(event_date=date(2024, 1, 1))
    assert spec.event_date > MAX_OBSERVATION_DATE
    transport = _transport(spec, _PDF)
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        fetch_policy_event_source_response(spec, transport)
    assert transport.calls == []


# --------------------------------------------------------------------------- #
# C. Immutable persistence: atomic, no-overwrite, body-format, bindings
# --------------------------------------------------------------------------- #
def test_persist_publishes_raw_and_manifest_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", root)
    replacements: list[tuple[Path, Path, bool]] = []
    original_replace = Path.replace

    def recording_replace(source: Path, target: Path) -> Path:
        replacements.append((source, target, target.exists()))
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", recording_replace)

    spec = _official_spec()
    published = persist_policy_event_source_artifact(
        spec, _response(spec, _PDF), RETRIEVED
    )
    assert isinstance(published, PolicyEventSourcePublication)
    destination = root / spec.artifact_key
    assert published.destination == destination
    assert published.raw_path.read_bytes() == _PDF
    stored = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert stored["schema"] == POLICY_EVENT_SOURCE_ARTIFACT_SCHEMA
    assert stored["source_artifact_id"] == published.manifest.source_artifact_id
    assert stored["acquisition_id"] == published.manifest.acquisition_id
    assert replacements == [(replacements[0][0], destination, False)]
    assert replacements[0][0].parent == destination.parent
    assert replacements[0][0].name.startswith(".")
    assert "@" not in str(published.raw_path)


def test_persist_does_not_overwrite_existing_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", root)
    spec = _official_spec()
    (root / spec.artifact_key).mkdir(parents=True)
    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        persist_policy_event_source_artifact(spec, _response(spec, _PDF), RETRIEVED)


def test_persist_rejects_conflicting_duplicate_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", root)
    spec = _official_spec()
    persist_policy_event_source_artifact(spec, _response(spec, _PDF), RETRIEVED)
    conflicting = _PDF + b"%conflicting-tail\n"
    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        persist_policy_event_source_artifact(spec, _response(spec, conflicting), RETRIEVED)
    # The original immutable bytes are preserved.
    assert (root / spec.artifact_key / "source.bin").read_bytes() == _PDF


def test_persist_atomic_failure_leaves_no_final_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", root)

    def failing_replace(source: Path, target: Path) -> Path:
        del source, target
        raise OSError("synthetic atomic publication failure")

    monkeypatch.setattr(Path, "replace", failing_replace)
    spec = _official_spec()
    with pytest.raises(OSError, match="synthetic atomic publication failure"):
        persist_policy_event_source_artifact(spec, _response(spec, _PDF), RETRIEVED)
    assert not (root / spec.artifact_key).exists()
    assert not tuple(root.glob(".*"))


@pytest.mark.parametrize(
    ("body_format", "media_type", "raw"),
    [
        (PolicyEventSourceBodyFormat.PDF, "application/pdf", _PDF_MALFORMED),
        (PolicyEventSourceBodyFormat.XML, "application/xml", _XML_MALFORMED),
        (PolicyEventSourceBodyFormat.OPAQUE, "application/octet-stream", b""),
    ],
)
def test_persist_rejects_malformed_body_before_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_format: PolicyEventSourceBodyFormat,
    media_type: str,
    raw: bytes,
) -> None:
    root = tmp_path / "root"
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", root)
    spec = _official_spec(
        body_format=body_format, response_media_type=media_type, accept_media_type=media_type
    )
    with pytest.raises(PolicyRateQualificationError, match="response_body_format_invalid"):
        persist_policy_event_source_artifact(spec, _response(spec, raw), RETRIEVED)
    assert not root.exists()


def test_persist_rejects_unsafe_xml_dtd_or_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", root)
    spec = _official_spec(
        body_format=PolicyEventSourceBodyFormat.XML,
        response_media_type="application/xml",
        accept_media_type="application/xml",
    )
    with pytest.raises(PolicyRateQualificationError, match="unsafe_xml_rejected"):
        persist_policy_event_source_artifact(spec, _response(spec, _XML_DTD), RETRIEVED)
    assert not root.exists()


def test_persist_rejects_sealed_window_event_date_before_reading_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", root)
    spec = _official_spec(event_date=date(2024, 1, 1))
    # A body that would also fail format validation proves the date is checked first.
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        persist_policy_event_source_artifact(
            spec, _response(spec, _PDF_MALFORMED), RETRIEVED
        )
    assert not root.exists()


def test_persist_binds_requested_returned_url_and_allowlisted_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", root)
    spec = _official_spec()
    manifest = persist_policy_event_source_artifact(
        spec, _response(spec, _PDF), RETRIEVED
    ).manifest
    assert manifest.requested_url == spec.approved_url
    assert manifest.returned_url == spec.approved_url
    assert set(manifest.response_headers).issubset(RETAINED_RESPONSE_HEADERS)
    assert "set-cookie" not in manifest.response_headers
    assert manifest.response_headers["etag"] == '"tag"'
    assert manifest.raw_sha256 == hashlib.sha256(_PDF).hexdigest()
    assert manifest.byte_count == len(_PDF)


def test_manifest_separates_source_artifact_and_acquisition_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    manifests = []
    for index, retrieved_at in enumerate(
        (RETRIEVED, RETRIEVED.replace(day=RETRIEVED.day + 1))
    ):
        monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / f"root-{index}")
        manifests.append(
            persist_policy_event_source_artifact(
                spec, _response(spec, _PDF), retrieved_at
            ).manifest
        )
    assert manifests[0].source_artifact_id == manifests[1].source_artifact_id
    assert manifests[0].acquisition_id != manifests[1].acquisition_id

    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / "root-same-time")
    same_time_other_path = persist_policy_event_source_artifact(
        spec, _response(spec, _PDF), RETRIEVED
    ).manifest
    assert same_time_other_path.source_artifact_id == manifests[0].source_artifact_id
    assert same_time_other_path.acquisition_id == manifests[0].acquisition_id


def test_manifest_identity_is_response_header_order_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    first = _response(
        spec, _PDF, headers={"Content-Type": "application/pdf", "ETag": '"z"'}
    )
    second = _response(
        spec, _PDF, headers={"ETag": '"z"', "Content-Type": "application/pdf"}
    )
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / "root-a")
    manifest_a = persist_policy_event_source_artifact(spec, first, RETRIEVED).manifest
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / "root-b")
    manifest_b = persist_policy_event_source_artifact(spec, second, RETRIEVED).manifest
    assert manifest_a.acquisition_id == manifest_b.acquisition_id


# --------------------------------------------------------------------------- #
# D. Independent verification
# --------------------------------------------------------------------------- #
def _publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: OfficialPolicyArtifactSpec,
    raw: bytes,
    response: PolicyEventSourceHttpResponse | None = None,
) -> PolicyEventSourcePublication:
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / "root")
    monkeypatch.setattr(
        pes,
        "OFFICIAL_POLICY_ARTIFACT_SPECS",
        MappingProxyType({spec.artifact_key: spec}),
    )
    resp = response if response is not None else _response(spec, raw)
    return persist_policy_event_source_artifact(spec, resp, RETRIEVED)


def test_verify_recomputes_both_identities_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    verified = verify_policy_event_source_artifact(published.manifest_path)
    assert isinstance(verified, VerifiedPolicyEventSourceArtifact)
    assert verified.raw_bytes == _PDF
    assert verified.manifest.source_artifact_id == published.manifest.source_artifact_id
    assert verified.manifest.acquisition_id == published.manifest.acquisition_id


def test_verify_rejects_tampered_raw_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    published.raw_path.write_bytes(_PDF + b"tampered")
    with pytest.raises(PolicyRateQualificationError, match="raw_content_mismatch"):
        verify_policy_event_source_artifact(published.manifest_path)


def test_verify_rejects_tampered_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    payload = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    payload["byte_count"] = payload["byte_count"] + 1
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(published.manifest_path)


def test_verify_rejects_sealed_window_manifest_before_value_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    payload = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    payload["event_date"] = "2024-01-01"
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        verify_policy_event_source_artifact(published.manifest_path)


def test_verify_rejects_manifest_that_is_not_a_source_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    payload = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    payload["schema"] = "NON_AUTHORITATIVE_POLICY_EVENT_SOURCE_DISCOVERY"
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PolicyRateQualificationError, match="not_a_policy_event_source_artifact"):
        verify_policy_event_source_artifact(published.manifest_path)


# --------------------------------------------------------------------------- #
# E. Bridge to evidence — verified artifact constructs only PolicySourceEvidence
# --------------------------------------------------------------------------- #
def test_verified_artifact_constructs_only_policy_source_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    verified = verify_policy_event_source_artifact(published.manifest_path)

    evidence = verified.to_source_evidence()
    assert isinstance(evidence, PolicySourceEvidence)
    assert not isinstance(evidence, PolicyRateEvent)
    assert evidence.source_url == spec.approved_url
    assert evidence.content_hash == hashlib.sha256(_PDF).hexdigest()
    assert evidence.byte_count == len(_PDF)
    assert evidence.media_type == spec.response_media_type
    assert evidence.source_kind == spec.source_kind.value

    # The verified artifact deliberately cannot manufacture a policy-rate event.
    assert not hasattr(verified, "to_policy_rate_event")
    assert not hasattr(verified, "to_event")


def test_verified_non_official_domain_artifact_cannot_become_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _opaque_spec()
    published = _publish(tmp_path, monkeypatch, spec, _OPAQUE)
    verified = verify_policy_event_source_artifact(published.manifest_path)
    # It verifies as a source artifact...
    assert verified.manifest.artifact_key == spec.artifact_key
    # ...but the frozen PolicySourceEvidence domain gate refuses a non-official host.
    with pytest.raises(ValueError):
        verified.to_source_evidence()


# --------------------------------------------------------------------------- #
# F. Network default + trust-domain boundary of the acquisition entrypoint
# --------------------------------------------------------------------------- #
def test_default_main_refuses_network_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        acquire,
        "_build_network_transport",
        lambda: pytest.fail("network transport must not be constructed"),
    )
    with pytest.raises(SystemExit, match="network_acquisition_not_authorized"):
        acquire.main([])


def test_acquire_and_publish_rejects_unknown_key_without_network() -> None:
    transport = FakePolicyEventSourceTransport(_response(_official_spec(), _PDF))
    with pytest.raises(
        PolicyRateQualificationError, match="unknown_policy_event_source_artifact"
    ):
        acquire_and_publish_policy_event_source("fed-fixture-a", transport, RETRIEVED)
    assert transport.calls == []


def test_acquisition_module_never_constructs_events_or_qualifies() -> None:
    source = inspect.getsource(acquire)
    assert "PolicyRateEvent(" not in source
    assert "qualify_formation" not in source
    assert "CandidateBFormation" not in source


def test_acquire_and_publish_end_to_end_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / "root")
    monkeypatch.setattr(
        pes,
        "OFFICIAL_POLICY_ARTIFACT_SPECS",
        MappingProxyType({spec.artifact_key: spec}),
    )
    transport = _transport(spec, _PDF)
    published = acquire_and_publish_policy_event_source(
        spec.artifact_key, transport, RETRIEVED
    )
    assert isinstance(published, PolicyEventSourcePublication)
    assert not isinstance(published, PolicyRateEvent)
    assert len(transport.calls) == 1
    verified = verify_policy_event_source_artifact(published.manifest_path)
    assert verified.to_source_evidence().source_url == spec.approved_url


def test_failed_acquisition_manifest_cannot_become_verified_evidence(
    tmp_path: Path,
) -> None:
    # A hand-written failure/discovery-style manifest must never verify as evidence.
    destination = tmp_path / "root" / "fed-fixture-a"
    destination.mkdir(parents=True)
    (destination / "source.bin").write_bytes(_PDF)
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "candidate_b_policy_event_source_failed.v1",
                "artifact_key": "fed-fixture-a",
                "raw_sha256": hashlib.sha256(_PDF).hexdigest(),
                "byte_count": len(_PDF),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PolicyRateQualificationError):
        verify_policy_event_source_artifact(destination / "manifest.json")


def test_spec_replace_revalidates_frozen_contract() -> None:
    spec = _official_spec()
    with pytest.raises(ValueError):
        replace(spec, approved_url="http://www.federalreserve.gov/x.pdf")


# --------------------------------------------------------------------------- #
# G. Redirect provenance identity & contract tests
# --------------------------------------------------------------------------- #
def test_no_redirect_case_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _official_spec()
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    verified = verify_policy_event_source_artifact(published.manifest_path)
    assert verified.manifest.redirect_chain == ()


def test_redirect_chain_changing_one_hop_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replace(
        _official_spec(),
        approved_redirect_chain=("https://www.federalreserve.gov/hop1",),
    )
    resp = _response(
        spec, _PDF, redirect_chain=("https://www.federalreserve.gov/hop1",)
    )
    published = _publish(tmp_path, monkeypatch, spec, _PDF, response=resp)

    # Tamper with redirect chain in manifest file
    payload = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    payload["redirect_chain"] = ["https://www.federalreserve.gov/hop1-tampered"]
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(published.manifest_path)


def test_redirect_chain_adding_or_removing_hop_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replace(
        _official_spec(),
        approved_redirect_chain=("https://www.federalreserve.gov/hop1",),
    )
    resp = _response(
        spec, _PDF, redirect_chain=("https://www.federalreserve.gov/hop1",)
    )
    published = _publish(tmp_path, monkeypatch, spec, _PDF, response=resp)

    # Add a hop
    payload = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    payload["redirect_chain"].append("https://www.federalreserve.gov/hop2")
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(published.manifest_path)

    # Remove all hops
    payload["redirect_chain"] = []
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(published.manifest_path)


def test_redirect_chain_order_change_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replace(
        _official_spec(),
        approved_redirect_chain=(
            "https://www.federalreserve.gov/hop1",
            "https://www.federalreserve.gov/hop2",
        ),
    )
    resp = _response(
        spec,
        _PDF,
        redirect_chain=(
            "https://www.federalreserve.gov/hop1",
            "https://www.federalreserve.gov/hop2",
        ),
    )
    published = _publish(tmp_path, monkeypatch, spec, _PDF, response=resp)

    # Swap hop order
    payload = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    payload["redirect_chain"] = [
        "https://www.federalreserve.gov/hop2",
        "https://www.federalreserve.gov/hop1",
    ]
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(published.manifest_path)


# --------------------------------------------------------------------------- #
# H. Approved spec contract preservation tests (query, port, redirect chain)
# --------------------------------------------------------------------------- #
def test_approved_query_synthetic_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.federalreserve.gov/policy.pdf?startPeriod=2014-01-01&endPeriod=2023-12-31"
    spec = replace(
        _official_spec(),
        approved_url=url,
        approved_query="startPeriod=2014-01-01&endPeriod=2023-12-31",
    )
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    verified = verify_policy_event_source_artifact(published.manifest_path)
    assert verified.manifest.requested_url == url

    # Mismatched query in manifest
    payload = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    payload["requested_url"] = "https://www.federalreserve.gov/policy.pdf?startPeriod=2014-01-01"
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(published.manifest_path)


def test_approved_non_default_port_synthetic_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.federalreserve.gov:8443/policy.pdf"
    spec = replace(
        _official_spec(),
        approved_url=url,
        approved_port=8443,
    )
    published = _publish(tmp_path, monkeypatch, spec, _PDF)
    verified = verify_policy_event_source_artifact(published.manifest_path)
    assert verified.manifest.requested_url == url

    # Mismatched port in manifest
    payload = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    payload["requested_url"] = "https://www.federalreserve.gov:8444/policy.pdf"
    published.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(published.manifest_path)


def test_approved_redirect_chain_synthetic_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replace(
        _official_spec(),
        approved_redirect_chain=("https://www.federalreserve.gov/redirect-hop",),
    )
    resp = _response(
        spec, _PDF, redirect_chain=("https://www.federalreserve.gov/redirect-hop",)
    )
    published = _publish(tmp_path, monkeypatch, spec, _PDF, response=resp)
    verified = verify_policy_event_source_artifact(published.manifest_path)
    assert verified.manifest.redirect_chain == ("https://www.federalreserve.gov/redirect-hop",)


# --------------------------------------------------------------------------- #
# I. Adversarial forged manifest tests
# --------------------------------------------------------------------------- #
def test_adversarial_forged_manifest_unapproved_key_fails(tmp_path: Path) -> None:
    spec = _official_spec()
    resp = _response(spec, _PDF)
    manifest = pes.PolicyEventSourceManifest.from_parts(spec, resp, RETRIEVED)
    payload = pes._manifest_payload(manifest)

    # Forger changes artifact_key (even if they recompute raw_sha256/identities correctly)
    payload["artifact_key"] = "unapproved-key"
    payload["source_artifact_id"] = pes._compute_source_artifact_id(
        artifact_key="unapproved-key",
        currency=payload["currency"],
        authority=payload["authority"],
        source_kind=payload["source_kind"],
        body_format=payload["body_format"],
        event_date=date.fromisoformat(payload["event_date"]),
        requested_url=payload["requested_url"],
        returned_url=payload["returned_url"],
        response_media_type=payload["response_media_type"],
        raw_sha256=payload["raw_sha256"],
    )
    payload["acquisition_id"] = pes._compute_acquisition_id(
        source_artifact_id=payload["source_artifact_id"],
        retrieved_at=RETRIEVED,
        returned_url=payload["returned_url"],
        status_code=payload["status_code"],
        byte_count=payload["byte_count"],
        response_headers=pes._retained_headers(payload["response_headers"]),
        redirect_chain=payload["redirect_chain"],
    )
    dest = tmp_path / "forged"
    dest.mkdir(parents=True)
    (dest / "source.bin").write_bytes(_PDF)
    (dest / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyRateQualificationError, match="unknown_policy_event_source_artifact"):
        verify_policy_event_source_artifact(dest / "manifest.json")


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("requested_url", "https://www.evil.com/policy.pdf"),
        ("returned_url", "https://www.evil.com/policy.pdf"),
        ("currency", "EUR"),
        ("authority", "ECB"),
        ("source_kind", "non_authoritative_third_party"),
        ("event_date", "2022-01-01"),
        ("response_media_type", "application/json"),
    ],
)
def test_adversarial_forged_manifest_field_tampering_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    forged_value: str,
) -> None:
    spec = _official_spec()
    monkeypatch.setattr(
        pes,
        "OFFICIAL_POLICY_ARTIFACT_SPECS",
        MappingProxyType({spec.artifact_key: spec}),
    )
    resp = _response(spec, _PDF)
    manifest = pes.PolicyEventSourceManifest.from_parts(spec, resp, RETRIEVED)
    payload = pes._manifest_payload(manifest)

    # Forger modifies one field and recomputes valid self-consistent identities
    payload[field_name] = forged_value
    event_date = date.fromisoformat(payload["event_date"])
    payload["source_artifact_id"] = pes._compute_source_artifact_id(
        artifact_key=payload["artifact_key"],
        currency=payload["currency"],
        authority=payload["authority"],
        source_kind=payload["source_kind"],
        body_format=payload["body_format"],
        event_date=event_date,
        requested_url=payload["requested_url"],
        returned_url=payload["returned_url"],
        response_media_type=payload["response_media_type"],
        raw_sha256=payload["raw_sha256"],
    )
    payload["acquisition_id"] = pes._compute_acquisition_id(
        source_artifact_id=payload["source_artifact_id"],
        retrieved_at=RETRIEVED,
        returned_url=payload["returned_url"],
        status_code=payload["status_code"],
        byte_count=payload["byte_count"],
        response_headers=pes._retained_headers(payload["response_headers"]),
        redirect_chain=payload["redirect_chain"],
    )
    dest = tmp_path / "forged_field"
    dest.mkdir(parents=True)
    (dest / "source.bin").write_bytes(_PDF)
    (dest / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(dest / "manifest.json")


# --------------------------------------------------------------------------- #
# J. Direct approved_spec parameter trust-boundary tests
# --------------------------------------------------------------------------- #
def test_approved_spec_parameter_succeeds_with_empty_production_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pes, "OFFICIAL_POLICY_ARTIFACT_SPECS", MappingProxyType({}))
    spec = _official_spec()
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / "root")
    published = persist_policy_event_source_artifact(
        spec, _response(spec, _PDF), RETRIEVED
    )
    verified = verify_policy_event_source_artifact(
        published.manifest_path, approved_spec=spec
    )
    assert verified.manifest.artifact_key == spec.artifact_key


def test_approved_spec_parameter_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pes, "OFFICIAL_POLICY_ARTIFACT_SPECS", MappingProxyType({}))
    spec = _official_spec()
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / "root")
    published = persist_policy_event_source_artifact(
        spec, _response(spec, _PDF), RETRIEVED
    )
    different_spec = replace(
        spec,
        approved_url="https://www.federalreserve.gov/different.pdf",
    )
    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(
            published.manifest_path, approved_spec=different_spec
        )


def test_approved_spec_parameter_cannot_override_artifact_key_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pes, "OFFICIAL_POLICY_ARTIFACT_SPECS", MappingProxyType({}))
    spec = _official_spec()
    monkeypatch.setattr(pes, "POLICY_EVENT_SOURCE_ROOT", tmp_path / "root")
    published = persist_policy_event_source_artifact(
        spec, _response(spec, _PDF), RETRIEVED
    )
    different_key_spec = replace(spec, artifact_key="different-key")
    with pytest.raises(PolicyRateQualificationError, match="manifest_identity_mismatch"):
        verify_policy_event_source_artifact(
            published.manifest_path, approved_spec=different_key_spec
        )
