"""Atomic mirror for reviewed Dukascopy direct-D1 yearly artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .dukascopy_direct_d1 import (
    DIRECT_D1_DECODER_VERSION,
    DIRECT_D1_MAX_RESPONSE_BYTES,
    DIRECT_D1_MAX_TIMEOUT_SECONDS,
    DIRECT_D1_PROVIDER_ID,
    DIRECT_D1_PROVIDER_VERSION,
    DIRECT_D1_SOURCE_REFERENCE,
    DukascopyDirectD1DirectoryTransport,
    DukascopyDirectD1HttpTransport,
    decode_dukascopy_direct_d1_year,
    dukascopy_direct_d1_relative_path,
)


def _iso_utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("retrieved_at_invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def mirror_direct_d1_year(
    *,
    pair: str,
    year: int,
    destination_root: Path,
    timeout_seconds: float = DIRECT_D1_MAX_TIMEOUT_SECONDS,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
) -> tuple[Path, Path]:
    """Publish one verified year through a temporary sibling directory."""
    if not isinstance(destination_root, Path):
        raise ValueError("destination_root_invalid")
    root = destination_root.resolve()
    relative = dukascopy_direct_d1_relative_path(pair, year)
    final_dir = (root / relative.parent).resolve()
    raw_path = final_dir / relative.name
    sidecar_path = final_dir / "acquisition.json"
    try:
        final_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError("path_traversal_prohibited") from exc

    if final_dir.exists():
        DukascopyDirectD1DirectoryTransport(root).fetch_year(
            pair=pair, year=year, max_response_bytes=DIRECT_D1_MAX_RESPONSE_BYTES
        )
        return raw_path, sidecar_path

    transport = DukascopyDirectD1HttpTransport(
        opener=opener if opener is not None else DukascopyDirectD1HttpTransport().opener,
        sleeper=sleeper,
    )
    source = transport.fetch_year(
        pair=pair,
        year=year,
        timeout_seconds=timeout_seconds,
        max_response_bytes=DIRECT_D1_MAX_RESPONSE_BYTES,
    )
    decode_dukascopy_direct_d1_year(source.body, pair, year)
    retrieved_at = (clock or (lambda: datetime.now(UTC)))()
    record = {
        "schema": "dukascopy_direct_d1_acquisition.v1",
        "provider_id": DIRECT_D1_PROVIDER_ID,
        "provider_version": DIRECT_D1_PROVIDER_VERSION,
        "source_reference": DIRECT_D1_SOURCE_REFERENCE,
        "pair": pair,
        "year": year,
        "requested_url": source.requested_url,
        "returned_url": source.returned_url,
        "http_status": 200,
        "response_media_type": source.response_media_type,
        "response_headers": [list(item) for item in source.response_headers],
        "byte_count": len(source.body),
        "raw_sha256": hashlib.sha256(source.body).hexdigest(),
        "decoder_version": DIRECT_D1_DECODER_VERSION,
        "retrieved_at_utc": _iso_utc(retrieved_at),
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()

    final_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = final_dir.parent / f".tmp-{year}-{uuid.uuid4().hex}"
    try:
        temp_dir.mkdir()
        for name, content in ((relative.name, source.body), ("acquisition.json", payload)):
            with open(temp_dir / name, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        if final_dir.exists():
            raise FileExistsError("destination_exists")
        os.rename(temp_dir, final_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise
    return raw_path, sidecar_path


def mirror_direct_d1_range(
    *,
    pair: str,
    start: datetime,
    end: datetime,
    destination_root: Path,
    timeout_seconds: float = DIRECT_D1_MAX_TIMEOUT_SECONDS,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
) -> tuple[tuple[Path, Path], ...]:
    """Mirror an explicitly bounded sequence of complete calendar years."""
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise ValueError("range_invalid")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("range_timezone_invalid")
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if (
        start_utc != datetime(start_utc.year, 1, 1, tzinfo=UTC)
        or end_utc != datetime(end_utc.year, 1, 1, tzinfo=UTC)
        or start_utc >= end_utc
    ):
        raise ValueError("range_must_contain_complete_years")
    results = []
    for year in range(start_utc.year, end_utc.year):
        results.append(
            mirror_direct_d1_year(
                pair=pair,
                year=year,
                destination_root=destination_root,
                timeout_seconds=timeout_seconds,
                opener=opener,
                sleeper=sleeper,
                clock=clock,
            )
        )
    return tuple(results)


__all__ = ["mirror_direct_d1_range", "mirror_direct_d1_year"]
