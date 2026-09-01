"""Bounded Candidate B BIS acquisition boundary.

No network call occurs on import or by default.  A caller must explicitly construct a transport and
pass validated requests.  The boundary performs exactly one transport call per series and has no
retry, cache, or fallback behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    APPROVED_REQUEST_END,
    APPROVED_REQUEST_START,
    AUTHORITATIVE_BIS_RAW_STATUS_SEMANTICS,
    AUTHORITATIVE_D_AU_ACCEPT,
    AUTHORITATIVE_D_AU_URL,
    AUTHORITATIVE_D_CA_ACCEPT,
    AUTHORITATIVE_D_CA_URL,
    AUTHORITATIVE_D_CH_ACCEPT,
    AUTHORITATIVE_D_CH_URL,
    AUTHORITATIVE_D_GB_ACCEPT,
    AUTHORITATIVE_D_GB_URL,
    AUTHORITATIVE_D_JP_ACCEPT,
    AUTHORITATIVE_D_JP_URL,
    AUTHORITATIVE_D_US_ACCEPT,
    AUTHORITATIVE_D_US_URL,
    AUTHORITATIVE_D_XM_ACCEPT,
    AUTHORITATIVE_D_XM_URL,
    PolicyRateMetadata,
    PolicyRateQualificationError,
    PolicyRateRequest,
    PolicyRateSeriesManifest,
    PolicyRateSeriesSpec,
    authoritative_d_au_request,
    authoritative_d_ca_request,
    authoritative_d_ch_request,
    authoritative_d_gb_request,
    authoritative_d_jp_request,
    authoritative_d_us_request,
    authoritative_d_xm_request,
    build_series_manifest,
    canonical_json,
    canonical_sha256,
    parse_authoritative_bis_d_us_sdmx,
    parse_authoritative_bis_sdmx,
)

AUTHORITATIVE_TIMEOUT_SECONDS = 15
AUTHORITATIVE_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
AUTHORITATIVE_BIS_ROOT = Path("data/raw/candidate_b/bis/authoritative")
AUTHORITATIVE_D_US_REPRESENTATION = "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
AUTHORITATIVE_D_AU_REPRESENTATION = AUTHORITATIVE_D_US_REPRESENTATION
AUTHORITATIVE_D_CA_REPRESENTATION = AUTHORITATIVE_D_US_REPRESENTATION
AUTHORITATIVE_D_CH_REPRESENTATION = AUTHORITATIVE_D_US_REPRESENTATION
AUTHORITATIVE_D_GB_REPRESENTATION = AUTHORITATIVE_D_US_REPRESENTATION
AUTHORITATIVE_D_JP_REPRESENTATION = AUTHORITATIVE_D_US_REPRESENTATION
AUTHORITATIVE_D_XM_REPRESENTATION = AUTHORITATIVE_D_US_REPRESENTATION


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
class AuthoritativeBisHttpResponse:
    status_code: int
    final_url: str
    media_type: str
    headers: Mapping[str, str]
    raw_bytes: bytes

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise ValueError("transport response status is invalid")
        if not isinstance(self.final_url, str) or not self.final_url:
            raise ValueError("transport response final URL is required")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("transport response media type is required")
        if not isinstance(self.raw_bytes, bytes):
            raise ValueError("transport response bytes are required")
        normalized_headers = {
            str(key).lower(): str(value) for key, value in self.headers.items()
        }
        object.__setattr__(
            self,
            "headers",
            MappingProxyType(dict(sorted(normalized_headers.items()))),
        )


class AuthoritativeBisTransport(Protocol):
    def fetch(
        self,
        request: PolicyRateRequest,
        *,
        exact_url: str,
        accept: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> AuthoritativeBisHttpResponse: ...


class _NoAuthoritativeRedirects(HTTPRedirectHandler):
    def http_error_302(self, request, response, code, message, headers):
        del request, code, message, headers
        return response

    http_error_300 = http_error_302
    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


@dataclass(frozen=True)
class UrllibAuthoritativeBisTransport:
    opener: object | None = None

    def __post_init__(self) -> None:
        if self.opener is None:
            object.__setattr__(
                self,
                "opener",
                build_opener(_NoAuthoritativeRedirects()),
            )

    def fetch(
        self,
        request: PolicyRateRequest,
        *,
        exact_url: str,
        accept: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> AuthoritativeBisHttpResponse:
        del request
        http_request = Request(
            exact_url,
            headers={"Accept": accept},
            method="GET",
        )
        try:
            response = self.opener.open(http_request, timeout=timeout_seconds)
        except HTTPError as exc:
            response = exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("authoritative BIS request timed out") from exc
            raise
        with response:
            status_code = getattr(response, "status", None)
            if status_code is None:
                status_code = response.getcode()
            final_url = response.geturl()
            headers = {str(key): str(value) for key, value in response.headers.items()}
            content_type = next(
                (
                    value.split(";", 1)[0].strip().lower()
                    for key, value in headers.items()
                    if key.lower() == "content-type"
                ),
                "",
            )
            raw_bytes = response.read(max_response_bytes + 1)
        return AuthoritativeBisHttpResponse(
            status_code=status_code,
            final_url=final_url,
            media_type=content_type,
            headers=headers,
            raw_bytes=raw_bytes,
        )


def fetch_authoritative_d_us_response(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
) -> AuthoritativeBisHttpResponse:
    if request != authoritative_d_us_request():
        raise PolicyRateQualificationError("request_not_approved")
    try:
        response = transport.fetch(
            request,
            exact_url=AUTHORITATIVE_D_US_URL,
            accept=AUTHORITATIVE_D_US_ACCEPT,
            timeout_seconds=AUTHORITATIVE_TIMEOUT_SECONDS,
            max_response_bytes=AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    except TimeoutError as exc:
        raise PolicyRateQualificationError("acquisition_timeout") from exc
    except PolicyRateQualificationError:
        raise
    except Exception as exc:
        raise PolicyRateQualificationError("transport_failure") from exc
    if not isinstance(response, AuthoritativeBisHttpResponse):
        raise PolicyRateQualificationError("transport_response_invalid")
    if 300 <= response.status_code <= 399:
        raise PolicyRateQualificationError("redirect_rejected")
    if response.status_code != 200:
        raise PolicyRateQualificationError("http_status_not_success")
    if response.final_url != AUTHORITATIVE_D_US_URL:
        raise PolicyRateQualificationError("redirect_rejected")
    if response.media_type != "application/xml":
        raise PolicyRateQualificationError("media_type_not_approved")
    if len(response.raw_bytes) > AUTHORITATIVE_MAX_RESPONSE_BYTES:
        raise PolicyRateQualificationError("response_too_large")
    return response


def _fetch_authoritative_sparse_response(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    *,
    approved_request: PolicyRateRequest,
    exact_url: str,
    accept: str,
) -> AuthoritativeBisHttpResponse:
    if request != approved_request:
        raise PolicyRateQualificationError("request_not_approved")
    try:
        response = transport.fetch(
            request,
            exact_url=exact_url,
            accept=accept,
            timeout_seconds=AUTHORITATIVE_TIMEOUT_SECONDS,
            max_response_bytes=AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    except TimeoutError as exc:
        raise PolicyRateQualificationError("acquisition_timeout") from exc
    except PolicyRateQualificationError:
        raise
    except Exception as exc:
        raise PolicyRateQualificationError("transport_failure") from exc
    if not isinstance(response, AuthoritativeBisHttpResponse):
        raise PolicyRateQualificationError("transport_response_invalid")
    if 300 <= response.status_code <= 399:
        raise PolicyRateQualificationError("redirect_rejected")
    if response.status_code != 200:
        raise PolicyRateQualificationError("http_status_not_success")
    if response.final_url != exact_url:
        raise PolicyRateQualificationError("redirect_rejected")
    if response.media_type != "application/xml":
        raise PolicyRateQualificationError("media_type_not_approved")
    if len(response.raw_bytes) > AUTHORITATIVE_MAX_RESPONSE_BYTES:
        raise PolicyRateQualificationError("response_too_large")
    return response


def fetch_authoritative_d_au_response(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
) -> AuthoritativeBisHttpResponse:
    return _fetch_authoritative_sparse_response(
        request,
        transport,
        approved_request=authoritative_d_au_request(),
        exact_url=AUTHORITATIVE_D_AU_URL,
        accept=AUTHORITATIVE_D_AU_ACCEPT,
    )


def fetch_authoritative_d_ca_response(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
) -> AuthoritativeBisHttpResponse:
    return _fetch_authoritative_sparse_response(
        request,
        transport,
        approved_request=authoritative_d_ca_request(),
        exact_url=AUTHORITATIVE_D_CA_URL,
        accept=AUTHORITATIVE_D_CA_ACCEPT,
    )


def fetch_authoritative_d_ch_response(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
) -> AuthoritativeBisHttpResponse:
    return _fetch_authoritative_sparse_response(
        request,
        transport,
        approved_request=authoritative_d_ch_request(),
        exact_url=AUTHORITATIVE_D_CH_URL,
        accept=AUTHORITATIVE_D_CH_ACCEPT,
    )


def fetch_authoritative_d_xm_response(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
) -> AuthoritativeBisHttpResponse:
    return _fetch_authoritative_sparse_response(
        request,
        transport,
        approved_request=authoritative_d_xm_request(),
        exact_url=AUTHORITATIVE_D_XM_URL,
        accept=AUTHORITATIVE_D_XM_ACCEPT,
    )


def fetch_authoritative_d_gb_response(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
) -> AuthoritativeBisHttpResponse:
    return _fetch_authoritative_sparse_response(
        request,
        transport,
        approved_request=authoritative_d_gb_request(),
        exact_url=AUTHORITATIVE_D_GB_URL,
        accept=AUTHORITATIVE_D_GB_ACCEPT,
    )


def fetch_authoritative_d_jp_response(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
) -> AuthoritativeBisHttpResponse:
    return _fetch_authoritative_sparse_response(
        request,
        transport,
        approved_request=authoritative_d_jp_request(),
        exact_url=AUTHORITATIVE_D_JP_URL,
        accept=AUTHORITATIVE_D_JP_ACCEPT,
    )


@dataclass(frozen=True)
class AuthoritativeDUsManifest:
    request_fingerprint: str
    exact_url: str
    representation_identity: str
    series_key: str
    frequency: str
    reference_area: str
    unit_measure: str
    unit_mult: str
    status_semantics: tuple[str, ...]
    raw_sha256: str
    canonical_observation_hash: str
    raw_row_count: int
    numeric_observation_count: int
    min_observation_date: date
    max_observation_date: date
    retrieved_at: datetime
    response_media_type: str
    byte_count: int
    dataset_id: str
    manifest_id: str


@dataclass(frozen=True)
class AuthoritativeDUsPublication:
    destination: Path
    raw_path: Path
    manifest_path: Path
    manifest: AuthoritativeDUsManifest


@dataclass(frozen=True)
class AuthoritativeDAuManifest:
    request_fingerprint: str
    exact_url: str
    representation_identity: str
    series_key: str
    frequency: str
    reference_area: str
    unit_measure: str
    unit_mult: str
    status_semantics: tuple[str, ...]
    raw_sha256: str
    canonical_observation_hash: str
    raw_row_count: int
    numeric_observation_count: int
    min_observation_date: date
    max_observation_date: date
    retrieved_at: datetime
    response_media_type: str
    byte_count: int
    dataset_id: str
    manifest_id: str


@dataclass(frozen=True)
class AuthoritativeDAuPublication:
    destination: Path
    raw_path: Path
    manifest_path: Path
    manifest: AuthoritativeDAuManifest


@dataclass(frozen=True)
class AuthoritativeDCaManifest:
    request_fingerprint: str
    exact_url: str
    representation_identity: str
    series_key: str
    frequency: str
    reference_area: str
    unit_measure: str
    unit_mult: str
    status_semantics: tuple[str, ...]
    raw_sha256: str
    canonical_observation_hash: str
    raw_row_count: int
    numeric_observation_count: int
    min_observation_date: date
    max_observation_date: date
    retrieved_at: datetime
    response_media_type: str
    byte_count: int
    dataset_id: str
    manifest_id: str


@dataclass(frozen=True)
class AuthoritativeDCaPublication:
    destination: Path
    raw_path: Path
    manifest_path: Path
    manifest: AuthoritativeDCaManifest


@dataclass(frozen=True)
class AuthoritativeDChManifest:
    request_fingerprint: str
    exact_url: str
    representation_identity: str
    series_key: str
    frequency: str
    reference_area: str
    unit_measure: str
    unit_mult: str
    status_semantics: tuple[str, ...]
    raw_sha256: str
    canonical_observation_hash: str
    raw_row_count: int
    numeric_observation_count: int
    min_observation_date: date
    max_observation_date: date
    retrieved_at: datetime
    response_media_type: str
    byte_count: int
    dataset_id: str
    manifest_id: str


@dataclass(frozen=True)
class AuthoritativeDChPublication:
    destination: Path
    raw_path: Path
    manifest_path: Path
    manifest: AuthoritativeDChManifest


@dataclass(frozen=True)
class AuthoritativeDXmManifest:
    request_fingerprint: str
    exact_url: str
    representation_identity: str
    series_key: str
    frequency: str
    reference_area: str
    unit_measure: str
    unit_mult: str
    status_semantics: tuple[str, ...]
    raw_sha256: str
    canonical_observation_hash: str
    raw_row_count: int
    numeric_observation_count: int
    min_observation_date: date
    max_observation_date: date
    retrieved_at: datetime
    response_media_type: str
    byte_count: int
    dataset_id: str
    manifest_id: str


@dataclass(frozen=True)
class AuthoritativeDXmPublication:
    destination: Path
    raw_path: Path
    manifest_path: Path
    manifest: AuthoritativeDXmManifest


@dataclass(frozen=True)
class AuthoritativeDGbManifest:
    request_fingerprint: str
    exact_url: str
    representation_identity: str
    series_key: str
    frequency: str
    reference_area: str
    unit_measure: str
    unit_mult: str
    status_semantics: tuple[str, ...]
    raw_sha256: str
    canonical_observation_hash: str
    raw_row_count: int
    numeric_observation_count: int
    min_observation_date: date
    max_observation_date: date
    retrieved_at: datetime
    response_media_type: str
    byte_count: int
    dataset_id: str
    manifest_id: str


@dataclass(frozen=True)
class AuthoritativeDGbPublication:
    destination: Path
    raw_path: Path
    manifest_path: Path
    manifest: AuthoritativeDGbManifest


@dataclass(frozen=True)
class AuthoritativeDJpManifest:
    request_fingerprint: str
    exact_url: str
    representation_identity: str
    series_key: str
    frequency: str
    reference_area: str
    unit_measure: str
    unit_mult: str
    status_semantics: tuple[str, ...]
    raw_sha256: str
    canonical_observation_hash: str
    raw_row_count: int
    numeric_observation_count: int
    min_observation_date: date
    max_observation_date: date
    retrieved_at: datetime
    response_media_type: str
    byte_count: int
    dataset_id: str
    manifest_id: str


@dataclass(frozen=True)
class AuthoritativeDJpPublication:
    destination: Path
    raw_path: Path
    manifest_path: Path
    manifest: AuthoritativeDJpManifest


def _authoritative_d_us_manifest(
    request: PolicyRateRequest,
    response: AuthoritativeBisHttpResponse,
    retrieved_at: datetime,
) -> AuthoritativeDUsManifest:
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("retrieval timestamp must be timezone-aware")
    retrieved = retrieved_at.astimezone(UTC)
    observations = parse_authoritative_bis_d_us_sdmx(response.raw_bytes, request)
    raw_sha256 = hashlib.sha256(response.raw_bytes).hexdigest()
    observation_hash = canonical_sha256(observations)
    semantic = {
        "format": 1,
        "request_fingerprint": request.fingerprint,
        "exact_url": AUTHORITATIVE_D_US_URL,
        "representation_identity": AUTHORITATIVE_D_US_REPRESENTATION,
        "series_key": "D.US",
        "frequency": "D",
        "reference_area": "US",
        "unit_measure": "368",
        "unit_mult": "0",
        "status_semantics": AUTHORITATIVE_BIS_RAW_STATUS_SEMANTICS,
        "raw_sha256": raw_sha256,
        "canonical_observation_hash": observation_hash,
        "raw_row_count": len(observations),
        "numeric_observation_count": len(observations),
        "min_observation_date": observations[0].observation_date,
        "max_observation_date": observations[-1].observation_date,
    }
    dataset_id = canonical_sha256(semantic)
    audit = {
        "format": 1,
        "dataset_id": dataset_id,
        "retrieved_at": retrieved,
        "returned_url": response.final_url,
        "response_media_type": response.media_type,
        "byte_count": len(response.raw_bytes),
        "headers": response.headers,
    }
    manifest_values = dict(semantic)
    del manifest_values["format"]
    return AuthoritativeDUsManifest(
        **manifest_values,
        retrieved_at=retrieved,
        response_media_type=response.media_type,
        byte_count=len(response.raw_bytes),
        dataset_id=dataset_id,
        manifest_id=canonical_sha256(audit),
    )


def _write_fully(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _authoritative_sparse_manifest_values(
    request: PolicyRateRequest,
    response: AuthoritativeBisHttpResponse,
    retrieved_at: datetime,
    *,
    exact_url: str,
    representation_identity: str,
    series_key: str,
    reference_area: str,
) -> dict[str, object]:
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("retrieval timestamp must be timezone-aware")
    retrieved = retrieved_at.astimezone(UTC)
    parsed = parse_authoritative_bis_sdmx(response.raw_bytes, request)
    observations = parsed.observations
    raw_sha256 = hashlib.sha256(response.raw_bytes).hexdigest()
    observation_hash = canonical_sha256(observations)
    semantic = {
        "format": 1,
        "request_fingerprint": request.fingerprint,
        "exact_url": exact_url,
        "representation_identity": representation_identity,
        "series_key": series_key,
        "frequency": "D",
        "reference_area": reference_area,
        "unit_measure": "368",
        "unit_mult": "0",
        "status_semantics": AUTHORITATIVE_BIS_RAW_STATUS_SEMANTICS,
        "raw_sha256": raw_sha256,
        "canonical_observation_hash": observation_hash,
        "raw_row_count": parsed.raw_row_count,
        "numeric_observation_count": parsed.numeric_observation_count,
        "min_observation_date": observations[0].observation_date,
        "max_observation_date": observations[-1].observation_date,
    }
    dataset_id = canonical_sha256(semantic)
    audit = {
        "format": 1,
        "dataset_id": dataset_id,
        "retrieved_at": retrieved,
        "returned_url": response.final_url,
        "response_media_type": response.media_type,
        "byte_count": len(response.raw_bytes),
        "headers": response.headers,
    }
    manifest_values = dict(semantic)
    del manifest_values["format"]
    return {
        **manifest_values,
        "retrieved_at": retrieved,
        "response_media_type": response.media_type,
        "byte_count": len(response.raw_bytes),
        "dataset_id": dataset_id,
        "manifest_id": canonical_sha256(audit),
    }


def _authoritative_d_au_manifest(
    request: PolicyRateRequest,
    response: AuthoritativeBisHttpResponse,
    retrieved_at: datetime,
) -> AuthoritativeDAuManifest:
    return AuthoritativeDAuManifest(
        **_authoritative_sparse_manifest_values(
            request,
            response,
            retrieved_at,
            exact_url=AUTHORITATIVE_D_AU_URL,
            representation_identity=AUTHORITATIVE_D_AU_REPRESENTATION,
            series_key="D.AU",
            reference_area="AU",
        )
    )


def _authoritative_d_ca_manifest(
    request: PolicyRateRequest,
    response: AuthoritativeBisHttpResponse,
    retrieved_at: datetime,
) -> AuthoritativeDCaManifest:
    return AuthoritativeDCaManifest(
        **_authoritative_sparse_manifest_values(
            request,
            response,
            retrieved_at,
            exact_url=AUTHORITATIVE_D_CA_URL,
            representation_identity=AUTHORITATIVE_D_CA_REPRESENTATION,
            series_key="D.CA",
            reference_area="CA",
        )
    )


def _authoritative_d_ch_manifest(
    request: PolicyRateRequest,
    response: AuthoritativeBisHttpResponse,
    retrieved_at: datetime,
) -> AuthoritativeDChManifest:
    return AuthoritativeDChManifest(
        **_authoritative_sparse_manifest_values(
            request,
            response,
            retrieved_at,
            exact_url=AUTHORITATIVE_D_CH_URL,
            representation_identity=AUTHORITATIVE_D_CH_REPRESENTATION,
            series_key="D.CH",
            reference_area="CH",
        )
    )


def _authoritative_d_xm_manifest(
    request: PolicyRateRequest,
    response: AuthoritativeBisHttpResponse,
    retrieved_at: datetime,
) -> AuthoritativeDXmManifest:
    return AuthoritativeDXmManifest(
        **_authoritative_sparse_manifest_values(
            request,
            response,
            retrieved_at,
            exact_url=AUTHORITATIVE_D_XM_URL,
            representation_identity=AUTHORITATIVE_D_XM_REPRESENTATION,
            series_key="D.XM",
            reference_area="XM",
        )
    )


def _authoritative_d_gb_manifest(
    request: PolicyRateRequest,
    response: AuthoritativeBisHttpResponse,
    retrieved_at: datetime,
) -> AuthoritativeDGbManifest:
    return AuthoritativeDGbManifest(
        **_authoritative_sparse_manifest_values(
            request,
            response,
            retrieved_at,
            exact_url=AUTHORITATIVE_D_GB_URL,
            representation_identity=AUTHORITATIVE_D_GB_REPRESENTATION,
            series_key="D.GB",
            reference_area="GB",
        )
    )


def _authoritative_d_jp_manifest(
    request: PolicyRateRequest,
    response: AuthoritativeBisHttpResponse,
    retrieved_at: datetime,
) -> AuthoritativeDJpManifest:
    return AuthoritativeDJpManifest(
        **_authoritative_sparse_manifest_values(
            request,
            response,
            retrieved_at,
            exact_url=AUTHORITATIVE_D_JP_URL,
            representation_identity=AUTHORITATIVE_D_JP_REPRESENTATION,
            series_key="D.JP",
            reference_area="JP",
        )
    )


def acquire_and_publish_authoritative_d_us(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    retrieved_at: datetime,
) -> AuthoritativeDUsPublication:
    if request != authoritative_d_us_request():
        raise PolicyRateQualificationError("request_not_approved")
    destination = AUTHORITATIVE_BIS_ROOT / f"d_us-{request.fingerprint}"
    if destination.exists():
        raise PolicyRateQualificationError("destination_exists")

    response = fetch_authoritative_d_us_response(request, transport)
    manifest = _authoritative_d_us_manifest(request, response, retrieved_at)

    AUTHORITATIVE_BIS_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".tmp-", dir=AUTHORITATIVE_BIS_ROOT)
    )
    try:
        raw_path = temporary / "response.xml"
        manifest_path = temporary / "manifest.json"
        _write_fully(raw_path, response.raw_bytes)
        _write_fully(manifest_path, canonical_json(manifest).encode("utf-8"))
        if destination.exists():
            raise PolicyRateQualificationError("destination_exists")
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return AuthoritativeDUsPublication(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=manifest,
    )


def _publish_authoritative_sparse_series(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    retrieved_at: datetime,
    *,
    approved_request: PolicyRateRequest,
    destination_slug: str,
    fetch_response: Callable[
        [PolicyRateRequest, AuthoritativeBisTransport], AuthoritativeBisHttpResponse
    ],
    build_manifest: Callable[
        [PolicyRateRequest, AuthoritativeBisHttpResponse, datetime],
        AuthoritativeDAuManifest
        | AuthoritativeDCaManifest
        | AuthoritativeDChManifest
        | AuthoritativeDGbManifest
        | AuthoritativeDJpManifest
        | AuthoritativeDXmManifest,
    ],
) -> tuple[
    Path,
    Path,
    Path,
    AuthoritativeDAuManifest
    | AuthoritativeDCaManifest
    | AuthoritativeDChManifest
    | AuthoritativeDGbManifest
    | AuthoritativeDJpManifest
    | AuthoritativeDXmManifest,
]:
    if request != approved_request:
        raise PolicyRateQualificationError("request_not_approved")
    destination = AUTHORITATIVE_BIS_ROOT / f"{destination_slug}-{request.fingerprint}"
    if destination.exists():
        raise PolicyRateQualificationError("destination_exists")

    response = fetch_response(request, transport)
    manifest = build_manifest(request, response, retrieved_at)

    AUTHORITATIVE_BIS_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".tmp-", dir=AUTHORITATIVE_BIS_ROOT))
    try:
        raw_path = temporary / "response.xml"
        manifest_path = temporary / "manifest.json"
        _write_fully(raw_path, response.raw_bytes)
        _write_fully(manifest_path, canonical_json(manifest).encode("utf-8"))
        if destination.exists():
            raise PolicyRateQualificationError("destination_exists")
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return (
        destination,
        destination / "response.xml",
        destination / "manifest.json",
        manifest,
    )


def acquire_and_publish_authoritative_d_au(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    retrieved_at: datetime,
) -> AuthoritativeDAuPublication:
    destination, raw_path, manifest_path, manifest = _publish_authoritative_sparse_series(
        request,
        transport,
        retrieved_at,
        approved_request=authoritative_d_au_request(),
        destination_slug="d_au",
        fetch_response=fetch_authoritative_d_au_response,
        build_manifest=_authoritative_d_au_manifest,
    )
    if not isinstance(manifest, AuthoritativeDAuManifest):
        raise TypeError("D.AU manifest type mismatch")
    return AuthoritativeDAuPublication(
        destination=destination,
        raw_path=raw_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def acquire_and_publish_authoritative_d_ca(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    retrieved_at: datetime,
) -> AuthoritativeDCaPublication:
    destination, raw_path, manifest_path, manifest = _publish_authoritative_sparse_series(
        request,
        transport,
        retrieved_at,
        approved_request=authoritative_d_ca_request(),
        destination_slug="d_ca",
        fetch_response=fetch_authoritative_d_ca_response,
        build_manifest=_authoritative_d_ca_manifest,
    )
    if not isinstance(manifest, AuthoritativeDCaManifest):
        raise TypeError("D.CA manifest type mismatch")
    return AuthoritativeDCaPublication(
        destination=destination,
        raw_path=raw_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def acquire_and_publish_authoritative_d_ch(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    retrieved_at: datetime,
) -> AuthoritativeDChPublication:
    destination, raw_path, manifest_path, manifest = _publish_authoritative_sparse_series(
        request,
        transport,
        retrieved_at,
        approved_request=authoritative_d_ch_request(),
        destination_slug="d_ch",
        fetch_response=fetch_authoritative_d_ch_response,
        build_manifest=_authoritative_d_ch_manifest,
    )
    if not isinstance(manifest, AuthoritativeDChManifest):
        raise TypeError("D.CH manifest type mismatch")
    return AuthoritativeDChPublication(
        destination=destination,
        raw_path=raw_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def acquire_and_publish_authoritative_d_xm(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    retrieved_at: datetime,
) -> AuthoritativeDXmPublication:
    destination, raw_path, manifest_path, manifest = _publish_authoritative_sparse_series(
        request,
        transport,
        retrieved_at,
        approved_request=authoritative_d_xm_request(),
        destination_slug="d_xm",
        fetch_response=fetch_authoritative_d_xm_response,
        build_manifest=_authoritative_d_xm_manifest,
    )
    if not isinstance(manifest, AuthoritativeDXmManifest):
        raise TypeError("D.XM manifest type mismatch")
    return AuthoritativeDXmPublication(
        destination=destination,
        raw_path=raw_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def acquire_and_publish_authoritative_d_gb(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    retrieved_at: datetime,
) -> AuthoritativeDGbPublication:
    destination, raw_path, manifest_path, manifest = _publish_authoritative_sparse_series(
        request,
        transport,
        retrieved_at,
        approved_request=authoritative_d_gb_request(),
        destination_slug="d_gb",
        fetch_response=fetch_authoritative_d_gb_response,
        build_manifest=_authoritative_d_gb_manifest,
    )
    if not isinstance(manifest, AuthoritativeDGbManifest):
        raise TypeError("D.GB manifest type mismatch")
    return AuthoritativeDGbPublication(
        destination=destination,
        raw_path=raw_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def acquire_and_publish_authoritative_d_jp(
    request: PolicyRateRequest,
    transport: AuthoritativeBisTransport,
    retrieved_at: datetime,
) -> AuthoritativeDJpPublication:
    destination, raw_path, manifest_path, manifest = _publish_authoritative_sparse_series(
        request,
        transport,
        retrieved_at,
        approved_request=authoritative_d_jp_request(),
        destination_slug="d_jp",
        fetch_response=fetch_authoritative_d_jp_response,
        build_manifest=_authoritative_d_jp_manifest,
    )
    if not isinstance(manifest, AuthoritativeDJpManifest):
        raise TypeError("D.JP manifest type mismatch")
    return AuthoritativeDJpPublication(
        destination=destination,
        raw_path=raw_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--authorize-network-acquisition",
        action="store_true",
    )
    parser.add_argument(
        "--target",
        choices=("d_us", "d_au", "d_ca", "d_ch", "d_xm", "d_gb", "d_jp"),
    )
    args = parser.parse_args([] if argv is None else argv)

    if not args.authorize_network_acquisition:
        raise SystemExit("network_acquisition_not_authorized")

    if args.target is None:
        parser.error("--target is required")

    if args.target == "d_us":
        publication = acquire_and_publish_authoritative_d_us(
            authoritative_d_us_request(),
            UrllibAuthoritativeBisTransport(),
            datetime.now(UTC),
        )
    elif args.target == "d_au":
        publication = acquire_and_publish_authoritative_d_au(
            authoritative_d_au_request(),
            UrllibAuthoritativeBisTransport(),
            datetime.now(UTC),
        )
    elif args.target == "d_ca":
        publication = acquire_and_publish_authoritative_d_ca(
            authoritative_d_ca_request(),
            UrllibAuthoritativeBisTransport(),
            datetime.now(UTC),
        )
    elif args.target == "d_ch":
        publication = acquire_and_publish_authoritative_d_ch(
            authoritative_d_ch_request(),
            UrllibAuthoritativeBisTransport(),
            datetime.now(UTC),
        )
    elif args.target == "d_xm":
        publication = acquire_and_publish_authoritative_d_xm(
            authoritative_d_xm_request(),
            UrllibAuthoritativeBisTransport(),
            datetime.now(UTC),
        )
    elif args.target == "d_gb":
        publication = acquire_and_publish_authoritative_d_gb(
            authoritative_d_gb_request(),
            UrllibAuthoritativeBisTransport(),
            datetime.now(UTC),
        )
    else:
        publication = acquire_and_publish_authoritative_d_jp(
            authoritative_d_jp_request(),
            UrllibAuthoritativeBisTransport(),
            datetime.now(UTC),
        )

    print(f"destination={publication.destination}")
    print(f"raw_path={publication.raw_path}")
    print(f"manifest_path={publication.manifest_path}")
    print(f"dataset_id={publication.manifest.dataset_id}")
    print(f"manifest_id={publication.manifest.manifest_id}")


if __name__ == "__main__":
    main(sys.argv[1:])
