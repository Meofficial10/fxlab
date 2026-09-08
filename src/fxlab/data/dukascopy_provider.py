"""Bounded Dukascopy historical BID-bar provider.

The provider deliberately owns no cache.  Its BI5 transport applies only a
fixed, bounded retry policy to explicitly transient failures and is injectable
so normal tests never use the network and callers receive only the existing
canonical provider contracts.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import math
import re
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

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
_BI5_ENDPOINT = "https://datafeed.dukascopy.com/datafeed"
_BI5_RECORD = struct.Struct(">IIIff")
_BI5_MAX_DECOMPRESSED_HOUR_BYTES = 64 * 1024 * 1024
_BI5_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_BI5_MAX_TIMEOUT_SECONDS = 30.0
_BI5_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
_BI5_SOURCE_REFERENCE = "dukascopy:datafeed:bi5:hourly:bid"
_BI5_IMPLEMENTATION_VERSION = "2"
_BI5_NORMALIZATION_VERSION = "dukascopy_bi5_bid_d1_v1"
BI5_RESEARCH_START = datetime(2014, 1, 1, tzinfo=UTC)
BI5_RESEARCH_END = datetime(2024, 1, 1, tzinfo=UTC)
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

DUKASCOPY_BI5_PRICE_DIVISORS: Mapping[str, int] = MappingProxyType(
    {
        "AUDUSD": 100_000,
        "EURUSD": 100_000,
        "GBPUSD": 100_000,
        "NZDUSD": 100_000,
        "USDCAD": 100_000,
        "USDCHF": 100_000,
        "USDJPY": 1_000,
    }
)


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
class DukascopyBi5Tick:
    timestamp: datetime
    ask: float
    bid: float
    ask_volume: float
    bid_volume: float

    def __post_init__(self) -> None:
        timestamp = _aware_utc(self.timestamp, "timestamp")
        values = (self.ask, self.bid, self.ask_volume, self.bid_volume)
        if not all(
            isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values
        ):
            raise ValueError("tick values must be finite numbers")
        if self.ask <= 0 or self.bid <= 0 or self.ask < self.bid:
            raise ValueError("tick prices are invalid")
        if self.ask_volume < 0 or self.bid_volume < 0:
            raise ValueError("tick volumes are invalid")
        object.__setattr__(self, "timestamp", timestamp)
        for name in ("ask", "bid", "ask_volume", "bid_volume"):
            object.__setattr__(self, name, float(getattr(self, name)))


@dataclass(frozen=True)
class DukascopyBi5Hour:
    hour_start: datetime
    body: bytes
    revision: str | None = None
    is_absent: bool = False

    def __post_init__(self) -> None:
        hour_start = _hour_start(self.hour_start)
        if not isinstance(self.body, bytes):
            raise ValueError("bi5 body must be immutable bytes")
        if not isinstance(self.is_absent, bool):
            raise ValueError("absent marker must be boolean")
        if self.is_absent and self.body:
            raise ValueError("absent hour cannot contain a body")
        object.__setattr__(self, "hour_start", hour_start)
        object.__setattr__(self, "revision", _safe_revision(self.revision))

    @classmethod
    def absent(cls, hour_start: datetime) -> DukascopyBi5Hour:
        return cls(hour_start, b"", None, True)

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class DukascopyBi5Transport(Protocol):
    def fetch_hour(
        self,
        *,
        native_symbol: str,
        hour_start: datetime,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DukascopyBi5Hour: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _bi5_default_opener() -> Callable[..., object]:
    return build_opener(_NoRedirectHandler()).open


@dataclass(frozen=True)
class DukascopyBi5HttpTransport:
    """Bounded HTTP transport for one allow-listed hourly .bi5 partition."""

    opener: Callable[..., object] = field(default_factory=_bi5_default_opener, repr=False)
    sleeper: Callable[[float], None] = field(default=time.sleep, repr=False)

    def fetch_hour(
        self,
        *,
        native_symbol: str,
        hour_start: datetime,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DukascopyBi5Hour:
        for attempt in range(len(_BI5_RETRY_BACKOFF_SECONDS) + 1):
            try:
                return self._fetch_hour_once(
                    native_symbol=native_symbol,
                    hour_start=hour_start,
                    timeout_seconds=timeout_seconds,
                    max_response_bytes=max_response_bytes,
                )
            except DukascopyTransportFailure as exc:
                if not exc.retryable or attempt == len(_BI5_RETRY_BACKOFF_SECONDS):
                    raise
                self.sleeper(_BI5_RETRY_BACKOFF_SECONDS[attempt])
        raise AssertionError("unreachable")

    def _fetch_hour_once(
        self,
        *,
        native_symbol: str,
        hour_start: datetime,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DukascopyBi5Hour:
        url = dukascopy_bi5_url(native_symbol, hour_start)
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "fxlab-market-data/1",
            },
        )
        try:
            response = self.opener(request, timeout=timeout_seconds)
        except HTTPError as exc:
            if exc.code == 404:
                return DukascopyBi5Hour.absent(hour_start)
            raise _bi5_http_failure(exc.code) from None
        except (TimeoutError, URLError, OSError):
            raise DukascopyTransportFailure(
                ProviderFailureCategory.TRANSIENT,
                "network_unavailable",
                retryable=True,
            ) from None

        try:
            with response:
                status = int(getattr(response, "status", 200))
                if status == 404:
                    return DukascopyBi5Hour.absent(hour_start)
                if status != 200:
                    raise _bi5_http_failure(status)
                returned_url = _response_url(response)
                if returned_url != url:
                    raise DukascopyTransportFailure(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                        "unexpected_response_url",
                    )
                headers = getattr(response, "headers", {})
                content_type = (_header_value_unchecked(headers, "Content-Type") or "").lower()
                if content_type != "application/octet-stream":
                    raise DukascopyTransportFailure(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                        "unexpected_media_type",
                    )
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise DukascopyTransportFailure(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                        "response_too_large",
                    )
                revision = _header_value(headers, "ETag") or _header_value(
                    headers, "Last-Modified"
                )
        except DukascopyTransportFailure:
            raise
        except (TimeoutError, URLError, OSError):
            raise DukascopyTransportFailure(
                ProviderFailureCategory.TRANSIENT,
                "network_unavailable",
                retryable=True,
            ) from None
        return DukascopyBi5Hour(hour_start, body, revision)


@dataclass(frozen=True)
class DukascopyBi5DirectoryTransport:
    """Offline directory transport reading raw .bi5 archives from a local mirror."""

    root: Path

    def __post_init__(self) -> None:
        if isinstance(self.root, str):
            if not self.root.strip():
                raise ValueError("root must be a non-empty path")
            object.__setattr__(self, "root", Path(self.root))
        elif not isinstance(self.root, Path):
            raise ValueError("root must be a pathlib.Path or non-empty str")
        resolved = self.root.resolve()
        object.__setattr__(self, "root", resolved)

    def fetch_hour(
        self,
        *,
        native_symbol: str,
        hour_start: datetime,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DukascopyBi5Hour:
        del timeout_seconds
        try:
            rel_path = dukascopy_bi5_relative_path(native_symbol, hour_start)
        except ValueError as exc:
            reason = str(exc)
            if reason == "symbol_unsupported":
                raise DukascopyTransportFailure(
                    ProviderFailureCategory.UNSUPPORTED, "symbol_unsupported"
                ) from exc
            if reason == "research_window_violation":
                raise DukascopyTransportFailure(
                    ProviderFailureCategory.CONFIGURATION, "research_window_violation"
                ) from exc
            raise DukascopyTransportFailure(
                ProviderFailureCategory.CONFIGURATION, "hour_start_invalid"
            ) from exc

        target_path = (self.root / rel_path).resolve()
        absent_path = target_path.with_name(f"{hour_start.hour:02d}h_ticks.absent.json")
        try:
            target_path.relative_to(self.root)
            absent_path.relative_to(self.root)
        except ValueError:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.SECURITY, "path_traversal_prohibited"
            ) from None

        has_bi5 = target_path.exists()
        has_absent = absent_path.exists()

        if has_bi5 and has_absent:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INVALID_DATA, "conflicting_partition_evidence"
            )

        if has_absent:
            if not absent_path.is_file():
                raise DukascopyTransportFailure(
                    ProviderFailureCategory.INVALID_DATA, "bi5_target_not_a_file"
                )
            try:
                payload = absent_path.read_bytes()
            except OSError:
                raise DukascopyTransportFailure(
                    ProviderFailureCategory.INVALID_DATA, "bi5_file_unreadable"
                ) from None
            _validate_absence_evidence_payload(payload, native_symbol, hour_start)
            return DukascopyBi5Hour.absent(hour_start)

        if not has_bi5:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.NO_DATA, "missing_local_bi5_file"
            )

        if not target_path.is_file():
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INVALID_DATA, "bi5_target_not_a_file"
            )

        try:
            stat_result = target_path.stat()
            file_size = stat_result.st_size
        except OSError:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INVALID_DATA, "bi5_file_unreadable"
            ) from None

        if file_size > max_response_bytes:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "response_too_large"
            )

        try:
            body = target_path.read_bytes()
        except OSError:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INVALID_DATA, "bi5_file_unreadable"
            ) from None

        if len(body) > max_response_bytes:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "response_too_large"
            )

        revision = f"mtime:{int(stat_result.st_mtime)}"
        return DukascopyBi5Hour(hour_start, body, revision)


@dataclass(frozen=True)
class DukascopyBi5Settings:
    timeout_seconds: float = 30.0
    max_response_bytes: int = 8 * 1024 * 1024
    max_hours: int = 24 * 366 * 10

    def __post_init__(self) -> None:
        try:
            timeout = float(self.timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("timeout_seconds must be finite and positive") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if timeout > _BI5_MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds exceeds the fixed transport ceiling")
        for name in ("max_response_bytes", "max_hours"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_response_bytes > _BI5_MAX_RESPONSE_BYTES:
            raise ValueError("max_response_bytes exceeds the fixed transport ceiling")
        object.__setattr__(self, "timeout_seconds", timeout)


def dukascopy_bi5_relative_path(native_symbol: str, hour_start: datetime) -> Path:
    if native_symbol not in DUKASCOPY_BI5_PRICE_DIVISORS:
        raise ValueError("symbol_unsupported")
    hour = _hour_start(hour_start)
    if hour < BI5_RESEARCH_START or hour >= BI5_RESEARCH_END:
        raise ValueError("research_window_violation")
    return Path(
        f"{native_symbol}/{hour.year:04d}/{hour.month - 1:02d}/"
        f"{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
    )


def dukascopy_bi5_url(native_symbol: str, hour_start: datetime) -> str:
    rel = dukascopy_bi5_relative_path(native_symbol, hour_start)
    return f"{_BI5_ENDPOINT}/{rel.as_posix()}"


def decode_dukascopy_bi5_hour(
    payload: bytes, hour_start: datetime, native_symbol: str
) -> tuple[DukascopyBi5Tick, ...]:
    if not isinstance(payload, bytes):
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "bi5_payload_invalid"
        )
    divisor = DUKASCOPY_BI5_PRICE_DIVISORS.get(native_symbol)
    if divisor is None:
        raise DukascopyTransportFailure(
            ProviderFailureCategory.UNSUPPORTED, "symbol_unsupported"
        )
    hour = _hour_start(hour_start)
    try:
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
        raw = decoder.decompress(payload, max_length=_BI5_MAX_DECOMPRESSED_HOUR_BYTES + 1)
        if (
            len(raw) > _BI5_MAX_DECOMPRESSED_HOUR_BYTES
            or not decoder.eof
            or decoder.unused_data
        ):
            raise ValueError
    except (lzma.LZMAError, EOFError, ValueError):
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "corrupt_bi5_lzma"
        ) from None
    if len(raw) % _BI5_RECORD.size:
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
            "malformed_bi5_record_length",
        )

    ticks: list[DukascopyBi5Tick] = []
    previous_offset: int | None = None
    for offset in range(0, len(raw), _BI5_RECORD.size):
        milliseconds, ask_raw, bid_raw, ask_volume, bid_volume = _BI5_RECORD.unpack_from(
            raw, offset
        )
        if milliseconds >= 3_600_000:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INVALID_DATA, "tick_offset_outside_hour"
            )
        if previous_offset is not None and milliseconds == previous_offset:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INVALID_DATA, "duplicate_tick_timestamp"
            )
        if previous_offset is not None and milliseconds < previous_offset:
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INVALID_DATA, "ticks_out_of_order"
            )
        try:
            tick = DukascopyBi5Tick(
                hour + timedelta(milliseconds=milliseconds),
                ask_raw / divisor,
                bid_raw / divisor,
                ask_volume,
                bid_volume,
            )
        except (TypeError, ValueError, OverflowError):
            raise DukascopyTransportFailure(
                ProviderFailureCategory.INVALID_DATA, "tick_values_invalid"
            ) from None
        ticks.append(tick)
        previous_offset = milliseconds
    return tuple(ticks)


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
            with self.opener(request, timeout=timeout_seconds) as response:
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


@dataclass(frozen=True)
class DukascopyBi5HistoricalBarsProvider:
    transport: DukascopyBi5Transport = field(repr=False)
    settings: DukascopyBi5Settings = field(default_factory=DukascopyBi5Settings)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    descriptor: ProviderDescriptor = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.settings, DukascopyBi5Settings):
            raise ValueError("settings must be DukascopyBi5Settings")
        if not callable(getattr(self.transport, "fetch_hour", None)):
            raise ValueError("transport must implement fetch_hour")
        if not callable(self.clock):
            raise ValueError("clock must be callable")
        object.__setattr__(
            self,
            "descriptor",
            ProviderDescriptor(
                _PROVIDER_ID,
                _BI5_IMPLEMENTATION_VERSION,
                frozenset({ProviderCapability.HISTORICAL_BARS, ProviderCapability.POINT_IN_TIME}),
                supported_symbols=frozenset(
                    CanonicalInstrument(symbol) for symbol in DUKASCOPY_BI5_PRICE_DIVISORS
                ),
                supported_timeframes=frozenset({"D1"}),
                deterministic=False,
                normalization_version=_BI5_NORMALIZATION_VERSION,
            ),
        )

    @property
    def mapping_fingerprint(self) -> str:
        return symbol_mapping_fingerprint(
            {symbol: symbol for symbol in DUKASCOPY_BI5_PRICE_DIVISORS}
        )

    def fetch_bars(self, query: BarQuery) -> BarDataset | ProviderFailure:
        if not isinstance(query, BarQuery):
            return _failure(ProviderFailureCategory.CONFIGURATION, "query_invalid")
        if query.instrument.symbol not in DUKASCOPY_BI5_PRICE_DIVISORS:
            return _failure(ProviderFailureCategory.UNSUPPORTED, "symbol_unsupported")
        if query.timeframe != "D1":
            return _failure(ProviderFailureCategory.UNSUPPORTED, "timeframe_unsupported")
        if not _valid_bi5_research_query(query):
            return _failure(ProviderFailureCategory.CONFIGURATION, "research_window_violation")
        hour_count = int((query.end - query.start).total_seconds() // 3600)
        if hour_count > self.settings.max_hours:
            return _failure(ProviderFailureCategory.CONFIGURATION, "hour_limit_exceeded")

        ticks: list[DukascopyBi5Tick] = []
        source_items: list[dict[str, object]] = []
        for number in range(hour_count):
            hour_start = query.start + timedelta(hours=number)
            try:
                source = self.transport.fetch_hour(
                    native_symbol=query.instrument.symbol,
                    hour_start=hour_start,
                    timeout_seconds=self.settings.timeout_seconds,
                    max_response_bytes=self.settings.max_response_bytes,
                )
            except DukascopyTransportFailure as exc:
                return _failure(exc.category, exc.reason, retryable=exc.retryable)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                return _failure(ProviderFailureCategory.INTERNAL, "transport_invariant_failed")
            if not isinstance(source, DukascopyBi5Hour) or source.hour_start != hour_start:
                return _failure(
                    ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "hour_response_invalid"
                )
            source_items.append(
                {
                    "hour": hour_start.isoformat(),
                    "absent": source.is_absent,
                    "raw_sha256": None if source.is_absent else source.raw_sha256,
                    "byte_count": len(source.body),
                    "revision": source.revision,
                }
            )
            if source.is_absent:
                continue
            try:
                decoded = decode_dukascopy_bi5_hour(
                    source.body, hour_start, query.instrument.symbol
                )
            except DukascopyTransportFailure as exc:
                return _failure(exc.category, exc.reason, retryable=exc.retryable)
            ticks.extend(decoded)
        if not ticks:
            return _failure(ProviderFailureCategory.NO_DATA, "no_ticks")

        try:
            frame = _bi5_daily_frame(ticks, query)
            retrieved_at = _aware_utc(self.clock(), "retrieved_at")
            content_hash = bar_content_hash(frame)
            source_hash = hashlib.sha256(
                json.dumps(source_items, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            provenance = DataProvenance(
                provider_id=self.descriptor.provider_id,
                provider_version=self.descriptor.implementation_version,
                normalization_version=self.descriptor.normalization_version,
                canonical_symbol=query.instrument.symbol,
                provider_symbol=query.instrument.symbol,
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
                revision=f"bi5_hour_set_sha256:{source_hash}",
                source_timezone="UTC",
                volume_semantics="sum_bid_tick_volume_millions_base",
                provenance_quality=ProvenanceQuality.VERIFIED,
                sanitized_source_reference=_BI5_SOURCE_REFERENCE,
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


def _response_url(response: object) -> str:
    getter = getattr(response, "geturl", None)
    value = getter() if callable(getter) else getattr(response, "url", None)
    return value if isinstance(value, str) else ""


def _header_value_unchecked(headers: object, key: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bi5_http_failure(status: int) -> DukascopyTransportFailure:
    if status == 401:
        return DukascopyTransportFailure(
            ProviderFailureCategory.AUTHENTICATION, "authentication_failed"
        )
    if status == 403:
        return DukascopyTransportFailure(
            ProviderFailureCategory.CONFIGURATION, "access_forbidden"
        )
    if status == 429:
        return DukascopyTransportFailure(
            ProviderFailureCategory.RATE_LIMIT, "rate_limited", retryable=True
        )
    if 500 <= status <= 599:
        return DukascopyTransportFailure(
            ProviderFailureCategory.TRANSIENT, "provider_unavailable", retryable=True
        )
    if 300 <= status <= 399:
        return DukascopyTransportFailure(
            ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "redirect_not_allowed"
        )
    return DukascopyTransportFailure(
        ProviderFailureCategory.INTERNAL, "unexpected_http_status"
    )


def _hour_start(value: datetime) -> datetime:
    hour = _aware_utc(value, "hour_start")
    if any((hour.minute, hour.second, hour.microsecond)):
        raise ValueError("hour_start must be aligned to a UTC hour")
    return hour


def _valid_bi5_research_query(query: BarQuery) -> bool:
    return (
        query.start >= BI5_RESEARCH_START
        and query.end <= BI5_RESEARCH_END
        and query.as_of >= query.end
        and query.start.hour == 0
        and query.start.minute == 0
        and query.start.second == 0
        and query.start.microsecond == 0
        and query.end.hour == 0
        and query.end.minute == 0
        and query.end.second == 0
        and query.end.microsecond == 0
    )


def _bi5_daily_frame(ticks: list[DukascopyBi5Tick], query: BarQuery) -> pd.DataFrame:
    by_day: dict[datetime, list[DukascopyBi5Tick]] = {}
    previous: datetime | None = None
    for tick in ticks:
        if previous is not None and tick.timestamp <= previous:
            raise ValueError("ticks must be globally strictly increasing")
        previous = tick.timestamp
        day = tick.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day.setdefault(day, []).append(tick)
    rows: list[list[float]] = []
    index: list[datetime] = []
    for day, day_ticks in by_day.items():
        bids = [tick.bid for tick in day_ticks]
        rows.append(
            [
                bids[0],
                max(bids),
                min(bids),
                bids[-1],
                sum(tick.bid_volume for tick in day_ticks),
            ]
        )
        index.append(day)
    frame = pd.DataFrame(
        rows,
        index=pd.DatetimeIndex(index, name="ts_open"),
        columns=OHLCV,
        dtype="float64",
    )
    frame.attrs = {"symbol": query.instrument.symbol, "timeframe": query.timeframe}
    return frame


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


def _iso_utc(value: datetime) -> str:
    iso = value.astimezone(UTC).isoformat()
    if iso.endswith("+00:00"):
        return iso[:-6] + "Z"
    return iso


def _validate_absence_evidence_payload(
    payload: bytes, expected_symbol: str, expected_hour: datetime
) -> None:
    if not isinstance(payload, bytes) or not payload:
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INVALID_DATA, "malformed_absence_record"
        )
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INVALID_DATA, "malformed_absence_record"
        ) from None
    if not isinstance(data, dict):
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INVALID_DATA, "malformed_absence_record"
        )
    if (
        data.get("schema_version") != 1
        or data.get("record_type") != "dukascopy_bi5_upstream_absence"
        or data.get("symbol") != expected_symbol
        or data.get("sanitized_source_reference") != _BI5_SOURCE_REFERENCE
        or data.get("http_status") != 404
    ):
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INVALID_DATA, "malformed_absence_record"
        )
    expected_iso = _iso_utc(_aware_utc(expected_hour, "expected_hour"))
    if data.get("hour_utc") != expected_iso:
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INVALID_DATA, "malformed_absence_record"
        )
    retrieved_at = data.get("retrieved_at_utc")
    if not isinstance(retrieved_at, str) or not retrieved_at.strip():
        raise DukascopyTransportFailure(
            ProviderFailureCategory.INVALID_DATA, "malformed_absence_record"
        )
