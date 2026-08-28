"""Focused contracts for Phase 18 deterministic paper margin."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from fxlab.execution.margin import (
    FixedLeveragePaperMargin,
    MarginDescriptor,
    MarginExposure,
    MarginResult,
    PaperMarginModel,
    UnmodeledPaperMargin,
)
from fxlab.execution.valuation import (
    ConversionQuote,
    FxInstrumentCatalog,
    FxValuationEngine,
    InstrumentSpec,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _spec(symbol: str, base: str, quote: str, pip: float = 0.0001) -> InstrumentSpec:
    return InstrumentSpec(symbol, "fx", base, quote, pip, 100_000, "1")


def _engine(*specs: InstrumentSpec) -> FxValuationEngine:
    return FxValuationEngine(FxInstrumentCatalog(specs), timedelta(minutes=5))


def _quote(symbol: str, bid: float, ask: float) -> ConversionQuote:
    return ConversionQuote(symbol, bid, ask, NOW, "replay-dataset-1")


def test_margin_contracts_are_frozen_and_protocol_is_structural() -> None:
    model = UnmodeledPaperMargin("USD")
    result = model.calculate((), equity=10_000, as_of=NOW, valuation=None, quotes=())
    assert isinstance(model.descriptor, MarginDescriptor)
    assert isinstance(result, MarginResult)
    assert isinstance(model, PaperMarginModel)
    with pytest.raises(FrozenInstanceError):
        model.account_currency = "EUR"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.margin_used = 1.0  # type: ignore[misc]


def test_unmodeled_margin_is_explicit_and_deterministic() -> None:
    model = UnmodeledPaperMargin("USD")
    first = model.calculate((), equity=10_000, as_of=NOW, valuation=None, quotes=())
    second = model.calculate((), equity=10_000, as_of=NOW, valuation=None, quotes=())
    assert model.descriptor.modeled is False
    assert model.descriptor.quality == "unmodeled-paper-margin"
    assert model.descriptor.model_id == "unmodeled-paper-margin-v1"
    assert first.margin_used == 0.0
    assert first.margin_available == 10_000.0
    assert first.sufficient is True
    assert first.result_id == second.result_id


@pytest.mark.parametrize("leverage", [0, -1, float("nan"), float("inf"), True])
def test_fixed_leverage_rejects_invalid_leverage(leverage: object) -> None:
    with pytest.raises(ValueError, match="leverage"):
        FixedLeveragePaperMargin("USD", {"EURUSD": leverage})  # type: ignore[dict-item]


def test_fixed_leverage_margin_uses_conservative_base_conversion() -> None:
    valuation = _engine(_spec("EURUSD", "EUR", "USD"))
    model = FixedLeveragePaperMargin("USD", {"EURUSD": 20})
    result = model.calculate(
        (MarginExposure("EURUSD", 1.0),),
        equity=10_000,
        as_of=NOW,
        valuation=valuation,
        quotes=(_quote("EURUSD", 1.10, 1.11),),
    )
    assert result.margin_used == pytest.approx(100_000 * 1.11 / 20)
    assert result.margin_available == pytest.approx(10_000 - 100_000 * 1.11 / 20)
    assert result.sufficient is True


def test_inverse_base_conversion_is_supported_explicitly() -> None:
    valuation = _engine(
        _spec("EURGBP", "EUR", "GBP"),
        _spec("USDEUR", "USD", "EUR"),
    )
    model = FixedLeveragePaperMargin("USD", {"EURGBP": 10})
    result = model.calculate(
        (MarginExposure("EURGBP", 0.5),),
        equity=10_000,
        as_of=NOW,
        valuation=valuation,
        quotes=(_quote("USDEUR", 0.90, 0.91),),
    )
    assert result.margin_used == pytest.approx(50_000 / 0.90 / 10)


def test_opposite_positions_are_gross_additive_not_netted() -> None:
    valuation = _engine(_spec("EURUSD", "EUR", "USD"))
    model = FixedLeveragePaperMargin("USD", {"EURUSD": 20})
    exposures = (
        MarginExposure("EURUSD", 0.5, side=1),
        MarginExposure("EURUSD", 0.5, side=-1),
    )
    result = model.calculate(
        exposures,
        equity=10_000,
        as_of=NOW,
        valuation=valuation,
        quotes=(_quote("EURUSD", 1.10, 1.10),),
    )
    assert result.margin_used == pytest.approx(100_000 * 1.10 / 20)


def test_projected_new_position_can_make_margin_insufficient_without_mutation() -> None:
    valuation = _engine(_spec("EURUSD", "EUR", "USD"))
    model = FixedLeveragePaperMargin("USD", {"EURUSD": 10})
    exposures = [MarginExposure("EURUSD", 1.0)]
    before = tuple(exposures)
    result = model.calculate(
        (*exposures, MarginExposure("EURUSD", 0.5)),
        equity=10_000,
        as_of=NOW,
        valuation=valuation,
        quotes=(_quote("EURUSD", 1.0, 1.0),),
    )
    assert result.margin_used == 15_000.0
    assert result.margin_available == -5_000.0
    assert result.sufficient is False
    assert tuple(exposures) == before


def test_margin_identity_is_stable_and_mapping_order_independent() -> None:
    first = FixedLeveragePaperMargin("USD", {"EURUSD": 20, "USDJPY": 25})
    second = FixedLeveragePaperMargin("USD", {"USDJPY": 25, "EURUSD": 20})
    assert first.descriptor.fingerprint == second.descriptor.fingerprint
    assert first.descriptor.leverage_by_symbol == (("EURUSD", 20.0), ("USDJPY", 25.0))


def test_missing_symbol_leverage_fails_without_mutating_exposures() -> None:
    valuation = _engine(_spec("EURUSD", "EUR", "USD"))
    model = FixedLeveragePaperMargin("USD", {"USDJPY": 20})
    exposures = (MarginExposure("EURUSD", 1.0),)
    with pytest.raises(ValueError, match="leverage"):
        model.calculate(
            exposures,
            equity=10_000,
            as_of=NOW,
            valuation=valuation,
            quotes=(_quote("EURUSD", 1.0, 1.0),),
        )
    assert exposures == (MarginExposure("EURUSD", 1.0),)
