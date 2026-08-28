"""Deterministic point-in-time FX valuation contracts (Phase 18)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Protocol, runtime_checkable

_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{6,12}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SENSITIVE = re.compile(r"(?i)(password|secret|token|api[_-]?key|authorization|credential)")
VALUATION_POLICY_VERSION = "fx-point-in-time-v1"
CATALOG_FORMAT_VERSION = 1


def approved_fx_instrument_catalog() -> FxInstrumentCatalog:
    """Return the explicit Phase 18 FX-only application instrument catalog."""
    return FxInstrumentCatalog(
        (
            InstrumentSpec("AUDUSD", "fx", "AUD", "USD", 0.0001, 100_000, "1"),
            InstrumentSpec("EURUSD", "fx", "EUR", "USD", 0.0001, 100_000, "1"),
            InstrumentSpec("GBPUSD", "fx", "GBP", "USD", 0.0001, 100_000, "1"),
            InstrumentSpec("NZDUSD", "fx", "NZD", "USD", 0.0001, 100_000, "1"),
            InstrumentSpec("USDJPY", "fx", "USD", "JPY", 0.01, 100_000, "1"),
        )
    )


class ValuationFailure(RuntimeError):
    """Stable fail-closed valuation error without provider-specific context."""

    def __init__(self, reason: str) -> None:
        if not _safe_id(reason):
            raise ValueError("valuation failure reason must be a safe identifier")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    canonical_symbol: str
    instrument_class: str
    base_currency: str
    quote_currency: str
    pip_size: float
    contract_units_per_lot: float
    specification_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_symbol, str) or not _SYMBOL.fullmatch(
            self.canonical_symbol
        ):
            raise ValueError("canonical_symbol must be an explicit uppercase identifier")
        if self.instrument_class != "fx":
            raise ValueError("instrument_class must be fx")
        _require_currency(self.base_currency, "base_currency")
        _require_currency(self.quote_currency, "quote_currency")
        if self.base_currency == self.quote_currency:
            raise ValueError("base_currency and quote_currency must differ")
        if not _positive_finite(self.pip_size):
            raise ValueError("pip_size must be finite and positive")
        if not _positive_finite(self.contract_units_per_lot):
            raise ValueError("contract_units_per_lot must be finite and positive")
        if not _safe_id(self.specification_version):
            raise ValueError("specification_version must be a safe identifier")

    @property
    def identity(self) -> str:
        return _sha256(
            {
                "symbol": self.canonical_symbol,
                "class": self.instrument_class,
                "base": self.base_currency,
                "quote": self.quote_currency,
                "pip_size": _number(self.pip_size),
                "contract_units_per_lot": _number(self.contract_units_per_lot),
                "specification_version": self.specification_version,
            }
        )


@dataclass(frozen=True, slots=True)
class ConversionQuote:
    canonical_instrument: str
    bid: float
    ask: float
    observation_time: datetime
    source_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_instrument, str) or not _SYMBOL.fullmatch(
            self.canonical_instrument
        ):
            raise ValueError("canonical_instrument must be an explicit uppercase identifier")
        if not _positive_finite(self.bid) or not _positive_finite(self.ask):
            raise ValueError("conversion prices must be finite and positive")
        if float(self.ask) < float(self.bid):
            raise ValueError("conversion ask cannot be below bid")
        object.__setattr__(
            self,
            "observation_time",
            _aware_utc(self.observation_time, "observation_time"),
        )
        if not _safe_id(self.source_identity):
            raise ValueError("source_identity must be a safe identifier")


@dataclass(frozen=True, slots=True)
class PipValuation:
    canonical_symbol: str
    account_currency: str
    quote_currency_pip_amount_per_lot: float
    pip_value_per_lot: float
    positive_conversion_rate: float
    negative_conversion_rate: float
    as_of: datetime
    observation_time: datetime | None
    route_identity: str
    source_identity: str
    policy_version: str
    instrument_specification_identity: str
    valuation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_symbol, str) or not _SYMBOL.fullmatch(
            self.canonical_symbol
        ):
            raise ValueError("canonical_symbol is invalid")
        _require_currency(self.account_currency, "account_currency")
        for name in (
            "quote_currency_pip_amount_per_lot",
            "pip_value_per_lot",
            "positive_conversion_rate",
            "negative_conversion_rate",
        ):
            if not _positive_finite(getattr(self, name)):
                raise ValueError(f"{name} must be finite and positive")
        object.__setattr__(self, "as_of", _aware_utc(self.as_of, "as_of"))
        if self.observation_time is not None:
            object.__setattr__(
                self,
                "observation_time",
                _aware_utc(self.observation_time, "observation_time"),
            )
            if self.observation_time > self.as_of:
                raise ValueError("observation_time cannot be later than as_of")
        for name in (
            "route_identity",
            "source_identity",
            "policy_version",
            "instrument_specification_identity",
            "valuation_id",
        ):
            if not _safe_id(getattr(self, name)):
                raise ValueError(f"{name} must be a safe identifier")
        expected_id = _valuation_identity(
            self.canonical_symbol,
            self.account_currency,
            self.quote_currency_pip_amount_per_lot,
            self.pip_value_per_lot,
            self.positive_conversion_rate,
            self.negative_conversion_rate,
            self.as_of,
            self.observation_time,
            self.route_identity,
            self.source_identity,
            self.policy_version,
            self.instrument_specification_identity,
        )
        if self.valuation_id != expected_id:
            raise ValueError("valuation_id does not match valuation evidence")

    def convert_signed(self, quote_currency_amount: float) -> float:
        if not _finite_number(quote_currency_amount):
            raise ValuationFailure("invalid_quote_currency_amount")
        value = float(quote_currency_amount)
        rate = self.positive_conversion_rate if value >= 0 else self.negative_conversion_rate
        converted = value * rate
        if not math.isfinite(converted):
            raise ValuationFailure("invalid_converted_amount")
        return converted


@dataclass(frozen=True, slots=True)
class FxInstrumentCatalog:
    specifications: tuple[InstrumentSpec, ...]
    _by_symbol: MappingProxyType[str, InstrumentSpec] = field(init=False, repr=False)
    fingerprint: str = field(init=False)

    def __init__(self, specifications: object) -> None:
        try:
            items = tuple(specifications)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("specifications must be an iterable") from exc
        if not items or any(not isinstance(item, InstrumentSpec) for item in items):
            raise ValueError("catalog requires InstrumentSpec values")
        ordered = tuple(sorted(items, key=lambda item: item.canonical_symbol))
        if len({item.canonical_symbol for item in ordered}) != len(ordered):
            raise ValueError("duplicate instrument specification")
        by_symbol = MappingProxyType({item.canonical_symbol: item for item in ordered})
        document = {
            "format": CATALOG_FORMAT_VERSION,
            "specifications": [
                {"symbol": item.canonical_symbol, "identity": item.identity}
                for item in ordered
            ],
        }
        object.__setattr__(self, "specifications", ordered)
        object.__setattr__(self, "_by_symbol", by_symbol)
        object.__setattr__(self, "fingerprint", _sha256(document))

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._by_symbol)

    def specification(self, symbol: str) -> InstrumentSpec:
        if not isinstance(symbol, str):
            raise ValuationFailure("instrument_unsupported")
        try:
            return self._by_symbol[symbol]
        except KeyError:
            raise ValuationFailure("instrument_unsupported") from None


@dataclass(frozen=True, slots=True)
class FxValuationEngine:
    catalog: FxInstrumentCatalog
    max_age: timedelta = timedelta(minutes=5)
    policy_version: str = VALUATION_POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, FxInstrumentCatalog):
            raise ValueError("catalog must be an FxInstrumentCatalog")
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        if not _safe_id(self.policy_version):
            raise ValueError("policy_version must be a safe identifier")

    def pip_valuation(
        self,
        symbol: str,
        account_currency: str,
        as_of: datetime,
        quotes: object,
    ) -> PipValuation:
        spec = self.catalog.specification(symbol)
        account = _require_currency(account_currency, "account_currency")
        query_time = _aware_utc(as_of, "as_of")
        positive, negative, route, source, observed, _ = self._conversion_rates(
            spec.quote_currency, account, query_time, quotes
        )
        quote_pip = float(spec.contract_units_per_lot) * float(spec.pip_size)
        pip_value = quote_pip * negative
        valuation_id = _valuation_identity(
            spec.canonical_symbol,
            account,
            quote_pip,
            pip_value,
            positive,
            negative,
            query_time,
            observed,
            route,
            source,
            self.policy_version,
            spec.identity,
        )
        return PipValuation(
            spec.canonical_symbol,
            account,
            quote_pip,
            pip_value,
            positive,
            negative,
            query_time,
            observed,
            route,
            source,
            self.policy_version,
            spec.identity,
            valuation_id,
        )

    def convert_base_notional(
        self,
        symbol: str,
        lots: float,
        account_currency: str,
        as_of: datetime,
        quotes: object,
    ) -> tuple[float, str, datetime | None]:
        spec = self.catalog.specification(symbol)
        if not _positive_finite(lots):
            raise ValuationFailure("invalid_position_size")
        account = _require_currency(account_currency, "account_currency")
        query_time = _aware_utc(as_of, "as_of")
        _, negative, route, _, observed, _ = self._conversion_rates(
            spec.base_currency, account, query_time, quotes
        )
        converted = float(lots) * float(spec.contract_units_per_lot) * negative
        if not math.isfinite(converted) or converted <= 0:
            raise ValuationFailure("invalid_base_notional")
        return converted, route, observed

    def _conversion_rates(
        self,
        source_currency: str,
        account_currency: str,
        as_of: datetime,
        quotes: object,
    ) -> tuple[float, float, str, str, datetime | None, dict[str, str] | None]:
        quote_map = self._quote_map(quotes)
        if source_currency == account_currency:
            return 1.0, 1.0, "quote-equals-account", "no-conversion", None, None
        direct = [
            item
            for item in self.catalog.specifications
            if item.base_currency == source_currency and item.quote_currency == account_currency
        ]
        inverse = [
            item
            for item in self.catalog.specifications
            if item.base_currency == account_currency and item.quote_currency == source_currency
        ]
        if len(direct) + len(inverse) != 1:
            raise ValuationFailure("conversion_route_unavailable")
        route_spec = direct[0] if direct else inverse[0]
        quote = quote_map.get(route_spec.canonical_symbol)
        if quote is None:
            raise ValuationFailure("conversion_quote_missing")
        if quote.observation_time > as_of:
            raise ValuationFailure("future_conversion_quote")
        if as_of - quote.observation_time > self.max_age:
            raise ValuationFailure("stale_conversion_quote")
        if direct:
            positive = float(quote.bid)
            negative = float(quote.ask)
            route = f"direct:{route_spec.canonical_symbol}"
        else:
            positive = 1.0 / float(quote.ask)
            negative = 1.0 / float(quote.bid)
            route = f"inverse:{route_spec.canonical_symbol}"
        return (
            positive,
            negative,
            route,
            quote.source_identity,
            quote.observation_time,
            {"bid": _number(quote.bid), "ask": _number(quote.ask)},
        )

    def _quote_map(self, quotes: object) -> dict[str, ConversionQuote]:
        try:
            items = tuple(quotes)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValuationFailure("conversion_quote_invalid") from exc
        result: dict[str, ConversionQuote] = {}
        for quote in items:
            if not isinstance(quote, ConversionQuote):
                raise ValuationFailure("conversion_quote_invalid")
            self.catalog.specification(quote.canonical_instrument)
            if quote.canonical_instrument in result:
                raise ValuationFailure("conversion_quote_invalid")
            result[quote.canonical_instrument] = quote
        return result


@runtime_checkable
class InstrumentValuationProvider(Protocol):
    def pip_valuation(
        self, symbol: str, account_currency: str, as_of: datetime
    ) -> PipValuation: ...


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    try:
        if value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)
    except (OverflowError, OSError):
        raise ValueError(f"{name} must be timezone-aware") from None


def _require_currency(value: object, name: str) -> str:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ValueError(f"{name} must be an uppercase three-letter currency code")
    return value


def _safe_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(_SAFE_ID.fullmatch(value))
        and not bool(_SENSITIVE.search(value))
    )


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _positive_finite(value: object) -> bool:
    return _finite_number(value) and float(value) > 0


def _number(value: float) -> str:
    return str(float(value))


def _valuation_identity(
    canonical_symbol: str,
    account_currency: str,
    quote_currency_pip_amount_per_lot: float,
    pip_value_per_lot: float,
    positive_conversion_rate: float,
    negative_conversion_rate: float,
    as_of: datetime,
    observation_time: datetime | None,
    route_identity: str,
    source_identity: str,
    policy_version: str,
    instrument_specification_identity: str,
) -> str:
    return _sha256(
        {
            "canonical_symbol": canonical_symbol,
            "instrument_specification_identity": instrument_specification_identity,
            "account_currency": account_currency,
            "quote_currency_pip_amount_per_lot": _number(
                quote_currency_pip_amount_per_lot
            ),
            "pip_value_per_lot": _number(pip_value_per_lot),
            "positive_conversion_rate": _number(positive_conversion_rate),
            "negative_conversion_rate": _number(negative_conversion_rate),
            "route_identity": route_identity,
            "source_identity": source_identity,
            "observation_time": (
                observation_time.isoformat() if observation_time else None
            ),
            "as_of": as_of.isoformat(),
            "policy_version": policy_version,
        }
    )


def _sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()
