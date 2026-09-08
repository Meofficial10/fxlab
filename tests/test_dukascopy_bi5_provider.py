"""Offline contract tests for the bounded Dukascopy hourly .bi5 provider."""

from __future__ import annotations

import hashlib
import lzma
import struct
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import URLError

import pytest

from fxlab.data import (
    BarQuery,
    CanonicalInstrument,
    ProviderFailure,
    ProviderFailureCategory,
)
from fxlab.data import (
    dukascopy_provider as bi5,
)
from fxlab.data import (
    ingest_dukascopy as ingest_module,
)

RECORD = struct.Struct(">IIIff")
START = datetime(2021, 1, 5, tzinfo=UTC)
END = datetime(2021, 1, 6, tzinfo=UTC)


def compressed(*records: tuple[int, int, int, float, float]) -> bytes:
    raw = b"".join(RECORD.pack(*record) for record in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def query(
    *,
    symbol: str = "AUDUSD",
    start: datetime = START,
    end: datetime = END,
) -> BarQuery:
    return BarQuery(CanonicalInstrument(symbol), "D1", start, end, end)


class FakeBi5Transport:
    def __init__(self, hours: dict[datetime, object]) -> None:
        self.hours = hours
        self.calls: list[dict[str, object]] = []

    def fetch_hour(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        return self.hours[kwargs["hour_start"]]  # type: ignore[index]


class FakeResponse(BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        url: str = "",
        content_type: str = "application/octet-stream",
    ) -> None:
        super().__init__(body)
        self.status = status
        self.url = url
        self.headers = {"Content-Type": content_type, "Last-Modified": "source-revision"}
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_bi5_decoder_reconstructs_hourly_ticks_and_pair_scaling() -> None:
    payload = compressed((1_000, 110_005, 110_000, 1.5, 2.5))
    aud = bi5.decode_dukascopy_bi5_hour(payload, START, "AUDUSD")
    jpy = bi5.decode_dukascopy_bi5_hour(payload, START, "USDJPY")

    assert aud == (
        bi5.DukascopyBi5Tick(
            datetime(2021, 1, 5, 0, 0, 1, tzinfo=UTC), 1.10005, 1.1, 1.5, 2.5
        ),
    )
    assert jpy[0].ask == pytest.approx(110.005)
    assert jpy[0].bid == pytest.approx(110.0)
    assert bi5.DUKASCOPY_BI5_PRICE_DIVISORS == {
        "AUDUSD": 100_000,
        "EURUSD": 100_000,
        "GBPUSD": 100_000,
        "NZDUSD": 100_000,
        "USDCAD": 100_000,
        "USDCHF": 100_000,
        "USDJPY": 1_000,
    }


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"not-lzma", "corrupt_bi5_lzma"),
        (lzma.compress(b"short", format=lzma.FORMAT_ALONE), "malformed_bi5_record_length"),
    ],
)
def test_bi5_decoder_rejects_corrupt_and_truncated_payloads(
    payload: bytes, reason: str
) -> None:
    with pytest.raises(bi5.DukascopyTransportFailure, match=reason):
        bi5.decode_dukascopy_bi5_hour(payload, START, "AUDUSD")


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        (((3_600_000, 110_005, 110_000, 1.0, 1.0),), "tick_offset_outside_hour"),
        (
            (
                (2_000, 110_005, 110_000, 1.0, 1.0),
                (1_000, 110_005, 110_000, 1.0, 1.0),
            ),
            "ticks_out_of_order",
        ),
        (
            (
                (1_000, 110_005, 110_000, 1.0, 1.0),
                (1_000, 110_005, 110_000, 1.0, 1.0),
            ),
            "duplicate_tick_timestamp",
        ),
        (((1_000, 0, 110_000, 1.0, 1.0),), "tick_values_invalid"),
        (((1_000, 109_995, 110_000, 1.0, 1.0),), "tick_values_invalid"),
        (((1_000, 110_005, 110_000, float("nan"), 1.0),), "tick_values_invalid"),
    ],
)
def test_bi5_decoder_fails_closed_on_tick_integrity(
    records: tuple[tuple[int, int, int, float, float], ...], reason: str
) -> None:
    with pytest.raises(bi5.DukascopyTransportFailure, match=reason):
        bi5.decode_dukascopy_bi5_hour(compressed(*records), START, "AUDUSD")


def test_bi5_provider_aggregates_bid_ticks_to_closed_utc_d1_without_fill() -> None:
    hours = {
        START.replace(hour=0): bi5.DukascopyBi5Hour(
            START.replace(hour=0),
            compressed(
                (1_000, 110_005, 110_000, 1.0, 2.0),
                (2_000, 120_005, 120_000, 1.0, 3.0),
            ),
            "rev-0",
        ),
        START.replace(hour=1): bi5.DukascopyBi5Hour(
            START.replace(hour=1),
            compressed(
                (1_000, 105_005, 105_000, 1.0, 4.0),
                (2_000, 115_005, 115_000, 1.0, 5.0),
            ),
            "rev-1",
        ),
    }
    for hour in range(2, 24):
        hour_start = START.replace(hour=hour)
        hours[hour_start] = bi5.DukascopyBi5Hour.absent(hour_start)

    provider = bi5.DukascopyBi5HistoricalBarsProvider(
        FakeBi5Transport(hours), clock=lambda: datetime(2021, 1, 7, tzinfo=UTC)
    )
    result = provider.fetch_bars(query())

    assert not isinstance(result, ProviderFailure)
    assert [item.to_pydatetime() for item in result.frame.index] == [START]
    assert result.frame.iloc[0].to_dict() == pytest.approx(
        {"open": 1.1, "high": 1.2, "low": 1.05, "close": 1.15, "volume": 14.0}
    )
    assert result.provenance.volume_semantics == "sum_bid_tick_volume_millions_base"
    assert result.provenance.sanitized_source_reference == "dukascopy:datafeed:bi5:hourly:bid"
    assert result.provenance.revision.startswith("bi5_hour_set_sha256:")


def test_bi5_provider_does_not_fabricate_an_empty_day() -> None:
    hours = {
        START.replace(hour=hour): bi5.DukascopyBi5Hour.absent(START.replace(hour=hour))
        for hour in range(24)
    }
    result = bi5.DukascopyBi5HistoricalBarsProvider(FakeBi5Transport(hours)).fetch_bars(query())
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.NO_DATA
    assert result.reason == "no_ticks"


def test_bi5_research_query_and_url_are_strictly_sealed() -> None:
    assert (
        bi5.dukascopy_bi5_url("AUDUSD", START)
        == "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    )
    assert bi5.dukascopy_bi5_url(
        "AUDUSD", datetime(2023, 12, 31, 23, tzinfo=UTC)
    ).endswith("/2023/11/31/23h_ticks.bi5")
    for invalid in (
        datetime(2013, 12, 31, 23, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
    ):
        with pytest.raises(ValueError, match="research_window_violation"):
            bi5.dukascopy_bi5_url("AUDUSD", invalid)
    with pytest.raises(ValueError, match="symbol_unsupported"):
        bi5.dukascopy_bi5_url("XAUUSD", START)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"timeout_seconds": 30.1},
        {"max_response_bytes": 8 * 1024 * 1024 + 1},
    ),
)
def test_bi5_settings_have_hard_transport_ceilings(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        bi5.DukascopyBi5Settings(**kwargs)


def test_bi5_provider_rejects_queries_that_could_generate_unsealed_urls() -> None:
    transport = FakeBi5Transport({})
    provider = bi5.DukascopyBi5HistoricalBarsProvider(transport)
    for invalid in (
        query(start=datetime(2013, 12, 31, tzinfo=UTC), end=END),
        query(start=START, end=datetime(2024, 1, 1, 1, tzinfo=UTC)),
    ):
        result = provider.fetch_bars(invalid)
        assert isinstance(result, ProviderFailure)
        assert result.reason == "research_window_violation"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("status", "category", "reason", "retryable"),
    [
        (403, ProviderFailureCategory.CONFIGURATION, "access_forbidden", False),
        (429, ProviderFailureCategory.RATE_LIMIT, "rate_limited", True),
        (503, ProviderFailureCategory.TRANSIENT, "provider_unavailable", True),
    ],
)
def test_bi5_http_statuses_are_explicitly_classified(
    status: int, category: ProviderFailureCategory, reason: str, retryable: bool
) -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    transport = bi5.DukascopyBi5HttpTransport(
        opener=lambda *_args, **_kwargs: FakeResponse(b"", status=status, url=url)
    )
    with pytest.raises(bi5.DukascopyTransportFailure) as caught:
        transport.fetch_hour(
            native_symbol="AUDUSD",
            hour_start=START,
            timeout_seconds=2.0,
            max_response_bytes=1024,
        )
    assert caught.value.category is category
    assert caught.value.reason == reason
    assert caught.value.retryable is retryable


def test_bi5_http_404_is_an_explicit_absent_hour_and_body_is_bounded() -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    calls: list[tuple[object, bytes | None, float | None]] = []
    response = FakeResponse(b"ignored", status=404, url=url)

    def opener(
        request: object, data: bytes | None = None, timeout: float | None = None
    ) -> FakeResponse:
        calls.append((request, data, timeout))
        return response

    result = bi5.DukascopyBi5HttpTransport(opener=opener).fetch_hour(
        native_symbol="AUDUSD", hour_start=START, timeout_seconds=2.5, max_response_bytes=8
    )
    assert result.is_absent is True
    assert len(calls) == 1 and calls[0][1:] == (None, 2.5)
    assert response.read_sizes == []


def test_bi5_http_200_empty_is_a_distinct_explicit_empty_partition() -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    response = FakeResponse(b"", status=200, url=url)

    result = bi5.DukascopyBi5HttpTransport(
        opener=lambda *_args, **_kwargs: response
    ).fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=2.0,
        max_response_bytes=1024,
    )

    assert result.is_absent is True
    assert result.body == b""
    assert result.absence_evidence_type == "http_200_empty_body"
    assert response.read_sizes == [1025]


def test_bi5_http_success_is_exact_url_media_type_and_size_bounded() -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    body = compressed((1_000, 110_005, 110_000, 1.0, 1.0))
    response = FakeResponse(body, url=url)
    result = bi5.DukascopyBi5HttpTransport(
        opener=lambda *_args, **_kwargs: response
    ).fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=2.0,
        max_response_bytes=len(body),
    )
    assert result.body == body
    assert result.raw_sha256 == hashlib.sha256(body).hexdigest()
    assert response.read_sizes == [len(body) + 1]

    for changed in (
        FakeResponse(body, url=url + "?redirected"),
        FakeResponse(body, url=url, content_type="text/html"),
        FakeResponse(body + b"x", url=url),
    ):
        with pytest.raises(bi5.DukascopyTransportFailure):
            bi5.DukascopyBi5HttpTransport(
                opener=lambda *_args, _changed=changed, **_kwargs: _changed
            ).fetch_hour(
                native_symbol="AUDUSD",
                hour_start=START,
                timeout_seconds=2.0,
                max_response_bytes=len(body),
            )


def test_bi5_content_and_source_hashes_are_deterministic() -> None:
    payload = compressed((1_000, 110_005, 110_000, 1.0, 2.0))
    hours = {
        START.replace(hour=0): bi5.DukascopyBi5Hour(START, payload, "rev"),
        **{
            START.replace(hour=hour): bi5.DukascopyBi5Hour.absent(START.replace(hour=hour))
            for hour in range(1, 24)
        },
    }
    one = bi5.DukascopyBi5HistoricalBarsProvider(
        FakeBi5Transport(hours), clock=lambda: datetime(2021, 1, 7, tzinfo=UTC)
    ).fetch_bars(query())
    two = bi5.DukascopyBi5HistoricalBarsProvider(
        FakeBi5Transport(hours), clock=lambda: datetime(2021, 1, 8, tzinfo=UTC)
    ).fetch_bars(query())
    assert not isinstance(one, ProviderFailure) and not isinstance(two, ProviderFailure)
    assert one.provenance.content_hash == two.provenance.content_hash
    assert one.provenance.dataset_id == two.provenance.dataset_id
    assert one.provenance.revision == two.provenance.revision
    assert one.provenance.retrieved_at != two.provenance.retrieved_at


def test_bi5_source_identity_distinguishes_404_from_http_200_empty_evidence() -> None:
    payload = compressed((1_000, 110_005, 110_000, 1.0, 2.0))
    legacy_hours = {
        START.replace(hour=0): bi5.DukascopyBi5Hour(START, payload, "rev"),
        **{
            START.replace(hour=hour): bi5.DukascopyBi5Hour.absent(
                START.replace(hour=hour)
            )
            for hour in range(1, 24)
        },
    }
    empty_200_hours = dict(legacy_hours)
    empty_200_hours[START.replace(hour=1)] = bi5.DukascopyBi5Hour.absent(
        START.replace(hour=1), evidence_type="http_200_empty_body"
    )
    def fixed_clock() -> datetime:
        return datetime(2021, 1, 7, tzinfo=UTC)

    legacy = bi5.DukascopyBi5HistoricalBarsProvider(
        FakeBi5Transport(legacy_hours), clock=fixed_clock
    ).fetch_bars(query())
    empty_200 = bi5.DukascopyBi5HistoricalBarsProvider(
        FakeBi5Transport(empty_200_hours), clock=fixed_clock
    ).fetch_bars(query())

    assert not isinstance(legacy, ProviderFailure)
    assert not isinstance(empty_200, ProviderFailure)
    assert legacy.provenance.content_hash == empty_200.provenance.content_hash
    assert legacy.provenance.dataset_id == empty_200.provenance.dataset_id
    assert legacy.provenance.revision != empty_200.provenance.revision


def test_bi5_ingest_entry_point_uses_explicit_d1_transport_without_network() -> None:
    payload = compressed((1_000, 110_005, 110_000, 1.0, 2.0))
    hours = {
        START.replace(hour=0): bi5.DukascopyBi5Hour(START, payload, "rev"),
        **{
            START.replace(hour=hour): bi5.DukascopyBi5Hour.absent(START.replace(hour=hour))
            for hour in range(1, 24)
        },
    }
    transport = FakeBi5Transport(hours)
    frame = ingest_module.fetch_dukascopy_bi5(
        "AUDUSD",
        "D1",
        "2021-01-05T00:00:00+00:00",
        "2021-01-06T00:00:00+00:00",
        transport=transport,
    )
    assert len(frame) == 1
    assert len(transport.calls) == 24
    assert all(call["timeout_seconds"] == 30.0 for call in transport.calls)


def test_bi5_default_timeout_is_thirty_seconds_and_bounded() -> None:
    settings = bi5.DukascopyBi5Settings()
    assert settings.timeout_seconds == 30.0
    assert bi5._BI5_MAX_TIMEOUT_SECONDS == 30.0
    with pytest.raises(ValueError, match="fixed transport ceiling"):
        bi5.DukascopyBi5Settings(timeout_seconds=30.001)


def test_bi5_retry_timeout_then_success_uses_fixed_first_backoff() -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    body = compressed((1_000, 110_005, 110_000, 1.0, 1.0))
    outcomes: list[object] = [
        URLError(TimeoutError("handshake timed out")),
        FakeResponse(body, url=url),
    ]
    calls: list[object] = []
    delays: list[float] = []

    def opener(request: object, **_kwargs: object) -> object:
        calls.append(request)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    result = bi5.DukascopyBi5HttpTransport(
        opener=opener, sleeper=delays.append
    ).fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=30.0,
        max_response_bytes=1024,
    )

    assert result.body == body
    assert len(calls) == 2
    assert delays == [1.0]


def test_bi5_retry_5xx_then_success_uses_fixed_first_backoff() -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    body = compressed((1_000, 110_005, 110_000, 1.0, 1.0))
    outcomes = [FakeResponse(b"", status=503, url=url), FakeResponse(body, url=url)]
    calls: list[object] = []
    delays: list[float] = []

    def opener(request: object, **_kwargs: object) -> object:
        calls.append(request)
        return outcomes.pop(0)

    result = bi5.DukascopyBi5HttpTransport(
        opener=opener, sleeper=delays.append
    ).fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=30.0,
        max_response_bytes=1024,
    )

    assert result.body == body
    assert len(calls) == 2
    assert delays == [1.0]


def test_bi5_retry_429_then_success_uses_fixed_first_backoff() -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    body = compressed((1_000, 110_005, 110_000, 1.0, 1.0))
    outcomes = [FakeResponse(b"", status=429, url=url), FakeResponse(body, url=url)]
    calls: list[object] = []
    delays: list[float] = []

    def opener(request: object, **_kwargs: object) -> object:
        calls.append(request)
        return outcomes.pop(0)

    result = bi5.DukascopyBi5HttpTransport(
        opener=opener, sleeper=delays.append
    ).fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=30.0,
        max_response_bytes=1024,
    )

    assert result.body == body
    assert len(calls) == 2
    assert delays == [1.0]


def test_bi5_retry_is_capped_at_initial_attempt_plus_two_retries() -> None:
    calls: list[object] = []
    delays: list[float] = []

    def opener(request: object, **_kwargs: object) -> object:
        calls.append(request)
        raise URLError(TimeoutError("handshake timed out"))

    transport = bi5.DukascopyBi5HttpTransport(
        opener=opener, sleeper=delays.append
    )
    with pytest.raises(bi5.DukascopyTransportFailure, match="network_unavailable"):
        transport.fetch_hour(
            native_symbol="AUDUSD",
            hour_start=START,
            timeout_seconds=30.0,
            max_response_bytes=1024,
        )

    assert len(calls) == 3
    assert delays == [1.0, 2.0]


@pytest.mark.parametrize(
    "response",
    (
        FakeResponse(b"", status=400, url=bi5.dukascopy_bi5_url("AUDUSD", START)),
        FakeResponse(b"", status=401, url=bi5.dukascopy_bi5_url("AUDUSD", START)),
        FakeResponse(b"", status=403, url=bi5.dukascopy_bi5_url("AUDUSD", START)),
        FakeResponse(b"", status=302, url=bi5.dukascopy_bi5_url("AUDUSD", START)),
        FakeResponse(
            b"payload",
            url=bi5.dukascopy_bi5_url("AUDUSD", START),
            content_type="text/html",
        ),
        FakeResponse(b"x" * 9, url=bi5.dukascopy_bi5_url("AUDUSD", START)),
    ),
)
def test_bi5_retry_never_retries_permanent_or_incompatible_responses(
    response: FakeResponse,
) -> None:
    calls: list[object] = []
    delays: list[float] = []

    def opener(request: object, **_kwargs: object) -> object:
        calls.append(request)
        return response

    transport = bi5.DukascopyBi5HttpTransport(
        opener=opener, sleeper=delays.append
    )
    with pytest.raises(bi5.DukascopyTransportFailure):
        transport.fetch_hour(
            native_symbol="AUDUSD",
            hour_start=START,
            timeout_seconds=30.0,
            max_response_bytes=8,
        )

    assert len(calls) == 1
    assert delays == []


def test_bi5_retry_does_not_retry_explicit_absent_hour() -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    calls: list[object] = []
    delays: list[float] = []

    def opener(request: object, **_kwargs: object) -> object:
        calls.append(request)
        return FakeResponse(b"", status=404, url=url)

    result = bi5.DukascopyBi5HttpTransport(
        opener=opener, sleeper=delays.append
    ).fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=30.0,
        max_response_bytes=1024,
    )

    assert result.is_absent is True
    assert len(calls) == 1
    assert delays == []
