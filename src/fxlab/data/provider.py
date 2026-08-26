"""Canonical, point-in-time-safe market-data provider contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from .schema import OHLCV, timeframe_to_timedelta

_INSTRUMENT_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "private_key",
}


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid timezone-aware datetime") from exc


def _identifier(value: object, field_name: str, pattern: re.Pattern[str] = _ID_RE) -> str:
    if not isinstance(value, str) or not (result := value.strip()) or not pattern.fullmatch(result):
        raise ValueError(f"{field_name} is malformed")
    normalized = result.lower().replace("-", "_")
    if any(item in normalized for item in _SENSITIVE_KEYS):
        raise ValueError(f"{field_name} may not contain sensitive material")
    return result


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not (result := value.strip()):
        raise ValueError(f"{field_name} must be non-empty when supplied")
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _safe_mapping(value: Mapping[str, object] | None) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("context must be a string-keyed mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        normalized = key.strip().lower().replace("-", "_")
        if normalized in _SENSITIVE_KEYS:
            raise ValueError(f"sensitive context key is not permitted: {key!r}")
        if isinstance(item, str) and re.search(
            r"(?i)(password|secret|token|api[_-]?key|authorization|credential)", item
        ):
            raise ValueError("sensitive context values are not permitted")
        if item is None or isinstance(item, (bool, str, int)):
            result[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            result[key] = item
        else:
            raise ValueError("context values must be immutable JSON primitives")
    return MappingProxyType(result)


class ProviderCapability(StrEnum):
    HISTORICAL_BARS = "historical_bars"
    REPLAY_EVENTS = "replay_events"
    LATEST_TICK = "latest_tick"
    POINT_IN_TIME = "point_in_time"
    DETERMINISTIC_REPLAY = "deterministic_replay"


class ProvenanceQuality(StrEnum):
    VERIFIED = "verified"
    SYNTHETIC = "synthetic"
    LEGACY_UNVERIFIED = "legacy_unverified"


class ProviderFailureCategory(StrEnum):
    TRANSIENT = "transient"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    NO_DATA = "no_data"
    STALE_DATA = "stale_data"
    UNSUPPORTED = "unsupported"
    INVALID_DATA = "invalid_data"
    INCOMPATIBLE_SCHEMA = "incompatible_schema"
    CONFIGURATION = "configuration"
    INTERNAL = "internal"


@dataclass(frozen=True, order=True)
class CanonicalInstrument:
    symbol: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str):
            raise ValueError("symbol must be a string")
        symbol = self.symbol.strip().upper()
        if not symbol or not _INSTRUMENT_RE.fullmatch(symbol):
            raise ValueError("canonical symbol is malformed")
        object.__setattr__(self, "symbol", symbol)

    def __str__(self) -> str:
        return self.symbol


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    implementation_version: str
    capabilities: frozenset[ProviderCapability]
    supported_symbols: frozenset[CanonicalInstrument] | None = None
    supported_timeframes: frozenset[str] | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    deterministic: bool = False
    normalization_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, "provider_id"))
        object.__setattr__(
            self,
            "implementation_version",
            _identifier(self.implementation_version, "implementation_version"),
        )
        object.__setattr__(
            self,
            "normalization_version",
            _identifier(self.normalization_version, "normalization_version"),
        )
        capabilities = frozenset(self.capabilities)
        if not capabilities or any(
            not isinstance(item, ProviderCapability) for item in capabilities
        ):
            raise ValueError("capabilities must contain ProviderCapability values")
        object.__setattr__(self, "capabilities", capabilities)
        if self.supported_symbols is not None:
            symbols = frozenset(self.supported_symbols)
            if any(not isinstance(item, CanonicalInstrument) for item in symbols):
                raise ValueError("supported_symbols must contain canonical instruments")
            object.__setattr__(self, "supported_symbols", symbols)
        if self.supported_timeframes is not None:
            frames = frozenset(_identifier(item, "timeframe") for item in self.supported_timeframes)
            object.__setattr__(self, "supported_timeframes", frames)
        start = _utc(self.coverage_start, "coverage_start") if self.coverage_start else None
        end = _utc(self.coverage_end, "coverage_end") if self.coverage_end else None
        if start and end and start >= end:
            raise ValueError("coverage_start must precede coverage_end")
        object.__setattr__(self, "coverage_start", start)
        object.__setattr__(self, "coverage_end", end)


@dataclass(frozen=True)
class ProviderRoute:
    primary_provider_id: str
    required_capability: ProviderCapability
    fallback_provider_ids: tuple[str, ...] = ()
    mapping_identity: str = "canonical-v1"
    freshness_policy_identity: str = "explicit-query-v1"
    fallback_policy_identity: str = "none"
    normalization_version: str = "1"

    def __post_init__(self) -> None:
        primary = _identifier(self.primary_provider_id, "primary_provider_id")
        fallbacks = tuple(
            _identifier(item, "fallback_provider_id") for item in self.fallback_provider_ids
        )
        if primary in fallbacks or len(set(fallbacks)) != len(fallbacks):
            raise ValueError("provider route IDs must be unique")
        if not isinstance(self.required_capability, ProviderCapability):
            raise ValueError("required_capability is invalid")
        object.__setattr__(self, "primary_provider_id", primary)
        object.__setattr__(self, "fallback_provider_ids", fallbacks)
        for name in (
            "mapping_identity",
            "freshness_policy_identity",
            "fallback_policy_identity",
            "normalization_version",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))

    @property
    def identity(self) -> str:
        return _sha(
            {
                "primary": self.primary_provider_id,
                "fallbacks": self.fallback_provider_ids,
                "capability": self.required_capability.value,
                "mapping": self.mapping_identity,
                "freshness": self.freshness_policy_identity,
                "fallback_policy": self.fallback_policy_identity,
                "normalization": self.normalization_version,
            }
        )


@dataclass(frozen=True)
class BarQuery:
    instrument: CanonicalInstrument
    timeframe: str
    start: datetime
    end: datetime
    as_of: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, CanonicalInstrument):
            raise ValueError("instrument must be canonical")
        timeframe_to_timedelta(self.timeframe)
        start, end, as_of = (
            _utc(self.start, "start"),
            _utc(self.end, "end"),
            _utc(self.as_of, "as_of"),
        )
        if start >= end:
            raise ValueError("bar query uses a half-open range with start < end")
        if start > as_of:
            raise ValueError("bar query cannot start after as_of")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "as_of", as_of)

    @property
    def fingerprint(self) -> str:
        return _sha(
            {
                "format": 1,
                "symbol": self.instrument.symbol,
                "timeframe": self.timeframe,
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "as_of": self.as_of.isoformat(),
            }
        )


@dataclass(frozen=True)
class TickQuery:
    instrument: CanonicalInstrument
    as_of: datetime
    max_age: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, CanonicalInstrument):
            raise ValueError("instrument must be canonical")
        as_of = _utc(self.as_of, "as_of")
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise ValueError("max_age must be an explicit positive timedelta")
        object.__setattr__(self, "as_of", as_of)

    @property
    def fingerprint(self) -> str:
        return _sha(
            {
                "format": 1,
                "symbol": self.instrument.symbol,
                "as_of": self.as_of.isoformat(),
                "max_age_seconds": self.max_age.total_seconds(),
            }
        )


@dataclass(frozen=True)
class DataProvenance:
    provider_id: str
    provider_version: str
    normalization_version: str
    canonical_symbol: str
    provider_symbol: str
    timeframe: str | None
    query_start: datetime | None
    query_end: datetime | None
    query_as_of: datetime
    retrieved_at: datetime
    actual_first_observation: datetime | None
    actual_last_observation: datetime | None
    row_count: int
    content_hash: str
    query_fingerprint: str
    dataset_id: str
    revision: str | None = None
    source_timezone: str = "UTC"
    volume_semantics: str = "provider_reported"
    provenance_quality: ProvenanceQuality = ProvenanceQuality.VERIFIED
    sanitized_source_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("provider_id", "provider_version", "normalization_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        canonical = CanonicalInstrument(self.canonical_symbol).symbol
        object.__setattr__(self, "canonical_symbol", canonical)
        provider_symbol = _optional_text(self.provider_symbol, "provider_symbol")
        if provider_symbol is None:
            raise ValueError("provider_symbol must be non-empty")
        object.__setattr__(self, "provider_symbol", provider_symbol)
        if self.timeframe is not None:
            timeframe_to_timedelta(self.timeframe)
        for name in (
            "query_start",
            "query_end",
            "actual_first_observation",
            "actual_last_observation",
        ):
            value = getattr(self, name)
            object.__setattr__(self, name, _utc(value, name) if value else None)
        object.__setattr__(self, "query_as_of", _utc(self.query_as_of, "query_as_of"))
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at, "retrieved_at"))
        if (
            isinstance(self.row_count, bool)
            or not isinstance(self.row_count, int)
            or self.row_count < 0
        ):
            raise ValueError("row_count must be a non-negative integer")
        for name in ("content_hash", "query_fingerprint", "dataset_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a SHA-256 hex digest")
        object.__setattr__(self, "revision", _optional_text(self.revision, "revision"))
        source_timezone = _optional_text(self.source_timezone, "source_timezone")
        if source_timezone is None:
            raise ValueError("source_timezone must be non-empty")
        object.__setattr__(self, "source_timezone", source_timezone)
        object.__setattr__(
            self, "volume_semantics", _identifier(self.volume_semantics, "volume_semantics")
        )
        if not isinstance(self.provenance_quality, ProvenanceQuality):
            raise ValueError("provenance_quality is invalid")
        reference = _optional_text(self.sanitized_source_reference, "sanitized_source_reference")
        if reference and (
            "?" in reference
            or "@" in reference
            or re.search(r"(?i)(token|secret|password|api[_-]?key|authorization)", reference)
        ):
            raise ValueError("source reference may contain sensitive data")
        object.__setattr__(self, "sanitized_source_reference", reference)


def bar_content_hash(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256(b"fxlab-canonical-bars-v1\0")
    for timestamp, values in zip(frame.index, frame[OHLCV].to_numpy(dtype="float64"), strict=True):
        digest.update(int(pd.Timestamp(timestamp).value).to_bytes(8, "big", signed=True))
        digest.update(np.asarray(values, dtype=">f8").tobytes())
    return digest.hexdigest()


def dataset_identity(
    provider_id: str, provider_version: str, query_fingerprint: str, content_hash: str
) -> str:
    return _sha(
        {
            "format": 1,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "query_fingerprint": query_fingerprint,
            "content_hash": content_hash,
        }
    )


@dataclass(frozen=True)
class BarDataset:
    query: BarQuery
    provenance: DataProvenance
    _frame: pd.DataFrame = field(repr=False)

    def __init__(self, query: BarQuery, frame: pd.DataFrame, provenance: DataProvenance):
        if not isinstance(query, BarQuery) or not isinstance(provenance, DataProvenance):
            raise ValueError("query and provenance must use provider contracts")
        if not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError("bars must be a DataFrame with a DatetimeIndex")
        snapshot = frame.copy(deep=True)
        if snapshot.index.tz is None:
            raise ValueError("bar index must be timezone-aware")
        snapshot.index = snapshot.index.tz_convert("UTC")
        snapshot.index.name = "ts_open"
        snapshot.attrs = {
            "symbol": snapshot.attrs.get("symbol"),
            "timeframe": snapshot.attrs.get("timeframe"),
        }
        if list(snapshot.columns) != OHLCV:
            raise ValueError(f"bar columns must be exactly {OHLCV}")
        if any(dtype != np.dtype("float64") for dtype in snapshot.dtypes):
            raise ValueError("bar columns must all be float64")
        if not snapshot.index.is_monotonic_increasing or not snapshot.index.is_unique:
            raise ValueError("bar timestamps must be strictly increasing and unique")
        values = snapshot.to_numpy(dtype="float64")
        if not np.isfinite(values).all():
            raise ValueError("bar values must be finite")
        if len(snapshot) and (
            (snapshot[["open", "high", "low", "close"]] <= 0).any().any()
            or (snapshot["volume"] < 0).any()
            or (snapshot["high"] < snapshot[["open", "close", "low"]].max(axis=1)).any()
            or (snapshot["low"] > snapshot[["open", "close", "high"]].min(axis=1)).any()
        ):
            raise ValueError("bar OHLCV values violate canonical integrity")
        if snapshot.attrs.get("symbol") != query.instrument.symbol:
            raise ValueError("bar symbol does not match query")
        if snapshot.attrs.get("timeframe") != query.timeframe:
            raise ValueError("bar timeframe does not match query")
        if len(snapshot):
            opens = snapshot.index
            if opens[0].to_pydatetime() < query.start or opens[-1].to_pydatetime() >= query.end:
                raise ValueError("bar rows are outside the half-open query range")
            closes = opens + timeframe_to_timedelta(query.timeframe)
            if (closes > pd.Timestamp(query.as_of)).any():
                raise ValueError("bar dataset exposes a bar unavailable at as_of")
        content_hash = bar_content_hash(snapshot)
        if (
            provenance.canonical_symbol != query.instrument.symbol
            or provenance.timeframe != query.timeframe
            or provenance.query_start != query.start
            or provenance.query_end != query.end
            or provenance.query_as_of != query.as_of
            or provenance.query_fingerprint != query.fingerprint
            or provenance.content_hash != content_hash
            or provenance.row_count != len(snapshot)
        ):
            raise ValueError("provenance does not describe the supplied bars")
        expected_first = snapshot.index[0].to_pydatetime() if len(snapshot) else None
        expected_last = snapshot.index[-1].to_pydatetime() if len(snapshot) else None
        if (
            provenance.actual_first_observation != expected_first
            or provenance.actual_last_observation != expected_last
        ):
            raise ValueError("provenance observation bounds do not match supplied bars")
        expected_id = dataset_identity(
            provenance.provider_id,
            provenance.provider_version,
            provenance.query_fingerprint,
            content_hash,
        )
        if provenance.dataset_id != expected_id:
            raise ValueError("dataset_id does not match provider/query/content identity")
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "_frame", snapshot)

    @property
    def frame(self) -> pd.DataFrame:
        return self._frame.copy(deep=True)


@dataclass(frozen=True)
class TickSnapshot:
    query: TickQuery
    instrument: CanonicalInstrument
    timestamp: datetime
    bid: float
    ask: float
    mid: float
    provenance: DataProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.query, TickQuery) or self.instrument != self.query.instrument:
            raise ValueError("tick instrument must match its query")
        timestamp = _utc(self.timestamp, "timestamp")
        prices = []
        for name in ("bid", "ask", "mid"):
            try:
                value = float(getattr(self, name))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{name} must be finite and positive") from exc
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
            prices.append(value)
        if self.ask < self.bid or not self.bid <= self.mid <= self.ask:
            raise ValueError("tick prices are incoherent")
        if timestamp > self.query.as_of:
            raise ValueError(ProviderFailureCategory.INVALID_DATA.value)
        if self.query.as_of - timestamp > self.query.max_age:
            raise ValueError(ProviderFailureCategory.STALE_DATA.value)
        if not isinstance(self.provenance, DataProvenance):
            raise ValueError("tick provenance is required")
        if self.provenance.canonical_symbol != self.instrument.symbol:
            raise ValueError("tick provenance symbol mismatch")
        object.__setattr__(self, "timestamp", timestamp)


@dataclass(frozen=True)
class ProviderFailure:
    category: ProviderFailureCategory
    reason: str
    provider_id: str
    retryable: bool = False
    context: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.category, ProviderFailureCategory):
            raise ValueError("failure category is invalid")
        object.__setattr__(self, "reason", _identifier(self.reason, "reason"))
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, "provider_id"))
        object.__setattr__(self, "context", _safe_mapping(self.context))


@runtime_checkable
class HistoricalBarsProvider(Protocol):
    descriptor: ProviderDescriptor

    def fetch_bars(self, query: BarQuery) -> BarDataset | ProviderFailure: ...


@runtime_checkable
class LatestTickProvider(Protocol):
    descriptor: ProviderDescriptor

    def latest_tick(self, query: TickQuery) -> TickSnapshot | ProviderFailure: ...


@runtime_checkable
class ReplayEventProvider(Protocol):
    descriptor: ProviderDescriptor

    def next_event(self, query: TickQuery) -> TickSnapshot | ProviderFailure | None: ...
