"""Bounded Candidate B BIS acquisition boundary.

No network call occurs on import or by default.  A caller must explicitly construct a transport and
pass validated requests.  The boundary performs exactly one transport call per series and has no
retry, cache, or fallback behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Protocol

from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    APPROVED_REQUEST_END,
    APPROVED_REQUEST_START,
    PolicyRateMetadata,
    PolicyRateRequest,
    PolicyRateSeriesManifest,
    PolicyRateSeriesSpec,
    build_series_manifest,
)


@dataclass(frozen=True)
class BisTransportResponse:
    raw_bytes: bytes
    media_type: str
    headers: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.raw_bytes, bytes) or not self.raw_bytes:
            raise ValueError("transport response bytes are required")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("transport response media type is required")
        normalized_headers = {str(key).lower(): str(value) for key, value in self.headers.items()}
        object.__setattr__(self, "headers", MappingProxyType(normalized_headers))


class BisTransport(Protocol):
    def fetch(self, request: PolicyRateRequest) -> BisTransportResponse: ...


@dataclass(frozen=True)
class BisIngestionResult:
    raw_bytes: bytes
    series_manifest: PolicyRateSeriesManifest


def approved_requests() -> tuple[PolicyRateRequest, ...]:
    return tuple(
        PolicyRateRequest(
            PolicyRateSeriesSpec(currency, series_key),
            APPROVED_REQUEST_START,
            APPROVED_REQUEST_END,
        )
        for currency, series_key in APPROVED_BIS_SERIES.items()
    )


def ingest_series(
    request: PolicyRateRequest,
    metadata: PolicyRateMetadata,
    transport: BisTransport,
    retrieved_at: datetime,
) -> BisIngestionResult:
    if not isinstance(request, PolicyRateRequest):
        raise ValueError("validated PolicyRateRequest is required")
    response = transport.fetch(request)
    if response.media_type.split(";", 1)[0].strip().lower() != metadata.media_type.lower():
        raise ValueError("transport media type does not match frozen metadata")
    manifest = build_series_manifest(request, metadata, response.raw_bytes, retrieved_at)
    return BisIngestionResult(response.raw_bytes, manifest)


def main() -> None:
    raise SystemExit(
        "network_acquisition_not_authorized: inject an explicitly approved one-attempt BIS "
        "transport"
    )


if __name__ == "__main__":
    main()
