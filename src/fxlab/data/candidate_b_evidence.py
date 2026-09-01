"""Offline persistence contracts for authoritative Candidate B evidence.

The APIs in this module never discover paths, acquire data, or perform research measurement.
They persist and independently reconstruct already-validated typed evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pandas as pd

from fxlab.data.dukascopy_provider import DUKASCOPY_SYMBOLS
from fxlab.data.policy_rates import (
    APPROVED_BIS_OBSERVATION_STATUS_SEMANTICS,
    APPROVED_BIS_SERIES,
    APPROVED_PAIRS,
    AUTHORITATIVE_D_AU_URL,
    AUTHORITATIVE_D_CA_URL,
    AUTHORITATIVE_D_CH_URL,
    AUTHORITATIVE_D_GB_URL,
    AUTHORITATIVE_D_JP_URL,
    AUTHORITATIVE_D_NZ_URL,
    AUTHORITATIVE_D_US_URL,
    AUTHORITATIVE_D_XM_URL,
    MAX_OBSERVATION_DATE,
    AmbiguityState,
    EvidenceClassification,
    PolicyEventKind,
    PolicyEventManifest,
    PolicyRateEvent,
    PolicyRateMetadata,
    PolicyRateQualificationError,
    PolicyRateRequest,
    PolicyRateSeriesManifest,
    PolicySourceEvidence,
    SpotObservationReference,
    SpotPanelManifestReference,
    TimePrecision,
    authoritative_d_au_request,
    authoritative_d_ca_request,
    authoritative_d_ch_request,
    authoritative_d_gb_request,
    authoritative_d_jp_request,
    authoritative_d_nz_request,
    authoritative_d_us_request,
    authoritative_d_xm_request,
    canonical_json,
    canonical_sha256,
    parse_authoritative_bis_d_us_sdmx,
    parse_authoritative_bis_sdmx,
)
from fxlab.data.provider import (
    BarDataset,
    BarQuery,
    CanonicalInstrument,
    DataProvenance,
    ProvenanceQuality,
    dataset_identity,
)

BIS_PERSISTED_SCHEMA = "candidate_b_bis_authoritative.v2"
BIS_ACQUISITION_AUDIT_CONTRACT = "candidate_b_bis_acquisition_audit.v1"
BIS_MIGRATION_AUDIT_CONTRACT = "candidate_b_bis_migration_audit.v1"
BIS_LEGACY_MIGRATION_CONTRACT = (
    "candidate_b_bis_authoritative_manifest_explicit_counts_v1"
)
POLICY_EVENT_PERSISTED_SCHEMA = "candidate_b_policy_events.v1"
SPOT_PANEL_PERSISTED_SCHEMA = "candidate_b_spot_panel.v1"

_BIS_REPRESENTATION = "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
_BIS_STATUS_SEMANTICS = ("A=normal", "M=missing_value_data_cannot_exist")
_BIS_CONTRACTS: Mapping[str, tuple[str, str, PolicyRateRequest]] = {
    "AUD": ("d_au", AUTHORITATIVE_D_AU_URL, authoritative_d_au_request()),
    "CAD": ("d_ca", AUTHORITATIVE_D_CA_URL, authoritative_d_ca_request()),
    "CHF": ("d_ch", AUTHORITATIVE_D_CH_URL, authoritative_d_ch_request()),
    "EUR": ("d_xm", AUTHORITATIVE_D_XM_URL, authoritative_d_xm_request()),
    "GBP": ("d_gb", AUTHORITATIVE_D_GB_URL, authoritative_d_gb_request()),
    "JPY": ("d_jp", AUTHORITATIVE_D_JP_URL, authoritative_d_jp_request()),
    "NZD": ("d_nz", AUTHORITATIVE_D_NZ_URL, authoritative_d_nz_request()),
    "USD": ("d_us", AUTHORITATIVE_D_US_URL, authoritative_d_us_request()),
}


def _fail(reason: str) -> None:
    raise ValueError(reason)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_mapping(value: object) -> dict[str, Any]:
    try:
        result = json.loads(canonical_json(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("evidence_manifest_invalid") from exc
    if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
        _fail("evidence_manifest_invalid")
    return result


def build_candidate_b_bis_persisted_manifest(
    *,
    manifest: Mapping[str, object],
    returned_url: str | None,
    response_headers: Mapping[str, str] | None,
) -> dict[str, object]:
    """Add every acquisition-audit identity input without changing either hash formula."""

    if not isinstance(manifest, Mapping):
        _fail("bis_manifest_invalid")
    stored = _json_mapping(dict(manifest))
    if "row_count" in stored:
        _fail("bis_legacy_manifest_requires_migration")
    if not isinstance(returned_url, str) or returned_url != stored.get("exact_url"):
        _fail("bis_audit_evidence_invalid")
    if not isinstance(response_headers, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in response_headers.items()
    ):
        _fail("bis_audit_evidence_invalid")
    headers = dict(sorted((key.lower(), value) for key, value in response_headers.items()))
    semantic_names = (
        "request_fingerprint",
        "exact_url",
        "representation_identity",
        "series_key",
        "frequency",
        "reference_area",
        "unit_measure",
        "unit_mult",
        "status_semantics",
        "raw_sha256",
        "canonical_observation_hash",
        "raw_row_count",
        "numeric_observation_count",
        "min_observation_date",
        "max_observation_date",
    )
    if any(name not in stored for name in semantic_names):
        _fail("bis_manifest_invalid")
    semantic = {"format": 1, **{name: stored[name] for name in semantic_names}}
    dataset_id = canonical_sha256(semantic)
    if stored.get("dataset_id") != dataset_id:
        _fail("bis_dataset_identity_mismatch")
    audit_names = ("retrieved_at", "response_media_type", "byte_count")
    if any(name not in stored for name in audit_names):
        _fail("bis_manifest_invalid")
    audit = {
        "format": 1,
        "dataset_id": dataset_id,
        "retrieved_at": stored["retrieved_at"],
        "returned_url": returned_url,
        "response_media_type": stored["response_media_type"],
        "byte_count": stored["byte_count"],
        "headers": headers,
    }
    manifest_id = canonical_sha256(audit)
    if stored.get("manifest_id") != manifest_id:
        _fail("bis_manifest_identity_mismatch")
    return {
        "schema": BIS_PERSISTED_SCHEMA,
        "audit_contract": BIS_ACQUISITION_AUDIT_CONTRACT,
        **stored,
        "returned_url": returned_url,
        "response_headers": headers,
    }


def build_candidate_b_bis_migration_persisted_manifest(
    *,
    manifest: Mapping[str, object],
    migration_contract: str,
    legacy_dataset_id: str,
    legacy_manifest_id: str,
) -> dict[str, object]:
    """Persist a migration-derived audit identity without claiming acquisition evidence."""

    if not isinstance(manifest, Mapping):
        _fail("bis_manifest_invalid")
    stored = _json_mapping(dict(manifest))
    semantic_names = (
        "request_fingerprint",
        "exact_url",
        "representation_identity",
        "series_key",
        "frequency",
        "reference_area",
        "unit_measure",
        "unit_mult",
        "status_semantics",
        "raw_sha256",
        "canonical_observation_hash",
        "raw_row_count",
        "numeric_observation_count",
        "min_observation_date",
        "max_observation_date",
    )
    if any(name not in stored for name in semantic_names):
        _fail("bis_manifest_invalid")
    dataset_id = canonical_sha256(
        {"format": 1, **{name: stored[name] for name in semantic_names}}
    )
    if stored.get("dataset_id") != dataset_id:
        _fail("bis_dataset_identity_mismatch")
    if not all(
        isinstance(value, str) and len(value) == 64
        for value in (legacy_dataset_id, legacy_manifest_id)
    ) or not isinstance(migration_contract, str):
        _fail("bis_migration_evidence_invalid")
    audit = {
        "format": 1,
        "migration_contract": migration_contract,
        "dataset_id": dataset_id,
        "retrieved_at": stored.get("retrieved_at"),
        "byte_count": stored.get("byte_count"),
        "response_media_type": stored.get("response_media_type"),
        "legacy_manifest_id": legacy_manifest_id,
        "legacy_dataset_id": legacy_dataset_id,
    }
    if stored.get("manifest_id") != canonical_sha256(audit):
        _fail("bis_manifest_identity_mismatch")
    return {
        "schema": BIS_PERSISTED_SCHEMA,
        "audit_contract": BIS_MIGRATION_AUDIT_CONTRACT,
        "migration_contract": migration_contract,
        "legacy_dataset_id": legacy_dataset_id,
        "legacy_manifest_id": legacy_manifest_id,
        **stored,
    }


def _sha256_text(value: object, reason: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(reason)
    return value


def _bis_date(value: object, reason: str) -> date:
    if not isinstance(value, str):
        _fail(reason)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(reason) from exc
    if parsed > MAX_OBSERVATION_DATE:
        _fail("sealed_window_violation")
    return parsed


def _bis_datetime(value: object, reason: str) -> datetime:
    if not isinstance(value, str):
        _fail(reason)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(reason)
    return parsed.astimezone(UTC)


def _bis_contract(series_key: object) -> tuple[str, str, PolicyRateRequest]:
    matches = tuple(
        contract
        for currency, contract in _BIS_CONTRACTS.items()
        if APPROVED_BIS_SERIES[currency] == series_key
    )
    if len(matches) != 1:
        _fail("bis_series_not_approved")
    return matches[0]


@dataclass(frozen=True)
class _VerifiedBisValues:
    publication_directory: Path
    persisted_manifest: Mapping[str, object]
    request: PolicyRateRequest
    metadata: PolicyRateMetadata
    retrieved_at: datetime
    raw_bytes: bytes
    observations: tuple[object, ...]
    raw_row_count: int
    numeric_observation_count: int
    audit_contract: str


def _verified_bis_values(publication_directory: Path) -> _VerifiedBisValues:
    if not isinstance(publication_directory, Path):
        _fail("bis_publication_path_invalid")
    manifest_path = publication_directory / "manifest.json"
    raw_path = publication_directory / "response.xml"
    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("bis_publication_invalid") from exc
    if not isinstance(stored, dict) or stored.get("schema") != BIS_PERSISTED_SCHEMA:
        _fail("bis_publication_invalid")
    audit_contract = stored.get("audit_contract")
    if audit_contract not in {
        BIS_ACQUISITION_AUDIT_CONTRACT,
        BIS_MIGRATION_AUDIT_CONTRACT,
    }:
        _fail("bis_audit_contract_invalid")
    slug, exact_url, request = _bis_contract(stored.get("series_key"))
    if publication_directory.name != f"{slug}-{request.fingerprint}":
        _fail("bis_publication_path_invalid")

    minimum = _bis_date(stored.get("min_observation_date"), "bis_observation_bounds_invalid")
    maximum = _bis_date(stored.get("max_observation_date"), "bis_observation_bounds_invalid")
    if minimum < request.start or maximum > request.end or minimum > maximum:
        _fail("bis_observation_bounds_invalid")
    retrieved_at = _bis_datetime(stored.get("retrieved_at"), "bis_retrieval_evidence_invalid")
    if (
        stored.get("request_fingerprint") != request.fingerprint
        or stored.get("exact_url") != exact_url
        or stored.get("representation_identity") != _BIS_REPRESENTATION
        or stored.get("frequency") != "D"
        or stored.get("reference_area") != request.series.series_key.split(".", 1)[1]
        or stored.get("unit_measure") != "368"
        or stored.get("unit_mult") != "0"
        or tuple(stored.get("status_semantics", ())) != _BIS_STATUS_SEMANTICS
        or stored.get("response_media_type") != "application/xml"
    ):
        _fail("bis_manifest_contract_mismatch")
    byte_count = stored.get("byte_count")
    raw_row_count = stored.get("raw_row_count")
    numeric_count = stored.get("numeric_observation_count")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (byte_count, raw_row_count, numeric_count)
    ) or numeric_count > raw_row_count:
        _fail("bis_manifest_invalid")
    raw_sha256 = _sha256_text(stored.get("raw_sha256"), "bis_manifest_invalid")
    observation_hash = _sha256_text(
        stored.get("canonical_observation_hash"), "bis_manifest_invalid"
    )
    _sha256_text(stored.get("dataset_id"), "bis_manifest_invalid")
    _sha256_text(stored.get("manifest_id"), "bis_manifest_invalid")

    if audit_contract == BIS_ACQUISITION_AUDIT_CONTRACT:
        returned_url = stored.get("returned_url")
        headers = stored.get("response_headers")
        if returned_url != exact_url or not isinstance(headers, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in headers.items()
        ):
            _fail("bis_audit_evidence_invalid")
        normalized_headers = dict(sorted((key.lower(), value) for key, value in headers.items()))
        if headers != normalized_headers:
            _fail("bis_audit_evidence_invalid")
        audit = {
            "format": 1,
            "dataset_id": stored["dataset_id"],
            "retrieved_at": retrieved_at,
            "returned_url": returned_url,
            "response_media_type": stored["response_media_type"],
            "byte_count": byte_count,
            "headers": normalized_headers,
        }
    else:
        if "returned_url" in stored or "response_headers" in stored:
            _fail("bis_migration_evidence_invalid")
        if stored.get("migration_contract") != BIS_LEGACY_MIGRATION_CONTRACT:
            _fail("bis_migration_evidence_invalid")
        legacy_dataset_id = _sha256_text(
            stored.get("legacy_dataset_id"), "bis_migration_evidence_invalid"
        )
        legacy_manifest_id = _sha256_text(
            stored.get("legacy_manifest_id"), "bis_migration_evidence_invalid"
        )
        audit = {
            "format": 1,
            "migration_contract": BIS_LEGACY_MIGRATION_CONTRACT,
            "dataset_id": stored["dataset_id"],
            "retrieved_at": retrieved_at,
            "byte_count": byte_count,
            "response_media_type": stored["response_media_type"],
            "legacy_manifest_id": legacy_manifest_id,
            "legacy_dataset_id": legacy_dataset_id,
        }
    try:
        raw_bytes = raw_path.read_bytes()
    except OSError as exc:
        raise ValueError("bis_raw_evidence_missing") from exc
    if len(raw_bytes) != byte_count:
        _fail("bis_raw_byte_count_mismatch")
    if _sha256_bytes(raw_bytes) != raw_sha256:
        _fail("bis_raw_hash_mismatch")
    try:
        if request.series.currency == "USD":
            observations = parse_authoritative_bis_d_us_sdmx(raw_bytes, request)
            parsed_raw_count = len(observations)
        else:
            parsed = parse_authoritative_bis_sdmx(raw_bytes, request)
            observations = parsed.observations
            parsed_raw_count = parsed.raw_row_count
    except PolicyRateQualificationError as exc:
        raise ValueError(str(exc)) from exc
    if (
        parsed_raw_count != raw_row_count
        or len(observations) != numeric_count
        or canonical_sha256(observations) != observation_hash
        or observations[0].observation_date != minimum
        or observations[-1].observation_date != maximum
    ):
        _fail("bis_observation_evidence_mismatch")
    semantic = {
        "format": 1,
        "request_fingerprint": request.fingerprint,
        "exact_url": exact_url,
        "representation_identity": _BIS_REPRESENTATION,
        "series_key": request.series.series_key,
        "frequency": "D",
        "reference_area": request.series.series_key.split(".", 1)[1],
        "unit_measure": "368",
        "unit_mult": "0",
        "status_semantics": _BIS_STATUS_SEMANTICS,
        "raw_sha256": raw_sha256,
        "canonical_observation_hash": observation_hash,
        "raw_row_count": raw_row_count,
        "numeric_observation_count": numeric_count,
        "min_observation_date": minimum,
        "max_observation_date": maximum,
    }
    if canonical_sha256(semantic) != stored["dataset_id"]:
        _fail("bis_dataset_identity_mismatch")
    if canonical_sha256(audit) != stored["manifest_id"]:
        _fail("bis_manifest_identity_mismatch")
    metadata = PolicyRateMetadata(
        agency="BIS",
        dataflow="WS_CBPOL",
        version="1.0",
        frequency="D",
        series_key=request.series.series_key,
        currency=request.series.currency,
        reference_area=request.series.series_key.split(".", 1)[1],
        unit="percent_per_annum",
        scale=0,
        observation_status_semantics=APPROVED_BIS_OBSERVATION_STATUS_SEMANTICS,
        dsd_identity="bis_cbpol_1_0",
        codelist_identity="cl_obs_status",
        instrument_metadata="principal_policy_rate",
        source_identity="bis_ws_cbpol",
        endpoint_identity="bis_api_v2",
        media_type="application/xml",
        revision="1.0",
    )
    return _VerifiedBisValues(
        publication_directory,
        MappingProxyType(stored),
        request,
        metadata,
        retrieved_at,
        raw_bytes,
        tuple(observations),
        raw_row_count,
        numeric_count,
        str(audit_contract),
    )


class AuthoritativeSdmxPolicyRateSeriesManifest(PolicyRateSeriesManifest):
    """PolicyRateSeriesManifest built only by verifying persisted authoritative SDMX."""

    def __init__(self, publication_directory: Path) -> None:
        values = _verified_bis_values(publication_directory)
        stored = values.persisted_manifest
        for name, value in (
            ("request", values.request),
            ("metadata", values.metadata),
            ("retrieved_at", values.retrieved_at),
            ("raw_sha256", stored["raw_sha256"]),
            ("byte_count", stored["byte_count"]),
            ("observations", values.observations),
            ("canonical_observation_hash", stored["canonical_observation_hash"]),
            ("dataset_id", stored["dataset_id"]),
            ("manifest_id", stored["manifest_id"]),
            ("parsed_min_observation_date", values.observations[0].observation_date),
            ("parsed_max_observation_date", values.observations[-1].observation_date),
            ("raw_row_count", values.raw_row_count),
            ("numeric_observation_count", values.numeric_observation_count),
            ("publication_directory", values.publication_directory),
            ("audit_contract", values.audit_contract),
        ):
            object.__setattr__(self, name, value)

    def revalidate(self) -> None:
        values = _verified_bis_values(self.publication_directory)
        stored = values.persisted_manifest
        if (
            values.request != self.request
            or values.metadata != self.metadata
            or values.observations != self.observations
            or values.raw_row_count != self.raw_row_count
            or values.numeric_observation_count != self.numeric_observation_count
            or values.audit_contract != self.audit_contract
            or stored["dataset_id"] != self.dataset_id
            or stored["manifest_id"] != self.manifest_id
        ):
            _fail("bis_typed_evidence_mismatch")


@dataclass(frozen=True)
class AuthoritativeBisEvidencePublication:
    publication_directory: Path
    series_manifest: AuthoritativeSdmxPolicyRateSeriesManifest
    persisted_manifest: Mapping[str, object]
    audit_contract: str


def verify_candidate_b_bis_evidence(
    publication_directory: Path,
) -> AuthoritativeBisEvidencePublication:
    series_manifest = AuthoritativeSdmxPolicyRateSeriesManifest(publication_directory)
    values = _verified_bis_values(publication_directory)
    return AuthoritativeBisEvidencePublication(
        publication_directory,
        series_manifest,
        values.persisted_manifest,
        values.audit_contract,
    )


def _write_fully(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_publication(destination: Path, writer) -> None:
    if not isinstance(destination, Path):
        _fail("publication_path_invalid")
    if destination.exists():
        _fail("destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        writer(temporary)
        if destination.exists():
            _fail("destination_exists")
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _event_payload(event: PolicyRateEvent) -> dict[str, object]:
    return _json_mapping(event)


def _event_from_payload(payload: object) -> PolicyRateEvent:
    if not isinstance(payload, dict):
        _fail("event_evidence_invalid")
    try:
        source_payload = payload["source"]
        if not isinstance(source_payload, dict):
            raise TypeError
        source = PolicySourceEvidence(
            source_url=source_payload["source_url"],
            retrieved_at=datetime.fromisoformat(source_payload["retrieved_at"]),
            content_hash=source_payload["content_hash"],
            byte_count=source_payload["byte_count"],
            media_type=source_payload["media_type"],
            source_kind=source_payload["source_kind"],
        )
        return PolicyRateEvent(
            event_id=payload["event_id"],
            kind=PolicyEventKind(payload["kind"]),
            currency=payload["currency"],
            central_bank_id=payload["central_bank_id"],
            policy_instrument_id=payload["policy_instrument_id"],
            announcement_lower=datetime.fromisoformat(payload["announcement_lower"]),
            announcement_upper=datetime.fromisoformat(payload["announcement_upper"]),
            announcement_precision=TimePrecision(payload["announcement_precision"]),
            effective_lower=datetime.fromisoformat(payload["effective_lower"]),
            effective_upper=datetime.fromisoformat(payload["effective_upper"]),
            effective_precision=TimePrecision(payload["effective_precision"]),
            source_timezone=payload["source_timezone"],
            old_rate=payload["old_rate"],
            new_rate=payload["new_rate"],
            source=source,
            evidence_classification=EvidenceClassification(payload["evidence_classification"]),
            ambiguity=AmbiguityState(payload["ambiguity"]),
            conflict=AmbiguityState(payload["conflict"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("event_evidence_invalid") from exc


def _validate_event_dates_before_values(payload: object) -> None:
    if not isinstance(payload, dict):
        _fail("event_evidence_invalid")
    for name in (
        "announcement_lower",
        "announcement_upper",
        "effective_lower",
        "effective_upper",
    ):
        observed = _datetime(payload.get(name), "event_evidence_invalid")
        if observed.date() > MAX_OBSERVATION_DATE:
            _fail("sealed_window_violation")


@dataclass(frozen=True)
class PolicyEventEvidencePublication:
    destination: Path
    manifest_path: Path
    event_manifest: PolicyEventManifest
    manifest: Mapping[str, object]
    publication_id: str


def _policy_event_publication_payload(
    event_manifest: PolicyEventManifest, source_artifacts: Mapping[str, bytes]
) -> dict[str, object]:
    if not isinstance(event_manifest, PolicyEventManifest):
        _fail("event_manifest_invalid")
    entries = []
    publication_entries = []
    for event in event_manifest.events:
        source_hash = event.source.content_hash
        source = source_artifacts.get(source_hash)
        if not isinstance(source, bytes) or _sha256_bytes(source) != source_hash:
            _fail("event_source_hash_mismatch")
        if len(source) != event.source.byte_count:
            _fail("event_source_byte_count_mismatch")
        artifact = f"sources/{source_hash}.bin"
        entries.append(
            {
                "event": _event_payload(event),
                "event_identity": event.identity,
                "source_artifact": artifact,
            }
        )
        publication_entries.append(
            {
                "event_identity": event.identity,
                "source_artifact": artifact,
                "source_sha256": source_hash,
                "source_byte_count": len(source),
            }
        )
    publication_id = canonical_sha256(
        {
            "schema": POLICY_EVENT_PERSISTED_SCHEMA,
            "event_manifest_id": event_manifest.manifest_id,
            "events": tuple(publication_entries),
        }
    )
    return {
        "schema": POLICY_EVENT_PERSISTED_SCHEMA,
        "event_manifest_id": event_manifest.manifest_id,
        "events": entries,
        "publication_id": publication_id,
    }


def persist_candidate_b_policy_event_evidence(
    destination: Path,
    event_manifest: PolicyEventManifest,
    source_artifacts: Mapping[str, bytes],
) -> PolicyEventEvidencePublication:
    manifest = _policy_event_publication_payload(event_manifest, source_artifacts)

    def write(temporary: Path) -> None:
        sources = temporary / "sources"
        sources.mkdir()
        for content_hash, payload in source_artifacts.items():
            if not isinstance(content_hash, str) or not isinstance(payload, bytes):
                _fail("event_source_evidence_invalid")
            if content_hash in {item.source.content_hash for item in event_manifest.events}:
                _write_fully(sources / f"{content_hash}.bin", payload)
        _write_fully(temporary / "manifest.json", canonical_json(manifest).encode("utf-8"))

    _atomic_publication(destination, write)
    return PolicyEventEvidencePublication(
        destination,
        destination / "manifest.json",
        event_manifest,
        manifest,
        str(manifest["publication_id"]),
    )


def verify_candidate_b_policy_event_evidence(destination: Path) -> PolicyEventEvidencePublication:
    manifest_path = destination / "manifest.json"
    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("event_publication_invalid") from exc
    if not isinstance(stored, dict) or stored.get("schema") != POLICY_EVENT_PERSISTED_SCHEMA:
        _fail("event_publication_invalid")
    entries = stored.get("events")
    if not isinstance(entries, list) or not entries:
        _fail("event_publication_invalid")
    events = []
    sources: dict[str, bytes] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("event_publication_invalid")
        event_payload = entry.get("event")
        _validate_event_dates_before_values(event_payload)
        event = _event_from_payload(event_payload)
        if entry.get("event_identity") != event.identity:
            _fail("event_identity_mismatch")
        expected_artifact = f"sources/{event.source.content_hash}.bin"
        if entry.get("source_artifact") != expected_artifact:
            _fail("event_source_artifact_invalid")
        try:
            raw = (destination / expected_artifact).read_bytes()
        except OSError as exc:
            raise ValueError("event_source_evidence_missing") from exc
        if _sha256_bytes(raw) != event.source.content_hash:
            _fail("event_source_hash_mismatch")
        if len(raw) != event.source.byte_count:
            _fail("event_source_byte_count_mismatch")
        sources[event.source.content_hash] = raw
        events.append(event)
    event_manifest = PolicyEventManifest(tuple(events))
    if stored.get("event_manifest_id") != event_manifest.manifest_id:
        _fail("event_manifest_identity_mismatch")
    expected = _policy_event_publication_payload(event_manifest, sources)
    if stored.get("publication_id") != expected["publication_id"]:
        _fail("event_publication_identity_mismatch")
    return PolicyEventEvidencePublication(
        destination,
        manifest_path,
        event_manifest,
        stored,
        str(stored["publication_id"]),
    )


def _query_payload(query: BarQuery) -> dict[str, object]:
    return {
        "symbol": query.instrument.symbol,
        "timeframe": query.timeframe,
        "start": query.start,
        "end": query.end,
        "as_of": query.as_of,
        "fingerprint": query.fingerprint,
    }


def _provenance_payload(provenance: DataProvenance) -> dict[str, object]:
    return _json_mapping(provenance)


def _reference_payload(reference: SpotObservationReference) -> dict[str, object]:
    return _json_mapping(reference)


def _spot_panel_manifest_id(
    dataset_ids: tuple[str, ...], references: tuple[SpotObservationReference, ...]
) -> str:
    return canonical_sha256(
        {"format": 1, "dataset_ids": dataset_ids, "observations": references}
    )


def _validate_spot_metadata(datasets: tuple[BarDataset, ...]) -> None:
    if len(datasets) != len(APPROVED_PAIRS):
        _fail("spot_pair_membership_invalid")
    by_pair = {item.query.instrument.symbol: item for item in datasets}
    if len(by_pair) != len(APPROVED_PAIRS) or set(by_pair) != set(APPROVED_PAIRS):
        _fail("spot_pair_membership_invalid")
    for pair in APPROVED_PAIRS:
        item = by_pair[pair]
        query = item.query
        provenance = item.provenance
        bounds = (
            query.start,
            query.end,
            query.as_of,
            provenance.query_start,
            provenance.query_end,
            provenance.query_as_of,
            provenance.actual_first_observation,
            provenance.actual_last_observation,
        )
        if any(value is None or value.date() > MAX_OBSERVATION_DATE for value in bounds):
            _fail("sealed_window_violation")
        if (
            provenance.provenance_quality is not ProvenanceQuality.VERIFIED
            or provenance.provider_id != "dukascopy"
            or provenance.provider_version != "1"
            or provenance.normalization_version != "dukascopy_bid_v1"
            or provenance.provider_symbol != DUKASCOPY_SYMBOLS[pair]
            or provenance.canonical_symbol != pair
            or query.timeframe != "D1"
            or provenance.timeframe != "D1"
            or provenance.source_timezone != "UTC"
            or provenance.sanitized_source_reference != "dukascopy:historical:bid"
        ):
            _fail("spot_provenance_not_verified")


@dataclass(frozen=True)
class SpotPanelEvidencePublication:
    destination: Path
    manifest_path: Path
    spot_panel: SpotPanelManifestReference
    pair_datasets: tuple[BarDataset, ...]
    manifest: Mapping[str, object]
    publication_id: str


def persist_candidate_b_spot_panel_evidence(
    destination: Path,
    pair_datasets: Sequence[BarDataset],
    observations: Sequence[SpotObservationReference],
) -> SpotPanelEvidencePublication:
    datasets = tuple(pair_datasets)
    if any(not isinstance(item, BarDataset) for item in datasets):
        _fail("spot_dataset_invalid")
    _validate_spot_metadata(datasets)
    by_pair = {item.query.instrument.symbol: item for item in datasets}
    ordered = tuple(by_pair[pair] for pair in APPROVED_PAIRS)
    references = tuple(sorted(observations, key=lambda item: (item.bar_close, item.pair)))
    if any(not isinstance(item, SpotObservationReference) for item in references):
        _fail("spot_observation_invalid")
    dataset_ids = tuple(item.provenance.dataset_id for item in ordered)
    expected_ids = dict(zip(APPROVED_PAIRS, dataset_ids, strict=True))
    if not references or any(item.dataset_id != expected_ids.get(item.pair) for item in references):
        _fail("spot_observation_binding_invalid")
    panel_id = _spot_panel_manifest_id(dataset_ids, references)
    spot_panel = SpotPanelManifestReference(panel_id, dataset_ids, references)
    manifest_holder: dict[str, object] = {}

    def write(temporary: Path) -> None:
        artifact_dir = temporary / "datasets"
        artifact_dir.mkdir()
        entries = []
        publication_entries = []
        for pair, dataset in zip(APPROVED_PAIRS, ordered, strict=True):
            validated = BarDataset(dataset.query, dataset.frame, dataset.provenance)
            artifact = f"datasets/{pair}.parquet"
            artifact_path = temporary / artifact
            validated.frame.to_parquet(artifact_path, engine="pyarrow")
            with artifact_path.open("ab") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            raw = artifact_path.read_bytes()
            artifact_hash = _sha256_bytes(raw)
            entry = {
                "pair": pair,
                "query": _query_payload(validated.query),
                "provenance": _provenance_payload(validated.provenance),
                "dataset_id": validated.provenance.dataset_id,
                "artifact": artifact,
                "artifact_sha256": artifact_hash,
                "artifact_byte_count": len(raw),
            }
            entries.append(entry)
            publication_entries.append(entry)
        publication_id = canonical_sha256(
            {
                "schema": SPOT_PANEL_PERSISTED_SCHEMA,
                "spot_panel_manifest_id": panel_id,
                "artifacts": tuple(publication_entries),
            }
        )
        manifest = {
            "schema": SPOT_PANEL_PERSISTED_SCHEMA,
            "spot_panel_manifest_id": panel_id,
            "dataset_ids": dataset_ids,
            "observations": tuple(_reference_payload(item) for item in references),
            "datasets": entries,
            "publication_id": publication_id,
        }
        manifest_holder.update(manifest)
        _write_fully(temporary / "manifest.json", canonical_json(manifest).encode("utf-8"))

    _atomic_publication(destination, write)
    return SpotPanelEvidencePublication(
        destination,
        destination / "manifest.json",
        spot_panel,
        ordered,
        manifest_holder,
        str(manifest_holder["publication_id"]),
    )


def _datetime(value: object, reason: str) -> datetime:
    if not isinstance(value, str):
        _fail(reason)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(reason)
    return parsed.astimezone(UTC)


def _query_from_payload(payload: object) -> BarQuery:
    if not isinstance(payload, dict):
        _fail("spot_query_invalid")
    try:
        query = BarQuery(
            CanonicalInstrument(payload["symbol"]),
            payload["timeframe"],
            _datetime(payload["start"], "spot_query_invalid"),
            _datetime(payload["end"], "spot_query_invalid"),
            _datetime(payload["as_of"], "spot_query_invalid"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("spot_query_invalid") from exc
    if payload.get("fingerprint") != query.fingerprint:
        _fail("spot_query_identity_mismatch")
    return query


def _provenance_from_payload(payload: object) -> DataProvenance:
    if not isinstance(payload, dict):
        _fail("spot_provenance_invalid")
    try:
        return DataProvenance(
            provider_id=payload["provider_id"],
            provider_version=payload["provider_version"],
            normalization_version=payload["normalization_version"],
            canonical_symbol=payload["canonical_symbol"],
            provider_symbol=payload["provider_symbol"],
            timeframe=payload["timeframe"],
            query_start=_datetime(payload["query_start"], "spot_provenance_invalid"),
            query_end=_datetime(payload["query_end"], "spot_provenance_invalid"),
            query_as_of=_datetime(payload["query_as_of"], "spot_provenance_invalid"),
            retrieved_at=_datetime(payload["retrieved_at"], "spot_provenance_invalid"),
            actual_first_observation=_datetime(
                payload["actual_first_observation"], "spot_provenance_invalid"
            ),
            actual_last_observation=_datetime(
                payload["actual_last_observation"], "spot_provenance_invalid"
            ),
            row_count=payload["row_count"],
            content_hash=payload["content_hash"],
            query_fingerprint=payload["query_fingerprint"],
            dataset_id=payload["dataset_id"],
            revision=payload["revision"],
            source_timezone=payload["source_timezone"],
            volume_semantics=payload["volume_semantics"],
            provenance_quality=ProvenanceQuality(payload["provenance_quality"]),
            sanitized_source_reference=payload["sanitized_source_reference"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("spot_provenance_invalid") from exc


def _reference_from_payload(payload: object) -> SpotObservationReference:
    if not isinstance(payload, dict):
        _fail("spot_observation_invalid")
    try:
        return SpotObservationReference(
            pair=payload["pair"],
            dataset_id=payload["dataset_id"],
            bar_open=_datetime(payload["bar_open"], "spot_observation_invalid"),
            bar_close=_datetime(payload["bar_close"], "spot_observation_invalid"),
            value_field=payload["value_field"],
            closed=payload["closed"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("spot_observation_invalid") from exc


def _validate_reference_dates_before_frame(payload: object) -> None:
    if not isinstance(payload, dict):
        _fail("spot_observation_invalid")
    opened = _datetime(payload.get("bar_open"), "spot_observation_invalid")
    closed = _datetime(payload.get("bar_close"), "spot_observation_invalid")
    if opened.date() > MAX_OBSERVATION_DATE or closed.date() > MAX_OBSERVATION_DATE:
        _fail("sealed_window_violation")


def verify_candidate_b_spot_panel_evidence(destination: Path) -> SpotPanelEvidencePublication:
    manifest_path = destination / "manifest.json"
    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("spot_publication_invalid") from exc
    if not isinstance(stored, dict) or stored.get("schema") != SPOT_PANEL_PERSISTED_SCHEMA:
        _fail("spot_publication_invalid")
    entries = stored.get("datasets")
    if not isinstance(entries, list) or len(entries) != len(APPROVED_PAIRS):
        _fail("spot_pair_membership_invalid")
    metadata = []
    for pair, entry in zip(APPROVED_PAIRS, entries, strict=True):
        if not isinstance(entry, dict) or entry.get("pair") != pair:
            _fail("spot_pair_membership_invalid")
        query = _query_from_payload(entry.get("query"))
        provenance = _provenance_from_payload(entry.get("provenance"))
        if entry.get("dataset_id") != provenance.dataset_id:
            _fail("spot_dataset_identity_mismatch")
        shell = object.__new__(BarDataset)
        object.__setattr__(shell, "query", query)
        object.__setattr__(shell, "provenance", provenance)
        object.__setattr__(shell, "_frame", pd.DataFrame())
        metadata.append(shell)
    _validate_spot_metadata(tuple(metadata))

    references_payload = stored.get("observations")
    if not isinstance(references_payload, list):
        _fail("spot_observation_invalid")
    for payload in references_payload:
        _validate_reference_dates_before_frame(payload)
    references = tuple(_reference_from_payload(item) for item in references_payload)

    datasets = []
    publication_entries = []
    for pair, entry, shell in zip(APPROVED_PAIRS, entries, metadata, strict=True):
        expected_artifact = f"datasets/{pair}.parquet"
        if entry.get("artifact") != expected_artifact:
            _fail("spot_artifact_invalid")
        artifact_path = destination / expected_artifact
        try:
            raw = artifact_path.read_bytes()
        except OSError as exc:
            raise ValueError("spot_artifact_missing") from exc
        if _sha256_bytes(raw) != entry.get("artifact_sha256"):
            _fail("spot_artifact_hash_mismatch")
        if len(raw) != entry.get("artifact_byte_count"):
            _fail("spot_artifact_byte_count_mismatch")
        frame = pd.read_parquet(artifact_path, engine="pyarrow")
        frame.attrs = {"symbol": pair, "timeframe": "D1"}
        dataset = BarDataset(shell.query, frame, shell.provenance)
        if dataset.provenance.dataset_id != dataset_identity(
            dataset.provenance.provider_id,
            dataset.provenance.provider_version,
            dataset.query.fingerprint,
            dataset.provenance.content_hash,
        ):
            _fail("spot_dataset_identity_mismatch")
        datasets.append(dataset)
        publication_entries.append(
            {
                "pair": pair,
                "query": _query_payload(dataset.query),
                "provenance": _provenance_payload(dataset.provenance),
                "dataset_id": dataset.provenance.dataset_id,
                "artifact": expected_artifact,
                "artifact_sha256": entry["artifact_sha256"],
                "artifact_byte_count": entry["artifact_byte_count"],
            }
        )
    dataset_ids = tuple(item.provenance.dataset_id for item in datasets)
    if stored.get("dataset_ids") != list(dataset_ids):
        _fail("spot_dataset_identity_mismatch")
    panel_id = _spot_panel_manifest_id(dataset_ids, references)
    if stored.get("spot_panel_manifest_id") != panel_id:
        _fail("spot_panel_identity_mismatch")
    panel = SpotPanelManifestReference(panel_id, dataset_ids, references)
    publication_id = canonical_sha256(
        {
            "schema": SPOT_PANEL_PERSISTED_SCHEMA,
            "spot_panel_manifest_id": panel_id,
            "artifacts": tuple(publication_entries),
        }
    )
    if stored.get("publication_id") != publication_id:
        _fail("spot_publication_identity_mismatch")
    return SpotPanelEvidencePublication(
        destination,
        manifest_path,
        panel,
        tuple(datasets),
        stored,
        publication_id,
    )


@dataclass(frozen=True)
class CandidateBVerifiedEvidenceBundle:
    series_manifests: tuple[PolicyRateSeriesManifest, ...]
    event_manifest: PolicyEventManifest
    spot_panel: SpotPanelManifestReference
    bis_publications: tuple[AuthoritativeBisEvidencePublication, ...]
    event_publication: PolicyEventEvidencePublication
    spot_publication: SpotPanelEvidencePublication


def load_candidate_b_verified_evidence(
    *,
    bis_publication_paths: Mapping[str, Path],
    policy_event_publication_path: Path,
    spot_panel_publication_path: Path,
) -> CandidateBVerifiedEvidenceBundle:
    """Load exact explicitly named publications; never discover, repair, or acquire evidence."""

    if not isinstance(bis_publication_paths, Mapping) or set(bis_publication_paths) != set(
        APPROVED_BIS_SERIES
    ):
        _fail("bis_path_membership_invalid")
    paths = tuple(bis_publication_paths[currency] for currency in APPROVED_BIS_SERIES)
    if any(not isinstance(path, Path) for path in paths) or len(set(paths)) != len(paths):
        _fail("bis_path_membership_invalid")
    publications = tuple(verify_candidate_b_bis_evidence(path) for path in paths)
    if any(
        publication.series_manifest.request.series.currency != currency
        for currency, publication in zip(APPROVED_BIS_SERIES, publications, strict=True)
    ):
        _fail("bis_path_membership_invalid")
    event_publication = verify_candidate_b_policy_event_evidence(
        policy_event_publication_path
    )
    spot_publication = verify_candidate_b_spot_panel_evidence(
        spot_panel_publication_path
    )
    return CandidateBVerifiedEvidenceBundle(
        tuple(publication.series_manifest for publication in publications),
        event_publication.event_manifest,
        spot_publication.spot_panel,
        publications,
        event_publication,
        spot_publication,
    )
