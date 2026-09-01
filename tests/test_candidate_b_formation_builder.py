from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from scripts.qualify_candidate_b_data import qualify_candidate_b

from fxlab.data.dukascopy_provider import DUKASCOPY_SYMBOLS
from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    APPROVED_PAIRS,
    AmbiguityState,
    CandidateBFormationManifest,
    EvidenceClassification,
    PolicyEventKind,
    PolicyEventManifest,
    PolicyRateEvent,
    PolicyRateMetadata,
    PolicyRateQualificationError,
    PolicyRateRequest,
    PolicyRateSeriesManifest,
    PolicyRateSeriesSpec,
    PolicySourceEvidence,
    SpotObservationReference,
    SpotPanelManifestReference,
    TimePrecision,
    build_series_manifest,
    canonical_sha256,
    reconcile_policy_series,
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
from fxlab.research.candidate_b_measurement import MEASURED_MONTHS, PURGED_MONTH
from fxlab.research.candidate_b_run_identity import (
    build_candidate_b_environment_evidence,
    build_candidate_b_qualified_run_bindings,
    build_candidate_b_spot_semantic_evidence,
    build_candidate_b_static_run_contract,
)


@dataclass(frozen=True)
class SyntheticEvidence:
    series_manifests: tuple[PolicyRateSeriesManifest, ...]
    event_manifest: PolicyEventManifest
    spot_panel: SpotPanelManifestReference
    spot_datasets: tuple[BarDataset, ...]


def _builder():
    from scripts.build_candidate_b_formations import (
        build_candidate_b_formation_manifest,
    )

    return build_candidate_b_formation_manifest


def _month_closes() -> tuple[datetime, ...]:
    return tuple(
        datetime(year, month, calendar.monthrange(year, month)[1], tzinfo=UTC)
        for year in range(2015, 2024)
        for month in range(1, 13)
    )


def _policy_request(currency: str) -> PolicyRateRequest:
    return PolicyRateRequest(
        PolicyRateSeriesSpec(currency, APPROVED_BIS_SERIES[currency]),
        date(2014, 1, 1),
        date(2023, 12, 31),
    )


def _policy_metadata(currency: str) -> PolicyRateMetadata:
    series_key = APPROVED_BIS_SERIES[currency]
    return PolicyRateMetadata(
        agency="BIS",
        dataflow="WS_CBPOL",
        version="1.0",
        frequency="D",
        series_key=series_key,
        currency=currency,
        reference_area=series_key.split(".", 1)[1],
        unit="percent",
        scale=0,
        observation_status_semantics=("A=normal",),
        dsd_identity="bis_cbpol_1_0",
        codelist_identity="cl_obs_status",
        instrument_metadata="principal_policy_rate",
        source_identity="bis_ws_cbpol",
        endpoint_identity="bis_api_v2",
        media_type="text/csv",
        revision="synthetic_revision_1",
    )


def _policy_raw(currency: str, *, transition: bool) -> bytes:
    area = APPROVED_BIS_SERIES[currency].split(".", 1)[1]
    lines = [
        "FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE,OBS_STATUS",
        f"D,{area},2014-01-01,2.50,A",
    ]
    if transition and currency == "AUD":
        lines.append(f"D,{area},2020-02-01,3.00,A")
    return ("\n".join(lines) + "\n").encode()


def _official_source(currency: str) -> PolicySourceEvidence:
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
    return PolicySourceEvidence(
        source_url=f"https://{domains[currency]}/synthetic-policy-history",
        retrieved_at=datetime(2023, 12, 31, tzinfo=UTC),
        content_hash=hashlib.sha256(currency.encode()).hexdigest(),
        byte_count=1,
        media_type="text/html",
        source_kind="official_rate_history",
    )


def _event(
    currency: str,
    *,
    event_id: str,
    kind: PolicyEventKind,
    old_rate: str | None,
    new_rate: str,
    announced: datetime,
    effective: datetime,
    ambiguity: AmbiguityState = AmbiguityState.CLEAR,
) -> PolicyRateEvent:
    return PolicyRateEvent(
        event_id=event_id,
        kind=kind,
        currency=currency,
        central_bank_id=f"{currency.lower()}_central_bank",
        policy_instrument_id="principal_policy_rate",
        announcement_lower=announced,
        announcement_upper=announced,
        announcement_precision=TimePrecision.EXACT_TIMESTAMP,
        effective_lower=effective,
        effective_upper=effective,
        effective_precision=TimePrecision.EXACT_TIMESTAMP,
        source_timezone="UTC",
        old_rate=old_rate,
        new_rate=new_rate,
        source=_official_source(currency),
        evidence_classification=EvidenceClassification.OFFICIAL_RATE_HISTORY,
        ambiguity=ambiguity,
        conflict=AmbiguityState.CLEAR,
    )


def _forge(instance, **changes):
    result = object.__new__(type(instance))
    for item in fields(instance):
        object.__setattr__(result, item.name, changes.get(item.name, getattr(instance, item.name)))
    return result


def _synthetic_evidence(*, transition: bool = False) -> SyntheticEvidence:
    manifests = tuple(
        build_series_manifest(
            _policy_request(currency),
            _policy_metadata(currency),
            _policy_raw(currency, transition=transition),
            datetime(2023, 12, 31, tzinfo=UTC),
        )
        for currency in APPROVED_BIS_SERIES
    )
    events = [
        _event(
            currency,
            event_id=f"{currency.lower()}_baseline",
            kind=PolicyEventKind.BASELINE,
            old_rate=None,
            new_rate="2.50",
            announced=datetime(2013, 12, 31, tzinfo=UTC),
            effective=datetime(2013, 12, 31, tzinfo=UTC),
        )
        for currency in APPROVED_BIS_SERIES
    ]
    if transition:
        events.append(
            _event(
                "AUD",
                event_id="aud_2020_change",
                kind=PolicyEventKind.RATE_CHANGE,
                old_rate="2.50",
                new_rate="3.00",
                announced=datetime(2020, 2, 1, tzinfo=UTC),
                effective=datetime(2020, 2, 1, tzinfo=UTC),
            )
        )

    closes = _month_closes()
    datasets: list[BarDataset] = []
    references: list[SpotObservationReference] = []
    dataset_ids: list[str] = []
    for pair in APPROVED_PAIRS:
        opens = tuple(item - timedelta(days=1) for item in closes)
        query = BarQuery(CanonicalInstrument(pair), "D1", opens[0], closes[-1], closes[-1])
        frame = pd.DataFrame(
            {
                "open": [1.0] * len(opens),
                "high": [1.0] * len(opens),
                "low": [1.0] * len(opens),
                "close": [1.0] * len(opens),
                "volume": [0.0] * len(opens),
            },
            index=pd.DatetimeIndex(opens),
            dtype="float64",
        )
        frame.index.name = "ts_open"
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
            retrieved_at=datetime(2023, 12, 31, tzinfo=UTC),
            actual_first_observation=opens[0],
            actual_last_observation=opens[-1],
            row_count=len(opens),
            content_hash=content_hash,
            query_fingerprint=query.fingerprint,
            dataset_id=identity,
            revision="synthetic_revision_1",
            source_timezone="UTC",
            provenance_quality=ProvenanceQuality.VERIFIED,
            sanitized_source_reference="dukascopy:historical:bid",
        )
        datasets.append(BarDataset(query, frame, provenance))
        dataset_ids.append(identity)
        references.extend(
            SpotObservationReference(pair, identity, opened, closed, "close", True)
            for opened, closed in zip(opens, closes, strict=True)
        )
    panel_id = canonical_sha256({"format": 1, "datasets": tuple(dataset_ids)})
    return SyntheticEvidence(
        manifests,
        PolicyEventManifest(tuple(events)),
        SpotPanelManifestReference(panel_id, tuple(dataset_ids), tuple(references)),
        tuple(datasets),
    )


def test_builder_constructs_exact_measured_formation_manifest() -> None:
    evidence = _synthetic_evidence()
    manifest = _builder()(
        series_manifests=evidence.series_manifests,
        event_manifest=evidence.event_manifest,
        spot_panel=evidence.spot_panel,
    )
    assert isinstance(manifest, CandidateBFormationManifest)
    assert tuple(item.formation_month for item in manifest.formations) == MEASURED_MONTHS
    assert PURGED_MONTH not in {item.formation_month for item in manifest.formations}
    assert (manifest.train_count, manifest.validation_count, manifest.total_count) == (83, 23, 106)
    assert manifest.qualified is True
    required_sources = {
        *(item.manifest_id for item in evidence.series_manifests),
        evidence.event_manifest.manifest_id,
        evidence.spot_panel.manifest_id,
    }
    for formation in manifest.formations:
        assert len(formation.spot_observations) == len(APPROVED_PAIRS) == 7
        assert {item.pair for item in formation.spot_observations} == set(APPROVED_PAIRS)
        assert {item.currency for item in formation.policy_states} == set(APPROVED_BIS_SERIES)
        assert required_sources.issubset(formation.source_manifest_fingerprints)


@pytest.mark.parametrize("kind", ["missing", "duplicate"])
def test_builder_rejects_missing_or_duplicate_series(kind: str) -> None:
    evidence = _synthetic_evidence()
    series = (
        evidence.series_manifests[:-1]
        if kind == "missing"
        else (*evidence.series_manifests, evidence.series_manifests[0])
    )
    with pytest.raises(PolicyRateQualificationError, match=f"{kind}_series"):
        _builder()(
            series_manifests=series,
            event_manifest=evidence.event_manifest,
            spot_panel=evidence.spot_panel,
        )


def test_builder_rejects_one_missing_spot_observation() -> None:
    evidence = _synthetic_evidence()
    target = datetime(2018, 5, 31, tzinfo=UTC)
    observations = tuple(
        item
        for item in evidence.spot_panel.observations
        if not (item.pair == "AUDUSD" and item.bar_close == target)
    )
    panel = SpotPanelManifestReference(
        evidence.spot_panel.manifest_id, evidence.spot_panel.dataset_ids, observations
    )
    with pytest.raises(PolicyRateQualificationError, match="missing_spot_pair"):
        _builder()(
            series_manifests=evidence.series_manifests,
            event_manifest=evidence.event_manifest,
            spot_panel=panel,
        )


def test_builder_rejects_one_missing_policy_state() -> None:
    evidence = _synthetic_evidence()
    events = PolicyEventManifest(
        tuple(item for item in evidence.event_manifest.events if item.currency != "NZD")
    )
    with pytest.raises(PolicyRateQualificationError, match="missing_official_baseline"):
        _builder()(
            series_manifests=evidence.series_manifests,
            event_manifest=events,
            spot_panel=evidence.spot_panel,
        )


def test_builder_does_not_leak_future_event_backward() -> None:
    evidence = _synthetic_evidence(transition=True)
    manifest = _builder()(
        series_manifests=evidence.series_manifests,
        event_manifest=evidence.event_manifest,
        spot_panel=evidence.spot_panel,
    )
    january = next(item for item in manifest.formations if item.formation_month == "2020-01")
    february = next(item for item in manifest.formations if item.formation_month == "2020-02")
    january_aud = next(item for item in january.policy_states if item.currency == "AUD")
    february_aud = next(item for item in february.policy_states if item.currency == "AUD")
    assert january_aud.event_id == "aud_baseline"
    assert january_aud.observation_value == Decimal("2.50")
    assert february_aud.event_id == "aud_2020_change"
    assert february_aud.observation_value == Decimal("3.00")


class _ExplodingEconomicValue:
    def __eq__(self, other):
        raise AssertionError("economic value was accessed before date/status validation")


def test_builder_rejects_m_row_without_creating_numeric_policy_state() -> None:
    evidence = _synthetic_evidence()
    manifest = evidence.series_manifests[0]
    missing = _forge(
        manifest.observations[0],
        observation_date=date(2014, 1, 2),
        value=Decimal("NaN"),
        status="M",
    )
    forged = _forge(manifest, observations=(*manifest.observations, missing))
    with pytest.raises(PolicyRateQualificationError, match="policy_observation_not_numeric"):
        _builder()(
            series_manifests=(forged, *evidence.series_manifests[1:]),
            event_manifest=evidence.event_manifest,
            spot_panel=evidence.spot_panel,
        )


def test_builder_rejects_forged_policy_dataset_identity() -> None:
    evidence = _synthetic_evidence()
    forged = _forge(evidence.series_manifests[0], dataset_id="f" * 64)
    with pytest.raises(PolicyRateQualificationError, match="wrong_dataset_identity"):
        _builder()(
            series_manifests=(forged, *evidence.series_manifests[1:]),
            event_manifest=evidence.event_manifest,
            spot_panel=evidence.spot_panel,
        )


def test_builder_rejects_wrong_event_observation_binding() -> None:
    evidence = _synthetic_evidence()
    baseline = next(item for item in evidence.event_manifest.events if item.currency == "AUD")
    wrong = _forge(baseline, new_rate=Decimal("9.99"))
    events = PolicyEventManifest(
        tuple(
            wrong if item.event_id == baseline.event_id else item
            for item in evidence.event_manifest.events
        )
    )
    with pytest.raises(PolicyRateQualificationError, match="concordance_failed"):
        _builder()(
            series_manifests=evidence.series_manifests,
            event_manifest=events,
            spot_panel=evidence.spot_panel,
        )


def test_builder_rejects_ambiguous_official_evidence() -> None:
    evidence = _synthetic_evidence()
    baseline = next(item for item in evidence.event_manifest.events if item.currency == "AUD")
    ambiguous = _forge(baseline, ambiguity=AmbiguityState.AMBIGUOUS)
    events = PolicyEventManifest(
        tuple(
            ambiguous if item.event_id == baseline.event_id else item
            for item in evidence.event_manifest.events
        )
    )
    with pytest.raises(PolicyRateQualificationError, match="ambiguous_official_evidence"):
        _builder()(
            series_manifests=evidence.series_manifests,
            event_manifest=events,
            spot_panel=evidence.spot_panel,
        )


def test_builder_rejects_post_seal_policy_date_before_economic_value_access() -> None:
    evidence = _synthetic_evidence()
    manifest = evidence.series_manifests[0]
    future = _forge(
        manifest.observations[0], observation_date=date(2024, 1, 1), value=_ExplodingEconomicValue()
    )
    forged = _forge(manifest, observations=(future,))
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        _builder()(
            series_manifests=(forged, *evidence.series_manifests[1:]),
            event_manifest=evidence.event_manifest,
            spot_panel=evidence.spot_panel,
        )


def test_builder_output_passes_aggregate_qualification_and_run_binding() -> None:
    evidence = _synthetic_evidence()
    formation_manifest = _builder()(
        series_manifests=evidence.series_manifests,
        event_manifest=evidence.event_manifest,
        spot_panel=evidence.spot_panel,
    )
    concordance = tuple(
        reconcile_policy_series(item, evidence.event_manifest) for item in evidence.series_manifests
    )
    qualification = qualify_candidate_b(
        series_manifests=evidence.series_manifests,
        event_manifest=evidence.event_manifest,
        concordance_results=concordance,
        spot_panel=evidence.spot_panel,
        formation_manifest=formation_manifest,
    )
    assert qualification.qualified is True
    spot_semantic = build_candidate_b_spot_semantic_evidence(
        spot_panel=evidence.spot_panel, pair_datasets=evidence.spot_datasets
    )
    lock = b"synthetic-lock"
    source = b"synthetic-measurement-source"
    environment = build_candidate_b_environment_evidence(
        measurement_implementation_commit="d" * 40,
        dependency_lock_bytes=lock,
        expected_dependency_lock_sha256=hashlib.sha256(lock).hexdigest(),
        measurement_source_bytes=source,
        expected_measurement_source_sha256=hashlib.sha256(source).hexdigest(),
    )
    bindings = build_candidate_b_qualified_run_bindings(
        static_contract=build_candidate_b_static_run_contract(),
        code_environment=environment,
        series_manifests=evidence.series_manifests,
        event_manifest=evidence.event_manifest,
        concordance_results=concordance,
        spot_panel=evidence.spot_panel,
        spot_semantic_evidence=spot_semantic,
        formation_manifest=formation_manifest,
        qualification_result=qualification,
    )
    assert bindings.qualification_passed is True
