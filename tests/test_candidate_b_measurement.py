from __future__ import annotations

import ast
import inspect
import math
from calendar import monthrange
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fxlab.research import candidate_b_measurement as measurement
from oracles.candidate_b_reference import (
    STUDENT_T_95_GOLDENS,
    circular_bootstrap_reference,
    frozen_weights,
    hac_fraction_goldens,
    normalized_return,
)

D = Decimal
CURRENCIES = ("AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD")


def differentials() -> dict[str, Decimal]:
    return {
        "AUD": D("1"),
        "CAD": D("0"),
        "CHF": D("-1"),
        "EUR": D("0.5"),
        "GBP": D("2"),
        "JPY": D("-2"),
        "NZD": D("1"),
    }


def test_oracle_is_structurally_independent() -> None:
    source = Path("tests/oracles/candidate_b_reference.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any("candidate_b_measurement" in item for item in imports)
    assert not imports.intersection({"socket", "urllib", "requests", "httpx"})
    assert "open(" not in source


@pytest.mark.parametrize(
    ("pair", "start", "end", "expected"),
    [
        ("AUDUSD", 1.0, 1.1, 0.1),
        ("EURUSD", 1.2, 1.08, -0.1),
        ("USDJPY", 100.0, 90.0, 100.0 / 90.0 - 1.0),
    ],
)
def test_quote_normalization_goldens(pair: str, start: float, end: float, expected: float) -> None:
    actual = measurement._normalize_quote_return(pair, start, end)
    assert actual == pytest.approx(expected)


def test_direct_inverse_economic_equivalence() -> None:
    oracle_direct = normalized_return(D("1.00"), D("1.10"), inverse=False)
    oracle_inverse = normalized_return(D("1"), D("1") / D("1.10"), inverse=True)
    direct = measurement._normalize_quote_return("AUDUSD", 1.0, 1.1)
    inverse = measurement._normalize_quote_return("USDCAD", 1.0, 1.0 / 1.1)
    assert direct == pytest.approx(float(oracle_direct))
    assert inverse == pytest.approx(float(oracle_inverse))
    assert direct == pytest.approx(inverse)


@pytest.mark.parametrize("value", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_invalid_prices_fail_closed(value: float) -> None:
    with pytest.raises(ValueError):
        measurement._normalize_quote_return("AUDUSD", value, 1.0)
    with pytest.raises(ValueError):
        measurement._normalize_quote_return("AUDUSD", 1.0, value)


def test_signal_ranking_ties_and_frozen_weights() -> None:
    rates = {currency: value + D("3") for currency, value in differentials().items()}
    actual = measurement._policy_differentials(rates, D("3"))
    assert actual.as_dict() == differentials()
    assert measurement._rank_currencies(actual) == ("GBP", "AUD", "NZD", "EUR", "CAD", "CHF", "JPY")
    weights = measurement._frozen_portfolio_weights(actual)
    assert weights.as_dict() == frozen_weights(differentials())
    assert set(weights.long_currencies) == {"GBP", "AUD"}
    assert set(weights.short_currencies) == {"CHF", "JPY"}
    assert sum(abs(item) for item in weights.values) == D("1.00")
    assert sum(weights.values) == D("0.00")


def test_signal_contract_rejects_wrong_universe() -> None:
    rates = {currency: D("1") for currency in CURRENCIES[:-1]}
    with pytest.raises(ValueError):
        measurement._policy_differentials(rates, D("0"))


def test_turnover_costs_entry_unchanged_partial_reversal_and_liquidation() -> None:
    first = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(differentials())
    )
    zero = measurement._zero_weights()
    assert measurement._turnover(zero, first) == D("1.00")
    assert measurement._turnover(first, first) == D("0.00")

    shifted = dict(differentials())
    shifted["CAD"] = D("3")
    second = measurement._frozen_portfolio_weights(measurement.DecimalVector.from_mapping(shifted))
    assert measurement._turnover(first, second) == D("0.50")

    reversed_signal = {currency: -value for currency, value in differentials().items()}
    reversed_book = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(reversed_signal)
    )
    assert measurement._turnover(first, reversed_book) == D("2.00")

    returns = measurement.FloatVector.from_mapping({currency: 0.0 for currency in CURRENCIES})
    entry = measurement._account_month(zero, first, returns)
    unchanged = measurement._account_month(first, first, returns)
    terminal = measurement._account_month(first, first, returns, terminal_liquidation=True)
    assert entry.headline.cost > 0
    assert unchanged.headline.cost == 0
    assert terminal.headline.cost == entry.headline.cost
    assert terminal.cohort_count == 1
    assert terminal.stress.cost == pytest.approx(1.5 * terminal.headline.cost)
    assert terminal.stress.net_return < terminal.headline.net_return


def test_validation_reset_recharges_initial_entry() -> None:
    weights = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(differentials())
    )
    returns = measurement.FloatVector.from_mapping({currency: 0.0 for currency in CURRENCIES})
    train_entry = measurement._account_month(measurement._zero_weights(), weights, returns)
    validation_entry = measurement._account_month(measurement._zero_weights(), weights, returns)
    assert train_entry == validation_entry


def test_gross_net_accounting_matches_hand_calculation() -> None:
    weights = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(differentials())
    )
    returns_map = {currency: 0.01 * (index + 1) for index, currency in enumerate(CURRENCIES)}
    returns = measurement.FloatVector.from_mapping(returns_map)
    month = measurement._account_month(measurement._zero_weights(), weights, returns)
    expected_gross = sum(
        float(weight) * returns_map[currency] for currency, weight in weights.items()
    )
    assert month.gross_return == pytest.approx(expected_gross)
    assert month.headline.net_return == pytest.approx(expected_gross - month.headline.cost)


def test_equity_compounding_and_drawdown_include_initial_peak() -> None:
    returns = (0.10, -0.20, 0.05)
    equity = measurement._compound_equity(returns)
    assert equity == pytest.approx((1.0, 1.1, 0.88, 0.924))
    assert measurement._maximum_drawdown(returns) == pytest.approx(0.20)
    assert measurement._maximum_drawdown((-0.10,)) == pytest.approx(0.10)


def test_sharpe_uses_sample_std_and_sqrt_12() -> None:
    values = (0.01, 0.02, 0.03)
    mean = sum(values) / 3
    sample_std = math.sqrt(sum((item - mean) ** 2 for item in values) / 2)
    assert measurement._annualized_sharpe(values) == pytest.approx(
        math.sqrt(12) * mean / sample_std
    )


@pytest.mark.parametrize("values", [(), (0.1,), (0.1, 0.1), (0.1, math.nan)])
def test_sharpe_invalid_inputs_cannot_pass(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        measurement._annualized_sharpe(values)


def test_hac_fraction_golden_and_student_t_path() -> None:
    values = (0.01, -0.02, 0.03, 0.0, 0.02)
    expected = hac_fraction_goldens()
    result = measurement._hac_mean_inference(values)
    assert result.mean == pytest.approx(float(expected["mean"]))
    assert result.gammas == pytest.approx(tuple(float(item) for item in expected["gammas"]))
    assert result.bartlett_weights == pytest.approx(
        tuple(float(item) for item in expected["weights"])
    )
    assert result.long_run_variance == pytest.approx(float(expected["lrv"]))
    assert result.variance_of_mean == pytest.approx(float(expected["variance_of_mean"]))
    assert result.df == 4
    assert result.lower_bound == pytest.approx(
        result.mean - result.critical_value * result.standard_error
    )


@pytest.mark.parametrize(("df", "golden"), STUDENT_T_95_GOLDENS.items())
def test_student_t_critical_values_against_independent_goldens(df: int, golden: float) -> None:
    assert measurement._student_t_critical_value(df) == pytest.approx(golden, rel=0, abs=5e-12)


def test_candidate_b_validation_student_t_df_is_22() -> None:
    result = measurement._hac_mean_inference(tuple(0.001 + index * 1e-7 for index in range(23)))
    assert result.df == 22
    assert result.critical_value == pytest.approx(STUDENT_T_95_GOLDENS[22], abs=5e-12)


def test_bootstrap_matches_independent_oracle_and_frozen_digest() -> None:
    values = (0.01, -0.02, 0.03, 0.0, 0.02)
    expected_bound, expected_digest = circular_bootstrap_reference(values)
    result = measurement._circular_moving_block_bootstrap(values)
    assert result.lower_bound == pytest.approx(expected_bound)
    assert result.sampled_index_digest == expected_digest
    assert (
        result.sampled_index_digest
        == "853ad887dbe2b1fe4b9199c0a18d9b87fbeeb6782560284cbf540b3c0544006d"
    )
    assert result == measurement._circular_moving_block_bootstrap(values)
    assert result.block_length == 3
    assert result.replications == 10_000
    assert result.seed == 20260829


def test_bootstrap_api_exposes_no_research_parameters() -> None:
    assert tuple(inspect.signature(measurement._circular_moving_block_bootstrap).parameters) == (
        "returns",
    )
    assert tuple(inspect.signature(measurement._hac_mean_inference).parameters) == ("returns",)
    with pytest.raises(TypeError):
        measurement.CandidateBWeights((D("0.25"),) * 7)
    with pytest.raises(TypeError):
        measurement.CandidateBWeights()
    with pytest.raises(TypeError):
        measurement.CandidateBCohort()


def test_concentration_uses_net_currency_contributions_and_zero_denominator_fails() -> None:
    weights = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(differentials())
    )
    returns = measurement.FloatVector.from_mapping({currency: 0.01 for currency in CURRENCIES})
    month = measurement._account_month(
        measurement._zero_weights(), weights, returns, terminal_liquidation=True
    )
    concentration = measurement._currency_concentration((month,), stress=False)
    assert sum(concentration.shares.values) == pytest.approx(1.0)
    assert concentration.maximum_share == max(concentration.shares.values)
    zero_month = measurement._account_month(
        weights, weights, measurement.FloatVector.from_mapping({c: 0.0 for c in CURRENCIES})
    )
    with pytest.raises(ValueError):
        measurement._currency_concentration((zero_month,), stress=False)


def test_exact_calendar_counts_boundary_purge_and_seal() -> None:
    calendar = measurement.expected_formation_calendar()
    assert sum(item.split is measurement.ResearchSplit.TRAIN for item in calendar) == 83
    assert sum(item.split is measurement.ResearchSplit.VALIDATION for item in calendar) == 23
    assert sum(item.purged for item in calendar) == 1
    assert len([item for item in calendar if not item.purged]) == 106
    assert calendar[0].formation_month == "2015-01"
    crossing = next(item for item in calendar if item.formation_month == "2021-12")
    assert crossing.purged
    assert crossing.exit_month == "2022-01"
    with pytest.raises(ValueError, match="sealed_window_violation"):
        measurement.validate_formation_month("2023-12")


def test_shifted_duplicate_or_missing_calendar_fails() -> None:
    months = tuple(
        item.formation_month
        for item in measurement.expected_formation_calendar()
        if not item.purged
    )
    measurement.validate_measured_formation_months(months)
    for invalid in (months[1:], months + (months[-1],), ("2014-12",) + months[:-1]):
        with pytest.raises(ValueError):
            measurement.validate_measured_formation_months(invalid)


class ExplodingValues(dict[str, float]):
    def __iter__(self):
        raise AssertionError("numeric values exposed")

    def items(self):
        raise AssertionError("numeric values exposed")


def test_post_2023_rejected_before_numeric_value_access() -> None:
    with pytest.raises(ValueError, match="sealed_window_violation"):
        measurement._measure_synthetic_month(
            formation_month="2023-12",
            rates=ExplodingValues(),
            prices_start=ExplodingValues(),
            prices_end=ExplodingValues(),
        )


def test_module_has_no_io_discovery_or_parameter_sweep_surface() -> None:
    source = Path(measurement.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    forbidden = {"socket", "urllib", "requests", "httpx", "glob", "pathlib", "subprocess"}
    assert not imports.intersection(forbidden)
    assert not any(
        token in source.lower() for token in ("parameter_grid", "grid_search", "provider_fallback")
    )


def test_frozen_quote_and_cost_mappings_cannot_mutate() -> None:
    with pytest.raises(TypeError):
        measurement.PAIR_BY_CURRENCY["AUD"] = "USDCAD"  # type: ignore[index]
    with pytest.raises(TypeError):
        measurement.ONE_WAY_COST_BY_PAIR["AUDUSD"] = D("0")  # type: ignore[index]


class NumericSentinel:
    def __float__(self) -> float:
        raise AssertionError("numeric value accessed before sealed date validation")

    def __str__(self) -> str:
        raise AssertionError("numeric value accessed before sealed date validation")


def test_dated_cohort_rejects_post_2023_before_any_numeric_value_access() -> None:
    sentinel_rates = {currency: NumericSentinel() for currency in (*CURRENCIES, "USD")}
    sentinel_prices = {pair: NumericSentinel() for pair in measurement.PAIRS}
    with pytest.raises(ValueError, match="sealed_window_violation"):
        measurement.create_candidate_b_cohort(
            cohort_id="sealed_sentinel",
            formation_month="2023-12",
            formation_at=datetime(2023, 12, 29, tzinfo=UTC),
            cutoff_at=datetime(2023, 12, 29, tzinfo=UTC),
            exit_at=datetime(2024, 1, 31, tzinfo=UTC),
            source_qualification_id="a" * 64,
            policy_rates=sentinel_rates,
            start_prices=sentinel_prices,
            end_prices=sentinel_prices,
        )


def test_cohort_identity_and_timestamps_validate_before_numeric_values() -> None:
    sentinel_rates = {currency: NumericSentinel() for currency in (*CURRENCIES, "USD")}
    sentinel_prices = {pair: NumericSentinel() for pair in measurement.PAIRS}
    with pytest.raises(ValueError, match="cohort identity is invalid"):
        measurement.create_candidate_b_cohort(
            cohort_id="identity_sentinel",
            formation_month="2015-01",
            formation_at=datetime(2015, 1, 30, tzinfo=UTC),
            cutoff_at=datetime(2015, 1, 30, tzinfo=UTC),
            exit_at=datetime(2015, 2, 27, tzinfo=UTC),
            source_qualification_id="not-a-digest",
            policy_rates=sentinel_rates,
            start_prices=sentinel_prices,
            end_prices=sentinel_prices,
        )
    with pytest.raises(ValueError, match="cohort timestamps"):
        measurement.create_candidate_b_cohort(
            cohort_id="timestamp_sentinel",
            formation_month="2015-01",
            formation_at=datetime(2015, 1, 30, tzinfo=UTC),
            cutoff_at=datetime(2015, 1, 29, tzinfo=UTC),
            exit_at=datetime(2015, 2, 27, tzinfo=UTC),
            source_qualification_id="a" * 64,
            policy_rates=sentinel_rates,
            start_prices=sentinel_prices,
            end_prices=sentinel_prices,
        )


def _synthetic_cohort(month: str):
    rates = {currency: value + D("3") for currency, value in differentials().items()}
    rates["USD"] = D("3")
    start = {pair: 1.0 for pair in measurement.PAIRS}
    end = {pair: 1.0 for pair in measurement.PAIRS}
    year, month_number = (int(item) for item in month.split("-"))
    formation_at = datetime(year, month_number, monthrange(year, month_number)[1], tzinfo=UTC)
    exit_month = measurement.validate_formation_month(month).exit_month
    exit_year, exit_number = (int(item) for item in exit_month.split("-"))
    exit_at = datetime(exit_year, exit_number, monthrange(exit_year, exit_number)[1], tzinfo=UTC)
    return measurement.create_candidate_b_cohort(
        cohort_id=f"cohort_{month}",
        formation_month=month,
        formation_at=formation_at,
        cutoff_at=formation_at,
        exit_at=exit_at,
        source_qualification_id="a" * 64,
        policy_rates=rates,
        start_prices=start,
        end_prices=end,
    )


def test_split_lifecycle_enforces_entry_reset_terminal_and_exact_counts() -> None:
    train = tuple(_synthetic_cohort(month) for month in measurement.TRAIN_MONTHS)
    validation = tuple(_synthetic_cohort(month) for month in measurement.VALIDATION_MONTHS)
    result = measurement.measure_candidate_b_splits(train=train, validation=validation)
    assert len(result.train.cohorts) == 83
    assert len(result.validation.cohorts) == 23
    assert result.train.cohorts[0].accounting.rebalance_turnover == D("1.00")
    assert result.validation.cohorts[0].accounting.rebalance_turnover == D("1.00")
    assert result.train.cohorts[-1].accounting.liquidation_turnover == D("1.00")
    assert result.validation.cohorts[-1].accounting.liquidation_turnover == D("1.00")
    assert all(item.accounting.liquidation_turnover == 0 for item in result.train.cohorts[:-1])
    assert all(item.accounting.liquidation_turnover == 0 for item in result.validation.cohorts[:-1])
    assert result.total_measured_cohorts == 106


def test_split_lifecycle_rejects_wrong_sequence_despite_correct_count() -> None:
    reordered = (
        measurement.TRAIN_MONTHS[1],
        measurement.TRAIN_MONTHS[0],
    ) + measurement.TRAIN_MONTHS[2:]
    train = tuple(_synthetic_cohort(month) for month in reordered)
    validation = tuple(_synthetic_cohort(month) for month in measurement.VALIDATION_MONTHS)
    with pytest.raises(ValueError):
        measurement.measure_candidate_b_splits(train=train, validation=validation)


def test_split_lifecycle_revalidates_dated_cohort_evidence() -> None:
    train = list(_synthetic_cohort(month) for month in measurement.TRAIN_MONTHS)
    validation = tuple(_synthetic_cohort(month) for month in measurement.VALIDATION_MONTHS)
    object.__setattr__(train[0], "exit_month", "2023-12")
    with pytest.raises(ValueError, match="cohort evidence is invalid"):
        measurement.measure_candidate_b_splits(train=tuple(train), validation=validation)


def test_independent_exact_accounting_goldens() -> None:
    weights = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(differentials())
    )
    returns = measurement.FloatVector.from_mapping(
        {
            "AUD": 0.01,
            "CAD": 0.0,
            "CHF": -0.01,
            "EUR": 0.0,
            "GBP": 0.02,
            "JPY": -0.02,
            "NZD": 0.0,
        }
    )
    entry_and_exit = measurement._account_month(
        measurement._zero_weights(), weights, returns, terminal_liquidation=True
    )
    assert entry_and_exit.gross_return == pytest.approx(0.015, rel=0, abs=1e-15)
    assert entry_and_exit.headline.cost == pytest.approx(0.00022, rel=0, abs=1e-15)
    assert entry_and_exit.headline.net_return == pytest.approx(0.01478, rel=0, abs=1e-15)
    assert entry_and_exit.headline.currency_net_contributions.values == pytest.approx(
        (0.00244, 0.0, 0.00244, 0.0, 0.00495, 0.00495, 0.0), rel=0, abs=1e-15
    )


def test_independent_turnover_cost_goldens_cover_every_lifecycle_transition() -> None:
    zero = measurement._zero_weights()
    first = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(differentials())
    )
    partial = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(
            {
                "AUD": D("3"),
                "CAD": D("7"),
                "CHF": D("1"),
                "EUR": D("2"),
                "GBP": D("6"),
                "JPY": D("0"),
                "NZD": D("4"),
            }
        )
    )
    reversed_book = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(
            {
                "AUD": D("0"),
                "CAD": D("3"),
                "CHF": D("6"),
                "EUR": D("4"),
                "GBP": D("1"),
                "JPY": D("7"),
                "NZD": D("2"),
            }
        )
    )
    flat_returns = measurement.FloatVector((0.0,) * 7)

    entry = measurement._account_month(zero, first, flat_returns)
    unchanged = measurement._account_month(first, first, flat_returns)
    partial_replacement = measurement._account_month(first, partial, flat_returns)
    full_reversal = measurement._account_month(first, reversed_book, flat_returns)
    terminal = measurement._account_month(first, first, flat_returns, terminal_liquidation=True)
    validation_reentry = measurement._account_month(zero, first, flat_returns)

    assert (entry.rebalance_turnover, entry.headline.cost) == (D("1.00"), 0.00011)
    assert (unchanged.rebalance_turnover, unchanged.headline.cost) == (D("0.00"), 0.0)
    assert (partial_replacement.rebalance_turnover, partial_replacement.headline.cost) == (
        D("0.50"),
        0.00006,
    )
    assert (full_reversal.rebalance_turnover, full_reversal.headline.cost) == (
        D("2.00"),
        0.00022,
    )
    assert (terminal.liquidation_turnover, terminal.headline.cost) == (D("1.00"), 0.00011)
    assert (validation_reentry.rebalance_turnover, validation_reentry.headline.cost) == (
        D("1.00"),
        0.00011,
    )


def test_split_lifecycle_exact_multi_cohort_cost_goldens_and_state_isolation() -> None:
    train = tuple(_synthetic_cohort(month) for month in measurement.TRAIN_MONTHS)
    validation = tuple(_synthetic_cohort(month) for month in measurement.VALIDATION_MONTHS)
    result = measurement.measure_candidate_b_splits(train=train, validation=validation)

    train_costs = tuple(item.accounting.headline.cost for item in result.train.cohorts)
    validation_costs = tuple(item.accounting.headline.cost for item in result.validation.cohorts)
    assert train_costs == (0.00011,) + (0.0,) * 81 + (0.00011,)
    assert validation_costs == (0.00011,) + (0.0,) * 21 + (0.00011,)
    assert len(result.train.cohorts) == 83
    assert len(result.validation.cohorts) == 23


def test_public_measurement_boundary_exposes_no_lifecycle_overrides() -> None:
    assert tuple(inspect.signature(measurement.measure_candidate_b_splits).parameters) == (
        "train",
        "validation",
    )
    for public_name in (
        "account_month",
        "turnover",
        "frozen_portfolio_weights",
        "normalize_quote_return",
        "hac_mean_inference",
        "circular_moving_block_bootstrap",
    ):
        assert not hasattr(measurement, public_name)


def test_independent_concentration_goldens_and_boundary() -> None:
    zero = measurement._zero_weights()
    weights = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(differentials())
    )
    balanced = measurement._account_month(
        weights,
        weights,
        measurement.FloatVector.from_mapping(
            {
                "AUD": 0.01,
                "CAD": 0.0,
                "CHF": 0.0,
                "EUR": 0.0,
                "GBP": 0.01,
                "JPY": 0.0,
                "NZD": 0.0,
            }
        ),
    )
    at_boundary = measurement._currency_concentration((balanced,), stress=False)
    assert at_boundary.maximum_share == pytest.approx(0.5, rel=0, abs=1e-15)
    assert at_boundary.passes
    concentrated = measurement._account_month(
        weights,
        weights,
        measurement.FloatVector.from_mapping(
            {
                "AUD": 0.01,
                "CAD": 0.0,
                "CHF": 0.0,
                "EUR": 0.0,
                "GBP": 0.0,
                "JPY": 0.0,
                "NZD": 0.0,
            }
        ),
    )
    over_boundary = measurement._currency_concentration((concentrated,), stress=False)
    assert over_boundary.maximum_share == pytest.approx(1.0, rel=0, abs=1e-15)
    assert not over_boundary.passes
    with pytest.raises(ValueError):
        measurement._currency_concentration(
            (measurement._account_month(zero, zero, measurement.FloatVector((0.0,) * 7)),),
            stress=False,
        )


def test_independent_currency_contribution_and_share_goldens() -> None:
    weights = measurement._frozen_portfolio_weights(
        measurement.DecimalVector.from_mapping(differentials())
    )
    month = measurement._account_month(
        measurement._zero_weights(),
        weights,
        measurement.FloatVector.from_mapping(
            {
                "AUD": 0.01,
                "CAD": 0.0,
                "CHF": -0.01,
                "EUR": 0.0,
                "GBP": 0.02,
                "JPY": -0.02,
                "NZD": 0.0,
            }
        ),
        terminal_liquidation=True,
    )
    expected_contributions = (0.00244, 0.0, 0.00244, 0.0, 0.00495, 0.00495, 0.0)
    expected_denominator = 0.01478
    expected_shares = tuple(abs(value) / expected_denominator for value in expected_contributions)
    result = measurement._currency_concentration((month,), stress=False)
    assert month.headline.currency_net_contributions.values == pytest.approx(
        expected_contributions, rel=0, abs=1e-15
    )
    assert result.shares.values == pytest.approx(expected_shares, rel=0, abs=1e-15)
    assert result.maximum_share == pytest.approx(0.00495 / 0.01478, rel=0, abs=1e-15)
