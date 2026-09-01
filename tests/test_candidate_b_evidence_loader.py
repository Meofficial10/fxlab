"""Offline loading of persisted Candidate B evidence into the formation builder."""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from fxlab.data.dukascopy_provider import DUKASCOPY_SYMBOLS
from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    APPROVED_PAIRS,
    AmbiguityState,
    EvidenceClassification,
    PolicyEventKind,
    PolicyEventManifest,
    PolicyRateEvent,
    PolicyRateSeriesManifest,
    PolicySourceEvidence,
    SpotObservationReference,
    TimePrecision,
    canonical_json,
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
from fxlab.research.candidate_b_measurement import MEASURED_MONTHS

RETRIEVED = datetime(2023, 12, 31, 23, 0, tzinfo=UTC)


def _sdmx_raw(currency: str) -> bytes:
    area = APPROVED_BIS_SERIES[currency].split(".", 1)[1]
    if currency == "USD":
        observed = date(2014, 1, 1)
        dates: list[date] = []
        while observed <= date(2023, 12, 31):
            dates.append(observed)
            observed += timedelta(days=1)
    else:
        dates = [date(2014, 1, 1), date(2023, 12, 29)]
    observations = "".join(
        f'<Obs TIME_PERIOD="{item.isoformat()}" OBS_VALUE="2.50" OBS_STATUS="A" />'
        for item in dates
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<mes:StructureSpecificData '
        'xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message" '
        'xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common" '
        'xmlns:ss="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/structurespecific" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:cbpol="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow='
        'BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD">'
        '<mes:Header><mes:Structure structureID="BIS_WS_CBPOL_1_0" '
        'namespace="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow='
        'BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD" '
        'dimensionAtObservation="TIME_PERIOD"><com:StructureUsage><Ref '
        'agencyID="BIS" id="WS_CBPOL" version="1.0" /></com:StructureUsage>'
        '</mes:Structure></mes:Header>'
        '<mes:DataSet UNIT_MEASURE="368" UNIT_MULT="0" '
        'ss:dataScope="DataStructure" ss:structureRef="BIS_WS_CBPOL_1_0" '
        f'xsi:type="cbpol:DataSetType"><Series FREQ="D" REF_AREA="{area}">'
        f"{observations}</Series></mes:DataSet></mes:StructureSpecificData>"
    ).encode()


class _Transport:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.calls: list[str] = []

    def fetch(self, request, *, exact_url, accept, timeout_seconds, max_response_bytes):
        from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

        del request, accept, timeout_seconds, max_response_bytes
        self.calls.append(exact_url)
        return AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=exact_url,
            media_type="application/xml",
            headers={"content-type": "application/xml", "etag": "synthetic"},
            raw_bytes=self.raw,
        )


def _publish_bis(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data import policy_rates

    root = tmp_path / "bis"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    slugs = {
        "AUD": "d_au",
        "CAD": "d_ca",
        "CHF": "d_ch",
        "EUR": "d_xm",
        "GBP": "d_gb",
        "JPY": "d_jp",
        "NZD": "d_nz",
        "USD": "d_us",
    }
    publications: dict[str, Path] = {}
    for currency in APPROVED_BIS_SERIES:
        request = getattr(policy_rates, f"authoritative_{slugs[currency]}_request")()
        acquire = getattr(ingestion, f"acquire_and_publish_authoritative_{slugs[currency]}")
        publication = acquire(request, _Transport(_sdmx_raw(currency)), RETRIEVED)
        publications[currency] = publication.destination
    return publications


def _event_publication(tmp_path: Path) -> Path:
    from fxlab.data.candidate_b_evidence import persist_candidate_b_policy_event_evidence

    domains = {
        "AUD": "rba.gov.au",
        "CAD": "bankofcanada.ca",
        "CHF": "snb.ch",
        "EUR": "ecb.europa.eu",
        "GBP": "bankofengland.co.uk",
        "JPY": "boj.or.jp",
        "NZD": "rbnz.govt.nz",
        "USD": "federalreserve.gov",
    }
    events = []
    sources: dict[str, bytes] = {}
    for currency in APPROVED_BIS_SERIES:
        raw = f"synthetic official {currency} policy history".encode()
        content_hash = hashlib.sha256(raw).hexdigest()
        sources[content_hash] = raw
        source = PolicySourceEvidence(
            source_url=f"https://{domains[currency]}/synthetic-policy-history",
            retrieved_at=RETRIEVED,
            content_hash=content_hash,
            byte_count=len(raw),
            media_type="text/html",
            source_kind="official_rate_history",
        )
        events.append(
            PolicyRateEvent(
                event_id=f"{currency.lower()}_baseline",
                kind=PolicyEventKind.BASELINE,
                currency=currency,
                central_bank_id=f"{currency.lower()}_central_bank",
                policy_instrument_id="principal_policy_rate",
                announcement_lower=datetime(2013, 12, 31, tzinfo=UTC),
                announcement_upper=datetime(2013, 12, 31, tzinfo=UTC),
                announcement_precision=TimePrecision.EXACT_TIMESTAMP,
                effective_lower=datetime(2013, 12, 31, tzinfo=UTC),
                effective_upper=datetime(2013, 12, 31, tzinfo=UTC),
                effective_precision=TimePrecision.EXACT_TIMESTAMP,
                source_timezone="UTC",
                old_rate=None,
                new_rate=Decimal("2.50"),
                source=source,
                evidence_classification=EvidenceClassification.OFFICIAL_RATE_HISTORY,
                ambiguity=AmbiguityState.CLEAR,
                conflict=AmbiguityState.CLEAR,
            )
        )
    destination = tmp_path / "events"
    persist_candidate_b_policy_event_evidence(
        destination, PolicyEventManifest(tuple(events)), sources
    )
    return destination


def _month_closes() -> tuple[datetime, ...]:
    return tuple(
        datetime(year, month, calendar.monthrange(year, month)[1], tzinfo=UTC)
        for year in range(2015, 2024)
        for month in range(1, 13)
    )


def _spot_publication(tmp_path: Path) -> Path:
    from fxlab.data.candidate_b_evidence import persist_candidate_b_spot_panel_evidence

    closes = _month_closes()
    datasets = []
    references = []
    for offset, pair in enumerate(APPROVED_PAIRS):
        opens = tuple(item - timedelta(days=1) for item in closes)
        query = BarQuery(CanonicalInstrument(pair), "D1", opens[0], closes[-1], closes[-1])
        frame = pd.DataFrame(
            {
                "open": [1.0 + offset] * len(opens),
                "high": [1.1 + offset] * len(opens),
                "low": [0.9 + offset] * len(opens),
                "close": [1.05 + offset] * len(opens),
                "volume": [0.0] * len(opens),
            },
            index=pd.DatetimeIndex(opens, name="ts_open"),
            dtype="float64",
        )
        frame.attrs = {"symbol": pair, "timeframe": "D1"}
        content_hash = bar_content_hash(frame)
        identity = dataset_identity("dukascopy", "1", query.fingerprint, content_hash)
        provenance = DataProvenance(
            provider_id="dukascopy",
            provider_version="1",
            normalization_version="dukascopy_bid_v1",
            canonical_symbol=pair,
            provider_symbol=DUKASCOPY_SYMBOLS[pair],
            timeframe="D1",
            query_start=query.start,
            query_end=query.end,
            query_as_of=query.as_of,
            retrieved_at=RETRIEVED,
            actual_first_observation=opens[0],
            actual_last_observation=opens[-1],
            row_count=len(opens),
            content_hash=content_hash,
            query_fingerprint=query.fingerprint,
            dataset_id=identity,
            revision="synthetic_revision",
            source_timezone="UTC",
            volume_semantics="provider_reported",
            provenance_quality=ProvenanceQuality.VERIFIED,
            sanitized_source_reference="dukascopy:historical:bid",
        )
        datasets.append(BarDataset(query, frame, provenance))
        references.extend(
            SpotObservationReference(pair, identity, opened, closed, "close", True)
            for opened, closed in zip(opens, closes, strict=True)
        )
    destination = tmp_path / "spot"
    persist_candidate_b_spot_panel_evidence(destination, datasets, references)
    return destination


def test_acquisition_bis_verifier_reconstructs_validated_authoritative_manifest(
    tmp_path, monkeypatch
) -> None:
    from fxlab.data.candidate_b_evidence import (
        AuthoritativeSdmxPolicyRateSeriesManifest,
        verify_candidate_b_bis_evidence,
    )

    publications = _publish_bis(tmp_path, monkeypatch)
    loaded = verify_candidate_b_bis_evidence(publications["AUD"])
    assert isinstance(loaded.series_manifest, PolicyRateSeriesManifest)
    assert isinstance(loaded.series_manifest, AuthoritativeSdmxPolicyRateSeriesManifest)
    assert loaded.audit_contract == "candidate_b_bis_acquisition_audit.v1"
    assert loaded.series_manifest.dataset_id == loaded.persisted_manifest["dataset_id"]
    assert loaded.series_manifest.manifest_id == loaded.persisted_manifest["manifest_id"]


def test_explicit_count_legacy_bis_migrates_only_to_distinct_migration_audit(
    tmp_path, monkeypatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.candidate_b_evidence import verify_candidate_b_bis_evidence

    publication = _publish_bis(tmp_path, monkeypatch)["CAD"]
    manifest_path = publication / "manifest.json"
    legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_manifest_id = legacy["manifest_id"]
    for name in ("schema", "audit_contract", "returned_url", "response_headers"):
        legacy.pop(name)
    manifest_path.write_text(canonical_json(legacy), encoding="utf-8")

    result = ingestion.migrate_legacy_authoritative_bis_manifest(publication)
    migrated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.status == "migrated"
    assert migrated["audit_contract"] == "candidate_b_bis_migration_audit.v1"
    assert migrated["legacy_manifest_id"] == original_manifest_id
    assert migrated["manifest_id"] != original_manifest_id
    assert "returned_url" not in migrated
    assert "response_headers" not in migrated
    assert verify_candidate_b_bis_evidence(publication).audit_contract == migrated[
        "audit_contract"
    ]


def test_bis_verifier_recomputes_and_rejects_forged_stored_identity(
    tmp_path, monkeypatch
) -> None:
    from fxlab.data.candidate_b_evidence import verify_candidate_b_bis_evidence

    publication = _publish_bis(tmp_path, monkeypatch)["AUD"]
    manifest_path = publication / "manifest.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored["dataset_id"] = "f" * 64
    manifest_path.write_text(canonical_json(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="bis_dataset_identity_mismatch"):
        verify_candidate_b_bis_evidence(publication)


def test_loader_requires_exact_eight_explicit_bis_paths(tmp_path, monkeypatch) -> None:
    from fxlab.data.candidate_b_evidence import load_candidate_b_verified_evidence

    publications = _publish_bis(tmp_path, monkeypatch)
    events = _event_publication(tmp_path)
    spot = _spot_publication(tmp_path)
    publications.pop("NZD")
    with pytest.raises(ValueError, match="bis_path_membership_invalid"):
        load_candidate_b_verified_evidence(
            bis_publication_paths=publications,
            policy_event_publication_path=events,
            spot_panel_publication_path=spot,
        )


def test_loader_reconstructs_all_three_verified_evidence_categories(
    tmp_path, monkeypatch
) -> None:
    from fxlab.data.candidate_b_evidence import load_candidate_b_verified_evidence

    publications = _publish_bis(tmp_path, monkeypatch)
    bundle = load_candidate_b_verified_evidence(
        bis_publication_paths=publications,
        policy_event_publication_path=_event_publication(tmp_path),
        spot_panel_publication_path=_spot_publication(tmp_path),
    )
    assert tuple(item.request.series.currency for item in bundle.series_manifests) == tuple(
        APPROVED_BIS_SERIES
    )
    assert all(isinstance(item, PolicyRateSeriesManifest) for item in bundle.series_manifests)
    assert len(bundle.event_manifest.events) == len(APPROVED_BIS_SERIES)
    assert len(bundle.spot_panel.dataset_ids) == len(APPROVED_PAIRS)


def test_composition_calls_existing_builder_only_after_complete_verification(
    tmp_path, monkeypatch
) -> None:
    from scripts.build_candidate_b_formations import (
        build_candidate_b_formation_manifest_from_persisted_evidence,
    )

    manifest = build_candidate_b_formation_manifest_from_persisted_evidence(
        bis_publication_paths=_publish_bis(tmp_path, monkeypatch),
        policy_event_publication_path=_event_publication(tmp_path),
        spot_panel_publication_path=_spot_publication(tmp_path),
    )
    assert tuple(item.formation_month for item in manifest.formations) == MEASURED_MONTHS
    assert (manifest.train_count, manifest.validation_count, manifest.total_count) == (83, 23, 106)


def test_composition_rejects_unverified_spot_before_builder(
    tmp_path, monkeypatch
) -> None:
    from scripts.build_candidate_b_formations import (
        build_candidate_b_formation_manifest_from_persisted_evidence,
    )

    spot = _spot_publication(tmp_path)
    stored = json.loads((spot / "manifest.json").read_text(encoding="utf-8"))
    stored["publication_id"] = "f" * 64
    (spot / "manifest.json").write_text(canonical_json(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="spot_publication_identity_mismatch"):
        build_candidate_b_formation_manifest_from_persisted_evidence(
            bis_publication_paths=_publish_bis(tmp_path, monkeypatch),
            policy_event_publication_path=_event_publication(tmp_path),
            spot_panel_publication_path=spot,
        )


def test_event_verifier_rejects_post_seal_before_rate_conversion(tmp_path) -> None:
    from fxlab.data.candidate_b_evidence import verify_candidate_b_policy_event_evidence

    events = _event_publication(tmp_path)
    manifest_path = events / "manifest.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored["events"][0]["event"]["announcement_upper"] = "2024-01-01T00:00:00+00:00"
    stored["events"][0]["event"]["new_rate"] = "economic-value-must-not-be-converted"
    manifest_path.write_text(canonical_json(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="sealed_window_violation"):
        verify_candidate_b_policy_event_evidence(events)


def test_spot_verifier_rejects_post_seal_reference_before_parquet_access(tmp_path) -> None:
    from fxlab.data.candidate_b_evidence import verify_candidate_b_spot_panel_evidence

    spot = _spot_publication(tmp_path)
    manifest_path = spot / "manifest.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored["observations"][0]["bar_open"] = "2024-01-01T00:00:00+00:00"
    stored["observations"][0]["bar_close"] = "2024-01-02T00:00:00+00:00"
    manifest_path.write_text(canonical_json(stored), encoding="utf-8")
    (spot / stored["datasets"][0]["artifact"]).write_bytes(b"must-not-be-read")
    with pytest.raises(ValueError, match="sealed_window_violation"):
        verify_candidate_b_spot_panel_evidence(spot)
