"""Offline persistence contracts for authoritative Candidate B evidence."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from fxlab.data.dukascopy_provider import DUKASCOPY_SYMBOLS
from fxlab.data.policy_rates import (
    APPROVED_PAIRS,
    AmbiguityState,
    EvidenceClassification,
    PolicyEventKind,
    PolicyEventManifest,
    PolicyRateEvent,
    PolicySourceEvidence,
    SpotObservationReference,
    TimePrecision,
    authoritative_d_au_request,
    canonical_sha256,
)
from fxlab.data.provider import (
    BarDataset,
    BarQuery,
    CanonicalInstrument,
    DataProvenance,
    ProvenanceQuality,
    bar_content_hash,
    dataset_identity,
)


def _persistence():
    from fxlab.data.candidate_b_evidence import (
        BIS_ACQUISITION_AUDIT_CONTRACT,
        BIS_PERSISTED_SCHEMA,
        POLICY_EVENT_PERSISTED_SCHEMA,
        SPOT_PANEL_PERSISTED_SCHEMA,
        build_candidate_b_bis_persisted_manifest,
        persist_candidate_b_policy_event_evidence,
        persist_candidate_b_spot_panel_evidence,
        verify_candidate_b_policy_event_evidence,
        verify_candidate_b_spot_panel_evidence,
    )

    return (
        BIS_ACQUISITION_AUDIT_CONTRACT,
        BIS_PERSISTED_SCHEMA,
        POLICY_EVENT_PERSISTED_SCHEMA,
        SPOT_PANEL_PERSISTED_SCHEMA,
        build_candidate_b_bis_persisted_manifest,
        persist_candidate_b_policy_event_evidence,
        persist_candidate_b_spot_panel_evidence,
        verify_candidate_b_policy_event_evidence,
        verify_candidate_b_spot_panel_evidence,
    )


def _bis_manifest() -> tuple[dict[str, object], str, dict[str, str]]:
    raw = b"synthetic-authoritative-sdmx"
    request = authoritative_d_au_request()
    semantic = {
        "format": 1,
        "request_fingerprint": request.fingerprint,
        "exact_url": (
            "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.AU"
            "?startPeriod=2014-01-01&endPeriod=2023-12-31"
        ),
        "representation_identity": "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA",
        "series_key": "D.AU",
        "frequency": "D",
        "reference_area": "AU",
        "unit_measure": "368",
        "unit_mult": "0",
        "status_semantics": ("A=normal", "M=missing_value_data_cannot_exist"),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "canonical_observation_hash": "a" * 64,
        "raw_row_count": 2,
        "numeric_observation_count": 2,
        "min_observation_date": date(2014, 1, 2),
        "max_observation_date": date(2023, 12, 29),
    }
    dataset_id = canonical_sha256(semantic)
    returned_url = semantic["exact_url"]
    headers = {"content-type": "application/xml", "etag": "synthetic-etag"}
    retrieved_at = datetime(2026, 9, 1, tzinfo=UTC)
    audit = {
        "format": 1,
        "dataset_id": dataset_id,
        "retrieved_at": retrieved_at,
        "returned_url": returned_url,
        "response_media_type": "application/xml",
        "byte_count": len(raw),
        "headers": headers,
    }
    stored = {key: value for key, value in semantic.items() if key != "format"}
    stored.update(
        retrieved_at=retrieved_at,
        response_media_type="application/xml",
        byte_count=len(raw),
        dataset_id=dataset_id,
        manifest_id=canonical_sha256(audit),
    )
    return stored, returned_url, headers


def _events() -> tuple[PolicyEventManifest, dict[str, bytes]]:
    payload = b"synthetic official policy history"
    content_hash = hashlib.sha256(payload).hexdigest()
    source = PolicySourceEvidence(
        source_url="https://rba.gov.au/synthetic-policy-history",
        retrieved_at=datetime(2023, 12, 31, tzinfo=UTC),
        content_hash=content_hash,
        byte_count=len(payload),
        media_type="text/html",
        source_kind="official_rate_history",
    )
    event = PolicyRateEvent(
        event_id="aud_baseline",
        kind=PolicyEventKind.BASELINE,
        currency="AUD",
        central_bank_id="rba",
        policy_instrument_id="cash_rate_target",
        announcement_lower=datetime(2013, 12, 30, tzinfo=UTC),
        announcement_upper=datetime(2013, 12, 30, tzinfo=UTC),
        announcement_precision=TimePrecision.EXACT_TIMESTAMP,
        effective_lower=datetime(2013, 12, 30, tzinfo=UTC),
        effective_upper=datetime(2013, 12, 30, tzinfo=UTC),
        effective_precision=TimePrecision.EXACT_TIMESTAMP,
        source_timezone="Australia_Sydney",
        old_rate=None,
        new_rate=Decimal("2.50"),
        source=source,
        evidence_classification=EvidenceClassification.OFFICIAL_RATE_HISTORY,
        ambiguity=AmbiguityState.CLEAR,
        conflict=AmbiguityState.CLEAR,
    )
    return PolicyEventManifest((event,)), {content_hash: payload}


def _spot_datasets(
    *, quality: ProvenanceQuality = ProvenanceQuality.VERIFIED,
) -> tuple[tuple[BarDataset, ...], tuple[SpotObservationReference, ...]]:
    datasets = []
    references = []
    opened = datetime(2023, 12, 28, tzinfo=UTC)
    closed = datetime(2023, 12, 29, tzinfo=UTC)
    for offset, pair in enumerate(APPROVED_PAIRS):
        frame = pd.DataFrame(
            [[1.0 + offset, 1.1 + offset, 0.9 + offset, 1.05 + offset, 10.0]],
            index=pd.DatetimeIndex([opened], name="ts_open"),
            columns=["open", "high", "low", "close", "volume"],
            dtype="float64",
        )
        frame.attrs = {"symbol": pair, "timeframe": "D1"}
        query = BarQuery(CanonicalInstrument(pair), "D1", opened, closed, closed)
        content_hash = bar_content_hash(frame)
        provenance = DataProvenance(
            provider_id="dukascopy",
            provider_version="1",
            normalization_version="dukascopy_bid_v1",
            canonical_symbol=pair,
            provider_symbol=DUKASCOPY_SYMBOLS[pair],
            timeframe="D1",
            query_start=opened,
            query_end=closed,
            query_as_of=closed,
            retrieved_at=datetime(2023, 12, 30, tzinfo=UTC),
            actual_first_observation=opened,
            actual_last_observation=opened,
            row_count=1,
            content_hash=content_hash,
            query_fingerprint=query.fingerprint,
            dataset_id=dataset_identity(
                "dukascopy", "1", query.fingerprint, content_hash
            ),
            revision="synthetic_revision",
            source_timezone="UTC",
            volume_semantics="provider_reported",
            provenance_quality=quality,
            sanitized_source_reference="dukascopy:historical:bid",
        )
        dataset = BarDataset(query, frame, provenance)
        datasets.append(dataset)
        references.append(
            SpotObservationReference(pair, provenance.dataset_id, opened, closed, "close", True)
        )
    return tuple(datasets), tuple(references)


def test_bis_v2_persists_every_acquisition_manifest_identity_input() -> None:
    acquisition_contract, schema, *_rest, build, _a, _b, _c, _d = _persistence()
    manifest, returned_url, headers = _bis_manifest()
    persisted = build(
        manifest=manifest,
        returned_url=returned_url,
        response_headers=headers,
    )
    assert persisted["schema"] == schema
    assert persisted["audit_contract"] == acquisition_contract
    assert persisted["returned_url"] == returned_url
    assert persisted["response_headers"] == headers
    expected_audit = {
        "format": 1,
        "dataset_id": persisted["dataset_id"],
        "retrieved_at": persisted["retrieved_at"],
        "returned_url": returned_url,
        "response_media_type": persisted["response_media_type"],
        "byte_count": persisted["byte_count"],
        "headers": headers,
    }
    assert persisted["manifest_id"] == canonical_sha256(expected_audit)


@pytest.mark.parametrize("missing", ("returned_url", "response_headers"))
def test_bis_v2_rejects_missing_acquisition_audit_inputs(missing: str) -> None:
    *_prefix, build, _a, _b, _c, _d = _persistence()
    manifest, returned_url, headers = _bis_manifest()
    arguments = {"manifest": manifest, "returned_url": returned_url, "response_headers": headers}
    arguments[missing] = None
    with pytest.raises(ValueError, match="bis_audit_evidence_invalid"):
        build(**arguments)


def test_bis_v2_recomputes_and_rejects_stored_identity_claims() -> None:
    *_prefix, build, _a, _b, _c, _d = _persistence()
    manifest, returned_url, headers = _bis_manifest()
    manifest["dataset_id"] = "f" * 64
    with pytest.raises(ValueError, match="bis_dataset_identity_mismatch"):
        build(manifest=manifest, returned_url=returned_url, response_headers=headers)


def test_authoritative_bis_publisher_materializes_the_v2_audit_contract() -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    manifest, returned_url, headers = _bis_manifest()
    persisted = ingestion.persisted_authoritative_bis_manifest(
        manifest,
        SimpleNamespace(final_url=returned_url, headers=headers),
    )
    assert persisted["schema"] == "candidate_b_bis_authoritative.v2"
    assert persisted["returned_url"] == returned_url
    assert persisted["response_headers"] == headers


def test_policy_event_publication_preserves_sources_and_recomputes_identities(tmp_path) -> None:
    *_head, event_schema, _spot_schema, _build, persist, _spot, verify, _verify_spot = (
        _persistence()
    )
    events, sources = _events()
    publication = persist(tmp_path / "events", events, sources)
    loaded = verify(publication.destination)
    persisted = publication.manifest
    assert persisted["schema"] == event_schema
    assert loaded.event_manifest == events
    assert loaded.event_manifest.manifest_id == events.manifest_id
    assert persisted["events"][0]["event_identity"] == events.events[0].identity
    assert publication.publication_id == persisted["publication_id"]
    assert (publication.destination / persisted["events"][0]["source_artifact"]).read_bytes()
    assert "source_url" in persisted["events"][0]["event"]["source"]


def test_policy_event_publication_rejects_source_hash_mismatch(tmp_path) -> None:
    *_head, persist, _spot, _verify, _verify_spot = _persistence()[4:]
    events, sources = _events()
    wrong = {next(iter(sources)): b"wrong"}
    with pytest.raises(ValueError, match="event_source_hash_mismatch"):
        persist(tmp_path / "events", events, wrong)


def test_spot_panel_publication_separates_dataset_panel_and_publication_ids(tmp_path) -> None:
    *_head, spot_schema, _build, _events_persist, persist, _events_verify, verify = (
        _persistence()[2:]
    )
    datasets, references = _spot_datasets()
    publication = persist(tmp_path / "spot", datasets, references)
    loaded = verify(publication.destination)
    expected_panel_id = canonical_sha256(
        {
            "format": 1,
            "dataset_ids": tuple(item.provenance.dataset_id for item in datasets),
            "observations": tuple(sorted(references, key=lambda item: (item.bar_close, item.pair))),
        }
    )
    assert publication.manifest["schema"] == spot_schema
    assert loaded.spot_panel.manifest_id == expected_panel_id
    assert loaded.spot_panel.dataset_ids == tuple(
        item.provenance.dataset_id for item in datasets
    )
    assert publication.publication_id != expected_panel_id
    assert all(
        entry["dataset_id"] == dataset.provenance.dataset_id
        for entry, dataset in zip(publication.manifest["datasets"], datasets, strict=True)
    )


def test_spot_panel_verification_recomputes_observation_and_publication_identities(
    tmp_path,
) -> None:
    *_head, persist, _events_verify, verify = _persistence()[6:]
    datasets, references = _spot_datasets()
    publication = persist(tmp_path / "spot", datasets, references)
    manifest_path = publication.destination / "manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    import json

    changed = json.loads(original)
    changed["publication_id"] = "f" * 64
    manifest_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="spot_publication_identity_mismatch"):
        verify(publication.destination)


def test_spot_publication_identity_binds_complete_persisted_provenance(tmp_path) -> None:
    *_head, persist, _events_verify, verify = _persistence()[6:]
    datasets, references = _spot_datasets()
    publication = persist(tmp_path / "spot", datasets, references)
    manifest_path = publication.destination / "manifest.json"
    import json

    changed = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed["datasets"][0]["provenance"]["revision"] = "different_audit_revision"
    manifest_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="spot_publication_identity_mismatch"):
        verify(publication.destination)


def test_spot_panel_rejects_legacy_unverified_before_publication(tmp_path) -> None:
    *_head, persist, _events_verify, _verify = _persistence()[6:]
    datasets, references = _spot_datasets(quality=ProvenanceQuality.LEGACY_UNVERIFIED)
    with pytest.raises(ValueError, match="spot_provenance_not_verified"):
        persist(tmp_path / "spot", datasets, references)


def test_spot_panel_requires_exact_seven_pair_membership(tmp_path) -> None:
    *_head, persist, _events_verify, _verify = _persistence()[6:]
    datasets, references = _spot_datasets()
    with pytest.raises(ValueError, match="spot_pair_membership_invalid"):
        persist(tmp_path / "spot", datasets[:-1], references[:-1])


def test_publications_reject_existing_destination_without_repair(tmp_path) -> None:
    *_head, persist_events, persist_spot, _verify_events, _verify_spot = _persistence()[5:]
    destination = tmp_path / "evidence"
    destination.mkdir()
    events, sources = _events()
    with pytest.raises(ValueError, match="destination_exists"):
        persist_events(destination, events, sources)
    datasets, references = _spot_datasets()
    with pytest.raises(ValueError, match="destination_exists"):
        persist_spot(destination, datasets, references)
