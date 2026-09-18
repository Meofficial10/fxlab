from __future__ import annotations

import hashlib
import math
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from scripts.run_candidate_c_measurement import (
    DEFAULT_ADR_PATH,
    CandidateCRunnerConfig,
    execute_candidate_c_measurement,
    format_text_report,
    get_git_environment,
    load_direct_d1_datasets,
    main,
    save_measurement_artifacts,
    verify_adr_preregistration,
)

from fxlab.data.dukascopy_direct_d1 import (
    DIRECT_D1_NORMALIZATION_VERSION,
    DIRECT_D1_PROVIDER_ID,
    DIRECT_D1_PROVIDER_VERSION,
    DIRECT_D1_SOURCE_REFERENCE,
    DIRECT_D1_VOLUME_SEMANTICS,
)
from fxlab.data.provider import (
    BarDataset,
    BarQuery,
    CanonicalInstrument,
    DataProvenance,
    ProvenanceQuality,
    ProviderFailure,
    ProviderFailureCategory,
    bar_content_hash,
    dataset_identity,
)
from fxlab.research.candidate_c_execution_evidence import (
    CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
    CandidateCExecutionEvidenceManifest,
    CandidateCExecutionState,
    _make_record,
    _manifest_payload,
    _sha,
)
from fxlab.research.candidate_c_measurement import (
    CANDIDATE_C_ADR_SHA256,
    CANDIDATE_C_END,
    CANDIDATE_C_PAIRS,
    CANDIDATE_C_START,
    CandidateCCodeEnvironment,
    CandidateCDecision,
    CandidateCMeasurementResult,
    CandidateCMetrics,
    CandidateCSplitResult,
)


def _synthetic_dataset(pair: str, pair_index: int) -> BarDataset:
    start = CANDIDATE_C_START
    end = CANDIDATE_C_END
    index = pd.date_range(start, end, inclusive="left", freq="D", tz="UTC")
    steps = np.arange(len(index), dtype=np.float64)
    close = 1.0 + pair_index * 0.1 + steps * (pair_index + 1) * 0.000001
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.where(steps == 10, 0.0, 1.0),
        },
        index=index,
        dtype="float64",
    )
    frame.index.name = "ts_open"
    frame.attrs = {"symbol": pair, "timeframe": "D1"}
    query = BarQuery(CanonicalInstrument(pair), "D1", start, end, end)
    content = bar_content_hash(frame)
    provenance = DataProvenance(
        provider_id=DIRECT_D1_PROVIDER_ID,
        provider_version=DIRECT_D1_PROVIDER_VERSION,
        normalization_version=DIRECT_D1_NORMALIZATION_VERSION,
        canonical_symbol=pair,
        provider_symbol=pair,
        timeframe="D1",
        query_start=start,
        query_end=end,
        query_as_of=end,
        retrieved_at=end,
        actual_first_observation=index[0].to_pydatetime(),
        actual_last_observation=index[-1].to_pydatetime(),
        row_count=len(frame),
        content_hash=content,
        query_fingerprint=query.fingerprint,
        dataset_id=dataset_identity(
            DIRECT_D1_PROVIDER_ID, DIRECT_D1_PROVIDER_VERSION, query.fingerprint, content
        ),
        revision=hashlib.sha256(f"revision-{pair}".encode()).hexdigest(),
        source_timezone="UTC",
        volume_semantics=DIRECT_D1_VOLUME_SEMANTICS,
        provenance_quality=ProvenanceQuality.VERIFIED,
        sanitized_source_reference=DIRECT_D1_SOURCE_REFERENCE,
    )
    return BarDataset(query, frame, provenance)


def _synthetic_manifest() -> CandidateCExecutionEvidenceManifest:
    start = CANDIDATE_C_START
    end = CANDIDATE_C_END
    records = []
    for pair_index, pair in enumerate(CANDIDATE_C_PAIRS):
        boundary = start
        while boundary < end:
            day = (boundary - start).days
            center = (110.0 if pair == "USDJPY" else 1.0 + pair_index * 0.1) + (
                math.sin(day / 11.0) * (0.01 if pair == "USDJPY" else 0.001)
            )
            spread = 0.02 if pair == "USDJPY" else 0.0002
            records.append(
                _make_record(
                    pair=pair,
                    boundary=boundary,
                    state=CandidateCExecutionState.AVAILABLE,
                    partition_state="present_staged",
                    raw_sha256="1" * 64,
                    raw_byte_count=120,
                    selected_tick_offset_ms=500,
                    selected_tick_timestamp=boundary,
                    bid=center - spread / 2,
                    ask=center + spread / 2,
                    bid_volume=1.0,
                    ask_volume=1.0,
                )
            )
            boundary += pd.Timedelta(days=1).to_pytimedelta()
    ordered_records = tuple(records)
    state_counts = (("available", len(ordered_records)),)
    payload = _manifest_payload(
        start, end, CANDIDATE_C_PAIRS, ordered_records, state_counts
    )
    return CandidateCExecutionEvidenceManifest(
        schema_version=CANDIDATE_C_EXECUTION_MANIFEST_SCHEMA,
        start=start,
        end=end,
        pairs=CANDIDATE_C_PAIRS,
        records=ordered_records,
        state_counts=state_counts,
        total_count=len(ordered_records),
        available_count=len(ordered_records),
        manifest_id=_sha(payload),
    )


def _sample_metrics() -> CandidateCMetrics:
    return CandidateCMetrics(
        trade_count=100,
        cohort_count=25,
        non_entry_reason_counts=(),
        gross_expectancy=0.002,
        net_expectancy=0.0015,
        annualized_return=0.08,
        sharpe=1.2,
        max_drawdown=0.04,
        cost_drag_expectancy=0.0005,
        cost_drag_annualized=0.01,
    )


def _sample_split_result() -> CandidateCSplitResult:
    m = _sample_metrics()
    return CandidateCSplitResult(
        headline=m,
        stress=m,
        headline_lcb=0.0002,
        stress_lcb=0.0001,
        yearly_headline=((2022, 0.001), (2023, 0.002)),
        yearly_stress=((2022, 0.0005), (2023, 0.001)),
        pair_headline=tuple((pair, 0.001) for pair in CANDIDATE_C_PAIRS),
        pair_stress=tuple((pair, 0.0005) for pair in CANDIDATE_C_PAIRS),
    )


# 1. CLI Help
def test_cli_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "Candidate C" in out
    assert "--run" in out


# 2. CLI No Args Exits Zero Without Run
def test_cli_no_args_exits_zero_without_run(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Use --run to execute" in out


# 3. CLI Unknown Arg Exits 2
def test_cli_unknown_arg_exits_two() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--invalid-argument"])
    assert exc_info.value.code == 2


# 4. get_git_environment Success
def test_get_git_environment_success() -> None:
    fake_commit = "a" * 40
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(stdout=f"{fake_commit}\n"),
            MagicMock(stdout=""),
        ]
        env = get_git_environment()
        assert env.commit == fake_commit
        assert env.worktree_clean is True


# 5. get_git_environment Dirty Worktree
def test_get_git_environment_dirty_worktree() -> None:
    fake_commit = "b" * 40
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(stdout=f"{fake_commit}\n"),
            MagicMock(stdout=" M file.py\n"),
        ]
        env = get_git_environment()
        assert env.commit == fake_commit
        assert env.worktree_clean is False


# 6. get_git_environment Subprocess Error
def test_get_git_environment_subprocess_error() -> None:
    with patch("subprocess.run", side_effect=subprocess.SubprocessError("git error")):
        with pytest.raises(RuntimeError, match="Failed to query git environment"):
            get_git_environment()


# 7. verify_adr_preregistration Success
def test_verify_adr_preregistration_success(tmp_path: Path) -> None:
    adr_file = tmp_path / "0008-adr.md"
    real_adr = Path(DEFAULT_ADR_PATH)
    if real_adr.is_file():
        adr_file.write_bytes(real_adr.read_bytes())
        verify_adr_preregistration(adr_file)
    else:
        with patch.object(Path, "read_bytes", return_value=b"correct"):
            with patch("hashlib.sha256") as mock_sha:
                mock_sha.return_value.hexdigest.return_value = CANDIDATE_C_ADR_SHA256
                verify_adr_preregistration(adr_file)


# 8. verify_adr_preregistration Missing File
def test_verify_adr_preregistration_missing_file(tmp_path: Path) -> None:
    non_existent = tmp_path / "missing.md"
    with pytest.raises(FileNotFoundError, match="not found"):
        verify_adr_preregistration(non_existent)


# 9. verify_adr_preregistration Hash Mismatch
def test_verify_adr_preregistration_hash_mismatch(tmp_path: Path) -> None:
    bad_adr = tmp_path / "bad.md"
    bad_adr.write_text("corrupted content", encoding="utf-8")
    with pytest.raises(ValueError, match="ADR 0008 SHA256 mismatch"):
        verify_adr_preregistration(bad_adr)


# 10. load_direct_d1_datasets Missing Dir
def test_load_direct_d1_datasets_missing_dir(tmp_path: Path) -> None:
    missing_dir = tmp_path / "no_such_dir"
    with pytest.raises(FileNotFoundError, match="not found"):
        load_direct_d1_datasets(missing_dir)


# 11. load_direct_d1_datasets Provider Failure
def test_load_direct_d1_datasets_provider_failure(tmp_path: Path) -> None:
    d1_dir = tmp_path / "d1_dir"
    d1_dir.mkdir()
    with patch(
        "scripts.run_candidate_c_measurement.DukascopyDirectD1HistoricalBarsProvider.fetch_bars"
    ) as mock_fetch:
        mock_fetch.return_value = ProviderFailure(
            ProviderFailureCategory.INVALID_DATA, "corrupt", DIRECT_D1_PROVIDER_ID
        )
        with pytest.raises(RuntimeError, match="Direct-D1 fetch failed"):
            load_direct_d1_datasets(d1_dir)


# 12. load_direct_d1_datasets Success
def test_load_direct_d1_datasets_success(tmp_path: Path) -> None:
    d1_dir = tmp_path / "d1_dir"
    d1_dir.mkdir()
    synthetic_ds = {
        pair: _synthetic_dataset(pair, idx) for idx, pair in enumerate(CANDIDATE_C_PAIRS)
    }
    with patch(
        "scripts.run_candidate_c_measurement.DukascopyDirectD1HistoricalBarsProvider.fetch_bars"
    ) as mock_fetch:
        mock_fetch.side_effect = lambda query: synthetic_ds[query.instrument.symbol]
        datasets = load_direct_d1_datasets(d1_dir)
        assert set(datasets.keys()) == set(CANDIDATE_C_PAIRS)
        assert len(datasets) == 7


# 13. format_text_report GO
def test_format_text_report_go() -> None:
    split = _sample_split_result()
    res = CandidateCMeasurementResult(
        schema="candidate_c_measurement_result.v1",
        policy_id="candidate_c_policy.v1",
        run_id="cand_c_run_123",
        decision=CandidateCDecision.GO,
        decision_meaning="Preregistered statistical and risk thresholds satisfied.",
        reasons=("All thresholds met.",),
        train=split,
        validation=split,
        execution_state_counts=(("available", 100),),
        result_id="e" * 64,
    )
    report = format_text_report(res)
    assert "cand_c_run_123" in report
    assert "GO" in report
    assert "Train Split Metrics" in report
    assert "Validation Split Metrics" in report
    assert "Sharpe Ratio (annualized): 1.2000" in report


# 14. format_text_report NO_GO
def test_format_text_report_no_go() -> None:
    split = _sample_split_result()
    res = CandidateCMeasurementResult(
        schema="candidate_c_measurement_result.v1",
        policy_id="candidate_c_policy.v1",
        run_id="cand_c_run_456",
        decision=CandidateCDecision.NO_GO,
        decision_meaning="Failed to meet preregistered criteria.",
        reasons=("validation_sharpe_below_threshold",),
        train=split,
        validation=split,
        execution_state_counts=(("available", 100),),
        result_id="f" * 64,
    )
    report = format_text_report(res)
    assert "NO_GO" in report
    assert "validation_sharpe_below_threshold" in report


# 15. format_text_report NOT_EVALUABLE
def test_format_text_report_not_evaluable() -> None:
    res = CandidateCMeasurementResult(
        schema="candidate_c_measurement_result.v1",
        policy_id="candidate_c_policy.v1",
        run_id="cand_c_run_789",
        decision=CandidateCDecision.NOT_EVALUABLE,
        decision_meaning="Measurement could not complete.",
        reasons=("missing_local_partition",),
        train=None,
        validation=None,
        execution_state_counts=(("missing_local_partition", 1),),
        result_id="0" * 64,
    )
    report = format_text_report(res)
    assert "NOT_EVALUABLE" in report
    assert "missing_local_partition" in report
    assert "Train Split Metrics" not in report


# 16. save_measurement_artifacts Creates Files
def test_save_measurement_artifacts_creates_files(tmp_path: Path) -> None:
    split = _sample_split_result()
    res = CandidateCMeasurementResult(
        schema="candidate_c_measurement_result.v1",
        policy_id="candidate_c_policy.v1",
        run_id="cand_c_run_test_save",
        decision=CandidateCDecision.GO,
        decision_meaning="Success",
        reasons=("ok",),
        train=split,
        validation=split,
        execution_state_counts=(("available", 10),),
        result_id="a" * 64,
    )
    results_root = tmp_path / "results"
    out_dir = save_measurement_artifacts(res, results_root)
    assert out_dir.is_dir()
    assert (out_dir / "candidate_c_measurement_result.json").is_file()
    assert (out_dir / "candidate_c_report.txt").is_file()


# 17. save_measurement_artifacts Fails If Exists
def test_save_measurement_artifacts_fails_if_exists(tmp_path: Path) -> None:
    split = _sample_split_result()
    res = CandidateCMeasurementResult(
        schema="candidate_c_measurement_result.v1",
        policy_id="candidate_c_policy.v1",
        run_id="cand_c_run_conflict",
        decision=CandidateCDecision.GO,
        decision_meaning="Success",
        reasons=("ok",),
        train=split,
        validation=split,
        execution_state_counts=(("available", 10),),
        result_id="a" * 64,
    )
    results_root = tmp_path / "results"
    (results_root / "cand_c_run_conflict").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        save_measurement_artifacts(res, results_root)


# 18. execute_candidate_c_measurement Rejects Dirty Worktree
def test_execute_candidate_c_measurement_rejects_dirty_worktree(tmp_path: Path) -> None:
    config = CandidateCRunnerConfig(
        results_root=tmp_path / "results",
    )
    with patch("scripts.run_candidate_c_measurement.verify_adr_preregistration"):
        with patch(
            "scripts.run_candidate_c_measurement.get_git_environment",
            return_value=CandidateCCodeEnvironment("a" * 40, worktree_clean=False),
        ):
            with pytest.raises(RuntimeError, match="Git working tree is dirty"):
                execute_candidate_c_measurement(config)


# 19. execute_candidate_c_measurement Rejects ADR Mismatch
def test_execute_candidate_c_measurement_rejects_adr_mismatch(tmp_path: Path) -> None:
    config = CandidateCRunnerConfig(
        results_root=tmp_path / "results",
    )
    with patch(
        "scripts.run_candidate_c_measurement.verify_adr_preregistration",
        side_effect=ValueError("ADR mismatch"),
    ):
        with pytest.raises(ValueError, match="ADR mismatch"):
            execute_candidate_c_measurement(config)


# 20. execute_candidate_c_measurement End-to-End Synthetic
def test_execute_candidate_c_measurement_end_to_end_synthetic(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    config = CandidateCRunnerConfig(
        results_root=results_root,
    )
    synthetic_ds = {
        pair: _synthetic_dataset(pair, idx) for idx, pair in enumerate(CANDIDATE_C_PAIRS)
    }
    manifest = _synthetic_manifest()
    clean_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    with patch("scripts.run_candidate_c_measurement.verify_adr_preregistration"):
        with patch(
            "scripts.run_candidate_c_measurement.get_git_environment", return_value=clean_env
        ):
            with patch(
                "scripts.run_candidate_c_measurement.load_direct_d1_datasets",
                return_value=synthetic_ds,
            ):
                with patch(
                    "scripts.run_candidate_c_measurement.build_candidate_c_execution_evidence_manifest",
                    return_value=manifest,
                ):
                    result = execute_candidate_c_measurement(config)
                    assert isinstance(result, CandidateCMeasurementResult)
                    assert (results_root / result.run_id).is_dir()
                    assert (
                        results_root / result.run_id / "candidate_c_measurement_result.json"
                    ).is_file()
                    assert (
                        results_root / result.run_id / "candidate_c_report.txt"
                    ).is_file()


# 21. main Run Success
def test_main_run_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results_root = tmp_path / "results"
    synthetic_ds = {
        pair: _synthetic_dataset(pair, idx) for idx, pair in enumerate(CANDIDATE_C_PAIRS)
    }
    manifest = _synthetic_manifest()
    clean_env = CandidateCCodeEnvironment("a" * 40, worktree_clean=True)

    with patch("scripts.run_candidate_c_measurement.verify_adr_preregistration"):
        with patch(
            "scripts.run_candidate_c_measurement.get_git_environment", return_value=clean_env
        ):
            with patch(
                "scripts.run_candidate_c_measurement.load_direct_d1_datasets",
                return_value=synthetic_ds,
            ):
                with patch(
                    "scripts.run_candidate_c_measurement.build_candidate_c_execution_evidence_manifest",
                    return_value=manifest,
                ):
                    code = main(["--run", "--results-root", str(results_root)])
                    assert code == 0
                    out = capsys.readouterr().out
                    assert "Candidate C Measurement Completed:" in out
                    assert "Run ID:" in out


# 22. main Run Error Returns 1
def test_main_run_error_returns_one(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "scripts.run_candidate_c_measurement.execute_candidate_c_measurement",
        side_effect=RuntimeError("Test error"),
    ):
        code = main(["--run"])
        assert code == 1
        err = capsys.readouterr().err
        assert "ERROR: Candidate C measurement failed: Test error" in err


# 23. Zero Trade Mutation Authority
def test_zero_trade_mutation_authority() -> None:
    import scripts.run_candidate_c_measurement as runner_mod

    forbidden = {
        "order_send",
        "send_order",
        "close_position",
        "trade_request",
        "MetaTrader5",
        "mt5",
        "Mt5DemoObservationService",
    }
    present = set(dir(runner_mod)).intersection(forbidden)
    assert not present, f"Forbidden trade mutation authority present: {present}"


# 24. Zero Network Calls
def test_zero_network_calls() -> None:
    forbidden_modules = {"requests", "httpx", "aiohttp"}
    imported = set(sys.modules.keys()).intersection(forbidden_modules)
    assert not imported, f"Forbidden network modules loaded: {imported}"


# 25. Boundary Enforcement 2014-2024
def test_boundary_enforcement_2014_2024() -> None:
    assert CANDIDATE_C_START == datetime(2014, 1, 1, tzinfo=UTC)
    assert CANDIDATE_C_END == datetime(2024, 1, 1, tzinfo=UTC)


# 26. Canonical Seven Pairs Enforced
def test_canonical_seven_pairs_enforced() -> None:
    expected = ("AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY")
    assert CANDIDATE_C_PAIRS == expected
