"""Bounded Dukascopy historical BID-bar provider.

The provider deliberately owns no cache and performs no retry.  Its transport is
injectable so normal tests never use the network and callers receive only the
existing canonical provider contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .provider import (
    BarDataset,
    BarQuery,
    CanonicalInstrument,
    DataProvenance,
    ProvenanceQuality,
    ProviderCapability,
    ProviderDescriptor,
    ProviderFailure,
    ProviderFailureCategory,
    bar_content_hash,
    dataset_identity,
)
from .schema import OHLCV, timeframe_to_timedelta

_PROVIDER_ID = "dukascopy"
_IMPLEMENTATION_VERSION = "1"
_NORMALIZATION_VERSION = "dukascopy_bid_v1"
_MAPPING_FORMAT_VERSION = 1
_SOURCE_REFERENCE = "dukascopy:historical:bid"
_ENDPOINT = "https://freeserv.dukascopy.com/2.0/index.php"
_CALLBACK = "fxlab_callback"
_MAX_PROVIDER_PAGE_SIZE = 30_000
_REVISION_RE = re.compile(r"^[A-Za-z0-9._:\-/ ]{1,128}$")

DUKASCOPY_SYMBOLS: Mapping[str, str] = MappingProxyType(
    {
        "EURUSD": "EUR/USD",
        "GBPUSD": "GBP/USD",
        "USDJPY": "USD/JPY",
        "AUDUSD": "AUD/USD",
        "USDCAD": "USD/CAD",
        "USDCHF": "USD/CHF",
        "NZDUSD": "NZD/USD",
        "XAUUSD": "XAU/USD",
        "XAGUSD": "XAG/USD",
        "BRENT": "E_Brent",
        "WTI": "E_Light",
        "SPX500": "E_SandP-500",
        "NAS100": "E_NQ-100",
        "GER40": "E_DAAX",
    }
)

DUKASCOPY_TIMEFRAMES: Mapping[str, str] = MappingProxyType(
    {
        "M1": "1MIN",
        "M5": "5MIN",
        "M15": "15MIN",
        "M30": "30MIN",
        "H1": "1HOUR",
        "H4": "4HOUR",
        "D1": "1DAY",
    }
)


def symbol_mapping_fingerprint(mapping: Mapping[str, str]) -> str:
    """Return a deterministic identity for an explicit bijective mapping."""
    if not isinstance(mapping, Mapping) or not mapping:
        raise ValueError("symbol mapping must be a non-empty mapping")
    pairs: list[tuple[str, str]] = []
    native_seen: set[str] = set()
    canonical_seen: set[str] = set()
    for raw_canonical, raw_native in mapping.items():
        canonical = CanonicalInstrument(raw_canonical).symbol
        if not isinstance(raw_native, str) or not (native := raw_native.strip()):
            raise ValueError("provider-native symbols must be non-empty")
        if canonical in canonical_seen or native in native_seen:
            raise ValueError("Dukascopy symbol mapping must be bijective")
        canonical_seen.add(canonical)
        native_seen.add(native)
        pairs.append((canonical, native))
    document = {
        "format": _MAPPING_FORMAT_VERSION,
        "provider_id": _PROVIDER_ID,
        "symbols": sorted(pairs),
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


DUKASCOPY_MAPPING_FINGERPRINT = symbol_mapping_fingerprint(DUKASCOPY_SYMBOLS)


@dataclass(frozen=True)
class DukascopyConnectorSettings:
    timeout_seconds: float = 10.0
    page_size: int = 30_000
    max_response_bytes: int = 8 * 1024 * 1024
    max_pages: int = 512

    def __post_init__(self) -> None:
        try:
            timeout = float(self.timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("timeout_seconds must be finite and positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        for name in ("page_size", "max_response_bytes", "max_pages"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.page_size > _MAX_PROVIDER_PAGE_SIZE:
            raise ValueError("page_size exceeds the Dukascopy provider limit")
        object.__setattr__(self, "timeout_seconds", timeout)


@dataclass(frozen=True)
class DukascopyPage:
    rows: tuple[tuple[object, ...], ...]
    complete: bool
    revision: str | None = None

    def __post_init__(self) -> None:
        rows = tuple(tuple(row) for row in self.rows)
        if not isinstance(self.complete, bool):
            raise ValueError("page completion marker must be boolean")
        revision = _safe_revision(self.revision)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "revision", revision)


class DukascopyTransport(Protocol):
    def fetch_page(
        self,
        *,
        native_symbol: str,
        native_timeframe: str,
        cursor_ms: int,
        end_ms: int,
        page_size: int,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DukascopyPage: ...


class DukascopyTransportFailure(RuntimeError):
    """Sanitized transport failure safe to map into ProviderFailure."""

    def __init__(
        self,
        category: ProviderFailureCategory,
        reason: str,
        *,
        retryable: bool = False,
    ) -> None:
        if not isinstance(category, ProviderFailureCategory):
            raise ValueError("transport failure category is invalid")
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]+", reason):
            raise ValueError("transport failure reason is malformed")
        self.category = category
        self.reason = reason
        self.retryable = bool(retryable)
        super().__init__(reason)


@dataclass(frozen=True)
class DukascopyHttpTransport:
    """One-attempt HTTP page transport for Dukascopy's historical JSON feed."""

    opener: Callable[..., object] = field(default=urlopen, repr=False)

    def fetch_page(
        self,
        *,
        native_symbol: str,
        native_timeframe: str,
        cursor_ms: int,
        end_ms: int,
        page_size: int,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DukascopyPage:
        params = {
            "path": "chart/json3",
            "splits": "true",
            "stocks": "true",
            "time_direction": "N",
            "jsonp": _CALLBACK,
            "last_update": str(cursor_ms),
            "offer_side": "B",
            "instrument": native_symbol,
            "interval": native_timeframe,
            "limit": str(page_size),
        }
        request = Request(
            f"{_ENDPOINT}?{urlencode(params)}",
            headers={"User-Agent": "fxlab-market-data/1", "Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise _http_failure(status)
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise DukascopyTransportFailure(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                        "response_too_large",
                    )
                headers = getattr(response, "headers", {})
                revision = _header_value(headers, "ETag") or _header_value(headers, "Last-Modified")
        except DukascopyTransportFailure:
            raise
        except HTTPError as exc:
            raise _http_failure(exc.code) from None
        except (TimeoutError, URLError, OSError):
            raise DukascopyTransportFailure(
                ProviderFailureCategory.TRANSIENT,
                "network_unavailable",
                retryable=True,
            ) from None

        rows = _parse_jsonp(body)
        bounded: list[tuple[object, ...]] = []
        complete = False
        for raw in rows:
            if not isinstance(raw, list):
                raise DukascopyTransportFailure(
                    ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                    "malformed_page",
                )
            if not raw or isinstance(raw[0], bool) or not isinstance(raw[0], int):
                raise DukascopyTransportFailure(
                    ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                    "malformed_page",
                )
            if raw[0] >= end_ms:
                complete = True
                break
            bounded.append(tuple(raw))
        if not rows:
            complete = True
        return DukascopyPage(tuple(bounded), complete=complete, revision=revision)


@dataclass(frozen=True)
class DukascopyHistoricalBarsProvider:
    transport: DukascopyTransport = field(repr=False)
    settings: DukascopyConnectorSettings = field(default_factory=DukascopyConnectorSettings)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    descriptor: ProviderDescriptor = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.settings, DukascopyConnectorSettings):
            raise ValueError("settings must be DukascopyConnectorSettings")
        if not callable(getattr(self.transport, "fetch_page", None)):
            raise ValueError("transport must implement fetch_page")
        if not callable(self.clock):
            raise ValueError("clock must be callable")
        object.__setattr__(
            self,
            "descriptor",
            ProviderDescriptor(
                _PROVIDER_ID,
                _IMPLEMENTATION_VERSION,
                frozenset({ProviderCapability.HISTORICAL_BARS, ProviderCapability.POINT_IN_TIME}),
                supported_symbols=frozenset(
                    CanonicalInstrument(symbol) for symbol in DUKASCOPY_SYMBOLS
                ),
                supported_timeframes=frozenset(DUKASCOPY_TIMEFRAMES),
                deterministic=False,
                normalization_version=_NORMALIZATION_VERSION,
            ),
        )

    @property
    def mapping_fingerprint(self) -> str:
        return DUKASCOPY_MAPPING_FINGERPRINT

    def fetch_bars(self, query: BarQuery) -> BarDataset | ProviderFailure:
        if not isinstance(query, BarQuery):
            return _failure(ProviderFailureCategory.CONFIGURATION, "query_invalid")
        native_symbol = DUKASCOPY_SYMBOLS.get(query.instrument.symbol)
        if native_symbol is None:
            return _failure(ProviderFailureCategory.UNSUPPORTED, "symbol_unsupported")
        native_timeframe = DUKASCOPY_TIMEFRAMES.get(query.timeframe)
        if native_timeframe is None:
            return _failure(ProviderFailureCategory.UNSUPPORTED, "timeframe_unsupported")

        remote_end = min(query.end, query.as_of)
        cursor_ms = _epoch_ms(query.start)
        end_ms = _epoch_ms(remote_end)
        raw_rows: list[tuple[object, ...]] = []
        previous: tuple[object, ...] | None = None
        previous_timestamp: int | None = None
        revision: str | None = None
        complete = False

        for _page_number in range(self.settings.max_pages):
            try:
                page = self.transport.fetch_page(
                    native_symbol=native_symbol,
                    native_timeframe=native_timeframe,
                    cursor_ms=cursor_ms,
                    end_ms=end_ms,
                    page_size=self.settings.page_size,
                    timeout_seconds=self.settings.timeout_seconds,
                    max_response_bytes=self.settings.max_response_bytes,
                )
            except DukascopyTransportFailure as exc:
                return _failure(exc.category, exc.reason, retryable=exc.retryable)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return _failure(ProviderFailureCategory.INTERNAL, "transport_invariant_failed")
            if not isinstance(page, DukascopyPage):
                return _failure(ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "malformed_page")
            if revision is None:
                revision = page.revision
            elif page.revision is not None and page.revision != revision:
                return _failure(ProviderFailureCategory.INVALID_DATA, "revision_changed")
            if not page.rows and not page.complete:
                return _failure(
                    ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "pagination_not_advancing"
                )

            advanced = False
            for index, raw in enumerate(page.rows):
                validation = _validate_raw_row(raw, query)
                if isinstance(validation, ProviderFailure):
                    return validation
                timestamp_ms = int(raw[0])
                if previous_timestamp is not None and timestamp_ms == previous_timestamp:
                    if index == 0 and raw == previous:
                        continue
                    return _failure(ProviderFailureCategory.INVALID_DATA, "duplicate_timestamp")
                if previous_timestamp is not None and timestamp_ms < previous_timestamp:
                    return _failure(ProviderFailureCategory.INVALID_DATA, "timestamps_out_of_order")
                raw_rows.append(raw)
                previous = raw
                previous_timestamp = timestamp_ms
                cursor_ms = timestamp_ms
                advanced = True
            if page.complete:
                complete = True
                break
            if not advanced:
                return _failure(
                    ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "pagination_not_advancing"
                )
        if not complete:
            return _failure(ProviderFailureCategory.CONFIGURATION, "page_limit_exceeded")
        if not raw_rows:
            return _failure(ProviderFailureCategory.NO_DATA, "no_data")

        delta = timeframe_to_timedelta(query.timeframe)
        closed_rows = [raw for raw in raw_rows if _timestamp(raw[0]) + delta <= query.as_of]
        if not closed_rows:
            return _failure(ProviderFailureCategory.NO_DATA, "no_closed_bars")
        try:
            frame = _canonical_frame(closed_rows, query)
            retrieved_at = _aware_utc(self.clock(), "retrieved_at")
            content_hash = bar_content_hash(frame)
            provenance = DataProvenance(
                provider_id=self.descriptor.provider_id,
                provider_version=self.descriptor.implementation_version,
                normalization_version=self.descriptor.normalization_version,
                canonical_symbol=query.instrument.symbol,
                provider_symbol=native_symbol,
                timeframe=query.timeframe,
                query_start=query.start,
                query_end=query.end,
                query_as_of=query.as_of,
                retrieved_at=retrieved_at,
                actual_first_observation=frame.index[0].to_pydatetime(),
                actual_last_observation=frame.index[-1].to_pydatetime(),
                row_count=len(frame),
                content_hash=content_hash,
                query_fingerprint=query.fingerprint,
                dataset_id=dataset_identity(
                    self.descriptor.provider_id,
                    self.descriptor.implementation_version,
                    query.fingerprint,
                    content_hash,
                ),
                revision=revision,
                source_timezone="UTC",
                volume_semantics="provider_reported_units",
                provenance_quality=ProvenanceQuality.VERIFIED,
                sanitized_source_reference=_SOURCE_REFERENCE,
            )
            return BarDataset(query, frame, provenance)
        except (TypeError, ValueError, OverflowError):
            return _failure(ProviderFailureCategory.INVALID_DATA, "canonical_validation_failed")
        except Exception:
            return _failure(ProviderFailureCategory.INTERNAL, "provider_invariant_failed")


def _validate_raw_row(raw: tuple[object, ...], query: BarQuery) -> ProviderFailure | None:
    if len(raw) != 6:
        return _failure(ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "malformed_row")
    if isinstance(raw[0], bool) or not isinstance(raw[0], int):
        return _failure(ProviderFailureCategory.INVALID_DATA, "timestamp_invalid")
    try:
        timestamp = _timestamp(raw[0])
        values = tuple(float(value) for value in raw[1:])
    except (TypeError, ValueError, OverflowError, OSError):
        return _failure(ProviderFailureCategory.INVALID_DATA, "row_values_invalid")
    if timestamp < query.start or timestamp >= query.end or timestamp > query.as_of:
        return _failure(ProviderFailureCategory.INVALID_DATA, "row_outside_query")
    if not all(math.isfinite(value) for value in values):
        return _failure(ProviderFailureCategory.INVALID_DATA, "row_values_invalid")
    open_, high, low, close, volume = values
    if (
        min(open_, high, low, close) <= 0
        or volume < 0
        or high < max(open_, close, low)
        or low > min(open_, close, high)
    ):
        return _failure(ProviderFailureCategory.INVALID_DATA, "ohlcv_invalid")
    return None


def _canonical_frame(rows: list[tuple[object, ...]], query: BarQuery) -> pd.DataFrame:
    index = pd.DatetimeIndex([_timestamp(row[0]) for row in rows], name="ts_open")
    frame = pd.DataFrame(
        [[float(value) for value in row[1:]] for row in rows],
        index=index,
        columns=OHLCV,
        dtype="float64",
    )
    frame.attrs = {"symbol": query.instrument.symbol, "timeframe": query.timeframe}
    return frame


def _parse_jsonp(body: bytes) -> list[object]:
    try:
        text = body.decode("utf-8")
        prefix, suffix = f"{_CALLBACK}(", ");"
        if not text.startswith(prefix) or not text.endswith(suffix):
            raise ValueError
        parsed = json.loads(text[len(prefix) : -len(suffix)])
        if not isinstance(parsed, list):
            raise ValueError
        return parsed
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "malformed_response"
        ) from None


def _http_failure(status: int) -> DukascopyTransportFailure:
    if status in (401, 403):
        return DukascopyTransportFailure(
            ProviderFailureCategory.AUTHENTICATION, "authentication_failed"
        )
    if status == 429:
        return DukascopyTransportFailure(
            ProviderFailureCategory.RATE_LIMIT, "rate_limited", retryable=True
        )
    if 500 <= status <= 599:
        return DukascopyTransportFailure(
            ProviderFailureCategory.TRANSIENT, "provider_unavailable", retryable=True
        )
    return DukascopyTransportFailure(ProviderFailureCategory.INTERNAL, "unexpected_http_status")


def _header_value(headers: object, key: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    try:
        return _safe_revision(getter(key))
    except ValueError:
        return None


def _safe_revision(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not (revision := value.strip()):
        raise ValueError("revision must be non-empty text")
    normalized = revision.lower().replace("-", "_")
    if not _REVISION_RE.fullmatch(revision) or any(
        item in normalized
        for item in ("password", "secret", "token", "api_key", "authorization", "credential")
    ):
        raise ValueError("revision is unsafe")
    return revision


def _failure(
    category: ProviderFailureCategory, reason: str, *, retryable: bool = False
) -> ProviderFailure:
    return ProviderFailure(category, reason, _PROVIDER_ID, retryable=retryable)


def _timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timestamp must be integer milliseconds")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _epoch_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
