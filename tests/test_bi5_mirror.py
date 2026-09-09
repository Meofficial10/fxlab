"""Offline unit tests for the Dukascopy .bi5 raw mirror utility."""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from fxlab.data import (
    Bi5AbsenceRecord,
    Bi5PartitionState,
    download_hour,
    inspect_partition,
    sync_range,
)
from fxlab.data import bi5_mirror as mirror_module
from fxlab.data.bi5_mirror import (
    PartitionConflictError,
    _atomic_publish_bytes,
    _atomic_publish_file_no_clobber,
    bi5_partition_paths,
)

START = datetime(2021, 1, 5, 0, tzinfo=UTC)
END = datetime(2021, 1, 6, 0, tzinfo=UTC)


class FakeHttpResponse(BytesIO):
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
        self.headers = {"Content-Type": content_type}

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_200_publishes_exact_raw_bytes(tmp_path: Path) -> None:
    raw_payload = b"compressed_bi5_binary_test_bytes"
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=lambda req, **_kwargs: FakeHttpResponse(raw_payload, url=url),
    )
    assert state == Bi5PartitionState.PRESENT_STAGED

    bi5_path, absent_path = bi5_partition_paths(tmp_path, "AUDUSD", START)
    assert bi5_path.exists()
    assert bi5_path.read_bytes() == raw_payload
    assert not absent_path.exists()


def test_200_empty_publishes_truthful_evidence_without_bi5(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    retrieved_at = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=lambda req, **_kwargs: FakeHttpResponse(b"", url=url),
        clock=lambda: retrieved_at,
    )

    assert state == Bi5PartitionState.ABSENT_EVIDENCED
    bi5_path, absent_path = bi5_partition_paths(tmp_path, "AUDUSD", START)
    assert not bi5_path.exists()
    record = Bi5AbsenceRecord.from_bytes(
        absent_path.read_bytes(), expected_symbol="AUDUSD", expected_hour=START
    )
    assert record.schema_version == 2
    assert record.http_status == 200
    assert record.evidence_type == "http_200_empty_body"
    assert record.body_byte_count == 0
    assert record.body_sha256 == hashlib.sha256(b"").hexdigest()
    assert record.retrieved_at_utc == "2026-09-08T12:00:00Z"


def test_sync_range_continues_after_200_empty_partition(tmp_path: Path) -> None:
    requested: list[str] = []

    def opener(req: object, **_kwargs: object) -> FakeHttpResponse:
        url = req.full_url  # type: ignore[attr-defined]
        requested.append(url)
        body = b"" if url.endswith("00h_ticks.bi5") else b"bi5_content"
        return FakeHttpResponse(body, url=url)

    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=2),
        destination_root=tmp_path,
        opener=opener,
    )

    assert report.ok
    assert report.absent_evidenced == 1
    assert report.present_staged == 1
    assert len(requested) == 2


def test_200_response_larger_than_eight_mib_remains_rejected(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    oversized = b"x" * (8 * 1024 * 1024 + 1)

    with pytest.raises(RuntimeError, match="bi5_body_size_invalid"):
        download_hour(
            symbol="AUDUSD",
            hour=START,
            destination_root=tmp_path,
            opener=lambda req, **_kwargs: FakeHttpResponse(oversized, url=url),
        )

    bi5_path, absent_path = bi5_partition_paths(tmp_path, "AUDUSD", START)
    assert not bi5_path.exists()
    assert not absent_path.exists()


def test_404_publishes_only_valid_absence_evidence(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"

    def opener_404(req: object, **_kwargs: object) -> FakeHttpResponse:
        raise HTTPError(url, 404, "Not Found", {}, BytesIO(b""))

    fixed_retrieved = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_404,
        clock=lambda: fixed_retrieved,
    )
    assert state == Bi5PartitionState.ABSENT_EVIDENCED

    bi5_path, absent_path = bi5_partition_paths(tmp_path, "AUDUSD", START)
    assert not bi5_path.exists()
    assert absent_path.exists()

    record = Bi5AbsenceRecord.from_bytes(
        absent_path.read_bytes(), expected_symbol="AUDUSD", expected_hour=START
    )
    assert record.schema_version == 1
    assert record.record_type == "dukascopy_bi5_upstream_absence"
    assert record.symbol == "AUDUSD"
    assert record.hour_utc == "2021-01-05T00:00:00Z"
    assert record.sanitized_source_reference == "dukascopy:datafeed:bi5:hourly:bid"
    assert record.http_status == 404
    assert record.retrieved_at_utc == "2026-09-08T12:00:00Z"


def test_resume_skips_present_staged(tmp_path: Path) -> None:
    bi5_path, _ = bi5_partition_paths(tmp_path, "AUDUSD", START)
    bi5_path.parent.mkdir(parents=True, exist_ok=True)
    bi5_path.write_bytes(b"existing_bytes")

    called = False

    def opener_fail(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal called
        called = True
        raise AssertionError("network must not be called on existing partition")

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_fail,
    )
    assert state == Bi5PartitionState.PRESENT_STAGED
    assert not called


def test_resume_skips_absent_evidenced(tmp_path: Path) -> None:
    _, absent_path = bi5_partition_paths(tmp_path, "AUDUSD", START)
    absent_path.parent.mkdir(parents=True, exist_ok=True)
    record = Bi5AbsenceRecord.create(
        symbol="AUDUSD", hour=START, retrieved_at=datetime.now(UTC)
    )
    absent_path.write_bytes(record.to_bytes())

    called = False

    def opener_fail(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal called
        called = True
        raise AssertionError("network must not be called on evidenced partition")

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_fail,
    )
    assert state == Bi5PartitionState.ABSENT_EVIDENCED
    assert not called


def test_missing_partition_is_incomplete(tmp_path: Path) -> None:
    assert inspect_partition(tmp_path, "AUDUSD", START) == Bi5PartitionState.INCOMPLETE


def test_both_artifacts_result_in_conflict_state(tmp_path: Path) -> None:
    bi5_path, absent_path = bi5_partition_paths(tmp_path, "AUDUSD", START)
    bi5_path.parent.mkdir(parents=True, exist_ok=True)
    bi5_path.write_bytes(b"bytes")
    record = Bi5AbsenceRecord.create(
        symbol="AUDUSD", hour=START, retrieved_at=datetime.now(UTC)
    )
    absent_path.write_bytes(record.to_bytes())

    assert inspect_partition(tmp_path, "AUDUSD", START) == Bi5PartitionState.CONFLICT


@pytest.mark.parametrize(
    "bad_content",
    [
        b"",  # zero-byte
        b"x" * (8 * 1024 * 1024 + 10),  # oversized
    ],
    ids=["empty", "oversized"],
)
def test_corrupt_bi5_file_is_corrupt_local(tmp_path: Path, bad_content: bytes) -> None:
    bi5_path, _ = bi5_partition_paths(tmp_path, "AUDUSD", START)
    bi5_path.parent.mkdir(parents=True, exist_ok=True)
    bi5_path.write_bytes(bad_content)

    assert inspect_partition(tmp_path, "AUDUSD", START) == Bi5PartitionState.CORRUPT_LOCAL


def test_malformed_absence_is_corrupt_local(tmp_path: Path) -> None:
    _, absent_path = bi5_partition_paths(tmp_path, "AUDUSD", START)
    absent_path.parent.mkdir(parents=True, exist_ok=True)
    absent_path.write_bytes(b"{\"schema_version\": 99}")

    assert inspect_partition(tmp_path, "AUDUSD", START) == Bi5PartitionState.CORRUPT_LOCAL


def test_non_empty_within_size_bi5_is_present_staged_without_scientific_validation(
    tmp_path: Path,
) -> None:
    # Payload is arbitrary non-LZMA bytes that would fail scientific decoding
    bad_bi5_bytes = b"not_lzma_compressed_ticks_data_12345"
    bi5_path, _ = bi5_partition_paths(tmp_path, "AUDUSD", START)
    bi5_path.parent.mkdir(parents=True, exist_ok=True)
    bi5_path.write_bytes(bad_bi5_bytes)

    assert inspect_partition(tmp_path, "AUDUSD", START) == Bi5PartitionState.PRESENT_STAGED

    called = False

    def opener_fail(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal called
        called = True
        raise AssertionError("network must not be called on existing staged partition")

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_fail,
    )
    assert state == Bi5PartitionState.PRESENT_STAGED
    assert not called


def test_retry_sleeps_on_503_then_success(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    sleeps: list[float] = []
    calls = 0

    def opener_flaky(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPError(url, 503, "Service Unavailable", {}, BytesIO(b""))
        return FakeHttpResponse(b"bi5_content", url=url)

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_flaky,
        sleeper=sleeps.append,
    )
    assert state == Bi5PartitionState.PRESENT_STAGED
    assert calls == 2
    assert sleeps == [1.0]


def test_retry_sleeps_on_501_not_implemented_then_success(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    sleeps: list[float] = []
    calls = 0

    def opener_flaky(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPError(url, 501, "Not Implemented", {}, BytesIO(b""))
        return FakeHttpResponse(b"bi5_content", url=url)

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_flaky,
        sleeper=sleeps.append,
    )
    assert state == Bi5PartitionState.PRESENT_STAGED
    assert calls == 2
    assert sleeps == [1.0]


def test_retry_sleeps_on_599_then_exhaustion(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    sleeps: list[float] = []
    calls = 0

    def opener_599(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        raise HTTPError(url, 599, "Network Connect Timeout Error", {}, BytesIO(b""))

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_599,
        sleeper=sleeps.append,
    )
    assert state == Bi5PartitionState.INCOMPLETE
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_retry_sleeps_on_timeouts_then_success(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    sleeps: list[float] = []
    calls = 0

    def opener_flaky(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        if calls in (1, 2):
            raise TimeoutError("connection timed out")
        return FakeHttpResponse(b"bi5_content", url=url)

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_flaky,
        sleeper=sleeps.append,
    )
    assert state == Bi5PartitionState.PRESENT_STAGED
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_three_transient_failures_exhaust_attempts(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    sleeps: list[float] = []
    calls = 0

    def opener_down(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        raise HTTPError(url, 503, "Service Unavailable", {}, BytesIO(b""))

    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener_down,
        sleeper=sleeps.append,
    )
    assert state == Bi5PartitionState.INCOMPLETE
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_default_retry_path_uses_the_frozen_one_then_two_second_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    sleeps: list[float] = []
    calls = 0

    def opener(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HTTPError(url, 503, "Down", {}, BytesIO(b""))
        return FakeHttpResponse(b"bi5_content", url=url)

    monkeypatch.setattr(mirror_module.time, "sleep", sleeps.append)
    state = download_hour(
        symbol="AUDUSD",
        hour=START,
        destination_root=tmp_path,
        opener=opener,
    )

    assert state is Bi5PartitionState.PRESENT_STAGED
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_permanent_403_performs_one_attempt_and_raises(tmp_path: Path) -> None:
    url = "https://datafeed.dukascopy.com/datafeed/AUDUSD/2021/00/05/00h_ticks.bi5"
    calls = 0

    def opener_403(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        raise HTTPError(url, 403, "Forbidden", {}, BytesIO(b""))

    with pytest.raises(RuntimeError, match="permanent_http_error_403"):
        download_hour(
            symbol="AUDUSD",
            hour=START,
            destination_root=tmp_path,
            opener=opener_403,
        )
    assert calls == 1


def test_range_sync_stops_at_first_failure_and_does_not_request_later_hours(
    tmp_path: Path,
) -> None:
    requested_urls: list[str] = []

    def opener_stops_at_hour_2(req: object, **_kwargs: object) -> FakeHttpResponse:
        full_url = getattr(req, "full_url", getattr(req, "url", str(req)))
        requested_urls.append(full_url)
        if full_url.endswith("02h_ticks.bi5"):
            raise HTTPError(full_url, 503, "Down", {}, BytesIO(b""))
        return FakeHttpResponse(b"bi5_content", url=full_url)

    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=5),
        destination_root=tmp_path,
        opener=opener_stops_at_hour_2,
    )
    assert not report.ok
    assert report.total_hours == 5
    assert report.present_staged == 2
    assert report.incomplete == 3
    assert report.stop_reason == "transient_retry_exhausted"
    assert report.stopped_at_hour == START.replace(hour=2)

    # Prove hour 03 and 04 were never requested
    assert not any(u.endswith("03h_ticks.bi5") for u in requested_urls)
    assert not any(u.endswith("04h_ticks.bi5") for u in requested_urls)


def test_range_sync_rerun_resumes_from_incomplete(tmp_path: Path) -> None:
    calls = 0

    def opener_all_200(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal calls
        calls += 1
        full_url = getattr(req, "full_url", getattr(req, "url", str(req)))
        return FakeHttpResponse(b"bi5_content", url=full_url)

    # First run succeeds for 2 hours
    report1 = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=2),
        destination_root=tmp_path,
        opener=opener_all_200,
    )
    assert report1.ok
    assert report1.present_staged == 2
    assert calls == 2

    # Second run for 4 hours should only download the 2 new hours
    calls = 0
    report2 = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=4),
        destination_root=tmp_path,
        opener=opener_all_200,
    )
    assert report2.ok
    assert report2.total_hours == 4
    assert report2.present_staged == 4
    assert calls == 2  # exactly 2 requests made


def test_workers_one_preserves_sequential_fail_fast_compatibility(tmp_path: Path) -> None:
    requested: list[str] = []

    def opener(req: object, **_kwargs: object) -> FakeHttpResponse:
        url = req.full_url  # type: ignore[attr-defined]
        requested.append(url)
        if url.endswith("01h_ticks.bi5"):
            raise HTTPError(url, 503, "Down", {}, BytesIO(b""))
        return FakeHttpResponse(b"bi5_content", url=url)

    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=4),
        destination_root=tmp_path,
        opener=opener,
        sleeper=lambda _seconds: None,
        workers=1,
    )

    assert [url.rsplit("/", 1)[-1] for url in requested] == [
        "00h_ticks.bi5",
        "01h_ticks.bi5",
        "01h_ticks.bi5",
        "01h_ticks.bi5",
    ]
    assert report.present_staged == 1
    assert report.incomplete == 3
    assert report.stopped_at_hour == START.replace(hour=1)
    assert report.stop_reason == "transient_retry_exhausted"


def test_concurrent_window_finishes_inflight_hours_but_keeps_failure_incomplete(
    tmp_path: Path,
) -> None:
    calls: dict[str, int] = {}
    lock = threading.Lock()

    def opener(req: object, **_kwargs: object) -> FakeHttpResponse:
        url = req.full_url  # type: ignore[attr-defined]
        name = url.rsplit("/", 1)[-1]
        with lock:
            calls[name] = calls.get(name, 0) + 1
        if name == "00h_ticks.bi5":
            raise HTTPError(url, 503, "Down", {}, BytesIO(b""))
        if name == "02h_ticks.bi5":
            raise HTTPError(url, 404, "Not Found", {}, BytesIO(b""))
        return FakeHttpResponse(b"" if name == "03h_ticks.bi5" else b"bi5_content", url=url)

    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=4),
        destination_root=tmp_path,
        opener_factory=lambda: opener,
        sleeper=lambda _seconds: None,
        workers=4,
    )

    assert not report.ok
    assert report.present_staged == 1
    assert report.absent_evidenced == 2
    assert report.incomplete == 1
    assert report.stopped_at_hour == START
    assert report.stop_reason == "transient_retry_exhausted"
    assert calls == {
        "00h_ticks.bi5": 3,
        "01h_ticks.bi5": 1,
        "02h_ticks.bi5": 1,
        "03h_ticks.bi5": 1,
    }
    failed_bi5, failed_evidence = bi5_partition_paths(tmp_path, "AUDUSD", START)
    assert not failed_bi5.exists()
    assert not failed_evidence.exists()
    assert bi5_partition_paths(tmp_path, "AUDUSD", START.replace(hour=1))[0].exists()
    assert bi5_partition_paths(tmp_path, "AUDUSD", START.replace(hour=2))[1].exists()
    empty_evidence = bi5_partition_paths(tmp_path, "AUDUSD", START.replace(hour=3))[1]
    record = Bi5AbsenceRecord.from_bytes(
        empty_evidence.read_bytes(),
        expected_symbol="AUDUSD",
        expected_hour=START.replace(hour=3),
    )
    assert record.http_status == 200
    assert record.evidence_type == "http_200_empty_body"


def test_concurrent_scheduler_never_queues_beyond_one_worker_sized_window(
    tmp_path: Path,
) -> None:
    requested: list[str] = []
    lock = threading.Lock()

    def opener(req: object, **_kwargs: object) -> FakeHttpResponse:
        url = req.full_url  # type: ignore[attr-defined]
        name = url.rsplit("/", 1)[-1]
        with lock:
            requested.append(name)
        if name == "00h_ticks.bi5":
            raise HTTPError(url, 503, "Down", {}, BytesIO(b""))
        return FakeHttpResponse(b"bi5_content", url=url)

    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=10),
        destination_root=tmp_path,
        opener_factory=lambda: opener,
        sleeper=lambda _seconds: None,
        workers=2,
    )

    assert set(requested) == {"00h_ticks.bi5", "01h_ticks.bi5"}
    assert len(requested) == 4
    assert report.present_staged == 1
    assert report.incomplete == 9


def test_concurrent_window_reports_earliest_of_multiple_failed_hours(
    tmp_path: Path,
) -> None:
    def opener(req: object, **_kwargs: object) -> FakeHttpResponse:
        url = req.full_url  # type: ignore[attr-defined]
        raise HTTPError(url, 503, "Down", {}, BytesIO(b""))

    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=2),
        destination_root=tmp_path,
        opener_factory=lambda: opener,
        sleeper=lambda _seconds: None,
        workers=2,
    )

    assert report.incomplete == 2
    assert report.stopped_at_hour == START
    assert report.stop_reason == "transient_retry_exhausted"


def test_concurrent_sync_skips_all_resolved_partition_kinds(tmp_path: Path) -> None:
    present, _ = bi5_partition_paths(tmp_path, "AUDUSD", START)
    present.parent.mkdir(parents=True, exist_ok=True)
    present.write_bytes(b"existing")
    _, absent_404 = bi5_partition_paths(tmp_path, "AUDUSD", START.replace(hour=1))
    absent_404.write_bytes(
        Bi5AbsenceRecord.create(
            symbol="AUDUSD",
            hour=START.replace(hour=1),
            retrieved_at=datetime.now(UTC),
        ).to_bytes()
    )
    _, absent_200 = bi5_partition_paths(tmp_path, "AUDUSD", START.replace(hour=2))
    absent_200.write_bytes(
        Bi5AbsenceRecord.create_empty_response(
            symbol="AUDUSD",
            hour=START.replace(hour=2),
            retrieved_at=datetime.now(UTC),
        ).to_bytes()
    )
    requested: list[str] = []

    def opener(req: object, **_kwargs: object) -> FakeHttpResponse:
        url = req.full_url  # type: ignore[attr-defined]
        requested.append(url.rsplit("/", 1)[-1])
        return FakeHttpResponse(b"new", url=url)

    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=4),
        destination_root=tmp_path,
        opener_factory=lambda: opener,
        workers=4,
    )

    assert report.ok
    assert report.present_staged == 2
    assert report.absent_evidenced == 2
    assert requested == ["03h_ticks.bi5"]


def test_concurrent_sync_fails_closed_on_preexisting_conflict_before_network(
    tmp_path: Path,
) -> None:
    bi5_path, absent_path = bi5_partition_paths(tmp_path, "AUDUSD", START)
    bi5_path.parent.mkdir(parents=True, exist_ok=True)
    bi5_path.write_bytes(b"existing")
    absent_path.write_bytes(
        Bi5AbsenceRecord.create(
            symbol="AUDUSD", hour=START, retrieved_at=datetime.now(UTC)
        ).to_bytes()
    )
    calls = 0

    def opener() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("conflict must stop before network scheduling")

    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=2),
        destination_root=tmp_path,
        opener_factory=lambda: opener,
        workers=2,
    )

    assert calls == 0
    assert not report.ok
    assert report.conflict == 1
    assert report.incomplete == 1
    assert report.stopped_at_hour == START
    assert report.stop_reason == "conflicting_partition_evidence"


@pytest.mark.parametrize("workers", [0, -1, 5, True, 1.5])
def test_worker_count_is_strictly_bounded_before_network(
    tmp_path: Path, workers: object
) -> None:
    called = False

    def opener() -> object:
        nonlocal called
        called = True
        raise AssertionError("invalid workers must fail before network")

    with pytest.raises(ValueError, match="workers must be an integer from 1 through 4"):
        sync_range(
            symbol="AUDUSD",
            start=START,
            end=START.replace(hour=1),
            destination_root=tmp_path,
            opener_factory=lambda: opener,
            workers=workers,  # type: ignore[arg-type]
        )
    assert not called


def test_concurrent_sync_shuts_down_workers_after_completion(tmp_path: Path) -> None:
    report = sync_range(
        symbol="AUDUSD",
        start=START,
        end=START.replace(hour=2),
        destination_root=tmp_path,
        opener_factory=lambda: (
            lambda req, **_kwargs: FakeHttpResponse(
                b"bi5_content", url=req.full_url  # type: ignore[attr-defined]
            )
        ),
        workers=2,
    )

    assert report.ok
    assert not any(
        thread.name.startswith("fxlab-bi5-mirror")
        for thread in threading.enumerate()
    )


def test_keyboard_interrupt_propagates_without_false_completion_or_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    barrier = threading.Barrier(2)

    def interrupted_download(*_args: object, **kwargs: object) -> Bi5PartitionState:
        hour = kwargs["hour"]
        assert isinstance(hour, datetime)
        barrier.wait(timeout=2)
        if hour == START:
            raise KeyboardInterrupt
        destination = kwargs["destination_root"]
        assert isinstance(destination, Path)
        target, _ = bi5_partition_paths(destination, "AUDUSD", hour)
        _atomic_publish_bytes(target, b"complete")
        return Bi5PartitionState.PRESENT_STAGED

    monkeypatch.setattr(mirror_module, "download_hour", interrupted_download)
    with pytest.raises(KeyboardInterrupt):
        sync_range(
            symbol="AUDUSD",
            start=START,
            end=START.replace(hour=2),
            destination_root=tmp_path,
            workers=2,
        )

    successful, _ = bi5_partition_paths(tmp_path, "AUDUSD", START.replace(hour=1))
    assert successful.read_bytes() == b"complete"
    assert list(successful.parent.glob(".tmp-*")) == []
    assert not any(
        thread.name.startswith("fxlab-bi5-mirror")
        for thread in threading.enumerate()
    )


def test_research_boundary_and_unsupported_symbol_fail_before_network(
    tmp_path: Path,
) -> None:
    called = False

    def opener_fail(req: object, **_kwargs: object) -> FakeHttpResponse:
        nonlocal called
        called = True
        raise AssertionError("network must not be called on boundary violations")

    with pytest.raises(ValueError, match="research_window_violation"):
        download_hour(
            symbol="AUDUSD",
            hour=datetime(2024, 1, 1, 0, tzinfo=UTC),
            destination_root=tmp_path,
            opener=opener_fail,
        )
    assert not called

    with pytest.raises(ValueError, match="symbol_unsupported"):
        download_hour(
            symbol="XAUUSD",
            hour=START,
            destination_root=tmp_path,
            opener=opener_fail,
        )
    assert not called


def test_atomic_publication_cannot_overwrite_existing_destination(tmp_path: Path) -> None:
    dest = tmp_path / "target.bi5"
    dest.write_bytes(b"original_authoritative_bytes")

    temp = tmp_path / ".tmp-test"
    temp.write_bytes(b"new_competing_bytes")

    with pytest.raises(PartitionConflictError, match="destination already exists"):
        _atomic_publish_file_no_clobber(temp, dest)

    assert dest.read_bytes() == b"original_authoritative_bytes"
    assert not temp.exists()  # temp cleaned safely


def test_destination_appearing_during_publication_preserves_existing_bytes(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "target.bi5"
    dest.write_bytes(b"already_there")

    with pytest.raises(PartitionConflictError):
        _atomic_publish_bytes(dest, b"attempted_overwrite")

    assert dest.read_bytes() == b"already_there"
    # Ensure no leftover temp files in directory
    temp_files = list(tmp_path.glob(".tmp-*"))
    assert temp_files == []
