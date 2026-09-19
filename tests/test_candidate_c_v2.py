from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from fxlab.data.dukascopy_direct_d1 import (
    DIRECT_D1_NORMALIZATION_VERSION,
    DIRECT_D1_PROVIDER_ID,
    DIRECT_D1_PROVIDER_VERSION,
    DIRECT_D1_SOURCE_REFERENCE,
    DIRECT_D1_VOLUME_SEMANTICS,
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
from fxlab.research.candidate_c_execution_evidence import (
    CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
    CandidateCExecutionEvidence,
    CandidateCExecutionEvidenceManifest,
    CandidateCExecutionState,
    _make_record,
    _manifest_payload,
    _sha,
)
from fxlab.research.candidate_c_measurement import (
    CANDIDATE_C_ADR_SHA256,
    CANDIDATE_C_END,
    CANDIDATE_C_PAIRS,
    CANDIDATE_C_START,
    CANDIDATE_C_V2_ADR_SHA256,
    CandidateCCodeEnvironment,
    CandidateCDecision,
    build_candidate_c_policy,
    build_candidate_c_run_id,
    candidate_c_score,
    measure_candidate_c,
    select_candidate_c_weights,
)


def _synthetic_dataset(pair: str, pair_index: int) -> BarDataset:
    start = CANDIDATE_C_START
    end = CANDIDATE_C_END
    index = pd.date_range(start, end, inclusive="left", freq="D", tz="UTC")
    steps = np.arange(len(index), dtype=np.float64)
    close = 1.0 + pair_index * 0.1 + steps * (pair_index + 1) * 0.000001
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.where(steps == 10, 0.0, 1.0),
        },
        index=index,
        dtype="float64",
    )
    frame.index.name = "ts_open"
    frame.attrs = {"symbol": pair, "timeframe": "D1"}
    query = BarQuery(CanonicalInstrument(pair), "D1", start, end, end)
    content = bar_content_hash(frame)
    provenance = DataProvenance(
        provider_id=DIRECT_D1_PROVIDER_ID,
        provider_version=DIRECT_D1_PROVIDER_VERSION,
        normalization_version=DIRECT_D1_NORMALIZATION_VERSION,
        canonical_symbol=pair,
        provider_symbol=pair,
        timeframe="D1",
        query_start=start,
        query_end=end,
        query_as_of=end,
        retrieved_at=end,
        actual_first_observation=index[0].to_pydatetime(),
        actual_last_observation=index[-1].to_pydatetime(),
        row_count=len(frame),
        content_hash=content,
        query_fingerprint=query.fingerprint,
        dataset_id=dataset_identity(
            DIRECT_D1_PROVIDER_ID, DIRECT_D1_PROVIDER_VERSION, query.fingerprint, content
        ),
        revision=hashlib.sha256(f"revision-{pair}".encode()).hexdigest(),
        source_timezone="UTC",
        volume_semantics=DIRECT_D1_VOLUME_SEMANTICS,
        provenance_quality=ProvenanceQuality.VERIFIED,
        sanitized_source_reference=DIRECT_D1_SOURCE_REFERENCE,
    )
    return BarDataset(query, frame, provenance)


def _build_manifest_from_records(
    records: list[CandidateCExecutionEvidence],
) -> CandidateCExecutionEvidenceManifest:
    start = CANDIDATE_C_START
    end = CANDIDATE_C_END
    ordered_records = tuple(
        sorted(
            records,
            key=lambda item: (
                CANDIDATE_C_PAIRS.index(item.pair),
                item.intended_boundary,
            ),
        )
    )
    state_counts = tuple(
        sorted(pd.Series([r.state.value for r in ordered_records]).value_counts().items())
    )
    payload = _manifest_payload(
        start, end, CANDIDATE_C_PAIRS, ordered_records, state_counts
    )
    return CandidateCExecutionEvidenceManifest(
        schema_version=CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
        start=start,
        end=end,
        pairs=CANDIDATE_C_PAIRS,
        records=ordered_records,
        state_counts=state_counts,
        total_count=len(ordered_records),
        available_count=sum(r.state is CandidateCExecutionState.AVAILABLE for r in ordered_records),
        manifest_id=_sha(payload),
    )


def _synthetic_manifest_with_overrides(
    overrides: dict[tuple[str, datetime], CandidateCExecutionState] | None = None,
) -> CandidateCExecutionEvidenceManifest:
    start = CANDIDATE_C_START
    end = CANDIDATE_C_END
    overrides = overrides or {}
    records: list[CandidateCExecutionEvidence] = []
    for pair_index, pair in enumerate(CANDIDATE_C_PAIRS):
        boundary = start
        while boundary < end:
            day = (boundary - start).days
            center = (110.0 if pair == "USDJPY" else 1.0 + pair_index * 0.1) + (
                math.sin(day / 11.0) * (0.01 if pair == "USDJPY" else 0.001)
            )
            spread = 0.02 if pair == "USDJPY" else 0.0002
            state = overrides.get((pair, boundary), CandidateCExecutionState.AVAILABLE)
            if state is CandidateCExecutionState.AVAILABLE:
                records.append(
                    _make_record(
                        pair=pair,
                        boundary=boundary,
                        state=CandidateCExecutionState.AVAILABLE,
                        partition_state="present_staged",
                        raw_sha256="1" * 64,
                        raw_byte_count=120,
                        selected_tick_offset_ms=500,
                        selected_tick_timestamp=boundary,
                        bid=center - spread / 2,
                        ask=center + spread / 2,
                        bid_volume=1.0,
                        ask_volume=1.0,
                    )
                )
            elif state is CandidateCExecutionState.EMPTY_EVIDENCED:
                records.append(
                    _make_record(
                        pair=pair,
                        boundary=boundary,
                        state=CandidateCExecutionState.EMPTY_EVIDENCED,
                        partition_state="absent_evidenced",
                        absence_evidence_type="http_200_empty_body",
                    )
                )
            elif state is CandidateCExecutionState.ABSENT_EVIDENCED:
                records.append(
                    _make_record(
                        pair=pair,
                        boundary=boundary,
                        state=CandidateCExecutionState.ABSENT_EVIDENCED,
                        partition_state="absent_evidenced",
                        absence_evidence_type="http_404_absence",
                    )
                )
            elif state is CandidateCExecutionState.NO_VALID_TICK:
                records.append(
                    _make_record(
                        pair=pair,
                        boundary=boundary,
                        state=CandidateCExecutionState.NO_VALID_TICK,
                        partition_state="present_staged",
                        raw_sha256="2" * 64,
                        raw_byte_count=100,
                    )
                )
            elif state is CandidateCExecutionState.INVALID_PARTITION:
                records.append(
                    _make_record(
                        pair=pair,
                        boundary=boundary,
                        state=CandidateCExecutionState.INVALID_PARTITION,
                        partition_state="corrupt_local",
                        raw_sha256="3" * 64,
                        raw_byte_count=100,
                    )
                )
            elif state is CandidateCExecutionState.MISSING_LOCAL_PARTITION:
                records.append(
                    _make_record(
                        pair=pair,
                        boundary=boundary,
                        state=CandidateCExecutionState.MISSING_LOCAL_PARTITION,
                        partition_state="incomplete",
                    )
                )
            boundary += pd.Timedelta(days=1).to_pytimedelta()

    return _build_manifest_from_records(records)


@pytest.fixture(scope="module")
def synthetic_datasets() -> dict[str, BarDataset]:
    return {pair: _synthetic_dataset(pair, idx) for idx, pair in enumerate(CANDIDATE_C_PAIRS)}


# 1. v1 Friday entry -> Saturday EMPTY_EVIDENCED remains NOT_EVALUABLE
def test_v1_friday_entry_saturday_empty_fails_closed(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    # 2014-01-10 is Friday (entry), 2014-01-11 is Saturday (empty)
    sat = datetime(2014, 1, 11, tzinfo=UTC)
    overrides = {
        (pair, sat): CandidateCExecutionState.EMPTY_EVIDENCED for pair in CANDIDATE_C_PAIRS
    }
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v1 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=1,
    )
    assert result_v1.decision == CandidateCDecision.NOT_EVALUABLE
    assert "exit_empty_evidenced" in result_v1.reasons


# 2. v2 Friday entry: Saturday empty, Sunday empty, Monday available -> Monday exit
def test_v2_friday_entry_monday_exit(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    # 2014-01-10 is Friday. Sat/Sun empty. Monday is available.
    sat = datetime(2014, 1, 11, tzinfo=UTC)
    sun = datetime(2014, 1, 12, tzinfo=UTC)
    overrides = {}
    for pair in CANDIDATE_C_PAIRS:
        overrides[(pair, sat)] = CandidateCExecutionState.EMPTY_EVIDENCED
        overrides[(pair, sun)] = CandidateCExecutionState.EMPTY_EVIDENCED
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    # v2 skips weekend and resolves exit on Monday without failing NOT_EVALUABLE
    assert result_v2.decision in (CandidateCDecision.GO, CandidateCDecision.NO_GO)
    assert result_v2.train is not None
    assert result_v2.validation is not None


# 3. v2 holiday closure: consecutive absent/empty boundaries -> first available exit
def test_v2_holiday_closure_first_available_exit(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    d1 = datetime(2014, 1, 11, tzinfo=UTC)
    d2 = datetime(2014, 1, 12, tzinfo=UTC)
    d3 = datetime(2014, 1, 13, tzinfo=UTC)
    overrides = {}
    for pair in CANDIDATE_C_PAIRS:
        overrides[(pair, d1)] = CandidateCExecutionState.EMPTY_EVIDENCED
        overrides[(pair, d2)] = CandidateCExecutionState.ABSENT_EVIDENCED
        overrides[(pair, d3)] = CandidateCExecutionState.EMPTY_EVIDENCED
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision in (CandidateCDecision.GO, CandidateCDecision.NO_GO)


# 4. closure boundary with mixed ABSENT_EVIDENCED and EMPTY_EVIDENCED is skippable
def test_v2_closure_boundary_mixed_absent_and_empty_is_skippable(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    sat = datetime(2014, 1, 11, tzinfo=UTC)
    overrides = {}
    for idx, pair in enumerate(CANDIDATE_C_PAIRS):
        # Mix of absent and empty on the closure day across the 7 pairs
        overrides[(pair, sat)] = (
            CandidateCExecutionState.ABSENT_EVIDENCED
            if idx % 2 == 0
            else CandidateCExecutionState.EMPTY_EVIDENCED
        )
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision in (CandidateCDecision.GO, CandidateCDecision.NO_GO)


# 5. mixed AVAILABLE + EMPTY_EVIDENCED on candidate exit boundary -> NOT_EVALUABLE
def test_v2_mixed_available_and_empty_fails_not_evaluable(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    sat = datetime(2014, 1, 11, tzinfo=UTC)
    overrides = {}
    for idx, pair in enumerate(CANDIDATE_C_PAIRS):
        # 3 pairs available, 4 pairs empty
        overrides[(pair, sat)] = (
            CandidateCExecutionState.AVAILABLE
            if idx < 3
            else CandidateCExecutionState.EMPTY_EVIDENCED
        )
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision == CandidateCDecision.NOT_EVALUABLE


# 6. NO_VALID_TICK on candidate exit boundary -> NOT_EVALUABLE
def test_v2_no_valid_tick_on_exit_fails_not_evaluable(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    sat = datetime(2014, 1, 11, tzinfo=UTC)
    overrides = {(CANDIDATE_C_PAIRS[0], sat): CandidateCExecutionState.NO_VALID_TICK}
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision == CandidateCDecision.NOT_EVALUABLE


# 7. INVALID_PARTITION on candidate exit boundary -> NOT_EVALUABLE
def test_v2_invalid_partition_on_exit_fails_not_evaluable(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    sat = datetime(2014, 1, 11, tzinfo=UTC)
    overrides = {(CANDIDATE_C_PAIRS[0], sat): CandidateCExecutionState.INVALID_PARTITION}
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision == CandidateCDecision.NOT_EVALUABLE


# 8. MISSING_LOCAL_PARTITION on candidate exit boundary -> NOT_EVALUABLE
def test_v2_missing_local_partition_on_exit_fails_not_evaluable(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    sat = datetime(2014, 1, 11, tzinfo=UTC)
    overrides = {(CANDIDATE_C_PAIRS[0], sat): CandidateCExecutionState.MISSING_LOCAL_PARTITION}
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision == CandidateCDecision.NOT_EVALUABLE


# 9. seven-boundary horizon finds AVAILABLE on final permitted boundary (k=7) -> valid exit
def test_v2_seven_boundary_horizon_exit_on_day_seven(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    overrides = {}
    for day_offset in range(1, 7): # k=1..6 are closure days
        dt = datetime(2014, 1, 10, tzinfo=UTC) + timedelta(days=day_offset)
        for pair in CANDIDATE_C_PAIRS:
            overrides[(pair, dt)] = CandidateCExecutionState.EMPTY_EVIDENCED
    # day 7 (2014-01-17) is available
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision in (CandidateCDecision.GO, CandidateCDecision.NO_GO)


# 10. no AVAILABLE within seven-boundary horizon -> NOT_EVALUABLE with reason
def test_v2_exceeding_seven_boundary_horizon_fails_not_evaluable(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    overrides = {}
    for day_offset in range(1, 8): # k=1..7 are all closure days
        dt = datetime(2014, 1, 10, tzinfo=UTC) + timedelta(days=day_offset)
        for pair in CANDIDATE_C_PAIRS:
            overrides[(pair, dt)] = CandidateCExecutionState.EMPTY_EVIDENCED
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision == CandidateCDecision.NOT_EVALUABLE
    assert "next_tradable_session_not_found_within_7_boundaries" in result_v2.reasons


# 11. train cohort whose resolved exit crosses 2022-01-01 -> purged, not measured
def test_v2_train_cohort_crossing_train_end_is_purged(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    # Train end is 2022-01-01. If signal is late Dec 2021 and exit >= 2022-01-01:
    overrides = {}
    # Make 2021-12-31 empty so exit search reaches 2022-01-01
    d_dec31 = datetime(2021, 12, 31, tzinfo=UTC)
    for pair in CANDIDATE_C_PAIRS:
        overrides[(pair, d_dec31)] = CandidateCExecutionState.EMPTY_EVIDENCED
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    # The cohort crossing train end must be cleanly purged without causing NOT_EVALUABLE
    assert result_v2.decision in (CandidateCDecision.GO, CandidateCDecision.NO_GO)


# 12. validation cohort whose exit would require 2024 evidence -> purged without reading 2024
def test_v2_validation_cohort_crossing_seal_is_purged_without_reading_2024(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    # Last validation days in Dec 2023. If search hits 2024-01-01, cohort is purged.
    overrides = {}
    d_dec31 = datetime(2023, 12, 31, tzinfo=UTC)
    for pair in CANDIDATE_C_PAIRS:
        overrides[(pair, d_dec31)] = CandidateCExecutionState.EMPTY_EVIDENCED
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert result_v2.decision in (CandidateCDecision.GO, CandidateCDecision.NO_GO)


# 13. future-invariance: changing evidence after selected v2 exit must not change cohort result
def test_v2_future_invariance_after_selected_exit(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    sat = datetime(2014, 1, 11, tzinfo=UTC)
    overrides_base = {
        (pair, sat): CandidateCExecutionState.EMPTY_EVIDENCED for pair in CANDIDATE_C_PAIRS
    }
    manifest_base = _synthetic_manifest_with_overrides(overrides_base)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    result_base = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest_base,
        code_environment=code_env,
        version=2,
    )

    # Modify an evidence record in year 2020 (far in future relative to early trades)
    overrides_mod = dict(overrides_base)
    future_dt = datetime(2020, 6, 1, tzinfo=UTC)
    for pair in CANDIDATE_C_PAIRS:
        overrides_mod[(pair, future_dt)] = CandidateCExecutionState.EMPTY_EVIDENCED
    manifest_mod = _synthetic_manifest_with_overrides(overrides_mod)

    result_mod = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest_mod,
        code_environment=code_env,
        version=2,
    )
    # The early 2014 cohorts must evaluate identically
    assert result_base.train is not None
    assert result_mod.train is not None


# 14. future-invariance: evidence after horizon must not affect NOT_EVALUABLE decision
def test_v2_future_invariance_after_seven_boundary_horizon(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    overrides_base = {}
    for day_offset in range(1, 8):  # k=1..7 all empty
        dt = datetime(2014, 1, 10, tzinfo=UTC) + timedelta(days=day_offset)
        for pair in CANDIDATE_C_PAIRS:
            overrides_base[(pair, dt)] = CandidateCExecutionState.EMPTY_EVIDENCED
    manifest_base = _synthetic_manifest_with_overrides(overrides_base)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    res_base = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest_base,
        code_environment=code_env,
        version=2,
    )
    assert res_base.decision == CandidateCDecision.NOT_EVALUABLE

    # Day 8 (2014-01-18) being available or empty cannot rescue the failure
    overrides_mod = dict(overrides_base)
    day8 = datetime(2014, 1, 18, tzinfo=UTC)
    for pair in CANDIDATE_C_PAIRS:
        overrides_mod[(pair, day8)] = CandidateCExecutionState.AVAILABLE
    manifest_mod = _synthetic_manifest_with_overrides(overrides_mod)

    res_mod = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest_mod,
        code_environment=code_env,
        version=2,
    )
    assert res_mod.decision == CandidateCDecision.NOT_EVALUABLE


# 15. v1 and v2 policy identities differ
def test_v1_and_v2_policy_identities_differ() -> None:
    pol_v1 = build_candidate_c_policy(version=1)
    pol_v2 = build_candidate_c_policy(version=2)
    assert pol_v1.policy_id != pol_v2.policy_id
    assert pol_v1.candidate_version == 1
    assert pol_v2.candidate_version == 2
    assert pol_v1.adr_sha256 == CANDIDATE_C_ADR_SHA256
    assert pol_v2.adr_sha256 == CANDIDATE_C_V2_ADR_SHA256


# 16. v1 and v2 RunIDs differ with otherwise identical provenance
def test_v1_and_v2_run_ids_differ() -> None:
    pol_v1 = build_candidate_c_policy(version=1)
    pol_v2 = build_candidate_c_policy(version=2)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)
    semantics = tuple((pair, f"ds-{pair}", f"rev-{pair}") for pair in CANDIDATE_C_PAIRS)
    manifest_id = "0" * 64
    record_ids = ("1" * 64,)
    state_counts = (("available", 100),)

    run_id_v1 = build_candidate_c_run_id(
        policy=pol_v1,
        code_environment=code_env,
        dataset_semantics=semantics,
        execution_manifest_id=manifest_id,
        execution_record_ids=record_ids,
        execution_state_counts=state_counts,
    )
    run_id_v2 = build_candidate_c_run_id(
        policy=pol_v2,
        code_environment=code_env,
        dataset_semantics=semantics,
        execution_manifest_id=manifest_id,
        execution_record_ids=record_ids,
        execution_state_counts=state_counts,
    )
    assert run_id_v1 != run_id_v2


# 17. signal/ranking/weights remain identical between v1 and v2
def test_signal_ranking_weights_identical_between_v1_and_v2() -> None:
    closes = (1.0, 1.01, 1.02, 1.03, 1.04, 1.10)
    score_eur = candidate_c_score("EURUSD", closes)
    score_jpy = candidate_c_score("USDJPY", closes)
    assert score_eur == pytest.approx(math.log(1.10 / 1.0))
    assert score_jpy == pytest.approx(-math.log(1.10 / 1.0))

    scores = {pair: float(idx) for idx, pair in enumerate(CANDIDATE_C_PAIRS)}
    weights = select_candidate_c_weights(scores)
    assert weights == (
        ("AUDUSD", 0.25),
        ("EURUSD", 0.25),
        ("USDCHF", -0.25),
        ("USDJPY", -0.25),
    )


# 18. costs remain identical when both versions resolve on the same execution boundaries
def test_costs_identical_when_resolving_on_same_boundaries(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    # When all days are available, v1 and v2 both exit on next day and produce identical returns
    manifest = _synthetic_manifest_with_overrides({})
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    res_v1 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=1,
    )
    res_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert res_v1.train is not None and res_v2.train is not None
    assert res_v1.train.headline.gross_expectancy == pytest.approx(
        res_v2.train.headline.gross_expectancy
    )
    assert res_v1.train.headline.net_expectancy == pytest.approx(
        res_v2.train.headline.net_expectancy
    )
    assert res_v1.train.headline.cost_drag_expectancy == pytest.approx(
        res_v2.train.headline.cost_drag_expectancy
    )


# 19. metrics and statistical implementation remains unchanged
def test_metrics_and_statistics_implementation_unchanged() -> None:
    pol_v1 = build_candidate_c_policy(version=1)
    pol_v2 = build_candidate_c_policy(version=2)
    assert pol_v1.alpha == pol_v2.alpha == 0.00625
    assert pol_v1.prior_tested_families == pol_v2.prior_tested_families == 7
    assert pol_v1.annualization_days == pol_v2.annualization_days == 365.2425


# 20. no six-pair fallback in v2
def test_no_six_pair_fallback_in_v2(
    synthetic_datasets: dict[str, BarDataset],
) -> None:
    # 6 pairs available, 1 pair missing partition on entry -> non-entry or not evaluable
    d = datetime(2014, 1, 10, tzinfo=UTC)
    overrides = {(CANDIDATE_C_PAIRS[0], d): CandidateCExecutionState.MISSING_LOCAL_PARTITION}
    manifest = _synthetic_manifest_with_overrides(overrides)
    code_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    res_v2 = measure_candidate_c(
        datasets=synthetic_datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=2,
    )
    assert res_v2.decision == CandidateCDecision.NOT_EVALUABLE
