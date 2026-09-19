from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from fxlab.research.bis_policy_rates import (
    FROZEN_SERIES_KEYS,
    NORMALIZATION_VERSION,
    START_INCLUSIVE,
    BisInitializationEvidence,
    PolicyRateStateOrigin,
    SourceObservationKind,
    build_bis_initialization_request_url,
    compute_initialization_evidence_identity,
    normalize_bis_policy_rate_payloads,
    parse_bis_initialization_payload,
)


def _payload(series: str, observations: list[tuple[str, str, str]]) -> bytes:
    area = series.split(".")[-1]
    rows = "".join(
        f'<Obs TIME_PERIOD="{day}" OBS_VALUE="{value}" OBS_STATUS="{status}" />'
        for day, value, status in observations
    )
    return (
        '<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/'
        'sdmxml/schemas/v2_1/message"><message:DataSet>'
        f'<Series FREQ="D" REF_AREA="{area}" SERIES_KEY="{series}">{rows}</Series>'
        '</message:DataSet></message:StructureSpecificData>'
    ).encode()


def _initialization_payloads() -> dict[str, bytes]:
    return {
        series: _payload(series, [("2013-12-31", "0.50", "A")])
        for series in FROZEN_SERIES_KEYS
    }


def _research_payloads() -> dict[str, bytes]:
    return {
        series: _payload(series, [("2014-01-01", "0.50", "A")])
        for series in FROZEN_SERIES_KEYS
    }


def test_boundary_request_uses_direct_predecessor_selection():
    assert build_bis_initialization_request_url("D.GB").endswith(
        "/D.GB?endPeriod=2013-12-31&lastNObservations=1"
    )


def test_finite_predecessor_is_auditable_and_deterministic():
    body = _initialization_payloads()["D.GB"]
    evidence = parse_bis_initialization_payload(body, expected_series="D.GB")
    assert isinstance(evidence, BisInitializationEvidence)
    assert evidence.selected_predecessor_date == date(2013, 12, 31)
    assert evidence.selected_predecessor_value == Decimal("0.50")
    assert evidence.raw_byte_count == len(body)
    assert evidence.raw_sha256 == hashlib.sha256(body).hexdigest()
    assert evidence.raw_payload == body
    assert compute_initialization_evidence_identity(evidence) == (
        compute_initialization_evidence_identity(evidence)
    )


@pytest.mark.parametrize(
    "observations, reason",
    [
        ([("2014-01-01", "0.50", "A")], "strictly before"),
        ([("2013-12-31", "NaN", "M")], "finite"),
        (
            [("2013-12-30", "0.50", "A"), ("2013-12-31", "0.50", "A")],
            "exactly one",
        ),
    ],
)
def test_invalid_predecessor_fails_closed(observations, reason):
    with pytest.raises(ValueError, match=reason):
        parse_bis_initialization_payload(
            _payload("D.GB", observations), expected_series="D.GB"
        )


def test_wrong_series_and_missing_series_fail_closed():
    with pytest.raises(ValueError, match="series mismatch"):
        parse_bis_initialization_payload(
            _initialization_payloads()["D.US"], expected_series="D.GB"
        )
    initialization = _initialization_payloads()
    del initialization["D.JP"]
    with pytest.raises(ValueError, match="missing required initialization"):
        normalize_bis_policy_rate_payloads(
            _research_payloads(), initialization_payloads_by_series=initialization
        )


def test_leading_missing_uses_same_series_predecessor_without_emitting_it():
    research = _research_payloads()
    research["D.GB"] = _payload(
        "D.GB",
        [("2014-01-01", "NaN", "M"), ("2014-01-02", "0.50", "A")],
    )
    dataset = normalize_bis_policy_rate_payloads(
        research, initialization_payloads_by_series=_initialization_payloads()
    )
    gb = [row for row in dataset.records if row.series_key == "D.GB"]
    assert [row.observation_date for row in gb] == [date(2014, 1, 1), date(2014, 1, 2)]
    assert gb[0].source_observation_kind == SourceObservationKind.MISSING
    assert gb[0].source_obs_value == "NaN"
    assert gb[0].source_obs_status == "M"
    assert gb[0].policy_rate_state == Decimal("0.50")
    assert gb[0].policy_rate_state_origin == PolicyRateStateOrigin.PERSISTED
    assert gb[0].source_state_date == date(2013, 12, 31)
    assert gb[1].policy_rate_state_origin == PolicyRateStateOrigin.OBSERVED
    assert gb[1].source_state_date == date(2014, 1, 2)
    assert all(row.observation_date >= START_INCLUSIVE for row in dataset.records)


def test_initialization_changes_scientific_identity_and_v3_stays_unresolved():
    initial = _initialization_payloads()
    first = normalize_bis_policy_rate_payloads(
        _research_payloads(), initialization_payloads_by_series=initial
    )
    initial["D.GB"] = _payload("D.GB", [("2013-12-30", "0.51", "A")])
    changed = normalize_bis_policy_rate_payloads(
        _research_payloads(), initialization_payloads_by_series=initial
    )
    assert NORMALIZATION_VERSION == "bis_cbpol_daily_v3"
    assert first.schema == "bis_cbpol_daily_v3"
    assert first.point_in_time_status == "UNRESOLVED"
    assert first.normalized_identity != changed.normalized_identity
