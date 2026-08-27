"""Focused tests for the bounded Dukascopy historical-bars connector."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from fxlab.data import (
    BarQuery,
    CanonicalInstrument,
    ProvenanceQuality,
    ProviderCapability,
    ProviderFailure,
    ProviderFailureCategory,
)
from fxlab.data.dukascopy_provider import (
    DUKASCOPY_MAPPING_FINGERPRINT,
    DukascopyConnectorSettings,
    DukascopyHistoricalBarsProvider,
    DukascopyHttpTransport,
    DukascopyPage,
    DukascopyTransportFailure,
    symbol_mapping_fingerprint,
)
from fxlab.data.ingest_dukascopy import fetch_dukascopy


def ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def row(timestamp: str, close: float = 1.1) -> tuple[object, ...]:
    return (ms(timestamp), close, close + 0.001, close - 0.001, close, 100.0)


def query(
    *,
    symbol: str = "EURUSD",
    timeframe: str = "M5",
    start: str = "2026-01-01T00:00:00+00:00",
    end: str = "2026-01-01T00:15:00+00:00",
    as_of: str = "2026-01-01T00:15:00+00:00",
) -> BarQuery:
    return BarQuery(
        CanonicalInstrument(symbol),
        timeframe,
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
        datetime.fromisoformat(as_of),
    )


class FakeTransport:
    def __init__(
        self,
        pages: list[DukascopyPage] | None = None,
        failure: DukascopyTransportFailure | None = None,
    ) -> None:
        self.pages = list(pages or [])
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    def fetch_page(self, **kwargs: object) -> DukascopyPage:
        self.calls.append(dict(kwargs))
        if self.failure is not None:
            raise self.failure
        return self.pages.pop(0)


def provider(
    pages: list[DukascopyPage] | None = None,
    *,
    failure: DukascopyTransportFailure | None = None,
    settings: DukascopyConnectorSettings | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[DukascopyHistoricalBarsProvider, FakeTransport]:
    transport = FakeTransport(pages, failure)
    return (
        DukascopyHistoricalBarsProvider(
            transport,
            settings=settings or DukascopyConnectorSettings(),
            clock=clock or (lambda: datetime(2026, 1, 2, tzinfo=UTC)),
        ),
        transport,
    )


def test_descriptor_declares_only_historical_point_in_time() -> None:
    item, _ = provider([])
    assert item.descriptor.provider_id == "dukascopy"
    assert item.descriptor.capabilities == frozenset(
        {ProviderCapability.HISTORICAL_BARS, ProviderCapability.POINT_IN_TIME}
    )
    assert item.descriptor.deterministic is False


def test_settings_are_finite_positive_and_bounded() -> None:
    for kwargs in (
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"page_size": 0},
        {"page_size": 30_001},
        {"max_response_bytes": 0},
        {"max_pages": 0},
    ):
        with pytest.raises(ValueError):
            DukascopyConnectorSettings(**kwargs)


def test_mapping_fingerprint_is_stable_and_content_bound() -> None:
    first = symbol_mapping_fingerprint({"EURUSD": "EUR/USD", "USDJPY": "USD/JPY"})
    second = symbol_mapping_fingerprint({"USDJPY": "USD/JPY", "EURUSD": "EUR/USD"})
    changed = symbol_mapping_fingerprint({"EURUSD": "EUR/USD", "USDJPY": "USDJPY"})
    assert first == second
    assert first != changed
    assert len(DUKASCOPY_MAPPING_FINGERPRINT) == 64
    with pytest.raises(ValueError):
        symbol_mapping_fingerprint({"EURUSD": "EUR/USD", "GBPUSD": "EUR/USD"})


@pytest.mark.parametrize("timeframe", ["M1", "M5", "M15", "M30", "H1", "H4", "D1"])
def test_supported_timeframes_are_invoked_explicitly(timeframe: str) -> None:
    item, transport = provider([DukascopyPage((), complete=True)])
    result = item.fetch_bars(
        query(
            timeframe=timeframe, end="2026-01-02T00:00:00+00:00", as_of="2026-01-02T00:00:00+00:00"
        )
    )
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.NO_DATA
    assert transport.calls[0]["native_timeframe"]


def test_unknown_symbol_mapping_is_unsupported() -> None:
    item, transport = provider([])
    result = item.fetch_bars(query(symbol="BTCUSD"))
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.UNSUPPORTED
    assert result.reason == "symbol_unsupported"
    assert transport.calls == []


def test_provider_descriptor_exposes_no_approximated_timeframes() -> None:
    item, _ = provider([])
    assert item.descriptor.supported_timeframes == frozenset(
        {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}
    )
    with pytest.raises(ValueError):
        query(timeframe="M10")


def test_valid_pages_are_canonical_point_in_time_and_provenanced() -> None:
    item, transport = provider(
        [
            DukascopyPage(
                (row("2026-01-01T00:00:00+00:00"), row("2026-01-01T00:05:00+00:00")),
                complete=False,
                revision="etag-1",
            ),
            DukascopyPage(
                (
                    row("2026-01-01T00:05:00+00:00"),
                    row("2026-01-01T00:10:00+00:00"),
                ),
                complete=True,
                revision="etag-1",
            ),
        ]
    )
    result = item.fetch_bars(query())
    assert not isinstance(result, ProviderFailure)
    assert len(transport.calls) == 2
    assert transport.calls[0]["timeout_seconds"] == 10.0
    assert transport.calls[0]["end_ms"] == ms("2026-01-01T00:15:00+00:00")
    assert list(result.frame.columns) == ["open", "high", "low", "close", "volume"]
    assert all(str(dtype) == "float64" for dtype in result.frame.dtypes)
    assert result.provenance.provider_id == "dukascopy"
    assert result.provenance.provider_symbol == "EUR/USD"
    assert result.provenance.provenance_quality is ProvenanceQuality.VERIFIED
    assert result.provenance.volume_semantics == "provider_reported_units"
    assert result.provenance.sanitized_source_reference == "dukascopy:historical:bid"
    assert result.provenance.revision == "etag-1"


def test_forming_candle_is_excluded_at_exact_point_in_time_boundary() -> None:
    item, _ = provider(
        [
            DukascopyPage(
                (
                    row("2026-01-01T00:00:00+00:00"),
                    row("2026-01-01T00:05:00+00:00"),
                    row("2026-01-01T00:10:00+00:00"),
                ),
                complete=True,
            )
        ]
    )
    result = item.fetch_bars(query(as_of="2026-01-01T00:10:00+00:00"))
    assert not isinstance(result, ProviderFailure)
    assert list(result.frame.index.minute) == [0, 5]


def test_retrieval_time_does_not_change_content_identity() -> None:
    pages = [DukascopyPage((row("2026-01-01T00:00:00+00:00"),), complete=True)]
    first, _ = provider(list(pages), clock=lambda: datetime(2026, 1, 2, tzinfo=UTC))
    second, _ = provider(list(pages), clock=lambda: datetime(2026, 1, 3, tzinfo=UTC))
    one = first.fetch_bars(
        query(end="2026-01-01T00:05:00+00:00", as_of="2026-01-01T00:05:00+00:00")
    )
    two = second.fetch_bars(
        query(end="2026-01-01T00:05:00+00:00", as_of="2026-01-01T00:05:00+00:00")
    )
    assert not isinstance(one, ProviderFailure) and not isinstance(two, ProviderFailure)
    assert one.provenance.retrieved_at != two.provenance.retrieved_at
    assert one.provenance.content_hash == two.provenance.content_hash
    assert one.provenance.dataset_id == two.provenance.dataset_id


def test_later_rows_do_not_change_an_earlier_as_of_result() -> None:
    early_rows = (
        row("2026-01-01T00:00:00+00:00"),
        row("2026-01-01T00:05:00+00:00"),
        row("2026-01-01T00:10:00+00:00"),
    )
    class BoundedSourceTransport(FakeTransport):
        def __init__(self, source: tuple[tuple[object, ...], ...]) -> None:
            super().__init__()
            self.source = source

        def fetch_page(self, **kwargs: object) -> DukascopyPage:
            self.calls.append(dict(kwargs))
            end_ms = int(kwargs["end_ms"])
            bounded = tuple(item for item in self.source if int(item[0]) <= end_ms)
            return DukascopyPage(bounded, complete=True)

    first = DukascopyHistoricalBarsProvider(BoundedSourceTransport(early_rows))
    second = DukascopyHistoricalBarsProvider(
        BoundedSourceTransport(early_rows + (row("2026-01-01T00:15:00+00:00"),))
    )
    early_query = query(
        end="2026-01-01T00:20:00+00:00", as_of="2026-01-01T00:10:00+00:00"
    )
    one = first.fetch_bars(early_query)
    two = second.fetch_bars(early_query)
    assert not isinstance(one, ProviderFailure) and not isinstance(two, ProviderFailure)
    assert one.provenance.content_hash == two.provenance.content_hash


def test_unexpected_clock_failure_is_structured_internal_failure() -> None:
    def broken_clock() -> datetime:
        raise RuntimeError("credential-bearing internal detail")

    item, _ = provider(
        [DukascopyPage((row("2026-01-01T00:00:00+00:00"),), complete=True)],
        clock=broken_clock,
    )
    result = item.fetch_bars(
        query(end="2026-01-01T00:05:00+00:00", as_of="2026-01-01T00:05:00+00:00")
    )
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.INTERNAL
    assert "credential" not in result.reason


@pytest.mark.parametrize(
    ("rows", "category"),
    [
        (
            (row("2026-01-01T00:05:00+00:00"), row("2026-01-01T00:00:00+00:00")),
            ProviderFailureCategory.INVALID_DATA,
        ),
        (
            (row("2026-01-01T00:00:00+00:00"), row("2026-01-01T00:00:00+00:00")),
            ProviderFailureCategory.INVALID_DATA,
        ),
        (
            ((ms("2026-01-01T00:00:00+00:00"), 1.1, 1.0, 1.2, 1.1, 1.0),),
            ProviderFailureCategory.INVALID_DATA,
        ),
        (
            ((ms("2026-01-01T00:00:00+00:00"), 1.1, 1.2, 1.0, float("nan"), 1.0),),
            ProviderFailureCategory.INVALID_DATA,
        ),
        ((row("2026-01-01T00:15:00+00:00"),), ProviderFailureCategory.INVALID_DATA),
        ((("bad-time", 1.1, 1.2, 1.0, 1.1, 1.0),), ProviderFailureCategory.INVALID_DATA),
        (
            ((ms("2026-01-01T00:00:00+00:00"), 1.1, 1.2),),
            ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
        ),
    ],
)
def test_invalid_source_rows_fail_closed(
    rows: tuple[tuple[object, ...], ...], category: ProviderFailureCategory
) -> None:
    item, _ = provider([DukascopyPage(rows, complete=True)])
    result = item.fetch_bars(query())
    assert isinstance(result, ProviderFailure)
    assert result.category is category


def test_non_identical_boundary_duplicate_is_invalid() -> None:
    first = row("2026-01-01T00:00:00+00:00")
    changed = row("2026-01-01T00:00:00+00:00", close=1.2)
    item, _ = provider(
        [DukascopyPage((first,), complete=False), DukascopyPage((changed,), complete=True)]
    )
    result = item.fetch_bars(query())
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.INVALID_DATA


def test_failed_page_discards_accumulated_rows_and_does_not_retry() -> None:
    transport = FakeTransport([DukascopyPage((row("2026-01-01T00:00:00+00:00"),), False)])

    def fail_second(**kwargs: object) -> DukascopyPage:
        transport.calls.append(dict(kwargs))
        if len(transport.calls) == 1:
            return transport.pages.pop(0)
        raise DukascopyTransportFailure(
            ProviderFailureCategory.TRANSIENT, "network_timeout", retryable=True
        )

    transport.fetch_page = fail_second  # type: ignore[method-assign]
    item = DukascopyHistoricalBarsProvider(transport)
    result = item.fetch_bars(query())
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.TRANSIENT
    assert result.retryable is True
    assert len(transport.calls) == 2


def test_non_advancing_page_and_page_limit_fail_closed() -> None:
    stalled, _ = provider([DukascopyPage((), complete=False)])
    stalled_result = stalled.fetch_bars(query())
    assert isinstance(stalled_result, ProviderFailure)
    assert stalled_result.category is ProviderFailureCategory.INCOMPATIBLE_SCHEMA

    limited, transport = provider(
        [DukascopyPage((row("2026-01-01T00:00:00+00:00"),), complete=False)],
        settings=DukascopyConnectorSettings(max_pages=1),
    )
    limited_result = limited.fetch_bars(query())
    assert isinstance(limited_result, ProviderFailure)
    assert limited_result.reason == "page_limit_exceeded"
    assert len(transport.calls) == 1


def test_empty_complete_response_is_no_data() -> None:
    item, _ = provider([DukascopyPage((), complete=True)])
    result = item.fetch_bars(query())
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.NO_DATA


class FakeResponse(BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_http_transport_uses_one_bounded_request_and_strict_jsonp() -> None:
    calls: list[tuple[object, float]] = []

    def opener(request: object, timeout: float) -> FakeResponse:
        calls.append((request, timeout))
        return FakeResponse(b"fxlab_callback([[1767225600000,1.1,1.2,1.0,1.1,12.0]]);")

    transport = DukascopyHttpTransport(opener=opener)
    page = transport.fetch_page(
        native_symbol="EUR/USD",
        native_timeframe="5MIN",
        cursor_ms=ms("2026-01-01T00:00:00+00:00"),
        end_ms=ms("2026-01-01T00:05:00+00:00"),
        page_size=10,
        timeout_seconds=2.5,
        max_response_bytes=1024,
    )
    assert len(calls) == 1
    assert calls[0][1] == 2.5
    assert page.complete is False


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (TimeoutError(), ProviderFailureCategory.TRANSIENT, True),
        (URLError("dns token=hidden"), ProviderFailureCategory.TRANSIENT, True),
        (
            HTTPError("https://secret.invalid/?token=x", 401, "bad", {}, None),
            ProviderFailureCategory.AUTHENTICATION,
            False,
        ),
        (
            HTTPError("https://secret.invalid/?token=x", 403, "bad", {}, None),
            ProviderFailureCategory.AUTHENTICATION,
            False,
        ),
        (
            HTTPError("https://secret.invalid/?token=x", 429, "bad", {}, None),
            ProviderFailureCategory.RATE_LIMIT,
            True,
        ),
        (
            HTTPError("https://secret.invalid/?token=x", 503, "bad", {}, None),
            ProviderFailureCategory.TRANSIENT,
            True,
        ),
    ],
)
def test_http_transport_sanitizes_network_failures(
    error: Exception, category: ProviderFailureCategory, retryable: bool
) -> None:
    def opener(_request: object, _timeout: float) -> FakeResponse:
        raise error

    transport = DukascopyHttpTransport(opener=opener)
    with pytest.raises(DukascopyTransportFailure) as caught:
        transport.fetch_page(
            native_symbol="EUR/USD",
            native_timeframe="5MIN",
            cursor_ms=1,
            end_ms=2,
            page_size=1,
            timeout_seconds=1.0,
            max_response_bytes=64,
        )
    assert caught.value.category is category
    assert caught.value.retryable is retryable
    assert "token" not in str(caught.value).lower()
    assert "https" not in str(caught.value).lower()


def test_http_transport_rejects_oversized_and_malformed_bodies() -> None:
    oversized = DukascopyHttpTransport(opener=lambda *_args: FakeResponse(b"x" * 20))
    with pytest.raises(DukascopyTransportFailure) as size_error:
        oversized.fetch_page(
            native_symbol="EUR/USD",
            native_timeframe="5MIN",
            cursor_ms=1,
            end_ms=2,
            page_size=1,
            timeout_seconds=1.0,
            max_response_bytes=8,
        )
    assert size_error.value.category is ProviderFailureCategory.INCOMPATIBLE_SCHEMA

    malformed = DukascopyHttpTransport(opener=lambda *_args: FakeResponse(b"not-jsonp"))
    with pytest.raises(DukascopyTransportFailure) as body_error:
        malformed.fetch_page(
            native_symbol="EUR/USD",
            native_timeframe="5MIN",
            cursor_ms=1,
            end_ms=2,
            page_size=1,
            timeout_seconds=1.0,
            max_response_bytes=64,
        )
    assert body_error.value.category is ProviderFailureCategory.INCOMPATIBLE_SCHEMA


def test_legacy_fetch_helper_delegates_to_safe_provider_path() -> None:
    transport = FakeTransport([DukascopyPage((row("2026-01-01T00:00:00+00:00"),), complete=True)])
    frame = fetch_dukascopy(
        "EURUSD",
        "M5",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:05:00+00:00",
        transport=transport,
    )
    assert len(frame) == 1
    assert transport.calls[0]["timeout_seconds"] == 10.0
    with pytest.raises(ValueError, match="BID"):
        fetch_dukascopy(
            "EURUSD",
            "M5",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:05:00+00:00",
            offer_side="ask",
            transport=transport,
        )
    with pytest.raises(ValueError, match="timezone"):
        fetch_dukascopy(
            "EURUSD",
            "M5",
            "2026-01-01T00:00:00",
            "2026-01-01T00:05:00+00:00",
            transport=transport,
        )
