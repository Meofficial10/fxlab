"""Focused contracts for Phase 18 point-in-time FX valuation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from fxlab.execution.valuation import (
    ConversionQuote,
    FxInstrumentCatalog,
    FxValuationEngine,
    InstrumentSpec,
    InstrumentValuationProvider,
    PipValuation,
    ValuationFailure,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _spec(
    symbol: str,
    base: str,
    quote: str,
    *,
    pip_size: float = 0.0001,
) -> InstrumentSpec:
    return InstrumentSpec(
        canonical_symbol=symbol,
        instrument_class="fx",
        base_currency=base,
        quote_currency=quote,
        pip_size=pip_size,
        contract_units_per_lot=100_000,
        specification_version="1",
    )


def _catalog(*specs: InstrumentSpec) -> FxInstrumentCatalog:
    return FxInstrumentCatalog(specs or (_spec("EURUSD", "EUR", "USD"),))


def _quote(
    symbol: str,
    bid: float,
    ask: float,
    *,
    observed_at: datetime = NOW,
) -> ConversionQuote:
    return ConversionQuote(symbol, bid, ask, observed_at, "replay-dataset-1")


def test_contracts_are_frozen_and_provider_is_structural() -> None:
    spec = _spec("EURUSD", "EUR", "USD")
    quote = _quote("EURUSD", 1.1, 1.2)
    valuation = FxValuationEngine(_catalog(spec), max_age=timedelta(minutes=5)).pip_valuation(
        "EURUSD", "USD", NOW, ()
    )
    with pytest.raises(FrozenInstanceError):
        spec.pip_size = 1.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        quote.bid = 2.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        valuation.pip_value_per_lot = 2.0  # type: ignore[misc]
    assert isinstance(
        type("Provider", (), {"pip_valuation": lambda *_args: valuation})(),
        InstrumentValuationProvider,
    )


@pytest.mark.parametrize("currency", ["", "usd", "US1", " USD", 123, True])
def test_currency_and_instrument_validation_is_strict(currency: object) -> None:
    with pytest.raises(ValueError):
        InstrumentSpec("EURUSD", "fx", currency, "USD", 0.0001, 100_000, "1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"canonical_symbol": "EUR_USD"}, "canonical_symbol"),
        ({"instrument_class": "metal"}, "instrument_class"),
        ({"pip_size": float("nan")}, "pip_size"),
        ({"pip_size": 0.0}, "pip_size"),
        ({"contract_units_per_lot": -1}, "contract_units_per_lot"),
        ({"specification_version": "token=secret"}, "specification_version"),
    ],
)
def test_malformed_instrument_spec_is_rejected(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "canonical_symbol": "EURUSD",
        "instrument_class": "fx",
        "base_currency": "EUR",
        "quote_currency": "USD",
        "pip_size": 0.0001,
        "contract_units_per_lot": 100_000,
        "specification_version": "1",
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        InstrumentSpec(**values)  # type: ignore[arg-type]


def test_catalog_identity_is_stable_and_mapping_order_independent() -> None:
    eurusd = _spec("EURUSD", "EUR", "USD")
    usdjpy = _spec("USDJPY", "USD", "JPY", pip_size=0.01)
    first = _catalog(eurusd, usdjpy)
    second = _catalog(usdjpy, eurusd)
    assert first.fingerprint == second.fingerprint
    assert first.symbols == ("EURUSD", "USDJPY")
    with pytest.raises(ValueError, match="duplicate"):
        _catalog(eurusd, eurusd)


def test_direct_quote_currency_valuation_needs_no_market_quote() -> None:
    engine = FxValuationEngine(_catalog(_spec("EURUSD", "EUR", "USD")))
    result = engine.pip_valuation("EURUSD", "USD", NOW, ())
    assert isinstance(result, PipValuation)
    assert result.pip_value_per_lot == 10.0
    assert result.positive_conversion_rate == 1.0
    assert result.negative_conversion_rate == 1.0
    assert result.route_identity == "quote-equals-account"
    assert result.observation_time is None
    assert result.convert_signed(25.0) == 25.0
    assert result.convert_signed(-25.0) == -25.0


def test_direct_quote_account_route_uses_bid_for_profit_and_ask_for_loss() -> None:
    engine = FxValuationEngine(
        _catalog(
            _spec("EURGBP", "EUR", "GBP"),
            _spec("GBPUSD", "GBP", "USD"),
        )
    )
    result = engine.pip_valuation(
        "EURGBP", "USD", NOW, (_quote("GBPUSD", 1.25, 1.26),)
    )
    assert result.route_identity == "direct:GBPUSD"
    assert result.positive_conversion_rate == 1.25
    assert result.negative_conversion_rate == 1.26
    assert result.pip_value_per_lot == 12.6
    assert result.convert_signed(10.0) == 12.5
    assert result.convert_signed(-10.0) == -12.6


def test_inverse_account_quote_route_and_usdjpy_use_conservative_loss_side() -> None:
    engine = FxValuationEngine(
        _catalog(_spec("USDJPY", "USD", "JPY", pip_size=0.01))
    )
    result = engine.pip_valuation(
        "USDJPY", "USD", NOW, (_quote("USDJPY", 149.0, 150.0),)
    )
    assert result.route_identity == "inverse:USDJPY"
    assert result.positive_conversion_rate == pytest.approx(1 / 150.0)
    assert result.negative_conversion_rate == pytest.approx(1 / 149.0)
    assert result.pip_value_per_lot == pytest.approx(1000.0 / 149.0)
    assert result.convert_signed(1000.0) == pytest.approx(1000.0 / 150.0)
    assert result.convert_signed(-1000.0) == pytest.approx(-1000.0 / 149.0)


def test_exact_freshness_boundary_is_accepted() -> None:
    engine = FxValuationEngine(
        _catalog(
            _spec("EURGBP", "EUR", "GBP"),
            _spec("GBPUSD", "GBP", "USD"),
        ),
        max_age=timedelta(minutes=5),
    )
    result = engine.pip_valuation(
        "EURGBP",
        "USD",
        NOW,
        (_quote("GBPUSD", 1.25, 1.26, observed_at=NOW - timedelta(minutes=5)),),
    )
    assert result.observation_time == NOW - timedelta(minutes=5)


def test_pip_valuation_itself_rejects_future_observation_evidence() -> None:
    engine = FxValuationEngine(
        _catalog(
            _spec("EURGBP", "EUR", "GBP"),
            _spec("GBPUSD", "GBP", "USD"),
        )
    )
    valuation = engine.pip_valuation(
        "EURGBP", "USD", NOW, (_quote("GBPUSD", 1.25, 1.26),)
    )
    with pytest.raises(ValueError, match="later than as_of"):
        replace(valuation, observation_time=NOW + timedelta(microseconds=1))


@pytest.mark.parametrize(
    ("observed_at", "reason"),
    [
        (NOW - timedelta(minutes=5, microseconds=1), "stale_conversion_quote"),
        (NOW + timedelta(microseconds=1), "future_conversion_quote"),
    ],
)
def test_stale_and_future_quotes_fail_closed(observed_at: datetime, reason: str) -> None:
    engine = FxValuationEngine(
        _catalog(
            _spec("EURGBP", "EUR", "GBP"),
            _spec("GBPUSD", "GBP", "USD"),
        ),
        max_age=timedelta(minutes=5),
    )
    with pytest.raises(ValuationFailure, match=reason):
        engine.pip_valuation(
            "EURGBP", "USD", NOW, (_quote("GBPUSD", 1.25, 1.26, observed_at=observed_at),)
        )


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(float("nan"), 1.2), (1.1, float("inf")), (0.0, 1.0), (1.2, 1.1)],
)
def test_conversion_quote_rejects_invalid_prices(bid: float, ask: float) -> None:
    with pytest.raises(ValueError):
        _quote("GBPUSD", bid, ask)


def test_missing_route_multi_hop_and_unsupported_instrument_fail_closed() -> None:
    engine = FxValuationEngine(
        _catalog(
            _spec("EURGBP", "EUR", "GBP"),
            _spec("GBPJPY", "GBP", "JPY", pip_size=0.01),
            _spec("USDJPY", "USD", "JPY", pip_size=0.01),
        )
    )
    with pytest.raises(ValuationFailure, match="conversion_route_unavailable"):
        engine.pip_valuation(
            "EURGBP",
            "USD",
            NOW,
            (
                _quote("GBPJPY", 190.0, 191.0),
                _quote("USDJPY", 149.0, 150.0),
            ),
        )
    with pytest.raises(ValuationFailure, match="instrument_unsupported"):
        engine.pip_valuation("XAUUSD", "USD", NOW, ())


def test_valuation_id_is_deterministic_and_behavior_sensitive() -> None:
    engine = FxValuationEngine(
        _catalog(
            _spec("EURGBP", "EUR", "GBP"),
            _spec("GBPUSD", "GBP", "USD"),
        )
    )
    quote = _quote("GBPUSD", 1.25, 1.26)
    first = engine.pip_valuation("EURGBP", "USD", NOW, (quote,))
    second = engine.pip_valuation("EURGBP", "USD", NOW, (quote,))
    changed = engine.pip_valuation(
        "EURGBP", "USD", NOW, (_quote("GBPUSD", 1.24, 1.26),)
    )
    assert first.valuation_id == second.valuation_id
    assert first.valuation_id != changed.valuation_id
    with pytest.raises(ValueError, match="does not match"):
        replace(first, valuation_id="0" * 64)


def test_secret_like_source_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="source_identity"):
        ConversionQuote("EURUSD", 1.1, 1.2, NOW, "authorization-token")


def test_engine_has_no_wall_clock_or_network_surface() -> None:
    public = {
        name
        for name in dir(FxValuationEngine)
        if not name.startswith("_") and callable(getattr(FxValuationEngine, name))
    }
    assert public == {"convert_base_notional", "pip_valuation"}
