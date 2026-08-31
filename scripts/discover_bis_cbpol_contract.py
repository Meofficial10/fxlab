"""Non-authoritative BIS WS_CBPOL contract discovery boundary.

The module has two fixed request shapes and performs at most one transport call.  Importing it, or
invoking it without the explicit CLI authorization flag, cannot construct a network transport.
Discovery artifacts are deliberately incompatible with Candidate B qualification and run-identity
contracts.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from fxlab.data.policy_rates import MAX_OBSERVATION_DATE, canonical_json, canonical_sha256

DISCOVERY_CLASSIFICATION = "NON_AUTHORITATIVE_DISCOVERY"
FAILED_DISCOVERY_CLASSIFICATION = "NON_AUTHORITATIVE_DISCOVERY_FAILED"
BIS_API_HOST = "stats.bis.org"
BIS_API_PREFIX = "/api/v2/"
DISCOVERY_SAMPLE_START = date(2023, 1, 1)
DISCOVERY_SAMPLE_END = date(2023, 1, 31)
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
RAW_DISCOVERY_ROOT = Path("data/raw/candidate_b/bis_discovery")

_STRUCTURE_URL = (
    "https://stats.bis.org/api/v2/structure/dataflow/BIS/WS_CBPOL/1.0"
    "?detail=referencepartial&references=descendants"
)
_PROBE_URL = (
    "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.US"
    "?startPeriod=2023-01-01&endPeriod=2023-01-31"
)
_STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+xml;version=2.1"
_PROBE_ACCEPT = "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
_RETAINED_HEADERS = frozenset(
    {
        "cache-control",
        "content-disposition",
        "content-location",
        "etag",
        "last-modified",
        "x-sdmx-version",
    }
)
_STRUCTURE_MEDIA_TYPES = frozenset(
    {
        "application/xml",
        "application/vnd.sdmx.structure+xml",
        "text/xml",
    }
)
_PROBE_MEDIA_TYPES = frozenset(
    {"application/vnd.sdmx.structurespecificdata+xml", "application/xml"}
)
_SDMX_21_MESSAGE_NAMESPACE = (
    "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
)
_SDMX_21_COMMON_NAMESPACE = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
_XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
_STRUCTURE_SPECIFIC_NAMESPACE = (
    "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow="
    "BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD"
)
_STRUCTURE_SPECIFIC_ROOT_QNAME = (
    f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}StructureSpecificData"
)
_PROBE_REPRESENTATION_IDENTITY = "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
_PROBE_STRUCTURE_ID = "BIS_WS_CBPOL_1_0"
_PROBE_DATE_SET = tuple(date(2023, 1, day) for day in range(1, 32))


class DiscoveryFailure(ValueError):
    """Stable, non-sensitive discovery failure."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class DiscoveryTarget(StrEnum):
    STRUCTURE = "structure"
    D_US_SCHEMA_PROBE = "d_us_schema_probe"


@dataclass(frozen=True)
class DiscoveryRequest:
    target: DiscoveryTarget
    url: str
    accept: str
    series_key: str | None = None
    start: date | None = None
    end: date | None = None

    def __post_init__(self) -> None:
        _validate_request_values(self)

    @property
    def request_identity(self) -> str:
        return canonical_sha256(
            {
                "format": 1,
                "classification": DISCOVERY_CLASSIFICATION,
                "target": self.target,
                "url": self.url,
                "accept": self.accept,
                "series_key": self.series_key,
                "start": self.start,
                "end": self.end,
            }
        )


def _validate_request_values(request: DiscoveryRequest) -> None:
    if request.target == DiscoveryTarget.STRUCTURE:
        expected = (_STRUCTURE_URL, _STRUCTURE_ACCEPT, None, None, None)
    elif request.target == DiscoveryTarget.D_US_SCHEMA_PROBE:
        if isinstance(request.end, date) and request.end > MAX_OBSERVATION_DATE:
            raise DiscoveryFailure("sealed_window_violation")
        expected = (
            _PROBE_URL,
            _PROBE_ACCEPT,
            "D.US",
            DISCOVERY_SAMPLE_START,
            DISCOVERY_SAMPLE_END,
        )
    else:
        raise DiscoveryFailure("request_not_approved")
    actual = (request.url, request.accept, request.series_key, request.start, request.end)
    if actual != expected:
        raise DiscoveryFailure("request_not_approved")
    parsed = urlsplit(request.url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DiscoveryFailure("request_not_approved") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != BIS_API_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith(BIS_API_PREFIX)
    ):
        raise DiscoveryFailure("request_not_approved")
    query = tuple(parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True))
    expected_query = (
        (("detail", "referencepartial"), ("references", "descendants"))
        if request.target == DiscoveryTarget.STRUCTURE
        else (("startPeriod", "2023-01-01"), ("endPeriod", "2023-01-31"))
    )
    if query != expected_query or "latest" in request.url.lower():
        raise DiscoveryFailure("request_not_approved")


def validate_discovery_request(request: DiscoveryRequest) -> None:
    if not isinstance(request, DiscoveryRequest):
        raise DiscoveryFailure("request_not_approved")
    _validate_request_values(request)


def build_structure_request() -> DiscoveryRequest:
    return DiscoveryRequest(DiscoveryTarget.STRUCTURE, _STRUCTURE_URL, _STRUCTURE_ACCEPT)


def build_d_us_schema_probe_request(*, metadata_insufficient: bool) -> DiscoveryRequest:
    if metadata_insufficient is not True:
        raise DiscoveryFailure("metadata_discovery_required_first")
    return DiscoveryRequest(
        DiscoveryTarget.D_US_SCHEMA_PROBE,
        _PROBE_URL,
        _PROBE_ACCEPT,
        "D.US",
        DISCOVERY_SAMPLE_START,
        DISCOVERY_SAMPLE_END,
    )


def discovery_plan() -> tuple[DiscoveryRequest, DiscoveryRequest]:
    """Return the review order; it does not execute or automatically chain requests."""

    return (build_structure_request(), build_d_us_schema_probe_request(metadata_insufficient=True))


@dataclass(frozen=True)
class DiscoveryHttpResponse:
    status_code: int
    final_url: str
    media_type: str
    headers: Mapping[str, str]
    raw_bytes: bytes

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise DiscoveryFailure("transport_response_invalid")
        if not isinstance(self.final_url, str) or not self.final_url:
            raise DiscoveryFailure("transport_response_invalid")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise DiscoveryFailure("transport_response_invalid")
        if not isinstance(self.raw_bytes, bytes):
            raise DiscoveryFailure("transport_response_invalid")
        normalized = {str(key).lower(): str(value) for key, value in self.headers.items()}
        object.__setattr__(self, "headers", MappingProxyType(dict(sorted(normalized.items()))))


class DiscoveryTransport(Protocol):
    def fetch(
        self,
        request: DiscoveryRequest,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> DiscoveryHttpResponse: ...


@dataclass(frozen=True)
class _SchemaFacts:
    structure_identifiers: tuple[str, ...] = ()
    dsd_identity: str | None = None
    codelist_identities: tuple[str, ...] = ()
    returned_columns: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    status_vocabulary: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    scales: tuple[str, ...] = ()
    dsd_content_fingerprint: str | None = None
    codelist_content_fingerprints: tuple[tuple[str, str], ...] = ()
    representation_identity: str | None = None
    root_qname: str | None = None
    structure_specific_namespace: str | None = None
    series_count: int = 0
    observation_count: int = 0
    parsed_min_observation_date: date | None = None
    parsed_max_observation_date: date | None = None


@dataclass(frozen=True)
class BisDiscoveryArtifact:
    classification: str
    target: DiscoveryTarget
    retrieved_at: datetime
    request_identity: str
    exact_url: str
    http_status: int
    content_type: str
    raw_sha256: str
    byte_count: int
    response_headers: Mapping[str, str]
    structure_identifiers: tuple[str, ...]
    dsd_identity: str | None
    codelist_identities: tuple[str, ...]
    returned_columns: tuple[str, ...]
    dimensions: tuple[str, ...]
    attributes: tuple[str, ...]
    status_vocabulary: tuple[str, ...]
    units: tuple[str, ...]
    scales: tuple[str, ...]
    dsd_content_fingerprint: str | None
    codelist_content_fingerprints: tuple[tuple[str, str], ...]
    representation_identity: str | None
    root_qname: str | None
    structure_specific_namespace: str | None
    series_count: int
    observation_count: int
    parsed_min_observation_date: date | None
    parsed_max_observation_date: date | None
    schema_fingerprint: str
    discovery_id: str
    authoritative_qualification_eligible: bool = False
    final_run_identity_eligible: bool = False
    r4_evidence_eligible: bool = False

    def __post_init__(self) -> None:
        if self.classification != DISCOVERY_CLASSIFICATION:
            raise DiscoveryFailure("discovery_classification_invalid")
        if any(
            (
                self.authoritative_qualification_eligible,
                self.final_run_identity_eligible,
                self.r4_evidence_eligible,
            )
        ):
            raise DiscoveryFailure("discovery_cannot_be_authoritative")
        headers = {str(key).lower(): str(value) for key, value in self.response_headers.items()}
        if not set(headers).issubset(_RETAINED_HEADERS):
            raise DiscoveryFailure("response_header_not_approved")
        object.__setattr__(
            self, "response_headers", MappingProxyType(dict(sorted(headers.items())))
        )


@dataclass(frozen=True)
class BisDiscoveryResult:
    raw_bytes: bytes
    artifact: BisDiscoveryArtifact


@dataclass(frozen=True)
class FailedBisDiscoveryArtifact:
    classification: str
    target: DiscoveryTarget
    retrieved_at: datetime
    request_identity: str
    exact_url: str
    http_status: int
    content_type: str
    raw_sha256: str
    byte_count: int
    response_headers: Mapping[str, str]
    failure_reason: str
    authoritative_qualification_eligible: bool = False
    final_run_identity_eligible: bool = False
    r4_evidence_eligible: bool = False

    def __post_init__(self) -> None:
        if self.classification != FAILED_DISCOVERY_CLASSIFICATION:
            raise DiscoveryFailure("discovery_classification_invalid")
        if any(
            (
                self.authoritative_qualification_eligible,
                self.final_run_identity_eligible,
                self.r4_evidence_eligible,
            )
        ):
            raise DiscoveryFailure("discovery_cannot_be_authoritative")
        if not isinstance(self.failure_reason, str) or not self.failure_reason:
            raise DiscoveryFailure("discovery_artifact_invalid")
        headers = {str(key).lower(): str(value) for key, value in self.response_headers.items()}
        if not set(headers).issubset(_RETAINED_HEADERS):
            raise DiscoveryFailure("response_header_not_approved")
        object.__setattr__(
            self, "response_headers", MappingProxyType(dict(sorted(headers.items())))
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str | None:
    if not tag.startswith("{") or "}" not in tag:
        return None
    return tag[1:].split("}", 1)[0]


def _sdmx_identity(element: ET.Element) -> str | None:
    agency = element.attrib.get("agencyID") or element.attrib.get("agencyId")
    resource_id = element.attrib.get("id")
    version = element.attrib.get("version")
    if not all((agency, resource_id, version)):
        return None
    return f"{agency}:{resource_id}({version})"


def _ordered_unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _referenced_codelists(component: ET.Element) -> tuple[str, ...]:
    references: list[str] = []
    for element in component.iter():
        if _local_name(element.tag) != "Ref":
            continue
        reference_class = element.attrib.get("class", "").lower()
        if reference_class != "codelist":
            continue
        identity = _sdmx_identity(element)
        if identity is None:
            raise DiscoveryFailure("metadata_response_malformed")
        references.append(identity)
    return _ordered_unique(references)


def _required_component_codelist(
    components: tuple[tuple[str, str, tuple[str, ...]], ...], component_id: str
) -> str:
    matches = [item for item in components if item[1] == component_id]
    if len(matches) != 1 or len(matches[0][2]) != 1:
        raise DiscoveryFailure("metadata_response_malformed")
    return matches[0][2][0]


def _parse_structure_xml(raw_bytes: bytes) -> _SchemaFacts:
    upper = raw_bytes.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise DiscoveryFailure("metadata_response_malformed")
    try:
        root = ET.fromstring(raw_bytes)
    except (ET.ParseError, ValueError) as exc:
        raise DiscoveryFailure("metadata_response_malformed") from exc
    if (
        _local_name(root.tag) != "Structure"
        or _namespace(root.tag) != _SDMX_21_MESSAGE_NAMESPACE
    ):
        raise DiscoveryFailure("metadata_representation_mismatch")

    flows = [
        element
        for element in root.iter()
        if _local_name(element.tag) == "Dataflow"
        and _sdmx_identity(element) == "BIS:WS_CBPOL(1.0)"
    ]
    if not flows:
        raise DiscoveryFailure("metadata_structure_incomplete")
    if len(flows) != 1:
        raise DiscoveryFailure("metadata_response_malformed")
    flow = flows[0]
    dsd_references: list[str] = []
    structure_attribute = flow.attrib.get("structure")
    if structure_attribute:
        dsd_references.append(structure_attribute)
    for element in flow.iter():
        if _local_name(element.tag) != "Ref":
            continue
        if element.attrib.get("class", "").lower() not in {
            "datastructure",
            "datastructuredefinition",
        }:
            continue
        identity = _sdmx_identity(element)
        if identity:
            dsd_references.append(identity)
    dsd_refs = _ordered_unique(dsd_references)
    if not dsd_refs:
        raise DiscoveryFailure("metadata_structure_incomplete")
    if len(dsd_refs) != 1:
        raise DiscoveryFailure("metadata_response_malformed")
    dsd_identity = dsd_refs[0]

    dsd_elements = [
        element
        for element in root.iter()
        if _local_name(element.tag) in {"DataStructure", "DataStructureDefinition"}
    ]
    if not dsd_elements:
        raise DiscoveryFailure("metadata_structure_incomplete")
    if len(dsd_elements) != 1 or _sdmx_identity(dsd_elements[0]) != dsd_identity:
        raise DiscoveryFailure("metadata_response_malformed")
    dsd_element = dsd_elements[0]

    codelist_elements = [
        element for element in root.iter() if _local_name(element.tag) == "Codelist"
    ]
    codelist_content: dict[str, tuple[str, ...]] = {}
    for codelist in codelist_elements:
        identity = _sdmx_identity(codelist)
        codes = tuple(
            item.attrib["id"]
            for item in codelist.iter()
            if _local_name(item.tag) == "Code" and item.attrib.get("id")
        )
        if identity is None or identity in codelist_content or not codes:
            raise DiscoveryFailure("metadata_response_malformed")
        codelist_content[identity] = codes

    components: list[tuple[str, str, tuple[str, ...]]] = []
    dimensions: list[str] = []
    attributes: list[str] = []
    for element in dsd_element.iter():
        kind = _local_name(element.tag)
        component_id = element.attrib.get("id")
        if kind in {"Dimension", "TimeDimension", "MeasureDimension"} and component_id:
            dimensions.append(component_id)
            components.append(("dimension", component_id, _referenced_codelists(element)))
        elif kind == "Attribute" and component_id:
            attributes.append(component_id)
            components.append(("attribute", component_id, _referenced_codelists(element)))
    if not {"FREQ", "REF_AREA"}.issubset(dimensions):
        raise DiscoveryFailure("metadata_response_malformed")
    if not {"OBS_STATUS", "UNIT_MEASURE", "UNIT_MULT"}.issubset(attributes):
        raise DiscoveryFailure("metadata_response_malformed")

    referenced_codelists = frozenset(
        reference for _, _, references in components for reference in references
    )
    if not referenced_codelists or referenced_codelists != frozenset(codelist_content):
        raise DiscoveryFailure("metadata_response_malformed")
    status_identity = _required_component_codelist(tuple(components), "OBS_STATUS")
    unit_identity = _required_component_codelist(tuple(components), "UNIT_MEASURE")
    scale_identity = _required_component_codelist(tuple(components), "UNIT_MULT")
    try:
        statuses = codelist_content[status_identity]
        units = codelist_content[unit_identity]
        scales = codelist_content[scale_identity]
    except KeyError as exc:
        raise DiscoveryFailure("metadata_response_malformed") from exc

    dsd_content_fingerprint = canonical_sha256(
        {"identity": dsd_identity, "components": tuple(components)}
    )
    codelist_fingerprints = tuple(
        (
            identity,
            canonical_sha256({"identity": identity, "codes": codelist_content[identity]}),
        )
        for identity in sorted(codelist_content)
    )
    return _SchemaFacts(
        structure_identifiers=("BIS:WS_CBPOL(1.0)", dsd_identity),
        dsd_identity=dsd_identity,
        codelist_identities=tuple(sorted(codelist_content)),
        dimensions=_ordered_unique(dimensions),
        attributes=_ordered_unique(attributes),
        status_vocabulary=statuses,
        units=units,
        scales=scales,
        dsd_content_fingerprint=dsd_content_fingerprint,
        codelist_content_fingerprints=codelist_fingerprints,
    )


def _parse_xml_with_namespaces(
    raw_bytes: bytes,
) -> tuple[ET.Element, Mapping[str, str]]:
    try:
        decoded_xml = raw_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise DiscoveryFailure("schema_response_malformed") from exc
    upper = decoded_xml.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise DiscoveryFailure("schema_response_malformed")
    namespaces: dict[str, str] = {}
    try:
        for _, (prefix, namespace) in ET.iterparse(
            io.BytesIO(raw_bytes), events=("start-ns",)
        ):
            previous = namespaces.get(prefix)
            if previous is not None and previous != namespace:
                raise DiscoveryFailure("schema_response_malformed")
            namespaces[prefix] = namespace
        root = ET.fromstring(raw_bytes)
    except DiscoveryFailure:
        raise
    except (ET.ParseError, UnicodeError, ValueError) as exc:
        raise DiscoveryFailure("schema_response_malformed") from exc
    return root, MappingProxyType(dict(namespaces))


def _expanded_lexical_qname(value: str | None, namespaces: Mapping[str, str]) -> str:
    if not value or value.count(":") != 1:
        raise DiscoveryFailure("response_series_mismatch")
    prefix, local_name = value.split(":", 1)
    namespace = namespaces.get(prefix)
    if not namespace or not local_name:
        raise DiscoveryFailure("response_series_mismatch")
    return f"{{{namespace}}}{local_name}"


def _validate_probe_observation_values(
    observations: tuple[ET.Element, ...],
) -> tuple[tuple[str, ...], bool]:
    statuses: set[str] = set()
    has_observation_confidence = False
    for observation in observations:
        value = observation.attrib.get("OBS_VALUE")
        status = observation.attrib.get("OBS_STATUS")
        if not value or not status:
            raise DiscoveryFailure("schema_response_malformed")
        try:
            parsed_value = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise DiscoveryFailure("schema_response_malformed") from exc
        if not parsed_value.is_finite():
            raise DiscoveryFailure("schema_response_malformed")
        statuses.add(status)
        if "OBS_CONF" in observation.attrib:
            if not observation.attrib["OBS_CONF"]:
                raise DiscoveryFailure("schema_response_malformed")
            has_observation_confidence = True
    return tuple(sorted(statuses)), has_observation_confidence


def _parse_probe_structure_specific_xml(
    raw_bytes: bytes, request: DiscoveryRequest
) -> _SchemaFacts:
    root, namespaces = _parse_xml_with_namespaces(raw_bytes)
    if root.tag != _STRUCTURE_SPECIFIC_ROOT_QNAME:
        raise DiscoveryFailure("schema_representation_mismatch")
    if _STRUCTURE_SPECIFIC_NAMESPACE not in namespaces.values():
        raise DiscoveryFailure("response_series_mismatch")

    header_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}Header"
    structure_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}Structure"
    usage_qname = f"{{{_SDMX_21_COMMON_NAMESPACE}}}StructureUsage"
    headers = root.findall(header_qname)
    if len(headers) != 1:
        raise DiscoveryFailure("response_series_mismatch")
    structures = headers[0].findall(structure_qname)
    if len(structures) != 1:
        raise DiscoveryFailure("response_series_mismatch")
    structure = structures[0]
    if (
        structure.attrib.get("structureID") != _PROBE_STRUCTURE_ID
        or structure.attrib.get("namespace") != _STRUCTURE_SPECIFIC_NAMESPACE
        or structure.attrib.get("dimensionAtObservation") != "TIME_PERIOD"
    ):
        raise DiscoveryFailure("response_series_mismatch")
    usages = structure.findall(usage_qname)
    if len(usages) != 1:
        raise DiscoveryFailure("response_series_mismatch")
    references = [element for element in usages[0] if element.tag == "Ref"]
    if len(references) != 1 or (
        references[0].attrib.get("agencyID"),
        references[0].attrib.get("id"),
        references[0].attrib.get("version"),
    ) != ("BIS", "WS_CBPOL", "1.0"):
        raise DiscoveryFailure("response_series_mismatch")

    dataset_qname = f"{{{_SDMX_21_MESSAGE_NAMESPACE}}}DataSet"
    datasets = root.findall(dataset_qname)
    all_datasets = [item for item in root.iter() if _local_name(item.tag) == "DataSet"]
    if len(datasets) != 1 or all_datasets != datasets:
        raise DiscoveryFailure("schema_response_malformed")
    dataset = datasets[0]
    unit_measure = dataset.attrib.get("UNIT_MEASURE")
    unit_mult = dataset.attrib.get("UNIT_MULT")
    if not unit_measure or not unit_mult:
        raise DiscoveryFailure("schema_response_malformed")
    if (
        dataset.attrib.get("dataScope") != "DataStructure"
        or dataset.attrib.get("structureRef") != _PROBE_STRUCTURE_ID
        or _expanded_lexical_qname(
            dataset.attrib.get(f"{{{_XSI_NAMESPACE}}}type"), namespaces
        )
        != f"{{{_STRUCTURE_SPECIFIC_NAMESPACE}}}DataSetType"
    ):
        raise DiscoveryFailure("response_series_mismatch")

    series = [element for element in dataset if element.tag == "Series"]
    all_series = [item for item in root.iter() if _local_name(item.tag) == "Series"]
    if len(series) != 1 or all_series != series:
        raise DiscoveryFailure("schema_response_malformed")
    series_element = series[0]
    if (
        series_element.attrib.get("FREQ") != "D"
        or series_element.attrib.get("REF_AREA") != "US"
    ):
        raise DiscoveryFailure("response_series_mismatch")

    observations = tuple(element for element in series_element if element.tag == "Obs")
    all_observations = [
        item for item in root.iter() if _local_name(item.tag) == "Obs"
    ]
    if (
        len(observations) != len(_PROBE_DATE_SET)
        or all_observations != list(observations)
    ):
        raise DiscoveryFailure("probe_date_set_mismatch")

    observed_dates: list[date] = []
    for observation in observations:
        timestamp = observation.attrib.get("TIME_PERIOD")
        try:
            if timestamp is None or len(timestamp) != 10:
                raise ValueError
            observed = date.fromisoformat(timestamp)
        except (TypeError, ValueError) as exc:
            raise DiscoveryFailure("observation_timestamp_invalid") from exc
        if observed > MAX_OBSERVATION_DATE:
            raise DiscoveryFailure("sealed_window_violation")
        observed_dates.append(observed)

    if request.start is None or request.end is None:
        raise DiscoveryFailure("request_not_approved")
    if any(observed < request.start or observed > request.end for observed in observed_dates):
        raise DiscoveryFailure("observation_outside_request")
    if len(set(observed_dates)) != len(observed_dates):
        raise DiscoveryFailure("duplicate_observation")
    if set(observed_dates) != set(_PROBE_DATE_SET):
        raise DiscoveryFailure("probe_date_set_mismatch")

    statuses, has_observation_confidence = _validate_probe_observation_values(
        observations
    )
    returned_columns = (
        "FREQ",
        "REF_AREA",
        "UNIT_MEASURE",
        "UNIT_MULT",
        "TIME_PERIOD",
        "OBS_VALUE",
        "OBS_STATUS",
    ) + (("OBS_CONF",) if has_observation_confidence else ())
    attributes = ("OBS_STATUS", "UNIT_MEASURE", "UNIT_MULT") + (
        ("OBS_CONF",) if has_observation_confidence else ()
    )
    return _SchemaFacts(
        structure_identifiers=("BIS:WS_CBPOL(1.0)", _STRUCTURE_SPECIFIC_NAMESPACE),
        returned_columns=returned_columns,
        dimensions=("FREQ", "REF_AREA", "TIME_PERIOD"),
        attributes=attributes,
        status_vocabulary=statuses,
        units=(unit_measure,),
        scales=(unit_mult,),
        representation_identity=_PROBE_REPRESENTATION_IDENTITY,
        root_qname=root.tag,
        structure_specific_namespace=_STRUCTURE_SPECIFIC_NAMESPACE,
        series_count=1,
        observation_count=len(observations),
        parsed_min_observation_date=min(observed_dates),
        parsed_max_observation_date=max(observed_dates),
    )


def _normalized_media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _validated_probe_media_type(value: str) -> str:
    parts = tuple(part.strip().lower() for part in value.split(";"))
    base = parts[0]
    parameters: dict[str, str] = {}
    for part in parts[1:]:
        if not part or part.count("=") != 1:
            raise DiscoveryFailure("media_type_not_approved")
        key, parameter_value = (item.strip() for item in part.split("=", 1))
        parameter_value = parameter_value.strip('"')
        if not key or not parameter_value or key in parameters:
            raise DiscoveryFailure("media_type_not_approved")
        parameters[key] = parameter_value
    if base == "application/vnd.sdmx.structurespecificdata+xml":
        if parameters.get("version") != "2.1" or not set(parameters).issubset(
            {"version", "charset"}
        ):
            raise DiscoveryFailure("media_type_not_approved")
    elif base == "application/xml":
        if not set(parameters).issubset({"charset"}):
            raise DiscoveryFailure("media_type_not_approved")
    else:
        raise DiscoveryFailure("media_type_not_approved")
    if "charset" in parameters and parameters["charset"] not in {"utf-8", "utf8"}:
        raise DiscoveryFailure("media_type_not_approved")
    return base


def _validated_retrieval_timestamp(retrieved_at: datetime) -> datetime:
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None:
        raise DiscoveryFailure("retrieval_timestamp_invalid")
    return retrieved_at.astimezone(UTC)


def _fetch_discovery_response(
    request: DiscoveryRequest,
    transport: DiscoveryTransport,
) -> DiscoveryHttpResponse:
    validate_discovery_request(request)
    try:
        response = transport.fetch(
            request,
            timeout_seconds=TIMEOUT_SECONDS,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
    except TimeoutError as exc:
        raise DiscoveryFailure("discovery_timeout") from exc
    except DiscoveryFailure:
        raise
    except Exception as exc:
        raise DiscoveryFailure("transport_failure") from exc
    if not isinstance(response, DiscoveryHttpResponse):
        raise DiscoveryFailure("transport_response_invalid")
    return response


def _build_discovery_result(
    request: DiscoveryRequest,
    response: DiscoveryHttpResponse,
    retrieved: datetime,
) -> BisDiscoveryResult:
    if 300 <= response.status_code <= 399:
        raise DiscoveryFailure("redirect_rejected")
    if response.status_code != 200:
        raise DiscoveryFailure("http_status_not_success")
    if response.final_url != request.url:
        raise DiscoveryFailure("redirect_rejected")
    if len(response.raw_bytes) > MAX_RESPONSE_BYTES:
        raise DiscoveryFailure("response_too_large")
    if not response.raw_bytes:
        raise DiscoveryFailure("empty_response")

    media_type = (
        _validated_probe_media_type(response.media_type)
        if request.target == DiscoveryTarget.D_US_SCHEMA_PROBE
        else _normalized_media_type(response.media_type)
    )
    allowed = (
        _STRUCTURE_MEDIA_TYPES
        if request.target == DiscoveryTarget.STRUCTURE
        else _PROBE_MEDIA_TYPES
    )
    if media_type not in allowed:
        raise DiscoveryFailure("media_type_not_approved")
    facts = (
        _parse_structure_xml(response.raw_bytes)
        if request.target == DiscoveryTarget.STRUCTURE
        else _parse_probe_structure_specific_xml(response.raw_bytes, request)
    )
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in _RETAINED_HEADERS
    }
    schema_fingerprint = canonical_sha256(
        {
            "format": 1,
            "target": request.target,
            "content_type": media_type,
            "facts": facts,
        }
    )
    raw_sha256 = hashlib.sha256(response.raw_bytes).hexdigest()
    discovery_id = canonical_sha256(
        {
            "format": 1,
            "classification": DISCOVERY_CLASSIFICATION,
            "request_identity": request.request_identity,
            "retrieved_at": retrieved,
            "raw_sha256": raw_sha256,
            "byte_count": len(response.raw_bytes),
            "content_type": media_type,
            "response_headers": headers,
            "schema_fingerprint": schema_fingerprint,
        }
    )
    artifact = BisDiscoveryArtifact(
        classification=DISCOVERY_CLASSIFICATION,
        target=request.target,
        retrieved_at=retrieved,
        request_identity=request.request_identity,
        exact_url=request.url,
        http_status=response.status_code,
        content_type=media_type,
        raw_sha256=raw_sha256,
        byte_count=len(response.raw_bytes),
        response_headers=headers,
        structure_identifiers=facts.structure_identifiers,
        dsd_identity=facts.dsd_identity,
        codelist_identities=facts.codelist_identities,
        returned_columns=facts.returned_columns,
        dimensions=facts.dimensions,
        attributes=facts.attributes,
        status_vocabulary=facts.status_vocabulary,
        units=facts.units,
        scales=facts.scales,
        dsd_content_fingerprint=facts.dsd_content_fingerprint,
        codelist_content_fingerprints=facts.codelist_content_fingerprints,
        representation_identity=facts.representation_identity,
        root_qname=facts.root_qname,
        structure_specific_namespace=facts.structure_specific_namespace,
        series_count=facts.series_count,
        observation_count=facts.observation_count,
        parsed_min_observation_date=facts.parsed_min_observation_date,
        parsed_max_observation_date=facts.parsed_max_observation_date,
        schema_fingerprint=schema_fingerprint,
        discovery_id=discovery_id,
    )
    return BisDiscoveryResult(response.raw_bytes, artifact)


def execute_discovery(
    request: DiscoveryRequest,
    transport: DiscoveryTransport,
    retrieved_at: datetime,
) -> BisDiscoveryResult:
    """Execute exactly one already-approved discovery request."""

    retrieved = _validated_retrieval_timestamp(retrieved_at)
    response = _fetch_discovery_response(request, transport)
    return _build_discovery_result(request, response, retrieved)


def discovery_artifact_json(artifact: BisDiscoveryArtifact) -> bytes:
    if not isinstance(artifact, BisDiscoveryArtifact):
        raise DiscoveryFailure("discovery_artifact_invalid")
    return (canonical_json(artifact) + "\n").encode("utf-8")


def failed_discovery_artifact_json(artifact: FailedBisDiscoveryArtifact) -> bytes:
    if not isinstance(artifact, FailedBisDiscoveryArtifact):
        raise DiscoveryFailure("discovery_artifact_invalid")
    return (canonical_json(artifact) + "\n").encode("utf-8")


def raw_artifact_paths(request: DiscoveryRequest) -> tuple[Path, Path]:
    validate_discovery_request(request)
    directory = RAW_DISCOVERY_ROOT / f"{request.target.value}-{request.request_identity}"
    return directory / "response.bin", directory / "discovery.json"


def persist_discovery_result(
    request: DiscoveryRequest, result: BisDiscoveryResult
) -> tuple[Path, Path]:
    """Persist only below the fixed, Git-ignored discovery root."""

    validate_discovery_request(request)
    if not isinstance(result, BisDiscoveryResult) or result.artifact.request_identity != (
        request.request_identity
    ):
        raise DiscoveryFailure("discovery_artifact_invalid")
    raw_path, manifest_path = raw_artifact_paths(request)
    raw_path.parent.mkdir(parents=True, exist_ok=False)
    raw_path.write_bytes(result.raw_bytes)
    manifest_path.write_bytes(discovery_artifact_json(result.artifact))
    return raw_path, manifest_path


def _failed_artifact_directory(
    request: DiscoveryRequest, retrieved: datetime, raw_sha256: str
) -> Path:
    timestamp = retrieved.strftime("%Y%m%dT%H%M%S%fZ")
    return (
        RAW_DISCOVERY_ROOT
        / f"{request.target.value}-{request.request_identity[:16]}"
        / f"failed-{timestamp}-{raw_sha256[:12]}"
    )


def persist_failed_discovery_response(
    request: DiscoveryRequest,
    response: DiscoveryHttpResponse,
    retrieved_at: datetime,
    failure_reason: str,
) -> tuple[Path, Path]:
    """Atomically retain a returned but rejected response as ineligible audit evidence."""

    validate_discovery_request(request)
    retrieved = _validated_retrieval_timestamp(retrieved_at)
    if (
        not isinstance(response, DiscoveryHttpResponse)
        or response.final_url != request.url
        or response.status_code != 200
        or not response.raw_bytes
        or len(response.raw_bytes) > MAX_RESPONSE_BYTES
    ):
        raise DiscoveryFailure("failed_response_not_persistable")
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in _RETAINED_HEADERS
    }
    raw_sha256 = hashlib.sha256(response.raw_bytes).hexdigest()
    artifact = FailedBisDiscoveryArtifact(
        classification=FAILED_DISCOVERY_CLASSIFICATION,
        target=request.target,
        retrieved_at=retrieved,
        request_identity=request.request_identity,
        exact_url=request.url,
        http_status=response.status_code,
        content_type=_normalized_media_type(response.media_type),
        raw_sha256=raw_sha256,
        byte_count=len(response.raw_bytes),
        response_headers=headers,
        failure_reason=failure_reason,
    )
    final_directory = _failed_artifact_directory(request, retrieved, raw_sha256)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    staging_directory = final_directory.parent / f".tmp-{uuid.uuid4().hex[:12]}"
    try:
        staging_directory.mkdir(exist_ok=False)
        raw_path = staging_directory / "response.bin"
        manifest_path = staging_directory / "failure.json"
        raw_path.write_bytes(response.raw_bytes)
        manifest_path.write_bytes(failed_discovery_artifact_json(artifact))
        staging_directory.replace(final_directory)
    except Exception:
        for path in (
            staging_directory / "response.bin",
            staging_directory / "failure.json",
        ):
            if path.exists():
                path.unlink()
        if staging_directory.exists():
            staging_directory.rmdir()
        raise
    return final_directory / "response.bin", final_directory / "failure.json"


def execute_and_persist_discovery(
    request: DiscoveryRequest,
    transport: DiscoveryTransport,
    retrieved_at: datetime,
) -> tuple[BisDiscoveryResult, tuple[Path, Path]]:
    """Execute one request, atomically preserving any returned response rejected by parsing."""

    retrieved = _validated_retrieval_timestamp(retrieved_at)
    response = _fetch_discovery_response(request, transport)
    try:
        result = _build_discovery_result(request, response, retrieved)
    except DiscoveryFailure as exc:
        if (
            response.status_code == 200
            and response.final_url == request.url
            and response.raw_bytes
            and len(response.raw_bytes) <= MAX_RESPONSE_BYTES
        ):
            persist_failed_discovery_response(request, response, retrieved, exc.reason)
        raise
    return result, persist_discovery_result(request, result)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        raise DiscoveryFailure("redirect_rejected")


def _read_bounded_response(wire_response: object, max_response_bytes: int) -> bytes:
    retained = bytearray()
    while True:
        chunk = wire_response.read(_READ_CHUNK_BYTES)
        if not isinstance(chunk, bytes):
            raise DiscoveryFailure("transport_response_invalid")
        if not chunk:
            return bytes(retained)
        if len(retained) + len(chunk) > max_response_bytes:
            raise DiscoveryFailure("response_too_large")
        retained.extend(chunk)


class _OneAttemptUrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def fetch(
        self,
        request: DiscoveryRequest,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> DiscoveryHttpResponse:
        validate_discovery_request(request)
        wire_request = urllib.request.Request(
            request.url,
            headers={"Accept": request.accept, "User-Agent": "fxlab-bis-contract-discovery/1"},
            method="GET",
        )
        try:
            with self._opener.open(wire_request, timeout=timeout_seconds) as wire_response:
                content_length = wire_response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > max_response_bytes:
                    raise DiscoveryFailure("response_too_large")
                raw_bytes = _read_bounded_response(wire_response, max_response_bytes)
                return DiscoveryHttpResponse(
                    status_code=wire_response.status,
                    final_url=wire_response.geturl(),
                    media_type=wire_response.headers.get("Content-Type", ""),
                    headers=dict(wire_response.headers.items()),
                    raw_bytes=raw_bytes,
                )
        except DiscoveryFailure:
            raise
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code <= 399:
                raise DiscoveryFailure("redirect_rejected") from exc
            raise DiscoveryFailure("http_status_not_success") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError from exc
            raise DiscoveryFailure("transport_failure") from exc


def _build_network_transport() -> DiscoveryTransport:
    return _OneAttemptUrllibTransport()


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize-network-discovery", action="store_true")
    parser.add_argument(
        "--target",
        choices=tuple(item.value for item in DiscoveryTarget),
        default=DiscoveryTarget.STRUCTURE.value,
    )
    parser.add_argument("--metadata-insufficient", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    if not args.authorize_network_discovery:
        raise SystemExit(
            "network_acquisition_not_authorized: pass the explicit discovery authorization flag"
        )
    if args.target == DiscoveryTarget.STRUCTURE.value:
        request = build_structure_request()
    else:
        request = build_d_us_schema_probe_request(
            metadata_insufficient=args.metadata_insufficient
        )
    _, (raw_path, manifest_path) = execute_and_persist_discovery(
        request, _build_network_transport(), datetime.now(UTC)
    )
    print(f"{DISCOVERY_CLASSIFICATION}: {raw_path} {manifest_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised only after separate authorization
    sys.exit(main())
