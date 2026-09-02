"""Separate, non-authoritative Candidate B policy-event *source discovery*
boundary.

Discovery lives in a different trust domain from evidence acquisition. Its whole
purpose is to explore where official policy-event material *might* live; it must
never establish authoritative facts. Every artifact it emits — success or
failure — is stamped ``NON_AUTHORITATIVE_POLICY_EVENT_SOURCE_DISCOVERY`` and
carries hard-``False`` eligibility flags, and the dataclasses refuse
construction if any eligibility flag is flipped true. A discovery manifest uses
a distinct schema, so the evidence verifier refuses it outright: discovery
output can never be promoted to a verified policy-event source artifact.

Network access is disabled by default: :func:`main` refuses to construct a
network transport unless ``--authorize-network-discovery`` is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    MAX_OBSERVATION_DATE,
    _media_type,
    _text,
    _utc,
    canonical_sha256,
)

POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION = "NON_AUTHORITATIVE_POLICY_EVENT_SOURCE_DISCOVERY"
FAILED_POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION = (
    "NON_AUTHORITATIVE_POLICY_EVENT_SOURCE_DISCOVERY_FAILED"
)
RAW_DISCOVERY_ROOT = Path("data/raw/candidate_b/policy_event_source_discovery")
DISCOVERY_TIMEOUT_SECONDS = 15
DISCOVERY_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_RETAINED_DISCOVERY_HEADERS = frozenset(
    {"content-type", "etag", "last-modified", "content-length"}
)
_DISCOVERY_HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class PolicyEventSourceDiscoveryFailure(ValueError):
    """Discovery-domain failure. Deliberately a *sibling* of
    ``PolicyRateQualificationError`` (both subclass ``ValueError``) so that the
    two trust domains never share a rejection type."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _validated_discovery_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("discovery url is malformed")
    if unicodedata.normalize("NFKC", value) != value:
        raise ValueError("discovery url has ambiguous Unicode normalization")
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise ValueError("discovery url must use https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("discovery url must not contain userinfo")
    if parsed.fragment:
        raise ValueError("discovery url must not contain a fragment")
    host = (parsed.hostname or "").lower()
    if not host or not _DISCOVERY_HOST_RE.fullmatch(host):
        raise ValueError("discovery url host is malformed")
    return value


@dataclass(frozen=True)
class PolicyEventSourceDiscoveryRequest:
    url: str
    accept: str
    currency: str
    note: str
    observation_date: date | None = None

    def __post_init__(self) -> None:
        # Date-first: a sealed-window date is rejected before anything else.
        if self.observation_date is not None and (
            not isinstance(self.observation_date, date)
            or isinstance(self.observation_date, datetime)
            or self.observation_date > MAX_OBSERVATION_DATE
        ):
            raise ValueError("discovery observation_date is sealed or invalid")
        object.__setattr__(self, "url", _validated_discovery_url(self.url))
        object.__setattr__(self, "accept", _media_type(self.accept))
        currency = self.currency.strip().upper() if isinstance(self.currency, str) else ""
        if currency not in APPROVED_BIS_SERIES:
            raise ValueError("discovery currency is unsupported")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "note", _text(self.note, "note"))

    @property
    def request_identity(self) -> str:
        return canonical_sha256(
            {
                "format": 1,
                "url": self.url,
                "accept": self.accept,
                "currency": self.currency,
                "observation_date": self.observation_date,
            }
        )


@dataclass(frozen=True)
class PolicyEventSourceDiscoveryHttpResponse:
    status_code: int
    final_url: str
    media_type: str
    headers: Mapping[str, str]
    raw_bytes: bytes

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise ValueError("status_code must be an integer")
        object.__setattr__(self, "final_url", str(self.final_url))
        object.__setattr__(self, "media_type", str(self.media_type).strip().lower())
        object.__setattr__(self, "headers", dict(self.headers))
        if not isinstance(self.raw_bytes, (bytes, bytearray)):
            raise ValueError("raw_bytes must be bytes")
        object.__setattr__(self, "raw_bytes", bytes(self.raw_bytes))


@runtime_checkable
class PolicyEventSourceDiscoveryTransport(Protocol):
    def fetch(
        self,
        request: PolicyEventSourceDiscoveryRequest,
        *,
        exact_url: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> PolicyEventSourceDiscoveryHttpResponse: ...


@dataclass(frozen=True)
class PolicyEventSourceDiscoveryArtifact:
    classification: str
    request_identity: str
    exact_url: str
    http_status: int
    content_type: str
    raw_sha256: str
    byte_count: int
    response_headers: Mapping[str, str]
    retrieved_at: datetime
    authoritative_qualification_eligible: bool = False
    final_run_identity_eligible: bool = False
    r4_evidence_eligible: bool = False

    def __post_init__(self) -> None:
        if (
            self.authoritative_qualification_eligible
            or self.final_run_identity_eligible
            or self.r4_evidence_eligible
        ):
            raise ValueError("discovery_cannot_be_authoritative")
        if self.classification != POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION:
            raise ValueError("discovery_classification_invalid")
        object.__setattr__(self, "response_headers", dict(self.response_headers))


@dataclass(frozen=True)
class FailedPolicyEventSourceDiscoveryArtifact:
    classification: str
    request_identity: str
    exact_url: str
    failure_reason: str
    http_status: int | None
    content_type: str
    raw_sha256: str
    byte_count: int
    response_headers: Mapping[str, str]
    retrieved_at: datetime
    authoritative_qualification_eligible: bool = False
    final_run_identity_eligible: bool = False
    r4_evidence_eligible: bool = False

    def __post_init__(self) -> None:
        if (
            self.authoritative_qualification_eligible
            or self.final_run_identity_eligible
            or self.r4_evidence_eligible
        ):
            raise ValueError("discovery_cannot_be_authoritative")
        if self.classification != FAILED_POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION:
            raise ValueError("discovery_classification_invalid")
        object.__setattr__(self, "response_headers", dict(self.response_headers))


@dataclass(frozen=True)
class PolicyEventSourceDiscoveryPublication:
    manifest_path: Path
    raw_path: Path
    artifact: PolicyEventSourceDiscoveryArtifact


def _retained_discovery_headers(headers: Mapping[str, str]) -> dict[str, str]:
    retained: dict[str, str] = {}
    for key, value in headers.items():
        lowered = str(key).strip().lower()
        if lowered in _RETAINED_DISCOVERY_HEADERS:
            retained[lowered] = str(value)
    return retained


def _looks_like_xml(accept: str, media_type: str) -> bool:
    return "xml" in accept.lower() or "xml" in media_type.lower()


def _reject_unsafe_xml(raw: bytes) -> None:
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PolicyEventSourceDiscoveryFailure("discovery_response_body_invalid") from exc
    upper = decoded.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise PolicyEventSourceDiscoveryFailure("unsafe_xml_rejected")


def _single_discovery_attempt(
    request: PolicyEventSourceDiscoveryRequest,
    transport: PolicyEventSourceDiscoveryTransport,
    timeout_seconds: int,
    max_response_bytes: int,
) -> PolicyEventSourceDiscoveryHttpResponse:
    try:
        return transport.fetch(
            request,
            exact_url=request.url,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
    except PolicyEventSourceDiscoveryFailure:
        raise
    except TimeoutError as exc:
        raise PolicyEventSourceDiscoveryFailure("discovery_timeout") from exc
    except Exception as exc:  # exactly one attempt, no retry, no fallback
        raise PolicyEventSourceDiscoveryFailure("transport_failure") from exc


def _enforce_discovery_response(
    request: PolicyEventSourceDiscoveryRequest,
    response: PolicyEventSourceDiscoveryHttpResponse,
    max_response_bytes: int,
) -> None:
    if not isinstance(response, PolicyEventSourceDiscoveryHttpResponse):
        raise PolicyEventSourceDiscoveryFailure("transport_failure")
    if response.final_url != request.url:
        raise PolicyEventSourceDiscoveryFailure("discovery_redirect_rejected")
    if response.status_code != 200:
        raise PolicyEventSourceDiscoveryFailure("discovery_http_status_not_success")
    raw = response.raw_bytes
    if not isinstance(raw, bytes) or not raw:
        raise PolicyEventSourceDiscoveryFailure("discovery_response_body_invalid")
    if len(raw) > max_response_bytes:
        raise PolicyEventSourceDiscoveryFailure("discovery_response_too_large")
    if _looks_like_xml(request.accept, response.media_type):
        _reject_unsafe_xml(raw)


def _build_discovery_artifact(
    request: PolicyEventSourceDiscoveryRequest,
    response: PolicyEventSourceDiscoveryHttpResponse,
    retrieved: datetime,
) -> PolicyEventSourceDiscoveryArtifact:
    raw = response.raw_bytes
    return PolicyEventSourceDiscoveryArtifact(
        classification=POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION,
        request_identity=request.request_identity,
        exact_url=request.url,
        http_status=response.status_code,
        content_type=response.media_type,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        response_headers=_retained_discovery_headers(response.headers),
        retrieved_at=retrieved,
    )


def execute_discovery(
    request: PolicyEventSourceDiscoveryRequest,
    transport: PolicyEventSourceDiscoveryTransport,
    retrieved_at: datetime,
    *,
    timeout_seconds: int = DISCOVERY_TIMEOUT_SECONDS,
    max_response_bytes: int = DISCOVERY_MAX_RESPONSE_BYTES,
) -> PolicyEventSourceDiscoveryArtifact:
    if not isinstance(request, PolicyEventSourceDiscoveryRequest):
        raise PolicyEventSourceDiscoveryFailure("discovery_request_invalid")
    retrieved = _utc(retrieved_at, "retrieved_at")
    response = _single_discovery_attempt(
        request, transport, timeout_seconds, max_response_bytes
    )
    _enforce_discovery_response(request, response, max_response_bytes)
    return _build_discovery_artifact(request, response, retrieved)


def _discovery_destination(request: PolicyEventSourceDiscoveryRequest) -> Path:
    return RAW_DISCOVERY_ROOT / request.request_identity


def _write_fully(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write_dir(destination: Path, files: tuple[tuple[str, bytes], ...]) -> None:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".tmp-", dir=parent))
    try:
        for name, data in files:
            _write_fully(temporary / name, data)
        if destination.exists():
            raise PolicyEventSourceDiscoveryFailure("discovery_destination_exists")
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _discovery_success_payload(artifact: PolicyEventSourceDiscoveryArtifact) -> dict[str, object]:
    return {
        "schema": artifact.classification,
        "classification": artifact.classification,
        "request_identity": artifact.request_identity,
        "exact_url": artifact.exact_url,
        "http_status": artifact.http_status,
        "content_type": artifact.content_type,
        "raw_sha256": artifact.raw_sha256,
        "byte_count": artifact.byte_count,
        "response_headers": dict(artifact.response_headers),
        "retrieved_at": artifact.retrieved_at.isoformat(),
        "authoritative_qualification_eligible": artifact.authoritative_qualification_eligible,
        "final_run_identity_eligible": artifact.final_run_identity_eligible,
        "r4_evidence_eligible": artifact.r4_evidence_eligible,
    }


def _discovery_failure_payload(
    artifact: FailedPolicyEventSourceDiscoveryArtifact,
) -> dict[str, object]:
    return {
        "schema": artifact.classification,
        "classification": artifact.classification,
        "request_identity": artifact.request_identity,
        "exact_url": artifact.exact_url,
        "failure_reason": artifact.failure_reason,
        "http_status": artifact.http_status,
        "content_type": artifact.content_type,
        "raw_sha256": artifact.raw_sha256,
        "byte_count": artifact.byte_count,
        "response_headers": dict(artifact.response_headers),
        "retrieved_at": artifact.retrieved_at.isoformat(),
        "authoritative_qualification_eligible": artifact.authoritative_qualification_eligible,
        "final_run_identity_eligible": artifact.final_run_identity_eligible,
        "r4_evidence_eligible": artifact.r4_evidence_eligible,
    }


def _persist_discovery_success(
    request: PolicyEventSourceDiscoveryRequest,
    response: PolicyEventSourceDiscoveryHttpResponse,
    artifact: PolicyEventSourceDiscoveryArtifact,
) -> PolicyEventSourceDiscoveryPublication:
    destination = _discovery_destination(request)
    payload = json.dumps(
        _discovery_success_payload(artifact), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _atomic_write_dir(
        destination, (("discovery.json", payload), ("response.bin", response.raw_bytes))
    )
    return PolicyEventSourceDiscoveryPublication(
        manifest_path=destination / "discovery.json",
        raw_path=destination / "response.bin",
        artifact=artifact,
    )


def _persist_discovery_failure(
    request: PolicyEventSourceDiscoveryRequest,
    response: PolicyEventSourceDiscoveryHttpResponse,
    failure: PolicyEventSourceDiscoveryFailure,
    retrieved: datetime,
) -> None:
    artifact = FailedPolicyEventSourceDiscoveryArtifact(
        classification=FAILED_POLICY_EVENT_SOURCE_DISCOVERY_CLASSIFICATION,
        request_identity=request.request_identity,
        exact_url=request.url,
        failure_reason=failure.reason,
        http_status=response.status_code,
        content_type=response.media_type,
        raw_sha256=hashlib.sha256(response.raw_bytes).hexdigest(),
        byte_count=len(response.raw_bytes),
        response_headers=_retained_discovery_headers(response.headers),
        retrieved_at=retrieved,
    )
    destination = _discovery_destination(request)
    payload = json.dumps(
        _discovery_failure_payload(artifact), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _atomic_write_dir(
        destination, (("failure.json", payload), ("response.bin", response.raw_bytes))
    )


def execute_and_persist_discovery(
    request: PolicyEventSourceDiscoveryRequest,
    transport: PolicyEventSourceDiscoveryTransport,
    retrieved_at: datetime,
) -> PolicyEventSourceDiscoveryPublication:
    if not isinstance(request, PolicyEventSourceDiscoveryRequest):
        raise PolicyEventSourceDiscoveryFailure("discovery_request_invalid")
    retrieved = _utc(retrieved_at, "retrieved_at")
    response: PolicyEventSourceDiscoveryHttpResponse | None = None
    try:
        response = _single_discovery_attempt(
            request, transport, DISCOVERY_TIMEOUT_SECONDS, DISCOVERY_MAX_RESPONSE_BYTES
        )
        _enforce_discovery_response(request, response, DISCOVERY_MAX_RESPONSE_BYTES)
        artifact = _build_discovery_artifact(request, response, retrieved)
    except PolicyEventSourceDiscoveryFailure as failure:
        if (
            isinstance(response, PolicyEventSourceDiscoveryHttpResponse)
            and isinstance(response.raw_bytes, bytes)
            and response.raw_bytes
        ):
            _persist_discovery_failure(request, response, failure, retrieved)
        raise
    return _persist_discovery_success(request, response, artifact)


class _NoDiscoveryRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        del req, fp, code, msg, headers, newurl
        raise PolicyEventSourceDiscoveryFailure("discovery_redirect_rejected")


class _UrllibDiscoveryTransport:
    """Minimal one-shot HTTPS transport with redirects disabled and a bounded,
    non-truncating read. Only constructed inside :func:`main` after explicit
    network authorization; never exercised by the offline suite."""

    def fetch(
        self,
        request: PolicyEventSourceDiscoveryRequest,
        *,
        exact_url: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> PolicyEventSourceDiscoveryHttpResponse:
        if not exact_url.startswith("https://"):
            raise PolicyEventSourceDiscoveryFailure("insecure_scheme_rejected")
        req = urllib.request.Request(
            exact_url, method="GET", headers={"Accept": request.accept}
        )
        opener = urllib.request.build_opener(_NoDiscoveryRedirectHandler)
        with opener.open(req, timeout=timeout_seconds) as handle:
            status = getattr(handle, "status", None) or handle.getcode()
            final_url = handle.geturl()
            media_type = handle.headers.get_content_type() if handle.headers else ""
            raw_bytes = handle.read(max_response_bytes + 1)
            headers = dict(handle.headers.items()) if handle.headers else {}
        return PolicyEventSourceDiscoveryHttpResponse(
            status_code=int(status),
            final_url=final_url,
            media_type=media_type,
            headers=headers,
            raw_bytes=raw_bytes,
        )


def _build_network_transport() -> PolicyEventSourceDiscoveryTransport:
    return _UrllibDiscoveryTransport()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a non-authoritative Candidate B policy-event source discovery probe."
    )
    parser.add_argument("--url")
    parser.add_argument("--accept", default="text/html")
    parser.add_argument("--currency")
    parser.add_argument("--note", default="manual-discovery-probe")
    parser.add_argument("--authorize-network-discovery", action="store_true")
    args = parser.parse_args(argv)
    if not args.authorize_network_discovery:
        raise SystemExit("network_discovery_not_authorized")
    if not args.url or not args.currency:
        raise SystemExit("discovery_url_and_currency_required")
    transport = _build_network_transport()
    request = PolicyEventSourceDiscoveryRequest(
        url=args.url, accept=args.accept, currency=args.currency, note=args.note
    )
    published = execute_and_persist_discovery(request, transport, datetime.now(tz=UTC))
    print(published.manifest_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
