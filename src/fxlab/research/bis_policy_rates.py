"""BIS central-bank policy-rate evidence acquisition and normalization layer (ADR 0014).

This module implements the frozen data acquisition and normalization contract for Bank for
International Settlements (BIS) central-bank policy rates under ADR 0014.

Governance invariants:
- Candidate E remains NOT SELECTED.
- Performance-test slot count remains 0 / 4 under ADR 0013.
- Sealed test partition (2024+) is strictly excluded.
- Contains NO policy differential, FX return, portfolio, or performance calculations.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

# Frozen ADR 0014 contract constants
BIS_DATASET_IDENTIFIER = "BIS:WS_CBPOL(1.0)"
BIS_DATAFLOW = "WS_CBPOL"
BIS_DATAFLOW_VERSION = "1.0"
BIS_AGENCY = "BIS"
BIS_FREQUENCY = "D"

# Exactly eight canonical series in fixed alphabetical order
FROZEN_SERIES_KEYS: tuple[str, ...] = (
    "D.AU",
    "D.CA",
    "D.CH",
    "D.GB",
    "D.JP",
    "D.NZ",
    "D.US",
    "D.XM",
)

SERIES_TO_CURRENCY: Mapping[str, str] = MappingProxyType(
    {
        "D.AU": "AUD",
        "D.CA": "CAD",
        "D.CH": "CHF",
        "D.GB": "GBP",
        "D.JP": "JPY",
        "D.NZ": "NZD",
        "D.US": "USD",
        "D.XM": "EUR",
    }
)

CURRENCY_TO_SERIES: Mapping[str, str] = MappingProxyType(
    {v: k for k, v in SERIES_TO_CURRENCY.items()}
)

START_INCLUSIVE: date = date(2014, 1, 1)
END_EXCLUSIVE: date = date(2024, 1, 1)
SOURCE_REQUEST_END_INCLUSIVE: date = date(2023, 12, 31)
INITIALIZATION_END_INCLUSIVE: date = START_INCLUSIVE - timedelta(days=1)
INITIALIZATION_CONTRACT_VERSION = "bis_cbpol_boundary_initialization_v1"
INITIALIZATION_ACQUISITION_RULE = "endPeriod=start_minus_one_day,lastNObservations=1"

NORMALIZATION_VERSION = "bis_cbpol_daily_v3"

BIS_SDMX_API_BASE_URL = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0"
BIS_SDMX_XML_ACCEPT = "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
BIS_SDMX_CSV_ACCEPT = "application/vnd.sdmx.data+csv;version=2.0.0"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class RateState(StrEnum):
    """Observation state of a policy rate."""

    OBSERVED = "OBSERVED"
    RATE_PERSISTS = "RATE_PERSISTS"
    MISSING_OR_UNKNOWN = "MISSING_OR_UNKNOWN"


class SourceObservationKind(StrEnum):
    """Source classification of raw observation."""

    NUMERIC = "NUMERIC"
    MISSING = "MISSING"


class PolicyRateStateOrigin(StrEnum):
    """Origin of the policy-rate state."""

    OBSERVED = "OBSERVED"
    PERSISTED = "PERSISTED"


def _primitive(value: object) -> object:
    """Recursively convert objects to JSON-serializable primitives in deterministic order."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite values are not canonical")
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical mappings require string keys")
        return {key: _primitive(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list, frozenset, set)):
        items = [_primitive(item) for item in value]
        if isinstance(value, (frozenset, set)):
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return items
    if hasattr(value, "__dataclass_fields__"):
        return {
            field_name: _primitive(getattr(value, field_name))
            for field_name in sorted(value.__dataclass_fields__)
        }
    raise ValueError(f"unsupported canonical type: {type(value)}")


def canonical_json(value: object) -> str:
    """Deterministic JSON serialization with sorted keys and compact separators."""
    return json.dumps(_primitive(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_sha256(value: object) -> str:
    """Cryptographic SHA-256 hash over canonical JSON serialization."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_bis_series_request_url(
    series_key: str,
    *,
    start_inclusive: date = START_INCLUSIVE,
    end_inclusive: date = SOURCE_REQUEST_END_INCLUSIVE,
) -> str:
    """Construct a bounded, immutable request URL for one frozen BIS policy rate series.

    Fails closed if the series is not one of the eight frozen series, or if the date bounds
    deviate from ADR 0014 constants.
    """
    if series_key not in FROZEN_SERIES_KEYS:
        raise ValueError(f"unsupported series_key {series_key}; must be in {FROZEN_SERIES_KEYS}")
    if start_inclusive != START_INCLUSIVE:
        raise ValueError(f"start_inclusive must be {START_INCLUSIVE}, got {start_inclusive}")
    if end_inclusive != SOURCE_REQUEST_END_INCLUSIVE:
        raise ValueError(
            f"end_inclusive must be {SOURCE_REQUEST_END_INCLUSIVE}, got {end_inclusive}"
        )
    return (
        f"{BIS_SDMX_API_BASE_URL}/{series_key}"
        f"?startPeriod={start_inclusive.isoformat()}&endPeriod={end_inclusive.isoformat()}"
    )


def build_bis_all_series_request_urls() -> dict[str, str]:
    """Construct all eight bounded series request URLs for ADR 0014."""
    return {
        series_key: build_bis_series_request_url(series_key) for series_key in FROZEN_SERIES_KEYS
    }


def build_bis_initialization_request_url(series_key: str) -> str:
    """Build the frozen direct-predecessor query from ADR 0016."""
    if series_key not in FROZEN_SERIES_KEYS:
        raise ValueError(f"unsupported series_key {series_key}; must be in {FROZEN_SERIES_KEYS}")
    return (
        f"{BIS_SDMX_API_BASE_URL}/{series_key}"
        f"?endPeriod={INITIALIZATION_END_INCLUSIVE.isoformat()}&lastNObservations=1"
    )

class BisObservationValidationError(ValueError):
    """Structured fail-closed error capturing diagnostic context on validation failure."""

    def __init__(
        self,
        reason: str,
        *,
        series_key: str,
        time_period: str,
        raw_obs_value: str,
        obs_status: str | None = None,
        obs_conf: str | None = None,
        obs_pre_break: str | None = None,
    ) -> None:
        self.reason = reason
        self.series_key = series_key
        self.time_period = time_period
        self.raw_obs_value = raw_obs_value
        self.obs_status = obs_status
        self.obs_conf = obs_conf
        self.obs_pre_break = obs_pre_break

        parts = [
            f"{reason}:",
            f"series={series_key}",
            f"time_period={time_period}",
            f"raw_obs_value={raw_obs_value}",
        ]
        if obs_status is not None:
            parts.append(f"obs_status={obs_status}")
        if obs_conf is not None:
            parts.append(f"obs_conf={obs_conf}")
        if obs_pre_break is not None:
            parts.append(f"obs_pre_break={obs_pre_break}")

        message = " ".join(parts)
        super().__init__(message)


@dataclass(frozen=True, order=True)
class PolicyRateRecord:
    """A single normalized daily policy-rate observation under ADR 0014 / ADR 0015."""

    observation_date: date
    series_key: str
    currency: str
    policy_rate_state: Decimal
    source_observation_kind: str = SourceObservationKind.NUMERIC
    source_obs_value: str = ""
    source_obs_status: str = "A"
    policy_rate_state_origin: str = PolicyRateStateOrigin.OBSERVED
    source_state_date: date | None = None
    point_in_time_status: str = "UNRESOLVED"
    obs_conf: str | None = None
    obs_pre_break: str | None = None

    def __init__(
        self,
        observation_date: date,
        series_key: str,
        currency: str,
        policy_rate_state: Decimal | None = None,
        source_observation_kind: str = SourceObservationKind.NUMERIC,
        source_obs_value: str = "",
        source_obs_status: str = "A",
        policy_rate_state_origin: str = PolicyRateStateOrigin.OBSERVED,
        source_state_date: date | None = None,
        point_in_time_status: str = "UNRESOLVED",
        obs_conf: str | None = None,
        obs_pre_break: str | None = None,
        rate_value: Decimal | None = None,
    ) -> None:
        effective_rate = policy_rate_state if policy_rate_state is not None else rate_value
        if effective_rate is None:
            raise ValueError("policy_rate_state must be provided")

        object.__setattr__(self, "observation_date", observation_date)
        object.__setattr__(self, "series_key", series_key)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "policy_rate_state", effective_rate)
        object.__setattr__(self, "source_observation_kind", str(source_observation_kind))
        object.__setattr__(self, "source_obs_value", str(source_obs_value))
        object.__setattr__(self, "source_obs_status", str(source_obs_status))
        object.__setattr__(self, "policy_rate_state_origin", str(policy_rate_state_origin))
        object.__setattr__(
            self,
            "source_state_date",
            source_state_date if source_state_date is not None else observation_date,
        )
        object.__setattr__(self, "point_in_time_status", point_in_time_status)
        object.__setattr__(self, "obs_conf", obs_conf)
        object.__setattr__(self, "obs_pre_break", obs_pre_break)

        if self.series_key not in FROZEN_SERIES_KEYS:
            raise ValueError(f"unsupported series_key: {self.series_key}")
        expected_currency = SERIES_TO_CURRENCY[self.series_key]
        if self.currency != expected_currency:
            raise ValueError(
                f"currency mismatch for {self.series_key}: expected {expected_currency}, "
                f"got {self.currency}"
            )
        if not isinstance(self.observation_date, date):
            raise ValueError("observation_date must be a date")
        if self.observation_date < START_INCLUSIVE or self.observation_date >= END_EXCLUSIVE:
            raise ValueError(
                f"observation_date {self.observation_date} outside frozen interval "
                f"[{START_INCLUSIVE}, {END_EXCLUSIVE})"
            )
        if (
            not isinstance(self.policy_rate_state, Decimal)
            or not self.policy_rate_state.is_finite()
        ):
            raise ValueError(
                f"policy_rate_state must be a finite Decimal, got {self.policy_rate_state}"
            )
        if self.source_observation_kind not in (
            SourceObservationKind.NUMERIC,
            SourceObservationKind.MISSING,
        ):
            raise ValueError(
                f"invalid source_observation_kind: {self.source_observation_kind}"
            )
        if self.policy_rate_state_origin not in (
            PolicyRateStateOrigin.OBSERVED,
            PolicyRateStateOrigin.PERSISTED,
        ):
            raise ValueError(
                f"invalid policy_rate_state_origin: {self.policy_rate_state_origin}"
            )

    @property
    def rate_value(self) -> Decimal:
        """Backward-compatible alias for policy_rate_state."""
        return self.policy_rate_state

    @property
    def obs_status(self) -> str:
        """Backward-compatible alias for source_obs_status."""
        return self.source_obs_status


@dataclass(frozen=True)
class BisRawArtifact:
    """Immutable raw evidence payload preserving exact provenance."""

    source_institution: str = "Bank for International Settlements (BIS)"
    dataset_identifier: str = BIS_DATASET_IDENTIFIER
    dataset_version: str = BIS_DATAFLOW_VERSION
    frequency: str = BIS_FREQUENCY
    requested_series: tuple[str, ...] = FROZEN_SERIES_KEYS
    start_inclusive: str = "2014-01-01"
    end_exclusive: str = "2024-01-01"
    content_format: str = "sdmx-xml"
    raw_byte_count: int = 0
    raw_sha256: str = ""
    series_payloads: tuple[tuple[str, bytes], ...] = ()
    acquisition_timestamp: str = ""  # Transient audit metadata only

    def __post_init__(self) -> None:
        if tuple(sorted(self.requested_series)) != FROZEN_SERIES_KEYS:
            raise ValueError("requested_series must contain exactly all eight frozen series")
        if self.start_inclusive != "2014-01-01" or self.end_exclusive != "2024-01-01":
            raise ValueError("time bounds must be exactly [2014-01-01, 2024-01-01)")
        if not self.raw_sha256 or not _SHA_RE.fullmatch(self.raw_sha256):
            raise ValueError("raw_sha256 must be a valid lowercase SHA-256 hash")
        if self.raw_byte_count <= 0:
            raise ValueError("raw_byte_count must be positive")


def compute_raw_evidence_identity(raw: BisRawArtifact) -> str:
    """Calculate the deterministic raw evidence identity binding contract metadata
    and raw_sha256.
    """
    payload = {
        "source_institution": raw.source_institution,
        "dataset_identifier": raw.dataset_identifier,
        "dataset_version": raw.dataset_version,
        "frequency": raw.frequency,
        "requested_series": sorted(raw.requested_series),
        "start_inclusive": raw.start_inclusive,
        "end_exclusive": raw.end_exclusive,
        "content_format": raw.content_format,
        "raw_byte_count": raw.raw_byte_count,
        "raw_sha256": raw.raw_sha256,
    }
    return canonical_sha256(payload)


@dataclass(frozen=True)
class BisInitializationEvidence:
    """Immutable same-series predecessor evidence under ADR 0016."""

    series_key: str
    raw_byte_count: int
    raw_sha256: str
    raw_payload: bytes
    selected_predecessor_date: date
    selected_predecessor_value: Decimal
    source_obs_status: str
    obs_conf: str | None = None
    obs_pre_break: str | None = None
    provider: str = "Bank for International Settlements (BIS)"
    dataset_identifier: str = BIS_DATASET_IDENTIFIER
    dataset_version: str = BIS_DATAFLOW_VERSION
    initialization_contract_version: str = INITIALIZATION_CONTRACT_VERSION
    acquisition_rule: str = INITIALIZATION_ACQUISITION_RULE
    end_inclusive: str = "2013-12-31"
    last_n_observations: int = 1
    start_inclusive: str = "2014-01-01"
    research_end_exclusive: str = "2024-01-01"
    normalization_version: str = NORMALIZATION_VERSION

    def __post_init__(self) -> None:
        if self.series_key not in FROZEN_SERIES_KEYS:
            raise ValueError(f"unsupported initialization series: {self.series_key}")
        if self.raw_byte_count <= 0 or not _SHA_RE.fullmatch(self.raw_sha256):
            raise ValueError("initialization raw byte count/hash invalid")
        if self.raw_byte_count != len(self.raw_payload):
            raise ValueError("initialization raw byte count mismatch")
        if self.raw_sha256 != hashlib.sha256(self.raw_payload).hexdigest():
            raise ValueError("initialization raw SHA-256 mismatch")
        if self.selected_predecessor_date >= START_INCLUSIVE:
            raise ValueError("initialization predecessor must be strictly before START_INCLUSIVE")
        if not self.selected_predecessor_value.is_finite():
            raise ValueError("initialization predecessor must be finite")
        if self.end_inclusive != INITIALIZATION_END_INCLUSIVE.isoformat():
            raise ValueError("initialization end bound mismatch")
        if self.last_n_observations != 1:
            raise ValueError("initialization must request exactly lastNObservations=1")
        if self.normalization_version != NORMALIZATION_VERSION:
            raise ValueError("initialization normalization version mismatch")


def compute_initialization_evidence_identity(evidence: BisInitializationEvidence) -> str:
    """Hash every decision-relevant initialization evidence field."""
    return canonical_sha256(evidence)

@dataclass(frozen=True)
class BisNormalizedDataset:
    """Normalized, validated, and deterministically ordered BIS policy rates dataset."""

    schema: str = NORMALIZATION_VERSION
    series_keys: tuple[str, ...] = FROZEN_SERIES_KEYS
    start_inclusive: str = "2014-01-01"
    end_exclusive: str = "2024-01-01"
    records: tuple[PolicyRateRecord, ...] = ()
    record_count: int = 0
    raw_evidence_identity: str = ""
    initialization_evidence_identities: tuple[tuple[str, str], ...] = ()
    point_in_time_status: str = "UNRESOLVED"
    normalized_identity: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != NORMALIZATION_VERSION:
            raise ValueError(f"schema must be {NORMALIZATION_VERSION}")
        if tuple(sorted(self.series_keys)) != FROZEN_SERIES_KEYS:
            raise ValueError("series_keys must match frozen eight series")
        if (
            tuple(key for key, _ in self.initialization_evidence_identities)
            != FROZEN_SERIES_KEYS
        ):
            raise ValueError(
                "initialization evidence identities must cover frozen series in order"
            )
        if any(
            not _SHA_RE.fullmatch(identity)
            for _, identity in self.initialization_evidence_identities
        ):
            raise ValueError(
                "initialization evidence identity must be a lowercase SHA-256"
            )
        if len(self.records) != self.record_count:
            raise ValueError(
                f"record_count mismatch: {len(self.records)} != {self.record_count}"
            )
        # Verify deterministic sorted ordering (date ascending, then series_key ascending)
        for prev, curr in zip(self.records, self.records[1:], strict=False):
            if (prev.observation_date, prev.series_key) >= (
                curr.observation_date,
                curr.series_key,
            ):
                raise ValueError(
                    "records must be strictly sorted by (observation_date, series_key)"
                )

        identity = compute_normalized_evidence_identity(self)
        if self.normalized_identity and self.normalized_identity != identity:
            raise ValueError("normalized_identity does not match dataset content")
        object.__setattr__(self, "normalized_identity", identity)


def compute_normalized_evidence_identity(dataset: BisNormalizedDataset) -> str:
    """Calculate the deterministic normalized dataset identity."""
    payload = {
        "schema": dataset.schema,
        "series_keys": sorted(dataset.series_keys),
        "start_inclusive": dataset.start_inclusive,
        "end_exclusive": dataset.end_exclusive,
        "record_count": dataset.record_count,
        "raw_evidence_identity": dataset.raw_evidence_identity,
        "initialization_evidence_identities": dataset.initialization_evidence_identities,
        "point_in_time_status": dataset.point_in_time_status,
        "records": [
            {
                "observation_date": r.observation_date.isoformat(),
                "series_key": r.series_key,
                "currency": r.currency,
                "source_observation_kind": r.source_observation_kind,
                "source_obs_value": r.source_obs_value,
                "source_obs_status": r.source_obs_status,
                "policy_rate_state": format(r.policy_rate_state, "f"),
                "policy_rate_state_origin": r.policy_rate_state_origin,
                "source_state_date": (
                    r.source_state_date.isoformat()
                    if r.source_state_date
                    else r.observation_date.isoformat()
                ),
                "point_in_time_status": r.point_in_time_status,
                "obs_conf": r.obs_conf,
                "obs_pre_break": r.obs_pre_break,
            }
            for r in dataset.records
        ],
    }
    return canonical_sha256(payload)


def _parse_decimal(value_str: str) -> Decimal:
    """Parse exact decimal rate value, rejecting non-finite and malformed values."""
    if not isinstance(value_str, str) or not value_str.strip():
        raise ValueError("empty or non-string rate value")
    cleaned = value_str.strip()
    if cleaned.lower() in ("nan", "inf", "-inf", "+inf", "null", "none"):
        raise ValueError(f"non-finite rate value: {value_str}")
    try:
        val = Decimal(cleaned)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"malformed decimal rate value: {value_str}") from exc
    if not val.is_finite():
        raise ValueError(f"non-finite decimal rate value: {value_str}")
    return val


RawObsTuple = tuple[date, str, str, str | None, str | None, str | None]


def _normalize_series_observations(
    series_key: str,
    raw_obs_list: list[RawObsTuple],
    initialization_evidence: BisInitializationEvidence | None = None,
) -> list[PolicyRateRecord]:
    """Normalize and deterministically persist policy-rate states for a single series.

    Under ADR 0015:
    - Finite numeric observations establish an OBSERVED state and update the active state.
    - Missing observations (OBS_VALUE="NaN", OBS_STATUS="M") inherit the prior established
      finite state as PERSISTED with the original source_state_date.
    - Fails closed on leading missing observations, non-M NaNs, absent status NaNs,
      malformed values, duplicate dates, or out-of-range observations.
    """
    if series_key not in FROZEN_SERIES_KEYS:
        raise ValueError(f"unknown series: {series_key}")
    currency = SERIES_TO_CURRENCY[series_key]

    seen_dates: dict[date, str] = {}
    for (
        obs_date,
        _time_period_str,
        raw_obs_val_str,
        _obs_status,
        _obs_conf,
        _obs_pre_break,
    ) in raw_obs_list:
        if obs_date in seen_dates:
            prev_val = seen_dates[obs_date]
            if prev_val != raw_obs_val_str:
                raise ValueError(
                    f"conflicting duplicate observation for ('{series_key}', {obs_date}): "
                    f"{prev_val} vs {raw_obs_val_str}"
                )
            raise ValueError(
                f"duplicate observation for ('{series_key}', {obs_date}): "
                f"series {series_key} has duplicate observation on {obs_date}"
            )
        seen_dates[obs_date] = raw_obs_val_str

    sorted_obs = sorted(raw_obs_list, key=lambda x: x[0])

    records: list[PolicyRateRecord] = []
    if initialization_evidence is not None and initialization_evidence.series_key != series_key:
        raise ValueError("initialization evidence series mismatch")
    last_finite_state = (
        initialization_evidence.selected_predecessor_value
        if initialization_evidence is not None
        else None
    )
    last_finite_date = (
        initialization_evidence.selected_predecessor_date
        if initialization_evidence is not None
        else None
    )

    for (
        obs_date,
        time_period_str,
        raw_obs_val_str,
        obs_status,
        obs_conf,
        obs_pre_break,
    ) in sorted_obs:
        cleaned_val = raw_obs_val_str.strip()
        is_missing_marker = cleaned_val.lower() in ("nan", "null", "none", ".", "")
        if is_missing_marker:
            if obs_status != "M":
                raise BisObservationValidationError(
                    f"non-finite rate value with non-M or absent status: {raw_obs_val_str}",
                    series_key=series_key,
                    time_period=time_period_str,
                    raw_obs_value=raw_obs_val_str,
                    obs_status=obs_status,
                    obs_conf=obs_conf,
                    obs_pre_break=obs_pre_break,
                )
            if last_finite_state is None or last_finite_date is None:
                raise BisObservationValidationError(
                    "missing observation before valid finite rate established",
                    series_key=series_key,
                    time_period=time_period_str,
                    raw_obs_value=raw_obs_val_str,
                    obs_status=obs_status,
                    obs_conf=obs_conf,
                    obs_pre_break=obs_pre_break,
                )
            records.append(
                PolicyRateRecord(
                    observation_date=obs_date,
                    series_key=series_key,
                    currency=currency,
                    policy_rate_state=last_finite_state,
                    source_observation_kind=SourceObservationKind.MISSING,
                    source_obs_value=raw_obs_val_str or "NaN",
                    source_obs_status="M",
                    policy_rate_state_origin=PolicyRateStateOrigin.PERSISTED,
                    source_state_date=last_finite_date,
                    obs_conf=obs_conf,
                    obs_pre_break=obs_pre_break,
                )
            )
        else:
            try:
                rate_val = _parse_decimal(raw_obs_val_str)
            except ValueError as exc:
                raise BisObservationValidationError(
                    str(exc),
                    series_key=series_key,
                    time_period=time_period_str,
                    raw_obs_value=raw_obs_val_str,
                    obs_status=obs_status,
                    obs_conf=obs_conf,
                    obs_pre_break=obs_pre_break,
                ) from exc

            last_finite_state = rate_val
            last_finite_date = obs_date
            records.append(
                PolicyRateRecord(
                    observation_date=obs_date,
                    series_key=series_key,
                    currency=currency,
                    policy_rate_state=rate_val,
                    source_observation_kind=SourceObservationKind.NUMERIC,
                    source_obs_value=raw_obs_val_str,
                    source_obs_status=obs_status or "A",
                    policy_rate_state_origin=PolicyRateStateOrigin.OBSERVED,
                    source_state_date=obs_date,
                    obs_conf=obs_conf,
                    obs_pre_break=obs_pre_break,
                )
            )

    return records


def parse_sdmx_csv_payload(
    body: str | bytes,
    expected_series: str | None = None,
    initialization_evidence: BisInitializationEvidence | None = None,
) -> list[PolicyRateRecord]:
    """Parse observations from an SDMX-CSV BIS response."""
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("SDMX-CSV payload missing header row")

    raw_by_series: dict[str, list[RawObsTuple]] = {}
    for row in reader:
        series_key = row.get("SERIES_KEY") or row.get("REF_AREA") or (expected_series or "")
        if not series_key and "FREQ" in row and "REF_AREA" in row:
            series_key = f"{row['FREQ']}.{row['REF_AREA']}"
        series_key = series_key.strip()
        if not series_key and expected_series:
            series_key = expected_series

        if expected_series and series_key != expected_series:
            raise ValueError(f"series mismatch: expected {expected_series}, got {series_key}")
        if series_key not in FROZEN_SERIES_KEYS:
            raise ValueError(f"unknown or unsupported series: {series_key}")

        time_period_str = (row.get("TIME_PERIOD") or row.get("time_period") or "").strip()
        obs_val_str = (row.get("OBS_VALUE") or row.get("obs_value") or "").strip()
        obs_status = (row.get("OBS_STATUS") or row.get("obs_status") or "").strip() or None
        obs_conf = (row.get("OBS_CONF") or row.get("obs_conf") or "").strip() or None
        obs_pre_break = (
            row.get("OBS_PRE_BREAK") or row.get("obs_pre_break") or ""
        ).strip() or None

        if not time_period_str:
            raise ValueError(f"missing TIME_PERIOD in row for {series_key}")
        if not obs_val_str and obs_status != "M":
            raise BisObservationValidationError(
                "missing OBS_VALUE",
                series_key=series_key,
                time_period=time_period_str,
                raw_obs_value=obs_val_str,
                obs_status=obs_status,
                obs_conf=obs_conf,
                obs_pre_break=obs_pre_break,
            )

        try:
            obs_date = date.fromisoformat(time_period_str)
        except ValueError as exc:
            raise BisObservationValidationError(
                f"malformed observation date: {time_period_str}",
                series_key=series_key,
                time_period=time_period_str,
                raw_obs_value=obs_val_str,
                obs_status=obs_status,
                obs_conf=obs_conf,
                obs_pre_break=obs_pre_break,
            ) from exc

        if obs_date < START_INCLUSIVE or obs_date >= END_EXCLUSIVE:
            raise BisObservationValidationError(
                f"observation date outside frozen interval [{START_INCLUSIVE}, {END_EXCLUSIVE})",
                series_key=series_key,
                time_period=time_period_str,
                raw_obs_value=obs_val_str,
                obs_status=obs_status,
                obs_conf=obs_conf,
                obs_pre_break=obs_pre_break,
            )

        raw_by_series.setdefault(series_key, []).append(
            (obs_date, time_period_str, obs_val_str, obs_status, obs_conf, obs_pre_break)
        )

    all_records: list[PolicyRateRecord] = []
    for sk in sorted(raw_by_series.keys()):
        all_records.extend(
            _normalize_series_observations(
                sk, raw_by_series[sk], initialization_evidence
            )
        )
    return all_records


def parse_sdmx_xml_payload(
    body: str | bytes,
    expected_series: str | None = None,
    initialization_evidence: BisInitializationEvidence | None = None,
) -> list[PolicyRateRecord]:
    """Parse observations from an SDMX-ML StructureSpecificData XML BIS response."""
    raw_bytes = body.encode("utf-8") if isinstance(body, str) else body
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"malformed XML payload: {exc}") from exc

    raw_by_series: dict[str, list[RawObsTuple]] = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "Series":
            series_ref_area = elem.attrib.get("REF_AREA") or elem.attrib.get("ref_area") or ""
            series_key = elem.attrib.get("SERIES_KEY") or (
                f"D.{series_ref_area}" if series_ref_area else (expected_series or "")
            )
            series_key = series_key.strip()
            if not series_key and expected_series:
                series_key = expected_series

            if expected_series and series_key != expected_series:
                raise ValueError(f"series mismatch: expected {expected_series}, got {series_key}")
            if series_key not in FROZEN_SERIES_KEYS:
                raise ValueError(f"unknown series in XML: {series_key}")

            for child in elem:
                child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child_tag == "Obs":
                    time_period = (
                        child.attrib.get("TIME_PERIOD") or child.attrib.get("time_period") or ""
                    ).strip()
                    obs_value = (
                        child.attrib.get("OBS_VALUE") or child.attrib.get("obs_value") or ""
                    ).strip()
                    obs_status = (
                        child.attrib.get("OBS_STATUS") or child.attrib.get("obs_status") or ""
                    ).strip() or None
                    obs_conf = (
                        child.attrib.get("OBS_CONF") or child.attrib.get("obs_conf") or ""
                    ).strip() or None
                    obs_pre_break = (
                        child.attrib.get("OBS_PRE_BREAK")
                        or child.attrib.get("obs_pre_break")
                        or ""
                    ).strip() or None

                    if not time_period:
                        raise ValueError(f"missing TIME_PERIOD in XML Obs for {series_key}")
                    if not obs_value and obs_status != "M":
                        raise BisObservationValidationError(
                            "missing OBS_VALUE",
                            series_key=series_key,
                            time_period=time_period,
                            raw_obs_value=obs_value,
                            obs_status=obs_status,
                            obs_conf=obs_conf,
                            obs_pre_break=obs_pre_break,
                        )

                    try:
                        obs_date = date.fromisoformat(time_period)
                    except ValueError as exc:
                        raise BisObservationValidationError(
                            f"malformed date: {time_period}",
                            series_key=series_key,
                            time_period=time_period,
                            raw_obs_value=obs_value,
                            obs_status=obs_status,
                            obs_conf=obs_conf,
                            obs_pre_break=obs_pre_break,
                        ) from exc

                    if obs_date < START_INCLUSIVE or obs_date >= END_EXCLUSIVE:
                        raise BisObservationValidationError(
                            f"observation date outside frozen interval "
                            f"[{START_INCLUSIVE}, {END_EXCLUSIVE})",
                            series_key=series_key,
                            time_period=time_period,
                            raw_obs_value=obs_value,
                            obs_status=obs_status,
                            obs_conf=obs_conf,
                            obs_pre_break=obs_pre_break,
                        )

                    raw_by_series.setdefault(series_key, []).append(
                        (obs_date, time_period, obs_value, obs_status, obs_conf, obs_pre_break)
                    )
        elif tag == "Obs" and "SERIES_KEY" in elem.attrib:
            series_key = elem.attrib["SERIES_KEY"].strip()
            if expected_series and series_key != expected_series:
                raise ValueError(f"series mismatch: expected {expected_series}, got {series_key}")
            if series_key not in FROZEN_SERIES_KEYS:
                raise ValueError(f"unknown series in XML Obs: {series_key}")

            time_period = (
                elem.attrib.get("TIME_PERIOD") or elem.attrib.get("time_period") or ""
            ).strip()
            obs_value = (
                elem.attrib.get("OBS_VALUE") or elem.attrib.get("obs_value") or ""
            ).strip()
            obs_status = (
                elem.attrib.get("OBS_STATUS") or elem.attrib.get("obs_status") or ""
            ).strip() or None
            obs_conf = (
                elem.attrib.get("OBS_CONF") or elem.attrib.get("obs_conf") or ""
            ).strip() or None
            obs_pre_break = (
                elem.attrib.get("OBS_PRE_BREAK") or elem.attrib.get("obs_pre_break") or ""
            ).strip() or None

            if not time_period:
                raise ValueError(f"missing TIME_PERIOD in standalone XML Obs for {series_key}")
            if not obs_value and obs_status != "M":
                raise BisObservationValidationError(
                    "missing OBS_VALUE",
                    series_key=series_key,
                    time_period=time_period,
                    raw_obs_value=obs_value,
                    obs_status=obs_status,
                    obs_conf=obs_conf,
                    obs_pre_break=obs_pre_break,
                )

            try:
                obs_date = date.fromisoformat(time_period)
            except ValueError as exc:
                raise BisObservationValidationError(
                    f"malformed date: {time_period}",
                    series_key=series_key,
                    time_period=time_period,
                    raw_obs_value=obs_value,
                    obs_status=obs_status,
                    obs_conf=obs_conf,
                    obs_pre_break=obs_pre_break,
                ) from exc

            if obs_date < START_INCLUSIVE or obs_date >= END_EXCLUSIVE:
                raise BisObservationValidationError(
                    f"observation date outside frozen interval "
                    f"[{START_INCLUSIVE}, {END_EXCLUSIVE})",
                    series_key=series_key,
                    time_period=time_period,
                    raw_obs_value=obs_value,
                    obs_status=obs_status,
                    obs_conf=obs_conf,
                    obs_pre_break=obs_pre_break,
                )

            raw_by_series.setdefault(series_key, []).append(
                (obs_date, time_period, obs_value, obs_status, obs_conf, obs_pre_break)
            )

    all_records: list[PolicyRateRecord] = []
    for sk in sorted(raw_by_series.keys()):
        all_records.extend(
            _normalize_series_observations(
                sk, raw_by_series[sk], initialization_evidence
            )
        )
    return all_records


def parse_bis_initialization_payload(
    body: str | bytes,
    *,
    expected_series: str,
) -> BisInitializationEvidence:
    """Validate the single direct-predecessor response frozen by ADR 0016."""
    if expected_series not in FROZEN_SERIES_KEYS:
        raise ValueError(f"unsupported initialization series: {expected_series}")
    raw_bytes = body.encode("utf-8") if isinstance(body, str) else body
    if not raw_bytes:
        raise ValueError("empty initialization payload")
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"malformed initialization XML payload: {exc}") from exc

    observations: list[tuple[str, str, str | None, str | None, str | None]] = []
    seen_series: set[str] = set()
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag != "Series":
            continue
        area = (elem.attrib.get("REF_AREA") or elem.attrib.get("ref_area") or "").strip()
        series_key = (elem.attrib.get("SERIES_KEY") or f"D.{area}").strip()
        seen_series.add(series_key)
        if series_key != expected_series:
            raise ValueError(
                f"initialization series mismatch: expected {expected_series}, got {series_key}"
            )
        for child in elem:
            child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if child_tag != "Obs":
                continue
            observations.append(
                (
                    (child.attrib.get("TIME_PERIOD") or "").strip(),
                    (child.attrib.get("OBS_VALUE") or "").strip(),
                    (child.attrib.get("OBS_STATUS") or "").strip() or None,
                    (child.attrib.get("OBS_CONF") or "").strip() or None,
                    (child.attrib.get("OBS_PRE_BREAK") or "").strip() or None,
                )
            )

    if seen_series != {expected_series}:
        raise ValueError(f"initialization series mismatch: expected only {expected_series}")
    if len(observations) != 1:
        raise ValueError("initialization response must contain exactly one observation")

    time_period, raw_value, obs_status, obs_conf, obs_pre_break = observations[0]
    try:
        predecessor_date = date.fromisoformat(time_period)
    except ValueError as exc:
        raise ValueError("malformed initialization predecessor date") from exc
    if predecessor_date >= START_INCLUSIVE:
        raise ValueError("initialization predecessor must be strictly before START_INCLUSIVE")
    try:
        predecessor_value = _parse_decimal(raw_value)
    except ValueError as exc:
        raise ValueError("initialization predecessor must be finite") from exc
    if obs_status == "M":
        raise ValueError("initialization predecessor must be finite and non-missing")
    if obs_pre_break is not None and obs_pre_break != raw_value:
        raise ValueError("initialization predecessor has ambiguous structural transition")

    return BisInitializationEvidence(
        series_key=expected_series,
        raw_byte_count=len(raw_bytes),
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        raw_payload=raw_bytes,
        selected_predecessor_date=predecessor_date,
        selected_predecessor_value=predecessor_value,
        source_obs_status=obs_status or "A",
        obs_conf=obs_conf,
        obs_pre_break=obs_pre_break,
    )

def normalize_bis_policy_rate_payloads(
    raw_payloads_by_series: Mapping[str, bytes],
    *,
    initialization_payloads_by_series: Mapping[str, bytes],
    content_format: str = "sdmx-xml",
) -> BisNormalizedDataset:
    """Normalize, cross-validate, and package multi-series BIS raw payloads into
    a canonical dataset.
    """
    provided_keys = set(raw_payloads_by_series.keys())
    expected_keys = set(FROZEN_SERIES_KEYS)

    if provided_keys != expected_keys:
        missing = expected_keys - provided_keys
        extra = provided_keys - expected_keys
        if missing:
            raise ValueError(f"missing required series payloads: {sorted(missing)}")
        if extra:
            raise ValueError(f"unknown series payloads provided: {sorted(extra)}")

    provided_initialization_keys = set(initialization_payloads_by_series.keys())
    if provided_initialization_keys != expected_keys:
        missing = expected_keys - provided_initialization_keys
        extra = provided_initialization_keys - expected_keys
        if missing:
            raise ValueError(f"missing required initialization payloads: {sorted(missing)}")
        raise ValueError(f"unknown initialization payloads provided: {sorted(extra)}")

    initialization_evidence = {
        series_key: parse_bis_initialization_payload(
            initialization_payloads_by_series[series_key], expected_series=series_key
        )
        for series_key in FROZEN_SERIES_KEYS
    }
    all_records_map: dict[tuple[str, date], PolicyRateRecord] = {}
    combined_bytes = bytearray()

    for series_key in FROZEN_SERIES_KEYS:
        body = raw_payloads_by_series[series_key]
        if not body:
            raise ValueError(f"empty raw payload for series {series_key}")
        combined_bytes.extend(series_key.encode("utf-8"))
        combined_bytes.extend(b":")
        combined_bytes.extend(body)

        if content_format == "sdmx-xml":
            records = parse_sdmx_xml_payload(
                body,
                expected_series=series_key,
                initialization_evidence=initialization_evidence[series_key],
            )
        elif content_format == "sdmx-csv":
            records = parse_sdmx_csv_payload(
                body,
                expected_series=series_key,
                initialization_evidence=initialization_evidence[series_key],
            )
        else:
            raise ValueError(f"unsupported content_format: {content_format}")

        if not records:
            raise ValueError(f"no valid records parsed for series {series_key}")

        for rec in records:
            key = (rec.series_key, rec.observation_date)
            if key in all_records_map:
                existing = all_records_map[key]
                if existing.policy_rate_state != rec.policy_rate_state:
                    raise ValueError(
                        f"conflicting duplicate observation for {key}: "
                        f"{existing.policy_rate_state} vs {rec.policy_rate_state}"
                    )
                raise ValueError(
                    f"duplicate observation for {key}: series {rec.series_key} "
                    f"has duplicate observation on {rec.observation_date}"
                )
            all_records_map[key] = rec

    sorted_records = tuple(
        sorted(all_records_map.values(), key=lambda r: (r.observation_date, r.series_key))
    )

    raw_sha256 = hashlib.sha256(combined_bytes).hexdigest()
    raw_artifact = BisRawArtifact(
        requested_series=FROZEN_SERIES_KEYS,
        start_inclusive="2014-01-01",
        end_exclusive="2024-01-01",
        content_format=content_format,
        raw_byte_count=len(combined_bytes),
        raw_sha256=raw_sha256,
        series_payloads=tuple(sorted(raw_payloads_by_series.items())),
    )
    raw_identity = compute_raw_evidence_identity(raw_artifact)

    return BisNormalizedDataset(
        schema=NORMALIZATION_VERSION,
        series_keys=FROZEN_SERIES_KEYS,
        start_inclusive="2014-01-01",
        end_exclusive="2024-01-01",
        records=sorted_records,
        record_count=len(sorted_records),
        raw_evidence_identity=raw_identity,
        initialization_evidence_identities=tuple(
            (
                series_key,
                compute_initialization_evidence_identity(initialization_evidence[series_key]),
            )
            for series_key in FROZEN_SERIES_KEYS
        ),
    )


@dataclass(frozen=True)
class PolicyRateGridEntry:
    """Status and value of a policy rate on a specific calendar day."""

    currency: str
    series_key: str
    date: date
    rate: Decimal | None
    state: RateState
    last_change_date: date | None


def build_daily_policy_rate_grid(
    dataset: BisNormalizedDataset,
) -> dict[tuple[str, date], PolicyRateGridEntry]:
    """Construct an exhaustive daily calendar grid for all 8 currencies.

    Distinguishes OBSERVED, RATE_PERSISTS (step function), and MISSING_OR_UNKNOWN.
    Does not provide trading decision boundary causal promotion.
    """
    obs_by_curr_date: dict[tuple[str, date], PolicyRateRecord] = {
        (r.currency, r.observation_date): r for r in dataset.records
    }

    grid: dict[tuple[str, date], PolicyRateGridEntry] = {}
    day_count = (END_EXCLUSIVE - START_INCLUSIVE).days

    for series_key in FROZEN_SERIES_KEYS:
        currency = SERIES_TO_CURRENCY[series_key]
        last_rate: Decimal | None = None
        last_change: date | None = None

        for offset in range(day_count):
            curr_date = START_INCLUSIVE + timedelta(days=offset)
            record = obs_by_curr_date.get((currency, curr_date))

            if record is not None:
                if record.policy_rate_state_origin == PolicyRateStateOrigin.OBSERVED:
                    last_rate = record.policy_rate_state
                    last_change = curr_date
                    grid[(currency, curr_date)] = PolicyRateGridEntry(
                        currency=currency,
                        series_key=series_key,
                        date=curr_date,
                        rate=record.policy_rate_state,
                        state=RateState.OBSERVED,
                        last_change_date=curr_date,
                    )
                elif record.policy_rate_state_origin == PolicyRateStateOrigin.PERSISTED:
                    last_rate = record.policy_rate_state
                    last_change = record.source_state_date
                    grid[(currency, curr_date)] = PolicyRateGridEntry(
                        currency=currency,
                        series_key=series_key,
                        date=curr_date,
                        rate=record.policy_rate_state,
                        state=RateState.RATE_PERSISTS,
                        last_change_date=record.source_state_date,
                    )
                else:
                    raise ValueError(
                        f"unknown policy_rate_state_origin: {record.policy_rate_state_origin}"
                    )
            elif last_rate is not None:
                grid[(currency, curr_date)] = PolicyRateGridEntry(
                    currency=currency,
                    series_key=series_key,
                    date=curr_date,
                    rate=last_rate,
                    state=RateState.RATE_PERSISTS,
                    last_change_date=last_change,
                )
            else:
                grid[(currency, curr_date)] = PolicyRateGridEntry(
                    currency=currency,
                    series_key=series_key,
                    date=curr_date,
                    rate=None,
                    state=RateState.MISSING_OR_UNKNOWN,
                    last_change_date=None,
                )

    return grid


def publish_raw_bis_artifact(
    raw_artifact: BisRawArtifact,
    output_dir: Path,
) -> Path:
    """Atomically write raw BIS evidence artifact to directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = output_dir / f"bis_cbpol_raw_{raw_artifact.raw_sha256[:16]}.json"

    content = canonical_json(raw_artifact)
    if manifest_file.exists():
        existing_content = manifest_file.read_text(encoding="utf-8")
        if existing_content != content:
            raise FileExistsError(
                f"raw artifact file {manifest_file} exists with different content"
            )
        return manifest_file

    with tempfile.NamedTemporaryFile("w", dir=output_dir, delete=False, encoding="utf-8") as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)

    temp_path.replace(manifest_file)
    return manifest_file


def publish_normalized_bis_dataset(
    dataset: BisNormalizedDataset,
    output_file: Path,
) -> Path:
    """Atomically write normalized BIS dataset to file."""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json(dataset)

    if output_file.exists():
        existing_content = output_file.read_text(encoding="utf-8")
        if existing_content != content:
            raise FileExistsError(
                f"normalized dataset file {output_file} exists with different content identity"
            )
        return output_file

    with tempfile.NamedTemporaryFile(
        "w", dir=output_file.parent, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)

    temp_path.replace(output_file)
    return output_file


def publish_canonical_acquisition_bundle(
    raw_artifacts_by_series: Mapping[str, BisRawArtifact],
    normalized_dataset: BisNormalizedDataset,
    raw_output_dir: Path,
    normalized_output_file: Path,
    *,
    initialization_artifacts_by_series: Mapping[str, BisInitializationEvidence] | None = None,
    initialization_output_dir: Path | None = None,
) -> tuple[dict[str, Path], Path] | tuple[dict[str, Path], dict[str, Path], Path]:
    """Transactionally publish complete canonical acquisition bundle.

    Safety contract:
    1. Pre-validates all 8 series keys are present and match raw artifacts.
    2. Pre-computes all 8 raw target filepaths and normalized target filepath.
    3. Pre-checks all destination paths: if any file already exists with different
       content, raises FileExistsError immediately before creating any new file.
    4. Genuinely new files are written to isolated temporary files in their respective
       target directories.
    5. Performs atomic replacement / rename of all new files.
    6. If an error occurs during multi-file replacement, rolls back and deletes only
       the files created by the current invocation, preserving pre-existing files.
    """
    if set(raw_artifacts_by_series.keys()) != set(FROZEN_SERIES_KEYS):
        raise ValueError(
            f"bundle requires all eight frozen series, got {sorted(raw_artifacts_by_series.keys())}"
        )

    if (initialization_artifacts_by_series is None) != (initialization_output_dir is None):
        raise ValueError("initialization artifacts and output directory must be provided together")
    if initialization_artifacts_by_series is not None and (
        set(initialization_artifacts_by_series) != set(FROZEN_SERIES_KEYS)
    ):
        raise ValueError("bundle requires initialization evidence for all eight series")

    raw_output_dir.mkdir(parents=True, exist_ok=True)
    if initialization_output_dir is not None:
        initialization_output_dir.mkdir(parents=True, exist_ok=True)
    normalized_output_file.parent.mkdir(parents=True, exist_ok=True)

    # Determine all target paths and contents
    raw_targets: dict[str, tuple[Path, str]] = {}
    for series_key in FROZEN_SERIES_KEYS:
        raw_art = raw_artifacts_by_series[series_key]
        raw_path = raw_output_dir / f"bis_cbpol_raw_{series_key}_{raw_art.raw_sha256[:16]}.json"
        raw_content = canonical_json(raw_art)
        raw_targets[series_key] = (raw_path, raw_content)

    initialization_targets: dict[str, tuple[Path, str]] = {}
    if initialization_artifacts_by_series is not None and initialization_output_dir is not None:
        for series_key in FROZEN_SERIES_KEYS:
            evidence = initialization_artifacts_by_series[series_key]
            if evidence.series_key != series_key:
                raise ValueError("initialization artifact series mismatch")
            identity = compute_initialization_evidence_identity(evidence)
            target = initialization_output_dir / (
                f"bis_cbpol_initialization_{series_key}_{identity[:16]}.json"
            )
            initialization_targets[series_key] = (target, canonical_json(evidence))
    norm_target_path = normalized_output_file
    norm_content = canonical_json(normalized_dataset)

    # Phase 1: Pre-check all targets for conflicts
    all_targets: list[tuple[Path, str]] = list(raw_targets.values())
    all_targets.extend(initialization_targets.values())
    all_targets.append((norm_target_path, norm_content))

    preexisting_paths: set[Path] = set()
    files_to_create: list[tuple[Path, str]] = []

    for target_path, content in all_targets:
        if target_path.exists():
            existing = target_path.read_text(encoding="utf-8")
            if existing != content:
                raise FileExistsError(
                    f"target file {target_path} exists with different content"
                )
            preexisting_paths.add(target_path)
        else:
            files_to_create.append((target_path, content))

    # Phase 2: Write all new files to temporary files in target directories
    temp_files: list[tuple[Path, Path]] = []
    try:
        for final_path, content in files_to_create:
            with tempfile.NamedTemporaryFile(
                "w", dir=final_path.parent, delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(content)
                temp_files.append((Path(tmp.name), final_path))
    except Exception:
        # Clean up any temporary files created so far
        for temp_p, _ in temp_files:
            if temp_p.exists():
                try:
                    temp_p.unlink()
                except Exception:
                    pass
        raise

    # Phase 3: Atomic commit / rename with rollback protection
    created_in_this_run: list[Path] = []
    try:
        for temp_p, final_p in temp_files:
            temp_p.replace(final_p)
            created_in_this_run.append(final_p)
    except Exception:
        # Rollback only files created in this invocation
        for p in created_in_this_run:
            if p.exists() and p not in preexisting_paths:
                try:
                    p.unlink()
                except Exception:
                    pass
        # Clean up any leftover temporary files
        for temp_p, _ in temp_files:
            if temp_p.exists():
                try:
                    temp_p.unlink()
                except Exception:
                    pass
        raise

    result_raw_paths = {k: v[0] for k, v in raw_targets.items()}
    if initialization_targets:
        result_initialization_paths = {k: v[0] for k, v in initialization_targets.items()}
        return result_raw_paths, result_initialization_paths, norm_target_path
    return result_raw_paths, norm_target_path
