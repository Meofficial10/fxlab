"""Offline contracts for the prospective Dukascopy direct BID D1 provider."""

from __future__ import annotations

import hashlib
import json
import lzma
import math
import struct
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pandas as pd
import pytest

from fxlab.data import BarQuery, CanonicalInstrument, ProviderFailure
from fxlab.data import dukascopy_direct_d1 as direct
from fxlab.data import dukascopy_direct_d1_mirror as mirror

RECORD = struct.Struct(">IIIIIf")


def _year_body(
    year: int,
    *,
    divisor: int = 100_000,
    mutate: dict[int, tuple[int, int, int, int, float]] | None = None,
) -> bytes:
    count = 366 if pd.Timestamp(year=year, month=12, day=31).is_leap_year else 365
    records = []
    for day in range(count):
        values = (110_000, 110_100, 109_900, 110_200, 10.5)
        if mutate and day in mutate:
            values = mutate[day]
        records.append(RECORD.pack(day * 86_400, *values))
    return lzma.compress(b"".join(records), format=lzma.FORMAT_ALONE)


class FakeResponse(BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        status: int = 200,
        content_type: str = "application/octet-stream",
    ) -> None:
        super().__init__(body)
        self.status = status
        self.url = url
        self.headers = {"Content-Type": content_type, "ETag": "source-etag"}
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


def _publish(tmp_path: Path, pair: str, year: int, body: bytes) -> tuple[Path, Path]:
    url = direct.dukascopy_direct_d1_url(pair, year)
    return mirror.mirror_direct_d1_year(
        pair=pair,
        year=year,
        destination_root=tmp_path,
        opener=lambda *_args, **_kwargs: FakeResponse(body, url=url),
        clock=lambda: datetime(2023, 12, 31, tzinfo=UTC),
    )


def test_decoder_reorders_ohlc_scales_prices_and_preserves_float32_volume() -> None:
    body = _year_body(
        2021,
        mutate={0: (77_027, 77_109, 76_896, 77_139, 5762.17)},
    )
    frame = direct.decode_dukascopy_direct_d1_year(body, "AUDUSD", 2021)

    first = frame.iloc[0]
    assert tuple(first.index) == ("open", "high", "low", "close", "volume")
    assert first.open == 0.77027
    assert first.high == 0.77139
    assert first.low == 0.76896
    assert first.close == 0.77109
    expected_volume = float(struct.unpack(">f", struct.pack(">f", 5762.17))[0])
    assert first.volume == expected_volume
    assert all(dtype.name == "float64" for dtype in frame.dtypes)


def test_decoder_uses_jpy_divisor_and_preserves_zero_volume_flat_rows() -> None:
    body = _year_body(2021, mutate={0: (110_005, 110_005, 110_005, 110_005, 0.0)})
    frame = direct.decode_dukascopy_direct_d1_year(body, "USDJPY", 2021)
    assert frame.iloc[0].to_dict() == {
        "open": 110.005,
        "high": 110.005,
        "low": 110.005,
        "close": 110.005,
        "volume": 0.0,
    }
    assert len(frame) == 365


@pytest.mark.parametrize("year,expected", [(2021, 365), (2020, 366)])
def test_decoder_requires_complete_year_and_exact_utc_daily_calendar(
    year: int, expected: int
) -> None:
    frame = direct.decode_dukascopy_direct_d1_year(_year_body(year), "AUDUSD", year)
    assert len(frame) == expected
    assert frame.index[0] == pd.Timestamp(year=year, month=1, day=1, tz="UTC")
    assert frame.index[-1] == pd.Timestamp(year=year, month=12, day=31, tz="UTC")
    assert frame.index.is_unique and frame.index.is_monotonic_increasing
    assert (frame.index.to_series().diff().dropna() == pd.Timedelta(days=1)).all()


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b"not-lzma", "direct_d1_lzma_invalid"),
        (lzma.compress(b"short", format=lzma.FORMAT_ALONE), "direct_d1_record_length_invalid"),
    ],
)
def test_decoder_rejects_corrupt_and_truncated_payload(raw: bytes, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        direct.decode_dukascopy_direct_d1_year(raw, "AUDUSD", 2021)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({0: (110_000, 110_100, 109_900, 110_200, math.nan)}, "volume_invalid"),
        ({0: (110_000, 110_100, 109_900, 110_200, -1.0)}, "volume_invalid"),
        ({0: (110_000, 110_100, 110_150, 110_200, 1.0)}, "ohlc_invalid"),
        ({0: (0, 0, 0, 0, 1.0)}, "price_invalid"),
    ],
)
def test_decoder_rejects_invalid_values(
    mutation: dict[int, tuple[int, int, int, int, float]], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        direct.decode_dukascopy_direct_d1_year(
            _year_body(2021, mutate=mutation), "AUDUSD", 2021
        )


def test_decoder_rejects_invalid_offsets_order_duplicates_and_record_count() -> None:
    count = 365
    records = [
        RECORD.pack(day * 86_400, 110_000, 110_100, 109_900, 110_200, 1.0)
        for day in range(count)
    ]
    for index, offset, reason in (
        (0, 1, "timestamp_not_midnight"),
        (1, 0, "timestamp_order_invalid"),
        (1, 2 * 86_400, "timestamp_order_invalid"),
    ):
        changed = list(records)
        changed[index] = RECORD.pack(offset, 110_000, 110_100, 109_900, 110_200, 1.0)
        with pytest.raises(ValueError, match=reason):
            direct.decode_dukascopy_direct_d1_year(
                lzma.compress(b"".join(changed), format=lzma.FORMAT_ALONE), "AUDUSD", 2021
            )
    with pytest.raises(ValueError, match="year_record_count_invalid"):
        direct.decode_dukascopy_direct_d1_year(
            lzma.compress(b"".join(records[:-1]), format=lzma.FORMAT_ALONE), "AUDUSD", 2021
        )
    shifted = [
        RECORD.pack((day + (day > 0)) * 86_400, 110_000, 110_100, 109_900, 110_200, 1.0)
        for day in range(count)
    ]
    with pytest.raises(ValueError, match="timestamp_calendar_invalid"):
        direct.decode_dukascopy_direct_d1_year(
            lzma.compress(b"".join(shifted), format=lzma.FORMAT_ALONE), "AUDUSD", 2021
        )


@pytest.mark.parametrize("pair", ["XAUUSD", "audusd", "../AUDUSD"])
def test_direct_path_rejects_unsupported_pairs(pair: str) -> None:
    with pytest.raises(ValueError, match="symbol_unsupported"):
        direct.dukascopy_direct_d1_url(pair, 2021)


@pytest.mark.parametrize("year", [2013, 2024])
def test_direct_path_rejects_unsealed_year_before_url_construction(year: int) -> None:
    with pytest.raises(ValueError, match="research_window_violation"):
        direct.dukascopy_direct_d1_url("AUDUSD", year)


def test_direct_path_is_exact_reviewed_https_yearly_family() -> None:
    assert direct.dukascopy_direct_d1_url("AUDUSD", 2021) == (
        "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/"
        "BID_candles_day_1.bi5"
    )


def test_http_transport_uses_exact_reviewed_url_and_bounded_read() -> None:
    body = _year_body(2021)
    url = direct.dukascopy_direct_d1_url("AUDUSD", 2021)
    calls: list[object] = []
    response = FakeResponse(body, url=url)

    def opener(request: object, **kwargs: object) -> FakeResponse:
        calls.append((request, kwargs))
        return response

    result = direct.DukascopyDirectD1HttpTransport(opener=opener).fetch_year(
        pair="AUDUSD", year=2021, timeout_seconds=30, max_response_bytes=8 * 1024 * 1024
    )
    request, kwargs = calls[0]  # type: ignore[misc]
    assert len(calls) == 1
    assert request.full_url == url  # type: ignore[attr-defined]
    assert request.get_method() == "GET"  # type: ignore[attr-defined]
    assert request.get_header("Accept") == "application/octet-stream"  # type: ignore[attr-defined]
    assert kwargs == {"timeout": 30}
    assert response.read_sizes == [8 * 1024 * 1024 + 1]
    assert result.body == body


def test_http_transport_rejects_redirect_media_type_and_oversize() -> None:
    url = direct.dukascopy_direct_d1_url("AUDUSD", 2021)
    cases = [
        (FakeResponse(b"x", url="https://example.com/elsewhere"), "unexpected_response_url"),
        (FakeResponse(b"x", url=url, content_type="text/html"), "unexpected_media_type"),
        (FakeResponse(b"123", url=url), "response_too_large"),
    ]
    for response, reason in cases:
        transport = direct.DukascopyDirectD1HttpTransport(
            opener=lambda *_args, _response=response, **_kwargs: _response
        )
        with pytest.raises(direct.DukascopyDirectD1TransportError, match=reason):
            transport.fetch_year(
                pair="AUDUSD", year=2021, timeout_seconds=30, max_response_bytes=2
            )


def test_http_transport_retries_transient_503_and_recovers() -> None:
    body = _year_body(2021)
    url = direct.dukascopy_direct_d1_url("AUDUSD", 2021)
    calls = 0
    sleeps: list[float] = []

    def opener(_request: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPError(url, 503, "Service Unavailable", {}, None)
        return FakeResponse(body, url=url)

    result = direct.DukascopyDirectD1HttpTransport(
        opener=opener, sleeper=sleeps.append
    ).fetch_year(pair="AUDUSD", year=2021)

    assert calls == 2
    assert sleeps == [1.0]
    assert result.body == body


@pytest.mark.parametrize(
    "status,reason",
    [
        (429, "provider_rate_limited"),
        (503, "provider_unavailable"),
    ],
)
def test_http_transport_retries_transient_status_until_exhausted(
    status: int, reason: str
) -> None:
    url = direct.dukascopy_direct_d1_url("AUDUSD", 2021)
    calls = 0
    sleeps: list[float] = []

    def opener(_request: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise HTTPError(url, status, "failure", {}, None)

    with pytest.raises(direct.DukascopyDirectD1TransportError, match=reason) as caught:
        direct.DukascopyDirectD1HttpTransport(
            opener=opener, sleeper=sleeps.append
        ).fetch_year(pair="AUDUSD", year=2021)

    assert calls == 3
    assert sleeps == [1.0, 2.0]
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    "status,reason",
    [
        (302, "redirect_not_allowed"),
        (403, "http_request_rejected"),
        (404, "year_not_found"),
    ],
)
def test_http_transport_does_not_retry_non_retryable_status(
    status: int, reason: str
) -> None:
    url = direct.dukascopy_direct_d1_url("AUDUSD", 2021)
    calls = 0
    sleeps: list[float] = []

    def opener(_request: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise HTTPError(url, status, "failure", {}, None)

    with pytest.raises(direct.DukascopyDirectD1TransportError, match=reason) as caught:
        direct.DukascopyDirectD1HttpTransport(
            opener=opener, sleeper=sleeps.append
        ).fetch_year(pair="AUDUSD", year=2021)

    assert calls == 1
    assert sleeps == []
    assert caught.value.retryable is False


def test_mirror_retries_transient_503_and_publishes_atomically(tmp_path: Path) -> None:
    body = _year_body(2021)
    url = direct.dukascopy_direct_d1_url("AUDUSD", 2021)
    calls = 0
    sleeps: list[float] = []

    def opener(_request: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPError(url, 503, "Service Unavailable", {}, None)
        return FakeResponse(body, url=url)

    raw_path, sidecar_path = mirror.mirror_direct_d1_year(
        pair="AUDUSD",
        year=2021,
        destination_root=tmp_path,
        opener=opener,
        sleeper=sleeps.append,
        clock=lambda: datetime(2023, 12, 31, tzinfo=UTC),
    )

    assert calls == 2
    assert sleeps == [1.0]
    assert raw_path.read_bytes() == body
    assert sidecar_path.exists()
    assert not list((tmp_path / "AUDUSD").glob(".tmp-*"))


def test_mirror_atomically_publishes_and_resumes_without_network(tmp_path: Path) -> None:
    body = _year_body(2021)
    raw_path, sidecar_path = _publish(tmp_path, "AUDUSD", 2021, body)
    assert raw_path.read_bytes() == body
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["raw_sha256"] == hashlib.sha256(body).hexdigest()
    assert sidecar["decoder_version"] == direct.DIRECT_D1_DECODER_VERSION
    assert not list(raw_path.parent.glob(".tmp-*"))

    calls = 0
    def fail_opener(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("network must not be called")
    same = mirror.mirror_direct_d1_year(
        pair="AUDUSD", year=2021, destination_root=tmp_path, opener=fail_opener
    )
    assert same == (raw_path, sidecar_path)
    assert calls == 0


def test_mirror_no_clobber_and_corrupt_existing_fail_closed(tmp_path: Path) -> None:
    raw_path, sidecar_path = _publish(tmp_path, "AUDUSD", 2021, _year_body(2021))
    sidecar_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="acquisition_sidecar_invalid"):
        mirror.mirror_direct_d1_year(
            pair="AUDUSD", year=2021, destination_root=tmp_path,
            opener=lambda *_args, **_kwargs: pytest.fail("must not call network"),
        )
    assert raw_path.exists()


def test_mirror_atomic_publication_failure_leaves_no_partial_publication(
    monkeypatch, tmp_path: Path
) -> None:
    def fail_rename(_source: object, _destination: object) -> None:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(mirror.os, "rename", fail_rename)
    with pytest.raises(OSError, match="synthetic publication failure"):
        _publish(tmp_path, "AUDUSD", 2021, _year_body(2021))
    assert not (tmp_path / "AUDUSD" / "2021").exists()
    assert not list((tmp_path / "AUDUSD").glob(".tmp-*"))


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"pair": "EURUSD"}, "sidecar_pair_mismatch"),
        ({"year": 2020}, "sidecar_year_mismatch"),
        ({"raw_sha256": "0" * 64}, "raw_sha256_mismatch"),
        ({"decoder_version": "wrong"}, "decoder_version_mismatch"),
    ],
)
def test_directory_transport_rejects_mismatched_sidecar(
    tmp_path: Path, change: dict[str, object], reason: str
) -> None:
    _, sidecar_path = _publish(tmp_path, "AUDUSD", 2021, _year_body(2021))
    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    data.update(change)
    sidecar_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=reason):
        direct.DukascopyDirectD1DirectoryTransport(tmp_path).fetch_year(
            pair="AUDUSD", year=2021, max_response_bytes=8 * 1024 * 1024
        )


def test_directory_transport_rejects_missing_year_sidecar_and_corrupt_raw(tmp_path: Path) -> None:
    transport = direct.DukascopyDirectD1DirectoryTransport(tmp_path)
    with pytest.raises(ValueError, match="missing_direct_d1_year"):
        transport.fetch_year(pair="AUDUSD", year=2021, max_response_bytes=8 * 1024 * 1024)
    raw_path, sidecar_path = _publish(tmp_path, "AUDUSD", 2021, _year_body(2021))
    sidecar_path.unlink()
    with pytest.raises(ValueError, match="missing_acquisition_sidecar"):
        transport.fetch_year(pair="AUDUSD", year=2021, max_response_bytes=8 * 1024 * 1024)
    sidecar_path.write_text("{}", encoding="utf-8")
    raw_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="acquisition_sidecar_invalid"):
        transport.fetch_year(pair="AUDUSD", year=2021, max_response_bytes=8 * 1024 * 1024)


def test_directory_transport_rejects_byte_count_and_source_family_mismatch(
    tmp_path: Path,
) -> None:
    _, sidecar_path = _publish(tmp_path, "AUDUSD", 2021, _year_body(2021))
    original = json.loads(sidecar_path.read_text(encoding="utf-8"))
    for change, reason in (
        ({"byte_count": original["byte_count"] + 1}, "byte_count_mismatch"),
        ({"provider_id": "dukascopy"}, "acquisition_sidecar_invalid"),
    ):
        data = dict(original)
        data.update(change)
        sidecar_path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match=reason):
            direct.DukascopyDirectD1DirectoryTransport(tmp_path).fetch_year(
                pair="AUDUSD", year=2021
            )


@pytest.mark.parametrize(
    "change",
    [
        {"retrieved_at_utc": "not-a-timestamp"},
        {"response_headers": [["Authorization", "secret"]]},
    ],
)
def test_directory_transport_rejects_malformed_audit_evidence(
    tmp_path: Path, change: dict[str, object]
) -> None:
    _, sidecar_path = _publish(tmp_path, "AUDUSD", 2021, _year_body(2021))
    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    data.update(change)
    sidecar_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="acquisition_sidecar_invalid"):
        direct.DukascopyDirectD1DirectoryTransport(tmp_path).fetch_year(
            pair="AUDUSD", year=2021
        )


def test_provider_combines_years_subsets_half_open_and_builds_truthful_identity(
    tmp_path: Path,
) -> None:
    _publish(tmp_path, "AUDUSD", 2020, _year_body(2020))
    _publish(tmp_path, "AUDUSD", 2021, _year_body(2021))
    provider = direct.DukascopyDirectD1HistoricalBarsProvider(
        direct.DukascopyDirectD1DirectoryTransport(tmp_path),
        clock=lambda: datetime(2023, 12, 31, tzinfo=UTC),
    )
    query = BarQuery(
        CanonicalInstrument("AUDUSD"), "D1",
        datetime(2020, 12, 31, tzinfo=UTC), datetime(2021, 1, 2, tzinfo=UTC),
        datetime(2021, 1, 2, tzinfo=UTC),
    )
    result = provider.fetch_bars(query)
    assert not isinstance(result, ProviderFailure)
    assert list(result.frame.index) == [
        pd.Timestamp("2020-12-31", tz="UTC"), pd.Timestamp("2021-01-01", tz="UTC")
    ]
    assert result.provenance.provider_id == direct.DIRECT_D1_PROVIDER_ID
    assert result.provenance.provider_version == direct.DIRECT_D1_PROVIDER_VERSION
    assert result.provenance.normalization_version == direct.DIRECT_D1_NORMALIZATION_VERSION
    assert result.provenance.volume_semantics == direct.DIRECT_D1_VOLUME_SEMANTICS
    assert result.provenance.sanitized_source_reference == direct.DIRECT_D1_SOURCE_REFERENCE
    assert result.provenance.revision.startswith("direct_d1_year_set_sha256:")


def test_provider_identity_is_retrieval_invariant_and_distinct_from_tick_provider(
    tmp_path: Path,
) -> None:
    _publish(tmp_path, "AUDUSD", 2021, _year_body(2021))
    transport = direct.DukascopyDirectD1DirectoryTransport(tmp_path)
    query = BarQuery(
        CanonicalInstrument("AUDUSD"), "D1", datetime(2021, 1, 1, tzinfo=UTC),
        datetime(2021, 1, 3, tzinfo=UTC), datetime(2021, 1, 3, tzinfo=UTC),
    )
    a = direct.DukascopyDirectD1HistoricalBarsProvider(
        transport, clock=lambda: datetime(2022, 1, 1, tzinfo=UTC)
    ).fetch_bars(query)
    b = direct.DukascopyDirectD1HistoricalBarsProvider(
        transport, clock=lambda: datetime(2023, 1, 1, tzinfo=UTC)
    ).fetch_bars(query)
    assert not isinstance(a, ProviderFailure) and not isinstance(b, ProviderFailure)
    assert a.provenance.dataset_id == b.provenance.dataset_id
    assert a.provenance.revision == b.provenance.revision
    assert a.provenance.dataset_id != direct.dataset_identity(
        "dukascopy", "2", query.fingerprint, a.provenance.content_hash
    )


def test_provider_missing_year_fails_closed_without_hourly_fallback(tmp_path: Path) -> None:
    provider = direct.DukascopyDirectD1HistoricalBarsProvider(
        direct.DukascopyDirectD1DirectoryTransport(tmp_path)
    )
    query = BarQuery(
        CanonicalInstrument("AUDUSD"), "D1", datetime(2021, 1, 1, tzinfo=UTC),
        datetime(2021, 1, 2, tzinfo=UTC), datetime(2021, 1, 2, tzinfo=UTC),
    )
    result = provider.fetch_bars(query)
    assert isinstance(result, ProviderFailure)
    assert result.reason == "missing_direct_d1_year"


def test_provider_rejects_forged_year_response_identity() -> None:
    body = _year_body(2021)
    url = direct.dukascopy_direct_d1_url("EURUSD", 2021)

    class ForgedTransport:
        def fetch_year(self, **_kwargs: object) -> object:
            return direct.DukascopyDirectD1Year(
                "EURUSD", 2021, body, url, url, "application/octet-stream"
            )

    query = BarQuery(
        CanonicalInstrument("AUDUSD"), "D1", datetime(2021, 1, 1, tzinfo=UTC),
        datetime(2021, 1, 2, tzinfo=UTC), datetime(2021, 1, 2, tzinfo=UTC),
    )
    result = direct.DukascopyDirectD1HistoricalBarsProvider(
        ForgedTransport()
    ).fetch_bars(query)
    assert isinstance(result, ProviderFailure)
    assert result.reason == "year_response_invalid"


def test_provider_rejects_sealed_range_before_transport() -> None:
    class NoCalls:
        calls = 0

        def fetch_year(self, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("transport must not be called")

    transport = NoCalls()
    provider = direct.DukascopyDirectD1HistoricalBarsProvider(transport)
    query = BarQuery(
        CanonicalInstrument("AUDUSD"), "D1", datetime(2023, 12, 31, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC),
    )
    result = provider.fetch_bars(query)
    assert isinstance(result, ProviderFailure)
    assert result.reason == "research_window_violation"
    assert transport.calls == 0


def test_mirror_cli_is_registered_and_invokes_yearly_mirror(monkeypatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    import fxlab.cli as cli

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        mirror, "mirror_direct_d1_range",
        lambda **kwargs: calls.append(kwargs) or ((tmp_path / "raw", tmp_path / "sidecar"),),
    )
    result = CliRunner().invoke(
        cli.app,
        ["mirror-direct-d1", "--pair", "AUDUSD", "--from", "2021-01-01T00:00:00Z",
         "--to", "2022-01-01T00:00:00Z", "--dest", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["pair"] == "AUDUSD"
