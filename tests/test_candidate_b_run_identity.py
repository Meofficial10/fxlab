from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import fxlab.research.candidate_b_run_identity as run_identity

sys.path.insert(0, str(Path(__file__).parent))
from scripts.qualify_candidate_b_data import qualify_candidate_b  # noqa: E402

from fxlab.data.dukascopy_provider import DUKASCOPY_SYMBOLS  # noqa: E402
from fxlab.data.policy_rates import (  # noqa: E402
    CandidateBFormationManifest,
    PolicyEventManifest,
    SpotPanelManifestReference,
    build_series_manifest,
    canonical_sha256,
    reconcile_policy_series,
)
from fxlab.data.provider import (  # noqa: E402
    BarDataset,
    BarQuery,
    CanonicalInstrument,
    DataProvenance,
    ProvenanceQuality,
    bar_content_hash,
    dataset_identity,
)
from fxlab.research.candidate_b_run_identity import (  # noqa: E402
    CandidateBCodeEnvironmentEvidence,
    CandidateBQualifiedRunBindings,
    CandidateBRunIdentity,
    CandidateBSpotSemanticEvidence,
    CandidateBStaticRunContract,
    build_candidate_b_environment_evidence,
    build_candidate_b_qualified_run_bindings,
    build_candidate_b_spot_semantic_evidence,
    build_candidate_b_static_run_contract,
    finalize_candidate_b_run_identity,
)
from test_policy_rate_data import (  # noqa: E402
    csv_bytes,
    fully_bound_qualification_inputs,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
CURRENT_ADR_SHA256 = "a13f2a79f9890b99c388bd2e59ef1d8ab3a20fa4bffea1575df72dad10680603"


def test_spot_semantic_evidence_factory_is_required() -> None:
    assert hasattr(run_identity, "build_candidate_b_spot_semantic_evidence")


class _ExplodingSpotFrame:
    def __init__(self, index: pd.DatetimeIndex | None = None) -> None:
        self.accessed = False
        self.index = index

    def copy(self, *, deep: bool):
        self.accessed = True
        raise AssertionError("economic frame value accessed before sealed-window validation")


def _spot_dataset_with_exploding_frame(
    dataset: BarDataset,
    *,
    query: BarQuery,
    provenance: DataProvenance,
    sentinel: _ExplodingSpotFrame,
) -> BarDataset:
    result = object.__new__(BarDataset)
    object.__setattr__(result, "query", query)
    object.__setattr__(result, "provenance", provenance)
    object.__setattr__(result, "_frame", sentinel)
    return result


def test_spot_post_2023_query_bound_rejected_before_frame_access() -> None:
    *_, spots, _formations, _qualification, semantic = evidence_bundle()
    original = semantic.pair_datasets[0]
    end = datetime(2024, 1, 1, tzinfo=UTC)
    query = BarQuery(original.query.instrument, "D1", original.query.start, end, end)
    provenance = replace(
        original.provenance,
        query_end=end,
        query_as_of=end,
        query_fingerprint=query.fingerprint,
    )
    sentinel = _ExplodingSpotFrame()
    contaminated = _spot_dataset_with_exploding_frame(
        original, query=query, provenance=provenance, sentinel=sentinel
    )
    with pytest.raises(ValueError, match="sealed_window_violation"):
        build_candidate_b_spot_semantic_evidence(
            spot_panel=spots,
            pair_datasets=(contaminated,) + semantic.pair_datasets[1:],
        )
    assert not sentinel.accessed


def test_spot_post_2023_as_of_rejected_before_frame_access() -> None:
    *_, spots, _formations, _qualification, semantic = evidence_bundle()
    original = semantic.pair_datasets[0]
    as_of = datetime(2024, 1, 2, tzinfo=UTC)
    query = BarQuery(
        original.query.instrument,
        "D1",
        original.query.start,
        original.query.end,
        as_of,
    )
    provenance = replace(
        original.provenance,
        query_as_of=as_of,
        query_fingerprint=query.fingerprint,
    )
    sentinel = _ExplodingSpotFrame()
    contaminated = _spot_dataset_with_exploding_frame(
        original, query=query, provenance=provenance, sentinel=sentinel
    )
    with pytest.raises(ValueError, match="sealed_window_violation"):
        build_candidate_b_spot_semantic_evidence(
            spot_panel=spots,
            pair_datasets=(contaminated,) + semantic.pair_datasets[1:],
        )
    assert not sentinel.accessed


def test_spot_post_2023_actual_bound_rejected_before_frame_access() -> None:
    *_, spots, _formations, _qualification, semantic = evidence_bundle()
    original = semantic.pair_datasets[0]
    actual_last = datetime(2024, 1, 1, tzinfo=UTC)
    provenance = replace(original.provenance, actual_last_observation=actual_last)
    sentinel = _ExplodingSpotFrame()
    contaminated = _spot_dataset_with_exploding_frame(
        original,
        query=original.query,
        provenance=provenance,
        sentinel=sentinel,
    )
    with pytest.raises(ValueError, match="sealed_window_violation"):
        build_candidate_b_spot_semantic_evidence(
            spot_panel=spots,
            pair_datasets=(contaminated,) + semantic.pair_datasets[1:],
        )
    assert not sentinel.accessed


def test_spot_mixed_2023_2024_rows_fail_without_value_access_or_truncation() -> None:
    *_, spots, _formations, _qualification, semantic = evidence_bundle()
    original = semantic.pair_datasets[0]
    mixed_index = original.frame.index.append(pd.DatetimeIndex([datetime(2024, 1, 2, tzinfo=UTC)]))
    sentinel = _ExplodingSpotFrame(mixed_index)
    contaminated = _spot_dataset_with_exploding_frame(
        original,
        query=original.query,
        provenance=original.provenance,
        sentinel=sentinel,
    )
    with pytest.raises(ValueError, match="sealed_window_violation"):
        build_candidate_b_spot_semantic_evidence(
            spot_panel=spots,
            pair_datasets=(contaminated,) + semantic.pair_datasets[1:],
        )
    assert not sentinel.accessed


def test_spot_fully_pre_2024_dataset_remains_accepted() -> None:
    *_, spots, _formations, _qualification, semantic = evidence_bundle()
    rebuilt = build_candidate_b_spot_semantic_evidence(
        spot_panel=spots,
        pair_datasets=semantic.pair_datasets,
    )
    assert rebuilt.semantic_identity == semantic.semantic_identity


def _replace_spot_audit_manifest(bundle, manifest_id: str, retrieval_delta: timedelta):
    manifests, events, concordance, spots, formations, _qualification, semantic = bundle
    panel = SpotPanelManifestReference(manifest_id, spots.dataset_ids, spots.observations)
    changed_formations = CandidateBFormationManifest(
        tuple(
            replace(
                item,
                source_manifest_fingerprints=tuple(
                    manifest_id if value == spots.manifest_id else value
                    for value in item.source_manifest_fingerprints
                ),
            )
            for item in formations.formations
        )
    )
    datasets = tuple(
        BarDataset(
            item.query,
            item.frame,
            replace(
                item.provenance,
                retrieved_at=item.provenance.retrieved_at + retrieval_delta,
            ),
        )
        for item in semantic.pair_datasets
    )
    spot_semantic = build_candidate_b_spot_semantic_evidence(
        spot_panel=panel, pair_datasets=datasets
    )
    qualification = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=panel,
        formation_manifest=changed_formations,
    )
    return (
        manifests,
        events,
        concordance,
        panel,
        changed_formations,
        qualification,
        spot_semantic,
    )


def environment(**changes: object) -> CandidateBCodeEnvironmentEvidence:
    lock_bytes = changes.pop("lock_bytes", b"synthetic-lock-a")
    source_bytes = changes.pop("source_bytes", b"synthetic-source-a")
    commit = changes.pop("measurement_implementation_commit", "d" * 40)
    if changes:
        raise ValueError("unsupported synthetic environment override")
    assert isinstance(lock_bytes, bytes)
    assert isinstance(source_bytes, bytes)
    assert isinstance(commit, str)
    return build_candidate_b_environment_evidence(
        measurement_implementation_commit=commit,
        dependency_lock_bytes=lock_bytes,
        expected_dependency_lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
        measurement_source_bytes=source_bytes,
        expected_measurement_source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )


def _spot_provenance_and_panel(spots: SpotPanelManifestReference):
    datasets = []
    dataset_by_pair = {}
    for pair in ("AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"):
        references = tuple(item for item in spots.observations if item.pair == pair)
        first = min(item.bar_open for item in references)
        last = max(item.bar_open for item in references)
        end = max(item.bar_close for item in references)
        query = BarQuery(CanonicalInstrument(pair), "D1", first, end, end)
        frame = pd.DataFrame(
            {
                "open": [1.0] * len(references),
                "high": [1.0] * len(references),
                "low": [1.0] * len(references),
                "close": [1.0] * len(references),
                "volume": [0.0] * len(references),
            },
            index=pd.DatetimeIndex([item.bar_open for item in references]),
            dtype="float64",
        )
        frame.index.name = "ts_open"
        frame.attrs = {"symbol": pair, "timeframe": "D1"}
        content_hash = bar_content_hash(frame)
        dataset_id = dataset_identity("dukascopy", "1", query.fingerprint, content_hash)
        dataset_by_pair[pair] = dataset_id
        provenance = DataProvenance(
            provider_id="dukascopy",
            provider_version="1",
            normalization_version="dukascopy_bid_v1",
            canonical_symbol=pair,
            provider_symbol=DUKASCOPY_SYMBOLS[pair],
            timeframe="D1",
            query_start=first,
            query_end=end,
            query_as_of=end,
            retrieved_at=end + timedelta(days=1),
            actual_first_observation=first,
            actual_last_observation=last,
            row_count=len(references),
            content_hash=content_hash,
            query_fingerprint=query.fingerprint,
            dataset_id=dataset_id,
            revision="synthetic_revision_1",
            source_timezone="UTC",
            provenance_quality=ProvenanceQuality.VERIFIED,
            sanitized_source_reference="dukascopy:historical:bid",
        )
        datasets.append(BarDataset(query, frame, provenance))
    observations = tuple(
        replace(item, dataset_id=dataset_by_pair[item.pair]) for item in spots.observations
    )
    manifest_id = canonical_sha256(
        {"schema": "synthetic_spot_panel.v1", "datasets": dataset_by_pair}
    )
    panel = SpotPanelManifestReference(
        manifest_id,
        tuple(dataset_by_pair[pair] for pair in dataset_by_pair),
        observations,
    )
    return tuple(datasets), panel


def _tampered_dataset(dataset: BarDataset, provenance: DataProvenance) -> BarDataset:
    result = object.__new__(BarDataset)
    object.__setattr__(result, "query", dataset.query)
    object.__setattr__(result, "provenance", provenance)
    object.__setattr__(result, "_frame", dataset.frame)
    return result


def evidence_bundle():
    manifests, events, concordance, spots, formations = fully_bound_qualification_inputs()
    datasets, spots = _spot_provenance_and_panel(spots)
    old_spot_manifest_id = formations.formations[0].source_manifest_fingerprints[-1]
    formations = CandidateBFormationManifest(
        tuple(
            replace(
                formation,
                spot_observations=tuple(
                    next(
                        ref
                        for ref in spots.observations
                        if ref.pair == old.pair and ref.bar_open == old.bar_open
                    )
                    for old in formation.spot_observations
                ),
                source_manifest_fingerprints=tuple(
                    spots.manifest_id if item == old_spot_manifest_id else item
                    for item in formation.source_manifest_fingerprints
                ),
            )
            for formation in formations.formations
        )
    )
    qualification = qualify_candidate_b(
        formation_manifest=formations,
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=spots,
    )
    spot_semantic = build_candidate_b_spot_semantic_evidence(
        spot_panel=spots, pair_datasets=datasets
    )
    return manifests, events, concordance, spots, formations, qualification, spot_semantic


def build_bindings(*, env: CandidateBCodeEnvironmentEvidence | None = None, reverse=False):
    manifests, events, concordance, spots, formations, qualification, spot_semantic = (
        evidence_bundle()
    )
    if reverse:
        manifests = tuple(reversed(manifests))
        concordance = tuple(reversed(concordance))
    return build_candidate_b_qualified_run_bindings(
        static_contract=build_candidate_b_static_run_contract(),
        code_environment=env or environment(),
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=spots,
        spot_semantic_evidence=spot_semantic,
        formation_manifest=formations,
        qualification_result=qualification,
    )


def test_static_contract_is_zero_parameter_frozen_and_matches_current_adr() -> None:
    assert not tuple(
        __import__("inspect").signature(build_candidate_b_static_run_contract).parameters
    )
    contract = build_candidate_b_static_run_contract()
    adr = Path(
        "docs/adr/0006-r2-candidate-b-public-policy-rate-differential-preregistration.md"
    ).read_bytes()
    assert hashlib.sha256(adr).hexdigest() == CURRENT_ADR_SHA256
    assert contract.identity.adr_content_sha256 == CURRENT_ADR_SHA256
    assert contract.identity.clarification_commits == (
        "95f903fc8c50d1bb5c181e75beae5cff1a45629b",
        "eee70b30456c913dfa94e96d5b6cf3e470b9d4fe",
        "7d073e5d6a06e80ee1efd16f1b6a7b8053b93df4",
    )
    assert contract.formation.train_count == 83
    assert contract.formation.validation_count == 23
    assert contract.formation.measured_total == 106
    assert contract.formation.boundary_purge_count == 1
    assert contract.sealed_maximum_date.isoformat() == "2023-12-31"
    with pytest.raises(TypeError):
        type(contract)(identity=contract.identity)
    with pytest.raises((AttributeError, TypeError)):
        contract.formation.train_count = 84  # type: ignore[misc]


def test_static_contract_binds_all_frozen_measurement_categories() -> None:
    contract = build_candidate_b_static_run_contract()
    assert contract.universe.currencies == ("AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD")
    assert contract.portfolio.long_count == contract.portfolio.short_count == 2
    assert contract.portfolio.selected_weight == "0.25"
    assert contract.costs.stress_multiplier == "1.5"
    assert contract.statistics.hac_lag == 3
    assert contract.statistics.student_t_probability == "0.95"
    assert contract.statistics.bootstrap_replications == 10_000
    assert contract.statistics.bootstrap_seed == 20260829
    assert contract.statistics.bootstrap_rng == "numpy.random.Generator(PCG64)"
    assert contract.gates.minimum_validation_mean == "0.001"
    assert contract.gates.minimum_annualized_sharpe == "0.50"
    assert contract.gates.maximum_drawdown_exclusive == "0.20"
    assert contract.multiple_comparison_prohibition


def test_identical_evidence_and_mapping_order_produce_identical_run_id() -> None:
    static = build_candidate_b_static_run_contract()
    first = finalize_candidate_b_run_identity(static, build_bindings())
    second = finalize_candidate_b_run_identity(static, build_bindings(reverse=True))
    assert first.run_id == second.run_id


def test_changed_commit_dependency_or_environment_changes_run_id() -> None:
    static = build_candidate_b_static_run_contract()
    baseline = finalize_candidate_b_run_identity(static, build_bindings()).run_id
    variants = (
        environment(measurement_implementation_commit="e" * 40),
        environment(lock_bytes=b"synthetic-lock-b"),
        environment(source_bytes=b"synthetic-source-b"),
    )
    assert all(
        finalize_candidate_b_run_identity(static, build_bindings(env=item)).run_id != baseline
        for item in variants
    )
    with pytest.raises(ValueError):
        environment(rng_identity="numpy.random.Generator(PCG64DXSM)")


def test_bindings_are_derived_from_evidence_not_naked_hashes() -> None:
    signature = __import__("inspect").signature(build_candidate_b_qualified_run_bindings)
    assert "aggregate_bis_manifest_identity" not in signature.parameters
    assert "spot_panel_manifest_id" not in signature.parameters
    bindings = build_bindings()
    assert len(bindings.series_manifest_identities) == 8
    assert bindings.qualification_passed


def test_missing_or_failed_qualification_fails_closed() -> None:
    manifests, events, concordance, spots, formations, qualification, spot_semantic = (
        evidence_bundle()
    )
    with pytest.raises((TypeError, ValueError)):
        build_candidate_b_qualified_run_bindings(
            static_contract=build_candidate_b_static_run_contract(),
            code_environment=environment(),
            series_manifests=manifests[:-1],
            event_manifest=events,
            concordance_results=concordance,
            spot_panel=spots,
            spot_semantic_evidence=spot_semantic,
            formation_manifest=formations,
            qualification_result=qualification,
        )
    failed = replace(qualification, qualified=False, reasons=("synthetic_failure",))
    with pytest.raises(ValueError):
        build_candidate_b_qualified_run_bindings(
            static_contract=build_candidate_b_static_run_contract(),
            code_environment=environment(),
            series_manifests=manifests,
            event_manifest=events,
            concordance_results=concordance,
            spot_panel=spots,
            spot_semantic_evidence=spot_semantic,
            formation_manifest=formations,
            qualification_result=failed,
        )


def test_wrong_static_contract_and_missing_late_bound_fields_fail() -> None:
    static = build_candidate_b_static_run_contract()
    bindings = build_bindings()
    wrong = static
    object.__setattr__(wrong, "contract_id", SHA_B)
    with pytest.raises(ValueError):
        finalize_candidate_b_run_identity(wrong, bindings)
    with pytest.raises(ValueError):
        environment(lock_bytes=b"")


def test_binding_constructor_requires_verified_factory() -> None:
    with pytest.raises(TypeError):
        CandidateBQualifiedRunBindings(static_contract_id=SHA_A)


def test_results_wall_clock_paths_and_credentials_are_absent_from_identity_schema() -> None:
    static = build_candidate_b_static_run_contract()
    bindings = build_bindings()
    text = repr((static, bindings)).lower()
    for forbidden in (
        "result_return",
        "performance",
        "executed_at",
        "retrieval_time",
        "local_path",
        "credential",
        "invocation_id",
    ):
        assert forbidden not in text


def test_shifted_formation_months_fail_even_when_counts_match() -> None:
    manifests, events, concordance, spots, formations, qualification, spot_semantic = (
        evidence_bundle()
    )
    shifted_first = replace(formations.formations[0])
    object.__setattr__(shifted_first, "formation_month", "2014-12")
    shifted = replace(formations, formations=(shifted_first,) + formations.formations[1:])
    with pytest.raises(ValueError):
        build_candidate_b_qualified_run_bindings(
            static_contract=build_candidate_b_static_run_contract(),
            code_environment=environment(),
            series_manifests=manifests,
            event_manifest=events,
            concordance_results=concordance,
            spot_panel=spots,
            spot_semantic_evidence=spot_semantic,
            formation_manifest=shifted,
            qualification_result=qualification,
        )


def test_naked_hashes_and_arbitrary_final_identity_cannot_be_constructed() -> None:
    assert not hasattr(CandidateBStaticRunContract, "_from_values")
    assert not hasattr(CandidateBQualifiedRunBindings, "_from_values")
    assert not hasattr(CandidateBRunIdentity, "_from_values")
    with pytest.raises(TypeError):
        CandidateBStaticRunContract(static_contract_id="a" * 64)
    with pytest.raises(TypeError):
        CandidateBStaticRunContract()
    with pytest.raises(TypeError):
        CandidateBCodeEnvironmentEvidence()
    with pytest.raises(TypeError):
        CandidateBQualifiedRunBindings(static_contract_id="a" * 64)
    with pytest.raises(TypeError):
        CandidateBQualifiedRunBindings()
    with pytest.raises(TypeError):
        CandidateBRunIdentity(
            schema="candidate_b_measurement_run.v1",
            static_contract_id="a" * 64,
            bindings_id="b" * 64,
            run_id="c" * 64,
        )
    with pytest.raises(TypeError):
        CandidateBRunIdentity()


def test_static_contract_binds_exact_bis_and_pit_semantics() -> None:
    contract = build_candidate_b_static_run_contract()
    assert contract.bis.series == (
        ("AUD", "D.AU"),
        ("CAD", "D.CA"),
        ("CHF", "D.CH"),
        ("EUR", "D.XM"),
        ("GBP", "D.GB"),
        ("JPY", "D.JP"),
        ("NZD", "D.NZ"),
        ("USD", "D.US"),
    )
    assert (
        contract.bis.agency,
        contract.bis.dataflow,
        contract.bis.version,
        contract.bis.frequency,
    ) == ("BIS", "WS_CBPOL", "1.0", "D")
    assert contract.bis.request_start.isoformat() == "2014-01-01"
    assert contract.bis.request_end.isoformat() == "2023-12-31"
    assert contract.pit.announcement_and_effective_required
    assert contract.pit.future_fill_forbidden
    assert contract.pit.revision_leakage_forbidden
    assert contract.pit.initialization_history_only_before_first_formation


def test_environment_factory_derives_versions_and_rejects_hash_mismatch() -> None:
    lock_bytes = b"synthetic-lock"
    source_bytes = b"synthetic-measurement-source"
    lock_hash = hashlib.sha256(lock_bytes).hexdigest()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    evidence = build_candidate_b_environment_evidence(
        measurement_implementation_commit="d" * 40,
        dependency_lock_bytes=lock_bytes,
        expected_dependency_lock_sha256=lock_hash,
        measurement_source_bytes=source_bytes,
        expected_measurement_source_sha256=source_hash,
    )
    assert evidence.dependency_lock_sha256 == lock_hash
    assert evidence.measurement_source_sha256 == source_hash
    with pytest.raises(ValueError, match="dependency_lock_hash_mismatch"):
        build_candidate_b_environment_evidence(
            measurement_implementation_commit="d" * 40,
            dependency_lock_bytes=lock_bytes,
            expected_dependency_lock_sha256="a" * 64,
            measurement_source_bytes=source_bytes,
            expected_measurement_source_sha256=source_hash,
        )


def _rebuild_with_later_retrieval():
    manifests, events, _concordance, spots, formations, _qualification, spot_semantic = (
        evidence_bundle()
    )
    later_manifests = []
    for manifest in manifests:
        area = manifest.request.series.series_key.split(".", 1)[1]
        raw = csv_bytes(
            *(
                (
                    "D",
                    area,
                    observation.observation_date.isoformat(),
                    str(observation.value),
                    observation.status,
                )
                for observation in manifest.observations
            )
        )
        later_manifests.append(
            build_series_manifest(
                manifest.request,
                manifest.metadata,
                raw,
                manifest.retrieved_at + timedelta(days=1),
            )
        )
    later_events = PolicyEventManifest(
        tuple(
            replace(
                event,
                source=replace(
                    event.source, retrieved_at=event.source.retrieved_at + timedelta(days=1)
                ),
            )
            for event in events.events
        )
    )
    replacements = {
        **{
            old.manifest_id: new.manifest_id
            for old, new in zip(manifests, later_manifests, strict=True)
        },
        events.manifest_id: later_events.manifest_id,
    }
    later_formations = CandidateBFormationManifest(
        tuple(
            replace(
                formation,
                source_manifest_fingerprints=tuple(
                    replacements.get(item, item) for item in formation.source_manifest_fingerprints
                ),
            )
            for formation in formations.formations
        )
    )
    later_concordance = tuple(
        reconcile_policy_series(manifest, later_events) for manifest in later_manifests
    )
    later_qualification = qualify_candidate_b(
        series_manifests=tuple(later_manifests),
        event_manifest=later_events,
        concordance_results=later_concordance,
        spot_panel=spots,
        formation_manifest=later_formations,
    )
    return (
        tuple(later_manifests),
        later_events,
        later_concordance,
        spots,
        later_formations,
        later_qualification,
        spot_semantic,
    )


def _bindings_from_bundle(bundle):
    manifests, events, concordance, spots, formations, qualification, spot_semantic = bundle
    return build_candidate_b_qualified_run_bindings(
        static_contract=build_candidate_b_static_run_contract(),
        code_environment=environment(),
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=spots,
        spot_semantic_evidence=spot_semantic,
        formation_manifest=formations,
        qualification_result=qualification,
    )


def test_retrieval_time_changes_audit_identity_but_not_semantic_run_id() -> None:
    static = build_candidate_b_static_run_contract()
    original = build_bindings()
    later = _bindings_from_bundle(_rebuild_with_later_retrieval())
    assert original.audit_evidence_identity != later.audit_evidence_identity
    assert original.bindings_id == later.bindings_id
    assert (
        finalize_candidate_b_run_identity(static, original).run_id
        == finalize_candidate_b_run_identity(static, later).run_id
    )


def test_spot_retrieval_audit_change_does_not_change_semantic_run_id() -> None:
    original_bundle = evidence_bundle()
    changed_bundle = _replace_spot_audit_manifest(original_bundle, "e" * 64, timedelta(days=7))
    original = _bindings_from_bundle(original_bundle)
    changed = _bindings_from_bundle(changed_bundle)
    static = build_candidate_b_static_run_contract()
    assert original.spot_semantic_audit_identity != changed.spot_semantic_audit_identity
    assert original.spot_panel_manifest_identity == changed.spot_panel_manifest_identity
    assert original.bindings_id == changed.bindings_id
    assert (
        finalize_candidate_b_run_identity(static, original).run_id
        == finalize_candidate_b_run_identity(static, changed).run_id
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider_id", "other"),
        ("provider_version", "2"),
        ("normalization_version", "dukascopy_mid_v1"),
        ("provider_symbol", "AUDUSD"),
        ("timeframe", "H4"),
        ("source_timezone", "Europe/Zurich"),
        ("sanitized_source_reference", "dukascopy:historical:ask"),
    ),
)
def test_spot_semantic_changes_fail_closed(field: str, value: str) -> None:
    *_, spots, _formations, _qualification, semantic = evidence_bundle()
    first = semantic.pair_datasets[0]
    changed = (
        _tampered_dataset(first, replace(first.provenance, **{field: value})),
    ) + semantic.pair_datasets[1:]
    with pytest.raises(ValueError, match="dataset evidence|semantic provenance"):
        build_candidate_b_spot_semantic_evidence(spot_panel=spots, pair_datasets=changed)


def test_spot_semantic_evidence_rejects_legacy_missing_duplicate_and_naked_hashes() -> None:
    *_, spots, _formations, _qualification, semantic = evidence_bundle()
    legacy = semantic.pair_datasets[0]
    with pytest.raises(ValueError, match="semantic provenance"):
        build_candidate_b_spot_semantic_evidence(
            spot_panel=spots,
            pair_datasets=(
                BarDataset(
                    legacy.query,
                    legacy.frame,
                    replace(
                        legacy.provenance,
                        provenance_quality=ProvenanceQuality.LEGACY_UNVERIFIED,
                    ),
                ),
            )
            + semantic.pair_datasets[1:],
        )
    with pytest.raises(ValueError, match="exactly seven"):
        build_candidate_b_spot_semantic_evidence(
            spot_panel=spots, pair_datasets=semantic.pair_datasets[:-1]
        )
    with pytest.raises(ValueError, match="exact Candidate B pair"):
        build_candidate_b_spot_semantic_evidence(
            spot_panel=spots,
            pair_datasets=semantic.pair_datasets[:-1] + (semantic.pair_datasets[0],),
        )
    with pytest.raises(TypeError):
        CandidateBSpotSemanticEvidence(
            semantic_identity=SHA_A,
            audit_identity=SHA_B,
        )


def test_spot_semantic_evidence_tampering_and_panel_mismatch_are_revalidated() -> None:
    bundle = evidence_bundle()
    manifests, events, concordance, spots, formations, qualification, semantic = bundle
    object.__setattr__(semantic, "mapping_fingerprint", SHA_B)
    with pytest.raises(ValueError, match="does not validate"):
        build_candidate_b_qualified_run_bindings(
            static_contract=build_candidate_b_static_run_contract(),
            code_environment=environment(),
            series_manifests=manifests,
            event_manifest=events,
            concordance_results=concordance,
            spot_panel=spots,
            spot_semantic_evidence=semantic,
            formation_manifest=formations,
            qualification_result=qualification,
        )
    clean = evidence_bundle()
    changed = _replace_spot_audit_manifest(clean, "f" * 64, timedelta(days=1))
    with pytest.raises(ValueError, match="does not validate"):
        build_candidate_b_qualified_run_bindings(
            static_contract=build_candidate_b_static_run_contract(),
            code_environment=environment(),
            series_manifests=clean[0],
            event_manifest=clean[1],
            concordance_results=clean[2],
            spot_panel=changed[3],
            spot_semantic_evidence=clean[6],
            formation_manifest=changed[4],
            qualification_result=changed[5],
        )
    semantic = clean[6]
    object.__setattr__(semantic, "d1_construction_identity", SHA_B)
    with pytest.raises(ValueError, match="does not validate"):
        build_candidate_b_qualified_run_bindings(
            static_contract=build_candidate_b_static_run_contract(),
            code_environment=environment(),
            series_manifests=clean[0],
            event_manifest=clean[1],
            concordance_results=clean[2],
            spot_panel=clean[3],
            spot_semantic_evidence=semantic,
            formation_manifest=clean[4],
            qualification_result=clean[5],
        )


def test_missing_spot_semantic_evidence_cannot_finalize() -> None:
    manifests, events, concordance, spots, formations, qualification, _semantic = evidence_bundle()
    with pytest.raises(TypeError):
        build_candidate_b_qualified_run_bindings(  # type: ignore[call-arg]
            static_contract=build_candidate_b_static_run_contract(),
            code_environment=environment(),
            series_manifests=manifests,
            event_manifest=events,
            concordance_results=concordance,
            spot_panel=spots,
            formation_manifest=formations,
            qualification_result=qualification,
        )


def test_changed_semantic_spot_content_changes_run_id() -> None:
    manifests, events, concordance, spots, formations, _qualification, spot_semantic = (
        evidence_bundle()
    )
    old_dataset = spot_semantic.pair_datasets[0]
    old = old_dataset.provenance
    changed_frame = old_dataset.frame
    changed_frame.iloc[0, changed_frame.columns.get_loc("close")] = 1.01
    changed_frame.iloc[0, changed_frame.columns.get_loc("high")] = 1.01
    content_hash = bar_content_hash(changed_frame)
    dataset_id = dataset_identity(
        old.provider_id, old.provider_version, old.query_fingerprint, content_hash
    )
    changed_datasets = (
        BarDataset(
            old_dataset.query,
            changed_frame,
            replace(old, content_hash=content_hash, dataset_id=dataset_id),
        ),
    ) + spot_semantic.pair_datasets[1:]
    dataset_ids = (dataset_id,) + spots.dataset_ids[1:]
    observations = tuple(
        replace(item, dataset_id=dataset_id) if item.pair == "AUDUSD" else item
        for item in spots.observations
    )
    changed_manifest_id = canonical_sha256(
        {"schema": "synthetic_spot_panel.v1", "datasets": dataset_ids}
    )
    changed_spots = SpotPanelManifestReference(changed_manifest_id, dataset_ids, observations)
    changed_formations = CandidateBFormationManifest(
        tuple(
            replace(
                formation,
                spot_observations=tuple(
                    replace(item, dataset_id=dataset_id) if item.pair == "AUDUSD" else item
                    for item in formation.spot_observations
                ),
                source_manifest_fingerprints=tuple(
                    changed_manifest_id if item == spots.manifest_id else item
                    for item in formation.source_manifest_fingerprints
                ),
            )
            for formation in formations.formations
        )
    )
    changed_qualification = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=changed_spots,
        formation_manifest=changed_formations,
    )
    changed_semantic = build_candidate_b_spot_semantic_evidence(
        spot_panel=changed_spots, pair_datasets=changed_datasets
    )
    changed = _bindings_from_bundle(
        (
            manifests,
            events,
            concordance,
            changed_spots,
            changed_formations,
            changed_qualification,
            changed_semantic,
        )
    )
    static = build_candidate_b_static_run_contract()
    assert (
        finalize_candidate_b_run_identity(static, changed).run_id
        != finalize_candidate_b_run_identity(static, build_bindings()).run_id
    )


def test_finalization_revalidates_tampered_binding_and_environment() -> None:
    static = build_candidate_b_static_run_contract()
    bindings = build_bindings()
    object.__setattr__(bindings, "series_manifest_identities", ())
    with pytest.raises(ValueError):
        finalize_candidate_b_run_identity(static, bindings)
    bindings = build_bindings()
    object.__setattr__(bindings.code_environment, "numpy_version", "0.0.0")
    with pytest.raises(ValueError):
        finalize_candidate_b_run_identity(static, bindings)
    bindings = build_bindings()
    object.__setattr__(bindings, "audit_evidence_identity", "f" * 64)
    with pytest.raises(ValueError):
        finalize_candidate_b_run_identity(static, bindings)


def test_static_contract_fields_are_revalidated_not_only_its_stored_id() -> None:
    static = build_candidate_b_static_run_contract()
    object.__setattr__(static.bis, "request_start", static.bis.request_end)
    manifests, events, concordance, spots, formations, qualification, spot_semantic = (
        evidence_bundle()
    )
    with pytest.raises(ValueError):
        build_candidate_b_qualified_run_bindings(
            static_contract=static,
            code_environment=environment(),
            series_manifests=manifests,
            event_manifest=events,
            concordance_results=concordance,
            spot_panel=spots,
            spot_semantic_evidence=spot_semantic,
            formation_manifest=formations,
            qualification_result=qualification,
        )
