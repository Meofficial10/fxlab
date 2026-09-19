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

NORMALIZATION_VERSION = "bis_cbpol_daily_v1"

BIS_SDMX_API_BASE_URL = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0"
BIS_SDMX_XML_ACCEPT = "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
BIS_SDMX_CSV_ACCEPT = "application/vnd.sdmx.data+csv;version=2.0.0"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class RateState(StrEnum):
    """Observation state of a policy rate."""

    OBSERVED = "OBSERVED"
    RATE_PERSISTS = "RATE_PERSISTS"
    MISSING_OR_UNKNOWN = "MISSING_OR_UNKNOWN"


def _primitive(value: object) -> object:
    """Recursively convert objects to JSON-serializable primitives in deterministic order."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
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


@dataclass(frozen=True, order=True)
class PolicyRateRecord:
    """A single normalized daily policy-rate observation."""

    observation_date: date
    series_key: str
    currency: str
    rate_value: Decimal
    obs_status: str = "A"
    point_in_time_status: str = "UNRESOLVED"

    def __post_init__(self) -> None:
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
        if not isinstance(self.rate_value, Decimal) or not self.rate_value.is_finite():
            raise ValueError(f"rate_value must be a finite Decimal, got {self.rate_value}")


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
class BisNormalizedDataset:
    """Normalized, validated, and deterministically ordered BIS policy rates dataset."""

    schema: str = NORMALIZATION_VERSION
    series_keys: tuple[str, ...] = FROZEN_SERIES_KEYS
    start_inclusive: str = "2014-01-01"
    end_exclusive: str = "2024-01-01"
    records: tuple[PolicyRateRecord, ...] = ()
    record_count: int = 0
    raw_evidence_identity: str = ""
    point_in_time_status: str = "UNRESOLVED"
    normalized_identity: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if self.schema != NORMALIZATION_VERSION:
            raise ValueError(f"schema must be {NORMALIZATION_VERSION}")
        if tuple(sorted(self.series_keys)) != FROZEN_SERIES_KEYS:
            raise ValueError("series_keys must match frozen eight series")
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
        "point_in_time_status": dataset.point_in_time_status,
        "records": [
            {
                "observation_date": r.observation_date.isoformat(),
                "series_key": r.series_key,
                "currency": r.currency,
                "rate_value": format(r.rate_value, "f"),
                "obs_status": r.obs_status,
                "point_in_time_status": r.point_in_time_status,
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


def parse_sdmx_csv_payload(
    body: str | bytes,
    expected_series: str | None = None,
) -> list[PolicyRateRecord]:
    """Parse observations from an SDMX-CSV BIS response."""
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("SDMX-CSV payload missing header row")

    records: list[PolicyRateRecord] = []
    for row in reader:
        series_key = row.get("SERIES_KEY") or row.get("REF_AREA") or ""
        if not series_key and "FREQ" in row and "REF_AREA" in row:
            series_key = f"{row['FREQ']}.{row['REF_AREA']}"
        series_key = series_key.strip()

        if expected_series and series_key != expected_series:
            raise ValueError(f"series mismatch: expected {expected_series}, got {series_key}")
        if series_key not in FROZEN_SERIES_KEYS:
            raise ValueError(f"unknown or unsupported series: {series_key}")

        time_period_str = (row.get("TIME_PERIOD") or row.get("time_period") or "").strip()
        if not time_period_str:
            raise ValueError("missing TIME_PERIOD in row")
        try:
            obs_date = date.fromisoformat(time_period_str)
        except ValueError as exc:
            raise ValueError(f"malformed observation date: {time_period_str}") from exc

        # Sealed boundary check
        if obs_date < START_INCLUSIVE or obs_date >= END_EXCLUSIVE:
            raise ValueError(
                f"observation date {obs_date} outside frozen interval "
                f"[{START_INCLUSIVE}, {END_EXCLUSIVE})"
            )

        obs_val_str = (row.get("OBS_VALUE") or row.get("obs_value") or "").strip()
        if not obs_val_str:
            raise ValueError(f"missing OBS_VALUE for {series_key} on {obs_date}")

        rate_val = _parse_decimal(obs_val_str)
        obs_status = (row.get("OBS_STATUS") or row.get("obs_status") or "A").strip()

        currency = SERIES_TO_CURRENCY[series_key]
        records.append(
            PolicyRateRecord(
                observation_date=obs_date,
                series_key=series_key,
                currency=currency,
                rate_value=rate_val,
                obs_status=obs_status,
            )
        )
    return records


def parse_sdmx_xml_payload(
    body: str | bytes,
    expected_series: str | None = None,
) -> list[PolicyRateRecord]:
    """Parse observations from an SDMX-ML StructureSpecificData XML BIS response."""
    raw_bytes = body.encode("utf-8") if isinstance(body, str) else body
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"malformed XML payload: {exc}") from exc

    records: list[PolicyRateRecord] = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "Series":
            series_ref_area = elem.attrib.get("REF_AREA") or elem.attrib.get("ref_area") or ""
            series_key = elem.attrib.get("SERIES_KEY") or f"D.{series_ref_area}"
            if expected_series and series_key != expected_series:
                raise ValueError(f"series mismatch: expected {expected_series}, got {series_key}")
            if series_key not in FROZEN_SERIES_KEYS:
                raise ValueError(f"unknown series in XML: {series_key}")

            currency = SERIES_TO_CURRENCY[series_key]
            for child in elem:
                child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child_tag == "Obs":
                    time_period = child.attrib.get("TIME_PERIOD") or child.attrib.get("time_period")
                    obs_value = child.attrib.get("OBS_VALUE") or child.attrib.get("obs_value")
                    obs_status = (
                        child.attrib.get("OBS_STATUS") or child.attrib.get("obs_status") or "A"
                    )

                    if not time_period:
                        raise ValueError(f"missing TIME_PERIOD in XML Obs for {series_key}")
                    if obs_value is None or not obs_value.strip():
                        raise ValueError(f"missing OBS_VALUE in XML Obs for {series_key}")

                    try:
                        obs_date = date.fromisoformat(time_period.strip())
                    except ValueError as exc:
                        raise ValueError(f"malformed date in XML Obs: {time_period}") from exc

                    if obs_date < START_INCLUSIVE or obs_date >= END_EXCLUSIVE:
                        raise ValueError(
                            f"XML observation date {obs_date} outside frozen interval "
                            f"[{START_INCLUSIVE}, {END_EXCLUSIVE})"
                        )

                    rate_val = _parse_decimal(obs_value)
                    records.append(
                        PolicyRateRecord(
                            observation_date=obs_date,
                            series_key=series_key,
                            currency=currency,
                            rate_value=rate_val,
                            obs_status=obs_status.strip(),
                        )
                    )
        elif tag == "Obs" and "SERIES_KEY" in elem.attrib:
            series_key = elem.attrib["SERIES_KEY"]
            if expected_series and series_key != expected_series:
                raise ValueError(f"series mismatch: expected {expected_series}, got {series_key}")
            if series_key not in FROZEN_SERIES_KEYS:
                raise ValueError(f"unknown series in XML Obs: {series_key}")

            currency = SERIES_TO_CURRENCY[series_key]
            time_period = elem.attrib.get("TIME_PERIOD")
            obs_value = elem.attrib.get("OBS_VALUE")
            obs_status = elem.attrib.get("OBS_STATUS") or "A"

            if not time_period:
                raise ValueError(f"missing TIME_PERIOD in standalone XML Obs for {series_key}")
            if obs_value is None or not obs_value.strip():
                raise ValueError(f"missing OBS_VALUE in standalone XML Obs for {series_key}")

            try:
                obs_date = date.fromisoformat(time_period.strip())
            except ValueError as exc:
                raise ValueError(f"malformed date in XML Obs: {time_period}") from exc

            if obs_date < START_INCLUSIVE or obs_date >= END_EXCLUSIVE:
                raise ValueError(
                    f"observation date {obs_date} outside frozen interval "
                    f"[{START_INCLUSIVE}, {END_EXCLUSIVE})"
                )
            rate_val = _parse_decimal(obs_value)
            records.append(
                PolicyRateRecord(
                    observation_date=obs_date,
                    series_key=series_key,
                    currency=currency,
                    rate_value=rate_val,
                    obs_status=obs_status.strip(),
                )
            )

    return records


def normalize_bis_policy_rate_payloads(
    raw_payloads_by_series: Mapping[str, bytes],
    *,
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
            records = parse_sdmx_xml_payload(body, expected_series=series_key)
        elif content_format == "sdmx-csv":
            records = parse_sdmx_csv_payload(body, expected_series=series_key)
        else:
            raise ValueError(f"unsupported content_format: {content_format}")

        if not records:
            raise ValueError(f"no valid records parsed for series {series_key}")

        for rec in records:
            key = (rec.series_key, rec.observation_date)
            if key in all_records_map:
                existing = all_records_map[key]
                if existing.rate_value != rec.rate_value:
                    raise ValueError(
                        f"conflicting duplicate observation for {key}: "
                        f"{existing.rate_value} vs {rec.rate_value}"
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
                last_rate = record.rate_value
                last_change = curr_date
                grid[(currency, curr_date)] = PolicyRateGridEntry(
                    currency=currency,
                    series_key=series_key,
                    date=curr_date,
                    rate=record.rate_value,
                    state=RateState.OBSERVED,
                    last_change_date=curr_date,
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
