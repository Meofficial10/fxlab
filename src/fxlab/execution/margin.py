"""Explicit deterministic paper-margin policies (Phase 18)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from .valuation import ConversionQuote, FxValuationEngine

_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{6,12}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MARGIN_POLICY_VERSION = "paper-margin-v1"


@dataclass(frozen=True, slots=True)
class MarginExposure:
    canonical_symbol: str
    size_lots: float
    side: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_symbol, str) or not _SYMBOL.fullmatch(
            self.canonical_symbol
        ):
            raise ValueError("canonical_symbol must be an explicit uppercase identifier")
        if not _positive_finite(self.size_lots):
            raise ValueError("size_lots must be finite and positive")
        if isinstance(self.side, bool) or self.side not in (1, -1):
            raise ValueError("side must be exactly +1 or -1")


@dataclass(frozen=True, slots=True)
class MarginDescriptor:
    model_id: str
    modeled: bool
    quality: str
    account_currency: str
    policy_version: str
    leverage_by_symbol: tuple[tuple[str, float], ...]
    fingerprint: str

    def __post_init__(self) -> None:
        for name in ("model_id", "quality", "policy_version", "fingerprint"):
            if not isinstance(getattr(self, name), str) or not _SAFE_ID.fullmatch(
                getattr(self, name)
            ):
                raise ValueError(f"{name} must be a safe identifier")
        _require_currency(self.account_currency)
        if not isinstance(self.modeled, bool):
            raise ValueError("modeled must be a bool")
        if not isinstance(self.leverage_by_symbol, tuple):
            raise ValueError("leverage_by_symbol must be an immutable tuple")


@dataclass(frozen=True, slots=True)
class MarginResult:
    account_currency: str
    equity: float
    margin_used: float
    margin_available: float
    modeled: bool
    sufficient: bool
    model_id: str
    result_id: str

    def __post_init__(self) -> None:
        _require_currency(self.account_currency)
        for name in ("equity", "margin_used", "margin_available"):
            if not _finite_number(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.margin_used < 0:
            raise ValueError("margin_used cannot be negative")
        if not isinstance(self.modeled, bool) or not isinstance(self.sufficient, bool):
            raise ValueError("margin result flags must be bools")
        if not _SAFE_ID.fullmatch(self.model_id) or not _SAFE_ID.fullmatch(self.result_id):
            raise ValueError("margin identities must be safe")


@runtime_checkable
class PaperMarginModel(Protocol):
    @property
    def descriptor(self) -> MarginDescriptor: ...

    def calculate(
        self,
        exposures: object,
        *,
        equity: float,
        as_of: datetime,
        valuation: FxValuationEngine | None,
        quotes: object,
    ) -> MarginResult: ...


@dataclass(frozen=True, slots=True)
class UnmodeledPaperMargin:
    account_currency: str
    descriptor: MarginDescriptor = field(init=False)

    def __post_init__(self) -> None:
        account = _require_currency(self.account_currency)
        document = {
            "model_id": "unmodeled-paper-margin-v1",
            "modeled": False,
            "quality": "unmodeled-paper-margin",
            "account_currency": account,
            "policy_version": MARGIN_POLICY_VERSION,
            "leverage_by_symbol": [],
        }
        object.__setattr__(
            self,
            "descriptor",
            MarginDescriptor(
                document["model_id"],
                False,
                document["quality"],
                account,
                MARGIN_POLICY_VERSION,
                (),
                _sha256(document),
            ),
        )

    def calculate(
        self,
        exposures: object,
        *,
        equity: float,
        as_of: datetime,
        valuation: FxValuationEngine | None,
        quotes: object,
    ) -> MarginResult:
        items = _exposures(exposures)
        equity_value = _finite_equity(equity)
        observed = _aware_utc(as_of)
        document = {
            "descriptor": self.descriptor.fingerprint,
            "equity": _number(equity_value),
            "as_of": observed.isoformat(),
            "exposures": _exposure_document(items),
        }
        return MarginResult(
            self.account_currency,
            equity_value,
            0.0,
            equity_value,
            False,
            equity_value >= 0,
            self.descriptor.model_id,
            _sha256(document),
        )


@dataclass(frozen=True, slots=True, init=False)
class FixedLeveragePaperMargin:
    account_currency: str
    _leverage: MappingProxyType[str, float] = field(repr=False)
    descriptor: MarginDescriptor

    def __init__(self, account_currency: str, leverage_by_symbol: object) -> None:
        account = _require_currency(account_currency)
        if not isinstance(leverage_by_symbol, dict) or not leverage_by_symbol:
            raise ValueError("leverage_by_symbol must be a non-empty mapping")
        parsed: dict[str, float] = {}
        for symbol, leverage in leverage_by_symbol.items():
            if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
                raise ValueError("leverage symbol must be an explicit uppercase identifier")
            if not _positive_finite(leverage):
                raise ValueError("leverage must be finite and positive")
            parsed[symbol] = float(leverage)
        ordered = tuple(sorted(parsed.items()))
        document = {
            "model_id": "fixed-leverage-paper-margin-v1",
            "modeled": True,
            "quality": "deterministic-fixed-leverage",
            "account_currency": account,
            "policy_version": MARGIN_POLICY_VERSION,
            "leverage_by_symbol": [
                [symbol, _number(leverage)] for symbol, leverage in ordered
            ],
        }
        object.__setattr__(self, "account_currency", account)
        object.__setattr__(self, "_leverage", MappingProxyType(dict(ordered)))
        object.__setattr__(
            self,
            "descriptor",
            MarginDescriptor(
                document["model_id"],
                True,
                document["quality"],
                account,
                MARGIN_POLICY_VERSION,
                ordered,
                _sha256(document),
            ),
        )

    def calculate(
        self,
        exposures: object,
        *,
        equity: float,
        as_of: datetime,
        valuation: FxValuationEngine | None,
        quotes: object,
    ) -> MarginResult:
        items = _exposures(exposures)
        equity_value = _finite_equity(equity)
        observed = _aware_utc(as_of)
        if not isinstance(valuation, FxValuationEngine):
            raise ValueError("valuation is required for fixed leverage margin")
        try:
            quote_items = tuple(quotes)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("quotes must be an iterable") from exc
        if any(not isinstance(item, ConversionQuote) for item in quote_items):
            raise ValueError("quotes must contain ConversionQuote values")
        used = Decimal(0)
        notionals: list[dict[str, str]] = []
        for item in items:
            leverage = self._leverage.get(item.canonical_symbol)
            if leverage is None:
                raise ValueError("leverage is unavailable for an exposure symbol")
            notional, route, _ = valuation.convert_base_notional(
                item.canonical_symbol,
                item.size_lots,
                self.account_currency,
                observed,
                quote_items,
            )
            margin = Decimal(str(notional)) / Decimal(str(leverage))
            used += margin
            notionals.append(
                {
                    "symbol": item.canonical_symbol,
                    "side": str(item.side),
                    "size_lots": _number(item.size_lots),
                    "notional": _number(notional),
                    "leverage": _number(leverage),
                    "route": route,
                }
            )
        margin_used = float(used)
        available = float(Decimal(str(equity_value)) - used)
        document = {
            "descriptor": self.descriptor.fingerprint,
            "equity": _number(equity_value),
            "as_of": observed.isoformat(),
            "exposures": sorted(
                notionals,
                key=lambda item: (item["symbol"], item["side"], item["size_lots"]),
            ),
            "margin_used": _number(margin_used),
        }
        return MarginResult(
            self.account_currency,
            equity_value,
            margin_used,
            available,
            True,
            used <= Decimal(str(equity_value)),
            self.descriptor.model_id,
            _sha256(document),
        )


def _exposures(value: object) -> tuple[MarginExposure, ...]:
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("exposures must be an iterable") from exc
    if any(not isinstance(item, MarginExposure) for item in items):
        raise ValueError("exposures must contain MarginExposure values")
    return items


def _exposure_document(items: tuple[MarginExposure, ...]) -> list[dict[str, str]]:
    return sorted(
        [
            {
                "symbol": item.canonical_symbol,
                "size_lots": _number(item.size_lots),
                "side": str(item.side),
            }
            for item in items
        ],
        key=lambda item: (item["symbol"], item["side"], item["size_lots"]),
    )


def _require_currency(value: object) -> str:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ValueError("account_currency must be an uppercase three-letter code")
    return value


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _positive_finite(value: object) -> bool:
    return _finite_number(value) and float(value) > 0


def _finite_equity(value: object) -> float:
    if not _finite_number(value):
        raise ValueError("equity must be finite")
    return float(value)


def _aware_utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(UTC)


def _number(value: float) -> str:
    return str(float(value))


def _sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()
