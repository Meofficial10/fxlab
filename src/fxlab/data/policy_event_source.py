"""Candidate B *policy-event source artifact* contracts, immutable persistence,
and independent verification.

This module is the evidence-acquisition trust domain for Candidate B policy
events. It freezes the *shape* of an official policy-event source artifact and
the machinery to persist and re-verify one; it deliberately freezes **no real
authority facts**. ``OFFICIAL_POLICY_ARTIFACT_SPECS`` ships empty and stays
empty until a separately authorized discovery establishes real URLs, endpoints,
instrument identifiers, redirect chains, and document formats.

A verified artifact can bridge to exactly one downstream type — the frozen
``PolicySourceEvidence`` from :mod:`fxlab.data.policy_rates`. It cannot and does
not construct a ``PolicyRateEvent`` or invoke qualification. The single
authoritative-domain checkpoint remains ``PolicySourceEvidence`` itself, so an
artifact acquired from a non-official host verifies as an artifact yet can never
become evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit

from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    MAX_OBSERVATION_DATE,
    EvidenceClassification,
    PolicyRateQualificationError,
    PolicySourceEvidence,
    _identifier,
    _media_type,
    _text,
    _utc,
    canonical_sha256,
)

POLICY_EVENT_SOURCE_ARTIFACT_SCHEMA = "candidate_b_policy_event_source_artifact.v1"
FAILED_POLICY_EVENT_SOURCE_SCHEMA = "candidate_b_policy_event_source_failed.v1"
POLICY_EVENT_SOURCE_ROOT = Path("data/raw/candidate_b/policy_event_source")
RETAINED_RESPONSE_HEADERS = frozenset(
    {"content-type", "etag", "last-modified", "content-length"}
)
_RAW_FILENAME = "source.bin"
_MANIFEST_FILENAME = "manifest.json"
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class PolicyEventSourceBodyFormat(StrEnum):
    """Generic body shape of an acquired artifact — NOT a document parser."""

    XML = "xml"
    PDF = "pdf"
    OPAQUE = "opaque"


def _validated_host(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("authority_host must be a string")
    host = value.strip().lower()
    if not host or host != value.strip() or not _HOST_RE.fullmatch(host):
        raise ValueError("authority_host is malformed")
    return host


def _validated_spec_url(
    value: object,
    *,
    authority_host: str,
    approved_port: int | None,
    approved_query: str | None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("approved_url is malformed")
    if unicodedata.normalize("NFKC", value) != value:
        raise ValueError("approved_url has ambiguous Unicode normalization")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("approved_url has an invalid port") from exc
    if parsed.scheme != "https":
        raise ValueError("approved_url must use https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("approved_url must not contain userinfo")
    if parsed.fragment:
        raise ValueError("approved_url must not contain a fragment")
    if host != authority_host:
        raise ValueError("approved_url host does not match authority_host")
    if port != approved_port:
        raise ValueError("approved_url port is not frozen")
    if (parsed.query or None) != approved_query:
        raise ValueError("approved_url query is not frozen")
    decoded_path = parsed.path
    for _ in range(4):
        if re.search(r"%(?:2f|5c)", decoded_path, flags=re.IGNORECASE):
            raise ValueError("approved_url path is unsafe")
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    if (
        "%" in decoded_path
        or "\\" in decoded_path
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded_path)
        or any(segment == ".." for segment in decoded_path.split("/"))
    ):
        raise ValueError("approved_url path is unsafe")
    return value


@dataclass(frozen=True)
class OfficialPolicyArtifactSpec:
    """Frozen description of a single official policy-event source artifact.

    The real inventory is empty. Synthetic specs drive tests; a real spec is
    only ever created by separately authorized discovery.
    """

    artifact_key: str
    currency: str
    authority: str
    source_kind: EvidenceClassification
    body_format: PolicyEventSourceBodyFormat
    event_date: date
    approved_url: str
    authority_host: str
    accept_media_type: str
    response_media_type: str
    approved_query: str | None = None
    approved_port: int | None = None
    approved_redirect_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_key", _identifier(self.artifact_key, "artifact_key"))
        currency = self.currency.strip().upper() if isinstance(self.currency, str) else ""
        if currency not in APPROVED_BIS_SERIES:
            raise ValueError("spec currency is unsupported")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "authority", _identifier(self.authority, "authority"))
        if not isinstance(self.source_kind, EvidenceClassification):
            raise ValueError("source_kind must be an EvidenceClassification")
        if not isinstance(self.body_format, PolicyEventSourceBodyFormat):
            raise ValueError("body_format is invalid")
        if not isinstance(self.event_date, date) or isinstance(self.event_date, datetime):
            raise ValueError("event_date must be a calendar date")
        object.__setattr__(self, "authority_host", _validated_host(self.authority_host))
        object.__setattr__(self, "accept_media_type", _media_type(self.accept_media_type))
        object.__setattr__(self, "response_media_type", _media_type(self.response_media_type))
        if self.approved_query is not None:
            object.__setattr__(
                self, "approved_query", _text(self.approved_query, "approved_query")
            )
        if self.approved_port is not None and (
            isinstance(self.approved_port, bool)
            or not isinstance(self.approved_port, int)
            or not 0 < self.approved_port <= 65535
        ):
            raise ValueError("approved_port is invalid")
        chain = tuple(self.approved_redirect_chain)
        for hop in chain:
            if not isinstance(hop, str) or not hop:
                raise ValueError("approved_redirect_chain entries must be non-empty strings")
        object.__setattr__(self, "approved_redirect_chain", chain)
        object.__setattr__(
            self,
            "approved_url",
            _validated_spec_url(
                self.approved_url,
                authority_host=self.authority_host,
                approved_port=self.approved_port,
                approved_query=self.approved_query,
            ),
        )


OFFICIAL_POLICY_ARTIFACT_SPECS: Mapping[str, OfficialPolicyArtifactSpec] = MappingProxyType({})


def resolve_official_policy_artifact_spec(artifact_key: object) -> OfficialPolicyArtifactSpec:
    try:
        key = _identifier(artifact_key, "artifact_key")
    except ValueError as exc:
        raise PolicyRateQualificationError("unknown_policy_event_source_artifact") from exc
    spec = OFFICIAL_POLICY_ARTIFACT_SPECS.get(key)
    if spec is None:
        raise PolicyRateQualificationError("unknown_policy_event_source_artifact")
    return spec


@dataclass(frozen=True)
class PolicyEventSourceHttpResponse:
    """Transport-boundary DTO. Validation lives in the acquisition and
    persistence layers, not here."""

    status_code: int
    final_url: str
    media_type: str
    headers: Mapping[str, str]
    raw_bytes: bytes
    redirect_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise ValueError("status_code must be an integer")
        object.__setattr__(self, "final_url", str(self.final_url))
        object.__setattr__(self, "media_type", str(self.media_type).strip().lower())
        object.__setattr__(self, "headers", dict(self.headers))
        if not isinstance(self.raw_bytes, (bytes, bytearray)):
            raise ValueError("raw_bytes must be bytes")
        object.__setattr__(self, "raw_bytes", bytes(self.raw_bytes))
        object.__setattr__(self, "redirect_chain", tuple(self.redirect_chain))


@runtime_checkable
class PolicyEventSourceTransport(Protocol):
    def fetch(
        self,
        spec: OfficialPolicyArtifactSpec,
        *,
        exact_url: str,
        accept: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> PolicyEventSourceHttpResponse: ...


def _retained_headers(headers: Mapping[str, str]) -> dict[str, str]:
    retained: dict[str, str] = {}
    for key, value in headers.items():
        lowered = str(key).strip().lower()
        if lowered in RETAINED_RESPONSE_HEADERS:
            retained[lowered] = str(value)
    return retained


def _reject_unsafe_xml_then_parse(raw: bytes) -> None:
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PolicyRateQualificationError("response_body_format_invalid") from exc
    upper = decoded.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise PolicyRateQualificationError("unsafe_xml_rejected")
    try:
        ET.fromstring(raw)
    except (ET.ParseError, UnicodeError, ValueError) as exc:
        raise PolicyRateQualificationError("response_body_format_invalid") from exc


def _validate_body_format(body_format: PolicyEventSourceBodyFormat, raw: bytes) -> None:
    if not isinstance(raw, bytes) or not raw:
        raise PolicyRateQualificationError("response_body_format_invalid")
    if body_format is PolicyEventSourceBodyFormat.PDF:
        if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw:
            raise PolicyRateQualificationError("response_body_format_invalid")
    elif body_format is PolicyEventSourceBodyFormat.XML:
        _reject_unsafe_xml_then_parse(raw)
    elif body_format is PolicyEventSourceBodyFormat.OPAQUE:
        return
    else:  # pragma: no cover - defensive
        raise PolicyRateQualificationError("response_body_format_invalid")


def _compute_source_artifact_id(
    *,
    artifact_key: str,
    currency: str,
    authority: str,
    source_kind: str,
    body_format: str,
    event_date: date,
    requested_url: str,
    returned_url: str,
    response_media_type: str,
    raw_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "format": 1,
            "schema": POLICY_EVENT_SOURCE_ARTIFACT_SCHEMA,
            "artifact_key": artifact_key,
            "currency": currency,
            "authority": authority,
            "source_kind": source_kind,
            "body_format": body_format,
            "event_date": event_date,
            "requested_url": requested_url,
            "returned_url": returned_url,
            "response_media_type": response_media_type,
            "raw_sha256": raw_sha256,
        }
    )


def _compute_acquisition_id(
    *,
    source_artifact_id: str,
    retrieved_at: datetime,
    returned_url: str,
    status_code: int,
    byte_count: int,
    response_headers: Mapping[str, str],
    redirect_chain: Sequence[str] = (),
) -> str:
    return canonical_sha256(
        {
            "format": 1,
            "source_artifact_id": source_artifact_id,
            "retrieved_at": retrieved_at,
            "returned_url": returned_url,
            "status_code": status_code,
            "byte_count": byte_count,
            "response_headers": dict(response_headers),
            "redirect_chain": list(redirect_chain),
        }
    )


@dataclass(frozen=True, init=False)
class PolicyEventSourceManifest:
    """Content-addressed manifest with a two-tier identity.

    ``source_artifact_id`` is the semantic (content) identity: it is independent
    of *when* the artifact was retrieved. ``acquisition_id`` is the audit
    identity: it additionally binds retrieval time, returned URL, status,
    byte count, allow-listed response headers, and redirect provenance.
    """

    schema: str
    artifact_key: str
    currency: str
    authority: str
    source_kind: str
    body_format: str
    event_date: date
    requested_url: str
    returned_url: str
    response_media_type: str
    raw_sha256: str
    byte_count: int
    retrieved_at: datetime
    status_code: int
    response_headers: Mapping[str, str]
    redirect_chain: tuple[str, ...]
    source_artifact_id: str
    acquisition_id: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("use PolicyEventSourceManifest.from_parts with exact raw bytes")

    @classmethod
    def from_parts(
        cls,
        spec: OfficialPolicyArtifactSpec,
        response: PolicyEventSourceHttpResponse,
        retrieved_at: datetime,
    ) -> PolicyEventSourceManifest:
        if not isinstance(spec, OfficialPolicyArtifactSpec):
            raise PolicyRateQualificationError("spec_not_approved")
        if not isinstance(response, PolicyEventSourceHttpResponse):
            raise PolicyRateQualificationError("response_not_approved")
        retrieved = _utc(retrieved_at, "retrieved_at")
        # Date-first sealed-window rejection, before any body interpretation.
        if spec.event_date > MAX_OBSERVATION_DATE:
            raise PolicyRateQualificationError("sealed_window_violation")
        if response.status_code != 200:
            raise PolicyRateQualificationError("response_status_invalid")
        if response.final_url != spec.approved_url:
            raise PolicyRateQualificationError("returned_url_not_bound")
        if response.media_type != spec.response_media_type:
            raise PolicyRateQualificationError("media_type_not_approved")
        raw = response.raw_bytes
        _validate_body_format(spec.body_format, raw)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        byte_count = len(raw)
        headers = _retained_headers(response.headers)
        source_kind = spec.source_kind.value
        body_format = spec.body_format.value
        source_artifact_id = _compute_source_artifact_id(
            artifact_key=spec.artifact_key,
            currency=spec.currency,
            authority=spec.authority,
            source_kind=source_kind,
            body_format=body_format,
            event_date=spec.event_date,
            requested_url=spec.approved_url,
            returned_url=response.final_url,
            response_media_type=spec.response_media_type,
            raw_sha256=raw_sha256,
        )
        acquisition_id = _compute_acquisition_id(
            source_artifact_id=source_artifact_id,
            retrieved_at=retrieved,
            returned_url=response.final_url,
            status_code=response.status_code,
            byte_count=byte_count,
            response_headers=headers,
            redirect_chain=response.redirect_chain,
        )
        instance = object.__new__(cls)
        for name, value in (
            ("schema", POLICY_EVENT_SOURCE_ARTIFACT_SCHEMA),
            ("artifact_key", spec.artifact_key),
            ("currency", spec.currency),
            ("authority", spec.authority),
            ("source_kind", source_kind),
            ("body_format", body_format),
            ("event_date", spec.event_date),
            ("requested_url", spec.approved_url),
            ("returned_url", response.final_url),
            ("response_media_type", spec.response_media_type),
            ("raw_sha256", raw_sha256),
            ("byte_count", byte_count),
            ("retrieved_at", retrieved),
            ("status_code", response.status_code),
            ("response_headers", MappingProxyType(dict(headers))),
            ("redirect_chain", tuple(response.redirect_chain)),
            ("source_artifact_id", source_artifact_id),
            ("acquisition_id", acquisition_id),
        ):
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True)
class PolicyEventSourcePublication:
    destination: Path
    raw_path: Path
    manifest_path: Path
    manifest: PolicyEventSourceManifest


@dataclass(frozen=True)
class VerifiedPolicyEventSourceArtifact:
    """An independently re-verified source artifact.

    It bridges to exactly one downstream type: the frozen
    ``PolicySourceEvidence``. It intentionally exposes no path to a
    ``PolicyRateEvent`` or to qualification.
    """

    manifest: PolicyEventSourceManifest
    raw_bytes: bytes

    def to_source_evidence(self) -> PolicySourceEvidence:
        return PolicySourceEvidence(
            source_url=self.manifest.returned_url,
            retrieved_at=self.manifest.retrieved_at,
            content_hash=self.manifest.raw_sha256,
            byte_count=self.manifest.byte_count,
            media_type=self.manifest.response_media_type,
            source_kind=self.manifest.source_kind,
        )


def policy_event_source_paths(spec: OfficialPolicyArtifactSpec) -> tuple[Path, Path, Path]:
    if not isinstance(spec, OfficialPolicyArtifactSpec):
        raise ValueError("a validated spec is required")
    destination = POLICY_EVENT_SOURCE_ROOT / spec.artifact_key
    return destination, destination / _RAW_FILENAME, destination / _MANIFEST_FILENAME


def _manifest_payload(manifest: PolicyEventSourceManifest) -> dict[str, object]:
    return {
        "schema": manifest.schema,
        "artifact_key": manifest.artifact_key,
        "currency": manifest.currency,
        "authority": manifest.authority,
        "source_kind": manifest.source_kind,
        "body_format": manifest.body_format,
        "event_date": manifest.event_date.isoformat(),
        "requested_url": manifest.requested_url,
        "returned_url": manifest.returned_url,
        "response_media_type": manifest.response_media_type,
        "raw_sha256": manifest.raw_sha256,
        "byte_count": manifest.byte_count,
        "retrieved_at": manifest.retrieved_at.isoformat(),
        "status_code": manifest.status_code,
        "response_headers": dict(manifest.response_headers),
        "redirect_chain": list(manifest.redirect_chain),
        "source_artifact_id": manifest.source_artifact_id,
        "acquisition_id": manifest.acquisition_id,
    }


def _write_fully(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_publication(
    destination: Path,
    files: tuple[tuple[str, bytes], ...],
) -> None:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".tmp-", dir=parent))
    try:
        for name, data in files:
            _write_fully(temporary / name, data)
        if destination.exists():
            raise PolicyRateQualificationError("destination_exists")
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def persist_policy_event_source_artifact(
    spec: OfficialPolicyArtifactSpec,
    response: PolicyEventSourceHttpResponse,
    retrieved_at: datetime,
) -> PolicyEventSourcePublication:
    manifest = PolicyEventSourceManifest.from_parts(spec, response, retrieved_at)
    destination, raw_path, manifest_path = policy_event_source_paths(spec)
    if destination.exists():
        raise PolicyRateQualificationError("destination_exists")
    payload = json.dumps(
        _manifest_payload(manifest), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _atomic_publication(
        destination,
        ((_RAW_FILENAME, response.raw_bytes), (_MANIFEST_FILENAME, payload)),
    )
    return PolicyEventSourcePublication(
        destination=destination,
        raw_path=raw_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def verify_policy_event_source_artifact(
    manifest_path: Path | str,
    approved_spec: OfficialPolicyArtifactSpec | None = None,
) -> VerifiedPolicyEventSourceArtifact:
    manifest_path = Path(manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyRateQualificationError("manifest_unreadable") from exc
    if not isinstance(payload, dict) or (
        payload.get("schema") != POLICY_EVENT_SOURCE_ARTIFACT_SCHEMA
    ):
        raise PolicyRateQualificationError("not_a_policy_event_source_artifact")

    # Date-first sealed-window rejection, before any content or identity use.
    try:
        event_date = date.fromisoformat(str(payload["event_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyRateQualificationError("manifest_field_invalid") from exc
    if event_date > MAX_OBSERVATION_DATE:
        raise PolicyRateQualificationError("sealed_window_violation")

    # Resolve approved spec independently from registry or argument (NEVER from manifest payload).
    artifact_key = str(payload.get("artifact_key", ""))
    if approved_spec is None:
        spec = OFFICIAL_POLICY_ARTIFACT_SPECS.get(artifact_key)
        if spec is None:
            raise PolicyRateQualificationError("unknown_policy_event_source_artifact")
    else:
        spec = approved_spec

    # Independently verify that every approved spec contract field matches the manifest.
    redirect_chain = tuple(str(hop) for hop in payload.get("redirect_chain", ()))
    requested_url = str(payload.get("requested_url", ""))
    returned_url = str(payload.get("returned_url", ""))
    parsed_req = urlsplit(requested_url)
    parsed_ret = urlsplit(returned_url)

    if (
        payload.get("artifact_key") != spec.artifact_key
        or payload.get("currency") != spec.currency
        or payload.get("authority") != spec.authority
        or payload.get("source_kind") != spec.source_kind.value
        or payload.get("body_format") != spec.body_format.value
        or event_date != spec.event_date
        or requested_url != spec.approved_url
        or payload.get("response_media_type") != spec.response_media_type
        or redirect_chain != spec.approved_redirect_chain
    ):
        raise PolicyRateQualificationError("manifest_identity_mismatch")

    if (parsed_req.query or None) != spec.approved_query:
        raise PolicyRateQualificationError("manifest_identity_mismatch")

    try:
        requested_port = parsed_req.port
        returned_port = parsed_ret.port
    except ValueError as exc:
        raise PolicyRateQualificationError("manifest_field_invalid") from exc

    if (
        requested_port != spec.approved_port
        or returned_port != spec.approved_port
    ):
        raise PolicyRateQualificationError("manifest_identity_mismatch")

    req_host = (parsed_req.hostname or "").lower()
    ret_host = (parsed_ret.hostname or "").lower()
    if req_host != spec.authority_host or ret_host != spec.authority_host:
        raise PolicyRateQualificationError("manifest_identity_mismatch")

    raw_path = manifest_path.parent / _RAW_FILENAME
    try:
        raw_bytes = raw_path.read_bytes()
    except OSError as exc:
        raise PolicyRateQualificationError("raw_content_missing") from exc
    if hashlib.sha256(raw_bytes).hexdigest() != payload.get("raw_sha256"):
        raise PolicyRateQualificationError("raw_content_mismatch")

    try:
        retrieved_at = _utc(datetime.fromisoformat(str(payload["retrieved_at"])), "retrieved_at")
        response_headers = _retained_headers(dict(payload["response_headers"]))
        recomputed_source_id = _compute_source_artifact_id(
            artifact_key=str(payload["artifact_key"]),
            currency=str(payload["currency"]),
            authority=str(payload["authority"]),
            source_kind=str(payload["source_kind"]),
            body_format=str(payload["body_format"]),
            event_date=event_date,
            requested_url=requested_url,
            returned_url=returned_url,
            response_media_type=str(payload["response_media_type"]),
            raw_sha256=str(payload["raw_sha256"]),
        )
        recomputed_acq_id = _compute_acquisition_id(
            source_artifact_id=recomputed_source_id,
            retrieved_at=retrieved_at,
            returned_url=returned_url,
            status_code=payload["status_code"],
            byte_count=payload["byte_count"],
            response_headers=response_headers,
            redirect_chain=redirect_chain,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyRateQualificationError("manifest_field_invalid") from exc

    if (
        recomputed_source_id != payload.get("source_artifact_id")
        or recomputed_acq_id != payload.get("acquisition_id")
        or len(raw_bytes) != payload.get("byte_count")
    ):
        raise PolicyRateQualificationError("manifest_identity_mismatch")

    response = PolicyEventSourceHttpResponse(
        status_code=payload["status_code"],
        final_url=returned_url,
        media_type=str(payload["response_media_type"]),
        headers=response_headers,
        raw_bytes=raw_bytes,
        redirect_chain=redirect_chain,
    )
    recomputed = PolicyEventSourceManifest.from_parts(spec, response, retrieved_at)
    if (
        recomputed.source_artifact_id != payload["source_artifact_id"]
        or recomputed.acquisition_id != payload["acquisition_id"]
    ):
        raise PolicyRateQualificationError("manifest_identity_mismatch")
    return VerifiedPolicyEventSourceArtifact(manifest=recomputed, raw_bytes=raw_bytes)
