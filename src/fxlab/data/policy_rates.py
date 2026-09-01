"""Candidate B policy-rate data qualification contracts.

This module validates provenance and point-in-time eligibility only.  It deliberately contains no
policy differential, FX return, portfolio, or performance calculation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

MAX_OBSERVATION_DATE = date(2023, 12, 31)
TRAIN_OUTCOME_END = date(2021, 12, 31)
VALIDATION_OUTCOME_START = date(2022, 1, 1)
APPROVED_REQUEST_START = date(2014, 1, 1)
APPROVED_REQUEST_END = MAX_OBSERVATION_DATE
EXPECTED_TRAIN_COHORTS = 83
EXPECTED_VALIDATION_COHORTS = 23
EXPECTED_TOTAL_COHORTS = 106

AUTHORITATIVE_D_US_ACCEPT = (
    "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
)
AUTHORITATIVE_D_AU_ACCEPT = AUTHORITATIVE_D_US_ACCEPT
AUTHORITATIVE_D_AU_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.AU"
    "?startPeriod=2014-01-01&endPeriod=2023-12-31"
)
AUTHORITATIVE_D_CA_ACCEPT = AUTHORITATIVE_D_US_ACCEPT
AUTHORITATIVE_D_CA_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.CA"
    "?startPeriod=2014-01-01&endPeriod=2023-12-31"
)
AUTHORITATIVE_D_CH_ACCEPT = AUTHORITATIVE_D_US_ACCEPT
AUTHORITATIVE_D_CH_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.CH"
    "?startPeriod=2014-01-01&endPeriod=2023-12-31"
)
AUTHORITATIVE_D_XM_ACCEPT = AUTHORITATIVE_D_US_ACCEPT
AUTHORITATIVE_D_XM_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.XM"
    "?startPeriod=2014-01-01&endPeriod=2023-12-31"
)
AUTHORITATIVE_D_GB_ACCEPT = AUTHORITATIVE_D_US_ACCEPT
AUTHORITATIVE_D_GB_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.GB"
    "?startPeriod=2014-01-01&endPeriod=2023-12-31"
)
AUTHORITATIVE_D_JP_ACCEPT = AUTHORITATIVE_D_US_ACCEPT
AUTHORITATIVE_D_JP_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.JP"
    "?startPeriod=2014-01-01&endPeriod=2023-12-31"
)
AUTHORITATIVE_D_US_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.US"
    "?startPeriod=2014-01-01&endPeriod=2023-12-31"
)
_SDMX_21_MESSAGE_NAMESPACE = (
    "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
)
_SDMX_21_COMMON_NAMESPACE = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
_SDMX_21_STRUCTURE_SPECIFIC_DATA_NAMESPACE = (
    "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/structurespecific"
)
_XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
_D_US_STRUCTURE_NAMESPACE = (
    "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow="
    "BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD"
)
_D_US_STRUCTURE_REFERENCE = "BIS_WS_CBPOL_1_0"
_D_US_ROOT_QNAME = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}StructureSpecificData"
_D_US_DATA_SCOPE_QNAME = (
    f"{{{_SDMX_21_STRUCTURE_SPECIFIC_DATA_NAMESPACE}}}dataScope"
)
_D_US_STRUCTURE_REF_QNAME = (
    f"{{{_SDMX_21_STRUCTURE_SPECIFIC_DATA_NAMESPACE}}}structureRef"
)
_D_US_EXPECTED_DATES = tuple(
    APPROVED_REQUEST_START + timedelta(days=offset)
    for offset in range((APPROVED_REQUEST_END - APPROVED_REQUEST_START).days + 1)
)

APPROVED_BIS_SERIES: Mapping[str, str] = MappingProxyType(
    {
        "AUD": "D.AU",
        "CAD": "D.CA",
        "CHF": "D.CH",
        "EUR": "D.XM",
        "GBP": "D.GB",
        "JPY": "D.JP",
        "NZD": "D.NZ",
        "USD": "D.US",
    }
)
APPROVED_PAIRS = (
    "AUDUSD",
    "EURUSD",
    "GBPUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
)
APPROVED_BIS_OBSERVATION_STATUS_SEMANTICS = ("A=normal",)
APPROVED_BIS_OBSERVATION_STATUS_CODES = frozenset({"A"})
AUTHORITATIVE_BIS_RAW_STATUS_SEMANTICS = (
    "A=normal",
    "M=missing_value_data_cannot_exist",
)
OFFICIAL_DOMAINS: Mapping[str, str] = MappingProxyType(
    {
        "AUD": "rba.gov.au",
        "CAD": "bankofcanada.ca",
        "CHF": "snb.ch",
        "EUR": "ecb.europa.eu",
        "GBP": "bankofengland.co.uk",
        "JPY": "boj.or.jp",
        "NZD": "rbnz.govt.nz",
        "USD": "federalreserve.gov",
    }
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PolicyRateQualificationError(ValueError):
    """Stable, non-sensitive qualification failure."""

    def __init__(self, reason: str):
        self.reason = _identifier(reason, "reason")
        super().__init__(self.reason)


class PolicyEventKind(StrEnum):
    BASELINE = "baseline"
    RATE_CHANGE = "rate_change"
    INSTRUMENT_TRANSITION = "instrument_transition"


class TimePrecision(StrEnum):
    EXACT_TIMESTAMP = "exact_timestamp"
    DATE_ONLY = "date_only"


class EvidenceClassification(StrEnum):
    OFFICIAL_ANNOUNCEMENT = "official_announcement"
    OFFICIAL_RATE_HISTORY = "official_rate_history"
    OFFICIAL_INSTRUMENT_NOTICE = "official_instrument_notice"


class AmbiguityState(StrEnum):
    CLEAR = "clear"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"


class ConcordanceStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class FormationSplit(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    PURGED = "purged"


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value.strip()):
        raise ValueError(f"{field_name} is malformed")
    return value.strip()


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
        raise ValueError(f"{field_name} must be bounded non-empty text")
    return value.strip()


def _media_type(value: object) -> str:
    normalized = _text(value, "media_type").lower()
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}",
        normalized,
    ):
        raise ValueError("media_type is malformed")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _sha(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _decimal(value: Decimal | str | None, field_name: str, *, optional: bool = False):
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field_name} is required")
    if isinstance(value, float) or isinstance(value, bool):
        raise ValueError(f"{field_name} must not use binary float")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an exact decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _primitive(value: object) -> object:
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
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _primitive(getattr(value, item.name)) for item in fields(value)}
    raise ValueError("only allow-listed immutable primitives are canonical")


def canonical_json(value: object) -> str:
    return json.dumps(_primitive(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True, order=True)
class PolicyRateSeriesSpec:
    currency: str
    series_key: str
    agency: str = "BIS"
    dataflow: str = "WS_CBPOL"
    version: str = "1.0"
    frequency: str = "D"

    def __post_init__(self) -> None:
        currency = self.currency.strip().upper() if isinstance(self.currency, str) else ""
        if APPROVED_BIS_SERIES.get(currency) != self.series_key:
            raise ValueError("unsupported Candidate B BIS series")
        if (self.agency, self.dataflow, self.version, self.frequency) != (
            "BIS",
            "WS_CBPOL",
            "1.0",
            "D",
        ):
            raise ValueError("unsupported Candidate B BIS contract")
        object.__setattr__(self, "currency", currency)


@dataclass(frozen=True)
class PolicyRateRequest:
    series: PolicyRateSeriesSpec
    start: date
    end: date

    def __post_init__(self) -> None:
        if not isinstance(self.series, PolicyRateSeriesSpec):
            raise ValueError("series must use the approved contract")
        if not isinstance(self.start, date) or not isinstance(self.end, date):
            raise ValueError("request start and end are required dates")
        if self.start != APPROVED_REQUEST_START or self.end != APPROVED_REQUEST_END:
            raise ValueError("unexpected Candidate B request range")
        if self.end > MAX_OBSERVATION_DATE:
            raise ValueError("sealed_window_violation")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "format": 1,
                "series": self.series,
                "start": self.start,
                "end": self.end,
            }
        )


def authoritative_d_us_request() -> PolicyRateRequest:
    return PolicyRateRequest(
        PolicyRateSeriesSpec("USD", "D.US"),
        APPROVED_REQUEST_START,
        APPROVED_REQUEST_END,
    )


def authoritative_d_au_request() -> PolicyRateRequest:
    return PolicyRateRequest(
        PolicyRateSeriesSpec("AUD", "D.AU"),
        APPROVED_REQUEST_START,
        APPROVED_REQUEST_END,
    )


def authoritative_d_ca_request() -> PolicyRateRequest:
    return PolicyRateRequest(
        PolicyRateSeriesSpec("CAD", "D.CA"),
        APPROVED_REQUEST_START,
        APPROVED_REQUEST_END,
    )


def authoritative_d_ch_request() -> PolicyRateRequest:
    return PolicyRateRequest(
        PolicyRateSeriesSpec("CHF", "D.CH"),
        APPROVED_REQUEST_START,
        APPROVED_REQUEST_END,
    )


def authoritative_d_xm_request() -> PolicyRateRequest:
    return PolicyRateRequest(
        PolicyRateSeriesSpec("EUR", "D.XM"),
        APPROVED_REQUEST_START,
        APPROVED_REQUEST_END,
    )


def authoritative_d_gb_request() -> PolicyRateRequest:
    return PolicyRateRequest(
        PolicyRateSeriesSpec("GBP", "D.GB"),
        APPROVED_REQUEST_START,
        APPROVED_REQUEST_END,
    )


def authoritative_d_jp_request() -> PolicyRateRequest:
    return PolicyRateRequest(
        PolicyRateSeriesSpec("JPY", "D.JP"),
        APPROVED_REQUEST_START,
        APPROVED_REQUEST_END,
    )


@dataclass(frozen=True, order=True)
class PolicyRateObservation:
    series_key: str
    observation_date: date
    value: Decimal
    status: str

    def __post_init__(self) -> None:
        if self.series_key not in APPROVED_BIS_SERIES.values():
            raise ValueError("unsupported observation series")
        if not isinstance(self.observation_date, date):
            raise ValueError("observation_date must be a date")
        if self.observation_date > MAX_OBSERVATION_DATE:
            raise PolicyRateQualificationError("sealed_window_violation")
        object.__setattr__(self, "value", _decimal(self.value, "value"))
        try:
            status = _identifier(self.status, "status")
        except ValueError as exc:
            raise PolicyRateQualificationError("observation_status_invalid") from exc
        if status not in APPROVED_BIS_OBSERVATION_STATUS_CODES:
            raise PolicyRateQualificationError("observation_status_invalid")
        object.__setattr__(self, "status", status)

    @property
    def identity(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True)
class AuthoritativePolicyRateParseResult(Sequence[PolicyRateObservation]):
    """Typed separation between returned BIS rows and accepted numeric observations."""

    raw_row_count: int
    observations: tuple[PolicyRateObservation, ...]
    numeric_observation_count: int = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.raw_row_count, bool)
            or not isinstance(self.raw_row_count, int)
            or self.raw_row_count < 1
        ):
            raise ValueError("raw_row_count must be a positive integer")
        observations = tuple(self.observations)
        if not observations or len(observations) > self.raw_row_count:
            raise ValueError("numeric observations must be a non-empty raw-row subset")
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "numeric_observation_count", len(observations))

    def __len__(self) -> int:
        return self.numeric_observation_count

    def __getitem__(
        self, index: int | slice
    ) -> PolicyRateObservation | tuple[PolicyRateObservation, ...]:
        return self.observations[index]


@dataclass(frozen=True)
class PolicyRateMetadata:
    agency: str
    dataflow: str
    version: str
    frequency: str
    series_key: str
    currency: str
    reference_area: str
    unit: str
    scale: int
    observation_status_semantics: tuple[str, ...]
    dsd_identity: str
    codelist_identity: str
    instrument_metadata: str
    source_identity: str
    endpoint_identity: str
    media_type: str
    revision: str

    def __post_init__(self) -> None:
        currency = self.currency.strip().upper() if isinstance(self.currency, str) else ""
        if APPROVED_BIS_SERIES.get(currency) != self.series_key:
            raise ValueError("metadata series does not match the allow-list")
        if (self.agency, self.dataflow, self.version, self.frequency) != (
            "BIS",
            "WS_CBPOL",
            "1.0",
            "D",
        ):
            raise ValueError("metadata does not match the BIS contract")
        expected_area = self.series_key.split(".", 1)[1]
        if self.reference_area != expected_area:
            raise ValueError("metadata reference area mismatch")
        if isinstance(self.scale, bool) or not isinstance(self.scale, int):
            raise ValueError("scale must be an integer")
        statuses = tuple(
            _text(item, "observation_status_semantic") for item in self.observation_status_semantics
        )
        if statuses != APPROVED_BIS_OBSERVATION_STATUS_SEMANTICS:
            raise ValueError("observation status semantics do not match the frozen vocabulary")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "observation_status_semantics", statuses)
        for name in (
            "unit",
            "dsd_identity",
            "codelist_identity",
            "instrument_metadata",
            "source_identity",
            "endpoint_identity",
            "revision",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "media_type", _media_type(self.media_type))

    @property
    def stable_identity(self) -> str:
        return canonical_sha256(self)


def parse_bis_csv(
    raw_bytes: bytes, series: PolicyRateSeriesSpec
) -> tuple[PolicyRateObservation, ...]:
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise PolicyRateQualificationError("empty_response")
    if not isinstance(series, PolicyRateSeriesSpec):
        raise PolicyRateQualificationError("unsupported_series")
    try:
        reader = csv.DictReader(io.StringIO(raw_bytes.decode("utf-8-sig", errors="strict")))
    except UnicodeError as exc:
        raise PolicyRateQualificationError("response_encoding_invalid") from exc
    required = {"FREQ", "REF_AREA", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"}
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise PolicyRateQualificationError("response_schema_invalid")
    area = series.series_key.split(".", 1)[1]
    inspected: list[tuple[date, dict[str, str]]] = []
    try:
        for row in reader:
            timestamp = row["TIME_PERIOD"]
            if len(timestamp) != 10:
                raise ValueError
            observed = date.fromisoformat(timestamp)
            if observed > MAX_OBSERVATION_DATE:
                raise PolicyRateQualificationError("sealed_window_violation")
            inspected.append((observed, row))
    except PolicyRateQualificationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyRateQualificationError("observation_timestamp_invalid") from exc
    if not inspected:
        raise PolicyRateQualificationError("no_observations")
    observations: list[PolicyRateObservation] = []
    for observed, row in inspected:
        if row["FREQ"] != "D" or row["REF_AREA"] != area:
            raise PolicyRateQualificationError("response_series_mismatch")
        try:
            value = Decimal(row["OBS_VALUE"])
        except (InvalidOperation, TypeError) as exc:
            raise PolicyRateQualificationError("observation_value_invalid") from exc
        observations.append(
            PolicyRateObservation(series.series_key, observed, value, row["OBS_STATUS"])
        )
    result = tuple(observations)
    if tuple(sorted(result, key=lambda item: item.observation_date)) != result:
        raise PolicyRateQualificationError("observation_order_invalid")
    if len({item.observation_date for item in result}) != len(result):
        raise PolicyRateQualificationError("duplicate_observation")
    return result


def _authoritative_sdmx_root_and_namespaces(
    raw_bytes: bytes,
) -> tuple[ET.Element, Mapping[str, str]]:
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise PolicyRateQualificationError("empty_response")
    try:
        decoded = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise PolicyRateQualificationError("response_encoding_invalid") from exc
    upper = decoded.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise PolicyRateQualificationError("response_schema_invalid")
    namespaces: dict[str, str] = {}
    try:
        for _, (prefix, namespace) in ET.iterparse(
            io.BytesIO(raw_bytes), events=("start-ns",)
        ):
            previous = namespaces.get(prefix)
            if previous is not None and previous != namespace:
                raise PolicyRateQualificationError("response_schema_invalid")
            namespaces[prefix] = namespace
        root = ET.fromstring(raw_bytes)
    except PolicyRateQualificationError:
        raise
    except (ET.ParseError, UnicodeError, ValueError) as exc:
        raise PolicyRateQualificationError("response_schema_invalid") from exc
    return root, MappingProxyType(namespaces)


def _expanded_sdmx_qname(value: str | None, namespaces: Mapping[str, str]) -> str:
    if value is None or value.count(":") != 1:
        raise PolicyRateQualificationError("response_series_mismatch")
    prefix, local_name = value.split(":", 1)
    namespace = namespaces.get(prefix)
    if not namespace or not local_name:
        raise PolicyRateQualificationError("response_series_mismatch")
    return f"{{{namespace}}}{local_name}"


def parse_authoritative_bis_d_us_sdmx(
    raw_bytes: bytes,
    request: PolicyRateRequest,
) -> tuple[PolicyRateObservation, ...]:
    if request != authoritative_d_us_request():
        raise PolicyRateQualificationError("request_not_approved")
    root, namespaces = _authoritative_sdmx_root_and_namespaces(raw_bytes)
    if root.tag != _D_US_ROOT_QNAME or _D_US_STRUCTURE_NAMESPACE not in namespaces.values():
        raise PolicyRateQualificationError("response_schema_invalid")

    header_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}Header"
    structure_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}Structure"
    usage_qname = f"{{{_SDMX_21_COMMON_NAMESPACE}}}StructureUsage"
    headers = root.findall(header_qname)
    if len(headers) != 1:
        raise PolicyRateQualificationError("response_series_mismatch")
    structures = headers[0].findall(structure_qname)
    if len(structures) != 1:
        raise PolicyRateQualificationError("response_series_mismatch")
    structure = structures[0]
    if (
        structure.attrib.get("structureID") != _D_US_STRUCTURE_REFERENCE
        or structure.attrib.get("namespace") != _D_US_STRUCTURE_NAMESPACE
        or structure.attrib.get("dimensionAtObservation") != "TIME_PERIOD"
    ):
        raise PolicyRateQualificationError("response_series_mismatch")
    usages = structure.findall(usage_qname)
    references = list(usages[0]) if len(usages) == 1 else []
    if len(references) != 1 or references[0].tag != "Ref" or (
        references[0].attrib.get("agencyID"),
        references[0].attrib.get("id"),
        references[0].attrib.get("version"),
    ) != ("BIS", "WS_CBPOL", "1.0"):
        raise PolicyRateQualificationError("response_series_mismatch")

    dataset_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}DataSet"
    datasets = root.findall(dataset_qname)
    all_datasets = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "DataSet"]
    if len(datasets) != 1 or all_datasets != datasets:
        raise PolicyRateQualificationError("response_schema_invalid")
    dataset = datasets[0]
    if (
        dataset.attrib.get(_D_US_DATA_SCOPE_QNAME) != "DataStructure"
        or dataset.attrib.get(_D_US_STRUCTURE_REF_QNAME) != _D_US_STRUCTURE_REFERENCE
        or _expanded_sdmx_qname(
            dataset.attrib.get(f"{{{_XSI_NAMESPACE}}}type"), namespaces
        )
        != f"{{{_D_US_STRUCTURE_NAMESPACE}}}DataSetType"
    ):
        raise PolicyRateQualificationError("response_series_mismatch")
    if (
        dataset.attrib.get("UNIT_MEASURE") != "368"
        or dataset.attrib.get("UNIT_MULT") != "0"
    ):
        raise PolicyRateQualificationError("response_unit_mismatch")

    series = [item for item in dataset if item.tag == "Series"]
    all_series = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "Series"]
    if len(series) != 1 or all_series != series:
        raise PolicyRateQualificationError("response_schema_invalid")
    series_element = series[0]
    if (
        series_element.attrib.get("FREQ") != "D"
        or series_element.attrib.get("REF_AREA") != "US"
    ):
        raise PolicyRateQualificationError("response_series_mismatch")

    observation_elements = tuple(item for item in series_element if item.tag == "Obs")
    all_observations = [
        item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "Obs"
    ]
    if not observation_elements or all_observations != list(observation_elements):
        raise PolicyRateQualificationError("response_schema_invalid")

    observed_dates: list[date] = []
    for observation in observation_elements:
        timestamp = observation.attrib.get("TIME_PERIOD")
        try:
            if timestamp is None or len(timestamp) != 10:
                raise ValueError
            observed = date.fromisoformat(timestamp)
        except (TypeError, ValueError) as exc:
            raise PolicyRateQualificationError("observation_timestamp_invalid") from exc
        if observed > MAX_OBSERVATION_DATE:
            raise PolicyRateQualificationError("sealed_window_violation")
        if observed < request.start or observed > request.end:
            raise PolicyRateQualificationError("observation_outside_request")
        observed_dates.append(observed)

    if len(set(observed_dates)) != len(observed_dates):
        raise PolicyRateQualificationError("duplicate_observation")
    if tuple(observed_dates) != _D_US_EXPECTED_DATES:
        raise PolicyRateQualificationError("observation_date_set_mismatch")

    observations: list[PolicyRateObservation] = []
    for observed, observation in zip(observed_dates, observation_elements, strict=True):
        if observation.attrib.get("OBS_STATUS") != "A":
            raise PolicyRateQualificationError("observation_status_invalid")
        value = observation.attrib.get("OBS_VALUE")
        if value is None:
            raise PolicyRateQualificationError("observation_value_invalid")
        try:
            parsed_value = Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PolicyRateQualificationError("observation_value_invalid") from exc
        if not parsed_value.is_finite():
            raise PolicyRateQualificationError("observation_value_invalid")
        observations.append(PolicyRateObservation("D.US", observed, parsed_value, "A"))
    return tuple(observations)


def parse_authoritative_bis_sdmx(
    raw_bytes: object,
    request: PolicyRateRequest,
) -> AuthoritativePolicyRateParseResult:
    """Parse raw rows while emitting numeric observations only for A + finite."""

    if not isinstance(raw_bytes, bytes):
        raise PolicyRateQualificationError("authoritative_raw_bytes_required")
    if not isinstance(request, PolicyRateRequest):
        raise PolicyRateQualificationError("request_not_approved")
    if request == authoritative_d_us_request():
        observations = parse_authoritative_bis_d_us_sdmx(raw_bytes, request)
        return AuthoritativePolicyRateParseResult(len(observations), observations)

    root, namespaces = _authoritative_sdmx_root_and_namespaces(raw_bytes)
    if root.tag != _D_US_ROOT_QNAME or _D_US_STRUCTURE_NAMESPACE not in namespaces.values():
        raise PolicyRateQualificationError("response_schema_invalid")

    header_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}Header"
    structure_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}Structure"
    usage_qname = f"{{{_SDMX_21_COMMON_NAMESPACE}}}StructureUsage"
    headers = root.findall(header_qname)
    if len(headers) != 1:
        raise PolicyRateQualificationError("response_series_mismatch")
    structures = headers[0].findall(structure_qname)
    if len(structures) != 1:
        raise PolicyRateQualificationError("response_series_mismatch")
    structure = structures[0]
    if (
        structure.attrib.get("structureID") != _D_US_STRUCTURE_REFERENCE
        or structure.attrib.get("namespace") != _D_US_STRUCTURE_NAMESPACE
        or structure.attrib.get("dimensionAtObservation") != "TIME_PERIOD"
    ):
        raise PolicyRateQualificationError("response_series_mismatch")
    usages = structure.findall(usage_qname)
    references = list(usages[0]) if len(usages) == 1 else []
    if len(references) != 1 or references[0].tag != "Ref" or (
        references[0].attrib.get("agencyID"),
        references[0].attrib.get("id"),
        references[0].attrib.get("version"),
    ) != ("BIS", "WS_CBPOL", "1.0"):
        raise PolicyRateQualificationError("response_series_mismatch")

    dataset_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}DataSet"
    datasets = root.findall(dataset_qname)
    all_datasets = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "DataSet"]
    if len(datasets) != 1 or all_datasets != datasets:
        raise PolicyRateQualificationError("response_schema_invalid")
    dataset = datasets[0]
    if (
        dataset.attrib.get(_D_US_DATA_SCOPE_QNAME) != "DataStructure"
        or dataset.attrib.get(_D_US_STRUCTURE_REF_QNAME) != _D_US_STRUCTURE_REFERENCE
        or _expanded_sdmx_qname(
            dataset.attrib.get(f"{{{_XSI_NAMESPACE}}}type"), namespaces
        )
        != f"{{{_D_US_STRUCTURE_NAMESPACE}}}DataSetType"
    ):
        raise PolicyRateQualificationError("response_series_mismatch")
    if (
        dataset.attrib.get("UNIT_MEASURE") != "368"
        or dataset.attrib.get("UNIT_MULT") != "0"
    ):
        raise PolicyRateQualificationError("response_unit_mismatch")

    series = [item for item in dataset if item.tag == "Series"]
    all_series = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "Series"]
    if len(series) != 1 or all_series != series:
        raise PolicyRateQualificationError("response_schema_invalid")
    series_element = series[0]
    expected_area = request.series.series_key.split(".", 1)[1]
    if (
        series_element.attrib.get("FREQ") != "D"
        or series_element.attrib.get("REF_AREA") != expected_area
    ):
        raise PolicyRateQualificationError("response_series_mismatch")

    observation_elements = tuple(item for item in series_element if item.tag == "Obs")
    all_observations = [
        item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "Obs"
    ]
    if not observation_elements or all_observations != list(observation_elements):
        raise PolicyRateQualificationError("response_schema_invalid")

    observed_dates: list[date] = []
    for observation in observation_elements:
        timestamp = observation.attrib.get("TIME_PERIOD")
        try:
            if timestamp is None or len(timestamp) != 10:
                raise ValueError
            observed = date.fromisoformat(timestamp)
        except (TypeError, ValueError) as exc:
            raise PolicyRateQualificationError("observation_timestamp_invalid") from exc
        if observed > MAX_OBSERVATION_DATE:
            raise PolicyRateQualificationError("sealed_window_violation")
        if observed < request.start or observed > request.end:
            raise PolicyRateQualificationError("observation_outside_request")
        observed_dates.append(observed)

    if len(set(observed_dates)) != len(observed_dates):
        raise PolicyRateQualificationError("duplicate_observation")
    if observed_dates != sorted(observed_dates):
        raise PolicyRateQualificationError("observation_order_invalid")

    observations: list[PolicyRateObservation] = []
    for observed, observation in zip(observed_dates, observation_elements, strict=True):
        status = observation.attrib.get("OBS_STATUS")
        value = observation.attrib.get("OBS_VALUE")
        if status == "M" and value == "NaN":
            continue
        if status != "A":
            raise PolicyRateQualificationError("observation_status_value_invalid")
        if value is None:
            raise PolicyRateQualificationError("observation_value_invalid")
        try:
            parsed_value = Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PolicyRateQualificationError("observation_value_invalid") from exc
        if not parsed_value.is_finite():
            raise PolicyRateQualificationError("observation_value_invalid")
        observations.append(
            PolicyRateObservation(request.series.series_key, observed, parsed_value, "A")
        )
    if not observations:
        raise PolicyRateQualificationError("numeric_observations_missing")
    return AuthoritativePolicyRateParseResult(
        raw_row_count=len(observation_elements),
        observations=tuple(observations),
    )


@dataclass(frozen=True, init=False)
class PolicyRateSeriesManifest:
    request: PolicyRateRequest
    metadata: PolicyRateMetadata
    retrieved_at: datetime
    raw_sha256: str
    byte_count: int
    observations: tuple[PolicyRateObservation, ...]
    canonical_observation_hash: str
    dataset_id: str
    manifest_id: str
    parsed_min_observation_date: date = field(init=False)
    parsed_max_observation_date: date = field(init=False)
    raw_row_count: int = field(init=False)
    numeric_observation_count: int = field(init=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("use PolicyRateSeriesManifest.from_parts with exact raw bytes")

    @classmethod
    def from_parts(
        cls,
        request: PolicyRateRequest,
        metadata: PolicyRateMetadata,
        raw_bytes: bytes,
        retrieved_at: datetime,
        observations: Sequence[PolicyRateObservation] | None = None,
    ) -> PolicyRateSeriesManifest:
        if not isinstance(request, PolicyRateRequest) or not isinstance(
            metadata, PolicyRateMetadata
        ):
            raise ValueError("validated request and metadata are required")
        if metadata.series_key != request.series.series_key:
            raise ValueError("metadata/request series mismatch")
        retrieved = _utc(retrieved_at, "retrieved_at")
        rows = parse_bis_csv(raw_bytes, request.series)
        if observations is not None and tuple(observations) != rows:
            raise PolicyRateQualificationError("raw_parsed_mismatch")
        if any(
            item.observation_date < request.start or item.observation_date > request.end
            for item in rows
        ):
            raise PolicyRateQualificationError("observation_outside_request")
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        observation_hash = canonical_sha256(rows)
        stable = canonical_sha256(
            {
                "format": 1,
                "request_fingerprint": request.fingerprint,
                "metadata_identity": metadata.stable_identity,
                "raw_sha256": raw_hash,
                "canonical_observation_hash": observation_hash,
                "raw_row_count": len(rows),
                "numeric_observation_count": len(rows),
                "revision": metadata.revision,
            }
        )
        manifest = canonical_sha256(
            {
                "format": 1,
                "dataset_id": stable,
                "retrieved_at": retrieved,
                "byte_count": len(raw_bytes),
                "media_type": metadata.media_type,
            }
        )
        instance = object.__new__(cls)
        for name, value in (
            ("request", request),
            ("metadata", metadata),
            ("retrieved_at", retrieved),
            ("raw_sha256", raw_hash),
            ("byte_count", len(raw_bytes)),
            ("observations", rows),
            ("canonical_observation_hash", observation_hash),
            ("dataset_id", stable),
            ("manifest_id", manifest),
            ("parsed_min_observation_date", rows[0].observation_date),
            ("parsed_max_observation_date", rows[-1].observation_date),
            ("raw_row_count", len(rows)),
            ("numeric_observation_count", len(rows)),
        ):
            object.__setattr__(instance, name, value)
        return instance


def build_series_manifest(
    request: PolicyRateRequest,
    metadata: PolicyRateMetadata,
    raw_bytes: bytes,
    retrieved_at: datetime,
) -> PolicyRateSeriesManifest:
    return PolicyRateSeriesManifest.from_parts(request, metadata, raw_bytes, retrieved_at)


def _validated_authoritative_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("source URL is not an approved authoritative source")
    if unicodedata.normalize("NFKC", value) != value:
        raise ValueError("source URL has ambiguous Unicode normalization")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source URL is not an approved authoritative source") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port is not None
        or not any(
            host == domain or host.endswith(f".{domain}") for domain in OFFICIAL_DOMAINS.values()
        )
    ):
        raise ValueError("source URL is not an approved authoritative source")
    decoded_path = parsed.path
    for _ in range(4):
        if re.search(r"%(?:2f|5c)", decoded_path, flags=re.IGNORECASE):
            raise ValueError("source URL path is unsafe")
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    if (
        "%" in decoded_path
        or "\\" in decoded_path
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded_path)
        or any(segment == ".." for segment in decoded_path.split("/"))
    ):
        raise ValueError("source URL path is unsafe")
    return value


@dataclass(frozen=True)
class PolicySourceEvidence:
    source_url: str
    retrieved_at: datetime
    content_hash: str
    byte_count: int
    media_type: str
    source_kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_url", _validated_authoritative_url(self.source_url))
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at, "retrieved_at"))
        _sha(self.content_hash, "content_hash")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count <= 0
        ):
            raise ValueError("byte_count must be positive")
        object.__setattr__(self, "media_type", _media_type(self.media_type))
        object.__setattr__(self, "source_kind", _identifier(self.source_kind, "source_kind"))


@dataclass(frozen=True)
class PolicyRateEvent:
    event_id: str
    kind: PolicyEventKind
    currency: str
    central_bank_id: str
    policy_instrument_id: str
    announcement_lower: datetime
    announcement_upper: datetime
    announcement_precision: TimePrecision
    effective_lower: datetime
    effective_upper: datetime
    effective_precision: TimePrecision
    source_timezone: str
    old_rate: Decimal | str | None
    new_rate: Decimal | str
    source: PolicySourceEvidence
    evidence_classification: EvidenceClassification
    ambiguity: AmbiguityState
    conflict: AmbiguityState

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _identifier(self.event_id, "event_id"))
        if not isinstance(self.kind, PolicyEventKind):
            raise ValueError("event kind is invalid")
        currency = self.currency.strip().upper() if isinstance(self.currency, str) else ""
        if currency not in APPROVED_BIS_SERIES:
            raise ValueError("event currency is unsupported")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(
            self, "central_bank_id", _identifier(self.central_bank_id, "central_bank_id")
        )
        object.__setattr__(
            self,
            "policy_instrument_id",
            _identifier(self.policy_instrument_id, "policy_instrument_id"),
        )
        for lower_name, upper_name, precision_name in (
            ("announcement_lower", "announcement_upper", "announcement_precision"),
            ("effective_lower", "effective_upper", "effective_precision"),
        ):
            lower = _utc(getattr(self, lower_name), lower_name)
            upper = _utc(getattr(self, upper_name), upper_name)
            precision = getattr(self, precision_name)
            if not isinstance(precision, TimePrecision) or upper < lower:
                raise ValueError("event time bounds are invalid")
            if precision is TimePrecision.EXACT_TIMESTAMP and upper != lower:
                raise ValueError("exact timestamps require equal bounds")
            if precision is TimePrecision.DATE_ONLY and upper <= lower:
                raise ValueError("date-only evidence requires a conservative interval")
            object.__setattr__(self, lower_name, lower)
            object.__setattr__(self, upper_name, upper)
        object.__setattr__(
            self, "source_timezone", _identifier(self.source_timezone, "source_timezone")
        )
        object.__setattr__(
            self,
            "old_rate",
            _decimal(self.old_rate, "old_rate", optional=self.kind is PolicyEventKind.BASELINE),
        )
        object.__setattr__(self, "new_rate", _decimal(self.new_rate, "new_rate"))
        if self.kind is not PolicyEventKind.BASELINE and self.old_rate is None:
            raise ValueError("non-baseline event requires old_rate")
        if not isinstance(self.source, PolicySourceEvidence):
            raise ValueError("source evidence is required")
        host = (urlsplit(self.source.source_url).hostname or "").lower()
        domain = OFFICIAL_DOMAINS[currency]
        if host != domain and not host.endswith(f".{domain}"):
            raise ValueError("source authority does not match currency")
        if not isinstance(self.evidence_classification, EvidenceClassification):
            raise ValueError("evidence classification is invalid")
        if not isinstance(self.ambiguity, AmbiguityState) or not isinstance(
            self.conflict, AmbiguityState
        ):
            raise ValueError("ambiguity/conflict state is invalid")

    @property
    def identity(self) -> str:
        return canonical_sha256(self)


def event_is_eligible(item: PolicyRateEvent, cutoff_at: datetime) -> bool:
    cutoff = _utc(cutoff_at, "cutoff_at")
    if item.ambiguity is not AmbiguityState.CLEAR or item.conflict is not AmbiguityState.CLEAR:
        return False
    if item.announcement_lower.date() == cutoff.date():
        return False
    return item.announcement_upper <= cutoff and item.effective_upper <= cutoff


@dataclass(frozen=True)
class PolicyEventManifest:
    events: tuple[PolicyRateEvent, ...]
    manifest_id: str = field(init=False)

    def __post_init__(self) -> None:
        events = tuple(self.events)
        if any(not isinstance(item, PolicyRateEvent) for item in events):
            raise ValueError("event manifest contains invalid items")
        ids = [item.event_id for item in events]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate event_id")
        transition_keys = [
            (item.currency, item.kind, item.effective_lower, item.effective_upper, item.new_rate)
            for item in events
        ]
        if len(transition_keys) != len(set(transition_keys)):
            raise ValueError("duplicate policy transition")
        ordered = tuple(
            sorted(events, key=lambda item: (item.currency, item.effective_upper, item.event_id))
        )
        object.__setattr__(self, "events", ordered)
        object.__setattr__(self, "manifest_id", canonical_sha256({"format": 1, "events": ordered}))


@dataclass(frozen=True)
class PolicyConcordanceResult:
    currency: str
    series_dataset_id: str
    event_manifest_id: str
    status: ConcordanceStatus
    reasons: tuple[str, ...]
    result_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.currency not in APPROVED_BIS_SERIES:
            raise ValueError("concordance currency is unsupported")
        _sha(self.series_dataset_id, "series_dataset_id")
        _sha(self.event_manifest_id, "event_manifest_id")
        if not isinstance(self.status, ConcordanceStatus):
            raise ValueError("concordance status is invalid")
        reasons = tuple(sorted({_identifier(item, "concordance_reason") for item in self.reasons}))
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(
            self,
            "result_id",
            canonical_sha256(
                {
                    "format": 1,
                    "currency": self.currency,
                    "series_dataset_id": self.series_dataset_id,
                    "event_manifest_id": self.event_manifest_id,
                    "status": self.status,
                    "reasons": reasons,
                }
            ),
        )


def reconcile_policy_series(
    series_manifest: PolicyRateSeriesManifest, event_manifest: PolicyEventManifest
) -> PolicyConcordanceResult:
    currency = series_manifest.request.series.currency
    events = tuple(item for item in event_manifest.events if item.currency == currency)
    reasons: set[str] = set()
    if any(
        item.conflict is AmbiguityState.CONFLICTING or item.ambiguity is AmbiguityState.CONFLICTING
        for item in events
    ):
        reasons.add("conflicting_official_evidence")
    if any(item.ambiguity is AmbiguityState.AMBIGUOUS for item in events):
        reasons.add("ambiguous_official_evidence")
    rows = series_manifest.observations
    baselines = [item for item in events if item.kind is PolicyEventKind.BASELINE]
    if not baselines:
        reasons.add("missing_baseline")
    elif len(baselines) != 1 or baselines[0].new_rate != rows[0].value:
        reasons.add("baseline_mismatch")
    transitions: list[tuple[date, Decimal, Decimal]] = []
    previous = rows[0]
    for current in rows[1:]:
        if current.value != previous.value:
            transitions.append((current.observation_date, previous.value, current.value))
        previous = current
    official = [item for item in events if item.kind is not PolicyEventKind.BASELINE]
    matched_ids: set[str] = set()
    for effective_date, old_rate, new_rate in transitions:
        matches = [
            item
            for item in official
            if item.effective_upper.date() == effective_date
            and item.old_rate == old_rate
            and item.new_rate == new_rate
            and item.ambiguity is AmbiguityState.CLEAR
            and item.conflict is AmbiguityState.CLEAR
        ]
        if len(matches) != 1:
            reasons.add("unexplained_bis_transition")
        else:
            matched_ids.add(matches[0].event_id)
    first_date = series_manifest.request.start
    last_date = series_manifest.request.end
    if any(
        first_date <= item.effective_upper.date() <= last_date and item.event_id not in matched_ids
        for item in official
    ):
        reasons.add("official_transition_absent_from_bis")
    status = ConcordanceStatus.PASS if not reasons else ConcordanceStatus.FAIL
    return PolicyConcordanceResult(
        currency,
        series_manifest.dataset_id,
        event_manifest.manifest_id,
        status,
        tuple(reasons),
    )


@dataclass(frozen=True, order=True)
class SpotObservationReference:
    pair: str
    dataset_id: str
    bar_open: datetime
    bar_close: datetime
    value_field: str
    closed: bool

    def __post_init__(self) -> None:
        if self.pair not in APPROVED_PAIRS:
            raise ValueError("spot pair is unsupported")
        _sha(self.dataset_id, "dataset_id")
        opened = _utc(self.bar_open, "bar_open")
        closed_at = _utc(self.bar_close, "bar_close")
        if closed_at.date() > MAX_OBSERVATION_DATE:
            raise PolicyRateQualificationError("sealed_window_violation")
        if self.value_field != "close":
            raise ValueError("Candidate B spot reference must use close")
        if self.closed is not True or closed_at - opened != timedelta(days=1):
            raise ValueError("spot reference must identify a closed D1 bar")
        object.__setattr__(self, "bar_open", opened)
        object.__setattr__(self, "bar_close", closed_at)


@dataclass(frozen=True)
class SpotPanelManifestReference:
    manifest_id: str
    dataset_ids: tuple[str, ...]
    observations: tuple[SpotObservationReference, ...] = ()

    def __post_init__(self) -> None:
        _sha(self.manifest_id, "manifest_id")
        dataset_ids = tuple(self.dataset_ids)
        if len(dataset_ids) != len(APPROVED_PAIRS):
            raise ValueError("spot panel must bind exactly seven datasets")
        for item in dataset_ids:
            _sha(item, "dataset_id")
        observations = tuple(self.observations)
        if any(not isinstance(item, SpotObservationReference) for item in observations):
            raise ValueError("spot panel contains invalid observation references")
        expected_ids = dict(zip(APPROVED_PAIRS, dataset_ids, strict=True))
        if any(item.dataset_id != expected_ids[item.pair] for item in observations):
            raise ValueError("spot observation dataset does not match panel")
        if len(set(observations)) != len(observations):
            raise ValueError("duplicate spot observation reference")
        object.__setattr__(self, "dataset_ids", dataset_ids)
        object.__setattr__(
            self,
            "observations",
            tuple(sorted(observations, key=lambda item: (item.bar_close, item.pair))),
        )


@dataclass(frozen=True, order=True)
class PolicyStateReference:
    currency: str
    series_key: str
    dataset_id: str
    observation_id: str
    event_id: str
    policy_instrument_id: str
    observation_date: date
    observation_value: Decimal | str
    observation_status: str
    announcement_upper: datetime
    effective_upper: datetime
    eligible: bool

    def __post_init__(self) -> None:
        currency = self.currency.strip().upper() if isinstance(self.currency, str) else ""
        if APPROVED_BIS_SERIES.get(currency) != self.series_key:
            raise ValueError("policy-state series mismatch")
        object.__setattr__(self, "currency", currency)
        _sha(self.dataset_id, "dataset_id")
        _sha(self.observation_id, "observation_id")
        object.__setattr__(self, "event_id", _identifier(self.event_id, "event_id"))
        object.__setattr__(
            self,
            "policy_instrument_id",
            _identifier(self.policy_instrument_id, "policy_instrument_id"),
        )
        if (
            not isinstance(self.observation_date, date)
            or self.observation_date > MAX_OBSERVATION_DATE
        ):
            raise ValueError("policy-state observation date is invalid")
        object.__setattr__(
            self, "observation_value", _decimal(self.observation_value, "observation_value")
        )
        if self.observation_status not in APPROVED_BIS_OBSERVATION_STATUS_CODES:
            raise ValueError("policy-state observation status is invalid")
        object.__setattr__(
            self, "announcement_upper", _utc(self.announcement_upper, "announcement_upper")
        )
        object.__setattr__(self, "effective_upper", _utc(self.effective_upper, "effective_upper"))
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be boolean")


@dataclass(frozen=True)
class CandidateBFormation:
    cohort_id: str
    formation_month: str
    formation_at: datetime
    cutoff_at: datetime
    exit_at: datetime
    split: FormationSplit
    purged: bool
    spot_observations: tuple[SpotObservationReference, ...]
    policy_states: tuple[PolicyStateReference, ...]
    pit_eligible: bool
    complete: bool
    rejection_reason: str | None
    source_manifest_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cohort_id", _identifier(self.cohort_id, "cohort_id"))
        if not re.fullmatch(r"\d{4}-\d{2}", self.formation_month):
            raise ValueError("formation_month must be YYYY-MM")
        formation = _utc(self.formation_at, "formation_at")
        cutoff = _utc(self.cutoff_at, "cutoff_at")
        exit_at = _utc(self.exit_at, "exit_at")
        if formation.date() > MAX_OBSERVATION_DATE or exit_at.date() > MAX_OBSERVATION_DATE:
            raise PolicyRateQualificationError("sealed_window_violation")
        if cutoff != datetime.combine(formation.date(), datetime.min.time(), tzinfo=UTC):
            raise ValueError("cutoff must be start of formation UTC date")
        if exit_at <= formation:
            raise ValueError("exit must follow formation")
        expected_month = f"{formation.year:04d}-{formation.month:02d}"
        if self.formation_month != expected_month:
            raise ValueError("formation month does not match F_m")
        next_month_index = formation.year * 12 + formation.month
        expected_exit_year, expected_exit_zero_month = divmod(next_month_index, 12)
        if (exit_at.year, exit_at.month) != (
            expected_exit_year,
            expected_exit_zero_month + 1,
        ):
            raise ValueError("exit must be in the calendar month following F_m")
        if not isinstance(self.split, FormationSplit):
            raise ValueError("formation split is invalid")
        expected_purged = formation.date() <= TRAIN_OUTCOME_END < exit_at.date()
        expected_split = (
            FormationSplit.PURGED
            if expected_purged
            else FormationSplit.TRAIN
            if exit_at.date() <= TRAIN_OUTCOME_END
            else FormationSplit.VALIDATION
        )
        if self.split is not expected_split:
            raise ValueError("formation split does not match outcome boundary")
        if self.purged is not expected_purged:
            raise ValueError("formation purge flag does not match outcome boundary")
        object.__setattr__(self, "formation_at", formation)
        object.__setattr__(self, "cutoff_at", cutoff)
        object.__setattr__(self, "exit_at", exit_at)
        spots = tuple(self.spot_observations)
        states = tuple(self.policy_states)
        if any(not isinstance(item, SpotObservationReference) for item in spots):
            raise ValueError("formation contains invalid spot references")
        if any(not isinstance(item, PolicyStateReference) for item in states):
            raise ValueError("formation contains invalid policy-state references")
        object.__setattr__(self, "spot_observations", spots)
        object.__setattr__(self, "policy_states", states)
        fingerprints = tuple(self.source_manifest_fingerprints)
        if not fingerprints:
            raise ValueError("source manifest fingerprints are required")
        for item in fingerprints:
            _sha(item, "source_manifest_fingerprint")
        object.__setattr__(self, "source_manifest_fingerprints", fingerprints)
        if self.rejection_reason is not None:
            object.__setattr__(
                self, "rejection_reason", _identifier(self.rejection_reason, "rejection_reason")
            )
        if any(
            not isinstance(value, bool) for value in (self.purged, self.pit_eligible, self.complete)
        ):
            raise ValueError("formation qualification flags must be boolean")
        if self.complete and (
            self.purged
            or self.rejection_reason is not None
            or not self.pit_eligible
            or len(spots) != len(APPROVED_PAIRS)
            or {item.pair for item in spots} != set(APPROVED_PAIRS)
            or len(states) != len(APPROVED_BIS_SERIES)
            or {item.currency for item in states} != set(APPROVED_BIS_SERIES)
            or any(item.bar_close != formation for item in spots)
            or any(
                not item.eligible
                or item.announcement_upper > cutoff
                or item.effective_upper > cutoff
                or item.observation_date > cutoff.date()
                for item in states
            )
        ):
            raise ValueError("complete formation requires complete point-in-time evidence")
        if not self.complete and self.rejection_reason is None:
            raise ValueError("incomplete formation requires a rejection reason")


def qualify_formation(
    *,
    cohort_id: str,
    formation_month: str,
    formation_at: datetime,
    cutoff_at: datetime,
    exit_at: datetime,
    split: FormationSplit,
    purged: bool,
    spot_observations: Iterable[SpotObservationReference],
    policy_states: Iterable[PolicyStateReference],
    source_manifest_fingerprints: Iterable[str],
) -> CandidateBFormation:
    spots = tuple(spot_observations)
    states = tuple(policy_states)
    cutoff = _utc(cutoff_at, "cutoff_at")
    reason: str | None = None
    if purged or split is FormationSplit.PURGED:
        reason = "split_boundary_purge"
    elif len(spots) != len(APPROVED_PAIRS) or {item.pair for item in spots} != set(APPROVED_PAIRS):
        reason = "missing_spot_observation"
    elif len(states) != len(APPROVED_BIS_SERIES) or {item.currency for item in states} != set(
        APPROVED_BIS_SERIES
    ):
        reason = "missing_policy_state"
    elif any(item.bar_close != _utc(formation_at, "formation_at") for item in spots):
        reason = "spot_not_closed_at_formation"
    elif any(
        not item.eligible
        or item.announcement_upper > cutoff
        or item.effective_upper > cutoff
        or item.observation_date > cutoff.date()
        for item in states
    ):
        reason = "policy_state_not_point_in_time_eligible"
    complete = reason is None
    return CandidateBFormation(
        cohort_id,
        formation_month,
        formation_at,
        cutoff,
        exit_at,
        split,
        purged,
        spots,
        states,
        complete,
        complete,
        reason,
        tuple(source_manifest_fingerprints),
    )


@dataclass(frozen=True)
class CandidateBFormationManifest:
    formations: tuple[CandidateBFormation, ...]
    manifest_id: str = field(init=False)

    def __post_init__(self) -> None:
        items = tuple(self.formations)
        if any(not isinstance(item, CandidateBFormation) for item in items):
            raise ValueError("formation manifest contains invalid items")
        ids = [item.cohort_id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate cohort_id")
        ordered = tuple(sorted(items, key=lambda item: (item.formation_at, item.cohort_id)))
        object.__setattr__(self, "formations", ordered)
        object.__setattr__(
            self, "manifest_id", canonical_sha256({"format": 1, "formations": ordered})
        )

    @property
    def train_count(self) -> int:
        return sum(item.complete and item.split is FormationSplit.TRAIN for item in self.formations)

    @property
    def validation_count(self) -> int:
        return sum(
            item.complete and item.split is FormationSplit.VALIDATION for item in self.formations
        )

    @property
    def total_count(self) -> int:
        return self.train_count + self.validation_count

    @property
    def qualified(self) -> bool:
        return (
            self.train_count == EXPECTED_TRAIN_COHORTS
            and self.validation_count == EXPECTED_VALIDATION_COHORTS
            and self.total_count == EXPECTED_TOTAL_COHORTS
            and all(item.complete or item.purged for item in self.formations)
        )


@dataclass(frozen=True)
class CandidateBQualificationResult:
    qualified: bool
    reasons: tuple[str, ...]
    series_manifest_ids: tuple[str, ...]
    event_manifest_id: str
    concordance_result_ids: tuple[str, ...]
    spot_manifest_id: str
    formation_manifest_id: str
    qualification_id: str = field(init=False)

    def __post_init__(self) -> None:
        reasons = tuple(
            sorted({_identifier(item, "qualification_reason") for item in self.reasons})
        )
        series_ids = tuple(sorted(self.series_manifest_ids))
        concordance_ids = tuple(sorted(self.concordance_result_ids))
        for name, values in (
            ("series_manifest_id", series_ids),
            ("concordance_result_id", concordance_ids),
        ):
            for item in values:
                _sha(item, name)
        for name in ("event_manifest_id", "spot_manifest_id", "formation_manifest_id"):
            _sha(getattr(self, name), name)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "series_manifest_ids", series_ids)
        object.__setattr__(self, "concordance_result_ids", concordance_ids)
        if not isinstance(self.qualified, bool):
            raise ValueError("qualified must be boolean")
        if self.qualified and (
            reasons
            or len(series_ids) != len(APPROVED_BIS_SERIES)
            or len(set(series_ids)) != len(APPROVED_BIS_SERIES)
            or len(concordance_ids) != len(APPROVED_BIS_SERIES)
            or len(set(concordance_ids)) != len(APPROVED_BIS_SERIES)
        ):
            raise ValueError("qualified result requires complete unique evidence")
        if not self.qualified and not reasons:
            raise ValueError("unqualified result requires a stable reason")
        object.__setattr__(
            self,
            "qualification_id",
            canonical_sha256(
                {
                    "format": 1,
                    "qualified": self.qualified,
                    "reasons": reasons,
                    "series_manifest_ids": series_ids,
                    "event_manifest_id": self.event_manifest_id,
                    "concordance_result_ids": concordance_ids,
                    "spot_manifest_id": self.spot_manifest_id,
                    "formation_manifest_id": self.formation_manifest_id,
                }
            ),
        )
