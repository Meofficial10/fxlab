"""Frozen Candidate C v1 measurement engine.

This module is pure research accounting.  It has no broker, network, execution,
or data-discovery authority.  Real inputs must already satisfy FXLab's typed data
and Candidate C execution-evidence contracts.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

from fxlab.data.dukascopy_direct_d1 import (
    DIRECT_D1_NORMALIZATION_VERSION,
    DIRECT_D1_PROVIDER_ID,
    DIRECT_D1_PROVIDER_VERSION,
    DIRECT_D1_SOURCE_REFERENCE,
)
from fxlab.data.policy_rates import canonical_json, canonical_sha256
from fxlab.data.provider import BarDataset, ProvenanceQuality
from fxlab.research.candidate_c_execution_evidence import (
    CANDIDATE_C_EXECUTION_DECODER_VERSION,
    CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
    CANDIDATE_C_EXECUTION_PAIRS,
    CANDIDATE_C_EXECUTION_SCHEMA,
    CANDIDATE_C_EXECUTION_SOURCE_REFERENCE,
    CandidateCExecutionEvidence,
    CandidateCExecutionEvidenceManifest,
    CandidateCExecutionState,
    _manifest_payload,
    _record_payload,
    _sha,
)

CANDIDATE_C_ADR_SHA256 = "375e88e341596769092dac5e648fadfc709c5fb8b7769a53567e34fe832bbf67"
CANDIDATE_C_PREREGISTRATION_COMMIT = "1348402b7374dcc8f2dfef9537fe876c9b561a6d"
CANDIDATE_C_PAIRS = CANDIDATE_C_EXECUTION_PAIRS
CANDIDATE_C_INVERSE_PAIRS = frozenset(("USDCAD", "USDCHF", "USDJPY"))
CANDIDATE_C_START = datetime(2014, 1, 1, tzinfo=UTC)
CANDIDATE_C_TRAIN_END = datetime(2022, 1, 1, tzinfo=UTC)
CANDIDATE_C_END = datetime(2024, 1, 1, tzinfo=UTC)
CANDIDATE_C_EARLIEST_SIGNAL = datetime(2014, 1, 7, tzinfo=UTC)
CANDIDATE_C_ANNUALIZATION = 365.2425
CANDIDATE_C_ALPHA = 0.00625
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class CandidateCDecision(StrEnum):
    GO = "GO"
    NO_GO = "NO_GO"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class CandidateCEntryDisposition(StrEnum):
    ENTER = "enter"
    NON_ENTRY = "non_entry"
    NOT_EVALUABLE = "not_evaluable"


@dataclass(frozen=True)
class CandidateCPolicy:
    schema: str = "candidate_c_measurement_policy.v1"
    candidate_id: str = "candidate_c_cross_sectional_reversal"
    candidate_version: int = 1
    adr_path: str = "docs/adr/0008-candidate-c-cross-sectional-reversal-preregistration.md"
    adr_sha256: str = CANDIDATE_C_ADR_SHA256
    preregistration_commit: str = CANDIDATE_C_PREREGISTRATION_COMMIT
    pairs: tuple[str, ...] = CANDIDATE_C_PAIRS
    inverse_pairs: tuple[str, ...] = tuple(sorted(CANDIDATE_C_INVERSE_PAIRS))
    lookback_bars: int = 5
    long_count: int = 2
    short_count: int = 2
    selected_weight: float = 0.25
    headline_factor: float = 1.0
    stress_factor: float = 1.5
    slippage_pips_per_side: float = 0.2
    commission_usd_per_lot_roundturn: float = 7.0
    standard_lot_base_units: int = 100_000
    annualization_days: float = CANDIDATE_C_ANNUALIZATION
    alpha: float = CANDIDATE_C_ALPHA
    prior_tested_families: int = 7
    policy_id: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        payload = _policy_payload(self)
        expected = canonical_sha256(payload)
        if self.policy_id and self.policy_id != expected:
            raise ValueError("policy_id does not match Candidate C policy")
        object.__setattr__(self, "policy_id", expected)


def _policy_payload(policy: CandidateCPolicy) -> dict[str, object]:
    return {
        "schema": policy.schema,
        "candidate_id": policy.candidate_id,
        "candidate_version": policy.candidate_version,
        "adr_path": policy.adr_path,
        "adr_sha256": policy.adr_sha256,
        "preregistration_commit": policy.preregistration_commit,
        "pairs": policy.pairs,
        "inverse_pairs": policy.inverse_pairs,
        "window": (CANDIDATE_C_START, CANDIDATE_C_TRAIN_END, CANDIDATE_C_END),
        "earliest_signal": CANDIDATE_C_EARLIEST_SIGNAL,
        "lookback_bars": policy.lookback_bars,
        "selection": (
            policy.long_count,
            policy.short_count,
            policy.selected_weight,
            "selection_boundary_tie_is_non_entry",
        ),
        "execution": (
            "all_seven_first_valid_00h_bid_ask",
            "causal_entry_non_entry",
            "unavailable_exit_is_not_evaluable",
            "no_fallback",
        ),
        "cost": (
            policy.headline_factor,
            policy.stress_factor,
            policy.slippage_pips_per_side,
            policy.commission_usd_per_lot_roundturn,
            policy.standard_lot_base_units,
            "observed_spread_once_norm_vol_zero",
        ),
        "metrics": (
            policy.annualization_days,
            "sample_std_ddof_1",
            "calendar_daily_returns",
            "unit_usd_equity",
        ),
        "statistics": (
            policy.alpha,
            policy.prior_tested_families,
            "newey_west_bartlett_automatic_lag",
            "student_t_one_sided",
            "no_randomness",
        ),
        "decision": "adr_0008_section_8_exact",
    }


def build_candidate_c_policy() -> CandidateCPolicy:
    return CandidateCPolicy()


@dataclass(frozen=True)
class CandidateCCodeEnvironment:
    commit: str
    worktree_clean: bool

    def __post_init__(self) -> None:
        if not isinstance(self.commit, str) or not _COMMIT_RE.fullmatch(self.commit):
            raise ValueError("code commit must be a lowercase 40-character Git hash")
        if not isinstance(self.worktree_clean, bool):
            raise ValueError("worktree_clean must be boolean")


@dataclass(frozen=True)
class CandidateCLegAccounting:
    pair: str
    foreign_side: int
    instrument_side: int
    gross_contribution: float
    net_contribution: float
    commission_usd: float
    entry_fill: float
    exit_fill: float


@dataclass(frozen=True)
class CandidateCMetrics:
    trade_count: int
    cohort_count: int
    non_entry_reason_counts: tuple[tuple[str, int], ...]
    gross_expectancy: float
    net_expectancy: float
    annualized_return: float
    sharpe: float
    max_drawdown: float
    cost_drag_expectancy: float
    cost_drag_annualized: float


@dataclass(frozen=True)
class CandidateCHACResult:
    n: int
    lag: int
    alpha: float
    critical_probability: float
    mean: float
    long_run_variance: float
    standard_error: float
    critical_value: float
    lower_confidence_bound: float


@dataclass(frozen=True)
class CandidateCSplitResult:
    headline: CandidateCMetrics
    stress: CandidateCMetrics
    headline_lcb: float
    stress_lcb: float
    yearly_headline: tuple[tuple[int, float], ...]
    yearly_stress: tuple[tuple[int, float], ...]
    pair_headline: tuple[tuple[str, float], ...]
    pair_stress: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class CandidateCDecisionResult:
    decision: CandidateCDecision
    meaning: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CandidateCMeasurementResult:
    schema: str
    policy_id: str
    run_id: str
    decision: CandidateCDecision
    decision_meaning: str
    reasons: tuple[str, ...]
    train: CandidateCSplitResult | None
    validation: CandidateCSplitResult | None
    execution_state_counts: tuple[tuple[str, int], ...]
    result_id: str


def candidate_c_score(
    pair: str,
    closes: Sequence[float],
    *,
    volumes: Sequence[float] | None = None,
) -> float:
    if pair not in CANDIDATE_C_PAIRS:
        raise ValueError("unsupported Candidate C pair")
    if len(closes) < 6:
        raise ValueError("Candidate C score requires six causal closes")
    window = tuple(float(item) for item in closes[-6:])
    if any(not math.isfinite(item) or item <= 0 for item in window):
        raise ValueError("Candidate C closes must be finite and positive")
    if volumes is not None:
        selected_volumes = tuple(float(item) for item in volumes[-6:])
        if len(selected_volumes) != 6 or any(
            not math.isfinite(item) or item < 0 for item in selected_volumes
        ):
            raise ValueError("Candidate C volumes must be finite and nonnegative")
    score = math.log(window[-1] / window[0])
    return -score if pair in CANDIDATE_C_INVERSE_PAIRS else score


def select_candidate_c_weights(
    scores: Mapping[str, float],
) -> tuple[tuple[str, float], ...] | None:
    if tuple(pair for pair in CANDIDATE_C_PAIRS if pair in scores) != CANDIDATE_C_PAIRS:
        raise ValueError("all seven Candidate C scores are required")
    if len(scores) != 7 or any(
        not math.isfinite(float(scores[pair])) for pair in CANDIDATE_C_PAIRS
    ):
        raise ValueError("Candidate C scores are invalid")
    ordered = sorted(CANDIDATE_C_PAIRS, key=lambda pair: (float(scores[pair]), pair))
    if float(scores[ordered[1]]) == float(scores[ordered[2]]) or float(
        scores[ordered[-3]]
    ) == float(scores[ordered[-2]]):
        return None
    longs, shorts = set(ordered[:2]), set(ordered[-2:])
    return tuple(
        (pair, 0.25 if pair in longs else -0.25)
        for pair in CANDIDATE_C_PAIRS
        if pair in longs or pair in shorts
    )


def validate_candidate_c_signal_boundary(boundary: datetime) -> bool:
    if not isinstance(boundary, datetime) or boundary.tzinfo is None:
        raise ValueError("signal boundary must be timezone-aware")
    boundary = boundary.astimezone(UTC)
    if boundary < CANDIDATE_C_START:
        raise ValueError("Candidate C research window violation")
    if boundary >= CANDIDATE_C_END:
        raise ValueError("Candidate C sealed window violation")
    if boundary.hour or boundary.minute or boundary.second or boundary.microsecond:
        raise ValueError("Candidate C signal requires a UTC daily boundary")
    if boundary < CANDIDATE_C_EARLIEST_SIGNAL:
        return False
    return boundary not in {
        datetime(2021, 12, 31, tzinfo=UTC),
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2023, 12, 31, tzinfo=UTC),
    }


def candidate_c_entry_disposition(
    states: Sequence[CandidateCExecutionState], *, after_entry: bool = False
) -> CandidateCEntryDisposition:
    values = tuple(states)
    if len(values) != 7 or any(not isinstance(item, CandidateCExecutionState) for item in values):
        raise ValueError("exactly seven execution states are required")
    if all(item is CandidateCExecutionState.AVAILABLE for item in values):
        return CandidateCEntryDisposition.ENTER
    if after_entry:
        return CandidateCEntryDisposition.NOT_EVALUABLE
    if any(
        item
        in {
            CandidateCExecutionState.INVALID_PARTITION,
            CandidateCExecutionState.MISSING_LOCAL_PARTITION,
        }
        for item in values
    ):
        return CandidateCEntryDisposition.NOT_EVALUABLE
    return CandidateCEntryDisposition.NON_ENTRY


def _pip_size(pair: str) -> float:
    if pair not in CANDIDATE_C_PAIRS:
        raise ValueError("unsupported Candidate C pair")
    return 0.01 if pair == "USDJPY" else 0.0001


def candidate_c_fill(
    pair: str,
    bid: float,
    ask: float,
    *,
    side: int,
    entry: bool,
    factor: float,
) -> float:
    bid, ask, factor = float(bid), float(ask), float(factor)
    if (
        side not in (-1, 1)
        or factor not in (1.0, 1.5)
        or not all(math.isfinite(item) and item > 0 for item in (bid, ask))
        or bid > ask
    ):
        raise ValueError("Candidate C executable quote is invalid")
    mid, half = (bid + ask) / 2.0, (ask - bid) / 2.0
    scenario_bid, scenario_ask = mid - factor * half, mid + factor * half
    slippage = factor * 0.2 * _pip_size(pair)
    buying = side == 1 if entry else side == -1
    fill = scenario_ask + slippage if buying else scenario_bid - slippage
    if not math.isfinite(fill) or fill <= 0:
        raise ValueError("Candidate C fill is invalid")
    return fill


def candidate_c_leg_accounting(
    *,
    pair: str,
    foreign_side: int,
    entry_bid: float,
    entry_ask: float,
    exit_bid: float,
    exit_ask: float,
    factor: float,
    pre_entry_equity: float,
) -> CandidateCLegAccounting:
    if foreign_side not in (-1, 1):
        raise ValueError("foreign_side must be -1 or 1")
    if not math.isfinite(pre_entry_equity) or pre_entry_equity <= 0:
        raise ValueError("pre-entry equity must be finite and positive")
    inverse = pair in CANDIDATE_C_INVERSE_PAIRS
    instrument_side = -foreign_side if inverse else foreign_side
    entry_fill = candidate_c_fill(
        pair, entry_bid, entry_ask, side=instrument_side, entry=True, factor=factor
    )
    exit_fill = candidate_c_fill(
        pair, exit_bid, exit_ask, side=instrument_side, entry=False, factor=factor
    )
    entry_mid, exit_mid = (entry_bid + entry_ask) / 2.0, (exit_bid + exit_ask) / 2.0
    q_entry = 1.0 / entry_fill if inverse else entry_fill
    q_exit = 1.0 / exit_fill if inverse else exit_fill
    q_entry_gross = 1.0 / entry_mid if inverse else entry_mid
    q_exit_gross = 1.0 / exit_mid if inverse else exit_mid
    allocated = 0.25 * pre_entry_equity
    sizing_fill = candidate_c_fill(
        pair, entry_bid, entry_ask, side=instrument_side, entry=True, factor=1.0
    )
    sizing_q = 1.0 / sizing_fill if inverse else sizing_fill
    foreign_units = allocated / sizing_q
    lots = (allocated if inverse else foreign_units) / 100_000.0
    commission = 7.0 * lots
    pnl = foreign_side * foreign_units * (q_exit - q_entry)
    gross_pnl = foreign_side * foreign_units * (q_exit_gross - q_entry_gross)
    net = (pnl - commission) / pre_entry_equity
    gross = gross_pnl / pre_entry_equity
    if not all(math.isfinite(item) for item in (net, gross, commission)):
        raise ValueError("Candidate C leg accounting is invalid")
    return CandidateCLegAccounting(
        pair,
        foreign_side,
        instrument_side,
        gross,
        net,
        commission,
        entry_fill,
        exit_fill,
    )


def _annualized_return(values: np.ndarray) -> float:
    growth = float(np.prod(1.0 + values))
    if not math.isfinite(growth) or growth <= 0:
        raise ValueError("Candidate C equity must remain finite and positive")
    return growth ** (CANDIDATE_C_ANNUALIZATION / len(values)) - 1.0


def _maximum_drawdown(values: np.ndarray) -> float:
    equity = np.concatenate(([1.0], np.cumprod(1.0 + values)))
    if not np.isfinite(equity).all() or np.any(equity <= 0):
        raise ValueError("Candidate C equity must remain finite and positive")
    peaks = np.maximum.accumulate(equity)
    return float(-np.min(equity / peaks - 1.0))


def compute_candidate_c_metrics(
    *,
    gross_daily: Sequence[float],
    net_daily: Sequence[float],
    completed_cohort_returns: Sequence[float],
    gross_cohort_returns: Sequence[float],
    trade_count: int,
    non_entry_reason_counts: tuple[tuple[str, int], ...],
) -> CandidateCMetrics:
    gross = np.asarray(gross_daily, dtype=np.float64)
    net = np.asarray(net_daily, dtype=np.float64)
    cohorts = np.asarray(completed_cohort_returns, dtype=np.float64)
    gross_cohorts = np.asarray(gross_cohort_returns, dtype=np.float64)
    if not len(cohorts) or len(cohorts) != len(gross_cohorts):
        raise ValueError("at least one completed cohort is required")
    if len(gross) != len(net) or len(net) < 2 or not all(
        np.isfinite(item).all() for item in (gross, net, cohorts, gross_cohorts)
    ):
        raise ValueError("Candidate C metric inputs are invalid")
    variance = float(np.var(net, ddof=1))
    if variance <= 0 or not math.isfinite(variance):
        raise ValueError("Candidate C daily return variance is undefined")
    gross_expectancy = float(np.mean(gross_cohorts))
    net_expectancy = float(np.mean(cohorts))
    gross_annualized = _annualized_return(gross)
    net_annualized = _annualized_return(net)
    metrics = CandidateCMetrics(
        trade_count=int(trade_count),
        cohort_count=len(cohorts),
        non_entry_reason_counts=tuple(non_entry_reason_counts),
        gross_expectancy=gross_expectancy,
        net_expectancy=net_expectancy,
        annualized_return=net_annualized,
        sharpe=float(np.mean(net) / math.sqrt(variance) * math.sqrt(CANDIDATE_C_ANNUALIZATION)),
        max_drawdown=_maximum_drawdown(net),
        cost_drag_expectancy=gross_expectancy - net_expectancy,
        cost_drag_annualized=gross_annualized - net_annualized,
    )
    if not all(
        math.isfinite(value)
        for value in (
            metrics.gross_expectancy,
            metrics.net_expectancy,
            metrics.annualized_return,
            metrics.sharpe,
            metrics.max_drawdown,
            metrics.cost_drag_expectancy,
            metrics.cost_drag_annualized,
        )
    ):
        raise ValueError("Candidate C metrics are nonfinite")
    return metrics


def candidate_c_newey_west(values: Sequence[float]) -> CandidateCHACResult:
    vector = np.asarray(values, dtype=np.float64)
    n = len(vector)
    if n < 2 or not np.isfinite(vector).all():
        raise ValueError("Candidate C HAC inputs are invalid")
    lag = math.floor(4 * (n / 100) ** (2 / 9))
    mean = float(np.mean(vector))
    residuals = vector - mean
    gammas = [float(np.dot(residuals[k:], residuals[: n - k]) / n) for k in range(lag + 1)]
    lrv = gammas[0] + 2.0 * math.fsum(
        (1.0 - k / (lag + 1)) * gammas[k] for k in range(1, lag + 1)
    )
    if lrv < 0 or not math.isfinite(lrv):
        raise ValueError("Candidate C HAC variance is invalid")
    standard_error = math.sqrt(lrv / n)
    critical_probability = 1.0 - CANDIDATE_C_ALPHA
    critical = float(student_t.ppf(critical_probability, n - 1))
    if not math.isfinite(critical):
        raise ValueError("Candidate C HAC critical value is invalid")
    return CandidateCHACResult(
        n,
        lag,
        CANDIDATE_C_ALPHA,
        critical_probability,
        mean,
        lrv,
        standard_error,
        critical,
        mean - critical * standard_error,
    )


def _metric_gate(metrics: CandidateCMetrics) -> bool:
    return (
        metrics.net_expectancy > 0
        and metrics.annualized_return > 0
        and metrics.sharpe > 0
        and 0 <= metrics.max_drawdown < 1
        and metrics.cost_drag_expectancy >= 0
        and metrics.cost_drag_annualized >= 0
    )


def decide_candidate_c(
    *,
    train: CandidateCSplitResult,
    validation: CandidateCSplitResult,
    not_evaluable_reasons: Sequence[str] = (),
) -> CandidateCDecisionResult:
    unavailable = tuple(sorted(set(str(item) for item in not_evaluable_reasons if str(item))))
    if unavailable:
        return CandidateCDecisionResult(
            CandidateCDecision.NOT_EVALUABLE,
            "NOT_EVALUABLE",
            unavailable,
        )
    reasons: list[str] = []
    if train.headline.net_expectancy <= 0 or train.stress.net_expectancy <= 0:
        reasons.append("train_expectancy_not_strictly_positive")
    if not _metric_gate(validation.headline) or not _metric_gate(validation.stress):
        reasons.append("validation_economic_gate_failed")
    if validation.headline_lcb <= 0 or validation.stress_lcb <= 0:
        reasons.append("validation_lcb_not_strictly_positive")
    for year in (2022, 2023):
        headline = dict(validation.yearly_headline).get(year)
        stress = dict(validation.yearly_stress).get(year)
        if headline is None or stress is None or headline <= 0 or stress <= 0:
            reasons.append(f"validation_{year}_not_strictly_positive")
    positive_both = {
        pair
        for pair in CANDIDATE_C_PAIRS
        if dict(validation.pair_headline).get(pair, 0.0) > 0
        and dict(validation.pair_stress).get(pair, 0.0) > 0
    }
    if len(positive_both) < 2:
        reasons.append("fewer_than_two_positive_pair_contributions")
    for label, split in (("train", train), ("validation", validation)):
        for scenario, metrics in (("headline", split.headline), ("stress", split.stress)):
            if not (0 <= metrics.max_drawdown < 1):
                reasons.append(f"{label}_{scenario}_drawdown_invalid")
            if metrics.cost_drag_expectancy < 0 or metrics.cost_drag_annualized < 0:
                reasons.append(f"{label}_{scenario}_cost_drag_negative")
    if reasons:
        return CandidateCDecisionResult(
            CandidateCDecision.NO_GO,
            "NO_GO",
            tuple(dict.fromkeys(reasons)),
        )
    return CandidateCDecisionResult(
        CandidateCDecision.GO,
        "GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST",
        (),
    )


def build_candidate_c_run_id(
    *,
    policy: CandidateCPolicy,
    code_environment: CandidateCCodeEnvironment,
    dataset_semantics: Sequence[Sequence[object]],
    execution_manifest_id: str,
    execution_record_ids: Sequence[str],
    execution_state_counts: Sequence[tuple[str, int]],
    audit_context: Mapping[str, object] | None = None,
) -> str:
    expected_policy = build_candidate_c_policy()
    if policy != expected_policy or policy.policy_id != expected_policy.policy_id:
        raise ValueError("wrong Candidate C frozen policy")
    if not code_environment.worktree_clean:
        raise ValueError("Candidate C measurement requires a clean worktree")
    semantics = tuple(tuple(item) for item in dataset_semantics)
    if tuple(item[0] for item in semantics) != CANDIDATE_C_PAIRS:
        raise ValueError("Candidate C dataset semantics must use canonical seven-pair order")
    if not _SHA_RE.fullmatch(execution_manifest_id) or any(
        not _SHA_RE.fullmatch(item) for item in execution_record_ids
    ):
        raise ValueError("Candidate C execution identities are invalid")
    del audit_context
    return canonical_sha256(
        {
            "schema": "candidate_c_measurement_run.v1",
            "policy_id": policy.policy_id,
            "code_commit": code_environment.commit,
            "datasets": semantics,
            "execution_manifest_id": execution_manifest_id,
            "execution_record_ids": tuple(execution_record_ids),
            "execution_state_counts": tuple(execution_state_counts),
        }
    )


def _verify_execution_manifest(manifest: CandidateCExecutionEvidenceManifest) -> None:
    if not isinstance(manifest, CandidateCExecutionEvidenceManifest):
        raise ValueError("typed Candidate C execution manifest is required")
    if (
        manifest.schema_version != CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA
        or manifest.start != CANDIDATE_C_START
        or manifest.end != CANDIDATE_C_END
        or manifest.pairs != CANDIDATE_C_PAIRS
    ):
        raise ValueError("Candidate C execution manifest scope is invalid")
    expected_order = tuple(
        sorted(
            manifest.records,
            key=lambda item: (
                CANDIDATE_C_PAIRS.index(item.pair),
                item.intended_boundary,
            ),
        )
    )
    if manifest.records != expected_order:
        raise ValueError("Candidate C execution records are not canonically ordered")
    expected_keys = tuple(
        (pair, boundary.to_pydatetime())
        for pair in CANDIDATE_C_PAIRS
        for boundary in pd.date_range(
            CANDIDATE_C_START,
            CANDIDATE_C_END,
            inclusive="left",
            freq="D",
            tz="UTC",
        )
    )
    if tuple((record.pair, record.intended_boundary) for record in manifest.records) != (
        expected_keys
    ):
        raise ValueError("Candidate C execution manifest coverage is incomplete")
    for record in manifest.records:
        if (
            not isinstance(record, CandidateCExecutionEvidence)
            or record.schema_version != CANDIDATE_C_EXECUTION_SCHEMA
            or record.evidence_id != _sha(_record_payload(record))
            or record.intended_boundary < CANDIDATE_C_START
            or record.intended_boundary >= CANDIDATE_C_END
            or record.window_start != record.intended_boundary
            or record.window_end != record.intended_boundary + timedelta(hours=1)
            or record.source_reference != CANDIDATE_C_EXECUTION_SOURCE_REFERENCE
            or record.decoder_version != CANDIDATE_C_EXECUTION_DECODER_VERSION
        ):
            raise ValueError("Candidate C execution record identity is invalid")
        if record.state is CandidateCExecutionState.AVAILABLE:
            _available_quote(record)
            if (
                record.raw_sha256 is None
                or not _SHA_RE.fullmatch(record.raw_sha256)
                or record.raw_byte_count is None
                or record.raw_byte_count <= 0
                or record.selected_tick_timestamp is None
                or not (
                    record.window_start
                    <= record.selected_tick_timestamp
                    < record.window_end
                )
            ):
                raise ValueError("Candidate C AVAILABLE evidence is invalid")
    recomputed_counts = tuple(
        sorted(Counter(record.state.value for record in manifest.records).items())
    )
    payload = _manifest_payload(
        manifest.start, manifest.end, manifest.pairs, manifest.records, manifest.state_counts
    )
    if (
        manifest.manifest_id != _sha(payload)
        or manifest.total_count != len(manifest.records)
        or manifest.state_counts != recomputed_counts
        or manifest.available_count
        != sum(record.state is CandidateCExecutionState.AVAILABLE for record in manifest.records)
    ):
        raise ValueError("Candidate C execution manifest identity is invalid")


def _dataset_semantics(
    datasets: Mapping[str, BarDataset],
) -> tuple[tuple[object, ...], ...]:
    if (
        tuple(pair for pair in CANDIDATE_C_PAIRS if pair in datasets) != CANDIDATE_C_PAIRS
        or len(datasets) != 7
    ):
        raise ValueError("all seven Candidate C Direct-D1 datasets are required")
    semantics: list[tuple[object, ...]] = []
    expected_index = pd.date_range(
        CANDIDATE_C_START, CANDIDATE_C_END, inclusive="left", freq="D", tz="UTC"
    )
    for pair in CANDIDATE_C_PAIRS:
        dataset = datasets[pair]
        provenance = dataset.provenance
        frame = dataset.frame
        if (
            dataset.query.instrument.symbol != pair
            or dataset.query.timeframe != "D1"
            or dataset.query.start != CANDIDATE_C_START
            or dataset.query.end != CANDIDATE_C_END
            or dataset.query.as_of > CANDIDATE_C_END
            or provenance.provider_id != DIRECT_D1_PROVIDER_ID
            or provenance.provider_version != DIRECT_D1_PROVIDER_VERSION
            or provenance.normalization_version != DIRECT_D1_NORMALIZATION_VERSION
            or provenance.sanitized_source_reference != DIRECT_D1_SOURCE_REFERENCE
            or provenance.provenance_quality is not ProvenanceQuality.VERIFIED
            or not frame.index.equals(expected_index)
        ):
            raise ValueError("Candidate C Direct-D1 dataset contract is invalid")
        semantics.append(
            (
                pair,
                provenance.dataset_id,
                provenance.revision,
                provenance.content_hash,
                provenance.query_fingerprint,
                provenance.provider_id,
                provenance.provider_version,
                provenance.normalization_version,
            )
        )
    return tuple(semantics)


def _available_quote(record: CandidateCExecutionEvidence) -> tuple[float, float]:
    if (
        record.state is not CandidateCExecutionState.AVAILABLE
        or record.bid is None
        or record.ask is None
        or not math.isfinite(record.bid)
        or not math.isfinite(record.ask)
        or record.bid <= 0
        or record.ask < record.bid
    ):
        raise ValueError("Candidate C AVAILABLE quote is invalid")
    return record.bid, record.ask


def _split_result(
    *,
    gross_daily: np.ndarray,
    headline_daily: np.ndarray,
    stress_daily: np.ndarray,
    gross_cohorts: list[float],
    headline_cohorts: list[float],
    stress_cohorts: list[float],
    non_entry_counts: Counter[str],
    yearly_headline: Mapping[int, list[float]],
    yearly_stress: Mapping[int, list[float]],
    pair_headline: Mapping[str, float],
    pair_stress: Mapping[str, float],
) -> CandidateCSplitResult:
    reason_counts = tuple(sorted(non_entry_counts.items()))
    trade_count = 4 * len(headline_cohorts)
    headline_metrics = compute_candidate_c_metrics(
        gross_daily=gross_daily,
        net_daily=headline_daily,
        completed_cohort_returns=headline_cohorts,
        gross_cohort_returns=gross_cohorts,
        trade_count=trade_count,
        non_entry_reason_counts=reason_counts,
    )
    stress_metrics = compute_candidate_c_metrics(
        gross_daily=gross_daily,
        net_daily=stress_daily,
        completed_cohort_returns=stress_cohorts,
        gross_cohort_returns=gross_cohorts,
        trade_count=trade_count,
        non_entry_reason_counts=reason_counts,
    )
    headline_hac = candidate_c_newey_west(headline_daily)
    stress_hac = candidate_c_newey_west(stress_daily)
    return CandidateCSplitResult(
        headline=headline_metrics,
        stress=stress_metrics,
        headline_lcb=headline_hac.lower_confidence_bound,
        stress_lcb=stress_hac.lower_confidence_bound,
        yearly_headline=tuple(
            (year, float(np.mean(values))) for year, values in sorted(yearly_headline.items())
        ),
        yearly_stress=tuple(
            (year, float(np.mean(values))) for year, values in sorted(yearly_stress.items())
        ),
        pair_headline=tuple(
            (pair, float(pair_headline.get(pair, 0.0))) for pair in CANDIDATE_C_PAIRS
        ),
        pair_stress=tuple(
            (pair, float(pair_stress.get(pair, 0.0))) for pair in CANDIDATE_C_PAIRS
        ),
    )


def _result(
    *,
    policy: CandidateCPolicy,
    run_id: str,
    decision: CandidateCDecisionResult,
    train: CandidateCSplitResult | None,
    validation: CandidateCSplitResult | None,
    state_counts: tuple[tuple[str, int], ...],
) -> CandidateCMeasurementResult:
    payload = {
        "schema": "candidate_c_measurement_result.v1",
        "policy_id": policy.policy_id,
        "run_id": run_id,
        "decision": decision,
        "train": train,
        "validation": validation,
        "execution_state_counts": state_counts,
    }
    return CandidateCMeasurementResult(
        "candidate_c_measurement_result.v1",
        policy.policy_id,
        run_id,
        decision.decision,
        decision.meaning,
        decision.reasons,
        train,
        validation,
        state_counts,
        canonical_sha256(payload),
    )


def measure_candidate_c(
    *,
    datasets: Mapping[str, BarDataset],
    execution_manifest: CandidateCExecutionEvidenceManifest,
    code_environment: CandidateCCodeEnvironment,
) -> CandidateCMeasurementResult:
    """Measure frozen Candidate C v1 from already-validated, sealed typed evidence."""
    policy = build_candidate_c_policy()
    _verify_execution_manifest(execution_manifest)
    semantics = _dataset_semantics(datasets)
    run_id = build_candidate_c_run_id(
        policy=policy,
        code_environment=code_environment,
        dataset_semantics=semantics,
        execution_manifest_id=execution_manifest.manifest_id,
        execution_record_ids=tuple(record.evidence_id for record in execution_manifest.records),
        execution_state_counts=execution_manifest.state_counts,
    )
    lookup = {
        (record.pair, record.intended_boundary): record for record in execution_manifest.records
    }
    index = datasets[CANDIDATE_C_PAIRS[0]].frame.index
    frames = {pair: datasets[pair].frame for pair in CANDIDATE_C_PAIRS}
    split_dates = {
        "train": pd.date_range(
            CANDIDATE_C_START,
            CANDIDATE_C_TRAIN_END,
            inclusive="left",
            freq="D",
            tz="UTC",
        ),
        "validation": pd.date_range(
            CANDIDATE_C_TRAIN_END,
            CANDIDATE_C_END,
            inclusive="left",
            freq="D",
            tz="UTC",
        ),
    }
    daily = {
        name: {
            "gross": np.zeros(len(dates), dtype=np.float64),
            "headline": np.zeros(len(dates), dtype=np.float64),
            "stress": np.zeros(len(dates), dtype=np.float64),
        }
        for name, dates in split_dates.items()
    }
    date_locations = {
        name: {timestamp.to_pydatetime(): offset for offset, timestamp in enumerate(dates)}
        for name, dates in split_dates.items()
    }
    cohorts = {
        name: {"gross": [], "headline": [], "stress": []} for name in split_dates
    }
    non_entries = {name: Counter() for name in split_dates}
    yearly_headline: dict[str, defaultdict[int, list[float]]] = {
        name: defaultdict(list) for name in split_dates
    }
    yearly_stress: dict[str, defaultdict[int, list[float]]] = {
        name: defaultdict(list) for name in split_dates
    }
    pair_headline = {name: defaultdict(float) for name in split_dates}
    pair_stress = {name: defaultdict(float) for name in split_dates}
    not_evaluable: list[str] = []

    for position in range(5, len(index)):
        signal_at = (index[position] + pd.Timedelta(days=1)).to_pydatetime()
        if signal_at >= CANDIDATE_C_END:
            break
        if not validate_candidate_c_signal_boundary(signal_at):
            continue
        split = "train" if signal_at < CANDIDATE_C_TRAIN_END else "validation"
        scores = {
            pair: candidate_c_score(
                pair,
                frames[pair]["close"].iloc[position - 5 : position + 1].to_numpy(),
                volumes=frames[pair]["volume"].iloc[position - 5 : position + 1].to_numpy(),
            )
            for pair in CANDIDATE_C_PAIRS
        }
        selected = select_candidate_c_weights(scores)
        if selected is None:
            non_entries[split]["selection_boundary_tie"] += 1
            continue
        entry_records = tuple(lookup.get((pair, signal_at)) for pair in CANDIDATE_C_PAIRS)
        if any(record is None for record in entry_records):
            not_evaluable.append("missing_local_partition")
            break
        entry_states = tuple(record.state for record in entry_records if record is not None)
        disposition = candidate_c_entry_disposition(entry_states)
        if disposition is CandidateCEntryDisposition.NOT_EVALUABLE:
            not_evaluable.extend(
                record.state.value
                for record in entry_records
                if record is not None and record.state is not CandidateCExecutionState.AVAILABLE
            )
            break
        if disposition is CandidateCEntryDisposition.NON_ENTRY:
            for record in entry_records:
                if record is not None and record.state is not CandidateCExecutionState.AVAILABLE:
                    non_entries[split][record.state.value] += 1
            continue
        exit_at = signal_at + timedelta(days=1)
        exit_records = tuple(lookup.get((pair, exit_at)) for pair in CANDIDATE_C_PAIRS)
        if any(record is None for record in exit_records):
            not_evaluable.append("missing_local_partition_after_entry")
            break
        exit_states = tuple(record.state for record in exit_records if record is not None)
        if (
            candidate_c_entry_disposition(exit_states, after_entry=True)
            is not CandidateCEntryDisposition.ENTER
        ):
            not_evaluable.extend(
                f"exit_{record.state.value}"
                for record in exit_records
                if record is not None and record.state is not CandidateCExecutionState.AVAILABLE
            )
            break
        gross_return = headline_return = stress_return = 0.0
        for pair, weight in selected:
            entry_record = lookup[(pair, signal_at)]
            exit_record = lookup[(pair, exit_at)]
            entry_bid, entry_ask = _available_quote(entry_record)
            exit_bid, exit_ask = _available_quote(exit_record)
            foreign_side = 1 if weight > 0 else -1
            headline_leg = candidate_c_leg_accounting(
                pair=pair,
                foreign_side=foreign_side,
                entry_bid=entry_bid,
                entry_ask=entry_ask,
                exit_bid=exit_bid,
                exit_ask=exit_ask,
                factor=1.0,
                pre_entry_equity=1.0,
            )
            stress_leg = candidate_c_leg_accounting(
                pair=pair,
                foreign_side=foreign_side,
                entry_bid=entry_bid,
                entry_ask=entry_ask,
                exit_bid=exit_bid,
                exit_ask=exit_ask,
                factor=1.5,
                pre_entry_equity=1.0,
            )
            gross_return += headline_leg.gross_contribution
            headline_return += headline_leg.net_contribution
            stress_return += stress_leg.net_contribution
            pair_headline[split][pair] += headline_leg.net_contribution
            pair_stress[split][pair] += stress_leg.net_contribution
        day_offset = date_locations[split].get(exit_at)
        if day_offset is None:
            not_evaluable.append("cohort_exit_outside_split")
            break
        daily[split]["gross"][day_offset] = gross_return
        daily[split]["headline"][day_offset] = headline_return
        daily[split]["stress"][day_offset] = stress_return
        cohorts[split]["gross"].append(gross_return)
        cohorts[split]["headline"].append(headline_return)
        cohorts[split]["stress"].append(stress_return)
        yearly_headline[split][exit_at.year].append(headline_return)
        yearly_stress[split][exit_at.year].append(stress_return)

    if not_evaluable:
        decision = CandidateCDecisionResult(
            CandidateCDecision.NOT_EVALUABLE,
            "NOT_EVALUABLE",
            tuple(sorted(set(not_evaluable))),
        )
        return _result(
            policy=policy,
            run_id=run_id,
            decision=decision,
            train=None,
            validation=None,
            state_counts=execution_manifest.state_counts,
        )
    try:
        results = {
            name: _split_result(
                gross_daily=daily[name]["gross"],
                headline_daily=daily[name]["headline"],
                stress_daily=daily[name]["stress"],
                gross_cohorts=cohorts[name]["gross"],
                headline_cohorts=cohorts[name]["headline"],
                stress_cohorts=cohorts[name]["stress"],
                non_entry_counts=non_entries[name],
                yearly_headline=yearly_headline[name],
                yearly_stress=yearly_stress[name],
                pair_headline=pair_headline[name],
                pair_stress=pair_stress[name],
            )
            for name in ("train", "validation")
        }
    except ValueError as exc:
        decision = CandidateCDecisionResult(
            CandidateCDecision.NOT_EVALUABLE,
            "NOT_EVALUABLE",
            (str(exc),),
        )
        return _result(
            policy=policy,
            run_id=run_id,
            decision=decision,
            train=None,
            validation=None,
            state_counts=execution_manifest.state_counts,
        )
    decision = decide_candidate_c(train=results["train"], validation=results["validation"])
    return _result(
        policy=policy,
        run_id=run_id,
        decision=decision,
        train=results["train"],
        validation=results["validation"],
        state_counts=execution_manifest.state_counts,
    )


def canonical_candidate_c_result(result: object) -> bytes:
    return canonical_json(result).encode("utf-8")


__all__ = [
    "CANDIDATE_C_ADR_SHA256",
    "CANDIDATE_C_PAIRS",
    "CandidateCCodeEnvironment",
    "CandidateCDecision",
    "CandidateCDecisionResult",
    "CandidateCEntryDisposition",
    "CandidateCHACResult",
    "CandidateCLegAccounting",
    "CandidateCMeasurementResult",
    "CandidateCMetrics",
    "CandidateCPolicy",
    "CandidateCSplitResult",
    "build_candidate_c_policy",
    "build_candidate_c_run_id",
    "candidate_c_entry_disposition",
    "candidate_c_fill",
    "candidate_c_leg_accounting",
    "candidate_c_newey_west",
    "candidate_c_score",
    "canonical_candidate_c_result",
    "compute_candidate_c_metrics",
    "decide_candidate_c",
    "measure_candidate_c",
    "select_candidate_c_weights",
    "validate_candidate_c_signal_boundary",
]
