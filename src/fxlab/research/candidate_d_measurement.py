"""Frozen Candidate D v1 measurement engine.

This module is pure, deterministic research accounting. It has no filesystem
discovery, network, broker, MT5, order, or trading authority. Inputs must already
satisfy FXLab's typed Direct-D1 and 00h execution-evidence contracts.
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
    CANDIDATE_C_EXECUTION_SCHEMA,
    CANDIDATE_C_EXECUTION_SOURCE_REFERENCE,
    CandidateCExecutionEvidence,
    CandidateCExecutionEvidenceManifest,
    CandidateCExecutionState,
    _manifest_payload,
    _record_payload,
    _sha,
)

CANDIDATE_D_ADR_SHA256 = "36a4fa7c2c8b88cce48b0dd0162177e991d5ca26a3a96599ec7cbc9bcb653e52"
CANDIDATE_D_PREREGISTRATION_COMMIT = "d0e77b7368e9862f78563892632144035800c2fa"
CANDIDATE_D_PROTOCOL_ID = "candidate_d_time_series_momentum.v1"
CANDIDATE_D_PAIRS = (
    "AUDUSD",
    "EURUSD",
    "GBPUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
)
CANDIDATE_D_START = datetime(2014, 1, 1, tzinfo=UTC)
CANDIDATE_D_TRAIN_END = datetime(2022, 1, 1, tzinfo=UTC)
CANDIDATE_D_END = datetime(2024, 1, 1, tzinfo=UTC)
CANDIDATE_D_EARLIEST_ENTRY = datetime(2014, 1, 22, tzinfo=UTC)
CANDIDATE_D_ANNUALIZATION = 365.2425
CANDIDATE_D_ALPHA = 0.00625
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class CandidateDDecision(StrEnum):
    GO = "GO"
    NO_GO = "NO_GO"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class CandidateDEntryDisposition(StrEnum):
    ENTER = "enter"
    NON_ENTRY = "non_entry"
    NOT_EVALUABLE = "not_evaluable"


@dataclass(frozen=True)
class CandidateDPolicy:
    schema: str = "candidate_d_measurement_policy.v1"
    protocol_id: str = CANDIDATE_D_PROTOCOL_ID
    adr_path: str = "docs/adr/0011-candidate-d-time-series-momentum-preregistration.md"
    adr_sha256: str = CANDIDATE_D_ADR_SHA256
    preregistration_commit: str = CANDIDATE_D_PREREGISTRATION_COMMIT
    pairs: tuple[str, ...] = CANDIDATE_D_PAIRS
    lookback_intervals: int = 20
    required_closes: int = 21
    holding_sessions: int = 20
    maximum_calendar_days: int = 40
    allocation_divisor: int = 7
    headline_factor: float = 1.0
    stress_factor: float = 1.5
    slippage_pips_per_side: float = 0.2
    commission_usd_per_lot_per_side: float = 3.5
    standard_lot_base_units: int = 100_000
    annualization_days: float = CANDIDATE_D_ANNUALIZATION
    alpha: float = CANDIDATE_D_ALPHA
    performance_test_index: int = 8
    policy_id: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        expected = canonical_sha256(_policy_payload(self))
        if self.policy_id and self.policy_id != expected:
            raise ValueError("policy_id does not match Candidate D policy")
        object.__setattr__(self, "policy_id", expected)


def _policy_payload(policy: CandidateDPolicy) -> dict[str, object]:
    return {
        "schema": policy.schema,
        "protocol_id": policy.protocol_id,
        "adr_path": policy.adr_path,
        "adr_sha256": policy.adr_sha256,
        "preregistration_commit": policy.preregistration_commit,
        "universe": policy.pairs,
        "windows": (CANDIDATE_D_START, CANDIDATE_D_TRAIN_END, CANDIDATE_D_END),
        "signal": (
            policy.lookback_intervals,
            policy.required_closes,
            "log_close_ratio_native_pair_orientation",
            "zero_volume_structurally_valid_rows_included",
        ),
        "causal_timing": (
            "bar_open_plus_24h_signal_known_and_entry_boundary",
            CANDIDATE_D_EARLIEST_ENTRY,
            "first_valid_tick_in_00h_partition",
        ),
        "entry": (
            "pair_independent",
            "available_enters",
            "empty_or_absent_non_entry",
            "no_valid_invalid_missing_not_evaluable",
            "no_fallback",
        ),
        "holding_exit": (
            policy.holding_sessions,
            policy.maximum_calendar_days,
            "available_increments_after_entry",
            "empty_or_absent_does_not_increment",
            "same_boundary_exit_signal_entry",
        ),
        "sizing": (
            policy.allocation_divisor,
            "post_exit_pre_entry_equity_snapshot",
            "fixed_usd_notional_until_exit",
            "canonical_exit_order",
        ),
        "cost": (
            policy.headline_factor,
            policy.stress_factor,
            policy.slippage_pips_per_side,
            policy.commission_usd_per_lot_per_side,
            policy.standard_lot_base_units,
            "observed_spread_adverse_fill_once",
            "native_pair_usd_valuation",
        ),
        "daily_mtm": (
            "execution_evidence_controls_fills",
            "direct_d1_close_controls_daily_valuation",
            "entry_intermediate_exit_costs_once",
            "independent_headline_stress_equity_paths",
        ),
        "metrics": (
            policy.annualization_days,
            "complete_calendar_day_returns",
            "sample_std_ddof_1",
            "gross_net_expectancy_return_sharpe_maxdd_cost_drag",
        ),
        "statistics": (
            "intercept_only_newey_west_bartlett",
            "automatic_lag_floor_4_n_over_100_pow_2_over_9",
            policy.alpha,
            policy.performance_test_index,
            "student_t_one_sided",
            "no_randomness",
        ),
        "decision": "adr_0011_section_11_exact",
    }


def build_candidate_d_policy() -> CandidateDPolicy:
    return CandidateDPolicy()


@dataclass(frozen=True)
class CandidateDCodeEnvironment:
    commit: str
    worktree_clean: bool

    def __post_init__(self) -> None:
        if not isinstance(self.commit, str) or not _COMMIT_RE.fullmatch(self.commit):
            raise ValueError("code commit must be a lowercase 40-character Git hash")
        if not isinstance(self.worktree_clean, bool):
            raise ValueError("worktree_clean must be boolean")


@dataclass(frozen=True)
class CandidateDExitResolution:
    status: str
    exit_boundary: datetime | None
    available_sessions: int
    reason: str


@dataclass(frozen=True)
class CandidateDMetrics:
    trade_count: int
    per_pair_trade_counts: tuple[tuple[str, int], ...]
    non_entry_reason_counts: tuple[tuple[str, int], ...]
    gross_return: float
    net_return: float
    gross_expectancy: float
    net_expectancy: float
    gross_annualized_return: float
    annualized_return: float
    sharpe: float
    max_drawdown: float
    cost_drag_return: float
    cost_drag_expectancy: float
    pair_contributions: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class CandidateDHACResult:
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
class CandidateDSplitResult:
    headline: CandidateDMetrics
    stress: CandidateDMetrics
    headline_lcb: float
    stress_lcb: float
    yearly_headline: tuple[tuple[int, float], ...]
    yearly_stress: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class CandidateDDecisionResult:
    decision: CandidateDDecision
    meaning: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CandidateDMeasurementResult:
    schema: str
    policy_id: str
    run_id: str
    decision: CandidateDDecision
    decision_meaning: str
    reasons: tuple[str, ...]
    train: CandidateDSplitResult | None
    validation: CandidateDSplitResult | None
    execution_state_counts: tuple[tuple[str, int], ...]
    result_id: str


def candidate_d_signal(
    pair: str,
    closes: Sequence[float],
    *,
    volumes: Sequence[float] | None = None,
) -> int:
    if pair not in CANDIDATE_D_PAIRS:
        raise ValueError("unsupported Candidate D pair")
    if len(closes) != 21:
        raise ValueError("Candidate D signal requires exactly 21 causal closes")
    window = tuple(float(value) for value in closes)
    if any(not math.isfinite(value) or value <= 0 for value in window):
        raise ValueError("Candidate D closes must be finite and positive")
    if volumes is not None:
        if len(volumes) != 21:
            raise ValueError("Candidate D signal requires exactly 21 causal volumes")
        selected = tuple(float(value) for value in volumes)
        if any(not math.isfinite(value) or value < 0 for value in selected):
            raise ValueError("Candidate D volumes must be finite and nonnegative")
    raw_return = math.log(window[-1] / window[0])
    return 1 if raw_return > 0 else -1 if raw_return < 0 else 0


def candidate_d_entry_boundary(bar_open: datetime) -> datetime:
    if not isinstance(bar_open, datetime) or bar_open.tzinfo is None:
        raise ValueError("bar timestamp must be timezone-aware")
    value = bar_open.astimezone(UTC)
    if value.hour or value.minute or value.second or value.microsecond:
        raise ValueError("Candidate D bars require UTC midnight")
    boundary = value + timedelta(days=1)
    if boundary < CANDIDATE_D_EARLIEST_ENTRY:
        raise ValueError("Candidate D entry precedes earliest eligible boundary")
    if boundary >= CANDIDATE_D_END:
        raise ValueError("Candidate D sealed window violation")
    return boundary


def candidate_d_entry_disposition(
    state: CandidateCExecutionState,
) -> CandidateDEntryDisposition:
    if not isinstance(state, CandidateCExecutionState):
        raise ValueError("typed Candidate D execution state is required")
    if state is CandidateCExecutionState.AVAILABLE:
        return CandidateDEntryDisposition.ENTER
    if state in {
        CandidateCExecutionState.EMPTY_EVIDENCED,
        CandidateCExecutionState.ABSENT_EVIDENCED,
    }:
        return CandidateDEntryDisposition.NON_ENTRY
    return CandidateDEntryDisposition.NOT_EVALUABLE


def resolve_candidate_d_exit(
    *,
    pair: str,
    entry_boundary: datetime,
    split_end: datetime,
    evidence_lookup: Mapping[tuple[str, datetime], CandidateCExecutionEvidence],
) -> CandidateDExitResolution:
    if pair not in CANDIDATE_D_PAIRS:
        raise ValueError("unsupported Candidate D pair")
    if entry_boundary.tzinfo is None or split_end.tzinfo is None:
        raise ValueError("Candidate D boundaries must be timezone-aware")
    entry = entry_boundary.astimezone(UTC)
    end = split_end.astimezone(UTC)
    if entry < CANDIDATE_D_START or entry >= CANDIDATE_D_END or end > CANDIDATE_D_END:
        raise ValueError("Candidate D sealed window violation")
    available_sessions = 0
    for day in range(1, 41):
        boundary = entry + timedelta(days=day)
        if boundary >= end:
            return CandidateDExitResolution("purged", None, available_sessions, "split_purge")
        record = evidence_lookup.get((pair, boundary))
        if record is None:
            return CandidateDExitResolution(
                "not_evaluable",
                None,
                available_sessions,
                CandidateCExecutionState.MISSING_LOCAL_PARTITION.value,
            )
        state = record.state
        if state is CandidateCExecutionState.AVAILABLE:
            available_sessions += 1
            if available_sessions == 20:
                return CandidateDExitResolution("found", boundary, 20, "")
        elif state in {
            CandidateCExecutionState.EMPTY_EVIDENCED,
            CandidateCExecutionState.ABSENT_EVIDENCED,
        }:
            continue
        else:
            return CandidateDExitResolution(
                "not_evaluable", None, available_sessions, state.value
            )
    return CandidateDExitResolution(
        "not_evaluable", None, available_sessions, "exit_horizon_exceeded"
    )


def _pip_size(pair: str) -> float:
    if pair not in CANDIDATE_D_PAIRS:
        raise ValueError("unsupported Candidate D pair")
    return 0.01 if pair == "USDJPY" else 0.0001


def candidate_d_fill(
    pair: str,
    bid: float,
    ask: float,
    *,
    side: int,
    factor: float,
) -> float:
    bid, ask, factor = float(bid), float(ask), float(factor)
    if (
        side not in (-1, 1)
        or factor not in (1.0, 1.5)
        or not all(math.isfinite(value) and value > 0 for value in (bid, ask))
        or bid > ask
    ):
        raise ValueError("Candidate D executable quote is invalid")
    mid, half = (bid + ask) / 2.0, (ask - bid) / 2.0
    scenario_bid, scenario_ask = mid - factor * half, mid + factor * half
    slippage = factor * 0.2 * _pip_size(pair)
    fill = scenario_ask + slippage if side == 1 else scenario_bid - slippage
    if not math.isfinite(fill) or fill <= 0:
        raise ValueError("Candidate D fill is invalid")
    return fill


def candidate_d_base_units(pair: str, notional_usd: float, entry_fill: float) -> float:
    if pair not in CANDIDATE_D_PAIRS:
        raise ValueError("unsupported Candidate D pair")
    notional, fill = float(notional_usd), float(entry_fill)
    if not math.isfinite(notional) or notional <= 0 or not math.isfinite(fill) or fill <= 0:
        raise ValueError("Candidate D position sizing inputs are invalid")
    return notional if pair.startswith("USD") else notional / fill


def candidate_d_commission_usd(base_units: float, *, sides: int = 1) -> float:
    units = float(base_units)
    if not math.isfinite(units) or units <= 0 or sides not in (1, 2):
        raise ValueError("Candidate D commission inputs are invalid")
    return 3.5 * sides * units / 100_000.0


def candidate_d_price_pnl_usd(
    pair: str,
    side: int,
    base_units: float,
    entry_price: float,
    from_price: float,
    to_price: float,
) -> float:
    if pair not in CANDIDATE_D_PAIRS or side not in (-1, 1):
        raise ValueError("Candidate D position orientation is invalid")
    units, entry, start, end = (
        float(base_units),
        float(entry_price),
        float(from_price),
        float(to_price),
    )
    if not all(
        math.isfinite(value) and value > 0 for value in (units, entry, start, end)
    ):
        raise ValueError("Candidate D valuation inputs are invalid")
    if not pair.startswith("USD"):
        return side * units * (end - start)

    def cumulative_value(mark: float) -> float:
        return side * units * (mark - entry) / mark

    return cumulative_value(end) - cumulative_value(start)


def _annualized_return(values: np.ndarray) -> float:
    growth = float(np.prod(1.0 + values))
    if not math.isfinite(growth) or growth <= 0 or not len(values):
        raise ValueError("Candidate D equity must remain finite and positive")
    return growth ** (CANDIDATE_D_ANNUALIZATION / len(values)) - 1.0


def _maximum_drawdown(values: np.ndarray) -> float:
    equity = np.concatenate(([1.0], np.cumprod(1.0 + values)))
    if not np.isfinite(equity).all() or np.any(equity <= 0):
        raise ValueError("Candidate D equity must remain finite and positive")
    peaks = np.maximum.accumulate(equity)
    return float(-np.min(equity / peaks - 1.0))


def compute_candidate_d_metrics(
    *,
    gross_daily: Sequence[float],
    net_daily: Sequence[float],
    gross_trade_returns: Sequence[float],
    net_trade_returns: Sequence[float],
    per_pair_trade_counts: Sequence[tuple[str, int]],
    non_entry_reason_counts: Sequence[tuple[str, int]],
    pair_contributions: Sequence[tuple[str, float]],
) -> CandidateDMetrics:
    gross = np.asarray(gross_daily, dtype=np.float64)
    net = np.asarray(net_daily, dtype=np.float64)
    gross_trades = np.asarray(gross_trade_returns, dtype=np.float64)
    net_trades = np.asarray(net_trade_returns, dtype=np.float64)
    if (
        len(net) < 2
        or len(gross) != len(net)
        or not len(net_trades)
        or len(gross_trades) != len(net_trades)
        or not all(np.isfinite(item).all() for item in (gross, net, gross_trades, net_trades))
    ):
        raise ValueError("Candidate D metric inputs are invalid")
    variance = float(np.var(net, ddof=1))
    if variance <= 0 or not math.isfinite(variance):
        raise ValueError("Candidate D daily return variance is undefined")
    gross_return = float(np.prod(1.0 + gross) - 1.0)
    net_return = float(np.prod(1.0 + net) - 1.0)
    gross_expectancy = float(np.mean(gross_trades))
    net_expectancy = float(np.mean(net_trades))
    gross_annualized = _annualized_return(gross)
    net_annualized = _annualized_return(net)
    metrics = CandidateDMetrics(
        trade_count=len(net_trades),
        per_pair_trade_counts=tuple(per_pair_trade_counts),
        non_entry_reason_counts=tuple(non_entry_reason_counts),
        gross_return=gross_return,
        net_return=net_return,
        gross_expectancy=gross_expectancy,
        net_expectancy=net_expectancy,
        gross_annualized_return=gross_annualized,
        annualized_return=net_annualized,
        sharpe=float(np.mean(net) / math.sqrt(variance) * math.sqrt(CANDIDATE_D_ANNUALIZATION)),
        max_drawdown=_maximum_drawdown(net),
        cost_drag_return=gross_return - net_return,
        cost_drag_expectancy=gross_expectancy - net_expectancy,
        pair_contributions=tuple(pair_contributions),
    )
    numeric = (
        metrics.gross_return,
        metrics.net_return,
        metrics.gross_expectancy,
        metrics.net_expectancy,
        metrics.gross_annualized_return,
        metrics.annualized_return,
        metrics.sharpe,
        metrics.max_drawdown,
        metrics.cost_drag_return,
        metrics.cost_drag_expectancy,
    )
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("Candidate D metrics are nonfinite")
    return metrics


def candidate_d_newey_west(values: Sequence[float]) -> CandidateDHACResult:
    vector = np.asarray(values, dtype=np.float64)
    n = len(vector)
    if n < 2 or not np.isfinite(vector).all():
        raise ValueError("Candidate D HAC inputs are invalid")
    lag = math.floor(4 * (n / 100) ** (2 / 9))
    mean = float(np.mean(vector))
    residuals = vector - mean
    gammas = [
        float(np.dot(residuals[k:], residuals[: n - k]) / n)
        for k in range(lag + 1)
    ]
    long_run_variance = gammas[0] + 2.0 * math.fsum(
        (1.0 - k / (lag + 1)) * gammas[k] for k in range(1, lag + 1)
    )
    if long_run_variance <= 0 or not math.isfinite(long_run_variance):
        raise ValueError("Candidate D HAC inference is undefined")
    standard_error = math.sqrt(long_run_variance / n)
    probability = 1.0 - CANDIDATE_D_ALPHA
    critical = float(student_t.ppf(probability, n - 1))
    if not math.isfinite(critical):
        raise ValueError("Candidate D HAC critical value is invalid")
    return CandidateDHACResult(
        n,
        lag,
        CANDIDATE_D_ALPHA,
        probability,
        mean,
        long_run_variance,
        standard_error,
        critical,
        mean - critical * standard_error,
    )


def decide_candidate_d(
    *,
    train: CandidateDSplitResult,
    validation: CandidateDSplitResult,
    not_evaluable_reasons: Sequence[str] = (),
    integrity_passed: bool = True,
) -> CandidateDDecisionResult:
    unavailable = tuple(sorted(set(str(reason) for reason in not_evaluable_reasons if reason)))
    if not integrity_passed:
        unavailable = tuple(sorted(set((*unavailable, "integrity_failure"))))
    if unavailable:
        return CandidateDDecisionResult(
            CandidateDDecision.NOT_EVALUABLE, "NOT_EVALUABLE", unavailable
        )
    reasons: list[str] = []
    if train.headline.net_expectancy <= 0 or train.stress.net_expectancy <= 0:
        reasons.append("train_expectancy_not_strictly_positive")
    if validation.headline.net_return <= 0 or validation.stress.net_return <= 0:
        reasons.append("validation_net_return_not_strictly_positive")
    if validation.headline.sharpe <= 0 or validation.stress.sharpe <= 0:
        reasons.append("validation_sharpe_not_strictly_positive")
    if validation.headline.net_expectancy <= 0 or validation.stress.net_expectancy <= 0:
        reasons.append("validation_expectancy_not_strictly_positive")
    if validation.headline_lcb <= 0 or validation.stress_lcb <= 0:
        reasons.append("validation_lcb_not_strictly_positive")
    all_metrics = (
        train.headline,
        train.stress,
        validation.headline,
        validation.stress,
    )
    if any(not 0 <= metrics.max_drawdown < 1 for metrics in all_metrics):
        reasons.append("max_drawdown_invalid")
    if any(
        metrics.cost_drag_return < 0 or metrics.cost_drag_expectancy < 0
        for metrics in all_metrics
    ):
        reasons.append("cost_drag_negative")
    for year in (2022, 2023):
        if (
            dict(validation.yearly_headline).get(year, 0.0) <= 0
            or dict(validation.yearly_stress).get(year, 0.0) <= 0
        ):
            reasons.append(f"validation_{year}_not_strictly_positive")
    positive_pairs = {
        pair
        for pair in CANDIDATE_D_PAIRS
        if dict(validation.headline.pair_contributions).get(pair, 0.0) > 0
        and dict(validation.stress.pair_contributions).get(pair, 0.0) > 0
    }
    if len(positive_pairs) < 2:
        reasons.append("fewer_than_two_positive_pair_contributions")
    if reasons:
        return CandidateDDecisionResult(
            CandidateDDecision.NO_GO, "NO_GO", tuple(dict.fromkeys(reasons))
        )
    return CandidateDDecisionResult(
        CandidateDDecision.GO,
        "GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST",
        (),
    )


def build_candidate_d_run_id(
    *,
    policy: CandidateDPolicy,
    code_environment: CandidateDCodeEnvironment,
    dataset_semantics: Sequence[Sequence[object]],
    execution_manifest_id: str,
    execution_record_ids: Sequence[str],
    execution_state_counts: Sequence[tuple[str, int]],
    audit_context: Mapping[str, object] | None = None,
) -> str:
    if policy != build_candidate_d_policy():
        raise ValueError("wrong Candidate D frozen policy")
    if not code_environment.worktree_clean:
        raise ValueError("Candidate D measurement requires a clean worktree")
    semantics = tuple(tuple(item) for item in dataset_semantics)
    if tuple(item[0] for item in semantics) != CANDIDATE_D_PAIRS:
        raise ValueError("Candidate D datasets must use canonical seven-pair order")
    if not _SHA_RE.fullmatch(execution_manifest_id) or any(
        not _SHA_RE.fullmatch(identity) for identity in execution_record_ids
    ):
        raise ValueError("Candidate D execution identities are invalid")
    del audit_context
    return canonical_sha256(
        {
            "schema": "candidate_d_measurement_run.v1",
            "protocol_id": CANDIDATE_D_PROTOCOL_ID,
            "adr_sha256": CANDIDATE_D_ADR_SHA256,
            "policy_id": policy.policy_id,
            "code_commit": code_environment.commit,
            "datasets": semantics,
            "execution_manifest_id": execution_manifest_id,
            "execution_record_ids": tuple(execution_record_ids),
            "execution_state_counts": tuple(execution_state_counts),
        }
    )


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
        raise ValueError("Candidate D AVAILABLE quote is invalid")
    return float(record.bid), float(record.ask)


def _verify_execution_manifest(manifest: CandidateCExecutionEvidenceManifest) -> None:
    if not isinstance(manifest, CandidateCExecutionEvidenceManifest):
        raise ValueError("typed Candidate D execution manifest is required")
    if (
        manifest.schema_version != CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA
        or manifest.start != CANDIDATE_D_START
        or manifest.end != CANDIDATE_D_END
        or manifest.pairs != CANDIDATE_D_PAIRS
    ):
        raise ValueError("Candidate D execution manifest scope is invalid")
    expected_order = tuple(
        sorted(
            manifest.records,
            key=lambda record: (
                CANDIDATE_D_PAIRS.index(record.pair),
                record.intended_boundary,
            ),
        )
    )
    if manifest.records != expected_order:
        raise ValueError("Candidate D execution records are not canonically ordered")
    expected_keys = tuple(
        (pair, boundary.to_pydatetime())
        for pair in CANDIDATE_D_PAIRS
        for boundary in pd.date_range(
            CANDIDATE_D_START,
            CANDIDATE_D_END,
            inclusive="left",
            freq="D",
            tz="UTC",
        )
    )
    if tuple((record.pair, record.intended_boundary) for record in manifest.records) != (
        expected_keys
    ):
        raise ValueError("Candidate D execution manifest coverage is incomplete")
    for record in manifest.records:
        if (
            record.schema_version != CANDIDATE_C_EXECUTION_SCHEMA
            or record.evidence_id != _sha(_record_payload(record))
            or record.intended_boundary < CANDIDATE_D_START
            or record.intended_boundary >= CANDIDATE_D_END
            or record.window_start != record.intended_boundary
            or record.window_end != record.intended_boundary + timedelta(hours=1)
            or record.source_reference != CANDIDATE_C_EXECUTION_SOURCE_REFERENCE
            or record.decoder_version != CANDIDATE_C_EXECUTION_DECODER_VERSION
        ):
            raise ValueError("Candidate D execution record identity is invalid")
        if record.state is CandidateCExecutionState.AVAILABLE:
            _available_quote(record)
            if (
                record.raw_sha256 is None
                or not _SHA_RE.fullmatch(record.raw_sha256)
                or record.raw_byte_count is None
                or record.raw_byte_count <= 0
                or record.selected_tick_timestamp is None
                or not record.window_start
                <= record.selected_tick_timestamp
                < record.window_end
            ):
                raise ValueError("Candidate D AVAILABLE evidence is invalid")
    counts = tuple(sorted(Counter(record.state.value for record in manifest.records).items()))
    payload = _manifest_payload(
        manifest.start, manifest.end, manifest.pairs, manifest.records, manifest.state_counts
    )
    if (
        manifest.manifest_id != _sha(payload)
        or manifest.total_count != len(manifest.records)
        or manifest.state_counts != counts
        or manifest.available_count
        != sum(record.state is CandidateCExecutionState.AVAILABLE for record in manifest.records)
    ):
        raise ValueError("Candidate D execution manifest identity is invalid")


def _dataset_semantics(
    datasets: Mapping[str, BarDataset],
) -> tuple[tuple[object, ...], ...]:
    if tuple(pair for pair in CANDIDATE_D_PAIRS if pair in datasets) != CANDIDATE_D_PAIRS or len(
        datasets
    ) != 7:
        raise ValueError("all seven Candidate D Direct-D1 datasets are required")
    expected_index = pd.date_range(
        CANDIDATE_D_START, CANDIDATE_D_END, inclusive="left", freq="D", tz="UTC"
    )
    semantics: list[tuple[object, ...]] = []
    for pair in CANDIDATE_D_PAIRS:
        dataset = datasets[pair]
        provenance, frame = dataset.provenance, dataset.frame
        if (
            dataset.query.instrument.symbol != pair
            or dataset.query.timeframe != "D1"
            or dataset.query.start != CANDIDATE_D_START
            or dataset.query.end != CANDIDATE_D_END
            or dataset.query.as_of > CANDIDATE_D_END
            or provenance.provider_id != DIRECT_D1_PROVIDER_ID
            or provenance.provider_version != DIRECT_D1_PROVIDER_VERSION
            or provenance.normalization_version != DIRECT_D1_NORMALIZATION_VERSION
            or provenance.sanitized_source_reference != DIRECT_D1_SOURCE_REFERENCE
            or provenance.provenance_quality is not ProvenanceQuality.VERIFIED
            or not frame.index.equals(expected_index)
        ):
            raise ValueError("Candidate D Direct-D1 dataset contract is invalid")
        values = frame[["open", "high", "low", "close", "volume"]].to_numpy(
            dtype=np.float64
        )
        if (
            not np.isfinite(values).all()
            or np.any(values[:, :4] <= 0)
            or np.any(values[:, 4] < 0)
            or np.any(values[:, 1] < np.maximum.reduce((values[:, 0], values[:, 2], values[:, 3])))
            or np.any(values[:, 2] > np.minimum.reduce((values[:, 0], values[:, 1], values[:, 3])))
        ):
            raise ValueError("Candidate D Direct-D1 rows are invalid")
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


@dataclass
class _ScenarioPosition:
    pair: str
    side: int
    exit_boundary: datetime
    entry_price: float
    reference_price: float
    base_units: float
    entry_equity: float
    pending_entry_commission: float
    exit_commission: float
    accumulated_pnl: float = 0.0


@dataclass
class _Scenario:
    factor: float | None
    equity: float = 1.0
    positions: dict[str, _ScenarioPosition] = field(default_factory=dict)
    daily_returns: list[float] = field(default_factory=list)
    trade_returns: list[float] = field(default_factory=list)
    pair_contributions: defaultdict[str, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    yearly_returns: defaultdict[int, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )


def _scenario_price(
    pair: str,
    record: CandidateCExecutionEvidence,
    *,
    transaction_side: int,
    factor: float | None,
) -> float:
    bid, ask = _available_quote(record)
    return (bid + ask) / 2.0 if factor is None else candidate_d_fill(
        pair, bid, ask, side=transaction_side, factor=factor
    )


def _scenario_commission(pair: str, units: float, factor: float | None) -> float:
    del pair
    return 0.0 if factor is None else candidate_d_commission_usd(units)


def _make_split_result(
    gross: _Scenario,
    headline: _Scenario,
    stress: _Scenario,
    *,
    trade_counts: Counter[str],
    non_entries: Counter[str],
) -> CandidateDSplitResult:
    pair_counts = tuple((pair, trade_counts[pair]) for pair in CANDIDATE_D_PAIRS)
    reasons = tuple(sorted(non_entries.items()))
    gross_daily = tuple(gross.daily_returns)
    headline_metrics = compute_candidate_d_metrics(
        gross_daily=gross_daily,
        net_daily=headline.daily_returns,
        gross_trade_returns=gross.trade_returns,
        net_trade_returns=headline.trade_returns,
        per_pair_trade_counts=pair_counts,
        non_entry_reason_counts=reasons,
        pair_contributions=tuple(
            (pair, headline.pair_contributions[pair]) for pair in CANDIDATE_D_PAIRS
        ),
    )
    stress_metrics = compute_candidate_d_metrics(
        gross_daily=gross_daily,
        net_daily=stress.daily_returns,
        gross_trade_returns=gross.trade_returns,
        net_trade_returns=stress.trade_returns,
        per_pair_trade_counts=pair_counts,
        non_entry_reason_counts=reasons,
        pair_contributions=tuple(
            (pair, stress.pair_contributions[pair]) for pair in CANDIDATE_D_PAIRS
        ),
    )
    headline_hac = candidate_d_newey_west(headline.daily_returns)
    stress_hac = candidate_d_newey_west(stress.daily_returns)

    def yearly(scenario: _Scenario) -> tuple[tuple[int, float], ...]:
        return tuple(
            (year, float(np.prod(1.0 + np.asarray(values, dtype=np.float64)) - 1.0))
            for year, values in sorted(scenario.yearly_returns.items())
        )

    return CandidateDSplitResult(
        headline_metrics,
        stress_metrics,
        headline_hac.lower_confidence_bound,
        stress_hac.lower_confidence_bound,
        yearly(headline),
        yearly(stress),
    )


def _measure_split(
    *,
    split_start: datetime,
    split_end: datetime,
    frames: Mapping[str, pd.DataFrame],
    lookup: Mapping[tuple[str, datetime], CandidateCExecutionEvidence],
) -> tuple[CandidateDSplitResult | None, tuple[str, ...]]:
    scenarios = {
        "gross": _Scenario(None),
        "headline": _Scenario(1.0),
        "stress": _Scenario(1.5),
    }
    active_exits: dict[str, datetime] = {}
    trade_counts: Counter[str] = Counter()
    non_entries: Counter[str] = Counter()
    positions_by_timestamp = {
        timestamp.to_pydatetime(): index
        for index, timestamp in enumerate(frames[CANDIDATE_D_PAIRS[0]].index)
    }

    boundary = split_start + timedelta(days=1)
    while boundary <= split_end:
        day_open = boundary - timedelta(days=1)
        day_index = positions_by_timestamp[day_open]

        for scenario in scenarios.values():
            before = scenario.equity
            for pair in CANDIDATE_D_PAIRS:
                position = scenario.positions.get(pair)
                if position is None:
                    continue
                exiting = position.exit_boundary == boundary
                if exiting:
                    record = lookup.get((pair, boundary))
                    if record is None or record.state is not CandidateCExecutionState.AVAILABLE:
                        return None, ("exit_execution_evidence_invalid",)
                    target = _scenario_price(
                        pair,
                        record,
                        transaction_side=-position.side,
                        factor=scenario.factor,
                    )
                else:
                    target = float(frames[pair]["close"].iloc[day_index])
                pnl = candidate_d_price_pnl_usd(
                    pair,
                    position.side,
                    position.base_units,
                    position.entry_price,
                    position.reference_price,
                    target,
                )
                if position.pending_entry_commission:
                    pnl -= position.pending_entry_commission
                    position.pending_entry_commission = 0.0
                if exiting:
                    pnl -= position.exit_commission
                scenario.equity += pnl
                position.accumulated_pnl += pnl
                if exiting:
                    scenario.trade_returns.append(
                        position.accumulated_pnl / position.entry_equity
                    )
                    scenario.pair_contributions[pair] += position.accumulated_pnl
                    del scenario.positions[pair]
                else:
                    position.reference_price = target
            if not math.isfinite(scenario.equity) or scenario.equity <= 0:
                return None, ("nonpositive_or_nonfinite_equity",)
            daily_return = (scenario.equity - before) / before
            scenario.daily_returns.append(daily_return)
            scenario.yearly_returns[day_open.year].append(daily_return)

        exited = tuple(
            pair for pair in CANDIDATE_D_PAIRS if active_exits.get(pair) == boundary
        )
        for pair in exited:
            del active_exits[pair]
            trade_counts[pair] += 1

        if boundary >= split_end:
            break
        if boundary == CANDIDATE_D_TRAIN_END or boundary < CANDIDATE_D_EARLIEST_ENTRY:
            boundary += timedelta(days=1)
            continue

        snapshots = {name: scenario.equity for name, scenario in scenarios.items()}
        entry_candidates: list[tuple[str, int, CandidateCExecutionEvidence, datetime]] = []
        for pair in CANDIDATE_D_PAIRS:
            if pair in active_exits:
                continue
            frame = frames[pair]
            signal = candidate_d_signal(
                pair,
                frame["close"].iloc[day_index - 20 : day_index + 1].to_numpy(),
                volumes=frame["volume"].iloc[day_index - 20 : day_index + 1].to_numpy(),
            )
            if signal == 0:
                non_entries["zero_signal"] += 1
                continue
            record = lookup.get((pair, boundary))
            if record is None:
                return None, (CandidateCExecutionState.MISSING_LOCAL_PARTITION.value,)
            disposition = candidate_d_entry_disposition(record.state)
            if disposition is CandidateDEntryDisposition.NOT_EVALUABLE:
                return None, (record.state.value,)
            if disposition is CandidateDEntryDisposition.NON_ENTRY:
                non_entries[record.state.value] += 1
                continue
            resolution = resolve_candidate_d_exit(
                pair=pair,
                entry_boundary=boundary,
                split_end=split_end,
                evidence_lookup=lookup,
            )
            if resolution.status == "not_evaluable":
                return None, (resolution.reason,)
            if resolution.status == "purged":
                continue
            assert resolution.exit_boundary is not None
            entry_candidates.append((pair, signal, record, resolution.exit_boundary))

        for pair, side, record, exit_boundary in entry_candidates:
            active_exits[pair] = exit_boundary
            for name, scenario in scenarios.items():
                entry_price = _scenario_price(
                    pair, record, transaction_side=side, factor=scenario.factor
                )
                notional = snapshots[name] / 7.0
                units = candidate_d_base_units(pair, notional, entry_price)
                commission = _scenario_commission(pair, units, scenario.factor)
                scenario.positions[pair] = _ScenarioPosition(
                    pair=pair,
                    side=side,
                    exit_boundary=exit_boundary,
                    entry_price=entry_price,
                    reference_price=entry_price,
                    base_units=units,
                    entry_equity=snapshots[name],
                    pending_entry_commission=commission,
                    exit_commission=commission,
                )
        boundary += timedelta(days=1)

    if any(scenario.positions for scenario in scenarios.values()) or active_exits:
        return None, ("split_purge_failed",)
    try:
        return (
            _make_split_result(
                scenarios["gross"],
                scenarios["headline"],
                scenarios["stress"],
                trade_counts=trade_counts,
                non_entries=non_entries,
            ),
            (),
        )
    except ValueError as exc:
        return None, (str(exc),)


def _result(
    *,
    policy: CandidateDPolicy,
    run_id: str,
    decision: CandidateDDecisionResult,
    train: CandidateDSplitResult | None,
    validation: CandidateDSplitResult | None,
    state_counts: tuple[tuple[str, int], ...],
) -> CandidateDMeasurementResult:
    payload = {
        "schema": "candidate_d_measurement_result.v1",
        "policy_id": policy.policy_id,
        "run_id": run_id,
        "decision": decision,
        "train": train,
        "validation": validation,
        "execution_state_counts": state_counts,
    }
    return CandidateDMeasurementResult(
        "candidate_d_measurement_result.v1",
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


def measure_candidate_d(
    *,
    datasets: Mapping[str, BarDataset],
    execution_manifest: CandidateCExecutionEvidenceManifest,
    code_environment: CandidateDCodeEnvironment,
    policy: CandidateDPolicy | None = None,
) -> CandidateDMeasurementResult:
    """Measure frozen Candidate D using only supplied sealed, typed evidence."""
    frozen_policy = policy or build_candidate_d_policy()
    _verify_execution_manifest(execution_manifest)
    semantics = _dataset_semantics(datasets)
    run_id = build_candidate_d_run_id(
        policy=frozen_policy,
        code_environment=code_environment,
        dataset_semantics=semantics,
        execution_manifest_id=execution_manifest.manifest_id,
        execution_record_ids=tuple(record.evidence_id for record in execution_manifest.records),
        execution_state_counts=execution_manifest.state_counts,
    )
    lookup = {
        (record.pair, record.intended_boundary): record
        for record in execution_manifest.records
    }
    frames = {pair: datasets[pair].frame for pair in CANDIDATE_D_PAIRS}
    train, train_reasons = _measure_split(
        split_start=CANDIDATE_D_START,
        split_end=CANDIDATE_D_TRAIN_END,
        frames=frames,
        lookup=lookup,
    )
    if train_reasons:
        decision = CandidateDDecisionResult(
            CandidateDDecision.NOT_EVALUABLE, "NOT_EVALUABLE", train_reasons
        )
        return _result(
            policy=frozen_policy,
            run_id=run_id,
            decision=decision,
            train=None,
            validation=None,
            state_counts=execution_manifest.state_counts,
        )
    validation, validation_reasons = _measure_split(
        split_start=CANDIDATE_D_TRAIN_END,
        split_end=CANDIDATE_D_END,
        frames=frames,
        lookup=lookup,
    )
    if validation_reasons:
        decision = CandidateDDecisionResult(
            CandidateDDecision.NOT_EVALUABLE, "NOT_EVALUABLE", validation_reasons
        )
        return _result(
            policy=frozen_policy,
            run_id=run_id,
            decision=decision,
            train=None,
            validation=None,
            state_counts=execution_manifest.state_counts,
        )
    assert train is not None and validation is not None
    decision = decide_candidate_d(train=train, validation=validation)
    return _result(
        policy=frozen_policy,
        run_id=run_id,
        decision=decision,
        train=train,
        validation=validation,
        state_counts=execution_manifest.state_counts,
    )


def canonical_candidate_d_result(result: object) -> bytes:
    return canonical_json(result).encode("utf-8")


__all__ = [
    "CANDIDATE_D_ADR_SHA256",
    "CANDIDATE_D_EARLIEST_ENTRY",
    "CANDIDATE_D_PAIRS",
    "CANDIDATE_D_PROTOCOL_ID",
    "CandidateDCodeEnvironment",
    "CandidateDDecision",
    "CandidateDDecisionResult",
    "CandidateDEntryDisposition",
    "CandidateDExitResolution",
    "CandidateDHACResult",
    "CandidateDMeasurementResult",
    "CandidateDMetrics",
    "CandidateDPolicy",
    "CandidateDSplitResult",
    "build_candidate_d_policy",
    "build_candidate_d_run_id",
    "candidate_d_base_units",
    "candidate_d_commission_usd",
    "candidate_d_entry_boundary",
    "candidate_d_entry_disposition",
    "candidate_d_fill",
    "candidate_d_newey_west",
    "candidate_d_price_pnl_usd",
    "candidate_d_signal",
    "canonical_candidate_d_result",
    "compute_candidate_d_metrics",
    "decide_candidate_d",
    "measure_candidate_d",
    "resolve_candidate_d_exit",
]
