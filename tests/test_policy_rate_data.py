"""Candidate B policy-rate data qualification contracts (no market outcomes)."""

from __future__ import annotations

import calendar
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from scripts.ingest_bis_policy_rates import (
    BisTransportResponse,
    ingest_series,
)
from scripts.ingest_bis_policy_rates import (
    main as ingest_main,
)
from scripts.qualify_candidate_b_data import qualify_candidate_b

from fxlab.data.policy_rates import (
    APPROVED_BIS_SERIES,
    APPROVED_PAIRS,
    APPROVED_REQUEST_END,
    APPROVED_REQUEST_START,
    EXPECTED_TOTAL_COHORTS,
    EXPECTED_TRAIN_COHORTS,
    EXPECTED_VALIDATION_COHORTS,
    MAX_OBSERVATION_DATE,
    AmbiguityState,
    CandidateBFormation,
    CandidateBFormationManifest,
    CandidateBQualificationResult,
    ConcordanceStatus,
    EvidenceClassification,
    FormationSplit,
    PolicyConcordanceResult,
    PolicyEventKind,
    PolicyEventManifest,
    PolicyRateEvent,
    PolicyRateMetadata,
    PolicyRateObservation,
    PolicyRateQualificationError,
    PolicyRateRequest,
    PolicyRateSeriesManifest,
    PolicyRateSeriesSpec,
    PolicySourceEvidence,
    PolicyStateReference,
    SpotObservationReference,
    SpotPanelManifestReference,
    TimePrecision,
    build_series_manifest,
    canonical_json,
    canonical_sha256,
    event_is_eligible,
    parse_bis_csv,
    qualify_formation,
    reconcile_policy_series,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
RETRIEVED = datetime(2026, 8, 29, 10, tzinfo=UTC)


def authoritative_d_us_xml() -> bytes:
    observations = "".join(
        f'<Obs TIME_PERIOD="{observed.isoformat()}" OBS_VALUE="5.25" OBS_STATUS="A" />'
        for observed in (
            APPROVED_REQUEST_START + timedelta(days=offset)
            for offset in range((APPROVED_REQUEST_END - APPROVED_REQUEST_START).days + 1)
        )
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<mes:StructureSpecificData '
        'xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message" '
        'xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common" '
        'xmlns:ss="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/structurespecific" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:cbpol="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow='
        'BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD">'
        '<mes:Header><mes:Structure structureID="BIS_WS_CBPOL_1_0" '
        'namespace="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow='
        'BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD" '
        'dimensionAtObservation="TIME_PERIOD"><com:StructureUsage><Ref '
        'agencyID="BIS" id="WS_CBPOL" version="1.0" /></com:StructureUsage>'
        '</mes:Structure></mes:Header>'
        '<mes:DataSet UNIT_MEASURE="368" UNIT_MULT="0" '
        'ss:dataScope="DataStructure" ss:structureRef="BIS_WS_CBPOL_1_0" '
        'xsi:type="cbpol:DataSetType"><Series FREQ="D" REF_AREA="US">'
        f"{observations}</Series></mes:DataSet></mes:StructureSpecificData>"
    ).encode()


def test_authoritative_d_us_sdmx_contract_accepts_full_frozen_request() -> None:
    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_US_ACCEPT,
        AUTHORITATIVE_D_US_URL,
        authoritative_d_us_request,
        parse_authoritative_bis_d_us_sdmx,
    )

    item = authoritative_d_us_request()
    assert item.series == spec("USD")
    assert item.start == date(2014, 1, 1)
    assert item.end == date(2023, 12, 31)
    assert AUTHORITATIVE_D_US_URL == (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.US"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    assert AUTHORITATIVE_D_US_ACCEPT == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    parsed = parse_authoritative_bis_d_us_sdmx(authoritative_d_us_xml(), item)
    assert len(parsed) == 3652
    assert parsed[0].observation_date == date(2014, 1, 1)
    assert parsed[-1].observation_date == date(2023, 12, 31)
    assert all(
        observation.series_key == "D.US" and observation.status == "A"
        for observation in parsed
    )


def _replace_authoritative_xml(old: bytes, new: bytes, *, count: int = -1) -> bytes:
    raw = authoritative_d_us_xml()
    assert old in raw
    return raw.replace(old, new, count)


@pytest.mark.parametrize(
    "raw",
    [
        _replace_authoritative_xml(
            b"<mes:StructureSpecificData ",
            b"<mes:GenericData ",
            count=1,
        ).replace(b"</mes:StructureSpecificData>", b"</mes:GenericData>", 1),
        _replace_authoritative_xml(
            b"urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow="
            b"BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD",
            b"urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow="
            b"BIS:OTHER(1.0):ObsLevelDim:TIME_PERIOD",
        ),
        _replace_authoritative_xml(b"ss:dataScope=", b"dataScope=", count=1),
        _replace_authoritative_xml(b"ss:structureRef=", b"structureRef=", count=1),
        _replace_authoritative_xml(
            b'UNIT_MEASURE="368"', b'UNIT_MEASURE="999"', count=1
        ),
        _replace_authoritative_xml(b'UNIT_MULT="0"', b'UNIT_MULT="1"', count=1),
        _replace_authoritative_xml(b' OBS_STATUS="A"', b"", count=1),
        _replace_authoritative_xml(
            b' OBS_STATUS="A"', b' OBS_CONF="A"', count=1
        ),
        _replace_authoritative_xml(
            b' OBS_STATUS="A"', b' OBS_STATUS="B"', count=1
        ),
    ],
    ids=(
        "wrong_root",
        "wrong_structure_namespace",
        "unqualified_data_scope",
        "unqualified_structure_ref",
        "wrong_unit_measure",
        "wrong_unit_mult",
        "missing_status",
        "obs_conf_cannot_substitute",
        "non_a_status",
    ),
)
def test_authoritative_d_us_adversarial_contract_mismatches_fail(raw: bytes) -> None:
    from fxlab.data.policy_rates import (
        authoritative_d_us_request,
        parse_authoritative_bis_d_us_sdmx,
    )

    with pytest.raises(PolicyRateQualificationError):
        parse_authoritative_bis_d_us_sdmx(raw, authoritative_d_us_request())


def test_authoritative_d_us_adversarial_duplicate_date_fails() -> None:
    from fxlab.data.policy_rates import (
        authoritative_d_us_request,
        parse_authoritative_bis_d_us_sdmx,
    )

    raw = _replace_authoritative_xml(b"2014-01-02", b"2014-01-01", count=1)
    with pytest.raises(PolicyRateQualificationError, match="duplicate_observation"):
        parse_authoritative_bis_d_us_sdmx(raw, authoritative_d_us_request())


def test_authoritative_d_us_adversarial_missing_required_date_fails() -> None:
    from fxlab.data.policy_rates import (
        authoritative_d_us_request,
        parse_authoritative_bis_d_us_sdmx,
    )

    raw = _replace_authoritative_xml(
        b'<Obs TIME_PERIOD="2014-01-02" OBS_VALUE="5.25" OBS_STATUS="A" />',
        b"",
        count=1,
    )
    with pytest.raises(PolicyRateQualificationError, match="observation_date_set_mismatch"):
        parse_authoritative_bis_d_us_sdmx(raw, authoritative_d_us_request())


def test_authoritative_d_us_adversarial_post_2023_rejects_before_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fxlab.data.policy_rates as policy_rates

    raw = _replace_authoritative_xml(b"2014-01-01", b"2024-01-01", count=1)

    def forbidden_decimal_access(_: object) -> None:
        raise AssertionError("OBS_VALUE accessed before sealed date rejection")

    monkeypatch.setattr(policy_rates, "Decimal", forbidden_decimal_access)
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        policy_rates.parse_authoritative_bis_d_us_sdmx(
            raw, policy_rates.authoritative_d_us_request()
        )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "not-a-decimal"])
def test_authoritative_d_us_adversarial_invalid_or_nonfinite_value_fails(
    value: str,
) -> None:
    from fxlab.data.policy_rates import (
        authoritative_d_us_request,
        parse_authoritative_bis_d_us_sdmx,
    )

    raw = _replace_authoritative_xml(
        b'OBS_VALUE="5.25"',
        f'OBS_VALUE="{value}"'.encode(),
        count=1,
    )
    with pytest.raises(PolicyRateQualificationError, match="observation_value_invalid"):
        parse_authoritative_bis_d_us_sdmx(raw, authoritative_d_us_request())


@pytest.mark.parametrize(
    "declaration",
    [
        b'<!DOCTYPE data SYSTEM "http://example.invalid/schema.dtd">',
        b'<!ENTITY secret "forbidden">',
    ],
)
def test_authoritative_d_us_adversarial_dtd_or_entity_fails(
    declaration: bytes,
) -> None:
    from fxlab.data.policy_rates import (
        authoritative_d_us_request,
        parse_authoritative_bis_d_us_sdmx,
    )

    raw = authoritative_d_us_xml().replace(b"?>", b"?>" + declaration, 1)
    with pytest.raises(PolicyRateQualificationError, match="response_schema_invalid"):
        parse_authoritative_bis_d_us_sdmx(raw, authoritative_d_us_request())


def authoritative_sparse_xml(
    *,
    reference_area: str,
    observations: tuple[tuple[str, str, str], ...],
) -> bytes:
    observation_xml = "".join(
        f'<Obs TIME_PERIOD="{observed}" OBS_VALUE="{value}" OBS_STATUS="{status}" />'
        for observed, value, status in observations
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<mes:StructureSpecificData '
        'xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message" '
        'xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common" '
        'xmlns:ss="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/structurespecific" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:cbpol="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow='
        'BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD">'
        '<mes:Header><mes:Structure structureID="BIS_WS_CBPOL_1_0" '
        'namespace="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow='
        'BIS:WS_CBPOL(1.0):ObsLevelDim:TIME_PERIOD" '
        'dimensionAtObservation="TIME_PERIOD"><com:StructureUsage><Ref '
        'agencyID="BIS" id="WS_CBPOL" version="1.0" /></com:StructureUsage>'
        '</mes:Structure></mes:Header>'
        '<mes:DataSet UNIT_MEASURE="368" UNIT_MULT="0" '
        'ss:dataScope="DataStructure" ss:structureRef="BIS_WS_CBPOL_1_0" '
        f'xsi:type="cbpol:DataSetType"><Series FREQ="D" REF_AREA="{reference_area}">'
        f"{observation_xml}</Series></mes:DataSet></mes:StructureSpecificData>"
    ).encode()


def _parse_authoritative_sparse(raw: object, currency: str = "AUD"):
    from fxlab.data.policy_rates import parse_authoritative_bis_sdmx

    return parse_authoritative_bis_sdmx(raw, request(currency))


def test_authoritative_sparse_a_only_series_preserves_exact_observations() -> None:
    supplied = (
        ("2014-01-03", "2.50", "A"),
        ("2014-01-07", "2.50", "A"),
        ("2014-01-31", "2.75", "A"),
    )

    parsed = _parse_authoritative_sparse(
        authoritative_sparse_xml(reference_area="AU", observations=supplied)
    )

    assert tuple(item.observation_date.isoformat() for item in parsed) == tuple(
        item[0] for item in supplied
    )
    assert tuple(item.value for item in parsed) == tuple(
        Decimal(item[1]) for item in supplied
    )
    assert tuple(item.status for item in parsed) == ("A", "A", "A")
    assert len(parsed) == len(supplied)


@pytest.mark.parametrize(
    ("observations", "reason"),
    (
        (
            (("2014-01-03", "2.50", "A"), ("2014-01-03", "2.75", "A")),
            "duplicate_observation",
        ),
        (
            (("2014-01-07", "2.50", "A"), ("2014-01-03", "2.75", "A")),
            "observation_order_invalid",
        ),
        ((("2013-12-31", "2.50", "A"),), "observation_outside_request"),
        ((("2024-01-01", "2.50", "A"),), "sealed_window_violation"),
    ),
)
def test_authoritative_sparse_series_rejects_invalid_date_sequences(
    observations: tuple[tuple[str, str, str], ...], reason: str
) -> None:
    with pytest.raises(PolicyRateQualificationError, match=reason):
        _parse_authoritative_sparse(
            authoritative_sparse_xml(reference_area="AU", observations=observations)
        )


@pytest.mark.parametrize("value", ("NaN", "Infinity", "not-a-number"))
def test_authoritative_sparse_a_status_rejects_nonfinite_or_malformed_values(
    value: str,
) -> None:
    with pytest.raises(PolicyRateQualificationError, match="observation_value_invalid"):
        _parse_authoritative_sparse(
            authoritative_sparse_xml(
                reference_area="AU",
                observations=(("2014-01-03", value, "A"),),
            )
        )


@pytest.mark.parametrize(
    ("currency", "reference_area"),
    (("AUD", "AU"), ("CAD", "CA"), ("GBP", "GB"), ("NZD", "NZ")),
)
def test_authoritative_sparse_m_nan_is_raw_only_uniformly(
    currency: str, reference_area: str
) -> None:
    raw = authoritative_sparse_xml(
        reference_area=reference_area,
        observations=(
            ("2014-01-03", "2.50", "A"),
            ("2014-01-07", "NaN", "M"),
            ("2014-01-31", "2.75", "A"),
        ),
    )

    parsed = _parse_authoritative_sparse(raw, currency)

    assert parsed.raw_row_count == 3
    assert parsed.numeric_observation_count == 2
    assert tuple(item.observation_date for item in parsed.observations) == (
        date(2014, 1, 3),
        date(2014, 1, 31),
    )
    assert tuple(item.value for item in parsed.observations) == (
        Decimal("2.50"),
        Decimal("2.75"),
    )
    assert all(item.status == "A" for item in parsed.observations)


@pytest.mark.parametrize(
    ("status", "value", "reason"),
    (
        ("A", "NaN", "observation_value_invalid"),
        ("M", "2.50", "observation_status_value_invalid"),
        ("X", "2.50", "observation_status_value_invalid"),
        ("X", "NaN", "observation_status_value_invalid"),
    ),
)
def test_authoritative_sparse_rejects_unsupported_status_value_combinations(
    status: str, value: str, reason: str
) -> None:
    raw = authoritative_sparse_xml(
        reference_area="AU",
        observations=(("2014-01-03", value, status),),
    )

    with pytest.raises(PolicyRateQualificationError, match=reason):
        _parse_authoritative_sparse(raw)


def test_authoritative_sparse_m_without_literal_nan_fails_closed() -> None:
    raw = authoritative_sparse_xml(
        reference_area="AU",
        observations=(("2014-01-03", "NaN", "M"),),
    ).replace(b' OBS_VALUE="NaN"', b"")

    with pytest.raises(
        PolicyRateQualificationError, match="observation_status_value_invalid"
    ):
        _parse_authoritative_sparse(raw)


def test_authoritative_sparse_canonical_identity_binds_only_exact_observations() -> None:
    original = _parse_authoritative_sparse(
        authoritative_sparse_xml(
            reference_area="AU",
            observations=(
                ("2014-01-03", "2.50", "A"),
                ("2014-01-31", "2.75", "A"),
            ),
        )
    )
    changed_date = _parse_authoritative_sparse(
        authoritative_sparse_xml(
            reference_area="AU",
            observations=(
                ("2014-01-04", "2.50", "A"),
                ("2014-01-31", "2.75", "A"),
            ),
        )
    )
    changed_value = _parse_authoritative_sparse(
        authoritative_sparse_xml(
            reference_area="AU",
            observations=(
                ("2014-01-03", "2.51", "A"),
                ("2014-01-31", "2.75", "A"),
            ),
        )
    )

    assert len(original) == len(changed_date) == len(changed_value) == 2
    assert (
    len(
        {
            canonical_sha256(original),
            canonical_sha256(changed_date),
            canonical_sha256(changed_value),
        }
    )
    == 3
)


def test_authoritative_sparse_parser_rejects_discovery_artifact_input() -> None:
    discovery_artifact = {
        "classification": "NON_AUTHORITATIVE_DISCOVERY",
        "raw_bytes": authoritative_sparse_xml(
            reference_area="AU",
            observations=(("2014-01-03", "2.50", "A"),),
        ),
    }

    with pytest.raises(
        PolicyRateQualificationError,
        match="authoritative_raw_bytes_required",
    ):
        _parse_authoritative_sparse(discovery_artifact)


class FakeAuthoritativeTransport:
    def __init__(self, response: object = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[object, str, str, int, int]] = []

    def fetch(
        self,
        request: object,
        *,
        exact_url: str,
        accept: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> object:
        self.calls.append(
            (request, exact_url, accept, timeout_seconds, max_response_bytes)
        )
        if self.error is not None:
            raise self.error
        return self.response


def test_authoritative_d_us_transport_uses_exact_fixed_contract_once() -> None:
    from urllib.parse import parse_qsl, urlsplit

    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AUTHORITATIVE_TIMEOUT_SECONDS,
        AuthoritativeBisHttpResponse,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_US_ACCEPT,
        AUTHORITATIVE_D_US_URL,
        authoritative_d_us_request,
    )

    item = authoritative_d_us_request()
    response = AuthoritativeBisHttpResponse(
        status_code=200,
        final_url=AUTHORITATIVE_D_US_URL,
        media_type="application/xml",
        headers={"Content-Type": "application/xml"},
        raw_bytes=b"<response/>",
    )
    transport = FakeAuthoritativeTransport(response)
    assert fetch_authoritative_d_us_response(item, transport) is response
    assert transport.calls == [
        (
            item,
            AUTHORITATIVE_D_US_URL,
            AUTHORITATIVE_D_US_ACCEPT,
            15,
            4 * 1024 * 1024,
        )
    ]
    assert AUTHORITATIVE_TIMEOUT_SECONDS == 15
    assert AUTHORITATIVE_MAX_RESPONSE_BYTES == 4 * 1024 * 1024
    parsed = urlsplit(transport.calls[0][1])
    assert parsed.scheme == "https"
    assert parsed.hostname == "stats.bis.org"
    assert parsed.path == "/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.US"
    assert tuple(parse_qsl(parsed.query)) == (
        ("startPeriod", "2014-01-01"),
        ("endPeriod", "2023-12-31"),
    )


@pytest.mark.parametrize(
    ("status_code", "final_url", "media_type", "reason"),
    [
        (302, "https://stats.bis.org/redirect", "application/xml", "redirect_rejected"),
        (
            500,
            "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.US"
            "?startPeriod=2014-01-01&endPeriod=2023-12-31",
            "application/xml",
            "http_status_not_success",
        ),
        (
            200,
            "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.US"
            "?startPeriod=2014-01-01&endPeriod=2023-12-30",
            "application/xml",
            "redirect_rejected",
        ),
        (
            200,
            "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.US"
            "?startPeriod=2014-01-01&endPeriod=2023-12-31",
            "text/xml",
            "media_type_not_approved",
        ),
    ],
)
def test_authoritative_d_us_transport_rejects_redirect_status_url_or_media(
    status_code: int,
    final_url: str,
    media_type: str,
    reason: str,
) -> None:
    from scripts.ingest_bis_policy_rates import (
        AuthoritativeBisHttpResponse,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import authoritative_d_us_request

    response = AuthoritativeBisHttpResponse(
        status_code=status_code,
        final_url=final_url,
        media_type=media_type,
        headers={},
        raw_bytes=b"<response/>",
    )
    transport = FakeAuthoritativeTransport(response)
    with pytest.raises(PolicyRateQualificationError, match=reason):
        fetch_authoritative_d_us_response(authoritative_d_us_request(), transport)
    assert len(transport.calls) == 1


def test_authoritative_d_us_transport_timeout_has_no_retry_or_fallback() -> None:
    from scripts.ingest_bis_policy_rates import fetch_authoritative_d_us_response

    from fxlab.data.policy_rates import authoritative_d_us_request

    transport = FakeAuthoritativeTransport(error=TimeoutError("synthetic timeout"))
    with pytest.raises(PolicyRateQualificationError, match="acquisition_timeout"):
        fetch_authoritative_d_us_response(authoritative_d_us_request(), transport)
    assert len(transport.calls) == 1


def test_authoritative_d_us_transport_rejects_more_than_four_mib_once() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AuthoritativeBisHttpResponse,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import AUTHORITATIVE_D_US_URL, authoritative_d_us_request

    response = AuthoritativeBisHttpResponse(
        status_code=200,
        final_url=AUTHORITATIVE_D_US_URL,
        media_type="application/xml",
        headers={},
        raw_bytes=b"x" * (AUTHORITATIVE_MAX_RESPONSE_BYTES + 1),
    )
    transport = FakeAuthoritativeTransport(response)
    with pytest.raises(PolicyRateQualificationError, match="response_too_large"):
        fetch_authoritative_d_us_response(authoritative_d_us_request(), transport)
    assert len(transport.calls) == 1


def test_authoritative_d_us_transport_never_continues_to_another_series() -> None:
    from scripts.ingest_bis_policy_rates import fetch_authoritative_d_us_response

    from fxlab.data.policy_rates import authoritative_d_us_request

    transport = FakeAuthoritativeTransport(error=ValueError("synthetic invalid response"))
    with pytest.raises(PolicyRateQualificationError, match="transport_failure"):
        fetch_authoritative_d_us_response(authoritative_d_us_request(), transport)
    assert [call[0] for call in transport.calls] == [authoritative_d_us_request()]


class FakeUrllibResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        final_url: str,
        content_type: str = "application/xml",
    ) -> None:
        self._body = body
        self._offset = 0
        self.status = status
        self._final_url = final_url
        self.headers = {"Content-Type": content_type, "ETag": "synthetic"}
        self.read_sizes: list[int] = []
        self.bytes_served = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def geturl(self) -> str:
        return self._final_url

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("authoritative adapter attempted an unbounded read")
        self.read_sizes.append(size)
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        self.bytes_served += len(chunk)
        return chunk


class FakeUrllibOpener:
    def __init__(
        self,
        response: FakeUrllibResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[object, int]] = []

    def open(self, request, *, timeout: int):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def test_authoritative_d_us_real_http_adapter_uses_exact_bounded_get() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        UrllibAuthoritativeBisTransport,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import authoritative_d_us_request

    exact_url = (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.US"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    raw = b"<synthetic-authoritative-response/>"
    response = FakeUrllibResponse(raw, final_url=exact_url)
    opener = FakeUrllibOpener(response)
    result = fetch_authoritative_d_us_response(
        authoritative_d_us_request(), UrllibAuthoritativeBisTransport(opener)
    )

    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.full_url == exact_url
    assert request.get_method() == "GET"
    assert request.get_header("Accept") == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert timeout == 15
    assert response.read_sizes
    assert all(0 < size <= AUTHORITATIVE_MAX_RESPONSE_BYTES + 1 for size in response.read_sizes)
    assert result.raw_bytes == raw
    assert result.final_url == exact_url
    assert result.media_type == "application/xml"
    assert result.headers["content-type"] == "application/xml"


def test_authoritative_d_us_real_http_adapter_rejects_max_plus_one() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        UrllibAuthoritativeBisTransport,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import AUTHORITATIVE_D_US_URL, authoritative_d_us_request

    response = FakeUrllibResponse(
        b"x" * (AUTHORITATIVE_MAX_RESPONSE_BYTES + 1),
        final_url=AUTHORITATIVE_D_US_URL,
    )
    opener = FakeUrllibOpener(response)
    with pytest.raises(PolicyRateQualificationError, match="response_too_large"):
        fetch_authoritative_d_us_response(
            authoritative_d_us_request(), UrllibAuthoritativeBisTransport(opener)
        )
    assert len(opener.calls) == 1
    assert response.bytes_served == AUTHORITATIVE_MAX_RESPONSE_BYTES + 1
    assert all(size <= AUTHORITATIVE_MAX_RESPONSE_BYTES + 1 for size in response.read_sizes)


def test_authoritative_d_us_real_http_adapter_redirect_is_not_followed() -> None:
    from scripts.ingest_bis_policy_rates import (
        UrllibAuthoritativeBisTransport,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import AUTHORITATIVE_D_US_URL, authoritative_d_us_request

    opener = FakeUrllibOpener(
        FakeUrllibResponse(
            b"redirect",
            status=302,
            final_url="https://stats.bis.org/redirected",
        )
    )
    with pytest.raises(PolicyRateQualificationError, match="redirect_rejected"):
        fetch_authoritative_d_us_response(
            authoritative_d_us_request(), UrllibAuthoritativeBisTransport(opener)
        )
    assert len(opener.calls) == 1
    assert opener.calls[0][0].full_url == AUTHORITATIVE_D_US_URL


def test_authoritative_d_us_real_http_adapter_rejects_changed_final_url() -> None:
    from scripts.ingest_bis_policy_rates import (
        UrllibAuthoritativeBisTransport,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import authoritative_d_us_request

    opener = FakeUrllibOpener(
        FakeUrllibResponse(
            b"xml",
            final_url="https://stats.bis.org/api/v2/data/alternate",
        )
    )
    with pytest.raises(PolicyRateQualificationError, match="redirect_rejected"):
        fetch_authoritative_d_us_response(
            authoritative_d_us_request(), UrllibAuthoritativeBisTransport(opener)
        )
    assert len(opener.calls) == 1


def test_authoritative_d_us_real_http_adapter_requires_http_200() -> None:
    from scripts.ingest_bis_policy_rates import (
        UrllibAuthoritativeBisTransport,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import AUTHORITATIVE_D_US_URL, authoritative_d_us_request

    opener = FakeUrllibOpener(
        FakeUrllibResponse(b"failure", status=503, final_url=AUTHORITATIVE_D_US_URL)
    )
    with pytest.raises(PolicyRateQualificationError, match="http_status_not_success"):
        fetch_authoritative_d_us_response(
            authoritative_d_us_request(), UrllibAuthoritativeBisTransport(opener)
        )
    assert len(opener.calls) == 1


def test_authoritative_d_us_real_http_adapter_timeout_has_no_retry() -> None:
    from scripts.ingest_bis_policy_rates import (
        UrllibAuthoritativeBisTransport,
        fetch_authoritative_d_us_response,
    )

    from fxlab.data.policy_rates import authoritative_d_us_request

    opener = FakeUrllibOpener(error=TimeoutError("synthetic timeout"))
    with pytest.raises(PolicyRateQualificationError, match="acquisition_timeout"):
        fetch_authoritative_d_us_response(
            authoritative_d_us_request(), UrllibAuthoritativeBisTransport(opener)
        )
    assert len(opener.calls) == 1


def test_authoritative_d_us_offline_end_to_end_integration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_US_URL,
        authoritative_d_us_request,
        canonical_sha256,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_us_request()
    destination = root / f"d_us-{item.fingerprint}"
    raw = authoritative_d_us_xml()
    response = FakeUrllibResponse(raw, final_url=AUTHORITATIVE_D_US_URL)
    opener = FakeUrllibOpener(response)
    validated_counts: list[int] = []
    original_parse = ingestion.parse_authoritative_bis_d_us_sdmx

    def recording_parse(raw_bytes: bytes, request: PolicyRateRequest):
        assert not destination.exists()
        observations = original_parse(raw_bytes, request)
        validated_counts.append(len(observations))
        assert not destination.exists()
        return observations

    monkeypatch.setattr(
        ingestion,
        "parse_authoritative_bis_d_us_sdmx",
        recording_parse,
    )
    published = ingestion.acquire_and_publish_authoritative_d_us(
        item,
        ingestion.UrllibAuthoritativeBisTransport(opener),
        RETRIEVED,
    )

    assert validated_counts == [3652]
    assert len(opener.calls) == 1
    http_request, timeout = opener.calls[0]
    assert http_request.full_url == AUTHORITATIVE_D_US_URL
    assert http_request.get_method() == "GET"
    assert http_request.get_header("Accept") == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert timeout == 15
    assert published.destination == destination
    assert destination.is_dir()
    assert published.raw_path.read_bytes() == raw
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["manifest_id"] == published.manifest.manifest_id
    assert persisted["dataset_id"] == published.manifest.dataset_id

    manifest = published.manifest
    expected_dataset_id = canonical_sha256(
        {
            "format": 1,
            "request_fingerprint": item.fingerprint,
            "exact_url": AUTHORITATIVE_D_US_URL,
            "representation_identity": "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA",
            "series_key": "D.US",
            "frequency": "D",
            "reference_area": "US",
            "unit_measure": "368",
            "unit_mult": "0",
            "status_semantics": (
                "A=normal",
                "M=missing_value_data_cannot_exist",
            ),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_observation_hash": manifest.canonical_observation_hash,
            "raw_row_count": 3652,
            "numeric_observation_count": 3652,
            "min_observation_date": date(2014, 1, 1),
            "max_observation_date": date(2023, 12, 31),
        }
    )
    assert manifest.dataset_id == expected_dataset_id
    assert item.series.series_key == "D.US"
    assert [call[0].full_url for call in opener.calls] == [AUTHORITATIVE_D_US_URL]


def test_authoritative_d_us_cli_help_is_network_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    def forbidden_transport():
        raise AssertionError("transport constructed while rendering help")

    monkeypatch.setattr(ingestion, "UrllibAuthoritativeBisTransport", forbidden_transport)
    with pytest.raises(SystemExit) as exc_info:
        ingestion.main(["--help"])
    assert exc_info.value.code == 0
    assert "--authorize-network-acquisition" in capsys.readouterr().out


def test_authoritative_d_us_cli_missing_authorization_is_network_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    calls: list[object] = []
    monkeypatch.setattr(
        ingestion,
        "acquire_and_publish_authoritative_d_us",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    with pytest.raises(SystemExit, match="network_acquisition_not_authorized"):
        ingestion.main(["--target", "d_us"])
    assert calls == []


def test_authoritative_d_us_cli_wrong_target_is_rejected_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    calls: list[object] = []
    monkeypatch.setattr(
        ingestion,
        "acquire_and_publish_authoritative_d_us",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    with pytest.raises(SystemExit) as exc_info:
        ingestion.main(
            ["--authorize-network-acquisition", "--target", "not_d_us"]
        )
    assert exc_info.value.code == 2
    assert calls == []


def test_authoritative_d_us_cli_exact_authorized_path_invokes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_us_request

    transport = object()
    calls: list[tuple[object, object, datetime]] = []
    destination = tmp_path / "d_us-fixed"
    publication = SimpleNamespace(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=SimpleNamespace(dataset_id="d" * 64, manifest_id="m" * 64),
    )

    monkeypatch.setattr(
        ingestion,
        "UrllibAuthoritativeBisTransport",
        lambda: transport,
    )

    def fake_acquire(request, supplied_transport, retrieved_at):
        calls.append((request, supplied_transport, retrieved_at))
        return publication

    monkeypatch.setattr(
        ingestion,
        "acquire_and_publish_authoritative_d_us",
        fake_acquire,
    )
    ingestion.main(["--authorize-network-acquisition", "--target", "d_us"])

    assert len(calls) == 1
    request, supplied_transport, retrieved_at = calls[0]
    assert request == authoritative_d_us_request()
    assert supplied_transport is transport
    assert retrieved_at.tzinfo is UTC
    assert capsys.readouterr().out.splitlines() == [
        f"destination={publication.destination}",
        f"raw_path={publication.raw_path}",
        f"manifest_path={publication.manifest_path}",
        f"dataset_id={publication.manifest.dataset_id}",
        f"manifest_id={publication.manifest.manifest_id}",
    ]


def _authoritative_transport_with_raw(raw_bytes: bytes) -> FakeAuthoritativeTransport:
    from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

    from fxlab.data.policy_rates import AUTHORITATIVE_D_US_URL

    return FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=AUTHORITATIVE_D_US_URL,
            media_type="application/xml",
            headers={
                "Content-Type": "application/xml",
                "ETag": "synthetic-etag",
            },
            raw_bytes=raw_bytes,
        )
    )


def test_authoritative_d_us_storage_existing_destination_precedes_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_us_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_us_request()
    destination = root / f"d_us-{item.fingerprint}"
    destination.mkdir(parents=True)
    transport = _authoritative_transport_with_raw(authoritative_d_us_xml())
    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        ingestion.acquire_and_publish_authoritative_d_us(item, transport, RETRIEVED)
    assert transport.calls == []


def test_authoritative_d_us_storage_publishes_raw_and_manifest_atomically(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from pathlib import Path

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_us_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    replacements: list[tuple[Path, Path, bool]] = []
    original_replace = Path.replace

    def recording_replace(source: Path, target: Path) -> Path:
        replacements.append((source, target, target.exists()))
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", recording_replace)
    item = authoritative_d_us_request()
    raw = authoritative_d_us_xml()
    published = ingestion.acquire_and_publish_authoritative_d_us(
        item, _authoritative_transport_with_raw(raw), RETRIEVED
    )
    expected_destination = root / f"d_us-{item.fingerprint}"
    assert published.destination == expected_destination
    assert published.raw_path == expected_destination / "response.xml"
    assert published.manifest_path == expected_destination / "manifest.json"
    assert published.raw_path.read_bytes() == raw
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["dataset_id"] == published.manifest.dataset_id
    assert persisted["manifest_id"] == published.manifest.manifest_id
    assert replacements == [
        (
            replacements[0][0],
            expected_destination,
            False,
        )
    ]
    assert replacements[0][0].parent == expected_destination.parent
    assert replacements[0][0].name.startswith(".tmp-")


def test_authoritative_d_us_storage_atomic_failure_leaves_no_final_destination(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_us_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)

    def fail_replace(source: Path, target: Path) -> Path:
        del source, target
        raise OSError("synthetic atomic publication failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    item = authoritative_d_us_request()
    with pytest.raises(OSError, match="synthetic atomic publication failure"):
        ingestion.acquire_and_publish_authoritative_d_us(
            item,
            _authoritative_transport_with_raw(authoritative_d_us_xml()),
            RETRIEVED,
        )
    destination = root / f"d_us-{item.fingerprint}"
    assert not destination.exists()
    assert not tuple(root.glob(".tmp-*"))


def test_authoritative_d_us_storage_invalid_body_never_publishes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_us_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_us_request()
    with pytest.raises(PolicyRateQualificationError, match="response_schema_invalid"):
        ingestion.acquire_and_publish_authoritative_d_us(
            item, _authoritative_transport_with_raw(b"<unrelated/>"), RETRIEVED
        )
    assert not (root / f"d_us-{item.fingerprint}").exists()
    assert not root.exists()


def test_authoritative_d_us_manifest_binds_frozen_semantic_evidence(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_US_URL,
        authoritative_d_us_request,
        canonical_sha256,
        parse_authoritative_bis_d_us_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_us_request()
    raw = authoritative_d_us_xml()
    published = ingestion.acquire_and_publish_authoritative_d_us(
        item, _authoritative_transport_with_raw(raw), RETRIEVED
    )
    manifest = published.manifest
    assert manifest.request_fingerprint == item.fingerprint
    assert manifest.exact_url == AUTHORITATIVE_D_US_URL
    assert manifest.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert (manifest.series_key, manifest.frequency, manifest.reference_area) == (
        "D.US",
        "D",
        "US",
    )
    assert (manifest.unit_measure, manifest.unit_mult) == ("368", "0")
    assert manifest.status_semantics == (
        "A=normal",
        "M=missing_value_data_cannot_exist",
    )
    assert manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.canonical_observation_hash == canonical_sha256(
        parse_authoritative_bis_d_us_sdmx(raw, item)
    )
    assert manifest.raw_row_count == 3652
    assert manifest.numeric_observation_count == 3652
    assert manifest.min_observation_date == date(2014, 1, 1)
    assert manifest.max_observation_date == date(2023, 12, 31)


def test_authoritative_d_us_manifest_separates_semantic_and_audit_identity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_us_request

    item = authoritative_d_us_request()
    raw = authoritative_d_us_xml()
    manifests = []
    for index, retrieved_at in enumerate((RETRIEVED, RETRIEVED + timedelta(days=1))):
        monkeypatch.setattr(
            ingestion,
            "AUTHORITATIVE_BIS_ROOT",
            tmp_path / f"root-{index}",
        )
        manifests.append(
            ingestion.acquire_and_publish_authoritative_d_us(
                item,
                _authoritative_transport_with_raw(raw),
                retrieved_at,
            ).manifest
        )
    assert manifests[0].dataset_id == manifests[1].dataset_id
    assert manifests[0].manifest_id != manifests[1].manifest_id

    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", tmp_path / "third-root")
    same_time_different_path = ingestion.acquire_and_publish_authoritative_d_us(
        item, _authoritative_transport_with_raw(raw), RETRIEVED
    ).manifest
    assert same_time_different_path.dataset_id == manifests[0].dataset_id


@pytest.mark.parametrize(
    "classification",
    ("NON_AUTHORITATIVE_DISCOVERY", "NON_AUTHORITATIVE_DISCOVERY_FAILED"),
)
def test_authoritative_d_us_manifest_discovery_classification_cannot_be_promoted(
    classification: str,
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_us_request

    transport = _authoritative_transport_with_raw(authoritative_d_us_xml())
    with pytest.raises(TypeError):
        ingestion.acquire_and_publish_authoritative_d_us(
            authoritative_d_us_request(),
            transport,
            RETRIEVED,
            classification=classification,
        )
    assert transport.calls == []


def _authoritative_d_au_raw() -> bytes:
    return authoritative_sparse_xml(
        reference_area="AU",
        observations=(
            ("2014-01-03", "2.50", "A"),
            ("2018-06-15", "1.50", "A"),
            ("2023-12-29", "4.35", "A"),
        ),
    )


def _authoritative_d_au_transport(raw_bytes: bytes) -> FakeAuthoritativeTransport:
    from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

    from fxlab.data.policy_rates import AUTHORITATIVE_D_AU_URL

    return FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=AUTHORITATIVE_D_AU_URL,
            media_type="application/xml",
            headers={"Content-Type": "application/xml", "ETag": "synthetic-au"},
            raw_bytes=raw_bytes,
        )
    )


def test_authoritative_d_au_request_and_transport_are_exact_and_one_attempt() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AUTHORITATIVE_TIMEOUT_SECONDS,
        fetch_authoritative_d_au_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_AU_ACCEPT,
        AUTHORITATIVE_D_AU_URL,
        authoritative_d_au_request,
    )

    item = authoritative_d_au_request()
    transport = _authoritative_d_au_transport(_authoritative_d_au_raw())

    assert item == request("AUD")
    assert AUTHORITATIVE_D_AU_URL == (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.AU"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    assert AUTHORITATIVE_D_AU_ACCEPT == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert fetch_authoritative_d_au_response(item, transport).raw_bytes == (
        _authoritative_d_au_raw()
    )
    assert transport.calls == [
        (
            item,
            AUTHORITATIVE_D_AU_URL,
            AUTHORITATIVE_D_AU_ACCEPT,
            AUTHORITATIVE_TIMEOUT_SECONDS,
            AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    ]


@pytest.mark.parametrize(
    ("status_code", "final_url", "reason"),
    (
        (302, None, "redirect_rejected"),
        (200, "https://stats.bis.org/api/v2/other", "redirect_rejected"),
        (503, None, "http_status_not_success"),
    ),
)
def test_authoritative_d_au_transport_fails_once_without_retry_or_fallback(
    status_code: int, final_url: str | None, reason: str
) -> None:
    from scripts.ingest_bis_policy_rates import (
        AuthoritativeBisHttpResponse,
        fetch_authoritative_d_au_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_AU_URL,
        authoritative_d_au_request,
    )

    item = authoritative_d_au_request()
    transport = FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=status_code,
            final_url=final_url or AUTHORITATIVE_D_AU_URL,
            media_type="application/xml",
            headers={},
            raw_bytes=_authoritative_d_au_raw(),
        )
    )
    with pytest.raises(PolicyRateQualificationError, match=reason):
        fetch_authoritative_d_au_response(item, transport)
    assert len(transport.calls) == 1


def test_authoritative_d_au_publication_is_atomic_and_binds_sparse_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_AU_URL,
        authoritative_d_au_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_au_request()
    raw = _authoritative_d_au_raw()
    transport = _authoritative_d_au_transport(raw)

    published = ingestion.acquire_and_publish_authoritative_d_au(
        item, transport, RETRIEVED
    )

    destination = root / f"d_au-{item.fingerprint}"
    observations = parse_authoritative_bis_sdmx(raw, item)
    manifest = published.manifest
    assert len(transport.calls) == 1
    assert published.destination == destination
    assert published.raw_path == destination / "response.xml"
    assert published.manifest_path == destination / "manifest.json"
    assert published.raw_path.read_bytes() == raw
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["dataset_id"] == manifest.dataset_id
    assert persisted["manifest_id"] == manifest.manifest_id
    assert manifest.request_fingerprint == item.fingerprint
    assert manifest.exact_url == AUTHORITATIVE_D_AU_URL
    assert manifest.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert (manifest.series_key, manifest.frequency, manifest.reference_area) == (
        "D.AU",
        "D",
        "AU",
    )
    assert (manifest.unit_measure, manifest.unit_mult) == ("368", "0")
    assert manifest.status_semantics == (
        "A=normal",
        "M=missing_value_data_cannot_exist",
    )
    assert manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.canonical_observation_hash == canonical_sha256(
        observations.observations
    )
    assert manifest.raw_row_count == 3
    assert manifest.numeric_observation_count == len(observations) == 3
    assert manifest.min_observation_date == date(2014, 1, 3)
    assert manifest.max_observation_date == date(2023, 12, 29)


def test_authoritative_d_au_m_nan_publishes_raw_without_numeric_missing_row(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_au_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_au_request()
    raw = authoritative_sparse_xml(
        reference_area="AU",
        observations=(
            ("2014-01-03", "2.50", "A"),
            ("2014-01-07", "NaN", "M"),
        ),
    )

    published = ingestion.acquire_and_publish_authoritative_d_au(
        item, _authoritative_d_au_transport(raw), RETRIEVED
    )

    assert published.raw_path.read_bytes() == raw
    assert published.manifest.raw_row_count == 2
    assert published.manifest.numeric_observation_count == 1


def test_authoritative_d_au_existing_destination_precedes_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_au_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_au_request()
    (root / f"d_au-{item.fingerprint}").mkdir(parents=True)
    transport = _authoritative_d_au_transport(_authoritative_d_au_raw())

    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        ingestion.acquire_and_publish_authoritative_d_au(item, transport, RETRIEVED)
    assert transport.calls == []


def test_authoritative_d_au_cli_exact_authorized_target_invokes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_au_request

    transport = object()
    calls: list[tuple[object, object, datetime]] = []
    destination = tmp_path / "d_au-fixed"
    publication = SimpleNamespace(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=SimpleNamespace(dataset_id="a" * 64, manifest_id="b" * 64),
    )
    monkeypatch.setattr(ingestion, "UrllibAuthoritativeBisTransport", lambda: transport)

    def fake_acquire(request, supplied_transport, retrieved_at):
        calls.append((request, supplied_transport, retrieved_at))
        return publication

    monkeypatch.setattr(
        ingestion, "acquire_and_publish_authoritative_d_au", fake_acquire
    )

    ingestion.main(["--authorize-network-acquisition", "--target", "d_au"])

    assert len(calls) == 1
    item, supplied_transport, retrieved_at = calls[0]
    assert item == authoritative_d_au_request()
    assert supplied_transport is transport
    assert retrieved_at.tzinfo is UTC
    assert capsys.readouterr().out.splitlines() == [
        f"destination={publication.destination}",
        f"raw_path={publication.raw_path}",
        f"manifest_path={publication.manifest_path}",
        f"dataset_id={publication.manifest.dataset_id}",
        f"manifest_id={publication.manifest.manifest_id}",
    ]


def _authoritative_d_ca_raw() -> bytes:
    return authoritative_sparse_xml(
        reference_area="CA",
        observations=(
            ("2014-01-03", "1.00", "A"),
            ("2019-10-30", "1.75", "A"),
            ("2023-12-29", "5.00", "A"),
        ),
    )


def _authoritative_d_ca_transport(raw_bytes: bytes) -> FakeAuthoritativeTransport:
    from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

    from fxlab.data.policy_rates import AUTHORITATIVE_D_CA_URL

    return FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=AUTHORITATIVE_D_CA_URL,
            media_type="application/xml",
            headers={"Content-Type": "application/xml", "ETag": "synthetic-ca"},
            raw_bytes=raw_bytes,
        )
    )


def test_authoritative_d_ca_request_and_transport_are_exact_and_one_attempt() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AUTHORITATIVE_TIMEOUT_SECONDS,
        fetch_authoritative_d_ca_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_CA_ACCEPT,
        AUTHORITATIVE_D_CA_URL,
        authoritative_d_ca_request,
    )

    item = authoritative_d_ca_request()
    transport = _authoritative_d_ca_transport(_authoritative_d_ca_raw())

    assert item == request("CAD")
    assert AUTHORITATIVE_D_CA_URL == (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.CA"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    assert AUTHORITATIVE_D_CA_ACCEPT == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert fetch_authoritative_d_ca_response(item, transport).raw_bytes == (
        _authoritative_d_ca_raw()
    )
    assert transport.calls == [
        (
            item,
            AUTHORITATIVE_D_CA_URL,
            AUTHORITATIVE_D_CA_ACCEPT,
            AUTHORITATIVE_TIMEOUT_SECONDS,
            AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    ]


def test_authoritative_d_ca_publication_binds_exact_sparse_observations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_CA_URL,
        authoritative_d_ca_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_ca_request()
    raw = _authoritative_d_ca_raw()
    transport = _authoritative_d_ca_transport(raw)

    published = ingestion.acquire_and_publish_authoritative_d_ca(
        item, transport, RETRIEVED
    )

    destination = root / f"d_ca-{item.fingerprint}"
    observations = parse_authoritative_bis_sdmx(raw, item)
    manifest = published.manifest
    assert len(transport.calls) == 1
    assert published.destination == destination
    assert published.raw_path.read_bytes() == raw
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["dataset_id"] == manifest.dataset_id
    assert persisted["manifest_id"] == manifest.manifest_id
    assert manifest.request_fingerprint == item.fingerprint
    assert manifest.exact_url == AUTHORITATIVE_D_CA_URL
    assert manifest.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert (manifest.series_key, manifest.frequency, manifest.reference_area) == (
        "D.CA",
        "D",
        "CA",
    )
    assert (manifest.unit_measure, manifest.unit_mult) == ("368", "0")
    assert manifest.status_semantics == (
        "A=normal",
        "M=missing_value_data_cannot_exist",
    )
    assert manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.canonical_observation_hash == canonical_sha256(
        observations.observations
    )
    assert manifest.raw_row_count == 3
    assert manifest.numeric_observation_count == len(observations) == 3
    assert manifest.min_observation_date == date(2014, 1, 3)
    assert manifest.max_observation_date == date(2023, 12, 29)


def test_authoritative_d_ca_m_nan_is_preserved_raw_and_excluded_from_numeric_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        authoritative_d_ca_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_ca_request()
    raw = authoritative_sparse_xml(
        reference_area="CA",
        observations=(
            ("2014-01-03", "1.00", "A"),
            ("2014-01-04", "NaN", "M"),
            ("2023-12-29", "5.00", "A"),
        ),
    )

    published = ingestion.acquire_and_publish_authoritative_d_ca(
        item, _authoritative_d_ca_transport(raw), RETRIEVED
    )
    parsed = parse_authoritative_bis_sdmx(raw, item)

    assert published.raw_path.read_bytes() == raw
    assert published.manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert published.manifest.raw_row_count == 3
    assert published.manifest.numeric_observation_count == 2
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["raw_row_count"] == 3
    assert persisted["numeric_observation_count"] == 2
    assert "row_count" not in persisted
    assert published.manifest.canonical_observation_hash == canonical_sha256(
        parsed.observations
    )
    assert tuple(item.observation_date for item in parsed.observations) == (
        date(2014, 1, 3),
        date(2023, 12, 29),
    )


def test_authoritative_m_nan_raw_provenance_changes_semantic_identity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_ca_request

    item = authoritative_d_ca_request()
    without_missing = authoritative_sparse_xml(
        reference_area="CA",
        observations=(
            ("2014-01-03", "1.00", "A"),
            ("2023-12-29", "5.00", "A"),
        ),
    )
    with_missing = authoritative_sparse_xml(
        reference_area="CA",
        observations=(
            ("2014-01-03", "1.00", "A"),
            ("2019-10-30", "NaN", "M"),
            ("2023-12-29", "5.00", "A"),
        ),
    )
    manifests = []
    for index, raw in enumerate((without_missing, with_missing)):
        monkeypatch.setattr(
            ingestion, "AUTHORITATIVE_BIS_ROOT", tmp_path / f"authoritative-{index}"
        )
        manifests.append(
            ingestion.acquire_and_publish_authoritative_d_ca(
                item, _authoritative_d_ca_transport(raw), RETRIEVED
            ).manifest
        )

    assert manifests[0].canonical_observation_hash == (
        manifests[1].canonical_observation_hash
    )
    assert manifests[0].numeric_observation_count == (
        manifests[1].numeric_observation_count
    ) == 2
    assert (manifests[0].raw_row_count, manifests[1].raw_row_count) == (2, 3)
    assert manifests[0].raw_sha256 != manifests[1].raw_sha256
    assert manifests[0].dataset_id != manifests[1].dataset_id


def test_authoritative_d_ca_existing_destination_precedes_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_ca_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_ca_request()
    (root / f"d_ca-{item.fingerprint}").mkdir(parents=True)
    transport = _authoritative_d_ca_transport(_authoritative_d_ca_raw())

    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        ingestion.acquire_and_publish_authoritative_d_ca(item, transport, RETRIEVED)
    assert transport.calls == []


def test_authoritative_d_ca_cli_exact_authorized_target_invokes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_ca_request

    transport = object()
    calls: list[tuple[object, object, datetime]] = []
    destination = tmp_path / "d_ca-fixed"
    publication = SimpleNamespace(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=SimpleNamespace(dataset_id="c" * 64, manifest_id="d" * 64),
    )
    monkeypatch.setattr(ingestion, "UrllibAuthoritativeBisTransport", lambda: transport)

    def fake_acquire(request, supplied_transport, retrieved_at):
        calls.append((request, supplied_transport, retrieved_at))
        return publication

    monkeypatch.setattr(
        ingestion, "acquire_and_publish_authoritative_d_ca", fake_acquire
    )

    ingestion.main(["--authorize-network-acquisition", "--target", "d_ca"])

    assert calls == [(authoritative_d_ca_request(), transport, calls[0][2])]
    assert calls[0][2].tzinfo is UTC
    assert capsys.readouterr().out.splitlines() == [
        f"destination={publication.destination}",
        f"raw_path={publication.raw_path}",
        f"manifest_path={publication.manifest_path}",
        f"dataset_id={publication.manifest.dataset_id}",
        f"manifest_id={publication.manifest.manifest_id}",
    ]


def _authoritative_d_ch_raw() -> bytes:
    return authoritative_sparse_xml(
        reference_area="CH",
        observations=(
            ("2014-01-03", "0.00", "A"),
            ("2019-06-14", "-0.75", "A"),
            ("2023-12-29", "1.75", "A"),
        ),
    )


def _authoritative_d_ch_transport(raw_bytes: bytes) -> FakeAuthoritativeTransport:
    from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

    from fxlab.data.policy_rates import AUTHORITATIVE_D_CH_URL

    return FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=AUTHORITATIVE_D_CH_URL,
            media_type="application/xml",
            headers={"Content-Type": "application/xml", "ETag": "synthetic-ch"},
            raw_bytes=raw_bytes,
        )
    )


def test_authoritative_d_ch_request_and_transport_are_exact_and_one_attempt() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AUTHORITATIVE_TIMEOUT_SECONDS,
        fetch_authoritative_d_ch_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_CH_ACCEPT,
        AUTHORITATIVE_D_CH_URL,
        authoritative_d_ch_request,
    )

    item = authoritative_d_ch_request()
    transport = _authoritative_d_ch_transport(_authoritative_d_ch_raw())

    assert item == request("CHF")
    assert AUTHORITATIVE_D_CH_URL == (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.CH"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    assert AUTHORITATIVE_D_CH_ACCEPT == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert fetch_authoritative_d_ch_response(item, transport).raw_bytes == (
        _authoritative_d_ch_raw()
    )
    assert transport.calls == [
        (
            item,
            AUTHORITATIVE_D_CH_URL,
            AUTHORITATIVE_D_CH_ACCEPT,
            AUTHORITATIVE_TIMEOUT_SECONDS,
            AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    ]


def test_authoritative_d_ch_publication_binds_exact_sparse_observations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_CH_URL,
        authoritative_d_ch_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_ch_request()
    raw = _authoritative_d_ch_raw()
    transport = _authoritative_d_ch_transport(raw)

    published = ingestion.acquire_and_publish_authoritative_d_ch(
        item, transport, RETRIEVED
    )

    destination = root / f"d_ch-{item.fingerprint}"
    observations = parse_authoritative_bis_sdmx(raw, item)
    manifest = published.manifest
    assert len(transport.calls) == 1
    assert published.destination == destination
    assert published.raw_path.read_bytes() == raw
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["dataset_id"] == manifest.dataset_id
    assert persisted["manifest_id"] == manifest.manifest_id
    assert manifest.request_fingerprint == item.fingerprint
    assert manifest.exact_url == AUTHORITATIVE_D_CH_URL
    assert manifest.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert (manifest.series_key, manifest.frequency, manifest.reference_area) == (
        "D.CH",
        "D",
        "CH",
    )
    assert (manifest.unit_measure, manifest.unit_mult) == ("368", "0")
    assert manifest.status_semantics == (
        "A=normal",
        "M=missing_value_data_cannot_exist",
    )
    assert manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.canonical_observation_hash == canonical_sha256(
        observations.observations
    )
    assert manifest.raw_row_count == 3
    assert manifest.numeric_observation_count == len(observations) == 3
    assert manifest.min_observation_date == date(2014, 1, 3)
    assert manifest.max_observation_date == date(2023, 12, 29)


def test_authoritative_d_ch_existing_destination_precedes_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_ch_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_ch_request()
    (root / f"d_ch-{item.fingerprint}").mkdir(parents=True)
    transport = _authoritative_d_ch_transport(_authoritative_d_ch_raw())

    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        ingestion.acquire_and_publish_authoritative_d_ch(item, transport, RETRIEVED)
    assert transport.calls == []


def test_authoritative_d_ch_cli_exact_authorized_target_invokes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_ch_request

    transport = object()
    calls: list[tuple[object, object, datetime]] = []
    destination = tmp_path / "d_ch-fixed"
    publication = SimpleNamespace(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=SimpleNamespace(dataset_id="e" * 64, manifest_id="f" * 64),
    )
    monkeypatch.setattr(ingestion, "UrllibAuthoritativeBisTransport", lambda: transport)

    def fake_acquire(request, supplied_transport, retrieved_at):
        calls.append((request, supplied_transport, retrieved_at))
        return publication

    monkeypatch.setattr(
        ingestion, "acquire_and_publish_authoritative_d_ch", fake_acquire
    )

    ingestion.main(["--authorize-network-acquisition", "--target", "d_ch"])

    assert calls == [(authoritative_d_ch_request(), transport, calls[0][2])]
    assert calls[0][2].tzinfo is UTC
    assert capsys.readouterr().out.splitlines() == [
        f"destination={publication.destination}",
        f"raw_path={publication.raw_path}",
        f"manifest_path={publication.manifest_path}",
        f"dataset_id={publication.manifest.dataset_id}",
        f"manifest_id={publication.manifest.manifest_id}",
    ]


def _authoritative_d_xm_raw() -> bytes:
    return authoritative_sparse_xml(
        reference_area="XM",
        observations=(
            ("2014-01-02", "0.25", "A"),
            ("2019-09-18", "0.00", "A"),
            ("2023-12-29", "4.50", "A"),
        ),
    )


def _authoritative_d_xm_transport(raw_bytes: bytes) -> FakeAuthoritativeTransport:
    from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

    from fxlab.data.policy_rates import AUTHORITATIVE_D_XM_URL

    return FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=AUTHORITATIVE_D_XM_URL,
            media_type="application/xml",
            headers={"Content-Type": "application/xml", "ETag": "synthetic-xm"},
            raw_bytes=raw_bytes,
        )
    )


def test_authoritative_d_xm_request_and_transport_are_exact_and_one_attempt() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AUTHORITATIVE_TIMEOUT_SECONDS,
        fetch_authoritative_d_xm_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_XM_ACCEPT,
        AUTHORITATIVE_D_XM_URL,
        authoritative_d_xm_request,
    )

    item = authoritative_d_xm_request()
    transport = _authoritative_d_xm_transport(_authoritative_d_xm_raw())

    assert item == request("EUR")
    assert AUTHORITATIVE_D_XM_URL == (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.XM"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    assert AUTHORITATIVE_D_XM_ACCEPT == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert fetch_authoritative_d_xm_response(item, transport).raw_bytes == (
        _authoritative_d_xm_raw()
    )
    assert transport.calls == [
        (
            item,
            AUTHORITATIVE_D_XM_URL,
            AUTHORITATIVE_D_XM_ACCEPT,
            AUTHORITATIVE_TIMEOUT_SECONDS,
            AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    ]


def test_authoritative_d_xm_publication_binds_exact_sparse_observations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_XM_URL,
        authoritative_d_xm_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_xm_request()
    raw = _authoritative_d_xm_raw()
    transport = _authoritative_d_xm_transport(raw)

    published = ingestion.acquire_and_publish_authoritative_d_xm(
        item, transport, RETRIEVED
    )

    destination = root / f"d_xm-{item.fingerprint}"
    observations = parse_authoritative_bis_sdmx(raw, item)
    manifest = published.manifest
    assert len(transport.calls) == 1
    assert published.destination == destination
    assert published.raw_path.read_bytes() == raw
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["dataset_id"] == manifest.dataset_id
    assert persisted["manifest_id"] == manifest.manifest_id
    assert manifest.request_fingerprint == item.fingerprint
    assert manifest.exact_url == AUTHORITATIVE_D_XM_URL
    assert manifest.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert (manifest.series_key, manifest.frequency, manifest.reference_area) == (
        "D.XM",
        "D",
        "XM",
    )
    assert (manifest.unit_measure, manifest.unit_mult) == ("368", "0")
    assert manifest.status_semantics == (
        "A=normal",
        "M=missing_value_data_cannot_exist",
    )
    assert manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.canonical_observation_hash == canonical_sha256(
        observations.observations
    )
    assert manifest.raw_row_count == 3
    assert manifest.numeric_observation_count == len(observations) == 3
    assert manifest.min_observation_date == date(2014, 1, 2)
    assert manifest.max_observation_date == date(2023, 12, 29)


def test_authoritative_d_xm_existing_destination_precedes_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_xm_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_xm_request()
    (root / f"d_xm-{item.fingerprint}").mkdir(parents=True)
    transport = _authoritative_d_xm_transport(_authoritative_d_xm_raw())

    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        ingestion.acquire_and_publish_authoritative_d_xm(item, transport, RETRIEVED)
    assert transport.calls == []


def test_authoritative_d_xm_cli_exact_authorized_target_invokes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_xm_request

    transport = object()
    calls: list[tuple[object, object, datetime]] = []
    destination = tmp_path / "d_xm-fixed"
    publication = SimpleNamespace(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=SimpleNamespace(dataset_id="1" * 64, manifest_id="2" * 64),
    )
    monkeypatch.setattr(ingestion, "UrllibAuthoritativeBisTransport", lambda: transport)

    def fake_acquire(request, supplied_transport, retrieved_at):
        calls.append((request, supplied_transport, retrieved_at))
        return publication

    monkeypatch.setattr(
        ingestion, "acquire_and_publish_authoritative_d_xm", fake_acquire
    )

    ingestion.main(["--authorize-network-acquisition", "--target", "d_xm"])

    assert calls == [(authoritative_d_xm_request(), transport, calls[0][2])]
    assert calls[0][2].tzinfo is UTC
    assert capsys.readouterr().out.splitlines() == [
        f"destination={publication.destination}",
        f"raw_path={publication.raw_path}",
        f"manifest_path={publication.manifest_path}",
        f"dataset_id={publication.manifest.dataset_id}",
        f"manifest_id={publication.manifest.manifest_id}",
    ]


def _authoritative_d_gb_raw() -> bytes:
    return authoritative_sparse_xml(
        reference_area="GB",
        observations=(
            ("2014-01-03", "0.50", "A"),
            ("2019-08-01", "0.75", "A"),
            ("2023-12-29", "5.25", "A"),
        ),
    )


def _authoritative_d_gb_transport(raw_bytes: bytes) -> FakeAuthoritativeTransport:
    from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

    from fxlab.data.policy_rates import AUTHORITATIVE_D_GB_URL

    return FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=AUTHORITATIVE_D_GB_URL,
            media_type="application/xml",
            headers={"Content-Type": "application/xml", "ETag": "synthetic-gb"},
            raw_bytes=raw_bytes,
        )
    )


def test_authoritative_d_gb_request_and_transport_are_exact_and_one_attempt() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AUTHORITATIVE_TIMEOUT_SECONDS,
        fetch_authoritative_d_gb_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_GB_ACCEPT,
        AUTHORITATIVE_D_GB_URL,
        authoritative_d_gb_request,
    )

    item = authoritative_d_gb_request()
    transport = _authoritative_d_gb_transport(_authoritative_d_gb_raw())

    assert item == request("GBP")
    assert AUTHORITATIVE_D_GB_URL == (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.GB"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    assert AUTHORITATIVE_D_GB_ACCEPT == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert fetch_authoritative_d_gb_response(item, transport).raw_bytes == (
        _authoritative_d_gb_raw()
    )
    assert transport.calls == [
        (
            item,
            AUTHORITATIVE_D_GB_URL,
            AUTHORITATIVE_D_GB_ACCEPT,
            AUTHORITATIVE_TIMEOUT_SECONDS,
            AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    ]


def test_authoritative_d_gb_publication_binds_exact_sparse_observations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_GB_URL,
        authoritative_d_gb_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_gb_request()
    raw = _authoritative_d_gb_raw()
    transport = _authoritative_d_gb_transport(raw)

    published = ingestion.acquire_and_publish_authoritative_d_gb(
        item, transport, RETRIEVED
    )

    destination = root / f"d_gb-{item.fingerprint}"
    observations = parse_authoritative_bis_sdmx(raw, item)
    manifest = published.manifest
    assert len(transport.calls) == 1
    assert published.destination == destination
    assert published.raw_path == destination / "response.xml"
    assert published.manifest_path == destination / "manifest.json"
    assert published.raw_path.read_bytes() == raw
    assert [path for path in root.iterdir() if path != destination] == []
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["dataset_id"] == manifest.dataset_id
    assert persisted["manifest_id"] == manifest.manifest_id
    assert manifest.request_fingerprint == item.fingerprint
    assert manifest.exact_url == AUTHORITATIVE_D_GB_URL
    assert manifest.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert (manifest.series_key, manifest.frequency, manifest.reference_area) == (
        "D.GB",
        "D",
        "GB",
    )
    assert (manifest.unit_measure, manifest.unit_mult) == ("368", "0")
    assert manifest.status_semantics == (
        "A=normal",
        "M=missing_value_data_cannot_exist",
    )
    assert manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.canonical_observation_hash == canonical_sha256(
        observations.observations
    )
    assert manifest.raw_row_count == 3
    assert manifest.numeric_observation_count == len(observations) == 3
    assert manifest.min_observation_date == date(2014, 1, 3)
    assert manifest.max_observation_date == date(2023, 12, 29)


def test_authoritative_d_gb_m_nan_is_preserved_raw_and_excluded_from_numeric_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        authoritative_d_gb_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_gb_request()
    raw = authoritative_sparse_xml(
        reference_area="GB",
        observations=(
            ("2014-01-03", "0.50", "A"),
            ("2014-01-06", "NaN", "M"),
            ("2023-12-29", "5.25", "A"),
        ),
    )

    published = ingestion.acquire_and_publish_authoritative_d_gb(
        item, _authoritative_d_gb_transport(raw), RETRIEVED
    )
    parsed = parse_authoritative_bis_sdmx(raw, item)

    assert published.raw_path.read_bytes() == raw
    assert published.manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert published.manifest.raw_row_count == 3
    assert published.manifest.numeric_observation_count == 2
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["raw_row_count"] == 3
    assert persisted["numeric_observation_count"] == 2
    assert "row_count" not in persisted
    assert published.manifest.canonical_observation_hash == canonical_sha256(
        parsed.observations
    )
    assert tuple(item.observation_date for item in parsed.observations) == (
        date(2014, 1, 3),
        date(2023, 12, 29),
    )
    assert all(item.status == "A" for item in parsed.observations)


def test_authoritative_d_gb_existing_destination_precedes_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_gb_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_gb_request()
    (root / f"d_gb-{item.fingerprint}").mkdir(parents=True)
    transport = _authoritative_d_gb_transport(_authoritative_d_gb_raw())

    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        ingestion.acquire_and_publish_authoritative_d_gb(item, transport, RETRIEVED)
    assert transport.calls == []


def test_authoritative_d_gb_cli_exact_authorized_target_invokes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_gb_request

    transport = object()
    calls: list[tuple[object, object, datetime]] = []
    destination = tmp_path / "d_gb-fixed"
    publication = SimpleNamespace(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=SimpleNamespace(dataset_id="5" * 64, manifest_id="6" * 64),
    )
    monkeypatch.setattr(ingestion, "UrllibAuthoritativeBisTransport", lambda: transport)

    def fake_acquire(request, supplied_transport, retrieved_at):
        calls.append((request, supplied_transport, retrieved_at))
        return publication

    monkeypatch.setattr(
        ingestion, "acquire_and_publish_authoritative_d_gb", fake_acquire
    )

    ingestion.main(["--authorize-network-acquisition", "--target", "d_gb"])

    assert calls == [(authoritative_d_gb_request(), transport, calls[0][2])]
    assert calls[0][2].tzinfo is UTC
    assert capsys.readouterr().out.splitlines() == [
        f"destination={publication.destination}",
        f"raw_path={publication.raw_path}",
        f"manifest_path={publication.manifest_path}",
        f"dataset_id={publication.manifest.dataset_id}",
        f"manifest_id={publication.manifest.manifest_id}",
    ]


def _authoritative_d_nz_raw() -> bytes:
    return authoritative_sparse_xml(
        reference_area="NZ",
        observations=(
            ("2014-01-03", "1.00", "A"),
            ("2019-08-01", "1.50", "A"),
            ("2023-12-29", "5.50", "A"),
        ),
    )


def _authoritative_d_nz_transport(raw_bytes: bytes) -> FakeAuthoritativeTransport:
    from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

    from fxlab.data.policy_rates import AUTHORITATIVE_D_NZ_URL

    return FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=AUTHORITATIVE_D_NZ_URL,
            media_type="application/xml",
            headers={"Content-Type": "application/xml", "ETag": "synthetic-nz"},
            raw_bytes=raw_bytes,
        )
    )


def test_authoritative_d_nz_request_and_transport_are_exact_and_one_attempt() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AUTHORITATIVE_TIMEOUT_SECONDS,
        fetch_authoritative_d_nz_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_NZ_ACCEPT,
        AUTHORITATIVE_D_NZ_URL,
        authoritative_d_nz_request,
    )

    item = authoritative_d_nz_request()
    transport = _authoritative_d_nz_transport(_authoritative_d_nz_raw())

    assert item == request("NZD")
    assert AUTHORITATIVE_D_NZ_URL == (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.NZ"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    assert AUTHORITATIVE_D_NZ_ACCEPT == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert fetch_authoritative_d_nz_response(item, transport).raw_bytes == (
        _authoritative_d_nz_raw()
    )
    assert transport.calls == [
        (
            item,
            AUTHORITATIVE_D_NZ_URL,
            AUTHORITATIVE_D_NZ_ACCEPT,
            AUTHORITATIVE_TIMEOUT_SECONDS,
            AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    ]


def test_authoritative_d_nz_publication_binds_exact_sparse_observations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_NZ_URL,
        authoritative_d_nz_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_nz_request()
    raw = _authoritative_d_nz_raw()
    transport = _authoritative_d_nz_transport(raw)

    published = ingestion.acquire_and_publish_authoritative_d_nz(
        item, transport, RETRIEVED
    )

    destination = root / f"d_nz-{item.fingerprint}"
    observations = parse_authoritative_bis_sdmx(raw, item)
    manifest = published.manifest
    assert len(transport.calls) == 1
    assert published.destination == destination
    assert published.raw_path == destination / "response.xml"
    assert published.manifest_path == destination / "manifest.json"
    assert published.raw_path.read_bytes() == raw
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["dataset_id"] == manifest.dataset_id
    assert persisted["manifest_id"] == manifest.manifest_id
    assert manifest.request_fingerprint == item.fingerprint
    assert manifest.exact_url == AUTHORITATIVE_D_NZ_URL
    assert manifest.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert (manifest.series_key, manifest.frequency, manifest.reference_area) == (
        "D.NZ",
        "D",
        "NZ",
    )
    assert (manifest.unit_measure, manifest.unit_mult) == ("368", "0")
    assert manifest.status_semantics == (
        "A=normal",
        "M=missing_value_data_cannot_exist",
    )
    assert manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.canonical_observation_hash == canonical_sha256(
        observations.observations
    )
    assert manifest.raw_row_count == 3
    assert manifest.numeric_observation_count == len(observations) == 3
    assert manifest.min_observation_date == date(2014, 1, 3)
    assert manifest.max_observation_date == date(2023, 12, 29)


def test_authoritative_d_nz_m_nan_is_preserved_raw_and_excluded_from_numeric_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        authoritative_d_nz_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_nz_request()
    raw = authoritative_sparse_xml(
        reference_area="NZ",
        observations=(
            ("2014-01-03", "1.00", "A"),
            ("2014-01-06", "NaN", "M"),
            ("2023-12-29", "5.50", "A"),
        ),
    )

    published = ingestion.acquire_and_publish_authoritative_d_nz(
        item, _authoritative_d_nz_transport(raw), RETRIEVED
    )
    parsed = parse_authoritative_bis_sdmx(raw, item)

    assert published.raw_path.read_bytes() == raw
    assert published.manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert published.manifest.raw_row_count == 3
    assert published.manifest.numeric_observation_count == 2
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["raw_row_count"] == 3
    assert persisted["numeric_observation_count"] == 2
    assert "row_count" not in persisted
    assert published.manifest.canonical_observation_hash == canonical_sha256(
        parsed.observations
    )
    assert tuple(item.observation_date for item in parsed.observations) == (
        date(2014, 1, 3),
        date(2023, 12, 29),
    )
    assert all(item.status == "A" for item in parsed.observations)


def test_authoritative_d_nz_existing_destination_precedes_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_nz_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_nz_request()
    (root / f"d_nz-{item.fingerprint}").mkdir(parents=True)
    transport = _authoritative_d_nz_transport(_authoritative_d_nz_raw())

    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        ingestion.acquire_and_publish_authoritative_d_nz(item, transport, RETRIEVED)
    assert transport.calls == []


def test_authoritative_d_nz_cli_exact_authorized_target_invokes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_nz_request

    transport = object()
    calls: list[tuple[object, object, datetime]] = []
    destination = tmp_path / "d_nz-fixed"
    publication = SimpleNamespace(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=SimpleNamespace(dataset_id="7" * 64, manifest_id="8" * 64),
    )
    monkeypatch.setattr(ingestion, "UrllibAuthoritativeBisTransport", lambda: transport)

    def fake_acquire(request, supplied_transport, retrieved_at):
        calls.append((request, supplied_transport, retrieved_at))
        return publication

    monkeypatch.setattr(
        ingestion, "acquire_and_publish_authoritative_d_nz", fake_acquire
    )

    ingestion.main(["--authorize-network-acquisition", "--target", "d_nz"])

    assert calls == [(authoritative_d_nz_request(), transport, calls[0][2])]
    assert calls[0][2].tzinfo is UTC
    assert capsys.readouterr().out.splitlines() == [
        f"destination={publication.destination}",
        f"raw_path={publication.raw_path}",
        f"manifest_path={publication.manifest_path}",
        f"dataset_id={publication.manifest.dataset_id}",
        f"manifest_id={publication.manifest.manifest_id}",
    ]


def _authoritative_d_jp_raw() -> bytes:
    return authoritative_sparse_xml(
        reference_area="JP",
        observations=(
            ("2014-01-06", "0.10", "A"),
            ("2019-07-16", "-0.10", "A"),
            ("2023-12-29", "-0.10", "A"),
        ),
    )


def _authoritative_d_jp_transport(raw_bytes: bytes) -> FakeAuthoritativeTransport:
    from scripts.ingest_bis_policy_rates import AuthoritativeBisHttpResponse

    from fxlab.data.policy_rates import AUTHORITATIVE_D_JP_URL

    return FakeAuthoritativeTransport(
        AuthoritativeBisHttpResponse(
            status_code=200,
            final_url=AUTHORITATIVE_D_JP_URL,
            media_type="application/xml",
            headers={"Content-Type": "application/xml", "ETag": "synthetic-jp"},
            raw_bytes=raw_bytes,
        )
    )


def test_authoritative_d_jp_request_and_transport_are_exact_and_one_attempt() -> None:
    from scripts.ingest_bis_policy_rates import (
        AUTHORITATIVE_MAX_RESPONSE_BYTES,
        AUTHORITATIVE_TIMEOUT_SECONDS,
        fetch_authoritative_d_jp_response,
    )

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_JP_ACCEPT,
        AUTHORITATIVE_D_JP_URL,
        authoritative_d_jp_request,
    )

    item = authoritative_d_jp_request()
    transport = _authoritative_d_jp_transport(_authoritative_d_jp_raw())

    assert item == request("JPY")
    assert AUTHORITATIVE_D_JP_URL == (
        "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.JP"
        "?startPeriod=2014-01-01&endPeriod=2023-12-31"
    )
    assert AUTHORITATIVE_D_JP_ACCEPT == (
        "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    )
    assert fetch_authoritative_d_jp_response(item, transport).raw_bytes == (
        _authoritative_d_jp_raw()
    )
    assert transport.calls == [
        (
            item,
            AUTHORITATIVE_D_JP_URL,
            AUTHORITATIVE_D_JP_ACCEPT,
            AUTHORITATIVE_TIMEOUT_SECONDS,
            AUTHORITATIVE_MAX_RESPONSE_BYTES,
        )
    ]


def test_authoritative_d_jp_publication_binds_exact_sparse_observations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import (
        AUTHORITATIVE_D_JP_URL,
        authoritative_d_jp_request,
        canonical_sha256,
        parse_authoritative_bis_sdmx,
    )

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_jp_request()
    raw = _authoritative_d_jp_raw()
    transport = _authoritative_d_jp_transport(raw)

    published = ingestion.acquire_and_publish_authoritative_d_jp(
        item, transport, RETRIEVED
    )

    destination = root / f"d_jp-{item.fingerprint}"
    observations = parse_authoritative_bis_sdmx(raw, item)
    manifest = published.manifest
    assert len(transport.calls) == 1
    assert published.destination == destination
    assert published.raw_path.read_bytes() == raw
    assert [path for path in root.iterdir() if path != destination] == []
    persisted = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    assert persisted["dataset_id"] == manifest.dataset_id
    assert persisted["manifest_id"] == manifest.manifest_id
    assert manifest.request_fingerprint == item.fingerprint
    assert manifest.exact_url == AUTHORITATIVE_D_JP_URL
    assert manifest.representation_identity == "SDMX_ML_2_1_STRUCTURE_SPECIFIC_DATA"
    assert (manifest.series_key, manifest.frequency, manifest.reference_area) == (
        "D.JP",
        "D",
        "JP",
    )
    assert (manifest.unit_measure, manifest.unit_mult) == ("368", "0")
    assert manifest.status_semantics == (
        "A=normal",
        "M=missing_value_data_cannot_exist",
    )
    assert manifest.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.canonical_observation_hash == canonical_sha256(
        observations.observations
    )
    assert manifest.raw_row_count == 3
    assert manifest.numeric_observation_count == len(observations) == 3
    assert manifest.min_observation_date == date(2014, 1, 6)
    assert manifest.max_observation_date == date(2023, 12, 29)


def test_authoritative_d_jp_existing_destination_precedes_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_jp_request

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    item = authoritative_d_jp_request()
    (root / f"d_jp-{item.fingerprint}").mkdir(parents=True)
    transport = _authoritative_d_jp_transport(_authoritative_d_jp_raw())

    with pytest.raises(PolicyRateQualificationError, match="destination_exists"):
        ingestion.acquire_and_publish_authoritative_d_jp(item, transport, RETRIEVED)
    assert transport.calls == []


def test_authoritative_d_jp_cli_exact_authorized_target_invokes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    import scripts.ingest_bis_policy_rates as ingestion

    from fxlab.data.policy_rates import authoritative_d_jp_request

    transport = object()
    calls: list[tuple[object, object, datetime]] = []
    destination = tmp_path / "d_jp-fixed"
    publication = SimpleNamespace(
        destination=destination,
        raw_path=destination / "response.xml",
        manifest_path=destination / "manifest.json",
        manifest=SimpleNamespace(dataset_id="3" * 64, manifest_id="4" * 64),
    )
    monkeypatch.setattr(ingestion, "UrllibAuthoritativeBisTransport", lambda: transport)

    def fake_acquire(request, supplied_transport, retrieved_at):
        calls.append((request, supplied_transport, retrieved_at))
        return publication

    monkeypatch.setattr(
        ingestion, "acquire_and_publish_authoritative_d_jp", fake_acquire
    )

    ingestion.main(["--authorize-network-acquisition", "--target", "d_jp"])

    assert calls == [(authoritative_d_jp_request(), transport, calls[0][2])]
    assert calls[0][2].tzinfo is UTC
    assert capsys.readouterr().out.splitlines() == [
        f"destination={publication.destination}",
        f"raw_path={publication.raw_path}",
        f"manifest_path={publication.manifest_path}",
        f"dataset_id={publication.manifest.dataset_id}",
        f"manifest_id={publication.manifest.manifest_id}",
    ]


def _legacy_authoritative_semantic(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "format": 1,
        "request_fingerprint": manifest["request_fingerprint"],
        "exact_url": manifest["exact_url"],
        "representation_identity": manifest["representation_identity"],
        "series_key": manifest["series_key"],
        "frequency": manifest["frequency"],
        "reference_area": manifest["reference_area"],
        "unit_measure": manifest["unit_measure"],
        "unit_mult": manifest["unit_mult"],
        "status_semantics": ("A=normal",),
        "raw_sha256": manifest["raw_sha256"],
        "canonical_observation_hash": manifest["canonical_observation_hash"],
        "row_count": manifest["row_count"],
        "min_observation_date": manifest["min_observation_date"],
        "max_observation_date": manifest["max_observation_date"],
    }


def _create_legacy_authoritative_publication(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    series_key: str,
) -> tuple[Path, bytes, dict[str, object]]:
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    if series_key == "D.AU":
        from fxlab.data.policy_rates import authoritative_d_au_request

        request_item = authoritative_d_au_request()
        raw = _authoritative_d_au_raw()
        published = ingestion.acquire_and_publish_authoritative_d_au(
            request_item,
            _authoritative_d_au_transport(raw),
            RETRIEVED,
        )
    elif series_key == "D.US":
        from fxlab.data.policy_rates import authoritative_d_us_request

        request_item = authoritative_d_us_request()
        raw = authoritative_d_us_xml()
        published = ingestion.acquire_and_publish_authoritative_d_us(
            request_item,
            _authoritative_transport_with_raw(raw),
            RETRIEVED,
        )
    else:
        raise AssertionError("test helper only creates D.AU or D.US")

    current = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    legacy = dict(current)
    for v2_only in ("schema", "audit_contract", "returned_url", "response_headers"):
        legacy.pop(v2_only)
    raw_count = legacy.pop("raw_row_count")
    numeric_count = legacy.pop("numeric_observation_count")
    assert raw_count == numeric_count
    legacy["row_count"] = numeric_count
    legacy["status_semantics"] = ["A=normal"]
    legacy["dataset_id"] = canonical_sha256(_legacy_authoritative_semantic(legacy))
    legacy["manifest_id"] = canonical_sha256(
        {
            "legacy_dataset_id": legacy["dataset_id"],
            "synthetic_preserved_audit": True,
        }
    )
    published.manifest_path.write_text(canonical_json(legacy), encoding="utf-8")
    return published.destination, raw, legacy


def _rewrite_manifest(path: Path, changes: dict[str, object]) -> bytes:
    import json

    original = path.read_bytes()
    manifest = json.loads(original)
    manifest.update(changes)
    path.write_text(canonical_json(manifest), encoding="utf-8")
    return path.read_bytes()


def test_legacy_authoritative_manifest_migration_d_au_preserves_evidence(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    destination, raw, legacy = _create_legacy_authoritative_publication(
        tmp_path / "authoritative", monkeypatch, "D.AU"
    )
    raw_before = (destination / "response.xml").read_bytes()

    result = ingestion.migrate_legacy_authoritative_bis_manifest(destination)

    migrated = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert result.status == "migrated"
    assert result.manifest_path == destination / "manifest.json"
    assert (destination / "response.xml").read_bytes() == raw_before == raw
    assert migrated["raw_sha256"] == legacy["raw_sha256"]
    assert migrated["canonical_observation_hash"] == (
        legacy["canonical_observation_hash"]
    )
    assert migrated["retrieved_at"] == legacy["retrieved_at"]
    assert migrated["raw_row_count"] == 3
    assert migrated["numeric_observation_count"] == 3
    assert "row_count" not in migrated
    assert migrated["status_semantics"] == [
        "A=normal",
        "M=missing_value_data_cannot_exist",
    ]
    assert migrated["dataset_id"] != legacy["dataset_id"]
    assert migrated["manifest_id"] != legacy["manifest_id"]
    assert result.dataset_id == migrated["dataset_id"]
    assert result.manifest_id == migrated["manifest_id"]
    assert migrated["schema"] == "candidate_b_bis_authoritative.v2"
    assert migrated["audit_contract"] == "candidate_b_bis_migration_audit.v1"
    assert migrated["migration_contract"] == (
        ingestion.LEGACY_AUTHORITATIVE_MANIFEST_MIGRATION_CONTRACT
    )
    assert migrated["legacy_dataset_id"] == legacy["dataset_id"]
    assert migrated["legacy_manifest_id"] == legacy["manifest_id"]
    assert "returned_url" not in migrated
    assert "response_headers" not in migrated


def test_legacy_authoritative_manifest_migration_d_us_preserves_evidence(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    destination, raw, legacy = _create_legacy_authoritative_publication(
        tmp_path / "authoritative", monkeypatch, "D.US"
    )

    ingestion.migrate_legacy_authoritative_bis_manifest(destination)

    migrated = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert (destination / "response.xml").read_bytes() == raw
    assert migrated["raw_row_count"] == 3652
    assert migrated["numeric_observation_count"] == 3652
    assert migrated["retrieved_at"] == legacy["retrieved_at"]
    assert migrated["dataset_id"] != legacy["dataset_id"]
    assert migrated["manifest_id"] != legacy["manifest_id"]


@pytest.mark.parametrize(
    ("field_name", "replacement", "reason"),
    (
        ("raw_sha256", "0" * 64, "raw_sha256_mismatch"),
        ("byte_count", 1, "byte_count_mismatch"),
        ("canonical_observation_hash", "0" * 64, "observation_hash_mismatch"),
        ("request_fingerprint", "0" * 64, "request_fingerprint_mismatch"),
        ("dataset_id", "0" * 64, "legacy_dataset_id_mismatch"),
        ("series_key", "D.CA", "migration_scope_not_approved"),
    ),
)
def test_legacy_authoritative_manifest_migration_rejects_forged_bindings(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    replacement: object,
    reason: str,
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    destination, _, _ = _create_legacy_authoritative_publication(
        tmp_path / "authoritative", monkeypatch, "D.AU"
    )
    manifest_path = destination / "manifest.json"
    forged = _rewrite_manifest(manifest_path, {field_name: replacement})

    with pytest.raises(PolicyRateQualificationError, match=reason):
        ingestion.migrate_legacy_authoritative_bis_manifest(destination)

    assert manifest_path.read_bytes() == forged


def test_legacy_authoritative_manifest_migration_rejects_unsupported_status_value(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    import scripts.ingest_bis_policy_rates as ingestion

    destination, _, _ = _create_legacy_authoritative_publication(
        tmp_path / "authoritative", monkeypatch, "D.AU"
    )
    invalid_raw = authoritative_sparse_xml(
        reference_area="AU",
        observations=(
            ("2014-01-03", "2.50", "A"),
            ("2014-01-07", "2.50", "M"),
            ("2023-12-29", "4.35", "A"),
        ),
    )
    (destination / "response.xml").write_bytes(invalid_raw)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["raw_sha256"] = hashlib.sha256(invalid_raw).hexdigest()
    manifest["byte_count"] = len(invalid_raw)
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")

    with pytest.raises(
        PolicyRateQualificationError, match="observation_status_value_invalid"
    ):
        ingestion.migrate_legacy_authoritative_bis_manifest(destination)


def test_legacy_authoritative_manifest_migration_is_idempotently_fail_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    destination, _, _ = _create_legacy_authoritative_publication(
        tmp_path / "authoritative", monkeypatch, "D.AU"
    )
    ingestion.migrate_legacy_authoritative_bis_manifest(destination)
    manifest_path = destination / "manifest.json"
    migrated = manifest_path.read_bytes()

    with pytest.raises(PolicyRateQualificationError, match="manifest_already_current"):
        ingestion.migrate_legacy_authoritative_bis_manifest(destination)

    assert manifest_path.read_bytes() == migrated


def test_legacy_authoritative_manifest_migration_rejects_outside_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    root = tmp_path / "authoritative"
    destination, _, _ = _create_legacy_authoritative_publication(
        root, monkeypatch, "D.AU"
    )
    outside = tmp_path / "outside"
    destination.rename(outside)

    with pytest.raises(PolicyRateQualificationError, match="migration_path_not_approved"):
        ingestion.migrate_legacy_authoritative_bis_manifest(outside)


def test_legacy_authoritative_manifest_migration_rejects_missing_files(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    root = tmp_path / "authoritative"
    monkeypatch.setattr(ingestion, "AUTHORITATIVE_BIS_ROOT", root)
    destination = root / "d_au-missing"
    destination.mkdir(parents=True)

    with pytest.raises(PolicyRateQualificationError, match="migration_evidence_missing"):
        ingestion.migrate_legacy_authoritative_bis_manifest(destination)


def test_legacy_authoritative_manifest_migration_atomic_failure_preserves_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.ingest_bis_policy_rates as ingestion

    destination, raw, _ = _create_legacy_authoritative_publication(
        tmp_path / "authoritative", monkeypatch, "D.AU"
    )
    manifest_path = destination / "manifest.json"
    original_manifest = manifest_path.read_bytes()

    def fail_replace(source, target):
        raise OSError("synthetic atomic replacement failure")

    monkeypatch.setattr(ingestion.os, "replace", fail_replace)

    with pytest.raises(OSError, match="synthetic atomic replacement failure"):
        ingestion.migrate_legacy_authoritative_bis_manifest(destination)

    assert manifest_path.read_bytes() == original_manifest
    assert (destination / "response.xml").read_bytes() == raw
    assert not tuple(destination.glob(".manifest-migration-*"))


def spec(currency: str = "AUD") -> PolicyRateSeriesSpec:
    return PolicyRateSeriesSpec(currency, APPROVED_BIS_SERIES[currency])


def request(currency: str = "AUD") -> PolicyRateRequest:
    return PolicyRateRequest(spec(currency), APPROVED_REQUEST_START, APPROVED_REQUEST_END)


def metadata(currency: str = "AUD", **changes: object) -> PolicyRateMetadata:
    values: dict[str, object] = {
        "agency": "BIS",
        "dataflow": "WS_CBPOL",
        "version": "1.0",
        "frequency": "D",
        "series_key": APPROVED_BIS_SERIES[currency],
        "currency": currency,
        "reference_area": APPROVED_BIS_SERIES[currency].split(".")[1],
        "unit": "percent_per_annum",
        "scale": 1,
        "observation_status_semantics": ("A=normal",),
        "dsd_identity": "bis_cbpol_dsd_v1",
        "codelist_identity": "bis_cbpol_codelist_v1",
        "instrument_metadata": "principal_policy_rate",
        "source_identity": "bis_data_portal",
        "endpoint_identity": "bis_sdmx_api_v2",
        "media_type": "text/csv",
        "revision": "revision_1",
    }
    values.update(changes)
    return PolicyRateMetadata(**values)  # type: ignore[arg-type]


def csv_bytes(*rows: tuple[str, str, str, str, str]) -> bytes:
    content = ["FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE,OBS_STATUS"]
    content.extend(",".join(row) for row in rows)
    return ("\n".join(content) + "\n").encode()


def observations(currency: str = "AUD") -> tuple[PolicyRateObservation, ...]:
    area = APPROVED_BIS_SERIES[currency].split(".")[1]
    return parse_bis_csv(
        csv_bytes(
            ("D", area, "2014-01-01", "2.50", "A"),
            ("D", area, "2014-01-02", "2.75", "A"),
        ),
        spec(currency),
    )


def source(currency: str = "AUD") -> PolicySourceEvidence:
    domains = {
        "AUD": "https://www.rba.gov.au/monetary-policy/decisions/2014/",
        "CAD": "https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/",
        "CHF": "https://www.snb.ch/en/the-snb/mandates-goals/monetary-policy/decisions",
        "EUR": "https://www.ecb.europa.eu/press/govcdec/mopo/html/index.en.html",
        "GBP": "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp",
        "JPY": "https://www.boj.or.jp/en/mopo/mpmdeci/index.htm",
        "NZD": "https://www.rbnz.govt.nz/monetary-policy/official-cash-rate-decisions",
        "USD": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    }
    return PolicySourceEvidence(
        domains[currency], RETRIEVED, SHA_A, 100, "text/html", "official_decision"
    )


def event(
    currency: str = "AUD",
    *,
    kind: PolicyEventKind = PolicyEventKind.RATE_CHANGE,
    event_id: str = "aud_rate_2014_01_02",
    announcement_lower: datetime | None = None,
    announcement_upper: datetime | None = None,
    effective_lower: datetime | None = None,
    effective_upper: datetime | None = None,
    old_rate: str | None = "2.50",
    new_rate: str = "2.75",
    ambiguity: AmbiguityState = AmbiguityState.CLEAR,
) -> PolicyRateEvent:
    announcement_lower = announcement_lower or datetime(2014, 1, 1, 4, tzinfo=UTC)
    announcement_upper = announcement_upper or announcement_lower
    effective_lower = effective_lower or datetime(2014, 1, 2, tzinfo=UTC)
    effective_upper = effective_upper or effective_lower
    return PolicyRateEvent(
        event_id=event_id,
        kind=kind,
        currency=currency,
        central_bank_id=f"{currency.lower()}_central_bank",
        policy_instrument_id="principal_policy_rate",
        announcement_lower=announcement_lower,
        announcement_upper=announcement_upper,
        announcement_precision=TimePrecision.EXACT_TIMESTAMP,
        effective_lower=effective_lower,
        effective_upper=effective_upper,
        effective_precision=TimePrecision.EXACT_TIMESTAMP,
        source_timezone="UTC",
        old_rate=old_rate,
        new_rate=new_rate,
        source=source(currency),
        evidence_classification=EvidenceClassification.OFFICIAL_ANNOUNCEMENT,
        ambiguity=ambiguity,
        conflict=AmbiguityState.CLEAR,
    )


def series_manifest(currency: str = "AUD", *, retrieved_at: datetime = RETRIEVED):
    raw = csv_bytes(
        ("D", APPROVED_BIS_SERIES[currency].split(".")[1], "2014-01-01", "2.50", "A"),
        ("D", APPROVED_BIS_SERIES[currency].split(".")[1], "2014-01-02", "2.75", "A"),
    )
    return build_series_manifest(request(currency), metadata(currency), raw, retrieved_at)


def spot_reference(
    pair: str, formation_at: datetime, dataset_id: str = SHA_A
) -> SpotObservationReference:
    return SpotObservationReference(
        pair=pair,
        dataset_id=dataset_id,
        bar_open=formation_at - timedelta(days=1),
        bar_close=formation_at,
        value_field="close",
        closed=True,
    )


def policy_reference(
    currency: str,
    cutoff: datetime,
    dataset_id: str = SHA_A,
    *,
    observation: PolicyRateObservation | None = None,
    policy_event: PolicyRateEvent | None = None,
) -> PolicyStateReference:
    observation = observation or PolicyRateObservation(
        APPROVED_BIS_SERIES[currency],
        cutoff.date() - timedelta(days=1),
        Decimal("2.50"),
        "A",
    )
    event_id = policy_event.event_id if policy_event else f"{currency.lower()}_baseline"
    instrument_id = policy_event.policy_instrument_id if policy_event else "principal_policy_rate"
    announcement_upper = (
        policy_event.announcement_upper if policy_event else cutoff - timedelta(days=2)
    )
    effective_upper = policy_event.effective_upper if policy_event else cutoff - timedelta(days=2)
    return PolicyStateReference(
        currency=currency,
        series_key=APPROVED_BIS_SERIES[currency],
        dataset_id=dataset_id,
        observation_id=observation.identity,
        event_id=event_id,
        policy_instrument_id=instrument_id,
        observation_date=observation.observation_date,
        observation_value=observation.value,
        observation_status=observation.status,
        announcement_upper=announcement_upper,
        effective_upper=effective_upper,
        eligible=True,
    )


def complete_formation(
    index: int,
    split: FormationSplit,
    *,
    spot_dataset_ids: dict[str, str] | None = None,
    policy_dataset_ids: dict[str, str] | None = None,
    policy_observations: dict[str, PolicyRateObservation] | None = None,
    policy_events: dict[str, PolicyRateEvent] | None = None,
    source_manifest_fingerprints: tuple[str, ...] = (SHA_A,),
) -> CandidateBFormation:
    if split is FormationSplit.TRAIN:
        base_year, base_month, offset = 2015, 1, index
    else:
        base_year, base_month, offset = 2022, 1, index - EXPECTED_TRAIN_COHORTS
    month_index = base_year * 12 + base_month - 1 + offset
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    formation_at = datetime(year, month, calendar.monthrange(year, month)[1], tzinfo=UTC)
    next_month_index = month_index + 1
    exit_year, exit_zero_based_month = divmod(next_month_index, 12)
    exit_month = exit_zero_based_month + 1
    exit_at = datetime(
        exit_year,
        exit_month,
        calendar.monthrange(exit_year, exit_month)[1],
        tzinfo=UTC,
    )
    cutoff = datetime.combine(formation_at.date(), datetime.min.time(), tzinfo=UTC)
    return qualify_formation(
        cohort_id=f"cohort_{index:03d}",
        formation_month=f"{formation_at.year:04d}-{formation_at.month:02d}",
        formation_at=formation_at,
        cutoff_at=cutoff,
        exit_at=exit_at,
        split=split,
        purged=False,
        spot_observations=tuple(
            spot_reference(pair, formation_at, (spot_dataset_ids or {}).get(pair, SHA_A))
            for pair in APPROVED_PAIRS
        ),
        policy_states=tuple(
            policy_reference(
                currency,
                cutoff,
                (policy_dataset_ids or {}).get(currency, SHA_A),
                observation=(policy_observations or {}).get(currency),
                policy_event=(policy_events or {}).get(currency),
            )
            for currency in APPROVED_BIS_SERIES
        ),
        source_manifest_fingerprints=source_manifest_fingerprints,
    )


def fully_bound_qualification_inputs() -> tuple[
    tuple[PolicyRateSeriesManifest, ...],
    PolicyEventManifest,
    tuple[PolicyConcordanceResult, ...],
    SpotPanelManifestReference,
    CandidateBFormationManifest,
]:
    templates = tuple(
        [complete_formation(index, FormationSplit.TRAIN) for index in range(83)]
        + [complete_formation(index + 83, FormationSplit.VALIDATION) for index in range(23)]
    )
    observation_dates = tuple(
        sorted({template.cutoff_at.date() - timedelta(days=1) for template in templates})
    )
    manifests: list[PolicyRateSeriesManifest] = []
    for currency, series_key in APPROVED_BIS_SERIES.items():
        area = series_key.split(".", 1)[1]
        raw = csv_bytes(*(("D", area, item.isoformat(), "2.50", "A") for item in observation_dates))
        manifests.append(
            build_series_manifest(request(currency), metadata(currency), raw, RETRIEVED)
        )
    events = tuple(
        event(
            currency,
            kind=PolicyEventKind.BASELINE,
            event_id=f"{currency.lower()}_baseline",
            old_rate=None,
            new_rate="2.50",
            effective_lower=datetime(2013, 12, 31, tzinfo=UTC),
            effective_upper=datetime(2013, 12, 31, tzinfo=UTC),
        )
        for currency in APPROVED_BIS_SERIES
    )
    event_manifest = PolicyEventManifest(events)
    concordance = tuple(reconcile_policy_series(manifest, event_manifest) for manifest in manifests)
    policy_ids = {manifest.request.series.currency: manifest.dataset_id for manifest in manifests}
    events_by_currency = {item.currency: item for item in events}
    observations_by_currency_and_date = {
        manifest.request.series.currency: {
            item.observation_date: item for item in manifest.observations
        }
        for manifest in manifests
    }
    source_ids = tuple(manifest.manifest_id for manifest in manifests) + (
        event_manifest.manifest_id,
        SHA_A,
    )
    formations = tuple(
        complete_formation(
            index,
            FormationSplit.TRAIN if index < 83 else FormationSplit.VALIDATION,
            policy_dataset_ids=policy_ids,
            policy_observations={
                currency: observations_by_currency_and_date[currency][
                    templates[index].cutoff_at.date() - timedelta(days=1)
                ]
                for currency in APPROVED_BIS_SERIES
            },
            policy_events=events_by_currency,
            source_manifest_fingerprints=source_ids,
        )
        for index in range(106)
    )
    spot_panel = SpotPanelManifestReference(
        SHA_A,
        tuple(SHA_A for _ in APPROVED_PAIRS),
        tuple(spot for formation in formations for spot in formation.spot_observations),
    )
    return (
        tuple(manifests),
        event_manifest,
        concordance,
        spot_panel,
        CandidateBFormationManifest(formations),
    )


def test_exact_bis_series_and_request_contract_are_frozen() -> None:
    assert dict(APPROVED_BIS_SERIES) == {
        "AUD": "D.AU",
        "CAD": "D.CA",
        "CHF": "D.CH",
        "EUR": "D.XM",
        "GBP": "D.GB",
        "JPY": "D.JP",
        "NZD": "D.NZ",
        "USD": "D.US",
    }
    assert MAX_OBSERVATION_DATE == date(2023, 12, 31)
    with pytest.raises(ValueError):
        PolicyRateSeriesSpec("AUD", "D.US")
    with pytest.raises(ValueError):
        PolicyRateSeriesSpec("NOK", "D.NO")
    with pytest.raises(ValueError):
        PolicyRateRequest(spec(), APPROVED_REQUEST_START, date(2024, 1, 1))
    with pytest.raises(ValueError):
        PolicyRateRequest(spec(), date(2015, 1, 1), APPROVED_REQUEST_END)


@pytest.mark.parametrize(
    "changes",
    [
        {"agency": "ECB"},
        {"dataflow": "OTHER"},
        {"version": "2.0"},
        {"frequency": "M"},
        {"series_key": "D.US"},
        {"unit": ""},
        {"observation_status_semantics": ()},
        {"dsd_identity": ""},
        {"instrument_metadata": ""},
    ],
)
def test_wrong_or_missing_bis_metadata_fails_closed(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        metadata(**changes)


@pytest.mark.parametrize(
    "status",
    ["UNKNOWN", "CUSTOM", ""],
)
def test_unknown_or_missing_observation_status_fails_closed(status: str) -> None:
    raw = csv_bytes(("D", "AU", "2023-12-29", "4.10", status))
    with pytest.raises(PolicyRateQualificationError, match="observation_status_invalid"):
        build_series_manifest(request(), metadata(), raw, RETRIEVED)


def test_conflicting_status_metadata_fails_closed() -> None:
    with pytest.raises(ValueError, match="status semantics"):
        metadata(observation_status_semantics=("A=provisional",))


def test_known_normal_observation_status_is_accepted() -> None:
    manifest = build_series_manifest(
        request(),
        metadata(),
        csv_bytes(("D", "AU", "2023-12-29", "4.10", "A")),
        RETRIEVED,
    )
    assert manifest.observations[0].status == "A"


def test_response_after_sealed_window_is_rejected_without_truncation() -> None:
    raw = csv_bytes(("D", "AU", "2024-01-01", "4.35", "A"))
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        parse_bis_csv(raw, spec())


def test_mixed_response_rejects_before_secret_sentinel_can_be_exposed() -> None:
    raw = csv_bytes(
        ("D", "AU", "2023-12-29", "4.10", "A"),
        ("D", "AU", "2024-01-02", "SECRET_SENTINEL", "A"),
    )
    transport = FakeTransport(BisTransportResponse(raw, "text/csv", {}))
    with pytest.raises(PolicyRateQualificationError) as captured:
        ingest_series(request(), metadata(), transport, RETRIEVED)
    assert captured.value.reason == "sealed_window_violation"
    assert "SECRET_SENTINEL" not in str(captured.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("timestamp", ["", "not-a-date", "2023-13-01", "2023-01-01T00:00"])
def test_malformed_observation_dates_fail(timestamp: str) -> None:
    with pytest.raises(PolicyRateQualificationError):
        parse_bis_csv(csv_bytes(("D", "AU", timestamp, "2.5", "A")), spec())


def test_parser_rejects_wrong_series_dimensions_and_binary_float_is_not_used() -> None:
    with pytest.raises(PolicyRateQualificationError):
        parse_bis_csv(csv_bytes(("M", "AU", "2023-01-01", "2.5", "A")), spec())
    with pytest.raises(PolicyRateQualificationError):
        parse_bis_csv(csv_bytes(("D", "US", "2023-01-01", "2.5", "A")), spec())
    parsed = parse_bis_csv(csv_bytes(("D", "AU", "2023-01-01", "2.50", "A")), spec())
    assert parsed[0].value == Decimal("2.50")
    assert isinstance(parsed[0].value, Decimal)


def test_manifest_identities_are_canonical_and_retrieval_time_is_not_stable_identity() -> None:
    first = series_manifest()
    second = series_manifest(retrieved_at=RETRIEVED + timedelta(days=1))
    assert first.dataset_id == second.dataset_id
    assert first.manifest_id != second.manifest_id
    assert first.raw_sha256 == second.raw_sha256
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})
    persisted = asdict(first)
    assert persisted["parsed_min_observation_date"] == date(2014, 1, 1)
    assert persisted["parsed_max_observation_date"] == date(2014, 1, 2)
    assert persisted["raw_row_count"] == 2
    assert persisted["numeric_observation_count"] == 2
    assert "row_count" not in persisted


def test_canonicalization_rejects_arbitrary_mapping_keys() -> None:
    with pytest.raises(ValueError, match="string keys"):
        canonical_sha256({object(): "not-safe"})


def test_revisions_change_dataset_identity_and_require_reconcordance() -> None:
    first = series_manifest()
    revised = build_series_manifest(
        request(),
        metadata(revision="revision_2"),
        csv_bytes(("D", "AU", "2014-01-01", "2.50", "A"), ("D", "AU", "2014-01-02", "2.75", "A")),
        RETRIEVED,
    )
    assert first.dataset_id != revised.dataset_id


def test_contracts_are_frozen_and_mutable_inputs_do_not_escape() -> None:
    statuses = ["A=normal"]
    item = metadata(observation_status_semantics=statuses)
    statuses.append("B=changed")
    assert item.observation_status_semantics == ("A=normal",)
    with pytest.raises(FrozenInstanceError):
        item.unit = "other"  # type: ignore[misc]
    supplied = list(observations())
    raw = csv_bytes(
        ("D", "AU", "2014-01-01", "2.50", "A"),
        ("D", "AU", "2014-01-02", "2.75", "A"),
    )
    manifest = PolicyRateSeriesManifest.from_parts(request(), item, raw, RETRIEVED, supplied)
    supplied.clear()
    assert len(manifest.observations) == 2

    headers = {"content-type": "text/csv"}
    response = BisTransportResponse(b"x", "text/csv", headers)
    headers["authorization"] = "secret"
    assert dict(response.headers) == {"content-type": "text/csv"}
    with pytest.raises(TypeError):
        response.headers["new"] = "value"  # type: ignore[index]


def test_manifest_from_parts_cannot_decouple_contaminated_raw_from_clean_rows() -> None:
    clean_raw = csv_bytes(("D", "AU", "2023-12-29", "4.10", "A"))
    contaminated_raw = clean_raw + b"D,AU,2024-01-02,SECRET_SENTINEL,A\n"
    clean_rows = parse_bis_csv(clean_raw, spec())
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        PolicyRateSeriesManifest.from_parts(
            request(), metadata(), contaminated_raw, RETRIEVED, clean_rows
        )


def test_manifest_computed_bindings_cannot_be_replaced_or_forged() -> None:
    with pytest.raises(TypeError, match="from_parts"):
        PolicyRateSeriesManifest()
    manifest = series_manifest()
    for field_name, replacement in (
        ("raw_sha256", SHA_B),
        ("canonical_observation_hash", SHA_B),
        ("raw_row_count", 999),
        ("numeric_observation_count", 999),
        ("parsed_min_observation_date", date(2015, 1, 1)),
        ("parsed_max_observation_date", date(2015, 1, 2)),
        ("dataset_id", SHA_B),
    ):
        with pytest.raises((TypeError, ValueError)):
            replace(manifest, **{field_name: replacement})

    with pytest.raises(PolicyRateQualificationError, match="raw_parsed_mismatch"):
        PolicyRateSeriesManifest.from_parts(
            request(),
            metadata(),
            csv_bytes(("D", "AU", "2014-01-01", "9.99", "A")),
            RETRIEVED,
            observations(),
        )
    revised = build_series_manifest(
        request(),
        metadata(revision="revision_2"),
        csv_bytes(
            ("D", "AU", "2014-01-01", "2.50", "A"),
            ("D", "AU", "2014-01-02", "2.75", "A"),
        ),
        RETRIEVED,
    )
    assert revised.dataset_id != manifest.dataset_id


@pytest.mark.parametrize(
    "url",
    [
        "http://www.rba.gov.au/decision",
        "https://user:pass@www.rba.gov.au/decision",
        "https://www.rba.gov.au/decision#secret",
        "https://example.com/decision",
        "https://rba.gov.au.evil.example/decision",
        "https://www.rba.gov.au:444/decision",
        "https://www.rba.gov.au/%2e%2e/private",
        "https://www.rba.gov.au/%2E%2e/private",
        "https://www.rba.gov.au/a/%2e./private",
        "https://www.rba.gov.au/a%2f..%2fprivate",
        "https://www.rba.gov.au/a%5c..%5cprivate",
        "https://www.rba.gov.au/safe%2fpath",
        "https://www.rba.gov.au/safe%2Fpath",
        "https://www.rba.gov.au/safe%5cpath",
        "https://www.rba.gov.au/safe%5Cpath",
        "https://www.rba.gov.au/safe%252fpath",
        "https://www.rba.gov.au/safe%255cpath",
        "https://www.rba.gov.au/decision?token=abc",
        "https://www.rba.gov.au/decision?api_key=abc",
        "https://www.rba.gov.au/decision?password=abc",
        "https://www.rba.gov.au/decision?authorization=Bearer%20abc",
        "https://www.rba.gov.au/decision?next=https%3A%2F%2Fevil.example",
        "https://user%3Apass@www.rba.gov.au/decision",
        "https://www.rba.gov.au/decision%00hidden",
    ],
)
def test_unsafe_policy_source_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        PolicySourceEvidence(url, RETRIEVED, SHA_A, 10, "text/html", "official_decision")


def test_approved_policy_source_url_with_literal_path_separators_is_accepted() -> None:
    evidence = PolicySourceEvidence(
        "https://www.rba.gov.au/safe/path",
        RETRIEVED,
        SHA_A,
        10,
        "text/html",
        "official_decision",
    )
    assert evidence.source_url == "https://www.rba.gov.au/safe/path"


def test_point_in_time_excludes_same_day_future_and_ambiguous_intervals() -> None:
    cutoff = datetime(2023, 6, 1, tzinfo=UTC)
    same_day = event(
        announcement_lower=cutoff,
        announcement_upper=cutoff,
        effective_lower=cutoff,
        effective_upper=cutoff,
    )
    assert event_is_eligible(same_day, cutoff) is False
    future_effective = event(
        announcement_lower=cutoff - timedelta(days=1),
        announcement_upper=cutoff - timedelta(days=1),
        effective_lower=cutoff + timedelta(days=1),
        effective_upper=cutoff + timedelta(days=1),
    )
    assert event_is_eligible(future_effective, cutoff) is False
    ambiguous = event(ambiguity=AmbiguityState.AMBIGUOUS)
    assert event_is_eligible(ambiguous, datetime(2014, 2, 1, tzinfo=UTC)) is False


def test_date_only_bounds_use_conservative_upper_bound() -> None:
    item = PolicyRateEvent(
        event_id="aud_date_only",
        kind=PolicyEventKind.RATE_CHANGE,
        currency="AUD",
        central_bank_id="aud_central_bank",
        policy_instrument_id="principal_policy_rate",
        announcement_lower=datetime(2023, 6, 1, tzinfo=UTC),
        announcement_upper=datetime(2023, 6, 2, tzinfo=UTC),
        announcement_precision=TimePrecision.DATE_ONLY,
        effective_lower=datetime(2023, 6, 1, tzinfo=UTC),
        effective_upper=datetime(2023, 6, 2, tzinfo=UTC),
        effective_precision=TimePrecision.DATE_ONLY,
        source_timezone="UTC",
        old_rate="3.50",
        new_rate="3.75",
        source=source(),
        evidence_classification=EvidenceClassification.OFFICIAL_ANNOUNCEMENT,
        ambiguity=AmbiguityState.CLEAR,
        conflict=AmbiguityState.CLEAR,
    )
    assert event_is_eligible(item, datetime(2023, 6, 1, 12, tzinfo=UTC)) is False
    assert event_is_eligible(item, datetime(2023, 6, 2, tzinfo=UTC)) is True


def test_concordance_passes_exact_official_transition() -> None:
    baseline = event(
        kind=PolicyEventKind.BASELINE,
        event_id="aud_baseline",
        old_rate=None,
        new_rate="2.50",
        effective_lower=datetime(2013, 12, 31, tzinfo=UTC),
        effective_upper=datetime(2013, 12, 31, tzinfo=UTC),
    )
    result = reconcile_policy_series(series_manifest(), PolicyEventManifest((baseline, event())))
    assert result.status is ConcordanceStatus.PASS
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("events", "reason"),
    [
        ((), "missing_baseline"),
        ((event(new_rate="3.00"),), "missing_baseline"),
        ((event(ambiguity=AmbiguityState.CONFLICTING),), "conflicting_official_evidence"),
    ],
)
def test_missing_conflicting_or_mismatched_events_fail(
    events: tuple[PolicyRateEvent, ...], reason: str
) -> None:
    result = reconcile_policy_series(series_manifest(), PolicyEventManifest(events))
    assert result.status is ConcordanceStatus.FAIL
    assert reason in result.reasons


def test_duplicate_events_fail_before_concordance() -> None:
    duplicate = event()
    with pytest.raises(ValueError, match="duplicate"):
        PolicyEventManifest((duplicate, duplicate))


def test_unexplained_transition_and_official_transition_absent_from_bis_fail() -> None:
    baseline = event(
        kind=PolicyEventKind.BASELINE,
        event_id="aud_baseline",
        old_rate=None,
        new_rate="2.50",
        effective_lower=datetime(2013, 12, 31, tzinfo=UTC),
        effective_upper=datetime(2013, 12, 31, tzinfo=UTC),
    )
    missing = reconcile_policy_series(series_manifest(), PolicyEventManifest((baseline,)))
    assert "unexplained_bis_transition" in missing.reasons
    extra = event(
        event_id="aud_extra",
        old_rate="2.75",
        new_rate="3.00",
        effective_lower=datetime(2014, 1, 3, tzinfo=UTC),
        effective_upper=datetime(2014, 1, 3, tzinfo=UTC),
    )
    absent = reconcile_policy_series(
        series_manifest(), PolicyEventManifest((baseline, event(), extra))
    )
    assert "official_transition_absent_from_bis" in absent.reasons


def test_instrument_transition_requires_explicit_transition_evidence() -> None:
    baseline = event(
        kind=PolicyEventKind.BASELINE,
        event_id="aud_baseline",
        old_rate=None,
        new_rate="2.50",
        effective_lower=datetime(2013, 12, 31, tzinfo=UTC),
        effective_upper=datetime(2013, 12, 31, tzinfo=UTC),
    )
    transition = event(kind=PolicyEventKind.INSTRUMENT_TRANSITION)
    result = reconcile_policy_series(series_manifest(), PolicyEventManifest((baseline, transition)))
    assert result.status is ConcordanceStatus.PASS


def test_closed_d1_reference_and_complete_formation_require_exact_universe() -> None:
    formation_at = datetime(2020, 1, 31, tzinfo=UTC)
    with pytest.raises(ValueError, match="closed"):
        SpotObservationReference(
            "AUDUSD", SHA_A, formation_at - timedelta(days=1), formation_at, "close", False
        )
    cutoff = datetime.combine(formation_at.date(), datetime.min.time(), tzinfo=UTC)
    missing_spot = qualify_formation(
        cohort_id="missing_spot",
        formation_month="2020-01",
        formation_at=formation_at,
        cutoff_at=cutoff,
        exit_at=formation_at + timedelta(days=28),
        split=FormationSplit.TRAIN,
        purged=False,
        spot_observations=tuple(spot_reference(pair, formation_at) for pair in APPROVED_PAIRS[:-1]),
        policy_states=tuple(policy_reference(currency, cutoff) for currency in APPROVED_BIS_SERIES),
        source_manifest_fingerprints=(SHA_A,),
    )
    assert missing_spot.complete is False
    assert missing_spot.rejection_reason == "missing_spot_observation"
    missing_rate = qualify_formation(
        cohort_id="missing_rate",
        formation_month="2020-01",
        formation_at=formation_at,
        cutoff_at=cutoff,
        exit_at=formation_at + timedelta(days=28),
        split=FormationSplit.TRAIN,
        purged=False,
        spot_observations=tuple(spot_reference(pair, formation_at) for pair in APPROVED_PAIRS),
        policy_states=tuple(
            policy_reference(currency, cutoff) for currency in tuple(APPROVED_BIS_SERIES)[:-1]
        ),
        source_manifest_fingerprints=(SHA_A,),
    )
    assert missing_rate.complete is False
    assert missing_rate.rejection_reason == "missing_policy_state"


def test_post_2023_spot_and_formation_references_are_rejected() -> None:
    future_close = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        spot_reference("AUDUSD", future_close)

    safe_close = datetime(2023, 12, 31, tzinfo=UTC)
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        qualify_formation(
            cohort_id="future_exit",
            formation_month="2023-12",
            formation_at=safe_close,
            cutoff_at=datetime(2023, 12, 31, tzinfo=UTC),
            exit_at=datetime(2024, 1, 31, tzinfo=UTC),
            split=FormationSplit.VALIDATION,
            purged=False,
            spot_observations=tuple(spot_reference(pair, safe_close) for pair in APPROVED_PAIRS),
            policy_states=tuple(
                policy_reference(currency, safe_close) for currency in APPROVED_BIS_SERIES
            ),
            source_manifest_fingerprints=(SHA_A,),
        )


def test_boundary_crossing_formation_is_purged() -> None:
    formation_at = datetime(2021, 12, 31, tzinfo=UTC)
    cutoff = datetime(2021, 12, 31, tzinfo=UTC)
    purged = qualify_formation(
        cohort_id="boundary",
        formation_month="2021-12",
        formation_at=formation_at,
        cutoff_at=cutoff,
        exit_at=datetime(2022, 1, 31, tzinfo=UTC),
        split=FormationSplit.PURGED,
        purged=True,
        spot_observations=tuple(spot_reference(pair, formation_at) for pair in APPROVED_PAIRS),
        policy_states=tuple(policy_reference(currency, cutoff) for currency in APPROVED_BIS_SERIES),
        source_manifest_fingerprints=(SHA_A,),
    )
    assert purged.complete is False
    assert purged.rejection_reason == "split_boundary_purge"


def test_formation_month_and_split_labels_cannot_override_calendar_derivation() -> None:
    item = complete_formation(0, FormationSplit.TRAIN)
    with pytest.raises(ValueError, match="formation month"):
        replace(item, formation_month="1999-01")
    with pytest.raises(ValueError, match="split"):
        replace(item, split=FormationSplit.VALIDATION)


def test_train_validation_crossing_cohort_must_be_purged() -> None:
    formation_at = datetime(2021, 12, 31, tzinfo=UTC)
    cutoff = datetime(2021, 12, 31, tzinfo=UTC)
    with pytest.raises(ValueError, match="split|purge"):
        qualify_formation(
            cohort_id="boundary_mismatch",
            formation_month="2021-12",
            formation_at=formation_at,
            cutoff_at=cutoff,
            exit_at=datetime(2022, 1, 31, tzinfo=UTC),
            split=FormationSplit.TRAIN,
            purged=False,
            spot_observations=tuple(spot_reference(pair, formation_at) for pair in APPROVED_PAIRS),
            policy_states=tuple(
                policy_reference(currency, cutoff) for currency in APPROVED_BIS_SERIES
            ),
            source_manifest_fingerprints=(SHA_A,),
        )


def test_policy_and_spot_references_expose_resolvable_immutable_identities() -> None:
    observation = PolicyRateObservation("D.AU", date(2020, 1, 30), Decimal("0.75"), "A")
    reference = PolicyStateReference(
        currency="AUD",
        series_key="D.AU",
        dataset_id=SHA_A,
        observation_id=observation.identity,
        observation_date=observation.observation_date,
        observation_value=observation.value,
        observation_status=observation.status,
        event_id="aud_baseline",
        policy_instrument_id="principal_policy_rate",
        announcement_upper=datetime(2019, 1, 1, tzinfo=UTC),
        effective_upper=datetime(2019, 1, 1, tzinfo=UTC),
        eligible=True,
    )
    assert reference.observation_id == observation.identity
    spot = spot_reference("AUDUSD", datetime(2020, 1, 31, tzinfo=UTC))
    panel = SpotPanelManifestReference(SHA_A, (SHA_A,) * 7, observations=(spot,))
    assert panel.observations == (spot,)


def test_direct_formation_construction_cannot_claim_false_completeness() -> None:
    formation_at = datetime(2020, 1, 31, tzinfo=UTC)
    with pytest.raises(ValueError, match="complete formation"):
        CandidateBFormation(
            cohort_id="forged_complete",
            formation_month="2020-01",
            formation_at=formation_at,
            cutoff_at=datetime(2020, 1, 31, tzinfo=UTC),
            exit_at=datetime(2020, 2, 28, tzinfo=UTC),
            split=FormationSplit.TRAIN,
            purged=False,
            spot_observations=(),
            policy_states=(),
            pit_eligible=True,
            complete=True,
            rejection_reason=None,
            source_manifest_fingerprints=(SHA_A,),
        )


def test_exact_83_23_106_cohort_gate() -> None:
    formations = tuple(
        [complete_formation(index, FormationSplit.TRAIN) for index in range(83)]
        + [complete_formation(index + 83, FormationSplit.VALIDATION) for index in range(23)]
    )
    manifest = CandidateBFormationManifest(formations)
    assert manifest.train_count == EXPECTED_TRAIN_COHORTS == 83
    assert manifest.validation_count == EXPECTED_VALIDATION_COHORTS == 23
    assert manifest.total_count == EXPECTED_TOTAL_COHORTS == 106
    assert manifest.qualified is True
    assert CandidateBFormationManifest(formations[:-1]).qualified is False


@pytest.mark.parametrize(
    ("train_count", "validation_count"),
    [(82, 23), (83, 22), (84, 23), (83, 24)],
)
def test_exact_count_gate_rejects_every_off_by_one_shape(
    train_count: int, validation_count: int
) -> None:
    train = [complete_formation(index, FormationSplit.TRAIN) for index in range(83)]
    validation = [complete_formation(index + 83, FormationSplit.VALIDATION) for index in range(23)]
    if train_count < 83:
        train = train[:train_count]
    elif train_count > 83:
        train.append(replace(train[-1], cohort_id="extra_train"))
    if validation_count < 23:
        validation = validation[:validation_count]
    elif validation_count > 23:
        validation.append(replace(validation[-1], cohort_id="extra_validation"))
    formations = tuple(train + validation)
    assert CandidateBFormationManifest(formations).qualified is False


def test_qualification_contract_has_no_performance_fields() -> None:
    forbidden = {
        "policy_differential",
        "rank",
        "weight",
        "return",
        "pnl",
        "sharpe",
        "drawdown",
        "performance",
    }
    for dto in (CandidateBFormation, CandidateBFormationManifest, CandidateBQualificationResult):
        names = {field.name.lower() for field in fields(dto)}
        assert not any(any(term in name for term in forbidden) for name in names)


class FakeTransport:
    def __init__(self, response: BisTransportResponse):
        self.response = response
        self.calls: list[PolicyRateRequest] = []

    def fetch(self, item: PolicyRateRequest) -> BisTransportResponse:
        self.calls.append(item)
        return self.response


def test_ingestion_uses_one_attempt_no_retry_or_fallback() -> None:
    raw = csv_bytes(("D", "AU", "2014-01-01", "2.50", "A"))
    transport = FakeTransport(BisTransportResponse(raw, "text/csv", {}))
    result = ingest_series(request(), metadata(), transport, RETRIEVED)
    assert len(transport.calls) == 1
    assert result.series_manifest.dataset_id
    assert result.raw_bytes == raw


def test_default_ingestion_main_refuses_network_acquisition() -> None:
    with pytest.raises(SystemExit, match="network_acquisition_not_authorized"):
        ingest_main()


def test_unexpected_future_response_never_produces_manifest_and_transport_is_not_retried() -> None:
    raw = csv_bytes(("D", "AU", "2024-01-01", "4.35", "A"))
    transport = FakeTransport(BisTransportResponse(raw, "text/csv", {}))
    with pytest.raises(PolicyRateQualificationError, match="sealed_window_violation"):
        ingest_series(request(), metadata(), transport, RETRIEVED)
    assert len(transport.calls) == 1


def test_future_source_rows_do_not_change_a_bounded_transport_result() -> None:
    allowed = csv_bytes(("D", "AU", "2014-01-01", "2.50", "A"))
    first = FakeTransport(BisTransportResponse(allowed, "text/csv", {}))
    second = FakeTransport(BisTransportResponse(allowed, "text/csv", {"source_has_later": "yes"}))
    assert ingest_series(request(), metadata(), first, RETRIEVED).series_manifest.dataset_id == (
        ingest_series(request(), metadata(), second, RETRIEVED).series_manifest.dataset_id
    )


def test_offline_qualification_requires_every_manifest_and_passing_concordance() -> None:
    formation_manifest = CandidateBFormationManifest(())
    spot = SpotPanelManifestReference(SHA_A, tuple(SHA_A for _ in APPROVED_PAIRS))
    failed = qualify_candidate_b(
        series_manifests=(),
        event_manifest=PolicyEventManifest(()),
        concordance_results=(),
        spot_panel=spot,
        formation_manifest=formation_manifest,
    )
    assert failed.qualified is False
    assert "missing_series_manifest" in failed.reasons


def test_offline_qualification_cross_binds_concordance_to_series_and_event_inventory() -> None:
    manifests = tuple(series_manifest(currency) for currency in APPROVED_BIS_SERIES)
    event_manifest = PolicyEventManifest(())
    mismatched = tuple(
        PolicyConcordanceResult(
            currency,
            SHA_B,
            event_manifest.manifest_id,
            ConcordanceStatus.PASS,
            (),
        )
        for currency in APPROVED_BIS_SERIES
    )
    result = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=event_manifest,
        concordance_results=mismatched,
        spot_panel=SpotPanelManifestReference(SHA_A, tuple(SHA_A for _ in APPROVED_PAIRS)),
        formation_manifest=CandidateBFormationManifest(()),
    )
    assert result.qualified is False
    assert "concordance_provenance_mismatch" in result.reasons


def test_offline_qualification_recomputes_concordance_instead_of_trusting_pass_flag() -> None:
    manifests = tuple(series_manifest(currency) for currency in APPROVED_BIS_SERIES)
    event_manifest = PolicyEventManifest(())
    forged_passes = tuple(
        PolicyConcordanceResult(
            currency,
            manifest.dataset_id,
            event_manifest.manifest_id,
            ConcordanceStatus.PASS,
            (),
        )
        for currency, manifest in zip(APPROVED_BIS_SERIES, manifests, strict=True)
    )
    result = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=event_manifest,
        concordance_results=forged_passes,
        spot_panel=SpotPanelManifestReference(SHA_A, tuple(SHA_A for _ in APPROVED_PAIRS)),
        formation_manifest=CandidateBFormationManifest(()),
    )
    assert result.qualified is False
    assert "concordance_result_mismatch" in result.reasons


def test_offline_qualification_requires_formation_provenance_binding() -> None:
    manifests = tuple(series_manifest(currency) for currency in APPROVED_BIS_SERIES)
    events: list[PolicyRateEvent] = []
    for currency in APPROVED_BIS_SERIES:
        events.extend(
            (
                event(
                    currency,
                    kind=PolicyEventKind.BASELINE,
                    event_id=f"{currency.lower()}_baseline",
                    old_rate=None,
                    new_rate="2.50",
                    effective_lower=datetime(2013, 12, 31, tzinfo=UTC),
                    effective_upper=datetime(2013, 12, 31, tzinfo=UTC),
                ),
                event(currency, event_id=f"{currency.lower()}_rate_2014_01_02"),
            )
        )
    event_manifest = PolicyEventManifest(tuple(events))
    concordance = tuple(reconcile_policy_series(manifest, event_manifest) for manifest in manifests)
    formations = tuple(
        [complete_formation(index, FormationSplit.TRAIN) for index in range(83)]
        + [complete_formation(index + 83, FormationSplit.VALIDATION) for index in range(23)]
    )
    spot_panel = SpotPanelManifestReference(SHA_A, tuple(SHA_A for _ in APPROVED_PAIRS))
    result = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=event_manifest,
        concordance_results=concordance,
        spot_panel=spot_panel,
        formation_manifest=CandidateBFormationManifest(formations),
    )
    assert result.qualified is False
    assert "formation_provenance_mismatch" in result.reasons

    policy_ids = {manifest.request.series.currency: manifest.dataset_id for manifest in manifests}
    source_ids = tuple(manifest.manifest_id for manifest in manifests) + (
        event_manifest.manifest_id,
        spot_panel.manifest_id,
    )
    bound_formations = tuple(
        [
            complete_formation(
                index,
                FormationSplit.TRAIN,
                policy_dataset_ids=policy_ids,
                source_manifest_fingerprints=source_ids,
            )
            for index in range(83)
        ]
        + [
            complete_formation(
                index + 83,
                FormationSplit.VALIDATION,
                policy_dataset_ids=policy_ids,
                source_manifest_fingerprints=source_ids,
            )
            for index in range(23)
        ]
    )
    qualified = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=event_manifest,
        concordance_results=concordance,
        spot_panel=spot_panel,
        formation_manifest=CandidateBFormationManifest(bound_formations),
    )
    assert qualified.qualified is False
    assert "formation_reference_unresolved" in qualified.reasons


def test_fully_bound_synthetic_qualification_can_pass_without_outcomes() -> None:
    manifests, events, concordance, spots, formations = fully_bound_qualification_inputs()
    result = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=spots,
        formation_manifest=formations,
    )
    assert result.qualified is True
    assert result.reasons == ()


@pytest.mark.parametrize(
    "mutation",
    [
        "nonexistent_event",
        "nonexistent_observation",
        "event_currency_mismatch",
        "observation_value_mismatch",
        "instrument_mismatch",
    ],
)
def test_dangling_or_mismatched_policy_reference_invalidates_qualification(
    mutation: str,
) -> None:
    manifests, events, concordance, spots, formations = fully_bound_qualification_inputs()
    first = formations.formations[0]
    state = first.policy_states[0]
    changes: dict[str, object] = {}
    if mutation == "nonexistent_event":
        changes["event_id"] = "aud_nonexistent"
    elif mutation == "nonexistent_observation":
        changes["observation_id"] = SHA_B
    elif mutation == "event_currency_mismatch":
        changes["event_id"] = "cad_baseline"
    elif mutation == "observation_value_mismatch":
        changes["observation_value"] = Decimal("9.99")
    else:
        changes["policy_instrument_id"] = "wrong_instrument"
    forged_state = replace(state, **changes)
    forged_formation = replace(first, policy_states=(forged_state, *first.policy_states[1:]))
    forged_manifest = CandidateBFormationManifest((forged_formation, *formations.formations[1:]))
    result = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=spots,
        formation_manifest=forged_manifest,
    )
    assert result.qualified is False
    assert "formation_reference_unresolved" in result.reasons


def test_policy_series_mismatch_and_forged_future_pit_are_rejected() -> None:
    _, _, _, _, formations = fully_bound_qualification_inputs()
    first = formations.formations[0]
    state = first.policy_states[0]
    with pytest.raises(ValueError, match="series mismatch"):
        replace(state, series_key="D.US")
    with pytest.raises(ValueError, match="point-in-time"):
        replace(
            first,
            policy_states=(
                replace(
                    state,
                    announcement_upper=first.cutoff_at + timedelta(days=1),
                    eligible=True,
                ),
                *first.policy_states[1:],
            ),
        )


def test_missing_spot_index_entry_and_dataset_mismatch_invalidate_qualification() -> None:
    manifests, events, concordance, spots, formations = fully_bound_qualification_inputs()
    missing_spot_panel = SpotPanelManifestReference(
        spots.manifest_id,
        spots.dataset_ids,
        spots.observations[1:],
    )
    missing = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=missing_spot_panel,
        formation_manifest=formations,
    )
    assert missing.qualified is False
    assert "formation_reference_unresolved" in missing.reasons

    first = formations.formations[0]
    forged_spot = replace(first.spot_observations[0], dataset_id=SHA_B)
    forged_formation = replace(first, spot_observations=(forged_spot, *first.spot_observations[1:]))
    forged_manifest = CandidateBFormationManifest((forged_formation, *formations.formations[1:]))
    mismatched = qualify_candidate_b(
        series_manifests=manifests,
        event_manifest=events,
        concordance_results=concordance,
        spot_panel=spots,
        formation_manifest=forged_manifest,
    )
    assert mismatched.qualified is False
    assert "formation_provenance_mismatch" in mismatched.reasons


def test_wrong_d1_close_and_missing_pair_or_policy_state_fail_before_qualification() -> None:
    formation_at = datetime(2020, 1, 31, tzinfo=UTC)
    with pytest.raises(ValueError, match="closed D1"):
        SpotObservationReference(
            "AUDUSD",
            SHA_A,
            formation_at - timedelta(hours=23),
            formation_at,
            "close",
            True,
        )


def test_qualification_result_cannot_claim_success_with_incomplete_evidence() -> None:
    with pytest.raises(ValueError, match="qualified result"):
        CandidateBQualificationResult(
            qualified=True,
            reasons=(),
            series_manifest_ids=(SHA_A,),
            event_manifest_id=SHA_A,
            concordance_result_ids=(SHA_A,),
            spot_manifest_id=SHA_A,
            formation_manifest_id=SHA_A,
        )
