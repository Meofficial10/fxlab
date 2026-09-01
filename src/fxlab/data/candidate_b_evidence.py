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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from fxlab.data.dukascopy_provider import DUKASCOPY_SYMBOLS
from fxlab.data.policy_rates import (
    APPROVED_PAIRS,
    MAX_OBSERVATION_DATE,
    AmbiguityState,
    EvidenceClassification,
    PolicyEventKind,
    PolicyEventManifest,
    PolicyRateEvent,
    PolicySourceEvidence,
    SpotObservationReference,
    SpotPanelManifestReference,
    TimePrecision,
    canonical_json,
    canonical_sha256,
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
POLICY_EVENT_PERSISTED_SCHEMA = "candidate_b_policy_events.v1"
SPOT_PANEL_PERSISTED_SCHEMA = "candidate_b_spot_panel.v1"


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
        event = _event_from_payload(entry.get("event"))
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
    references_payload = stored.get("observations")
    if not isinstance(references_payload, list):
        _fail("spot_observation_invalid")
    references = tuple(_reference_from_payload(item) for item in references_payload)
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
