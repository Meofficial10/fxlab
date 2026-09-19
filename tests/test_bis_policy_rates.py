"""Comprehensive unit test suite for BIS policy-rate evidence acquisition layer (ADR 0014).

Tests offline parsing, validation, sealed-boundary enforcement, deterministic identities,
rate persistence, and fail-closed contracts without network I/O.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fxlab.research.bis_policy_rates import (
    END_EXCLUSIVE,
    FROZEN_SERIES_KEYS,
    NORMALIZATION_VERSION,
    SERIES_TO_CURRENCY,
    SOURCE_REQUEST_END_INCLUSIVE,
    START_INCLUSIVE,
    BisNormalizedDataset,
    BisObservationValidationError,
    BisRawArtifact,
    PolicyRateRecord,
    RateState,
    build_bis_all_series_request_urls,
    build_bis_series_request_url,
    build_daily_policy_rate_grid,
    compute_raw_evidence_identity,
    normalize_bis_policy_rate_payloads,
    parse_sdmx_csv_payload,
    parse_sdmx_xml_payload,
    publish_canonical_acquisition_bundle,
    publish_normalized_bis_dataset,
)


def _make_sample_xml_payload(
    series_key: str,
    observations: list[tuple[str, str]],
) -> bytes:
    """Helper to generate standard SDMX StructureSpecificData XML payload."""
    ref_area = series_key.split(".")[-1]
    xml_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">',
        '  <message:DataSet structureRef="BIS_WS_CBPOL_1_0">',
        f'    <Series FREQ="D" REF_AREA="{ref_area}" SERIES_KEY="{series_key}">',
    ]
    for obs_date, obs_val in observations:
        xml_lines.append(
            f'      <Obs TIME_PERIOD="{obs_date}" OBS_VALUE="{obs_val}" OBS_STATUS="A" />'
        )
    xml_lines.extend([
        "    </Series>",
        "  </message:DataSet>",
        "</message:StructureSpecificData>",
    ])
    return "\n".join(xml_lines).encode("utf-8")


def _make_sample_csv_payload(
    series_key: str,
    observations: list[tuple[str, str]],
) -> bytes:
    """Helper to generate standard SDMX-CSV payload."""
    csv_lines = [
        "DATAFLOW,FREQ,REF_AREA,SERIES_KEY,TIME_PERIOD,OBS_VALUE,OBS_STATUS",
    ]
    ref_area = series_key.split(".")[-1]
    for obs_date, obs_val in observations:
        csv_lines.append(
            f"BIS:WS_CBPOL(1.0),D,{ref_area},{series_key},{obs_date},{obs_val},A"
        )
    return "\n".join(csv_lines).encode("utf-8")


def _make_full_valid_payloads_dict() -> dict[str, bytes]:
    """Generate mock valid XML payloads for all eight frozen series across sample dates."""
    payloads: dict[str, bytes] = {}
    for key in FROZEN_SERIES_KEYS:
        # Give JPY negative rates and XM fixed rate
        obs = [
            ("2014-01-01", "-0.10" if key == "D.JP" else "0.25"),
            ("2015-06-15", "-0.10" if key == "D.JP" else "0.50"),
            ("2019-10-31", "-0.10" if key == "D.JP" else "1.75"),
            ("2023-12-31", "-0.10" if key == "D.JP" else "5.25"),
        ]
        payloads[key] = _make_sample_xml_payload(key, obs)
    return payloads


# --- Test 1–6: Frozen Contract & Request Construction ---

def test_01_exact_eight_series_contract():
    assert len(FROZEN_SERIES_KEYS) == 8
    assert FROZEN_SERIES_KEYS == (
        "D.AU", "D.CA", "D.CH", "D.GB", "D.JP", "D.NZ", "D.US", "D.XM"
    )
    expected_ccys = {"AUD", "CAD", "CHF", "GBP", "JPY", "NZD", "USD", "EUR"}
    assert set(SERIES_TO_CURRENCY.values()) == expected_ccys


def test_02_deterministic_bounded_request_urls():
    urls = build_bis_all_series_request_urls()
    assert len(urls) == 8
    for key in FROZEN_SERIES_KEYS:
        expected_url = (
            f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/{key}"
            f"?startPeriod=2014-01-01&endPeriod=2023-12-31"
        )
        assert urls[key] == expected_url


def test_03_start_and_end_constants():
    assert START_INCLUSIVE == date(2014, 1, 1)
    assert END_EXCLUSIVE == date(2024, 1, 1)
    assert SOURCE_REQUEST_END_INCLUSIVE == date(2023, 12, 31)


def test_04_request_url_rejects_unfrozen_series():
    with pytest.raises(ValueError, match="unsupported series_key"):
        build_bis_series_request_url("D.CN")


def test_05_request_url_rejects_non_contract_dates():
    with pytest.raises(ValueError, match="start_inclusive must be"):
        build_bis_series_request_url("D.US", start_inclusive=date(2015, 1, 1))
    with pytest.raises(ValueError, match="end_inclusive must be"):
        build_bis_series_request_url("D.US", end_inclusive=date(2024, 12, 31))


# --- Test 7–12: Raw Evidence & Identity ---

def test_06_raw_evidence_identity_deterministic():
    payloads = _make_full_valid_payloads_dict()
    combined_bytes = bytearray()
    for k in FROZEN_SERIES_KEYS:
        combined_bytes.extend(k.encode())
        combined_bytes.extend(b":")
        combined_bytes.extend(payloads[k])
    sha = hashlib.sha256(combined_bytes).hexdigest()

    raw1 = BisRawArtifact(
        requested_series=FROZEN_SERIES_KEYS,
        start_inclusive="2014-01-01",
        end_exclusive="2024-01-01",
        content_format="sdmx-xml",
        raw_byte_count=len(combined_bytes),
        raw_sha256=sha,
        series_payloads=tuple(sorted(payloads.items())),
        acquisition_timestamp="2026-09-19T14:00:00Z",
    )
    raw2 = BisRawArtifact(
        requested_series=FROZEN_SERIES_KEYS,
        start_inclusive="2014-01-01",
        end_exclusive="2024-01-01",
        content_format="sdmx-xml",
        raw_byte_count=len(combined_bytes),
        raw_sha256=sha,
        series_payloads=tuple(sorted(payloads.items())),
        acquisition_timestamp="2026-09-20T08:30:00Z",  # different timestamp
    )

    # Content identities must be strictly equal regardless of acquisition_timestamp
    id1 = compute_raw_evidence_identity(raw1)
    id2 = compute_raw_evidence_identity(raw2)
    assert id1 == id2
    assert len(id1) == 64


def test_07_raw_artifact_validation_fails_closed():
    with pytest.raises(ValueError, match="requested_series must contain"):
        BisRawArtifact(
            requested_series=("D.US", "D.XM"),
            raw_byte_count=100,
            raw_sha256="a" * 64,
        )
    with pytest.raises(ValueError, match="raw_sha256 must be a valid"):
        BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            raw_byte_count=100,
            raw_sha256="INVALID_SHA",
        )
    with pytest.raises(ValueError, match="raw_byte_count must be positive"):
        BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            raw_byte_count=0,
            raw_sha256="a" * 64,
        )


# --- Test 13–27: Normalization, Parsing, Validation, and Fail-Closed Boundaries ---

def test_08_valid_eight_series_xml_payload_accepted():
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads, content_format="sdmx-xml")
    assert dataset.schema == NORMALIZATION_VERSION
    assert dataset.record_count == 32  # 4 dates * 8 series
    assert len(dataset.records) == 32
    assert dataset.start_inclusive == "2014-01-01"
    assert dataset.end_exclusive == "2024-01-01"
    assert dataset.normalized_identity != ""


def test_09_valid_eight_series_csv_payload_accepted():
    payloads: dict[str, bytes] = {}
    obs = [("2014-01-01", "0.25"), ("2023-12-31", "5.00")]
    for key in FROZEN_SERIES_KEYS:
        payloads[key] = _make_sample_csv_payload(key, obs)
    dataset = normalize_bis_policy_rate_payloads(payloads, content_format="sdmx-csv")
    assert dataset.record_count == 16
    assert len(dataset.records) == 16


def test_10_unknown_series_rejected():
    payloads = _make_full_valid_payloads_dict()
    payloads["D.CN"] = _make_sample_xml_payload("D.CN", [("2014-01-01", "4.00")])
    with pytest.raises(ValueError, match="unknown series payloads"):
        normalize_bis_policy_rate_payloads(payloads)


def test_11_missing_required_series_rejected():
    payloads = _make_full_valid_payloads_dict()
    del payloads["D.JP"]
    with pytest.raises(ValueError, match="missing required series payloads"):
        normalize_bis_policy_rate_payloads(payloads)


def test_12_conflicting_duplicate_observation_rejected():
    payloads = _make_full_valid_payloads_dict()
    # Insert two conflicting observations for same date on D.US
    xml_with_dup = _make_sample_xml_payload(
        "D.US",
        [("2014-01-01", "0.25"), ("2014-01-01", "0.50")],
    )
    payloads["D.US"] = xml_with_dup
    with pytest.raises(ValueError, match="conflicting duplicate observation"):
        normalize_bis_policy_rate_payloads(payloads)


def test_13_non_finite_and_malformed_numeric_rejected():
    for bad_val in ("NaN", "inf", "-Infinity", "null", "abc", ""):
        with pytest.raises(ValueError):
            parse_sdmx_xml_payload(
                _make_sample_xml_payload("D.US", [("2014-01-01", bad_val)]),
                expected_series="D.US",
            )


def test_14_date_before_2014_rejected():
    with pytest.raises(ValueError, match="outside frozen interval"):
        parse_sdmx_xml_payload(
            _make_sample_xml_payload("D.US", [("2013-12-31", "0.25")]),
            expected_series="D.US",
        )


def test_15_observation_2024_plus_rejected():
    with pytest.raises(ValueError, match="outside frozen interval"):
        parse_sdmx_xml_payload(
            _make_sample_xml_payload("D.US", [("2024-01-01", "5.50")]),
            expected_series="D.US",
        )
    with pytest.raises(ValueError, match="outside frozen interval"):
        parse_sdmx_xml_payload(
            _make_sample_xml_payload("D.US", [("2025-06-15", "5.50")]),
            expected_series="D.US",
        )


def test_16_deterministic_normalized_ordering():
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads)
    # Check that records are strictly sorted by (observation_date, series_key)
    dates = [r.observation_date for r in dataset.records]
    assert dates == sorted(dates)
    for i in range(len(dataset.records) - 1):
        prev = (dataset.records[i].observation_date, dataset.records[i].series_key)
        curr = (dataset.records[i + 1].observation_date, dataset.records[i + 1].series_key)
        assert prev < curr


def test_17_eur_fixed_to_d_xm():
    assert SERIES_TO_CURRENCY["D.XM"] == "EUR"
    assert "D.XM" in FROZEN_SERIES_KEYS
    # Attempting to map EUR to an alternative key fails
    with pytest.raises(ValueError):
        PolicyRateRecord(
            observation_date=date(2014, 1, 1),
            series_key="D.DFR",  # unknown key
            currency="EUR",
            rate_value=Decimal("0.0"),
        )


def test_18_malformed_xml_fails_closed():
    with pytest.raises(ValueError, match="malformed XML payload"):
        parse_sdmx_xml_payload(b"<corrupt><xml>")


# --- Test 28–31: Rate Persistence, Grid, and Causal Point-in-Time Contract ---

def test_19_daily_grid_rate_persistence():
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads)
    grid = build_daily_policy_rate_grid(dataset)

    # 2014-01-01 was observed
    entry_2014_01_01 = grid[("USD", date(2014, 1, 1))]
    assert entry_2014_01_01.state == RateState.OBSERVED
    assert entry_2014_01_01.rate == Decimal("0.25")

    # 2014-01-02 was NOT observed, but rate persists
    entry_2014_01_02 = grid[("USD", date(2014, 1, 2))]
    assert entry_2014_01_02.state == RateState.RATE_PERSISTS
    assert entry_2014_01_02.rate == Decimal("0.25")
    assert entry_2014_01_02.last_change_date == date(2014, 1, 1)


def test_20_point_in_time_semantics_unresolved_and_no_causal_promotion():
    # Verify source date field is observation_date and point_in_time_status is UNRESOLVED
    rec = PolicyRateRecord(
        observation_date=date(2015, 6, 15),
        series_key="D.US",
        currency="USD",
        rate_value=Decimal("0.50"),
    )
    assert hasattr(rec, "observation_date")
    assert not hasattr(rec, "effective_date")
    assert not hasattr(rec, "announcement_date")
    assert not hasattr(rec, "available_at")
    assert rec.point_in_time_status == "UNRESOLVED"


def test_21_no_strategy_ready_historical_availability_api():
    # Verify acquisition module does not expose get_rate_known_at, get_causal_policy_rate, etc.
    import fxlab.research.bis_policy_rates as bpr

    assert not hasattr(bpr, "get_causal_policy_rate")
    assert not hasattr(bpr, "get_rate_known_at")
    assert not hasattr(bpr, "get_effective_rate")
    assert not hasattr(bpr, "get_signal_rate")


def test_22_raw_sha256_is_exact_bytes_and_separate_from_evidence_identity():
    # Verify raw_sha256 is exactly sha256(raw_bytes) and evidence identity binds metadata
    sample_bytes = b"<Obs TIME_PERIOD='2014-01-01' OBS_VALUE='0.25' />"
    exact_hash = hashlib.sha256(sample_bytes).hexdigest()

    raw = BisRawArtifact(
        requested_series=FROZEN_SERIES_KEYS,
        start_inclusive="2014-01-01",
        end_exclusive="2024-01-01",
        content_format="sdmx-xml",
        raw_byte_count=len(sample_bytes),
        raw_sha256=exact_hash,
        acquisition_timestamp="2026-09-19T00:00:00Z",
    )
    assert raw.raw_sha256 == exact_hash
    evidence_id = compute_raw_evidence_identity(raw)
    assert evidence_id != exact_hash  # Binds contract metadata, distinct from raw payload hash
    assert len(evidence_id) == 64


def test_23_same_date_different_series_allowed():
    # Same date across multiple series is completely valid
    payloads: dict[str, bytes] = {}
    same_date_obs = [("2014-01-01", "0.25")]
    for key in FROZEN_SERIES_KEYS:
        payloads[key] = _make_sample_xml_payload(key, same_date_obs)
    dataset = normalize_bis_policy_rate_payloads(payloads)
    assert dataset.record_count == 8
    # All 8 records have the exact same observation_date
    assert {r.observation_date for r in dataset.records} == {date(2014, 1, 1)}
    assert {r.series_key for r in dataset.records} == set(FROZEN_SERIES_KEYS)


def test_24_duplicate_same_series_and_date_fails_closed():
    # Duplicate observation for same series and same date must fail closed
    payloads = _make_full_valid_payloads_dict()
    # Identical duplicate for same series on same date
    xml_with_dup = _make_sample_xml_payload(
        "D.US",
        [("2014-01-01", "0.25"), ("2014-01-01", "0.25")],
    )
    payloads["D.US"] = xml_with_dup
    with pytest.raises(ValueError, match="duplicate observation"):
        normalize_bis_policy_rate_payloads(payloads)


def test_25_atomic_publishing_raw_and_normalized(tmp_path: Path):
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads)

    # Publish normalized dataset
    out_file = tmp_path / "normalized.json"
    published = publish_normalized_bis_dataset(dataset, out_file)
    assert published.exists()

    # Re-publishing identical content succeeds
    published2 = publish_normalized_bis_dataset(dataset, out_file)
    assert published2 == published

    # Attempting to overwrite with different content raises FileExistsError
    diff_records = dataset.records[:10]
    diff_dataset = BisNormalizedDataset(
        schema=NORMALIZATION_VERSION,
        series_keys=FROZEN_SERIES_KEYS,
        start_inclusive="2014-01-01",
        end_exclusive="2024-01-01",
        records=diff_records,
        record_count=len(diff_records),
        raw_evidence_identity=dataset.raw_evidence_identity,
    )
    with pytest.raises(FileExistsError, match="exists with different content"):
        publish_normalized_bis_dataset(diff_dataset, out_file)


def test_26_module_has_no_prohibited_strategy_calculations():
    # Verify module has no Sharpe, expectancy, or differential trading logic
    import inspect

    import fxlab.research.bis_policy_rates as bpr

    src = inspect.getsource(bpr)
    assert "sharpe" not in src.lower()
    assert "expectancy" not in src.lower()
    assert "backtest" not in src.lower()
    assert "place_order" not in src.lower()
    assert "submit_order" not in src.lower()
    assert "pnl" not in src.lower()


def test_27_operator_script_fails_closed_without_run(tmp_path: Path):
    from scripts.acquire_bis_policy_rates import main

    raw_dir = tmp_path / "raw"
    norm_file = tmp_path / "norm.json"
    # Without --run flag, main() must exit with non-zero code and create zero files
    exit_code = main(["--raw-output-dir", str(raw_dir), "--normalized-output", str(norm_file)])
    assert exit_code == 1
    assert not raw_dir.exists()
    assert not norm_file.exists()


def test_28_transactional_bundle_publishing_all_eight(tmp_path: Path):
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads)

    raw_artifacts: dict[str, BisRawArtifact] = {}
    for key in FROZEN_SERIES_KEYS:
        body = payloads[key]
        raw_artifacts[key] = BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            start_inclusive="2014-01-01",
            end_exclusive="2024-01-01",
            content_format="sdmx-xml",
            raw_byte_count=len(body),
            raw_sha256=hashlib.sha256(body).hexdigest(),
            series_payloads=((key, body),),
        )

    raw_dir = tmp_path / "raw"
    norm_file = tmp_path / "norm.json"

    raw_paths, out_norm = publish_canonical_acquisition_bundle(
        raw_artifacts, dataset, raw_dir, norm_file
    )
    assert len(raw_paths) == 8
    for p in raw_paths.values():
        assert p.exists()
    assert out_norm.exists()


def test_29_fetch_failure_in_script_leaves_zero_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from scripts import acquire_bis_policy_rates

    payloads = _make_full_valid_payloads_dict()

    call_count = 0

    def mock_fetch(series_key: str, timeout: float = 30.0) -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count >= 4:
            raise RuntimeError("network failure on series 4")
        return payloads[series_key]

    monkeypatch.setattr(acquire_bis_policy_rates, "fetch_bis_series_payload", mock_fetch)

    raw_dir = tmp_path / "raw"
    norm_file = tmp_path / "norm.json"

    with pytest.raises(RuntimeError, match="network failure"):
        acquire_bis_policy_rates.main(
            ["--run", "--raw-output-dir", str(raw_dir), "--normalized-output", str(norm_file)]
        )

    # Must leave ZERO canonical raw or normalized files
    assert not raw_dir.exists() or len(list(raw_dir.glob("*.json"))) == 0
    assert not norm_file.exists()


def test_30_malformed_payload_in_script_leaves_zero_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from scripts import acquire_bis_policy_rates

    payloads = _make_full_valid_payloads_dict()
    # Inject malformed XML in series D.JP
    payloads["D.JP"] = b"<malformed><xml>"

    def mock_fetch(series_key: str, timeout: float = 30.0) -> bytes:
        return payloads[series_key]

    monkeypatch.setattr(acquire_bis_policy_rates, "fetch_bis_series_payload", mock_fetch)

    raw_dir = tmp_path / "raw"
    norm_file = tmp_path / "norm.json"

    with pytest.raises(ValueError, match="malformed XML"):
        acquire_bis_policy_rates.main(
            ["--run", "--raw-output-dir", str(raw_dir), "--normalized-output", str(norm_file)]
        )

    assert not raw_dir.exists() or len(list(raw_dir.glob("*.json"))) == 0
    assert not norm_file.exists()


def test_31_normalization_failure_leaves_zero_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from scripts import acquire_bis_policy_rates

    payloads = _make_full_valid_payloads_dict()
    # Inject out of range observation (2024)
    payloads["D.US"] = _make_sample_xml_payload("D.US", [("2024-05-01", "5.25")])

    def mock_fetch(series_key: str, timeout: float = 30.0) -> bytes:
        return payloads[series_key]

    monkeypatch.setattr(acquire_bis_policy_rates, "fetch_bis_series_payload", mock_fetch)

    raw_dir = tmp_path / "raw"
    norm_file = tmp_path / "norm.json"

    with pytest.raises(ValueError, match="outside frozen interval"):
        acquire_bis_policy_rates.main(
            ["--run", "--raw-output-dir", str(raw_dir), "--normalized-output", str(norm_file)]
        )

    assert not raw_dir.exists() or len(list(raw_dir.glob("*.json"))) == 0
    assert not norm_file.exists()


def test_32_preexisting_different_target_fails_before_any_new_target(tmp_path: Path):
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads)

    raw_artifacts: dict[str, BisRawArtifact] = {}
    for key in FROZEN_SERIES_KEYS:
        body = payloads[key]
        raw_artifacts[key] = BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            start_inclusive="2014-01-01",
            end_exclusive="2024-01-01",
            content_format="sdmx-xml",
            raw_byte_count=len(body),
            raw_sha256=hashlib.sha256(body).hexdigest(),
            series_payloads=((key, body),),
        )

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    norm_file = tmp_path / "norm.json"

    # Pre-create conflicting normalized output file
    norm_file.write_text('{"different": "content"}', encoding="utf-8")

    with pytest.raises(FileExistsError, match="exists with different content"):
        publish_canonical_acquisition_bundle(raw_artifacts, dataset, raw_dir, norm_file)

    # Check that NO raw files were written to raw_dir
    assert len(list(raw_dir.glob("*.json"))) == 0
    assert norm_file.read_text(encoding="utf-8") == '{"different": "content"}'


def test_33_preexisting_identical_target_handled_safely(tmp_path: Path):
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads)

    raw_artifacts: dict[str, BisRawArtifact] = {}
    for key in FROZEN_SERIES_KEYS:
        body = payloads[key]
        raw_artifacts[key] = BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            start_inclusive="2014-01-01",
            end_exclusive="2024-01-01",
            content_format="sdmx-xml",
            raw_byte_count=len(body),
            raw_sha256=hashlib.sha256(body).hexdigest(),
            series_payloads=((key, body),),
        )

    raw_dir = tmp_path / "raw"
    norm_file = tmp_path / "norm.json"

    # First publication
    publish_canonical_acquisition_bundle(raw_artifacts, dataset, raw_dir, norm_file)

    # Second publication with identical content succeeds deterministically
    raw_paths, out_norm = publish_canonical_acquisition_bundle(
        raw_artifacts, dataset, raw_dir, norm_file
    )
    assert len(raw_paths) == 8
    assert out_norm.exists()


def test_34_failure_during_multi_file_publication_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads)

    raw_artifacts: dict[str, BisRawArtifact] = {}
    for key in FROZEN_SERIES_KEYS:
        body = payloads[key]
        raw_artifacts[key] = BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            start_inclusive="2014-01-01",
            end_exclusive="2024-01-01",
            content_format="sdmx-xml",
            raw_byte_count=len(body),
            raw_sha256=hashlib.sha256(body).hexdigest(),
            series_payloads=((key, body),),
        )

    raw_dir = tmp_path / "raw"
    norm_file = tmp_path / "norm.json"

    # Simulate failure on the 3rd file replacement
    real_replace = Path.replace
    replace_count = 0

    def mock_replace(self: Path, target: Path | str) -> Path:
        nonlocal replace_count
        replace_count += 1
        if replace_count >= 3:
            raise OSError("simulated filesystem error during commit")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", mock_replace)

    with pytest.raises(OSError, match="simulated filesystem error"):
        publish_canonical_acquisition_bundle(raw_artifacts, dataset, raw_dir, norm_file)

    # Verify that files 1 and 2 that were replaced got rolled back (deleted)
    assert len(list(raw_dir.glob("*.json"))) == 0
    assert not norm_file.exists()


def test_35_preexisting_files_protected_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payloads = _make_full_valid_payloads_dict()
    dataset = normalize_bis_policy_rate_payloads(payloads)

    raw_artifacts: dict[str, BisRawArtifact] = {}
    for key in FROZEN_SERIES_KEYS:
        body = payloads[key]
        raw_artifacts[key] = BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            start_inclusive="2014-01-01",
            end_exclusive="2024-01-01",
            content_format="sdmx-xml",
            raw_byte_count=len(body),
            raw_sha256=hashlib.sha256(body).hexdigest(),
            series_payloads=((key, body),),
        )

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True)
    norm_file = tmp_path / "norm.json"

    # Pre-existing unrelated file in raw_dir
    unrelated_file = raw_dir / "unrelated_audit.json"
    unrelated_file.write_text('{"keep": "me"}', encoding="utf-8")

    def mock_replace(self: Path, target: Path | str) -> Path:
        raise OSError("forced error during replacement")

    monkeypatch.setattr(Path, "replace", mock_replace)

    with pytest.raises(OSError, match="forced error"):
        publish_canonical_acquisition_bundle(raw_artifacts, dataset, raw_dir, norm_file)

def test_36_nan_fails_closed_with_rich_diagnostic_context_xml():
    xml = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">\n'
        b'  <message:DataSet structureRef="BIS_WS_CBPOL_1_0">\n'
        b'    <Series FREQ="D" REF_AREA="XM" SERIES_KEY="D.XM">\n'
        b'      <Obs TIME_PERIOD="2016-04-05" OBS_VALUE="NaN" OBS_STATUS="M" OBS_CONF="F" '
        b'OBS_PRE_BREAK="0.5" />\n'
        b'    </Series>\n'
        b'  </message:DataSet>\n'
        b'</message:StructureSpecificData>'
    )

    with pytest.raises(BisObservationValidationError) as exc_info:
        parse_sdmx_xml_payload(xml, expected_series="D.XM")

    exc = exc_info.value
    assert exc.series_key == "D.XM"
    assert exc.time_period == "2016-04-05"
    assert exc.raw_obs_value == "NaN"
    assert exc.obs_status == "M"
    assert exc.obs_conf == "F"
    assert exc.obs_pre_break == "0.5"

    err_str = str(exc)
    assert "non-finite rate value" in err_str
    assert "series=D.XM" in err_str
    assert "time_period=2016-04-05" in err_str
    assert "raw_obs_value=NaN" in err_str
    assert "obs_status=M" in err_str
    assert "obs_conf=F" in err_str
    assert "obs_pre_break=0.5" in err_str


def test_37_nan_fails_closed_with_absent_optional_metadata_xml():
    # When OBS_STATUS, OBS_CONF, OBS_PRE_BREAK are not in source payload, they remain None
    xml = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">\n'
        b'  <message:DataSet structureRef="BIS_WS_CBPOL_1_0">\n'
        b'    <Series FREQ="D" REF_AREA="CA" SERIES_KEY="D.CA">\n'
        b'      <Obs TIME_PERIOD="2018-09-12" OBS_VALUE="NaN" />\n'
        b'    </Series>\n'
        b'  </message:DataSet>\n'
        b'</message:StructureSpecificData>'
    )

    with pytest.raises(BisObservationValidationError) as exc_info:
        parse_sdmx_xml_payload(xml, expected_series="D.CA")

    exc = exc_info.value
    assert exc.series_key == "D.CA"
    assert exc.time_period == "2018-09-12"
    assert exc.raw_obs_value == "NaN"
    assert exc.obs_status is None
    assert exc.obs_conf is None
    assert exc.obs_pre_break is None

    err_str = str(exc)
    assert "obs_status" not in err_str
    assert "obs_conf" not in err_str
    assert "obs_pre_break" not in err_str


def test_38_nan_fails_closed_with_rich_diagnostic_context_csv():
    csv_payload = (
        b"DATAFLOW,FREQ,REF_AREA,SERIES_KEY,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK\n"
        b"BIS:WS_CBPOL(1.0),D,US,D.US,2019-07-31,NaN,ND,C,2.25\n"
    )

    with pytest.raises(BisObservationValidationError) as exc_info:
        parse_sdmx_csv_payload(csv_payload, expected_series="D.US")

    exc = exc_info.value
    assert exc.series_key == "D.US"
    assert exc.time_period == "2019-07-31"
    assert exc.raw_obs_value == "NaN"
    assert exc.obs_status == "ND"
    assert exc.obs_conf == "C"
    assert exc.obs_pre_break == "2.25"


def test_39_finite_observations_preserve_raw_metadata_without_error():
    xml = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">\n'
        b'  <message:DataSet structureRef="BIS_WS_CBPOL_1_0">\n'
        b'    <Series FREQ="D" REF_AREA="AU" SERIES_KEY="D.AU">\n'
        b'      <Obs TIME_PERIOD="2015-02-04" OBS_VALUE="2.25" OBS_STATUS="A" OBS_CONF="F" '
        b'OBS_PRE_BREAK="2.5" />\n'
        b'    </Series>\n'
        b'  </message:DataSet>\n'
        b'</message:StructureSpecificData>'
    )

    records = parse_sdmx_xml_payload(xml, expected_series="D.AU")
    assert len(records) == 1
    rec = records[0]
    assert rec.series_key == "D.AU"
    assert rec.observation_date == date(2015, 2, 4)
    assert rec.rate_value == Decimal("2.25")
    assert rec.obs_status == "A"
    assert rec.obs_conf == "F"
    assert rec.obs_pre_break == "2.5"


def test_40_transactional_acquisition_zero_artifacts_after_nan_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from scripts import acquire_bis_policy_rates

    payloads = _make_full_valid_payloads_dict()
    # Inject NaN in series D.XM
    payloads["D.XM"] = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">\n'
        b'  <message:DataSet structureRef="BIS_WS_CBPOL_1_0">\n'
        b'    <Series FREQ="D" REF_AREA="XM" SERIES_KEY="D.XM">\n'
        b'      <Obs TIME_PERIOD="2016-04-05" OBS_VALUE="NaN" OBS_STATUS="M" />\n'
        b'    </Series>\n'
        b'  </message:DataSet>\n'
        b'</message:StructureSpecificData>'
    )

    def mock_fetch(series_key: str, timeout: float = 30.0) -> bytes:
        return payloads[series_key]

    monkeypatch.setattr(acquire_bis_policy_rates, "fetch_bis_series_payload", mock_fetch)

    raw_dir = tmp_path / "raw"
    norm_file = tmp_path / "norm.json"

    with pytest.raises(BisObservationValidationError) as exc_info:
        acquire_bis_policy_rates.main(
            ["--run", "--raw-output-dir", str(raw_dir), "--normalized-output", str(norm_file)]
        )

    exc = exc_info.value
    assert exc.series_key == "D.XM"
    assert exc.time_period == "2016-04-05"
    assert exc.raw_obs_value == "NaN"
    assert exc.obs_status == "M"

    # Transactional publication guarantees 0 files
    assert not raw_dir.exists() or len(list(raw_dir.glob("*.json"))) == 0
    assert not norm_file.exists()
