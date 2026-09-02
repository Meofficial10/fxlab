"""Synthetic offline tests for the *separate* non-authoritative policy-event
source discovery boundary.

Discovery lives in a different trust domain from evidence acquisition. Its
outputs are structurally incapable of being promoted to authoritative
qualification, final-run identity, or R4 evidence, and can never be verified as
a policy-event source artifact. All fixtures are synthetic; no network I/O.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import scripts.discover_policy_event_source as discovery
from scripts.discover_policy_event_source import (
    FAILED_POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION,
    POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION,
    PolicyEventSourceDiscoveryArtifact,
    PolicyEventSourceDiscoveryFailure,
    PolicyEventSourceDiscoveryHttpResponse,
    PolicyEventSourceDiscoveryRequest,
    execute_and_persist_discovery,
    execute_discovery,
)

from fxlab.data.policy_event_source import verify_policy_event_source_artifact
from fxlab.data.policy_rates import PolicyRateQualificationError

RETRIEVED = datetime(2026, 9, 2, 15, 30, tzinfo=UTC)

_HTML = b"<!doctype html><html><body><a href='/notice'>notice</a></body></html>"
_XML_DTD = b"<?xml version='1.0'?><!DOCTYPE n [<!ENTITY e 'v'>]><catalog>&e;</catalog>"


def _request(**overrides: object) -> PolicyEventSourceDiscoveryRequest:
    base: dict[str, object] = dict(
        url="https://docs.authority.example/catalog",
        accept="text/html",
        currency="EUR",
        note="synthetic-discovery-probe",
    )
    base.update(overrides)
    return PolicyEventSourceDiscoveryRequest(**base)  # type: ignore[arg-type]


def _response(
    request: PolicyEventSourceDiscoveryRequest,
    raw: bytes = _HTML,
    *,
    status: int = 200,
    final_url: str | None = None,
    media_type: str = "text/html",
    headers: dict[str, str] | None = None,
) -> PolicyEventSourceDiscoveryHttpResponse:
    return PolicyEventSourceDiscoveryHttpResponse(
        status_code=status,
        final_url=request.url if final_url is None else final_url,
        media_type=media_type,
        headers=headers if headers is not None else {"Content-Type": media_type},
        raw_bytes=raw,
    )


class FakeDiscoveryTransport:
    def __init__(
        self,
        response: PolicyEventSourceDiscoveryHttpResponse | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[object, str, int, int]] = []

    def fetch(
        self,
        request: object,
        *,
        exact_url: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> PolicyEventSourceDiscoveryHttpResponse:
        self.calls.append((request, exact_url, timeout_seconds, max_response_bytes))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def test_default_discovery_main_is_network_disabled_before_transport_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        discovery,
        "_build_network_transport",
        lambda: pytest.fail("discovery transport must not be constructed"),
    )
    with pytest.raises(SystemExit, match="network_discovery_not_authorized"):
        discovery.main([])


def test_discovery_artifact_cannot_be_authoritative_by_construction() -> None:
    request = _request()
    transport = FakeDiscoveryTransport(_response(request))
    artifact = execute_discovery(request, transport, RETRIEVED)
    assert isinstance(artifact, PolicyEventSourceDiscoveryArtifact)
    assert artifact.classification == POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION
    assert artifact.authoritative_qualification_eligible is False
    assert artifact.final_run_identity_eligible is False
    assert artifact.r4_evidence_eligible is False


@pytest.mark.parametrize(
    "flag",
    [
        "authoritative_qualification_eligible",
        "final_run_identity_eligible",
        "r4_evidence_eligible",
    ],
)
def test_discovery_artifact_rejects_any_authoritative_eligibility(flag: str) -> None:
    request = _request()
    transport = FakeDiscoveryTransport(_response(request))
    artifact = execute_discovery(request, transport, RETRIEVED)
    with pytest.raises(ValueError, match="discovery_cannot_be_authoritative"):
        artifact.__class__(**{**artifact.__dict__, flag: True})


def test_discovery_uses_its_own_failure_type_distinct_from_acquisition() -> None:
    assert issubclass(PolicyEventSourceDiscoveryFailure, ValueError)
    assert not issubclass(PolicyEventSourceDiscoveryFailure, PolicyRateQualificationError)
    assert not issubclass(PolicyRateQualificationError, PolicyEventSourceDiscoveryFailure)


def test_discovery_rejects_unsafe_xml_dtd_or_entity() -> None:
    request = _request(accept="application/xml")
    transport = FakeDiscoveryTransport(
        _response(request, _XML_DTD, media_type="application/xml")
    )
    with pytest.raises(PolicyEventSourceDiscoveryFailure, match="unsafe_xml_rejected"):
        execute_discovery(request, transport, RETRIEVED)


def test_discovery_timeout_is_one_attempt_without_retry() -> None:
    request = _request()
    transport = FakeDiscoveryTransport(error=TimeoutError("synthetic timeout"))
    with pytest.raises(PolicyEventSourceDiscoveryFailure, match="discovery_timeout"):
        execute_discovery(request, transport, RETRIEVED)
    assert len(transport.calls) == 1


def test_discovery_transport_failure_is_one_attempt_without_fallback() -> None:
    request = _request()
    transport = FakeDiscoveryTransport(error=ValueError("synthetic wire error"))
    with pytest.raises(PolicyEventSourceDiscoveryFailure, match="transport_failure"):
        execute_discovery(request, transport, RETRIEVED)
    assert len(transport.calls) == 1


def test_failed_discovery_is_persisted_non_authoritatively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "RAW_DISCOVERY_ROOT", tmp_path / "disc")
    request = _request()
    transport = FakeDiscoveryTransport(_response(request, status=503))
    with pytest.raises(PolicyEventSourceDiscoveryFailure):
        execute_and_persist_discovery(request, transport, RETRIEVED)
    assert len(transport.calls) == 1
    failure_files = list((tmp_path / "disc").rglob("failure.json"))
    assert len(failure_files) == 1
    payload = json.loads(failure_files[0].read_text(encoding="utf-8"))
    assert payload["classification"] == FAILED_POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION
    assert payload["authoritative_qualification_eligible"] is False
    assert payload["final_run_identity_eligible"] is False
    assert payload["r4_evidence_eligible"] is False
    # No success artifact and no temp residue.
    assert not list((tmp_path / "disc").rglob("discovery.json"))
    assert not list((tmp_path / "disc").rglob(".*"))


def test_discovery_output_cannot_become_verified_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "RAW_DISCOVERY_ROOT", tmp_path / "disc")
    request = _request()
    transport = FakeDiscoveryTransport(_response(request))
    published = execute_and_persist_discovery(request, transport, RETRIEVED)
    manifest_path = Path(published.manifest_path)
    assert manifest_path.name == "discovery.json"
    # The evidence verifier must refuse a discovery manifest outright.
    with pytest.raises(PolicyRateQualificationError):
        verify_policy_event_source_artifact(manifest_path)


def test_successful_discovery_persists_non_authoritative_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "RAW_DISCOVERY_ROOT", tmp_path / "disc")
    request = _request()
    transport = FakeDiscoveryTransport(_response(request))
    published = execute_and_persist_discovery(request, transport, RETRIEVED)
    payload = json.loads(Path(published.manifest_path).read_text(encoding="utf-8"))
    assert payload["classification"] == POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION
    assert payload["authoritative_qualification_eligible"] is False
    assert Path(published.raw_path).read_bytes() == _HTML


def test_discovery_request_rejects_non_https_and_sealed_event_date() -> None:
    with pytest.raises(ValueError):
        _request(url="http://docs.authority.example/catalog")
    # A sealed-window date supplied to discovery is rejected date-first.
    with pytest.raises(ValueError):
        _request(observation_date=date(2024, 1, 1))
