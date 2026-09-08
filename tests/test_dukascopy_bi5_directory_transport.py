"""Offline contract tests for the Dukascopy .bi5 directory transport."""

from __future__ import annotations

import hashlib
import lzma
import struct
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from fxlab.data import (
    BarQuery,
    CanonicalInstrument,
    DukascopyBi5DirectoryTransport,
    DukascopyBi5HistoricalBarsProvider,
    DukascopyBi5HttpTransport,
    DukascopyTransportFailure,
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


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        url: str = "",
        content_type: str = "application/octet-stream",
    ) -> None:
        self.body = body
        self.status = status
        self.url = url
        self.headers = {"Content-Type": content_type, "Last-Modified": "source-revision"}
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size == -1:
            chunk = self.body[self._pos :]
            self._pos = len(self.body)
            return chunk
        chunk = self.body[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


def test_directory_transport_constructs_correct_relative_path() -> None:
    rel_path = bi5.dukascopy_bi5_relative_path("AUDUSD", START)
    assert rel_path == Path("AUDUSD/2021/00/05/00h_ticks.bi5")


def test_directory_transport_correct_zero_based_month_mapping() -> None:
    jan = bi5.dukascopy_bi5_relative_path("EURUSD", datetime(2021, 1, 15, 12, tzinfo=UTC))
    assert jan == Path("EURUSD/2021/00/15/12h_ticks.bi5")

    dec = bi5.dukascopy_bi5_relative_path("USDJPY", datetime(2023, 12, 31, 23, tzinfo=UTC))
    assert dec == Path("USDJPY/2023/11/31/23h_ticks.bi5")

    feb = bi5.dukascopy_bi5_relative_path("GBPUSD", datetime(2014, 2, 1, 0, tzinfo=UTC))
    assert feb == Path("GBPUSD/2014/01/01/00h_ticks.bi5")


def test_directory_transport_reads_exact_raw_bytes_and_computes_sha256(tmp_path: Path) -> None:
    payload = compressed((1_000, 110_005, 110_000, 1.5, 2.5))
    target = tmp_path / "AUDUSD" / "2021" / "00" / "05" / "00h_ticks.bi5"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    transport = DukascopyBi5DirectoryTransport(tmp_path)
    hour = transport.fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=30.0,
        max_response_bytes=1024 * 1024,
    )
    assert not hour.is_absent
    assert hour.body == payload
    assert hour.raw_sha256 == hashlib.sha256(payload).hexdigest()
    assert hour.hour_start == START


def test_directory_transport_missing_file_fails_closed_without_absent(tmp_path: Path) -> None:
    transport = DukascopyBi5DirectoryTransport(tmp_path)
    with pytest.raises(DukascopyTransportFailure) as exc_info:
        transport.fetch_hour(
            native_symbol="AUDUSD",
            hour_start=START,
            timeout_seconds=30.0,
            max_response_bytes=1024 * 1024,
        )
    assert exc_info.value.category is ProviderFailureCategory.NO_DATA
    assert exc_info.value.reason == "missing_local_bi5_file"

    provider = DukascopyBi5HistoricalBarsProvider(transport)
    result = provider.fetch_bars(query())
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.NO_DATA
    assert result.reason == "missing_local_bi5_file"


def test_directory_transport_present_corrupt_file_fails_closed_in_provider(tmp_path: Path) -> None:
    corrupt_bytes = b"corrupted_non_lzma_payload_bytes"
    target = tmp_path / "AUDUSD" / "2021" / "00" / "05" / "00h_ticks.bi5"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(corrupt_bytes)

    transport = DukascopyBi5DirectoryTransport(tmp_path)
    hour = transport.fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=30.0,
        max_response_bytes=1024 * 1024,
    )
    assert not hour.is_absent
    assert hour.body == corrupt_bytes

    provider = DukascopyBi5HistoricalBarsProvider(transport)
    result = provider.fetch_bars(query())
    assert isinstance(result, ProviderFailure)
    assert result.category is ProviderFailureCategory.INCOMPATIBLE_SCHEMA
    assert result.reason == "corrupt_bi5_lzma"


def test_directory_transport_oversized_file_fails_closed(tmp_path: Path) -> None:
    oversized_bytes = b"x" * 100
    target = tmp_path / "AUDUSD" / "2021" / "00" / "05" / "00h_ticks.bi5"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(oversized_bytes)

    transport = DukascopyBi5DirectoryTransport(tmp_path)
    with pytest.raises(DukascopyTransportFailure) as exc_info:
        transport.fetch_hour(
            native_symbol="AUDUSD",
            hour_start=START,
            timeout_seconds=30.0,
            max_response_bytes=50,
        )
    assert exc_info.value.category is ProviderFailureCategory.INCOMPATIBLE_SCHEMA
    assert exc_info.value.reason == "response_too_large"


def test_directory_transport_unsupported_symbol_and_out_of_window_fail(tmp_path: Path) -> None:
    transport = DukascopyBi5DirectoryTransport(tmp_path)

    with pytest.raises(DukascopyTransportFailure) as exc_unsupported:
        transport.fetch_hour(
            native_symbol="XAUUSD",
            hour_start=START,
            timeout_seconds=30.0,
            max_response_bytes=1024,
        )
    assert exc_unsupported.value.category is ProviderFailureCategory.UNSUPPORTED
    assert exc_unsupported.value.reason == "symbol_unsupported"

    with pytest.raises(DukascopyTransportFailure) as exc_window:
        transport.fetch_hour(
            native_symbol="AUDUSD",
            hour_start=datetime(2024, 1, 1, tzinfo=UTC),
            timeout_seconds=30.0,
            max_response_bytes=1024,
        )
    assert exc_window.value.category is ProviderFailureCategory.CONFIGURATION
    assert exc_window.value.reason == "research_window_violation"


def test_directory_transport_target_not_a_file_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "AUDUSD" / "2021" / "00" / "05" / "00h_ticks.bi5"
    target.mkdir(parents=True, exist_ok=True)

    transport = DukascopyBi5DirectoryTransport(tmp_path)
    with pytest.raises(DukascopyTransportFailure) as exc_info:
        transport.fetch_hour(
            native_symbol="AUDUSD",
            hour_start=START,
            timeout_seconds=30.0,
            max_response_bytes=1024,
        )
    assert exc_info.value.category is ProviderFailureCategory.INVALID_DATA
    assert exc_info.value.reason == "bi5_target_not_a_file"


def test_directory_transport_root_validation(tmp_path: Path) -> None:
    valid_str = DukascopyBi5DirectoryTransport(str(tmp_path))
    assert valid_str.root == tmp_path.resolve()

    with pytest.raises(ValueError, match="root must be a pathlib.Path or non-empty str"):
        DukascopyBi5DirectoryTransport(None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="root must be a non-empty path"):
        DukascopyBi5DirectoryTransport("   ")


def test_http_transport_404_semantics_remain_unchanged() -> None:
    url = bi5.dukascopy_bi5_url("AUDUSD", START)
    transport = bi5.DukascopyBi5HttpTransport(
        opener=lambda *_args, **_kwargs: FakeResponse(b"", status=404, url=url)
    )
    hour = transport.fetch_hour(
        native_symbol="AUDUSD",
        hour_start=START,
        timeout_seconds=30.0,
        max_response_bytes=1024,
    )
    assert hour.is_absent is True
    assert hour.body == b""
    assert hour.hour_start == START


def test_directory_transport_and_http_transport_produce_identical_provenance_parity(
    tmp_path: Path,
) -> None:
    hour0_payload = compressed(
        (1_000, 110_005, 110_000, 1.0, 2.0),
        (2_000, 120_005, 120_000, 1.0, 3.0),
    )
    hour1_payload = compressed(
        (1_000, 105_005, 105_000, 1.0, 4.0),
        (2_000, 115_005, 115_000, 1.0, 5.0),
    )
    empty_hour_payload = compressed()

    # 1. Setup all 24 hour files on disk for directory transport
    payloads: dict[int, bytes] = {0: hour0_payload, 1: hour1_payload}
    for h in range(24):
        p = payloads.get(h, empty_hour_payload)
        f = tmp_path / "AUDUSD" / "2021" / "00" / "05" / f"{h:02d}h_ticks.bi5"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(p)

    # 2. Setup mock opener for HTTP transport serving identical payloads
    urls = {
        bi5.dukascopy_bi5_url("AUDUSD", START.replace(hour=h)): payloads.get(h, empty_hour_payload)
        for h in range(24)
    }

    def fake_opener(request: object, **_kwargs: object) -> FakeResponse:
        req_url = getattr(request, "full_url", getattr(request, "url", str(request)))
        if req_url in urls:
            return FakeResponse(urls[req_url], url=req_url)
        return FakeResponse(b"", status=404, url=req_url)

    def fixed_clock() -> datetime:
        return datetime(2021, 1, 7, 12, 0, 0, tzinfo=UTC)

    dir_provider = DukascopyBi5HistoricalBarsProvider(
        DukascopyBi5DirectoryTransport(tmp_path), clock=fixed_clock
    )
    http_provider = DukascopyBi5HistoricalBarsProvider(
        DukascopyBi5HttpTransport(opener=fake_opener), clock=fixed_clock
    )

    dir_result = dir_provider.fetch_bars(query())
    http_result = http_provider.fetch_bars(query())

    assert not isinstance(dir_result, ProviderFailure)
    assert not isinstance(http_result, ProviderFailure)

    pd.testing.assert_frame_equal(dir_result.frame, http_result.frame)
    assert dir_result.provenance.content_hash == http_result.provenance.content_hash
    assert dir_result.provenance.dataset_id == http_result.provenance.dataset_id
    assert dir_result.provenance.volume_semantics == http_result.provenance.volume_semantics
    assert dir_result.provenance.source_timezone == http_result.provenance.source_timezone
    assert (
        dir_result.provenance.sanitized_source_reference
        == http_result.provenance.sanitized_source_reference
    )
    assert dir_result.provenance.query_fingerprint == http_result.provenance.query_fingerprint


def test_fetch_dukascopy_bi5_with_directory_transport(tmp_path: Path) -> None:
    hour0_payload = compressed(
        (1_000, 110_005, 110_000, 1.0, 2.0),
        (2_000, 120_005, 120_000, 1.0, 3.0),
    )
    empty_payload = compressed()
    for h in range(24):
        p = hour0_payload if h == 0 else empty_payload
        f = tmp_path / "AUDUSD" / "2021" / "00" / "05" / f"{h:02d}h_ticks.bi5"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(p)

    frame = ingest_module.fetch_dukascopy_bi5(
        "AUDUSD",
        "D1",
        "2021-01-05T00:00:00+00:00",
        "2021-01-06T00:00:00+00:00",
        transport=DukascopyBi5DirectoryTransport(tmp_path),
    )
    assert len(frame) == 1
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
