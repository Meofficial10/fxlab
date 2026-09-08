"""Resumable, atomic raw Dukascopy .bi5 mirror acquisition utility.

This module is strictly acquisition/staging infrastructure. It downloads and
stages exact upstream bytes and positive HTTP 404 absence evidence without
performing scientific bar decoding, aggregation, or provenance claims.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .dukascopy_provider import (
    _BI5_MAX_RESPONSE_BYTES,
    _BI5_MAX_TIMEOUT_SECONDS,
    _BI5_SOURCE_REFERENCE,
    BI5_RESEARCH_END,
    BI5_RESEARCH_START,
    DUKASCOPY_BI5_PRICE_DIVISORS,
    _bi5_default_opener,
    _header_value_unchecked,
    _hour_start,
    _response_url,
    dukascopy_bi5_relative_path,
    dukascopy_bi5_url,
)

BI5_ABSENCE_SCHEMA_VERSION = 1
BI5_ABSENCE_RECORD_TYPE = "dukascopy_bi5_upstream_absence"
BI5_MIRROR_RETRY_SLEEPS = (1.0, 2.0)


class Bi5PartitionState(StrEnum):
    PRESENT_STAGED = "present_staged"
    ABSENT_EVIDENCED = "absent_evidenced"
    INCOMPLETE = "incomplete"
    CONFLICT = "conflict"
    CORRUPT_LOCAL = "corrupt_local"


class PartitionConflictError(RuntimeError):
    """Raised when mirror destination already exists during atomic publication."""


@dataclass(frozen=True)
class Bi5AbsenceRecord:
    """Positive upstream HTTP 404 absence evidence for an hourly partition."""

    schema_version: int
    record_type: str
    symbol: str
    hour_utc: str
    sanitized_source_reference: str
    http_status: int
    retrieved_at_utc: str

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        hour: datetime,
        retrieved_at: datetime,
    ) -> Bi5AbsenceRecord:
        hour_utc = _aware_utc(hour, "hour")
        retrieved_utc = _aware_utc(retrieved_at, "retrieved_at")
        return cls(
            schema_version=BI5_ABSENCE_SCHEMA_VERSION,
            record_type=BI5_ABSENCE_RECORD_TYPE,
            symbol=symbol,
            hour_utc=_iso_utc(hour_utc),
            sanitized_source_reference=_BI5_SOURCE_REFERENCE,
            http_status=404,
            retrieved_at_utc=_iso_utc(retrieved_utc),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        expected_symbol: str,
        expected_hour: datetime,
    ) -> Bi5AbsenceRecord:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("absence payload must be non-empty bytes")
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("absence record is not valid UTF-8 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("absence record must be a JSON object")

        if data.get("schema_version") != BI5_ABSENCE_SCHEMA_VERSION:
            raise ValueError("unsupported absence schema version")
        if data.get("record_type") != BI5_ABSENCE_RECORD_TYPE:
            raise ValueError("unexpected absence record type")
        if data.get("symbol") != expected_symbol:
            raise ValueError("absence record symbol mismatch")
        expected_iso = _iso_utc(_aware_utc(expected_hour, "expected_hour"))
        if data.get("hour_utc") != expected_iso:
            raise ValueError("absence record hour mismatch")
        if data.get("sanitized_source_reference") != _BI5_SOURCE_REFERENCE:
            raise ValueError("absence record source reference mismatch")
        if data.get("http_status") != 404:
            raise ValueError("absence record http_status must be 404")
        retrieved_at = data.get("retrieved_at_utc")
        if not isinstance(retrieved_at, str) or not retrieved_at.strip():
            raise ValueError("absence record retrieved_at_utc is malformed")

        return cls(
            schema_version=BI5_ABSENCE_SCHEMA_VERSION,
            record_type=BI5_ABSENCE_RECORD_TYPE,
            symbol=expected_symbol,
            hour_utc=expected_iso,
            sanitized_source_reference=_BI5_SOURCE_REFERENCE,
            http_status=404,
            retrieved_at_utc=retrieved_at,
        )


@dataclass(frozen=True)
class Bi5SyncReport:
    symbol: str
    start: datetime
    end: datetime
    total_hours: int
    present_staged: int
    absent_evidenced: int
    incomplete: int
    conflict: int
    corrupt_local: int
    stopped_at_hour: datetime | None = None
    stop_reason: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.total_hours > 0
            and self.incomplete == 0
            and self.conflict == 0
            and self.corrupt_local == 0
            and self.stop_reason is None
        )


def bi5_partition_paths(root: Path, symbol: str, hour: datetime) -> tuple[Path, Path]:
    """Return absolute target paths for (bi5_data_path, absent_evidence_path)."""
    rel = dukascopy_bi5_relative_path(symbol, hour)
    bi5_path = (root / rel).resolve()
    absent_path = bi5_path.with_name(f"{hour.hour:02d}h_ticks.absent.json")
    return bi5_path, absent_path


def inspect_partition(root: Path, symbol: str, hour: datetime) -> Bi5PartitionState:
    """Inspect local filesystem state for a single hourly partition."""
    try:
        bi5_path, absent_path = bi5_partition_paths(root, symbol, hour)
    except ValueError:
        return Bi5PartitionState.CORRUPT_LOCAL

    has_bi5 = bi5_path.exists()
    has_absent = absent_path.exists()

    if has_bi5 and has_absent:
        return Bi5PartitionState.CONFLICT

    if has_bi5:
        if not bi5_path.is_file():
            return Bi5PartitionState.CORRUPT_LOCAL
        try:
            size = bi5_path.stat().st_size
            if size <= 0 or size > _BI5_MAX_RESPONSE_BYTES:
                return Bi5PartitionState.CORRUPT_LOCAL
            return Bi5PartitionState.PRESENT_STAGED
        except OSError:
            return Bi5PartitionState.CORRUPT_LOCAL

    if has_absent:
        if not absent_path.is_file():
            return Bi5PartitionState.CORRUPT_LOCAL
        try:
            payload = absent_path.read_bytes()
            Bi5AbsenceRecord.from_bytes(payload, expected_symbol=symbol, expected_hour=hour)
            return Bi5PartitionState.ABSENT_EVIDENCED
        except (OSError, ValueError):
            return Bi5PartitionState.CORRUPT_LOCAL

    return Bi5PartitionState.INCOMPLETE


def _atomic_publish_file_no_clobber(temp_path: Path, destination: Path) -> None:
    """Publish a temporary file to destination atomically with strict no-clobber semantics.

    On Windows, os.rename is atomic and fails with FileExistsError if destination exists.
    On POSIX, os.link provides atomic no-clobber creation, followed by unlinking temp.
    """
    destination_parent = destination.parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    try:
        if destination.exists():
            raise PartitionConflictError(f"destination already exists: {destination}")
        if hasattr(os, "link"):
            try:
                os.link(temp_path, destination)
                temp_path.unlink(missing_ok=True)
                return
            except (AttributeError, NotImplementedError, OSError) as exc:
                if isinstance(exc, FileExistsError):
                    raise PartitionConflictError(
                        f"destination already exists: {destination}"
                    ) from exc
        try:
            os.rename(temp_path, destination)
        except FileExistsError as exc:
            raise PartitionConflictError(
                f"destination already exists: {destination}"
            ) from exc
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _atomic_publish_bytes(destination: Path, data: bytes) -> None:
    """Write data to a sibling temporary file, fsync, and publish without clobbering."""
    destination_parent = destination.parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination_parent / f".tmp-{uuid.uuid4().hex}"
    with open(temp_path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _atomic_publish_file_no_clobber(temp_path, destination)


def download_hour(
    symbol: str,
    hour: datetime,
    destination_root: Path,
    *,
    timeout_seconds: float = 30.0,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Bi5PartitionState:
    """Acquire one hourly partition, publish evidence atomically, or return status."""
    if not isinstance(destination_root, Path):
        raise ValueError("destination_root must be a pathlib.Path")
    root = destination_root.resolve()

    if symbol not in DUKASCOPY_BI5_PRICE_DIVISORS:
        raise ValueError("symbol_unsupported")
    hour_utc = _hour_start(hour)
    if hour_utc < BI5_RESEARCH_START or hour_utc >= BI5_RESEARCH_END:
        raise ValueError("research_window_violation")

    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > _BI5_MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds invalid or exceeds hard ceiling")

    # Pre-check local filesystem state
    initial_state = inspect_partition(root, symbol, hour_utc)
    if initial_state in (
        Bi5PartitionState.PRESENT_STAGED,
        Bi5PartitionState.ABSENT_EVIDENCED,
        Bi5PartitionState.CONFLICT,
        Bi5PartitionState.CORRUPT_LOCAL,
    ):
        return initial_state

    url = dukascopy_bi5_url(symbol, hour_utc)
    bi5_path, absent_path = bi5_partition_paths(root, symbol, hour_utc)
    net_opener = opener or _bi5_default_opener()
    net_sleeper = sleeper or (lambda s: None)
    get_clock = clock or (lambda: datetime.now(UTC))

    request = Request(
        url,
        method="GET",
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "fxlab-market-data/1",
        },
    )

    max_attempts = len(BI5_MIRROR_RETRY_SLEEPS) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            response = net_opener(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code == 404:
                record = Bi5AbsenceRecord.create(
                    symbol=symbol,
                    hour=hour_utc,
                    retrieved_at=get_clock(),
                )
                _atomic_publish_bytes(absent_path, record.to_bytes())
                return Bi5PartitionState.ABSENT_EVIDENCED
            if exc.code in (429,) or (500 <= exc.code <= 599):
                if attempt < max_attempts:
                    net_sleeper(BI5_MIRROR_RETRY_SLEEPS[attempt - 1])
                    continue
                return Bi5PartitionState.INCOMPLETE
            # Permanent protocol failure (401, 403, etc.) -> fail closed
            raise RuntimeError(f"permanent_http_error_{exc.code}") from exc
        except (TimeoutError, URLError, OSError):
            if attempt < max_attempts:
                net_sleeper(BI5_MIRROR_RETRY_SLEEPS[attempt - 1])
                continue
            return Bi5PartitionState.INCOMPLETE

        # Process successful response
        try:
            with response:
                status = int(getattr(response, "status", 200))
                if status == 404:
                    record = Bi5AbsenceRecord.create(
                        symbol=symbol,
                        hour=hour_utc,
                        retrieved_at=get_clock(),
                    )
                    _atomic_publish_bytes(absent_path, record.to_bytes())
                    return Bi5PartitionState.ABSENT_EVIDENCED
                if status != 200:
                    if status in (429,) or (500 <= status <= 599):
                        if attempt < max_attempts:
                            net_sleeper(BI5_MIRROR_RETRY_SLEEPS[attempt - 1])
                            continue
                        return Bi5PartitionState.INCOMPLETE
                    raise RuntimeError(f"unexpected_http_status_{status}")

                returned_url = _response_url(response)
                if returned_url != url:
                    raise RuntimeError("unexpected_response_url")

                headers = getattr(response, "headers", {})
                content_type = (_header_value_unchecked(headers, "Content-Type") or "").lower()
                if content_type != "application/octet-stream":
                    raise RuntimeError("unexpected_media_type")

                body = response.read(_BI5_MAX_RESPONSE_BYTES + 1)
                if len(body) > _BI5_MAX_RESPONSE_BYTES or len(body) == 0:
                    raise RuntimeError("bi5_body_size_invalid")

            _atomic_publish_bytes(bi5_path, body)
            return Bi5PartitionState.PRESENT_STAGED
        except (TimeoutError, URLError, OSError):
            if attempt < max_attempts:
                net_sleeper(BI5_MIRROR_RETRY_SLEEPS[attempt - 1])
                continue
            return Bi5PartitionState.INCOMPLETE

    return Bi5PartitionState.INCOMPLETE


def sync_range(
    symbol: str,
    start: datetime,
    end: datetime,
    destination_root: Path,
    *,
    timeout_seconds: float = 30.0,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Bi5SyncReport:
    """Synchronize an hourly range for a single pair sequentially and fail-fast."""
    if symbol not in DUKASCOPY_BI5_PRICE_DIVISORS:
        raise ValueError("symbol_unsupported")
    start_utc = _hour_start(start)
    end_utc = _hour_start(end)
    if start_utc >= end_utc:
        raise ValueError("start must precede end")
    if start_utc < BI5_RESEARCH_START or end_utc > BI5_RESEARCH_END:
        raise ValueError("research_window_violation")
    if not isinstance(destination_root, Path):
        raise ValueError("destination_root must be a pathlib.Path")

    root = destination_root.resolve()
    hour_count = int((end_utc - start_utc).total_seconds() // 3600)

    present_staged = 0
    absent_evidenced = 0
    incomplete = 0
    conflict = 0
    corrupt_local = 0
    stopped_at_hour: datetime | None = None
    stop_reason: str | None = None

    for number in range(hour_count):
        hour = start_utc + timedelta(hours=number)
        state = download_hour(
            symbol=symbol,
            hour=hour,
            destination_root=root,
            timeout_seconds=timeout_seconds,
            opener=opener,
            sleeper=sleeper,
            clock=clock,
        )

        if state is Bi5PartitionState.PRESENT_STAGED:
            present_staged += 1
        elif state is Bi5PartitionState.ABSENT_EVIDENCED:
            absent_evidenced += 1
        elif state is Bi5PartitionState.INCOMPLETE:
            incomplete += 1
            stopped_at_hour = hour
            stop_reason = "transient_retry_exhausted"
            break
        elif state is Bi5PartitionState.CONFLICT:
            conflict += 1
            stopped_at_hour = hour
            stop_reason = "conflicting_partition_evidence"
            break
        elif state is Bi5PartitionState.CORRUPT_LOCAL:
            corrupt_local += 1
            stopped_at_hour = hour
            stop_reason = "corrupt_local_file"
            break
        else:
            stopped_at_hour = hour
            stop_reason = f"unexpected_state_{state}"
            break

    # If stopped early, remaining hours are counted as incomplete
    processed = present_staged + absent_evidenced + conflict + corrupt_local + incomplete
    remaining = hour_count - processed
    incomplete += remaining

    return Bi5SyncReport(
        symbol=symbol,
        start=start_utc,
        end=end_utc,
        total_hours=hour_count,
        present_staged=present_staged,
        absent_evidenced=absent_evidenced,
        incomplete=incomplete,
        conflict=conflict,
        corrupt_local=corrupt_local,
        stopped_at_hour=stopped_at_hour,
        stop_reason=stop_reason,
    )


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must have a non-null utcoffset")
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    iso = value.astimezone(UTC).isoformat()
    if iso.endswith("+00:00"):
        return iso[:-6] + "Z"
    return iso
