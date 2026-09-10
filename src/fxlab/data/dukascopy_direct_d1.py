"""Prospective Dukascopy direct BID D1 provider with offline-verifiable provenance."""

from __future__ import annotations

import calendar
import hashlib
import json
import lzma
import math
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

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

DIRECT_D1_PROVIDER_ID = "dukascopy_direct_d1"
DIRECT_D1_PROVIDER_VERSION = "dukascopy_direct_d1_v1"
DIRECT_D1_NORMALIZATION_VERSION = "dukascopy_direct_bid_d1_v1"
DIRECT_D1_SOURCE_REFERENCE = "dukascopy:datafeed:direct:d1:bid"
DIRECT_D1_DECODER_VERSION = "dukascopy_direct_d1_bi5_decoder_v1"
DIRECT_D1_VOLUME_SEMANTICS = "vendor_supplied_direct_d1_bid_candle_volume_float32"
DIRECT_D1_RESEARCH_START = datetime(2014, 1, 1, tzinfo=UTC)
DIRECT_D1_RESEARCH_END = datetime(2024, 1, 1, tzinfo=UTC)
DIRECT_D1_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DIRECT_D1_MAX_DECOMPRESSED_BYTES = 16 * 1024 * 1024
DIRECT_D1_MAX_TIMEOUT_SECONDS = 30.0
DIRECT_D1_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0)
DIRECT_D1_ENDPOINT = "https://datafeed.dukascopy.com/datafeed"
DIRECT_D1_RECORD = struct.Struct(">IIIIIf")
DIRECT_D1_PRICE_DIVISORS: Mapping[str, int] = MappingProxyType(
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


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _default_opener() -> Callable[..., object]:
    return build_opener(_NoRedirectHandler()).open


class DukascopyDirectD1TransportError(RuntimeError):
    """A sanitized fail-closed direct-D1 transport failure."""

    def __init__(
        self,
        category: ProviderFailureCategory,
        reason: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.category = category
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


@dataclass(frozen=True)
class DukascopyDirectD1Year:
    pair: str
    year: int
    body: bytes
    requested_url: str
    returned_url: str
    response_media_type: str
    response_headers: tuple[tuple[str, str], ...] = ()

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def _validate_pair_year(pair: str, year: int) -> None:
    if pair not in DIRECT_D1_PRICE_DIVISORS:
        raise ValueError("symbol_unsupported")
    if isinstance(year, bool) or not isinstance(year, int):
        raise ValueError("year_invalid")
    if year < DIRECT_D1_RESEARCH_START.year or year >= DIRECT_D1_RESEARCH_END.year:
        raise ValueError("research_window_violation")


def dukascopy_direct_d1_relative_path(pair: str, year: int) -> Path:
    _validate_pair_year(pair, year)
    return Path(pair) / str(year) / "BID_candles_day_1.bi5"


def dukascopy_direct_d1_url(pair: str, year: int) -> str:
    relative = dukascopy_direct_d1_relative_path(pair, year).as_posix()
    return f"{DIRECT_D1_ENDPOINT}/{relative}"


def _decompress_bounded(body: bytes) -> bytes:
    if not isinstance(body, bytes) or not body:
        raise ValueError("direct_d1_body_invalid")
    if len(body) > DIRECT_D1_MAX_RESPONSE_BYTES:
        raise ValueError("direct_d1_compressed_size_invalid")
    try:
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
        decoded = decoder.decompress(body, max_length=DIRECT_D1_MAX_DECOMPRESSED_BYTES + 1)
    except lzma.LZMAError as exc:
        raise ValueError("direct_d1_lzma_invalid") from exc
    if len(decoded) > DIRECT_D1_MAX_DECOMPRESSED_BYTES:
        raise ValueError("direct_d1_decompressed_size_invalid")
    if not decoder.eof or decoder.unused_data:
        raise ValueError("direct_d1_lzma_invalid")
    return decoded


def decode_dukascopy_direct_d1_year(body: bytes, pair: str, year: int) -> pd.DataFrame:
    """Decode and fully validate one immutable Dukascopy direct-D1 year."""
    _validate_pair_year(pair, year)
    raw = _decompress_bounded(body)
    if len(raw) % DIRECT_D1_RECORD.size:
        raise ValueError("direct_d1_record_length_invalid")
    expected_count = 366 if calendar.isleap(year) else 365
    if len(raw) // DIRECT_D1_RECORD.size != expected_count:
        raise ValueError("year_record_count_invalid")

    offsets: list[int] = []
    encoded_values: list[tuple[int, int, int, int, float]] = []
    for record in DIRECT_D1_RECORD.iter_unpack(raw):
        offset, open_raw, close_raw, low_raw, high_raw, volume = record
        offsets.append(offset)
        encoded_values.append((open_raw, high_raw, low_raw, close_raw, volume))

    expected_offsets = [day * 86_400 for day in range(expected_count)]
    if any(offset % 86_400 for offset in offsets):
        raise ValueError("timestamp_not_midnight")
    if any(
        current <= previous
        for previous, current in zip(offsets, offsets[1:], strict=False)
    ):
        raise ValueError("timestamp_order_invalid")
    if offsets != expected_offsets:
        raise ValueError("timestamp_calendar_invalid")

    year_start = datetime(year, 1, 1, tzinfo=UTC)
    timestamps = [year_start + timedelta(seconds=offset) for offset in offsets]
    if timestamps[0] < DIRECT_D1_RESEARCH_START or timestamps[-1] >= DIRECT_D1_RESEARCH_END:
        raise ValueError("research_window_violation")

    divisor = DIRECT_D1_PRICE_DIVISORS[pair]
    rows: list[tuple[float, float, float, float, float]] = []
    for open_raw, high_raw, low_raw, close_raw, volume_raw in encoded_values:
        if min(open_raw, high_raw, low_raw, close_raw) <= 0:
            raise ValueError("price_invalid")
        volume = float(volume_raw)
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("volume_invalid")
        open_value = open_raw / divisor
        high_value = high_raw / divisor
        low_value = low_raw / divisor
        close_value = close_raw / divisor
        if high_value < max(open_value, low_value, close_value) or low_value > min(
            open_value, high_value, close_value
        ):
            raise ValueError("ohlc_invalid")
        rows.append((open_value, high_value, low_value, close_value, volume))

    frame = pd.DataFrame(
        rows,
        index=pd.DatetimeIndex(timestamps, name="ts_open"),
        columns=["open", "high", "low", "close", "volume"],
        dtype="float64",
    )
    frame.attrs.update(symbol=pair, timeframe="D1")
    return frame


@dataclass(frozen=True)
class DukascopyDirectD1HttpTransport:
    opener: Callable[..., object] = field(default_factory=_default_opener, repr=False)
    sleeper: Callable[[float], None] = field(default=time.sleep, repr=False)

    def fetch_year(
        self,
        *,
        pair: str,
        year: int,
        timeout_seconds: float = DIRECT_D1_MAX_TIMEOUT_SECONDS,
        max_response_bytes: int = DIRECT_D1_MAX_RESPONSE_BYTES,
    ) -> DukascopyDirectD1Year:
        for attempt in range(len(DIRECT_D1_RETRY_BACKOFF_SECONDS) + 1):
            try:
                return self._fetch_year_once(
                    pair=pair,
                    year=year,
                    timeout_seconds=timeout_seconds,
                    max_response_bytes=max_response_bytes,
                )
            except DukascopyDirectD1TransportError as exc:
                if not exc.retryable or attempt == len(DIRECT_D1_RETRY_BACKOFF_SECONDS):
                    raise
                self.sleeper(DIRECT_D1_RETRY_BACKOFF_SECONDS[attempt])
        raise AssertionError("unreachable")

    def _fetch_year_once(
        self,
        *,
        pair: str,
        year: int,
        timeout_seconds: float = DIRECT_D1_MAX_TIMEOUT_SECONDS,
        max_response_bytes: int = DIRECT_D1_MAX_RESPONSE_BYTES,
    ) -> DukascopyDirectD1Year:
        url = dukascopy_direct_d1_url(pair, year)
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > DIRECT_D1_MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_invalid")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not (
            0 < max_response_bytes <= DIRECT_D1_MAX_RESPONSE_BYTES
        ):
            raise ValueError("response_limit_invalid")
        request = Request(
            url,
            method="GET",
            headers={"Accept": "application/octet-stream", "User-Agent": "fxlab-market-data/1"},
        )
        try:
            response = self.opener(request, timeout=timeout_seconds)
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise DukascopyDirectD1TransportError(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "redirect_not_allowed"
                ) from None
            if exc.code == 404:
                category, reason, retryable = (
                    ProviderFailureCategory.NO_DATA,
                    "year_not_found",
                    False,
                )
            elif exc.code == 429:
                category, reason, retryable = (
                    ProviderFailureCategory.RATE_LIMIT,
                    "provider_rate_limited",
                    True,
                )
            elif 500 <= exc.code <= 599:
                category, reason, retryable = (
                    ProviderFailureCategory.TRANSIENT, "provider_unavailable", True
                )
            elif exc.code in (401, 403):
                category, reason, retryable = (
                    ProviderFailureCategory.AUTHENTICATION,
                    "http_request_rejected",
                    False,
                )
            else:
                category, reason, retryable = (
                    ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                    "http_request_rejected",
                    False,
                )
            raise DukascopyDirectD1TransportError(category, reason, retryable=retryable) from None
        except (TimeoutError, URLError, OSError):
            raise DukascopyDirectD1TransportError(
                ProviderFailureCategory.TRANSIENT, "network_unavailable", retryable=True
            ) from None
        try:
            with response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise DukascopyDirectD1TransportError(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
                        "unexpected_http_status",
                    )
                returned_url = str(response.geturl())
                if returned_url != url:
                    raise DukascopyDirectD1TransportError(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "unexpected_response_url"
                    )
                headers = getattr(response, "headers", {})
                media_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
                if media_type != "application/octet-stream":
                    raise DukascopyDirectD1TransportError(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "unexpected_media_type"
                    )
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise DukascopyDirectD1TransportError(
                        ProviderFailureCategory.INCOMPATIBLE_SCHEMA, "response_too_large"
                    )
                if not body:
                    raise DukascopyDirectD1TransportError(
                        ProviderFailureCategory.INVALID_DATA, "empty_year_body"
                    )
                safe_headers = tuple(
                    sorted(
                        (name, str(headers[name]))
                        for name in ("Content-Type", "ETag", "Last-Modified")
                        if name in headers
                    )
                )
        except DukascopyDirectD1TransportError:
            raise
        except (TimeoutError, URLError, OSError):
            raise DukascopyDirectD1TransportError(
                ProviderFailureCategory.TRANSIENT, "network_unavailable", retryable=True
            ) from None
        return DukascopyDirectD1Year(
            pair, year, body, url, returned_url, media_type, safe_headers
        )


@dataclass(frozen=True)
class DukascopyDirectD1DirectoryTransport:
    root: Path

    def __post_init__(self) -> None:
        root = Path(self.root).resolve()
        object.__setattr__(self, "root", root)

    def fetch_year(
        self,
        *,
        pair: str,
        year: int,
        max_response_bytes: int = DIRECT_D1_MAX_RESPONSE_BYTES,
    ) -> DukascopyDirectD1Year:
        relative = dukascopy_direct_d1_relative_path(pair, year)
        raw_path = (self.root / relative).resolve()
        sidecar_path = raw_path.with_name("acquisition.json")
        try:
            raw_path.relative_to(self.root)
            sidecar_path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("path_traversal_prohibited") from exc
        if not raw_path.exists() and not sidecar_path.exists():
            raise ValueError("missing_direct_d1_year")
        if not raw_path.exists():
            raise ValueError("missing_direct_d1_year")
        if not sidecar_path.exists():
            raise ValueError("missing_acquisition_sidecar")
        if not raw_path.is_file() or not sidecar_path.is_file():
            raise ValueError("direct_d1_artifact_invalid")
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("acquisition_sidecar_invalid") from exc
        if not isinstance(data, dict):
            raise ValueError("acquisition_sidecar_invalid")
        required = {
            "schema", "provider_id", "provider_version", "source_reference", "pair", "year",
            "requested_url", "returned_url", "http_status", "response_media_type",
            "response_headers", "byte_count", "raw_sha256", "decoder_version", "retrieved_at_utc",
        }
        if set(data) != required:
            raise ValueError("acquisition_sidecar_invalid")
        if data["pair"] != pair:
            raise ValueError("sidecar_pair_mismatch")
        if data["year"] != year:
            raise ValueError("sidecar_year_mismatch")
        if data["decoder_version"] != DIRECT_D1_DECODER_VERSION:
            raise ValueError("decoder_version_mismatch")
        if (
            data["schema"] != "dukascopy_direct_d1_acquisition.v1"
            or data["provider_id"] != DIRECT_D1_PROVIDER_ID
            or data["provider_version"] != DIRECT_D1_PROVIDER_VERSION
            or data["source_reference"] != DIRECT_D1_SOURCE_REFERENCE
            or data["requested_url"] != dukascopy_direct_d1_url(pair, year)
            or data["returned_url"] != data["requested_url"]
            or data["http_status"] != 200
            or data["response_media_type"] != "application/octet-stream"
        ):
            raise ValueError("acquisition_sidecar_invalid")
        try:
            retrieved_at = datetime.fromisoformat(
                str(data["retrieved_at_utc"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("acquisition_sidecar_invalid") from exc
        if retrieved_at.tzinfo is None:
            raise ValueError("acquisition_sidecar_invalid")
        response_headers = data["response_headers"]
        allowed_headers = {"Content-Type", "ETag", "Last-Modified"}
        if not isinstance(response_headers, list):
            raise ValueError("acquisition_sidecar_invalid")
        for item in response_headers:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or item[0] not in allowed_headers
                or not isinstance(item[1], str)
                or not item[1]
                or "\r" in item[1]
                or "\n" in item[1]
            ):
                raise ValueError("acquisition_sidecar_invalid")
        try:
            size = raw_path.stat().st_size
        except OSError as exc:
            raise ValueError("direct_d1_artifact_unreadable") from exc
        if size > max_response_bytes or size != data["byte_count"]:
            raise ValueError("byte_count_mismatch")
        try:
            body = raw_path.read_bytes()
        except OSError as exc:
            raise ValueError("direct_d1_artifact_unreadable") from exc
        if hashlib.sha256(body).hexdigest() != data["raw_sha256"]:
            raise ValueError("raw_sha256_mismatch")
        decode_dukascopy_direct_d1_year(body, pair, year)
        return DukascopyDirectD1Year(
            pair,
            year,
            body,
            data["requested_url"],
            data["returned_url"],
            data["response_media_type"],
            tuple(tuple(item) for item in data["response_headers"]),
        )


@dataclass(frozen=True)
class DukascopyDirectD1HistoricalBarsProvider:
    transport: object = field(repr=False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    descriptor: ProviderDescriptor = field(init=False)

    def __post_init__(self) -> None:
        if not callable(getattr(self.transport, "fetch_year", None)):
            raise ValueError("transport must implement fetch_year")
        object.__setattr__(
            self,
            "descriptor",
            ProviderDescriptor(
                DIRECT_D1_PROVIDER_ID,
                DIRECT_D1_PROVIDER_VERSION,
                frozenset({ProviderCapability.HISTORICAL_BARS, ProviderCapability.POINT_IN_TIME}),
                supported_symbols=frozenset(
                    CanonicalInstrument(pair) for pair in DIRECT_D1_PRICE_DIVISORS
                ),
                supported_timeframes=frozenset({"D1"}),
                coverage_start=DIRECT_D1_RESEARCH_START,
                coverage_end=DIRECT_D1_RESEARCH_END,
                deterministic=False,
                normalization_version=DIRECT_D1_NORMALIZATION_VERSION,
            ),
        )

    def fetch_bars(self, query: BarQuery) -> BarDataset | ProviderFailure:
        def failure(category: ProviderFailureCategory, reason: str) -> ProviderFailure:
            return ProviderFailure(category, reason, DIRECT_D1_PROVIDER_ID)

        if not isinstance(query, BarQuery):
            return failure(ProviderFailureCategory.CONFIGURATION, "query_invalid")
        if query.instrument.symbol not in DIRECT_D1_PRICE_DIVISORS:
            return failure(ProviderFailureCategory.UNSUPPORTED, "symbol_unsupported")
        if query.timeframe != "D1":
            return failure(ProviderFailureCategory.UNSUPPORTED, "timeframe_unsupported")
        if (
            query.start < DIRECT_D1_RESEARCH_START
            or query.end > DIRECT_D1_RESEARCH_END
            or query.as_of > DIRECT_D1_RESEARCH_END
        ):
            return failure(ProviderFailureCategory.CONFIGURATION, "research_window_violation")
        last_year = (query.end - timedelta(microseconds=1)).year
        sources: list[DukascopyDirectD1Year] = []
        frames: list[pd.DataFrame] = []
        for year in range(query.start.year, last_year + 1):
            try:
                source = self.transport.fetch_year(
                    pair=query.instrument.symbol,
                    year=year,
                    max_response_bytes=DIRECT_D1_MAX_RESPONSE_BYTES,
                )
                expected_url = dukascopy_direct_d1_url(query.instrument.symbol, year)
                if (
                    not isinstance(source, DukascopyDirectD1Year)
                    or source.pair != query.instrument.symbol
                    or source.year != year
                    or source.requested_url != expected_url
                    or source.returned_url != expected_url
                    or source.response_media_type != "application/octet-stream"
                ):
                    raise ValueError("year_response_invalid")
                frame = decode_dukascopy_direct_d1_year(source.body, query.instrument.symbol, year)
            except ValueError as exc:
                return failure(ProviderFailureCategory.INVALID_DATA, str(exc))
            sources.append(source)
            frames.append(frame)
        combined = pd.concat(frames).sort_index()
        selected = combined.loc[
            (combined.index >= query.start) & (combined.index < query.end)
        ].copy()
        if selected.empty:
            return failure(ProviderFailureCategory.NO_DATA, "no_direct_d1_rows")
        selected.attrs.update(symbol=query.instrument.symbol, timeframe="D1")
        source_entries = [
            {
                "pair": source.pair,
                "year": source.year,
                "raw_sha256": source.raw_sha256,
                "byte_count": len(source.body),
                "decoder_version": DIRECT_D1_DECODER_VERSION,
            }
            for source in sources
        ]
        source_hash = hashlib.sha256(
            json.dumps(source_entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            retrieved_at = self.clock().astimezone(UTC)
            content_hash = bar_content_hash(selected)
            provenance = DataProvenance(
                provider_id=DIRECT_D1_PROVIDER_ID,
                provider_version=DIRECT_D1_PROVIDER_VERSION,
                normalization_version=DIRECT_D1_NORMALIZATION_VERSION,
                canonical_symbol=query.instrument.symbol,
                provider_symbol=query.instrument.symbol,
                timeframe="D1",
                query_start=query.start,
                query_end=query.end,
                query_as_of=query.as_of,
                retrieved_at=retrieved_at,
                actual_first_observation=selected.index[0].to_pydatetime(),
                actual_last_observation=selected.index[-1].to_pydatetime(),
                row_count=len(selected),
                content_hash=content_hash,
                query_fingerprint=query.fingerprint,
                dataset_id=dataset_identity(
                    DIRECT_D1_PROVIDER_ID,
                    DIRECT_D1_PROVIDER_VERSION,
                    query.fingerprint,
                    content_hash,
                ),
                revision=f"direct_d1_year_set_sha256:{source_hash}",
                source_timezone="UTC",
                volume_semantics=DIRECT_D1_VOLUME_SEMANTICS,
                provenance_quality=ProvenanceQuality.VERIFIED,
                sanitized_source_reference=DIRECT_D1_SOURCE_REFERENCE,
            )
            return BarDataset(query, selected, provenance)
        except (TypeError, ValueError, OverflowError):
            return failure(ProviderFailureCategory.INVALID_DATA, "canonical_validation_failed")


__all__ = [
    "DIRECT_D1_DECODER_VERSION",
    "DIRECT_D1_MAX_RESPONSE_BYTES",
    "DIRECT_D1_NORMALIZATION_VERSION",
    "DIRECT_D1_PRICE_DIVISORS",
    "DIRECT_D1_PROVIDER_ID",
    "DIRECT_D1_PROVIDER_VERSION",
    "DIRECT_D1_RESEARCH_END",
    "DIRECT_D1_RESEARCH_START",
    "DIRECT_D1_SOURCE_REFERENCE",
    "DIRECT_D1_VOLUME_SEMANTICS",
    "DukascopyDirectD1DirectoryTransport",
    "DukascopyDirectD1HistoricalBarsProvider",
    "DukascopyDirectD1HttpTransport",
    "DukascopyDirectD1TransportError",
    "DukascopyDirectD1Year",
    "dataset_identity",
    "decode_dukascopy_direct_d1_year",
    "dukascopy_direct_d1_relative_path",
    "dukascopy_direct_d1_url",
]
