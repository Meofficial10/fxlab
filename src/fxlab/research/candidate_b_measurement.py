"""Pure, in-memory Candidate B measurement primitives.

The public surface is intentionally candidate-specific.  Frozen research choices are constants,
not parameters, and this module performs no I/O, discovery, acquisition, or trade execution.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType

import numpy as np
from scipy.stats import t as student_t

CURRENCIES = ("AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD")
PAIRS = ("AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY")
PAIR_BY_CURRENCY = MappingProxyType(
    {
        "AUD": "AUDUSD",
        "CAD": "USDCAD",
        "CHF": "USDCHF",
        "EUR": "EURUSD",
        "GBP": "GBPUSD",
        "JPY": "USDJPY",
        "NZD": "NZDUSD",
    }
)
DIRECT_PAIRS = frozenset({"AUDUSD", "EURUSD", "GBPUSD", "NZDUSD"})
INVERSE_PAIRS = frozenset({"USDCAD", "USDCHF", "USDJPY"})
ONE_WAY_COST_BY_PAIR = MappingProxyType(
    {
        "EURUSD": Decimal("0.00010"),
        "GBPUSD": Decimal("0.00010"),
        "USDJPY": Decimal("0.00010"),
        "AUDUSD": Decimal("0.00012"),
        "USDCAD": Decimal("0.00012"),
        "USDCHF": Decimal("0.00012"),
        "NZDUSD": Decimal("0.00015"),
    }
)
SEALED_MAXIMUM_DATE = date(2023, 12, 31)
TRAIN_MONTHS = tuple(
    f"{year:04d}-{month:02d}"
    for year in range(2015, 2022)
    for month in range(1, 13)
    if not (year == 2021 and month == 12)
)
VALIDATION_MONTHS = tuple(
    f"{year:04d}-{month:02d}"
    for year in (2022, 2023)
    for month in range(1, 13)
    if not (year == 2023 and month == 12)
)
MEASURED_MONTHS = TRAIN_MONTHS + VALIDATION_MONTHS
PURGED_MONTH = "2021-12"
HAC_LAG = 3
BOOTSTRAP_BLOCK_LENGTH = 3
BOOTSTRAP_REPLICATIONS = 10_000
BOOTSTRAP_SEED = 20260829


class ResearchSplit(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    PURGED = "purged"


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


def _finite_float(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


@dataclass(frozen=True)
class DecimalVector:
    values: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        values = tuple(_decimal(item, "vector value") for item in self.values)
        if len(values) != len(CURRENCIES):
            raise ValueError("Candidate B vectors require exactly seven currencies")
        object.__setattr__(self, "values", values)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> DecimalVector:
        if set(values) != set(CURRENCIES):
            raise ValueError("Candidate B vectors require the frozen currency universe")
        return cls(tuple(_decimal(values[currency], currency) for currency in CURRENCIES))

    def as_dict(self) -> dict[str, Decimal]:
        return dict(self.items())

    def items(self) -> tuple[tuple[str, Decimal], ...]:
        return tuple(zip(CURRENCIES, self.values, strict=True))


@dataclass(frozen=True)
class FloatVector:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        values = tuple(_finite_float(item, "vector value") for item in self.values)
        if len(values) != len(CURRENCIES):
            raise ValueError("Candidate B vectors require exactly seven currencies")
        object.__setattr__(self, "values", values)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> FloatVector:
        if set(values) != set(CURRENCIES):
            raise ValueError("Candidate B vectors require the frozen currency universe")
        return cls(tuple(_finite_float(values[currency], currency) for currency in CURRENCIES))

    def items(self) -> tuple[tuple[str, float], ...]:
        return tuple(zip(CURRENCIES, self.values, strict=True))


@dataclass(frozen=True, init=False)
class CandidateBWeights:
    values: tuple[Decimal, ...]

    def __new__(cls) -> CandidateBWeights:
        raise TypeError("Candidate B weights are created only by the frozen ranking rule")

    def _validate(self) -> None:
        allowed = {Decimal("-0.25"), Decimal("0"), Decimal("0.25")}
        if any(item not in allowed for item in self.values):
            raise ValueError("weights violate the frozen Candidate B construction")
        if self.values.count(Decimal("0.25")) not in (0, 2):
            raise ValueError("Candidate B requires two long currencies or a zero book")
        if self.values.count(Decimal("-0.25")) not in (0, 2):
            raise ValueError("Candidate B requires two short currencies or a zero book")
        if sum(self.values) != 0:
            raise ValueError("Candidate B currency weights must be net zero")

    def items(self) -> tuple[tuple[str, Decimal], ...]:
        return tuple(zip(CURRENCIES, self.values, strict=True))

    @property
    def long_currencies(self) -> tuple[str, ...]:
        return tuple(currency for currency, value in self.items() if value > 0)

    @property
    def short_currencies(self) -> tuple[str, ...]:
        return tuple(currency for currency, value in self.items() if value < 0)

    def as_dict(self) -> dict[str, Decimal]:
        return dict(self.items())


@dataclass(frozen=True)
class ScenarioAccounting:
    multiplier: float
    cost: float
    net_return: float
    currency_net_contributions: FloatVector


@dataclass(frozen=True)
class CandidateBMonthAccounting:
    gross_return: float
    rebalance_turnover: Decimal
    liquidation_turnover: Decimal
    headline: ScenarioAccounting
    stress: ScenarioAccounting
    cohort_count: int = 1


@dataclass(frozen=True)
class HACResult:
    mean: float
    gammas: tuple[float, ...]
    bartlett_weights: tuple[float, ...]
    long_run_variance: float
    variance_of_mean: float
    standard_error: float
    critical_value: float
    lower_bound: float
    df: int


@dataclass(frozen=True)
class BootstrapResult:
    lower_bound: float
    sampled_index_digest: str
    block_length: int = BOOTSTRAP_BLOCK_LENGTH
    replications: int = BOOTSTRAP_REPLICATIONS
    seed: int = BOOTSTRAP_SEED


@dataclass(frozen=True)
class ConcentrationResult:
    shares: FloatVector
    maximum_share: float
    passes: bool


@dataclass(frozen=True)
class FormationPeriod:
    formation_month: str
    exit_month: str
    split: ResearchSplit
    purged: bool


@dataclass(frozen=True, init=False)
class CandidateBCohort:
    cohort_id: str
    formation_month: str
    exit_month: str
    formation_at: datetime
    cutoff_at: datetime
    exit_at: datetime
    split: ResearchSplit
    source_qualification_id: str
    differentials: DecimalVector
    normalized_returns: FloatVector

    def __new__(cls) -> CandidateBCohort:
        raise TypeError("use create_candidate_b_cohort")


@dataclass(frozen=True)
class CandidateBMeasuredCohort:
    cohort: CandidateBCohort
    weights: CandidateBWeights
    accounting: CandidateBMonthAccounting


@dataclass(frozen=True)
class CandidateBSplitMeasurement:
    split: ResearchSplit
    cohorts: tuple[CandidateBMeasuredCohort, ...]
    headline_equity: tuple[float, ...]
    stress_equity: tuple[float, ...]


@dataclass(frozen=True)
class CandidateBMeasurement:
    train: CandidateBSplitMeasurement
    validation: CandidateBSplitMeasurement

    @property
    def total_measured_cohorts(self) -> int:
        return len(self.train.cohorts) + len(self.validation.cohorts)


def _normalize_quote_return(pair: str, start_price: object, end_price: object) -> float:
    if pair not in PAIRS:
        raise ValueError("pair is outside the frozen Candidate B universe")
    start = _finite_float(start_price, "start_price")
    end = _finite_float(end_price, "end_price")
    if start <= 0 or end <= 0:
        raise ValueError("prices must be positive")
    if pair in DIRECT_PAIRS:
        return end / start - 1.0
    return start / end - 1.0


def _policy_differentials(foreign_rates: Mapping[str, object], usd_rate: object) -> DecimalVector:
    if set(foreign_rates) != set(CURRENCIES):
        raise ValueError("policy rates require the frozen seven-currency universe")
    usd = _decimal(usd_rate, "USD rate")
    return DecimalVector.from_mapping(
        {currency: _decimal(foreign_rates[currency], currency) - usd for currency in CURRENCIES}
    )


def _rank_currencies(differentials: DecimalVector) -> tuple[str, ...]:
    values = differentials.as_dict()
    return tuple(sorted(CURRENCIES, key=lambda currency: (-values[currency], currency)))


def _frozen_portfolio_weights(differentials: DecimalVector) -> CandidateBWeights:
    ranked = _rank_currencies(differentials)
    weights = {currency: Decimal(0) for currency in CURRENCIES}
    for currency in ranked[:2]:
        weights[currency] = Decimal("0.25")
    for currency in ranked[-2:]:
        weights[currency] = Decimal("-0.25")
    return _candidate_b_weights(tuple(weights[currency] for currency in CURRENCIES))


def _zero_weights() -> CandidateBWeights:
    return _candidate_b_weights(tuple(Decimal(0) for _ in CURRENCIES))


def _candidate_b_weights(values: tuple[Decimal, ...]) -> CandidateBWeights:
    result = object.__new__(CandidateBWeights)
    object.__setattr__(result, "values", tuple(_decimal(item, "weight") for item in values))
    if len(result.values) != len(CURRENCIES):
        raise ValueError("Candidate B weights require exactly seven currencies")
    result._validate()
    return result


def _turnover(previous: CandidateBWeights, current: CandidateBWeights) -> Decimal:
    return sum(
        (abs(new - old) for old, new in zip(previous.values, current.values, strict=True)),
        Decimal(0),
    )


def _gross_monthly_return(weights: CandidateBWeights, returns: FloatVector) -> float:
    return math.fsum(
        float(weight) * value for weight, value in zip(weights.values, returns.values, strict=True)
    )


def _scenario(
    previous: CandidateBWeights,
    current: CandidateBWeights,
    returns: FloatVector,
    *,
    multiplier: float,
    terminal_liquidation: bool,
) -> ScenarioAccounting:
    contributions: list[float] = []
    for currency, old, new, value in zip(
        CURRENCIES, previous.values, current.values, returns.values, strict=True
    ):
        one_way = float(ONE_WAY_COST_BY_PAIR[PAIR_BY_CURRENCY[currency]])
        cost = multiplier * one_way * float(abs(new - old))
        if terminal_liquidation:
            cost += multiplier * one_way * float(abs(new))
        contributions.append(float(new) * value - cost)
    total_cost = _gross_monthly_return(current, returns) - math.fsum(contributions)
    return ScenarioAccounting(
        multiplier,
        total_cost,
        math.fsum(contributions),
        FloatVector(tuple(contributions)),
    )


def _account_month(
    previous_weights: CandidateBWeights,
    new_weights: CandidateBWeights,
    normalized_returns: FloatVector,
    *,
    terminal_liquidation: bool = False,
) -> CandidateBMonthAccounting:
    rebalance = _turnover(previous_weights, new_weights)
    liquidation = (
        sum((abs(item) for item in new_weights.values), Decimal(0))
        if terminal_liquidation
        else Decimal(0)
    )
    return CandidateBMonthAccounting(
        _gross_monthly_return(new_weights, normalized_returns),
        rebalance,
        liquidation,
        _scenario(
            previous_weights,
            new_weights,
            normalized_returns,
            multiplier=1.0,
            terminal_liquidation=terminal_liquidation,
        ),
        _scenario(
            previous_weights,
            new_weights,
            normalized_returns,
            multiplier=1.5,
            terminal_liquidation=terminal_liquidation,
        ),
    )


def _compound_equity(returns: Sequence[object]) -> tuple[float, ...]:
    equity = 1.0
    path = [equity]
    for value in returns:
        item = _finite_float(value, "monthly return")
        if item <= -1.0:
            raise ValueError("monthly return must be greater than -1")
        equity *= 1.0 + item
        if not math.isfinite(equity):
            raise ValueError("equity path is nonfinite")
        path.append(equity)
    return tuple(path)


def _maximum_drawdown(returns: Sequence[object]) -> float:
    path = _compound_equity(returns)
    peak = path[0]
    minimum_drawdown = 0.0
    for equity in path:
        peak = max(peak, equity)
        minimum_drawdown = min(minimum_drawdown, equity / peak - 1.0)
    return -minimum_drawdown


def _annualized_sharpe(returns: Sequence[object]) -> float:
    values = tuple(_finite_float(item, "monthly return") for item in returns)
    if len(values) < 2:
        raise ValueError("Sharpe requires at least two monthly observations")
    mean = math.fsum(values) / len(values)
    variance = math.fsum((item - mean) ** 2 for item in values) / (len(values) - 1)
    if variance <= 0 or not math.isfinite(variance):
        raise ValueError("Sharpe requires positive finite sample variance")
    return math.sqrt(12.0) * mean / math.sqrt(variance)


def _student_t_critical_value(df: int) -> float:
    if not isinstance(df, int) or isinstance(df, bool) or df < 1:
        raise ValueError("Student-t degrees of freedom must be a positive integer")
    result = float(student_t.ppf(0.95, df))
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Student-t critical value is invalid")
    return result


def _hac_mean_inference(returns: Sequence[object]) -> HACResult:
    values = tuple(_finite_float(item, "monthly return") for item in returns)
    n = len(values)
    if n <= HAC_LAG:
        raise ValueError("HAC lag 3 requires more than three observations")
    mean = math.fsum(values) / n
    residuals = tuple(item - mean for item in values)
    gammas = tuple(
        math.fsum(residuals[index] * residuals[index - lag] for index in range(lag, n)) / n
        for lag in range(HAC_LAG + 1)
    )
    weights = tuple(1.0 - lag / (HAC_LAG + 1) for lag in range(1, HAC_LAG + 1))
    lrv = gammas[0] + 2.0 * math.fsum(
        weight * gammas[lag] for lag, weight in enumerate(weights, start=1)
    )
    if lrv < 0 or not math.isfinite(lrv):
        raise ValueError("HAC long-run variance is invalid")
    variance_of_mean = lrv / n
    standard_error = math.sqrt(variance_of_mean)
    critical = _student_t_critical_value(n - 1)
    return HACResult(
        mean,
        gammas,
        weights,
        lrv,
        variance_of_mean,
        standard_error,
        critical,
        mean - critical * standard_error,
        n - 1,
    )


def _circular_moving_block_bootstrap(returns: Sequence[object]) -> BootstrapResult:
    values = tuple(_finite_float(item, "monthly return") for item in returns)
    n = len(values)
    if not n:
        raise ValueError("bootstrap requires observations")
    rng = np.random.Generator(np.random.PCG64(BOOTSTRAP_SEED))
    starts_per_replication = math.ceil(n / BOOTSTRAP_BLOCK_LENGTH)
    means = np.empty(BOOTSTRAP_REPLICATIONS, dtype=np.float64)
    all_indices = np.empty((BOOTSTRAP_REPLICATIONS, n), dtype="<i8")
    vector = np.asarray(values, dtype=np.float64)
    offsets = np.arange(BOOTSTRAP_BLOCK_LENGTH, dtype=np.int64)
    for replication in range(BOOTSTRAP_REPLICATIONS):
        starts = rng.integers(0, n, size=starts_per_replication)
        indices = ((starts[:, None] + offsets[None, :]) % n).reshape(-1)[:n]
        all_indices[replication] = indices
        means[replication] = float(np.mean(vector[indices]))
    digest = hashlib.sha256(all_indices.tobytes(order="C")).hexdigest()
    lower_bound = float(np.quantile(means, 0.05, method="linear"))
    return BootstrapResult(lower_bound, digest)


def _currency_concentration(
    monthly_accounting: Sequence[CandidateBMonthAccounting], *, stress: bool
) -> ConcentrationResult:
    items = tuple(monthly_accounting)
    if not items:
        raise ValueError("concentration requires monthly accounting")
    cumulative = [0.0] * len(CURRENCIES)
    for month in items:
        scenario = month.stress if stress else month.headline
        for index, contribution in enumerate(scenario.currency_net_contributions.values):
            cumulative[index] += contribution
    denominator = math.fsum(abs(item) for item in cumulative)
    if denominator == 0 or not math.isfinite(denominator):
        raise ValueError("concentration denominator is undefined")
    shares = FloatVector(tuple(abs(item) / denominator for item in cumulative))
    maximum = max(shares.values)
    return ConcentrationResult(shares, maximum, maximum <= 0.50)


def _parse_month(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
        raise ValueError("formation month must use YYYY-MM")
    try:
        year, month = int(value[:4]), int(value[5:])
        date(year, month, 1)
    except (ValueError, TypeError) as exc:
        raise ValueError("formation month must use YYYY-MM") from exc
    return year, month


def _next_month(value: str) -> str:
    year, month = _parse_month(value)
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def validate_formation_month(formation_month: str) -> FormationPeriod:
    exit_month = _next_month(formation_month)
    exit_year, exit_number = _parse_month(exit_month)
    if date(exit_year, exit_number, 1) > SEALED_MAXIMUM_DATE:
        raise ValueError("sealed_window_violation")
    if formation_month == PURGED_MONTH:
        return FormationPeriod(formation_month, exit_month, ResearchSplit.PURGED, True)
    if formation_month in TRAIN_MONTHS:
        return FormationPeriod(formation_month, exit_month, ResearchSplit.TRAIN, False)
    if formation_month in VALIDATION_MONTHS:
        return FormationPeriod(formation_month, exit_month, ResearchSplit.VALIDATION, False)
    raise ValueError("formation month is outside the frozen Candidate B calendar")


def expected_formation_calendar() -> tuple[FormationPeriod, ...]:
    ordered = TRAIN_MONTHS + (PURGED_MONTH,) + VALIDATION_MONTHS
    return tuple(validate_formation_month(item) for item in ordered)


def validate_measured_formation_months(months: Sequence[str]) -> tuple[FormationPeriod, ...]:
    supplied = tuple(months)
    if supplied != MEASURED_MONTHS:
        raise ValueError("formation months violate the exact 83/23/106 contract")
    return tuple(validate_formation_month(item) for item in supplied)


def create_candidate_b_cohort(
    *,
    cohort_id: str,
    formation_month: str,
    formation_at: datetime,
    cutoff_at: datetime,
    exit_at: datetime,
    source_qualification_id: str,
    policy_rates: Mapping[str, object],
    start_prices: Mapping[str, object],
    end_prices: Mapping[str, object],
) -> CandidateBCohort:
    """Validate dates and identities before accessing any economic value."""
    period = validate_formation_month(formation_month)
    if period.purged:
        raise ValueError("purged formation cannot be measured")
    expected_cutoff = (
        datetime.combine(formation_at.date(), time.min, tzinfo=UTC)
        if isinstance(formation_at, datetime)
        else None
    )
    if (
        not isinstance(formation_at, datetime)
        or not isinstance(cutoff_at, datetime)
        or not isinstance(exit_at, datetime)
        or formation_at.utcoffset() != UTC.utcoffset(formation_at)
        or cutoff_at.utcoffset() != UTC.utcoffset(cutoff_at)
        or exit_at.utcoffset() != UTC.utcoffset(exit_at)
        or formation_at.strftime("%Y-%m") != period.formation_month
        or exit_at.strftime("%Y-%m") != period.exit_month
        or cutoff_at != expected_cutoff
        or exit_at <= formation_at
        or exit_at.date() > SEALED_MAXIMUM_DATE
    ):
        raise ValueError("cohort timestamps violate the frozen Candidate B calendar")
    if (
        not isinstance(cohort_id, str)
        or not cohort_id
        or len(cohort_id) > 128
        or not isinstance(source_qualification_id, str)
        or len(source_qualification_id) != 64
        or any(character not in "0123456789abcdef" for character in source_qualification_id)
    ):
        raise ValueError("cohort identity is invalid")
    if set(policy_rates) != set(CURRENCIES) | {"USD"}:
        raise ValueError("rates require seven foreign currencies and USD")
    if set(start_prices) != set(PAIRS) or set(end_prices) != set(PAIRS):
        raise ValueError("prices require the frozen seven-pair universe")
    signal = _policy_differentials(
        {currency: policy_rates[currency] for currency in CURRENCIES}, policy_rates["USD"]
    )
    normalized = FloatVector.from_mapping(
        {
            currency: _normalize_quote_return(
                PAIR_BY_CURRENCY[currency],
                start_prices[PAIR_BY_CURRENCY[currency]],
                end_prices[PAIR_BY_CURRENCY[currency]],
            )
            for currency in CURRENCIES
        }
    )
    result = object.__new__(CandidateBCohort)
    for name, value in (
        ("cohort_id", cohort_id),
        ("formation_month", period.formation_month),
        ("exit_month", period.exit_month),
        ("formation_at", formation_at),
        ("cutoff_at", cutoff_at),
        ("exit_at", exit_at),
        ("split", period.split),
        ("source_qualification_id", source_qualification_id),
        ("differentials", signal),
        ("normalized_returns", normalized),
    ):
        object.__setattr__(result, name, value)
    return result


def _measure_split(
    cohorts: tuple[CandidateBCohort, ...],
    *,
    split: ResearchSplit,
    expected_months: tuple[str, ...],
) -> CandidateBSplitMeasurement:
    for cohort in cohorts:
        period = validate_formation_month(cohort.formation_month)
        if (
            not isinstance(cohort.formation_at, datetime)
            or not isinstance(cohort.cutoff_at, datetime)
            or not isinstance(cohort.exit_at, datetime)
        ):
            raise ValueError("cohort evidence is invalid")
        expected_cutoff = datetime.combine(cohort.formation_at.date(), time.min, tzinfo=UTC)
        if (
            cohort.exit_month != period.exit_month
            or cohort.formation_at.utcoffset() != UTC.utcoffset(cohort.formation_at)
            or cohort.cutoff_at.utcoffset() != UTC.utcoffset(cohort.cutoff_at)
            or cohort.exit_at.utcoffset() != UTC.utcoffset(cohort.exit_at)
            or cohort.formation_at.strftime("%Y-%m") != period.formation_month
            or cohort.exit_at.strftime("%Y-%m") != period.exit_month
            or cohort.cutoff_at != expected_cutoff
            or cohort.exit_at <= cohort.formation_at
            or cohort.exit_at.date() > SEALED_MAXIMUM_DATE
            or cohort.split is not period.split
            or period.purged
            or not isinstance(cohort.differentials, DecimalVector)
            or not isinstance(cohort.normalized_returns, FloatVector)
            or not isinstance(cohort.source_qualification_id, str)
            or len(cohort.source_qualification_id) != 64
            or any(
                character not in "0123456789abcdef" for character in cohort.source_qualification_id
            )
        ):
            raise ValueError("cohort evidence is invalid")
    if (
        len(cohorts) != len(expected_months)
        or tuple(item.formation_month for item in cohorts) != expected_months
        or any(item.split is not split for item in cohorts)
        or len({item.cohort_id for item in cohorts}) != len(cohorts)
        or len({item.source_qualification_id for item in cohorts}) != 1
    ):
        raise ValueError("cohorts violate the frozen Candidate B split contract")
    previous = _zero_weights()
    measured: list[CandidateBMeasuredCohort] = []
    for index, cohort in enumerate(cohorts):
        weights = _frozen_portfolio_weights(cohort.differentials)
        accounting = _account_month(
            previous,
            weights,
            cohort.normalized_returns,
            terminal_liquidation=index == len(cohorts) - 1,
        )
        measured.append(CandidateBMeasuredCohort(cohort, weights, accounting))
        previous = weights
    headline = tuple(item.accounting.headline.net_return for item in measured)
    stress = tuple(item.accounting.stress.net_return for item in measured)
    return CandidateBSplitMeasurement(
        split,
        tuple(measured),
        _compound_equity(headline),
        _compound_equity(stress),
    )


def measure_candidate_b_splits(
    *,
    train: Sequence[CandidateBCohort],
    validation: Sequence[CandidateBCohort],
) -> CandidateBMeasurement:
    """Measure the two frozen splits with independent state and mandatory liquidation."""
    train_items = tuple(train)
    validation_items = tuple(validation)
    if any(not isinstance(item, CandidateBCohort) for item in train_items + validation_items):
        raise ValueError("measurement requires validated Candidate B cohorts")
    if len({item.source_qualification_id for item in train_items + validation_items}) != 1:
        raise ValueError("all cohorts must bind one qualification identity")
    result = CandidateBMeasurement(
        _measure_split(train_items, split=ResearchSplit.TRAIN, expected_months=TRAIN_MONTHS),
        _measure_split(
            validation_items,
            split=ResearchSplit.VALIDATION,
            expected_months=VALIDATION_MONTHS,
        ),
    )
    if result.total_measured_cohorts != 106:
        raise ValueError("measurement violates the exact 83/23/106 contract")
    return result


def _measure_synthetic_month(
    *,
    formation_month: str,
    rates: Mapping[str, object],
    prices_start: Mapping[str, object],
    prices_end: Mapping[str, object],
) -> tuple[DecimalVector, FloatVector]:
    """A narrow synthetic convenience path; calendar validation deliberately occurs first."""
    validate_formation_month(formation_month)
    if set(rates) != set(CURRENCIES) | {"USD"}:
        raise ValueError("rates require seven foreign currencies and USD")
    if set(prices_start) != set(PAIRS) or set(prices_end) != set(PAIRS):
        raise ValueError("prices require the frozen seven-pair universe")
    signal = _policy_differentials(
        {currency: rates[currency] for currency in CURRENCIES}, rates["USD"]
    )
    normalized = FloatVector.from_mapping(
        {
            currency: _normalize_quote_return(
                PAIR_BY_CURRENCY[currency],
                prices_start[PAIR_BY_CURRENCY[currency]],
                prices_end[PAIR_BY_CURRENCY[currency]],
            )
            for currency in CURRENCIES
        }
    )
    return signal, normalized
