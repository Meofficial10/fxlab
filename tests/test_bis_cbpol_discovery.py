from __future__ import annotations

import hashlib
import json
import urllib.error
from dataclasses import fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

import pytest
import scripts.discover_bis_cbpol_contract as discovery
from scripts.discover_bis_cbpol_contract import (
    DISCOVERY_CLASSIFICATION,
    MAX_RESPONSE_BYTES,
    DiscoveryFailure,
    DiscoveryHttpResponse,
    DiscoveryTarget,
    build_d_us_schema_probe_request,
    build_structure_request,
    execute_discovery,
    raw_artifact_paths,
)

from fxlab.data.policy_rates import CandidateBQualificationResult

RETRIEVED = datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC)


def structure_xml(
    *,
    flow_dsd_id: str = "DSD_CBPOL",
    actual_dsd_id: str | None = "DSD_CBPOL",
    status_ref: str | None = "CL_OBS_STATUS",
    omitted_codelists: frozenset[str] = frozenset(),
    extra_codelist: bool = False,
    orphan_components: bool = False,
) -> bytes:
    def component(kind: str, component_id: str, codelist_id: str | None) -> str:
        reference = (
            ""
            if codelist_id is None
            else (
                "<LocalRepresentation><Enumeration>"
                f'<Ref agencyID="BIS" id="{codelist_id}" version="1.0" '
                'class="Codelist" />'
                "</Enumeration></LocalRepresentation>"
            )
        )
        return f'<{kind} id="{component_id}">{reference}</{kind}>'

    components = "".join(
        (
            component("Dimension", "FREQ", "CL_FREQ"),
            component("Dimension", "REF_AREA", "CL_REF_AREA"),
            component("Attribute", "OBS_STATUS", status_ref),
            component("Attribute", "UNIT_MEASURE", "CL_UNIT_MEASURE"),
            component("Attribute", "UNIT_MULT", "CL_UNIT_MULT"),
        )
    )
    dsd = (
        ""
        if actual_dsd_id is None
        else (
            f'<DataStructure agencyID="BIS" id="{actual_dsd_id}" version="1.0">'
            f"{components}</DataStructure>"
        )
    )
    orphan = components if orphan_components else ""
    vocabularies = {
        "CL_FREQ": ("D",),
        "CL_REF_AREA": ("US",),
        "CL_OBS_STATUS": ("A",),
        "CL_UNIT_MEASURE": ("PCT",),
        "CL_UNIT_MULT": ("0",),
    }
    if extra_codelist:
        vocabularies["CL_UNRELATED"] = ("X",)
    codelists = "".join(
        (
            f'<Codelist agencyID="BIS" id="{identity}" version="1.0">'
            + "".join(f'<Code id="{code}" />' for code in codes)
            + "</Codelist>"
        )
        for identity, codes in vocabularies.items()
        if identity not in omitted_codelists
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Structure xmlns="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">'
        '<Dataflow agencyID="BIS" id="WS_CBPOL" version="1.0" '
        f'structure="BIS:{flow_dsd_id}(1.0)" />'
        f"{dsd}{orphan}{codelists}</Structure>"
    ).encode()


STRUCTURE_XML = structure_xml()
PROBE_CSV = (
    b"FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE,OBS_STATUS,UNIT_MEASURE,UNIT_MULT\n"
    b"D,US,2023-01-03,4.50,A,PCT,0\n"
)

SDMX_21_MESSAGE = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
SDMX_21_COMMON = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
STRUCTURE_SPECIFIC_NAMESPACE = (
    "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow="
    "BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD"
)
STRUCTURE_SPECIFIC_MEDIA_TYPE = (
    "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
)


def probe_xml(
    *,
    agency: str = "BIS",
    flow: str = "WS_CBPOL",
    version: str = "1.0",
    observation_dimension: str = "TIME_PERIOD",
    message_namespace: str = SDMX_21_MESSAGE,
    root_name: str = "StructureSpecificData",
    dataset_count: int = 1,
    series_count: int = 1,
    frequency: str = "D",
    reference_area: str = "US",
    unit_measure: str | None = "368",
    unit_mult: str | None = "0",
    dates: tuple[str, ...] | None = None,
    missing_observation_field: str | None = None,
    structure_ref: str = "BIS_WS_CBPOL_1_0",
    dataset_structure_ref: str = "BIS_WS_CBPOL_1_0",
    dataset_type_namespace: str | None = None,
) -> bytes:
    if dates is None:
        dates = tuple(f"2023-01-{day:02d}" for day in range(1, 32))
    semantic_namespace = dataset_type_namespace or (
        "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow="
        f"{agency}:{flow}({version}):ObsLevelDim:{observation_dimension}"
    )

    def optional_attribute(name: str, value: str | None) -> str:
        return "" if value is None else f' {name}="{value}"'

    observations = "".join(
        "<Obs"
        + ("" if missing_observation_field == "TIME_PERIOD" else f' TIME_PERIOD="{day}"')
        + (
            ""
            if missing_observation_field == "OBS_VALUE"
            else f' OBS_VALUE="{index / 100:.2f}"'
        )
        + ("" if missing_observation_field == "OBS_STATUS" else ' OBS_STATUS="A"')
        + ' OBS_CONF="F" />'
        for index, day in enumerate(dates, start=1)
    )
    series = "".join(
        f'<Series FREQ="{frequency}" REF_AREA="{reference_area}">{observations}</Series>'
        for _ in range(series_count)
    )
    datasets = "".join(
        "<message:DataSet"
        + optional_attribute("UNIT_MEASURE", unit_measure)
        + optional_attribute("UNIT_MULT", unit_mult)
        + ' dataScope="DataStructure"'
        + ' xsi:type="ss:DataSetType"'
        + f' structureRef="{dataset_structure_ref}">{series}</message:DataSet>'
        for _ in range(dataset_count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<message:{root_name} xmlns:message="{message_namespace}" '
        f'xmlns:common="{SDMX_21_COMMON}" '
        f'xmlns:ss="{semantic_namespace}" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<message:Header>"
        f'<message:Structure structureID="{structure_ref}" namespace="{semantic_namespace}" '
        f'dimensionAtObservation="{observation_dimension}">'
        "<common:StructureUsage>"
        f'<Ref agencyID="{agency}" id="{flow}" version="{version}" />'
        "</common:StructureUsage></message:Structure>"
        "</message:Header>"
        f"{datasets}"
        f"</message:{root_name}>"
    ).encode()


PROBE_XML = probe_xml()


def realistic_sdmx21_xml(*, include_flow: bool = True, include_dsd: bool = True) -> bytes:
    flow = (
        ""
        if not include_flow
        else (
            '<str:Dataflows><str:Dataflow agencyID="BIS" id="WS_CBPOL" version="1.0">'
            "<str:Structure>"
            '<com:Ref agencyID="BIS" id="DSD_CBPOL" version="1.0" '
            'class="DataStructure" package="datastructure" />'
            "</str:Structure></str:Dataflow></str:Dataflows>"
        )
    )
    components = "".join(
        (
            '<str:Dimension id="FREQ"><str:LocalRepresentation><str:Enumeration>'
            '<com:Ref agencyID="BIS" id="CL_FREQ" version="1.0" class="Codelist" />'
            "</str:Enumeration></str:LocalRepresentation></str:Dimension>",
            '<str:Dimension id="REF_AREA"><str:LocalRepresentation><str:Enumeration>'
            '<com:Ref agencyID="BIS" id="CL_REF_AREA" version="1.0" '
            'class="Codelist" />'
            "</str:Enumeration></str:LocalRepresentation></str:Dimension>",
            '<str:Attribute id="OBS_STATUS"><str:LocalRepresentation><str:Enumeration>'
            '<com:Ref agencyID="BIS" id="CL_OBS_STATUS" version="1.0" '
            'class="Codelist" />'
            "</str:Enumeration></str:LocalRepresentation></str:Attribute>",
            '<str:Attribute id="UNIT_MEASURE"><str:LocalRepresentation><str:Enumeration>'
            '<com:Ref agencyID="BIS" id="CL_UNIT_MEASURE" version="1.0" '
            'class="Codelist" />'
            "</str:Enumeration></str:LocalRepresentation></str:Attribute>",
            '<str:Attribute id="UNIT_MULT"><str:LocalRepresentation><str:Enumeration>'
            '<com:Ref agencyID="BIS" id="CL_UNIT_MULT" version="1.0" '
            'class="Codelist" />'
            "</str:Enumeration></str:LocalRepresentation></str:Attribute>",
        )
    )
    dsd = (
        ""
        if not include_dsd
        else (
            '<str:DataStructures><str:DataStructure agencyID="BIS" id="DSD_CBPOL" '
            'version="1.0"><str:DataStructureComponents>'
            f"{components}"
            "</str:DataStructureComponents></str:DataStructure></str:DataStructures>"
        )
    )
    vocabularies = {
        "CL_FREQ": ("D",),
        "CL_REF_AREA": ("US",),
        "CL_OBS_STATUS": ("A",),
        "CL_UNIT_MEASURE": ("PCT",),
        "CL_UNIT_MULT": ("0",),
    }
    codelists = "".join(
        '<str:Codelist agencyID="BIS" id="{}" version="1.0">{}</str:Codelist>'.format(
            identity,
            "".join(f'<str:Code id="{code}" />' for code in codes),
        )
        for identity, codes in vocabularies.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<mes:Structure xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message" '
        'xmlns:str="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure" '
        'xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common">'
        f"<mes:Structures>{flow}{dsd}<str:Codelists>{codelists}</str:Codelists>"
        "</mes:Structures></mes:Structure>"
    ).encode()


class FakeTransport:
    def __init__(self, response: DiscoveryHttpResponse | BaseException):
        self.response = response
        self.calls: list[tuple[object, int, int]] = []

    def fetch(self, request: object, *, timeout_seconds: int, max_response_bytes: int):
        self.calls.append((request, timeout_seconds, max_response_bytes))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class FakeWireResponse:
    def __init__(self, request_url: str, payload: bytes):
        self.status = 200
        self._request_url = request_url
        self._payload = payload
        self._offset = 0
        self.read_requests: list[int] = []
        self.headers = {
            "Content-Type": "application/vnd.sdmx.structure+xml;version=2.1"
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def geturl(self) -> str:
        return self._request_url

    def close(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        self.read_requests.append(size)
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, outcome: FakeWireResponse | BaseException):
        self.outcome = outcome
        self.calls = 0

    def open(self, request, *, timeout: int):
        del request, timeout
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def response(
    request_url: str,
    raw: bytes = STRUCTURE_XML,
    media_type: str = "application/vnd.sdmx.structure+xml;version=2.1",
    *,
    status: int = 200,
    final_url: str | None = None,
    headers: dict[str, str] | None = None,
) -> DiscoveryHttpResponse:
    return DiscoveryHttpResponse(
        status_code=status,
        final_url=final_url or request_url,
        media_type=media_type,
        headers=headers or {"ETag": '"abc"', "Set-Cookie": "not-retained"},
        raw_bytes=raw,
    )


def test_discovery_plan_is_metadata_first_and_probe_second() -> None:
    plan = discovery.discovery_plan()
    assert tuple(item.target for item in plan) == (
        DiscoveryTarget.STRUCTURE,
        DiscoveryTarget.D_US_SCHEMA_PROBE,
    )


def test_default_main_is_network_disabled_before_transport_construction(monkeypatch) -> None:
    monkeypatch.setattr(
        discovery,
        "_build_network_transport",
        lambda: pytest.fail("network transport must not be constructed"),
    )
    with pytest.raises(SystemExit, match="network_acquisition_not_authorized"):
        discovery.main([])


def test_only_exact_bis_host_path_and_query_are_accepted() -> None:
    approved = build_structure_request()
    assert approved.url == (
        "https://stats.bis.org/api/v2/structure/dataflow/BIS/WS_CBPOL/1.0"
        "?detail=referencepartial&references=descendants"
    )
    assert approved.accept == "application/vnd.sdmx.structure+xml;version=2.1"
    rejected = (
        approved.url.replace("https://", "http://"),
        approved.url.replace("stats.bis.org", "stats.bis.org.evil.test"),
        approved.url.replace("/api/v2/", "/api/v1/"),
        approved.url + "&token=secret",
        approved.url.replace("references=descendants", "references=descendants%26latest=true"),
        approved.url.replace("stats.bis.org", "user:pass@stats.bis.org"),
        approved.url + "#fragment",
    )
    for url in rejected:
        with pytest.raises(DiscoveryFailure, match="request_not_approved"):
            replace(approved, url=url)


def test_probe_is_fixed_to_d_us_and_predeclared_pre_2024_dates() -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    assert request.series_key == "D.US"
    assert request.start == date(2023, 1, 1)
    assert request.end == date(2023, 1, 31)
    assert "startPeriod=2023-01-01" in request.url
    assert "endPeriod=2023-01-31" in request.url
    assert "latest" not in request.url.lower()
    assert request.accept == STRUCTURE_SPECIFIC_MEDIA_TYPE
    with pytest.raises(DiscoveryFailure, match="metadata_discovery_required_first"):
        build_d_us_schema_probe_request(metadata_insufficient=False)
    with pytest.raises(DiscoveryFailure, match="request_not_approved"):
        replace(request, series_key="D.*")
    with pytest.raises(DiscoveryFailure, match="sealed_window_violation"):
        replace(request, end=date(2024, 1, 1))


def test_structure_discovery_captures_bounded_metadata_and_allowlisted_headers() -> None:
    request = build_structure_request()
    transport = FakeTransport(response(request.url))
    result = execute_discovery(request, transport, RETRIEVED)
    artifact = result.artifact
    assert len(transport.calls) == 1
    assert artifact.classification == DISCOVERY_CLASSIFICATION
    assert artifact.authoritative_qualification_eligible is False
    assert artifact.final_run_identity_eligible is False
    assert artifact.r4_evidence_eligible is False
    assert artifact.http_status == 200
    assert artifact.raw_sha256 == hashlib.sha256(STRUCTURE_XML).hexdigest()
    assert artifact.byte_count == len(STRUCTURE_XML)
    assert artifact.response_headers == MappingProxyType({"etag": '"abc"'})
    assert "BIS:WS_CBPOL(1.0)" in artifact.structure_identifiers
    assert artifact.dsd_identity == "BIS:DSD_CBPOL(1.0)"
    assert "BIS:CL_OBS_STATUS(1.0)" in artifact.codelist_identities
    assert artifact.dimensions == ("FREQ", "REF_AREA")
    assert artifact.attributes == ("OBS_STATUS", "UNIT_MEASURE", "UNIT_MULT")
    assert artifact.status_vocabulary == ("A",)
    assert artifact.units == ("PCT",)
    assert artifact.scales == ("0",)
    assert artifact.dsd_content_fingerprint
    assert len(artifact.codelist_content_fingerprints) == 5
    assert artifact.schema_fingerprint


def test_realistic_sdmx21_descendants_response_cross_binds_flow_dsd_and_codelists() -> None:
    request = build_structure_request()
    result = execute_discovery(
        request,
        FakeTransport(response(request.url, realistic_sdmx21_xml())),
        RETRIEVED,
    )
    assert result.artifact.structure_identifiers == (
        "BIS:WS_CBPOL(1.0)",
        "BIS:DSD_CBPOL(1.0)",
    )
    assert result.artifact.status_vocabulary == ("A",)


@pytest.mark.parametrize(
    "payload",
    (
        realistic_sdmx21_xml(include_dsd=False),
        realistic_sdmx21_xml(include_flow=False),
    ),
)
def test_incomplete_dataflow_or_dsd_representation_is_diagnosed(payload: bytes) -> None:
    request = build_structure_request()
    with pytest.raises(DiscoveryFailure, match="metadata_structure_incomplete"):
        execute_discovery(request, FakeTransport(response(request.url, payload)), RETRIEVED)


def test_sdmx20_representation_is_rejected_as_request_parser_mismatch() -> None:
    request = build_structure_request()
    payload = realistic_sdmx21_xml().replace(b"v2_1", b"v2_0")
    with pytest.raises(DiscoveryFailure, match="metadata_representation_mismatch"):
        execute_discovery(request, FakeTransport(response(request.url, payload)), RETRIEVED)


def test_structure_rejects_dsd_reference_without_actual_dsd_content() -> None:
    request = build_structure_request()
    bare_reference = structure_xml(actual_dsd_id=None, orphan_components=True)
    with pytest.raises(DiscoveryFailure, match="metadata_structure_incomplete"):
        execute_discovery(request, FakeTransport(response(request.url, bare_reference)), RETRIEVED)


def test_structure_rejects_unrelated_actual_dsd() -> None:
    request = build_structure_request()
    unrelated = structure_xml(actual_dsd_id="DSD_OTHER")
    with pytest.raises(DiscoveryFailure, match="metadata_response_malformed"):
        execute_discovery(request, FakeTransport(response(request.url, unrelated)), RETRIEVED)


def test_structure_rejects_unrelated_or_missing_codelists() -> None:
    request = build_structure_request()
    unrelated = structure_xml(extra_codelist=True)
    with pytest.raises(DiscoveryFailure, match="metadata_response_malformed"):
        execute_discovery(request, FakeTransport(response(request.url, unrelated)), RETRIEVED)
    missing = structure_xml(omitted_codelists=frozenset({"CL_OBS_STATUS"}))
    with pytest.raises(DiscoveryFailure, match="metadata_response_malformed"):
        execute_discovery(request, FakeTransport(response(request.url, missing)), RETRIEVED)


def test_status_vocabulary_requires_dsd_component_codelist_provenance() -> None:
    request = build_structure_request()
    unbound_status = structure_xml(status_ref=None)
    with pytest.raises(DiscoveryFailure, match="metadata_response_malformed"):
        execute_discovery(request, FakeTransport(response(request.url, unbound_status)), RETRIEVED)


@pytest.mark.parametrize(
    "media_type",
    (STRUCTURE_SPECIFIC_MEDIA_TYPE, "application/xml"),
)
def test_probe_discovers_strict_structure_specific_schema_without_exposing_values(
    media_type: str,
) -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    transport = FakeTransport(response(request.url, PROBE_XML, media_type))
    artifact = execute_discovery(request, transport, RETRIEVED).artifact
    assert artifact.returned_columns == (
        "FREQ",
        "REF_AREA",
        "UNIT_MEASURE",
        "UNIT_MULT",
        "TIME_PERIOD",
        "OBS_VALUE",
        "OBS_STATUS",
        "OBS_CONF",
    )
    assert artifact.status_vocabulary == ("A",)
    assert artifact.units == ("368",)
    assert artifact.scales == ("0",)
    assert artifact.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert artifact.root_qname == f"{{{SDMX_21_MESSAGE}}}StructureSpecificData"
    assert artifact.structure_specific_namespace == STRUCTURE_SPECIFIC_NAMESPACE
    assert artifact.series_count == 1
    assert artifact.observation_count == 31
    assert artifact.parsed_min_observation_date == date(2023, 1, 1)
    assert artifact.parsed_max_observation_date == date(2023, 1, 31)
    assert not hasattr(artifact, "observations")
    assert b"OBS_VALUE" in discovery.discovery_artifact_json(artifact)
    assert b'"0.01"' not in discovery.discovery_artifact_json(artifact)


@pytest.mark.parametrize(
    ("media_type", "payload", "reason"),
    (
        ("application/xml", b"<unrelated />", "schema_representation_mismatch"),
        (
            "application/xml",
            probe_xml(root_name="GenericData"),
            "schema_representation_mismatch",
        ),
        ("text/csv", PROBE_XML, "media_type_not_approved"),
        ("application/xml", PROBE_CSV, "schema_response_malformed"),
        (
            "application/vnd.sdmx.structurespecificdata+xml;version=2.0",
            PROBE_XML,
            "media_type_not_approved",
        ),
        ("application/json", PROBE_XML, "media_type_not_approved"),
    ),
)
def test_probe_media_and_body_dispatch_fail_closed(
    media_type: str, payload: bytes, reason: str
) -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    with pytest.raises(DiscoveryFailure, match=reason):
        execute_discovery(
            request,
            FakeTransport(response(request.url, payload, media_type)),
            RETRIEVED,
        )


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        (b"<message:StructureSpecificData", "schema_response_malformed"),
        (
            b'<!DOCTYPE x [<!ELEMENT x ANY>]><x />',
            "schema_response_malformed",
        ),
        (
            b'<!DOCTYPE x [<!ENTITY secret "x">]><x>&secret;</x>',
            "schema_response_malformed",
        ),
        (
            b"\xff\xfe"
            + '<!DOCTYPE x [<!ENTITY secret "x">]><x>&secret;</x>'.encode(
                "utf-16-le"
            ),
            "schema_response_malformed",
        ),
        (b"<html><body>error</body></html>", "schema_representation_mismatch"),
        (
            probe_xml(message_namespace=SDMX_21_MESSAGE.replace("v2_1", "v2_0")),
            "schema_representation_mismatch",
        ),
    ),
)
def test_probe_rejects_unsafe_or_incompatible_xml(payload: bytes, reason: str) -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    with pytest.raises(DiscoveryFailure, match=reason):
        execute_discovery(
            request,
            FakeTransport(response(request.url, payload, "application/xml")),
            RETRIEVED,
        )


@pytest.mark.parametrize(
    "payload",
    (
        probe_xml(agency="ECB"),
        probe_xml(flow="OTHER_FLOW"),
        probe_xml(version="2.0"),
        probe_xml(observation_dimension="OTHER_TIME"),
        probe_xml(dataset_structure_ref="OTHER_STRUCTURE"),
        probe_xml(dataset_type_namespace="urn:example:unrelated"),
    ),
)
def test_probe_rejects_wrong_structure_specific_binding(payload: bytes) -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    with pytest.raises(DiscoveryFailure, match="response_series_mismatch"):
        execute_discovery(
            request,
            FakeTransport(response(request.url, payload, "application/xml")),
            RETRIEVED,
        )


@pytest.mark.parametrize(
    "payload",
    (
        probe_xml(dataset_count=0),
        probe_xml(dataset_count=2),
        probe_xml(series_count=0),
        probe_xml(series_count=2),
        probe_xml(frequency="M"),
        probe_xml(reference_area="CA"),
        probe_xml(unit_measure=None),
        probe_xml(unit_mult=None),
        probe_xml(missing_observation_field="TIME_PERIOD"),
        probe_xml(missing_observation_field="OBS_VALUE"),
        probe_xml(missing_observation_field="OBS_STATUS"),
    ),
)
def test_probe_rejects_missing_or_inconsistent_required_structure(payload: bytes) -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    with pytest.raises(DiscoveryFailure):
        execute_discovery(
            request,
            FakeTransport(response(request.url, payload, "application/xml")),
            RETRIEVED,
        )


@pytest.mark.parametrize(
    "dates",
    (
        tuple(f"2023-01-{day:02d}" for day in range(1, 31)),
        tuple(f"2023-01-{day:02d}" for day in range(1, 31)) + ("2023-01-30",),
        ("not-a-date",) + tuple(f"2023-01-{day:02d}" for day in range(2, 32)),
        ("2022-12-31",) + tuple(f"2023-01-{day:02d}" for day in range(2, 32)),
        tuple(f"2023-01-{day:02d}" for day in range(1, 31)) + ("2023-02-01",),
    ),
)
def test_probe_requires_exact_complete_january_2023_date_set(dates: tuple[str, ...]) -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    with pytest.raises(DiscoveryFailure):
        execute_discovery(
            request,
            FakeTransport(response(request.url, probe_xml(dates=dates), "application/xml")),
            RETRIEVED,
        )


def test_probe_rejects_post_2023_before_observation_value_validation(monkeypatch) -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    contaminated_dates = tuple(f"2023-01-{day:02d}" for day in range(1, 31)) + (
        "2024-01-01",
    )
    payload = probe_xml(dates=contaminated_dates).replace(
        b'OBS_VALUE="0.31"', b'OBS_VALUE="SECRET_SENTINEL"'
    )

    def fail_if_values_are_touched(_observations) -> None:
        pytest.fail("observation values accessed before sealed dates passed")

    monkeypatch.setattr(
        discovery,
        "_validate_probe_observation_values",
        fail_if_values_are_touched,
        raising=False,
    )
    with pytest.raises(DiscoveryFailure, match="sealed_window_violation"):
        execute_discovery(
            request,
            FakeTransport(response(request.url, payload, "application/xml")),
            RETRIEVED,
        )


@pytest.mark.parametrize(
    ("status", "final_url", "reason"),
    (
        (302, None, "redirect_rejected"),
        (200, "https://stats.bis.org/api/v2/other", "redirect_rejected"),
        (503, None, "http_status_not_success"),
    ),
)
def test_redirect_and_non_success_responses_fail_closed(
    status: int, final_url: str | None, reason: str
) -> None:
    request = build_structure_request()
    transport = FakeTransport(response(request.url, status=status, final_url=final_url))
    with pytest.raises(DiscoveryFailure, match=reason):
        execute_discovery(request, transport, RETRIEVED)
    assert len(transport.calls) == 1


def test_timeout_is_one_attempt_with_no_retry_or_fallback() -> None:
    request = build_structure_request()
    transport = FakeTransport(TimeoutError("synthetic timeout"))
    with pytest.raises(DiscoveryFailure, match="discovery_timeout"):
        execute_discovery(request, transport, RETRIEVED)
    assert len(transport.calls) == 1


def test_oversized_response_fails_without_retry_or_artifact() -> None:
    request = build_structure_request()
    raw = b"x" * (MAX_RESPONSE_BYTES + 1)
    transport = FakeTransport(response(request.url, raw))
    with pytest.raises(DiscoveryFailure, match="response_too_large"):
        execute_discovery(request, transport, RETRIEVED)
    assert len(transport.calls) == 1


def test_real_urllib_transport_accepts_exactly_four_mib_without_oversized_read() -> None:
    request = build_structure_request()
    payload = STRUCTURE_XML + b" " * (MAX_RESPONSE_BYTES - len(STRUCTURE_XML))
    wire_response = FakeWireResponse(request.url, payload)
    opener = FakeOpener(wire_response)
    transport = discovery._OneAttemptUrllibTransport()
    transport._opener = opener
    result = execute_discovery(request, transport, RETRIEVED)
    assert len(result.raw_bytes) == MAX_RESPONSE_BYTES
    assert opener.calls == 1
    assert max(wire_response.read_requests) <= discovery._READ_CHUNK_BYTES


def test_real_urllib_transport_rejects_four_mib_plus_one_before_response_creation() -> None:
    request = build_structure_request()
    payload = STRUCTURE_XML + b" " * (MAX_RESPONSE_BYTES - len(STRUCTURE_XML)) + b"X"
    wire_response = FakeWireResponse(request.url, payload)
    opener = FakeOpener(wire_response)
    transport = discovery._OneAttemptUrllibTransport()
    transport._opener = opener
    with pytest.raises(DiscoveryFailure, match="response_too_large"):
        execute_discovery(request, transport, RETRIEVED)
    assert opener.calls == 1
    assert max(wire_response.read_requests) <= discovery._READ_CHUNK_BYTES
    assert wire_response.read_requests[-1] == discovery._READ_CHUNK_BYTES


def test_real_urllib_transport_rejects_redirect_without_reading_body() -> None:
    request = build_structure_request()
    redirected_body = FakeWireResponse("https://stats.bis.org/api/v2/other", STRUCTURE_XML)
    redirect = urllib.error.HTTPError(request.url, 302, "Found", {}, redirected_body)
    opener = FakeOpener(redirect)
    transport = discovery._OneAttemptUrllibTransport()
    transport._opener = opener
    with pytest.raises(DiscoveryFailure, match="redirect_rejected"):
        execute_discovery(request, transport, RETRIEVED)
    assert opener.calls == 1
    assert redirected_body.read_requests == []


def test_malformed_metadata_and_unexpected_media_type_fail_closed() -> None:
    request = build_structure_request()
    malformed = FakeTransport(response(request.url, b"<root />"))
    with pytest.raises(DiscoveryFailure, match="metadata_representation_mismatch"):
        execute_discovery(request, malformed, RETRIEVED)
    unexpected = FakeTransport(response(request.url, STRUCTURE_XML, "text/html"))
    with pytest.raises(DiscoveryFailure, match="media_type_not_approved"):
        execute_discovery(request, unexpected, RETRIEVED)
    assert len(malformed.calls) == len(unexpected.calls) == 1


def test_probe_rejects_post_2023_response_without_truncation() -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    contaminated_dates = tuple(f"2023-01-{day:02d}" for day in range(1, 31)) + (
        "2024-01-02",
    )
    contaminated = probe_xml(dates=contaminated_dates)
    transport = FakeTransport(response(request.url, contaminated, "application/xml"))
    with pytest.raises(DiscoveryFailure, match="sealed_window_violation"):
        execute_discovery(request, transport, RETRIEVED)
    assert len(transport.calls) == 1


def test_request_response_mismatch_and_outside_probe_interval_fail_closed() -> None:
    request = build_d_us_schema_probe_request(metadata_insufficient=True)
    wrong_series = probe_xml(reference_area="CA")
    with pytest.raises(DiscoveryFailure, match="response_series_mismatch"):
        execute_discovery(
            request,
            FakeTransport(response(request.url, wrong_series, "application/xml")),
            RETRIEVED,
        )
    outside_dates = tuple(f"2023-01-{day:02d}" for day in range(1, 31)) + (
        "2023-02-01",
    )
    outside = probe_xml(dates=outside_dates)
    with pytest.raises(DiscoveryFailure, match="observation_outside_request"):
        execute_discovery(
            request,
            FakeTransport(response(request.url, outside, "application/xml")),
            RETRIEVED,
        )


def test_discovery_artifact_cannot_masquerade_as_authoritative_qualification() -> None:
    request = build_structure_request()
    artifact = execute_discovery(request, FakeTransport(response(request.url)), RETRIEVED).artifact
    assert not isinstance(artifact, CandidateBQualificationResult)
    assert artifact.authoritative_qualification_eligible is False
    assert artifact.final_run_identity_eligible is False


def test_discovery_contract_contains_no_performance_or_measurement_fields() -> None:
    forbidden = {
        "differential",
        "rank",
        "weight",
        "return",
        "pnl",
        "sharpe",
        "drawdown",
        "performance",
    }
    names = {item.name.lower() for item in fields(discovery.BisDiscoveryArtifact)}
    assert names.isdisjoint(forbidden)


def test_raw_artifact_paths_are_fixed_under_ignored_candidate_b_directory() -> None:
    request = build_structure_request()
    raw_path, manifest_path = raw_artifact_paths(request)
    approved = Path("data/raw/candidate_b/bis_discovery")
    assert raw_path.is_relative_to(approved)
    assert manifest_path.is_relative_to(approved)
    assert raw_path.name == "response.bin"
    assert manifest_path.name == "discovery.json"
    assert "@" not in str(raw_path)


def test_discovery_identity_is_header_insertion_order_invariant() -> None:
    request = build_structure_request()
    first = response(request.url, headers={"ETag": "a", "Cache-Control": "max-age=0"})
    second = response(request.url, headers={"Cache-Control": "max-age=0", "ETag": "a"})
    first_artifact = execute_discovery(request, FakeTransport(first), RETRIEVED).artifact
    second_artifact = execute_discovery(request, FakeTransport(second), RETRIEVED).artifact
    assert first_artifact.discovery_id == second_artifact.discovery_id


def test_returned_but_malformed_response_is_atomically_preserved_as_failed_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "data/raw/candidate_b/bis_discovery"
    monkeypatch.setattr(discovery, "RAW_DISCOVERY_ROOT", root)
    request = build_structure_request()
    raw = realistic_sdmx21_xml(include_dsd=False)
    transport = FakeTransport(response(request.url, raw, headers={"ETag": '"failed"'}))

    with pytest.raises(DiscoveryFailure, match="metadata_structure_incomplete"):
        discovery.execute_and_persist_discovery(request, transport, RETRIEVED)

    assert len(transport.calls) == 1
    raw_paths = tuple(root.rglob("response.bin"))
    manifest_paths = tuple(root.rglob("failure.json"))
    assert len(raw_paths) == len(manifest_paths) == 1
    assert raw_paths[0].read_bytes() == raw
    failure = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
    assert failure["classification"] == "NON_AUTHORITATIVE_DISCOVERY_FAILED"
    assert failure["failure_reason"] == "metadata_structure_incomplete"
    assert failure["request_identity"] == request.request_identity
    assert failure["exact_url"] == request.url
    assert failure["http_status"] == 200
    assert failure["content_type"] == "application/vnd.sdmx.structure+xml"
    assert failure["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert failure["byte_count"] == len(raw)
    assert failure["response_headers"] == {"etag": '"failed"'}
    assert failure["authoritative_qualification_eligible"] is False
    assert failure["final_run_identity_eligible"] is False
    assert failure["r4_evidence_eligible"] is False
    assert not tuple(root.rglob("*.tmp-*"))
    assert not tuple(root.rglob("discovery.json"))


def test_transport_failure_without_response_persists_nothing(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "data/raw/candidate_b/bis_discovery"
    monkeypatch.setattr(discovery, "RAW_DISCOVERY_ROOT", root)
    request = build_structure_request()
    transport = FakeTransport(TimeoutError("synthetic timeout"))

    with pytest.raises(DiscoveryFailure, match="discovery_timeout"):
        discovery.execute_and_persist_discovery(request, transport, RETRIEVED)

    assert len(transport.calls) == 1
    assert not root.exists()


def test_failed_discovery_is_not_automatically_followed_by_another_request(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        discovery,
        "RAW_DISCOVERY_ROOT",
        tmp_path / "data/raw/candidate_b/bis_discovery",
    )
    request = build_structure_request()
    transport = FakeTransport(
        response(request.url, realistic_sdmx21_xml(include_dsd=False))
    )

    with pytest.raises(DiscoveryFailure, match="metadata_structure_incomplete"):
        discovery.execute_and_persist_discovery(request, transport, RETRIEVED)

    assert len(transport.calls) == 1


def test_failed_discovery_artifact_cannot_masquerade_as_authoritative(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "data/raw/candidate_b/bis_discovery"
    monkeypatch.setattr(discovery, "RAW_DISCOVERY_ROOT", root)
    request = build_structure_request()
    with pytest.raises(DiscoveryFailure):
        discovery.execute_and_persist_discovery(
            request,
            FakeTransport(response(request.url, b"<not-sdmx />")),
            RETRIEVED,
        )
    failure = json.loads(next(root.rglob("failure.json")).read_text(encoding="utf-8"))
    assert failure["classification"] != DISCOVERY_CLASSIFICATION
    assert not isinstance(failure, CandidateBQualificationResult)
    assert all(
        failure[field] is False
        for field in (
            "authoritative_qualification_eligible",
            "final_run_identity_eligible",
            "r4_evidence_eligible",
        )
    )
