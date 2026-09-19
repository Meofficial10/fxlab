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
    CandidateCExecutionEvidence,
    CandidateCExecutionEvidenceManifest,
    CandidateCExecutionState,
    _make_record,
    _manifest_payload,
    _sha,
)
from fxlab.research.candidate_d_measurement import (
    CANDIDATE_D_ADR_SHA256,
    CANDIDATE_D_EARLIEST_ENTRY,
    CANDIDATE_D_PAIRS,
    CANDIDATE_D_PROTOCOL_ID,
    CandidateDCodeEnvironment,
    CandidateDDecision,
    CandidateDEntryDisposition,
    CandidateDMetrics,
    CandidateDSplitResult,
    _measure_split,
    build_candidate_d_policy,
    build_candidate_d_run_id,
    candidate_d_base_units,
    candidate_d_commission_usd,
    candidate_d_entry_boundary,
    candidate_d_entry_disposition,
    candidate_d_fill,
    candidate_d_newey_west,
    candidate_d_price_pnl_usd,
    candidate_d_signal,
    canonical_candidate_d_result,
    compute_candidate_d_metrics,
    decide_candidate_d,
    measure_candidate_d,
    resolve_candidate_d_exit,
)


def _available(pair: str, boundary: datetime, *, bid: float = 1.0, ask: float = 1.0002):
    if pair == "USDJPY":
        bid, ask = 110.0, 110.02
    return _make_record(
        pair=pair,
        boundary=boundary,
        state=CandidateCExecutionState.AVAILABLE,
        partition_state="present_staged",
        raw_sha256=hashlib.sha256(f"{pair}-{boundary.isoformat()}".encode()).hexdigest(),
        raw_byte_count=20,
        selected_tick_offset_ms=0,
        selected_tick_timestamp=boundary,
        bid=bid,
        ask=ask,
        bid_volume=1.0,
        ask_volume=1.0,
    )


def _state_record(pair: str, boundary: datetime, state: CandidateCExecutionState):
    if state is CandidateCExecutionState.AVAILABLE:
        return _available(pair, boundary)
    return _make_record(
        pair=pair,
        boundary=boundary,
        state=state,
        partition_state=state.value,
        absence_evidence_type=(
            "http_404"
            if state is CandidateCExecutionState.ABSENT_EVIDENCED
            else "http_200_empty_body"
            if state is CandidateCExecutionState.EMPTY_EVIDENCED
            else None
        ),
    )


def test_adr_identity_and_frozen_policy_are_exact() -> None:
    adr = Path("docs/adr/0011-candidate-d-time-series-momentum-preregistration.md")
    assert hashlib.sha256(adr.read_bytes()).hexdigest() == CANDIDATE_D_ADR_SHA256
    policy = build_candidate_d_policy()
    assert policy.protocol_id == CANDIDATE_D_PROTOCOL_ID
    assert policy.adr_sha256 == CANDIDATE_D_ADR_SHA256
    assert policy == build_candidate_d_policy()
    assert replace(policy, holding_sessions=19, policy_id="").policy_id != policy.policy_id


def test_signal_requires_21_closes_and_uses_native_pair_orientation() -> None:
    rising = tuple(1.0 + index * 0.01 for index in range(21))
    falling = tuple(reversed(rising))
    assert candidate_d_signal("EURUSD", rising) == 1
    assert candidate_d_signal("USDJPY", rising) == 1
    assert candidate_d_signal("USDCAD", falling) == -1
    assert candidate_d_signal("USDCHF", (1.0,) * 21) == 0
    with pytest.raises(ValueError, match="21"):
        candidate_d_signal("EURUSD", rising[-20:])
    with pytest.raises(ValueError, match="exactly 21"):
        candidate_d_signal("EURUSD", rising + (2.0,))


def test_zero_volume_is_valid_and_future_close_cannot_change_signal() -> None:
    closes = tuple(1.0 + index * 0.001 for index in range(21))
    volumes = (1.0,) * 10 + (0.0,) + (1.0,) * 10
    baseline = candidate_d_signal("AUDUSD", closes, volumes=volumes)
    assert baseline == 1
    assert candidate_d_signal("AUDUSD", (closes + (99.0,))[:21], volumes=volumes) == baseline


def test_causal_boundary_mapping_and_earliest_entry() -> None:
    bar_open = datetime(2014, 1, 21, tzinfo=UTC)
    assert candidate_d_entry_boundary(bar_open) == CANDIDATE_D_EARLIEST_ENTRY
    with pytest.raises(ValueError, match="earliest"):
        candidate_d_entry_boundary(datetime(2014, 1, 20, tzinfo=UTC))
    with pytest.raises(ValueError, match="sealed"):
        candidate_d_entry_boundary(datetime(2023, 12, 31, tzinfo=UTC))


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (CandidateCExecutionState.AVAILABLE, CandidateDEntryDisposition.ENTER),
        (CandidateCExecutionState.EMPTY_EVIDENCED, CandidateDEntryDisposition.NON_ENTRY),
        (CandidateCExecutionState.ABSENT_EVIDENCED, CandidateDEntryDisposition.NON_ENTRY),
        (CandidateCExecutionState.NO_VALID_TICK, CandidateDEntryDisposition.NOT_EVALUABLE),
        (CandidateCExecutionState.INVALID_PARTITION, CandidateDEntryDisposition.NOT_EVALUABLE),
        (
            CandidateCExecutionState.MISSING_LOCAL_PARTITION,
            CandidateDEntryDisposition.NOT_EVALUABLE,
        ),
    ],
)
def test_entry_evidence_is_pair_independent(
    state: CandidateCExecutionState, expected: CandidateDEntryDisposition
) -> None:
    assert candidate_d_entry_disposition(state) is expected


def test_one_pairs_empty_entries_do_not_block_other_pairs() -> None:
    start = datetime(2014, 1, 1, tzinfo=UTC)
    end = datetime(2014, 4, 1, tzinfo=UTC)
    index = pd.date_range(start, end, inclusive="left", freq="D", tz="UTC")
    frames: dict[str, pd.DataFrame] = {}
    lookup: dict[tuple[str, datetime], CandidateCExecutionEvidence] = {}
    for pair_index, pair in enumerate(CANDIDATE_D_PAIRS):
        base = 110.0 if pair == "USDJPY" else 1.0 + pair_index * 0.1
        close = base + np.arange(len(index), dtype=np.float64) * (
            0.01 if pair == "USDJPY" else 0.0001
        )
        frames[pair] = pd.DataFrame(
            {
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": np.ones(len(index)),
            },
            index=index,
        )
        boundary = start
        while boundary < end:
            state = (
                CandidateCExecutionState.EMPTY_EVIDENCED
                if pair == "EURUSD"
                else CandidateCExecutionState.AVAILABLE
            )
            lookup[(pair, boundary)] = _state_record(pair, boundary, state)
            boundary += timedelta(days=1)

    result, reasons = _measure_split(
        split_start=start,
        split_end=end,
        frames=frames,
        lookup=lookup,
    )
    assert reasons == ()
    assert result is not None
    counts = dict(result.headline.per_pair_trade_counts)
    assert counts["EURUSD"] == 0
    assert all(counts[pair] == 3 for pair in CANDIDATE_D_PAIRS if pair != "EURUSD")


def test_exit_counts_20_available_sessions_and_skips_evidenced_closures() -> None:
    pair = "EURUSD"
    entry = datetime(2021, 1, 1, tzinfo=UTC)
    lookup: dict[tuple[str, datetime], CandidateCExecutionEvidence] = {}
    available = 0
    expected = None
    for day in range(1, 25):
        boundary = entry + timedelta(days=day)
        state = (
            CandidateCExecutionState.EMPTY_EVIDENCED
            if day in (6, 7)
            else CandidateCExecutionState.ABSENT_EVIDENCED
            if day == 14
            else CandidateCExecutionState.AVAILABLE
        )
        lookup[(pair, boundary)] = _state_record(pair, boundary, state)
        if state is CandidateCExecutionState.AVAILABLE:
            available += 1
            if available == 20:
                expected = boundary
                break
    resolution = resolve_candidate_d_exit(
        pair=pair,
        entry_boundary=entry,
        split_end=datetime(2022, 1, 1, tzinfo=UTC),
        evidence_lookup=lookup,
    )
    assert resolution.status == "found"
    assert resolution.exit_boundary == expected
    assert resolution.available_sessions == 20


def test_twentieth_available_session_on_day_40_is_a_valid_exit() -> None:
    pair = "EURUSD"
    entry = datetime(2021, 1, 1, tzinfo=UTC)
    lookup = {
        (pair, entry + timedelta(days=day)): _state_record(
            pair,
            entry + timedelta(days=day),
            (
                CandidateCExecutionState.EMPTY_EVIDENCED
                if day % 2
                else CandidateCExecutionState.AVAILABLE
            ),
        )
        for day in range(1, 41)
    }
    resolution = resolve_candidate_d_exit(
        pair=pair,
        entry_boundary=entry,
        split_end=datetime(2022, 1, 1, tzinfo=UTC),
        evidence_lookup=lookup,
    )
    assert resolution.status == "found"
    assert resolution.exit_boundary == entry + timedelta(days=40)
    assert resolution.available_sessions == 20


@pytest.mark.parametrize(
    "state",
    [
        CandidateCExecutionState.NO_VALID_TICK,
        CandidateCExecutionState.INVALID_PARTITION,
        CandidateCExecutionState.MISSING_LOCAL_PARTITION,
    ],
)
def test_exit_missing_or_corrupt_state_fails_closed(state: CandidateCExecutionState) -> None:
    entry = datetime(2021, 1, 1, tzinfo=UTC)
    resolution = resolve_candidate_d_exit(
        pair="EURUSD",
        entry_boundary=entry,
        split_end=datetime(2022, 1, 1, tzinfo=UTC),
        evidence_lookup={
            ("EURUSD", entry + timedelta(days=1)): _state_record(
                "EURUSD", entry + timedelta(days=1), state
            )
        },
    )
    assert resolution.status == "not_evaluable"
    assert state.value in resolution.reason


def test_exit_40_day_cap_and_split_purge_do_not_read_future() -> None:
    entry = datetime(2021, 1, 1, tzinfo=UTC)
    closure_lookup = {
        ("EURUSD", entry + timedelta(days=day)): _state_record(
            "EURUSD", entry + timedelta(days=day), CandidateCExecutionState.EMPTY_EVIDENCED
        )
        for day in range(1, 41)
    }
    capped = resolve_candidate_d_exit(
        pair="EURUSD",
        entry_boundary=entry,
        split_end=datetime(2022, 1, 1, tzinfo=UTC),
        evidence_lookup=closure_lookup,
    )
    assert capped.status == "not_evaluable"
    assert capped.reason == "exit_horizon_exceeded"

    class Sentinel(dict):
        def get(self, key, default=None):
            if key[1] >= datetime(2022, 1, 1, tzinfo=UTC):
                raise AssertionError("split-end evidence was read")
            return super().get(key, default)

    near_boundary = datetime(2021, 12, 20, tzinfo=UTC)
    sentinel = Sentinel(
        {
            ("EURUSD", near_boundary + timedelta(days=day)): _state_record(
                "EURUSD",
                near_boundary + timedelta(days=day),
                CandidateCExecutionState.EMPTY_EVIDENCED,
            )
            for day in range(1, 12)
        }
    )
    purged = resolve_candidate_d_exit(
        pair="EURUSD",
        entry_boundary=near_boundary,
        split_end=datetime(2022, 1, 1, tzinfo=UTC),
        evidence_lookup=sentinel,
    )
    assert purged.status == "purged"


def test_adverse_long_short_fills_stress_and_jpy_pips() -> None:
    assert candidate_d_fill("EURUSD", 1.1000, 1.1002, side=1, factor=1.0) == pytest.approx(
        1.10022
    )
    assert candidate_d_fill("EURUSD", 1.1000, 1.1002, side=-1, factor=1.0) == pytest.approx(
        1.09998
    )
    assert candidate_d_fill("EURUSD", 1.1000, 1.1002, side=1, factor=1.5) == pytest.approx(
        1.10028
    )
    assert candidate_d_fill("USDJPY", 110.00, 110.02, side=1, factor=1.0) == pytest.approx(
        110.022
    )


def test_position_sizing_and_usd_conversion_are_native_and_order_independent() -> None:
    assert candidate_d_base_units("EURUSD", 1_000.0, 1.25) == pytest.approx(800.0)
    assert candidate_d_base_units("USDJPY", 1_000.0, 110.0) == pytest.approx(1_000.0)
    assert candidate_d_price_pnl_usd(
        "EURUSD", 1, 800.0, 1.25, 1.25, 1.26
    ) == pytest.approx(8.0)
    assert candidate_d_price_pnl_usd(
        "USDJPY", 1, 1_000.0, 110.0, 110.0, 111.0
    ) == pytest.approx(1_000.0 / 111.0)
    equity = 7_000.0
    fills = {"USDJPY": 110.0, "EURUSD": 1.25}
    first = {
        pair: candidate_d_base_units(pair, equity / 7.0, fills[pair])
        for pair in ("EURUSD", "USDJPY")
    }
    second = {
        pair: candidate_d_base_units(pair, equity / 7.0, fills[pair])
        for pair in ("USDJPY", "EURUSD")
    }
    assert first == second


@pytest.mark.parametrize(
    ("side", "entry", "first", "second", "exit_price"),
    [
        (1, 110.0, 111.0, 112.0, 113.0),
        (-1, 110.0, 109.0, 108.0, 107.0),
    ],
)
def test_usd_base_daily_marks_telescope_to_authoritative_exit_value(
    side: int,
    entry: float,
    first: float,
    second: float,
    exit_price: float,
) -> None:
    units = 1_000.0
    increments = (
        candidate_d_price_pnl_usd(
            "USDJPY", side, units, entry, entry, first
        ),
        candidate_d_price_pnl_usd(
            "USDJPY", side, units, entry, first, second
        ),
        candidate_d_price_pnl_usd(
            "USDJPY", side, units, entry, second, exit_price
        ),
    )
    authoritative = side * units * (exit_price - entry) / exit_price
    assert math.fsum(increments) == pytest.approx(authoritative)


@pytest.mark.parametrize(
    ("side", "entry_fill", "first_close", "second_close", "exit_fill"),
    [
        (1, 1.1002, 1.1010, 1.1020, 1.1028),
        (-1, 1.1028, 1.1020, 1.1010, 1.1002),
    ],
)
def test_entry_intermediate_exit_mtm_charges_commission_once_per_side(
    side: int,
    entry_fill: float,
    first_close: float,
    second_close: float,
    exit_fill: float,
) -> None:
    units = candidate_d_base_units("EURUSD", 1_100.0, entry_fill)
    commission = candidate_d_commission_usd(units, sides=1)
    entry_day = (
        candidate_d_price_pnl_usd(
            "EURUSD", side, units, entry_fill, entry_fill, first_close
        )
        - commission
    )
    intermediate = candidate_d_price_pnl_usd(
        "EURUSD", side, units, entry_fill, first_close, second_close
    )
    exit_day = (
        candidate_d_price_pnl_usd(
            "EURUSD", side, units, entry_fill, second_close, exit_fill
        )
        - commission
    )
    expected = (
        candidate_d_price_pnl_usd(
            "EURUSD", side, units, entry_fill, entry_fill, exit_fill
        )
        - candidate_d_commission_usd(units, sides=2)
    )
    assert entry_day + intermediate + exit_day == pytest.approx(expected)
    assert candidate_d_commission_usd(units, sides=2) == pytest.approx(
        7.0 * units / 100_000
    )


def test_metrics_use_calendar_returns_and_costs_once() -> None:
    gross = np.array([0.01, 0.0, -0.005, 0.002])
    net = np.array([0.009, 0.0, -0.006, 0.0015])
    metrics = compute_candidate_d_metrics(
        gross_daily=gross,
        net_daily=net,
        gross_trade_returns=(0.01, -0.005),
        net_trade_returns=(0.009, -0.006),
        per_pair_trade_counts=((pair, 0) for pair in CANDIDATE_D_PAIRS),
        non_entry_reason_counts=(("empty_evidenced", 1),),
        pair_contributions=((pair, 0.0) for pair in CANDIDATE_D_PAIRS),
    )
    assert metrics.trade_count == 2
    assert metrics.gross_return == pytest.approx(np.prod(1 + gross) - 1)
    assert metrics.net_return == pytest.approx(np.prod(1 + net) - 1)
    assert metrics.net_expectancy == pytest.approx(0.0015)
    assert metrics.sharpe == pytest.approx(np.mean(net) / np.std(net, ddof=1) * math.sqrt(365.2425))
    assert metrics.cost_drag_return == pytest.approx(metrics.gross_return - metrics.net_return)


def test_newey_west_is_deterministic_and_zero_variance_is_undefined() -> None:
    values = np.linspace(-0.01, 0.02, 200)
    first = candidate_d_newey_west(values)
    assert first == candidate_d_newey_west(values)
    assert first.lag == math.floor(4 * (len(values) / 100) ** (2 / 9))
    assert first.alpha == 0.00625
    assert first.lower_confidence_bound == pytest.approx(
        first.mean - first.critical_value * first.standard_error
    )
    with pytest.raises(ValueError, match="undefined"):
        candidate_d_newey_west(np.zeros(20))


def _positive_metrics() -> CandidateDMetrics:
    return CandidateDMetrics(
        trade_count=14,
        per_pair_trade_counts=tuple((pair, 2) for pair in CANDIDATE_D_PAIRS),
        non_entry_reason_counts=(),
        gross_return=0.12,
        net_return=0.10,
        gross_expectancy=0.01,
        net_expectancy=0.008,
        gross_annualized_return=0.06,
        annualized_return=0.05,
        sharpe=1.0,
        max_drawdown=0.1,
        cost_drag_return=0.02,
        cost_drag_expectancy=0.002,
        pair_contributions=tuple((pair, 0.01) for pair in CANDIDATE_D_PAIRS),
    )


def _positive_split() -> CandidateDSplitResult:
    headline = _positive_metrics()
    stress = replace(
        headline,
        net_return=0.05,
        net_expectancy=0.004,
        annualized_return=0.025,
        sharpe=0.5,
        pair_contributions=tuple((pair, 0.005) for pair in CANDIDATE_D_PAIRS),
    )
    return CandidateDSplitResult(
        headline=headline,
        stress=stress,
        headline_lcb=0.0002,
        stress_lcb=0.0001,
        yearly_headline=((2022, 0.02), (2023, 0.03)),
        yearly_stress=((2022, 0.01), (2023, 0.015)),
    )


def test_p4_go_no_go_and_not_evaluable_are_distinct() -> None:
    train, validation = _positive_split(), _positive_split()
    go = decide_candidate_d(train=train, validation=validation)
    assert go.decision is CandidateDDecision.GO
    assert go.meaning == "GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST"
    assert decide_candidate_d(
        train=train,
        validation=replace(validation, stress=replace(validation.stress, net_return=0.0)),
    ).decision is CandidateDDecision.NO_GO
    assert decide_candidate_d(
        train=train,
        validation=validation,
        not_evaluable_reasons=("missing_local_partition",),
    ).decision is CandidateDDecision.NOT_EVALUABLE


def test_run_id_is_deterministic_decision_sensitive_and_audit_independent() -> None:
    policy = build_candidate_d_policy()
    environment = CandidateDCodeEnvironment("a" * 40, True)
    datasets = tuple((pair, f"dataset-{pair}", f"revision-{pair}") for pair in CANDIDATE_D_PAIRS)
    arguments = dict(
        policy=policy,
        code_environment=environment,
        dataset_semantics=datasets,
        execution_manifest_id="b" * 64,
        execution_record_ids=("c" * 64,),
        execution_state_counts=(("available", 1),),
    )
    first = build_candidate_d_run_id(**arguments)
    assert first == build_candidate_d_run_id(
        **arguments,
        audit_context={"path": "elsewhere", "mtime": 123, "result": -999},
    )
    assert first != build_candidate_d_run_id(
        **{**arguments, "execution_manifest_id": "d" * 64}
    )
    with pytest.raises(ValueError, match="clean"):
        build_candidate_d_run_id(
            **{**arguments, "code_environment": CandidateDCodeEnvironment("a" * 40, False)}
        )


def test_canonical_result_has_no_trading_authority() -> None:
    decision = decide_candidate_d(train=_positive_split(), validation=_positive_split())
    assert canonical_candidate_d_result(decision) == canonical_candidate_d_result(decision)
    import fxlab.research.candidate_d_measurement as module

    assert {"order_send", "send_order", "close_position", "ExecutionIntent"}.isdisjoint(
        set(dir(module))
    )


def _synthetic_dataset(pair: str, pair_index: int) -> BarDataset:
    start = datetime(2014, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 1, tzinfo=UTC)
    index = pd.date_range(start, end, inclusive="left", freq="D", tz="UTC")
    steps = np.arange(len(index), dtype=np.float64)
    base = 110.0 if pair == "USDJPY" else 1.0 + pair_index * 0.1
    close = base * np.exp(0.00005 * steps + 0.002 * np.sin(steps / 17.0 + pair_index))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.where(steps % 37 == 0, 0.0, 1.0),
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
        for pair_index, pair in enumerate(CANDIDATE_D_PAIRS)
    }
    records = []
    for pair_index, pair in enumerate(CANDIDATE_D_PAIRS):
        boundary = start
        while boundary < end:
            day = (boundary - start).days
            center = (110.0 if pair == "USDJPY" else 1.0 + pair_index * 0.1) * math.exp(
                0.00005 * day + 0.002 * math.sin(day / 17.0 + pair_index)
            )
            spread = 0.02 if pair == "USDJPY" else 0.0002
            records.append(
                _available(pair, boundary, bid=center - spread / 2, ask=center + spread / 2)
            )
            boundary += timedelta(days=1)
    ordered = tuple(records)
    state_counts = ((CandidateCExecutionState.AVAILABLE.value, len(ordered)),)
    manifest = CandidateCExecutionEvidenceManifest(
        schema_version=CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
        start=start,
        end=end,
        pairs=CANDIDATE_D_PAIRS,
        records=ordered,
        state_counts=state_counts,
        total_count=len(ordered),
        available_count=len(ordered),
        manifest_id=_sha(_manifest_payload(start, end, CANDIDATE_D_PAIRS, ordered, state_counts)),
    )
    return datasets, manifest


def test_end_to_end_synthetic_measurement_is_deterministic_and_sealed(
    synthetic_bundle: tuple[dict[str, BarDataset], CandidateCExecutionEvidenceManifest],
) -> None:
    datasets, manifest = synthetic_bundle
    environment = CandidateDCodeEnvironment("a" * 40, True)
    first = measure_candidate_d(
        datasets=datasets,
        execution_manifest=manifest,
        code_environment=environment,
    )
    second = measure_candidate_d(
        datasets=datasets,
        execution_manifest=manifest,
        code_environment=environment,
    )
    assert first == second
    assert canonical_candidate_d_result(first) == canonical_candidate_d_result(second)
    assert first.decision in {CandidateDDecision.GO, CandidateDDecision.NO_GO}
    assert first.train is not None and first.validation is not None
    assert dict(first.train.headline.per_pair_trade_counts) == {
        pair: 145 for pair in CANDIDATE_D_PAIRS
    }
    assert dict(first.validation.headline.per_pair_trade_counts) == {
        pair: 36 for pair in CANDIDATE_D_PAIRS
    }
    assert first.train.headline.trade_count == 145 * 7
    assert first.validation.headline.trade_count == 36 * 7
    assert max(record.intended_boundary for record in manifest.records) < datetime(
        2024, 1, 1, tzinfo=UTC
    )
