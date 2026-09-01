"""Deterministic G1 static contract and qualified run identity for Candidate B."""

from __future__ import annotations

import hashlib
import platform
import re
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
import scipy

from fxlab.data.dukascopy_provider import (
    DUKASCOPY_MAPPING_FINGERPRINT,
    DUKASCOPY_SYMBOLS,
    DUKASCOPY_TIMEFRAMES,
)
from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    APPROVED_PAIRS,
    CandidateBFormationManifest,
    CandidateBQualificationResult,
    ConcordanceStatus,
    FormationSplit,
    PolicyConcordanceResult,
    PolicyEventManifest,
    PolicyRateSeriesManifest,
    SpotPanelManifestReference,
    canonical_sha256,
    reconcile_policy_series,
)
from fxlab.data.provider import (
    BarDataset,
    BarQuery,
    CanonicalInstrument,
    DataProvenance,
    ProvenanceQuality,
    dataset_identity,
)
from fxlab.research.candidate_b_measurement import (
    MEASURED_MONTHS,
    PURGED_MONTH,
    TRAIN_MONTHS,
    VALIDATION_MONTHS,
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ADR_SHA256 = "a13f2a79f9890b99c388bd2e59ef1d8ab3a20fa4bffea1575df72dad10680603"
_ADR_COMMITS = (
    "83ba9a2f7792d3cd1cacbd0a684dfcde51bd1bae",
    "95f903fc8c50d1bb5c181e75beae5cff1a45629b",
    "eee70b30456c913dfa94e96d5b6cf3e470b9d4fe",
    "7d073e5d6a06e80ee1efd16f1b6a7b8053b93df4",
)


def _sha(value: str, name: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _bounded(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be bounded non-empty text")
    if any(character.isspace() for character in value.strip()):
        raise ValueError(f"{name} must not contain whitespace")
    return value.strip()


@dataclass(frozen=True)
class StaticIdentityContract:
    schema_version: int
    candidate_id: str
    candidate_version: int
    adr_path: str
    adr_content_sha256: str
    preregistration_commit: str
    clarification_commits: tuple[str, ...]


@dataclass(frozen=True)
class UniverseContract:
    currencies: tuple[str, ...]
    pairs: tuple[str, ...]
    quote_mapping: tuple[tuple[str, str, str], ...]
    reference_currency: str


@dataclass(frozen=True)
class FormationContract:
    first_formation: str
    train_months: tuple[str, ...]
    boundary_purge_month: str
    validation_months: tuple[str, ...]
    train_count: int
    validation_count: int
    measured_total: int
    boundary_purge_count: int
    holding_rule: str
    split_reset_rule: str


@dataclass(frozen=True)
class SignalContract:
    definition: str
    ranking: str
    tie_break: str
    rate_family: str


@dataclass(frozen=True)
class PortfolioContract:
    long_count: int
    short_count: int
    selected_weight: str
    middle_weight: str
    gross_exposure: str
    net_currency_weight: str


@dataclass(frozen=True)
class CostContract:
    one_way_costs: tuple[tuple[str, str], ...]
    headline_multiplier: str
    stress_multiplier: str
    turnover_rule: str
    initial_entry_charged: bool
    terminal_liquidation_rule: str


@dataclass(frozen=True)
class AccountingContract:
    outcome: str
    portfolio_aggregation: str
    initial_equity: str
    compounding: str
    drawdown: str
    concentration: str
    quotation_direction_invariant: str


@dataclass(frozen=True)
class StatisticalContract:
    sharpe: str
    hac: str
    hac_lag: int
    student_t_probability: str
    student_t_df: str
    student_t_implementation: str
    bootstrap_statistic: str
    bootstrap_block_length: int
    bootstrap_replications: int
    bootstrap_seed: int
    bootstrap_rng: str
    bootstrap_quantile: str


@dataclass(frozen=True)
class GateContract:
    minimum_validation_mean: str
    minimum_annualized_sharpe: str
    maximum_drawdown_exclusive: str
    concentration_maximum: str
    inference: str
    robustness: tuple[str, ...]
    stress: tuple[str, ...]


@dataclass(frozen=True)
class BisContract:
    agency: str
    dataflow: str
    version: str
    frequency: str
    series: tuple[tuple[str, str], ...]
    request_start: date
    request_end: date


@dataclass(frozen=True)
class PitContract:
    cutoff: str
    announcement_and_effective_required: bool
    same_day_decisions_excluded: bool
    future_fill_forbidden: bool
    revision_leakage_forbidden: bool
    initialization_history_only_before_first_formation: bool


@dataclass(frozen=True, init=False)
class CandidateBStaticRunContract:
    identity: StaticIdentityContract
    sealed_maximum_date: date
    universe: UniverseContract
    formation: FormationContract
    signal: SignalContract
    portfolio: PortfolioContract
    costs: CostContract
    accounting: AccountingContract
    statistics: StatisticalContract
    gates: GateContract
    bis: BisContract
    pit: PitContract
    multiple_comparison_prohibition: bool
    contract_id: str = field(init=False)

    def __new__(cls) -> CandidateBStaticRunContract:
        raise TypeError("use build_candidate_b_static_run_contract")

    def __post_init__(self) -> None:
        object.__setattr__(self, "contract_id", canonical_sha256(_static_payload(self)))


def _static_payload(contract: CandidateBStaticRunContract) -> dict[str, object]:
    return {
        "schema": "candidate_b_static_run_contract.v1",
        "identity": contract.identity,
        "sealed_maximum_date": contract.sealed_maximum_date,
        "universe": contract.universe,
        "formation": contract.formation,
        "signal": contract.signal,
        "portfolio": contract.portfolio,
        "costs": contract.costs,
        "accounting": contract.accounting,
        "statistics": contract.statistics,
        "gates": contract.gates,
        "bis": contract.bis,
        "pit": contract.pit,
        "multiple_comparison_prohibition": contract.multiple_comparison_prohibition,
    }


def build_candidate_b_static_run_contract() -> CandidateBStaticRunContract:
    """Build the only permitted Candidate B static research contract."""
    values = dict(
        identity=StaticIdentityContract(
            1,
            "candidate_b_public_policy_rate_differential",
            1,
            "docs/adr/0006-r2-candidate-b-public-policy-rate-differential-preregistration.md",
            _ADR_SHA256,
            _ADR_COMMITS[0],
            _ADR_COMMITS[1:],
        ),
        sealed_maximum_date=date(2023, 12, 31),
        universe=UniverseContract(
            ("AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD"),
            tuple(APPROVED_PAIRS),
            (
                ("AUD", "AUDUSD", "direct"),
                ("CAD", "USDCAD", "inverse_reciprocal"),
                ("CHF", "USDCHF", "inverse_reciprocal"),
                ("EUR", "EURUSD", "direct"),
                ("GBP", "GBPUSD", "direct"),
                ("JPY", "USDJPY", "inverse_reciprocal"),
                ("NZD", "NZDUSD", "direct"),
            ),
            "USD",
        ),
        formation=FormationContract(
            "2015-01",
            TRAIN_MONTHS,
            PURGED_MONTH,
            VALIDATION_MONTHS,
            83,
            23,
            106,
            1,
            "formation_close_to_next_formation_close",
            "validation_starts_from_zero",
        ),
        signal=SignalContract(
            "principal_policy_rate_currency_minus_usd_percentage_points",
            "descending",
            "iso_currency_lexical_ascending",
            "BIS.WS_CBPOL.1.0.D",
        ),
        portfolio=PortfolioContract(2, 2, "0.25", "0", "1.00", "0.00"),
        costs=CostContract(
            (
                ("AUDUSD", "0.00012"),
                ("EURUSD", "0.00010"),
                ("GBPUSD", "0.00010"),
                ("NZDUSD", "0.00015"),
                ("USDCAD", "0.00012"),
                ("USDCHF", "0.00012"),
                ("USDJPY", "0.00010"),
            ),
            "1.0",
            "1.5",
            "sum_currency_cost_times_absolute_weight_change",
            True,
            "charged_to_final_measured_cohort_of_each_split",
        ),
        accounting=AccountingContract(
            "subsequent_normalized_spot_return_only",
            "arithmetic_sum_of_currency_weight_times_return_minus_cost",
            "1.0_per_split",
            "equity_t_equals_equity_previous_times_one_plus_return",
            "magnitude_of_minimum_equity_over_running_peak_minus_one_including_initial_equity",
            "absolute_cumulative_net_currency_contribution_share",
            "direct_and_reciprocal_representations_must_match",
        ),
        statistics=StatisticalContract(
            "sqrt_12_times_arithmetic_mean_over_sample_std_ddof_1_zero_risk_free",
            "intercept_only_newey_west_bartlett_gamma_denominator_n_no_small_sample_multiplier",
            3,
            "0.95",
            "n_minus_1",
            "scipy.stats.t.ppf",
            "arithmetic_mean_monthly_net_return",
            3,
            10_000,
            20260829,
            "numpy.random.Generator(PCG64)",
            "fifth_percentile_method_linear",
        ),
        gates=GateContract(
            "0.001",
            "0.50",
            "0.20",
            "0.50",
            "one_sided_hac_and_bootstrap_lower_bounds_strictly_positive",
            (
                "positive_train_and_validation_mean",
                "positive_2022_and_2023",
                "at_least_4_of_6_chronological_blocks_positive",
                "at_least_6_of_7_leave_one_currency_out_positive_in_each_split",
                "no_single_currency_above_50_percent_absolute_cumulative_contribution",
                "no_leakage_or_future_data",
            ),
            (
                "one_way_costs_times_1.5",
                "validation_mean_at_least_0.001",
                "validation_sharpe_at_least_0.50",
                "positive_2022_and_2023",
                "drawdown_below_0.20",
                "both_lower_bounds_positive",
            ),
        ),
        bis=BisContract(
            "BIS",
            "WS_CBPOL",
            "1.0",
            "D",
            tuple(APPROVED_BIS_SERIES.items()),
            date(2014, 1, 1),
            date(2023, 12, 31),
        ),
        pit=PitContract(
            "C_m_is_00_00_00_UTC_at_start_of_F_m_UTC_calendar_date",
            True,
            True,
            True,
            True,
            True,
        ),
        multiple_comparison_prohibition=True,
    )
    result = object.__new__(CandidateBStaticRunContract)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    result.__post_init__()
    return result


@dataclass(frozen=True, init=False)
class CandidateBCodeEnvironmentEvidence:
    measurement_implementation_commit: str
    python_version: str
    numpy_version: str
    scipy_version: str
    dependency_lock_sha256: str
    measurement_source_sha256: str
    rng_identity: str
    student_t_identity: str
    _dependency_lock_bytes: bytes = field(repr=False, compare=False)
    _measurement_source_bytes: bytes = field(repr=False, compare=False)

    def __new__(cls) -> CandidateBCodeEnvironmentEvidence:
        raise TypeError("use build_candidate_b_environment_evidence")

    @property
    def semantic_payload(self) -> dict[str, str]:
        return {
            "measurement_implementation_commit": self.measurement_implementation_commit,
            "python_version": self.python_version,
            "numpy_version": self.numpy_version,
            "scipy_version": self.scipy_version,
            "dependency_lock_sha256": self.dependency_lock_sha256,
            "measurement_source_sha256": self.measurement_source_sha256,
            "rng_identity": self.rng_identity,
            "student_t_identity": self.student_t_identity,
        }


def build_candidate_b_environment_evidence(
    *,
    measurement_implementation_commit: str,
    dependency_lock_bytes: bytes,
    expected_dependency_lock_sha256: str,
    measurement_source_bytes: bytes,
    expected_measurement_source_sha256: str,
) -> CandidateBCodeEnvironmentEvidence:
    if not _COMMIT_RE.fullmatch(measurement_implementation_commit):
        raise ValueError("measurement_implementation_commit must be a Git commit hash")
    if not isinstance(dependency_lock_bytes, bytes) or not dependency_lock_bytes:
        raise ValueError("dependency_lock_bytes are required")
    if not isinstance(measurement_source_bytes, bytes) or not measurement_source_bytes:
        raise ValueError("measurement_source_bytes are required")
    lock_hash = hashlib.sha256(dependency_lock_bytes).hexdigest()
    source_hash = hashlib.sha256(measurement_source_bytes).hexdigest()
    if lock_hash != _sha(expected_dependency_lock_sha256, "expected_dependency_lock_sha256"):
        raise ValueError("dependency_lock_hash_mismatch")
    if source_hash != _sha(
        expected_measurement_source_sha256, "expected_measurement_source_sha256"
    ):
        raise ValueError("measurement_source_hash_mismatch")
    result = object.__new__(CandidateBCodeEnvironmentEvidence)
    for name, value in (
        ("measurement_implementation_commit", measurement_implementation_commit),
        ("python_version", platform.python_version()),
        ("numpy_version", np.__version__),
        ("scipy_version", scipy.__version__),
        ("dependency_lock_sha256", lock_hash),
        ("measurement_source_sha256", source_hash),
        ("rng_identity", "numpy.random.Generator(PCG64)"),
        ("student_t_identity", "scipy.stats.t.ppf"),
        ("_dependency_lock_bytes", bytes(dependency_lock_bytes)),
        ("_measurement_source_bytes", bytes(measurement_source_bytes)),
    ):
        object.__setattr__(result, name, value)
    return result


def _validate_environment_evidence(evidence: CandidateBCodeEnvironmentEvidence) -> None:
    if not isinstance(evidence, CandidateBCodeEnvironmentEvidence):
        raise ValueError("validated Candidate B environment evidence is required")
    if (
        hashlib.sha256(evidence._dependency_lock_bytes).hexdigest()
        != evidence.dependency_lock_sha256
        or hashlib.sha256(evidence._measurement_source_bytes).hexdigest()
        != evidence.measurement_source_sha256
        or evidence.python_version != platform.python_version()
        or evidence.numpy_version != np.__version__
        or evidence.scipy_version != scipy.__version__
        or evidence.rng_identity != "numpy.random.Generator(PCG64)"
        or evidence.student_t_identity != "scipy.stats.t.ppf"
        or not _COMMIT_RE.fullmatch(evidence.measurement_implementation_commit)
    ):
        raise ValueError("Candidate B environment evidence does not validate")


@dataclass(frozen=True, init=False)
class CandidateBSpotSemanticEvidence:
    semantic_identity: str
    audit_identity: str
    mapping_fingerprint: str
    d1_construction_identity: str
    pair_datasets: tuple[BarDataset, ...] = field(repr=False, compare=False)
    _spot_panel: SpotPanelManifestReference = field(repr=False, compare=False)

    def __new__(cls) -> CandidateBSpotSemanticEvidence:
        raise TypeError("use build_candidate_b_spot_semantic_evidence")

    @property
    def pair_provenance(self) -> tuple[DataProvenance, ...]:
        return tuple(item.provenance for item in self.pair_datasets)


def _spot_semantic_payload(
    provenances: tuple[DataProvenance, ...],
) -> dict[str, object]:
    return {
        "schema": "candidate_b_spot_semantic_evidence.v1",
        "provider_id": "dukascopy",
        "provider_version": "1",
        "quote_source": "dukascopy_historical_bid",
        "normalization_version": "dukascopy_bid_v1",
        "mapping_fingerprint": DUKASCOPY_MAPPING_FINGERPRINT,
        "d1_construction_identity": canonical_sha256(
            {
                "provider_id": "dukascopy",
                "provider_version": "1",
                "native_timeframe": DUKASCOPY_TIMEFRAMES["D1"],
                "canonical_timeframe": "D1",
                "source_timezone": "UTC",
                "closed_bar": "ts_open_plus_one_day_lte_query_as_of",
                "value_field": "close",
                "quote_source": "bid",
            }
        ),
        "pairs": tuple(
            {
                "pair": item.canonical_symbol,
                "provider_symbol": item.provider_symbol,
                "query_fingerprint": item.query_fingerprint,
                "content_hash": item.content_hash,
                "dataset_id": item.dataset_id,
                "revision": item.revision,
            }
            for item in provenances
        ),
    }


def build_candidate_b_spot_semantic_evidence(
    *,
    spot_panel: SpotPanelManifestReference,
    pair_datasets: tuple[BarDataset, ...],
) -> CandidateBSpotSemanticEvidence:
    """Validate typed Dukascopy BID/D1 provenance and separate semantic from audit identity."""
    if not isinstance(spot_panel, SpotPanelManifestReference):
        raise ValueError("Candidate B spot panel evidence is required")
    datasets = tuple(pair_datasets)
    if len(datasets) != len(APPROVED_PAIRS) or any(
        not isinstance(item, BarDataset) for item in datasets
    ):
        raise ValueError("exactly seven typed spot datasets are required")
    _validate_spot_sealed_metadata_before_values(datasets)
    try:
        validated = tuple(BarDataset(item.query, item.frame, item.provenance) for item in datasets)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("spot dataset evidence does not validate") from exc
    provenances = tuple(item.provenance for item in validated)
    by_pair = {item.canonical_symbol: item for item in provenances}
    datasets_by_pair = {item.provenance.canonical_symbol: item for item in validated}
    if len(by_pair) != len(APPROVED_PAIRS) or set(by_pair) != set(APPROVED_PAIRS):
        raise ValueError("spot provenance must have exact Candidate B pair membership")
    ordered = tuple(by_pair[pair] for pair in APPROVED_PAIRS)
    ordered_datasets = tuple(datasets_by_pair[pair] for pair in APPROVED_PAIRS)
    expected_dataset_ids = dict(zip(APPROVED_PAIRS, spot_panel.dataset_ids, strict=True))
    refs_by_pair = {pair: [] for pair in APPROVED_PAIRS}
    for reference in spot_panel.observations:
        refs_by_pair[reference.pair].append(reference)
    for pair, item in zip(APPROVED_PAIRS, ordered, strict=True):
        if (
            item.provider_id != "dukascopy"
            or item.provider_version != "1"
            or item.normalization_version != "dukascopy_bid_v1"
            or item.provider_symbol != DUKASCOPY_SYMBOLS[pair]
            or item.timeframe != "D1"
            or item.source_timezone != "UTC"
            or item.sanitized_source_reference != "dukascopy:historical:bid"
            or item.provenance_quality is not ProvenanceQuality.VERIFIED
            or item.dataset_id != expected_dataset_ids[pair]
            or item.row_count <= 0
            or item.query_start is None
            or item.query_end is None
            or item.actual_first_observation is None
            or item.actual_last_observation is None
        ):
            raise ValueError("spot semantic provenance is incomplete or incompatible")
        query = BarQuery(
            CanonicalInstrument(pair),
            "D1",
            item.query_start,
            item.query_end,
            item.query_as_of,
        )
        if (
            query.fingerprint != item.query_fingerprint
            or dataset_identity(
                item.provider_id,
                item.provider_version,
                item.query_fingerprint,
                item.content_hash,
            )
            != item.dataset_id
        ):
            raise ValueError("spot dataset identity does not validate")
        references = refs_by_pair[pair]
        if not references or any(
            ref.dataset_id != item.dataset_id
            or ref.bar_open < item.query_start
            or ref.bar_close > item.query_end
            or ref.bar_close > item.query_as_of
            or ref.bar_open < item.actual_first_observation
            or ref.bar_open > item.actual_last_observation
            for ref in references
        ):
            raise ValueError("spot observations do not resolve to typed provenance")
    semantic_payload = _spot_semantic_payload(ordered)
    audit_payload = {
        "schema": "candidate_b_spot_audit_evidence.v1",
        "spot_panel_manifest_id": spot_panel.manifest_id,
        "semantic_identity": canonical_sha256(semantic_payload),
        "retrieval_evidence": tuple(
            {
                "pair": item.canonical_symbol,
                "retrieved_at": item.retrieved_at,
                "source_reference": item.sanitized_source_reference,
            }
            for item in ordered
        ),
    }
    result = object.__new__(CandidateBSpotSemanticEvidence)
    for name, value in (
        ("semantic_identity", canonical_sha256(semantic_payload)),
        ("audit_identity", canonical_sha256(audit_payload)),
        ("mapping_fingerprint", DUKASCOPY_MAPPING_FINGERPRINT),
        ("d1_construction_identity", semantic_payload["d1_construction_identity"]),
        ("pair_datasets", ordered_datasets),
        ("_spot_panel", spot_panel),
    ):
        object.__setattr__(result, name, value)
    return result


def _validate_spot_sealed_metadata_before_values(datasets: tuple[BarDataset, ...]) -> None:
    for dataset in datasets:
        query = dataset.query
        provenance = dataset.provenance
        if (
            not isinstance(query, BarQuery)
            or not isinstance(provenance, DataProvenance)
            or provenance.query_start != query.start
            or provenance.query_end != query.end
            or provenance.query_as_of != query.as_of
            or provenance.actual_first_observation is None
            or provenance.actual_last_observation is None
            or provenance.row_count <= 0
        ):
            raise ValueError("spot query and actual bounds are inconsistent")
        bounded_dates = (
            query.start,
            query.end,
            query.as_of,
            provenance.actual_first_observation,
            provenance.actual_last_observation,
        )
        if any(item.date() > date(2023, 12, 31) for item in bounded_dates):
            raise ValueError("sealed_window_violation")
        if (
            provenance.actual_first_observation > provenance.actual_last_observation
            or provenance.actual_first_observation < query.start
            or provenance.actual_last_observation >= query.end
        ):
            raise ValueError("spot query and actual bounds are inconsistent")
        raw_frame = getattr(dataset, "_frame", None)
        index = getattr(raw_frame, "index", None)
        if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
            raise ValueError("spot row timestamps are unavailable")
        utc_index = index.tz_convert("UTC")
        if any(timestamp.date() > date(2023, 12, 31) for timestamp in utc_index):
            raise ValueError("sealed_window_violation")
        if (
            len(utc_index) != provenance.row_count
            or not utc_index.is_monotonic_increasing
            or not utc_index.is_unique
            or utc_index[0].to_pydatetime() != provenance.actual_first_observation
            or utc_index[-1].to_pydatetime() != provenance.actual_last_observation
        ):
            raise ValueError("spot query and actual bounds are inconsistent")


def _validate_spot_semantic_evidence(
    evidence: CandidateBSpotSemanticEvidence,
    spot_panel: SpotPanelManifestReference,
) -> None:
    if not isinstance(evidence, CandidateBSpotSemanticEvidence):
        raise ValueError("validated Candidate B spot semantic evidence is required")
    rebuilt = build_candidate_b_spot_semantic_evidence(
        spot_panel=evidence._spot_panel,
        pair_datasets=evidence.pair_datasets,
    )
    if (
        evidence._spot_panel != spot_panel
        or evidence.semantic_identity != rebuilt.semantic_identity
        or evidence.audit_identity != rebuilt.audit_identity
        or evidence.mapping_fingerprint != rebuilt.mapping_fingerprint
        or evidence.d1_construction_identity != rebuilt.d1_construction_identity
    ):
        raise ValueError("spot semantic evidence does not validate")


@dataclass(frozen=True, init=False)
class CandidateBQualifiedRunBindings:
    static_contract_id: str
    code_environment: CandidateBCodeEnvironmentEvidence
    aggregate_bis_manifest_identity: str
    series_manifest_identities: tuple[tuple[str, str], ...]
    event_manifest_identity: str
    concordance_identities: tuple[tuple[str, str], ...]
    spot_panel_manifest_identity: str
    spot_semantic_audit_identity: str
    formation_manifest_identity: str
    qualification_identity: str
    audit_evidence_identity: str
    bindings_id: str = field(init=False)
    _evidence: _CandidateBVerifiedEvidence = field(repr=False, compare=False)

    def __new__(cls) -> CandidateBQualifiedRunBindings:
        raise TypeError("use build_candidate_b_qualified_run_bindings")

    @property
    def qualification_passed(self) -> bool:
        return self._evidence.qualification_result.qualified


@dataclass(frozen=True, init=False)
class CandidateBRunIdentity:
    schema: str
    static_contract_id: str
    bindings_id: str
    audit_evidence_identity: str
    run_id: str

    def __new__(cls) -> CandidateBRunIdentity:
        raise TypeError("use finalize_candidate_b_run_identity")


@dataclass(frozen=True)
class _CandidateBVerifiedEvidence:
    series_manifests: tuple[PolicyRateSeriesManifest, ...]
    event_manifest: PolicyEventManifest
    concordance_results: tuple[PolicyConcordanceResult, ...]
    spot_panel: SpotPanelManifestReference
    spot_semantic_evidence: CandidateBSpotSemanticEvidence
    formation_manifest: CandidateBFormationManifest
    qualification_result: CandidateBQualificationResult


def _validate_formation_evidence(
    series_manifests: tuple[PolicyRateSeriesManifest, ...],
    event_manifest: PolicyEventManifest,
    spot_panel: SpotPanelManifestReference,
    formation_manifest: CandidateBFormationManifest,
) -> None:
    formations = formation_manifest.formations
    months = tuple(item.formation_month for item in formations)
    if months != MEASURED_MONTHS:
        raise ValueError("formation evidence violates the exact frozen month set")
    series_by_currency = {item.request.series.currency: item for item in series_manifests}
    events_by_id = {item.event_id: item for item in event_manifest.events}
    spot_index = set(spot_panel.observations)
    expected_spot_ids = dict(zip(APPROVED_PAIRS, spot_panel.dataset_ids, strict=True))
    required_sources = {
        *(item.manifest_id for item in series_manifests),
        event_manifest.manifest_id,
        spot_panel.manifest_id,
    }
    for formation in formations:
        expected_split = (
            FormationSplit.TRAIN
            if formation.formation_month in TRAIN_MONTHS
            else FormationSplit.VALIDATION
        )
        if (
            not formation.complete
            or formation.purged
            or formation.split is not expected_split
            or formation.formation_at.year != int(formation.formation_month[:4])
            or formation.formation_at.month != int(formation.formation_month[5:])
            or formation.exit_at.date() > date(2023, 12, 31)
            or not required_sources.issubset(formation.source_manifest_fingerprints)
        ):
            raise ValueError("formation evidence is invalid")
        if {
            item.pair: item.dataset_id for item in formation.spot_observations
        } != expected_spot_ids:
            raise ValueError("formation spot evidence is invalid")
        if any(
            item not in spot_index or item.bar_close != formation.formation_at
            for item in formation.spot_observations
        ):
            raise ValueError("formation spot reference is unresolved")
        for state in formation.policy_states:
            manifest = series_by_currency.get(state.currency)
            event = events_by_id.get(state.event_id)
            observation = (
                next(
                    (
                        item
                        for item in manifest.observations
                        if item.identity == state.observation_id
                    ),
                    None,
                )
                if manifest is not None
                else None
            )
            if (
                manifest is None
                or event is None
                or state.dataset_id != manifest.dataset_id
                or observation is None
                or observation.series_key != state.series_key
                or observation.observation_date != state.observation_date
                or observation.value != state.observation_value
                or observation.status != state.observation_status
                or event.currency != state.currency
                or event.policy_instrument_id != state.policy_instrument_id
                or event.new_rate != state.observation_value
                or event.announcement_upper > formation.cutoff_at
                or event.effective_upper > formation.cutoff_at
                or not state.eligible
            ):
                raise ValueError("formation policy reference is unresolved")


def _semantic_event_identity(event_manifest: PolicyEventManifest) -> str:
    events = tuple(
        {
            "event_id": item.event_id,
            "kind": item.kind,
            "currency": item.currency,
            "central_bank_id": item.central_bank_id,
            "policy_instrument_id": item.policy_instrument_id,
            "announcement_lower": item.announcement_lower,
            "announcement_upper": item.announcement_upper,
            "announcement_precision": item.announcement_precision,
            "effective_lower": item.effective_lower,
            "effective_upper": item.effective_upper,
            "effective_precision": item.effective_precision,
            "source_timezone": item.source_timezone,
            "old_rate": item.old_rate,
            "new_rate": item.new_rate,
            "source": {
                "source_url": item.source.source_url,
                "content_hash": item.source.content_hash,
                "byte_count": item.source.byte_count,
                "media_type": item.source.media_type,
                "source_kind": item.source.source_kind,
            },
            "evidence_classification": item.evidence_classification,
            "ambiguity": item.ambiguity,
            "conflict": item.conflict,
        }
        for item in event_manifest.events
    )
    return canonical_sha256({"schema": "candidate_b_policy_events.semantic.v1", "events": events})


def _semantic_formation_identity(formation_manifest: CandidateBFormationManifest) -> str:
    formations = tuple(
        {
            "cohort_id": item.cohort_id,
            "formation_month": item.formation_month,
            "formation_at": item.formation_at,
            "cutoff_at": item.cutoff_at,
            "exit_at": item.exit_at,
            "split": item.split,
            "purged": item.purged,
            "spot_observations": item.spot_observations,
            "policy_states": item.policy_states,
            "pit_eligible": item.pit_eligible,
            "complete": item.complete,
            "rejection_reason": item.rejection_reason,
        }
        for item in formation_manifest.formations
    )
    return canonical_sha256(
        {"schema": "candidate_b_formations.semantic.v1", "formations": formations}
    )


def _semantic_evidence_identities(
    manifests: tuple[PolicyRateSeriesManifest, ...],
    event_manifest: PolicyEventManifest,
    concordances: tuple[PolicyConcordanceResult, ...],
    spot_panel: SpotPanelManifestReference,
    spot_semantic_evidence: CandidateBSpotSemanticEvidence,
    formation_manifest: CandidateBFormationManifest,
) -> dict[str, object]:
    manifests_by_currency = {item.request.series.currency: item for item in manifests}
    concordance_by_currency = {item.currency: item for item in concordances}
    series_ids = tuple(
        (currency, manifests_by_currency[currency].dataset_id) for currency in APPROVED_BIS_SERIES
    )
    event_id = _semantic_event_identity(event_manifest)
    concordance_ids = tuple(
        (
            currency,
            canonical_sha256(
                {
                    "schema": "candidate_b_concordance.semantic.v1",
                    "currency": currency,
                    "series_dataset_id": concordance_by_currency[currency].series_dataset_id,
                    "event_semantic_id": event_id,
                    "status": concordance_by_currency[currency].status,
                    "reasons": concordance_by_currency[currency].reasons,
                }
            ),
        )
        for currency in APPROVED_BIS_SERIES
    )
    aggregate_bis = canonical_sha256(
        {"schema": "candidate_b_bis_dataset_set.semantic.v1", "series": series_ids}
    )
    spot_id = spot_semantic_evidence.semantic_identity
    formation_id = _semantic_formation_identity(formation_manifest)
    qualification_id = canonical_sha256(
        {
            "schema": "candidate_b_qualification.semantic.v1",
            "aggregate_bis": aggregate_bis,
            "events": event_id,
            "concordance": concordance_ids,
            "spot": spot_id,
            "formations": formation_id,
            "qualified": True,
        }
    )
    return {
        "aggregate_bis": aggregate_bis,
        "series": series_ids,
        "events": event_id,
        "concordance": concordance_ids,
        "spot": spot_id,
        "formations": formation_id,
        "qualification": qualification_id,
    }


def build_candidate_b_qualified_run_bindings(
    *,
    static_contract: CandidateBStaticRunContract,
    code_environment: CandidateBCodeEnvironmentEvidence,
    series_manifests: tuple[PolicyRateSeriesManifest, ...],
    event_manifest: PolicyEventManifest,
    concordance_results: tuple[PolicyConcordanceResult, ...],
    spot_panel: SpotPanelManifestReference,
    spot_semantic_evidence: CandidateBSpotSemanticEvidence,
    formation_manifest: CandidateBFormationManifest,
    qualification_result: CandidateBQualificationResult,
) -> CandidateBQualifiedRunBindings:
    expected_static = build_candidate_b_static_run_contract()
    if (
        not isinstance(static_contract, CandidateBStaticRunContract)
        or canonical_sha256(_static_payload(static_contract)) != static_contract.contract_id
        or _static_payload(static_contract) != _static_payload(expected_static)
    ):
        raise ValueError("wrong Candidate B static contract")
    _validate_environment_evidence(code_environment)
    _validate_spot_semantic_evidence(spot_semantic_evidence, spot_panel)
    manifests = tuple(series_manifests)
    concordances = tuple(concordance_results)
    manifest_currencies = {item.request.series.currency for item in manifests}
    concordance_currencies = {item.currency for item in concordances}
    if (
        len(manifests) != 8
        or manifest_currencies != set(APPROVED_BIS_SERIES)
        or len(concordances) != 8
        or concordance_currencies != set(APPROVED_BIS_SERIES)
    ):
        raise ValueError("all eight qualified BIS evidence objects are required")
    manifests_by_currency = {item.request.series.currency: item for item in manifests}
    concordance_by_currency = {item.currency: item for item in concordances}
    for currency in APPROVED_BIS_SERIES:
        expected = reconcile_policy_series(manifests_by_currency[currency], event_manifest)
        supplied = concordance_by_currency[currency]
        if (
            supplied.status is not ConcordanceStatus.PASS
            or supplied.result_id != expected.result_id
        ):
            raise ValueError("concordance evidence does not independently validate")
    _validate_formation_evidence(manifests, event_manifest, spot_panel, formation_manifest)
    expected_series_ids = tuple(sorted(item.manifest_id for item in manifests))
    expected_concordance_ids = tuple(sorted(item.result_id for item in concordances))
    if (
        not qualification_result.qualified
        or qualification_result.reasons
        or qualification_result.series_manifest_ids != expected_series_ids
        or qualification_result.event_manifest_id != event_manifest.manifest_id
        or qualification_result.concordance_result_ids != expected_concordance_ids
        or qualification_result.spot_manifest_id != spot_panel.manifest_id
        or qualification_result.formation_manifest_id != formation_manifest.manifest_id
        or not formation_manifest.qualified
    ):
        raise ValueError("final qualification evidence is missing or inconsistent")
    semantic = _semantic_evidence_identities(
        manifests,
        event_manifest,
        concordances,
        spot_panel,
        spot_semantic_evidence,
        formation_manifest,
    )
    audit_identity = canonical_sha256(
        {
            "schema": "candidate_b_audit_evidence.v1",
            "series_manifests": tuple(sorted(item.manifest_id for item in manifests)),
            "event_manifest": event_manifest.manifest_id,
            "concordance_results": tuple(sorted(item.result_id for item in concordances)),
            "spot_manifest": spot_panel.manifest_id,
            "spot_semantic_audit": spot_semantic_evidence.audit_identity,
            "formation_manifest": formation_manifest.manifest_id,
            "qualification": qualification_result.qualification_id,
        }
    )
    evidence = _CandidateBVerifiedEvidence(
        manifests,
        event_manifest,
        concordances,
        spot_panel,
        spot_semantic_evidence,
        formation_manifest,
        qualification_result,
    )
    result = object.__new__(CandidateBQualifiedRunBindings)
    values = {
        "static_contract_id": static_contract.contract_id,
        "code_environment": code_environment,
        "aggregate_bis_manifest_identity": semantic["aggregate_bis"],
        "series_manifest_identities": semantic["series"],
        "event_manifest_identity": semantic["events"],
        "concordance_identities": semantic["concordance"],
        "spot_panel_manifest_identity": semantic["spot"],
        "spot_semantic_audit_identity": spot_semantic_evidence.audit_identity,
        "formation_manifest_identity": semantic["formations"],
        "qualification_identity": semantic["qualification"],
        "audit_evidence_identity": audit_identity,
        "_evidence": evidence,
    }
    for name, value in values.items():
        object.__setattr__(result, name, value)
    object.__setattr__(result, "bindings_id", canonical_sha256(_semantic_bindings_payload(result)))
    return result


def _semantic_bindings_payload(bindings: CandidateBQualifiedRunBindings) -> dict[str, object]:
    return {
        "schema": "candidate_b_qualified_run_bindings.semantic.v1",
        "static_contract_id": bindings.static_contract_id,
        "code_environment": bindings.code_environment.semantic_payload,
        "aggregate_bis_manifest_identity": bindings.aggregate_bis_manifest_identity,
        "series_manifest_identities": bindings.series_manifest_identities,
        "event_manifest_identity": bindings.event_manifest_identity,
        "concordance_identities": bindings.concordance_identities,
        "spot_panel_manifest_identity": bindings.spot_panel_manifest_identity,
        "formation_manifest_identity": bindings.formation_manifest_identity,
        "qualification_identity": bindings.qualification_identity,
    }


def finalize_candidate_b_run_identity(
    static_contract: CandidateBStaticRunContract,
    bindings: CandidateBQualifiedRunBindings,
) -> CandidateBRunIdentity:
    expected_static = build_candidate_b_static_run_contract()
    if not isinstance(bindings, CandidateBQualifiedRunBindings):
        raise ValueError("verified Candidate B bindings are required")
    evidence = bindings._evidence
    rebuilt = build_candidate_b_qualified_run_bindings(
        static_contract=static_contract,
        code_environment=bindings.code_environment,
        series_manifests=evidence.series_manifests,
        event_manifest=evidence.event_manifest,
        concordance_results=evidence.concordance_results,
        spot_panel=evidence.spot_panel,
        spot_semantic_evidence=evidence.spot_semantic_evidence,
        formation_manifest=evidence.formation_manifest,
        qualification_result=evidence.qualification_result,
    )
    if (
        static_contract.contract_id != expected_static.contract_id
        or bindings.static_contract_id != static_contract.contract_id
        or bindings.bindings_id != rebuilt.bindings_id
        or bindings.audit_evidence_identity != rebuilt.audit_evidence_identity
        or bindings.spot_semantic_audit_identity != rebuilt.spot_semantic_audit_identity
        or _semantic_bindings_payload(bindings) != _semantic_bindings_payload(rebuilt)
        or not bindings.qualification_passed
    ):
        raise ValueError("Candidate B run identity requires the current qualified static contract")
    schema = "candidate_b_measurement_run.v1"
    run_id = canonical_sha256(
        {
            "schema": schema,
            "static_contract_id": static_contract.contract_id,
            "semantic_bindings_id": bindings.bindings_id,
        }
    )
    result = object.__new__(CandidateBRunIdentity)
    for name, value in (
        ("schema", schema),
        ("static_contract_id", static_contract.contract_id),
        ("bindings_id", bindings.bindings_id),
        ("audit_evidence_identity", bindings.audit_evidence_identity),
        ("run_id", run_id),
    ):
        object.__setattr__(result, name, value)
    return result
