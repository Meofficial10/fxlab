from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    CandidateCExecutionEvidenceManifest,
    CandidateCExecutionState,
    _make_record,
    _manifest_payload,
    _sha,
)
from fxlab.research.candidate_c_measurement import (
    CANDIDATE_C_ADR_SHA256,
    CANDIDATE_C_PAIRS,
    CandidateCCodeEnvironment,
    CandidateCDecision,
    CandidateCEntryDisposition,
    CandidateCMetrics,
    CandidateCSplitResult,
    build_candidate_c_policy,
    build_candidate_c_run_id,
    candidate_c_entry_disposition,
    candidate_c_fill,
    candidate_c_leg_accounting,
    candidate_c_newey_west,
    candidate_c_score,
    canonical_candidate_c_result,
    compute_candidate_c_metrics,
    decide_candidate_c,
    measure_candidate_c,
    select_candidate_c_weights,
    validate_candidate_c_signal_boundary,
)


def _positive_metrics() -> CandidateCMetrics:
    return CandidateCMetrics(
        trade_count=40,
        cohort_count=10,
        non_entry_reason_counts=(),
        gross_expectancy=0.003,
        net_expectancy=0.002,
        annualized_return=0.10,
        sharpe=1.0,
        max_drawdown=0.05,
        cost_drag_expectancy=0.001,
        cost_drag_annualized=0.02,
    )


def _positive_split() -> CandidateCSplitResult:
    metrics = _positive_metrics()
    return CandidateCSplitResult(
        headline=metrics,
        stress=replace(metrics, net_expectancy=0.001, annualized_return=0.05, sharpe=0.5),
        headline_lcb=0.0002,
        stress_lcb=0.0001,
        yearly_headline=((2022, 0.001), (2023, 0.002)),
        yearly_stress=((2022, 0.0005), (2023, 0.001)),
        pair_headline=tuple((pair, 0.001) for pair in CANDIDATE_C_PAIRS),
        pair_stress=tuple((pair, 0.0005) for pair in CANDIDATE_C_PAIRS),
    )


def test_five_bar_score_and_inverse_orientation_require_six_closes() -> None:
    closes = (1.0, 1.01, 1.02, 1.03, 1.04, 1.10)
    expected = math.log(1.10 / 1.0)
    assert candidate_c_score("EURUSD", closes) == pytest.approx(expected)
    assert candidate_c_score("USDJPY", closes) == pytest.approx(-expected)
    with pytest.raises(ValueError, match="six"):
        candidate_c_score("EURUSD", closes[-5:])


def test_selection_is_low_two_high_two_fixed_weight_and_tie_fails_closed() -> None:
    scores = {pair: float(index) for index, pair in enumerate(CANDIDATE_C_PAIRS)}
    selected = select_candidate_c_weights(scores)
    assert selected == (
        ("AUDUSD", 0.25),
        ("EURUSD", 0.25),
        ("USDCHF", -0.25),
        ("USDJPY", -0.25),
    )
    assert sum(abs(weight) for _, weight in selected) == 1.0
    assert sum(weight for _, weight in selected) == 0.0
    scores["GBPUSD"] = scores["EURUSD"]
    assert select_candidate_c_weights(scores) is None


def test_score_ignores_volume_and_future_values() -> None:
    closes = (1.0, 1.01, 1.02, 1.03, 1.04, 1.05)
    baseline = candidate_c_score("AUDUSD", closes)
    extended = closes + (99.0,)
    assert candidate_c_score("AUDUSD", extended[:6]) == baseline
    assert candidate_c_score("AUDUSD", closes, volumes=(1, 1, 0, 1, 1, 1)) == baseline


@pytest.mark.parametrize(
    "boundary, expected",
    [
        (datetime(2021, 12, 31, tzinfo=UTC), False),
        (datetime(2022, 1, 1, tzinfo=UTC), False),
        (datetime(2022, 1, 2, tzinfo=UTC), True),
        (datetime(2023, 12, 31, tzinfo=UTC), False),
    ],
)
def test_frozen_signal_boundaries(boundary: datetime, expected: bool) -> None:
    assert validate_candidate_c_signal_boundary(boundary) is expected


def test_sealed_or_pre_history_signal_boundary_fails_closed() -> None:
    with pytest.raises(ValueError, match="sealed"):
        validate_candidate_c_signal_boundary(datetime(2024, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="window"):
        validate_candidate_c_signal_boundary(datetime(2013, 12, 31, tzinfo=UTC))


def test_entry_evidence_dispositions_are_exact_and_all_seven_are_required() -> None:
    available = [CandidateCExecutionState.AVAILABLE] * 7
    assert candidate_c_entry_disposition(available) is CandidateCEntryDisposition.ENTER
    for state in (
        CandidateCExecutionState.ABSENT_EVIDENCED,
        CandidateCExecutionState.EMPTY_EVIDENCED,
        CandidateCExecutionState.NO_VALID_TICK,
    ):
        states = available.copy()
        states[3] = state
        assert candidate_c_entry_disposition(states) is CandidateCEntryDisposition.NON_ENTRY
    for state in (
        CandidateCExecutionState.INVALID_PARTITION,
        CandidateCExecutionState.MISSING_LOCAL_PARTITION,
    ):
        states = available.copy()
        states[3] = state
        assert candidate_c_entry_disposition(states) is CandidateCEntryDisposition.NOT_EVALUABLE
    with pytest.raises(ValueError, match="seven"):
        candidate_c_entry_disposition(available[:-1])


def test_unavailable_exit_after_entry_is_not_evaluable() -> None:
    states = [CandidateCExecutionState.AVAILABLE] * 7
    states[-1] = CandidateCExecutionState.EMPTY_EVIDENCED
    assert candidate_c_entry_disposition(states, after_entry=True) is (
        CandidateCEntryDisposition.NOT_EVALUABLE
    )


def test_adverse_fills_stress_and_jpy_pips() -> None:
    buy = candidate_c_fill("EURUSD", 1.1000, 1.1002, side=1, entry=True, factor=1.0)
    sell = candidate_c_fill("EURUSD", 1.1000, 1.1002, side=-1, entry=True, factor=1.0)
    assert buy == pytest.approx(1.10022)
    assert sell == pytest.approx(1.09998)
    stressed = candidate_c_fill("EURUSD", 1.1000, 1.1002, side=1, entry=True, factor=1.5)
    assert stressed == pytest.approx(1.10028)
    assert candidate_c_fill("USDJPY", 110.00, 110.02, side=1, entry=True, factor=1.0) == (
        pytest.approx(110.022)
    )


def test_leg_accounting_orientation_commission_and_gross_net_separation() -> None:
    direct = candidate_c_leg_accounting(
        pair="EURUSD",
        foreign_side=1,
        entry_bid=1.1000,
        entry_ask=1.1002,
        exit_bid=1.1010,
        exit_ask=1.1012,
        factor=1.0,
        pre_entry_equity=1.0,
    )
    inverse = candidate_c_leg_accounting(
        pair="USDCAD",
        foreign_side=1,
        entry_bid=1.3500,
        entry_ask=1.3502,
        exit_bid=1.3490,
        exit_ask=1.3492,
        factor=1.0,
        pre_entry_equity=1.0,
    )
    assert direct.instrument_side == 1
    assert inverse.instrument_side == -1
    assert direct.gross_contribution > direct.net_contribution
    assert inverse.gross_contribution > 0
    stressed = candidate_c_leg_accounting(
        pair="EURUSD",
        foreign_side=1,
        entry_bid=1.1000,
        entry_ask=1.1002,
        exit_bid=1.1010,
        exit_ask=1.1012,
        factor=1.5,
        pre_entry_equity=1.0,
    )
    assert stressed.commission_usd == pytest.approx(direct.commission_usd)
    assert stressed.net_contribution < direct.net_contribution


def test_observed_spread_is_not_double_charged() -> None:
    fill = candidate_c_fill("EURUSD", 1.0, 1.0002, side=1, entry=True, factor=1.0)
    assert fill == pytest.approx(1.00022)


def test_metrics_exact_formulas_and_non_entry_flat_day() -> None:
    gross = np.array([0.0, 0.02, -0.01, 0.0], dtype=np.float64)
    net = np.array([0.0, 0.015, -0.012, 0.0], dtype=np.float64)
    metrics = compute_candidate_c_metrics(
        gross_daily=gross,
        net_daily=net,
        completed_cohort_returns=(0.015, -0.012),
        gross_cohort_returns=(0.02, -0.01),
        trade_count=8,
        non_entry_reason_counts=(("empty_evidenced", 1),),
    )
    assert metrics.trade_count == 8
    assert metrics.cohort_count == 2
    assert metrics.net_expectancy == pytest.approx(0.0015)
    assert metrics.gross_expectancy == pytest.approx(0.005)
    assert metrics.sharpe == pytest.approx(np.mean(net) / np.std(net, ddof=1) * math.sqrt(365.2425))
    assert metrics.max_drawdown == pytest.approx(0.012)
    assert metrics.cost_drag_expectancy == pytest.approx(0.0035)
    assert metrics.annualized_return == pytest.approx(np.prod(1 + net) ** (365.2425 / 4) - 1)


def test_metrics_undefined_inputs_fail_not_coerce() -> None:
    with pytest.raises(ValueError, match="cohort"):
        compute_candidate_c_metrics(
            gross_daily=np.zeros(3),
            net_daily=np.zeros(3),
            completed_cohort_returns=(),
            gross_cohort_returns=(),
            trade_count=0,
            non_entry_reason_counts=(),
        )
    with pytest.raises(ValueError, match="variance"):
        compute_candidate_c_metrics(
            gross_daily=np.ones(3) * 0.01,
            net_daily=np.ones(3) * 0.01,
            completed_cohort_returns=(0.01,),
            gross_cohort_returns=(0.01,),
            trade_count=4,
            non_entry_reason_counts=(),
        )


def test_newey_west_automatic_lag_and_one_sided_alpha() -> None:
    values = np.linspace(-0.01, 0.02, 200, dtype=np.float64)
    result = candidate_c_newey_west(values)
    assert result.lag == math.floor(4 * (len(values) / 100) ** (2 / 9))
    assert result.alpha == 0.00625
    assert result.critical_probability == 0.99375
    assert result.lower_confidence_bound == pytest.approx(
        result.mean - result.critical_value * result.standard_error
    )


def test_all_frozen_gates_are_required_for_go() -> None:
    train = _positive_split()
    validation = _positive_split()
    result = decide_candidate_c(train=train, validation=validation)
    assert result.decision is CandidateCDecision.GO
    assert result.meaning == "GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST"

    failures = (
        replace(train, headline=replace(train.headline, net_expectancy=0.0)),
        replace(validation, stress=replace(validation.stress, annualized_return=0.0)),
        replace(validation, headline_lcb=0.0),
        replace(validation, yearly_stress=((2022, 0.0), (2023, 0.001))),
        replace(
            validation,
            pair_stress=tuple(
                (pair, 0.001 if index == 0 else -0.001)
                for index, pair in enumerate(CANDIDATE_C_PAIRS)
            ),
        ),
        replace(validation, headline=replace(validation.headline, max_drawdown=1.0)),
    )
    for index, failed in enumerate(failures):
        candidate_train, candidate_validation = (
            (failed, validation) if index == 0 else (train, failed)
        )
        assert decide_candidate_c(
            train=candidate_train, validation=candidate_validation
        ).decision is CandidateCDecision.NO_GO


def test_not_evaluable_has_precedence_over_no_go() -> None:
    result = decide_candidate_c(
        train=_positive_split(),
        validation=_positive_split(),
        not_evaluable_reasons=("missing_local_partition",),
    )
    assert result.decision is CandidateCDecision.NOT_EVALUABLE


def test_policy_identity_is_deterministic_and_decision_sensitive() -> None:
    first = build_candidate_c_policy()
    second = build_candidate_c_policy()
    assert first == second
    assert first.adr_sha256 == CANDIDATE_C_ADR_SHA256
    changed = replace(first, stress_factor=1.6, policy_id="")
    assert changed.policy_id != first.policy_id


def test_run_identity_semantics_and_audit_only_fields() -> None:
    policy = build_candidate_c_policy()
    environment = CandidateCCodeEnvironment("a" * 40, True)
    datasets = tuple((pair, f"dataset-{pair}", f"revision-{pair}") for pair in CANDIDATE_C_PAIRS)
    first = build_candidate_c_run_id(
        policy=policy,
        code_environment=environment,
        dataset_semantics=datasets,
        execution_manifest_id="b" * 64,
        execution_record_ids=("c" * 64,),
        execution_state_counts=(("available", 1),),
    )
    second = build_candidate_c_run_id(
        policy=policy,
        code_environment=environment,
        dataset_semantics=datasets,
        execution_manifest_id="b" * 64,
        execution_record_ids=("c" * 64,),
        execution_state_counts=(("available", 1),),
        audit_context={"path": "elsewhere", "mtime": 123, "result": -999},
    )
    assert first == second
    changed = build_candidate_c_run_id(
        policy=policy,
        code_environment=environment,
        dataset_semantics=datasets,
        execution_manifest_id="d" * 64,
        execution_record_ids=("c" * 64,),
        execution_state_counts=(("available", 1),),
    )
    assert changed != first


def test_dirty_worktree_is_prohibited() -> None:
    with pytest.raises(ValueError, match="clean"):
        build_candidate_c_run_id(
            policy=build_candidate_c_policy(),
            code_environment=CandidateCCodeEnvironment("a" * 40, False),
            dataset_semantics=tuple(
                (pair, f"dataset-{pair}", f"revision-{pair}") for pair in CANDIDATE_C_PAIRS
            ),
            execution_manifest_id="b" * 64,
            execution_record_ids=("c" * 64,),
            execution_state_counts=(("available", 1),),
        )


def test_canonical_result_is_byte_deterministic_and_has_no_trading_surface() -> None:
    result = decide_candidate_c(train=_positive_split(), validation=_positive_split())
    assert canonical_candidate_c_result(result) == canonical_candidate_c_result(result)
    import fxlab.research.candidate_c_measurement as module

    forbidden = {"order_send", "send_order", "close_position", "ExecutionIntent"}
    assert forbidden.isdisjoint(set(dir(module)))


def _synthetic_dataset(pair: str, pair_index: int) -> BarDataset:
    start = datetime(2014, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 1, tzinfo=UTC)
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


@pytest.fixture(scope="module")
def synthetic_bundle() -> tuple[dict[str, BarDataset], CandidateCExecutionEvidenceManifest]:
    start = datetime(2014, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 1, tzinfo=UTC)
    datasets = {
        pair: _synthetic_dataset(pair, pair_index)
        for pair_index, pair in enumerate(CANDIDATE_C_PAIRS)
    }
    records = []
    for pair_index, pair in enumerate(CANDIDATE_C_PAIRS):
        boundary = start
        while boundary < end:
            day = (boundary - start).days
            center = (110.0 if pair == "USDJPY" else 1.0 + pair_index * 0.1) + (
                math.sin(day / 11.0) * (0.01 if pair == "USDJPY" else 0.001)
            )
            spread = 0.02 if pair == "USDJPY" else 0.0002
            records.append(
                _make_record(
                    pair=pair,
                    boundary=boundary,
                    state=CandidateCExecutionState.AVAILABLE,
                    partition_state="present_staged",
                    raw_sha256=hashlib.sha256(f"{pair}-{day}".encode()).hexdigest(),
                    raw_byte_count=20,
                    selected_tick_offset_ms=0,
                    selected_tick_timestamp=boundary,
                    bid=center - spread / 2,
                    ask=center + spread / 2,
                    bid_volume=1.0,
                    ask_volume=1.0,
                )
            )
            boundary += timedelta(days=1)
    ordered = tuple(records)
    state_counts = ((CandidateCExecutionState.AVAILABLE.value, len(ordered)),)
    payload = _manifest_payload(start, end, CANDIDATE_C_PAIRS, ordered, state_counts)
    manifest = CandidateCExecutionEvidenceManifest(
        schema_version=CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
        start=start,
        end=end,
        pairs=CANDIDATE_C_PAIRS,
        records=ordered,
        state_counts=state_counts,
        total_count=len(ordered),
        available_count=len(ordered),
        manifest_id=_sha(payload),
    )
    return datasets, manifest


def test_end_to_end_synthetic_measurement_is_deterministic_and_sealed(
    synthetic_bundle: tuple[dict[str, BarDataset], CandidateCExecutionEvidenceManifest],
) -> None:
    datasets, manifest = synthetic_bundle
    environment = CandidateCCodeEnvironment("a" * 40, True)
    first = measure_candidate_c(
        datasets=datasets, execution_manifest=manifest, code_environment=environment
    )
    second = measure_candidate_c(
        datasets=datasets, execution_manifest=manifest, code_environment=environment
    )
    assert first == second
    assert canonical_candidate_c_result(first) == canonical_candidate_c_result(second)
    assert first.decision in {CandidateCDecision.GO, CandidateCDecision.NO_GO}
    assert first.train is not None and first.validation is not None
    assert first.train.headline.trade_count == first.train.headline.cohort_count * 4
    assert first.validation.headline.trade_count == first.validation.headline.cohort_count * 4
    assert max(record.intended_boundary for record in manifest.records) < datetime(
        2024, 1, 1, tzinfo=UTC
    )
    adr = Path("docs/adr/0008-candidate-c-cross-sectional-reversal-preregistration.md")
    assert hashlib.sha256(adr.read_bytes()).hexdigest() == CANDIDATE_C_ADR_SHA256