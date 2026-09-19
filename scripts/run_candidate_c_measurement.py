#!/usr/bin/env python3
"""Runner for Candidate C Real-Data Measurement (v1 and v2).

Orchestrates the frozen Candidate C measurement engine against validated local evidence.
Enforces research boundaries, Git worktree cleanliness, and preregistration ADR integrity.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fxlab.data.dukascopy_direct_d1 import (
    DukascopyDirectD1DirectoryTransport,
    DukascopyDirectD1HistoricalBarsProvider,
)
from fxlab.data.provider import BarDataset, BarQuery, CanonicalInstrument, ProviderFailure
from fxlab.research.candidate_c_execution_evidence import (
    build_candidate_c_execution_evidence_manifest,
)
from fxlab.research.candidate_c_measurement import (
    CANDIDATE_C_ADR_SHA256,
    CANDIDATE_C_END,
    CANDIDATE_C_PAIRS,
    CANDIDATE_C_START,
    CANDIDATE_C_V2_ADR_SHA256,
    CandidateCCodeEnvironment,
    CandidateCMeasurementResult,
    canonical_candidate_c_result,
    measure_candidate_c,
)

DEFAULT_DIRECT_D1_ROOT = Path("E:/jarvis-data/raw_dukascopy_direct_d1")
DEFAULT_EXECUTION_EVIDENCE_ROOT = Path("E:/jarvis-data/candidate_c_execution_2014_2023")
DEFAULT_RESULTS_ROOT = Path("E:/jarvis-data/candidate-c-results")
DEFAULT_V1_ADR_PATH = Path("docs/adr/0008-candidate-c-cross-sectional-reversal-preregistration.md")
DEFAULT_V2_ADR_PATH = Path("docs/adr/0009-candidate-c-v2-tradable-session-exit-preregistration.md")
DEFAULT_ADR_PATH = DEFAULT_V2_ADR_PATH


def get_git_environment(cwd: Path | str | None = None) -> CandidateCCodeEnvironment:
    """Get Git commit hash and verify worktree is clean."""
    repo_dir = Path(cwd) if cwd is not None else Path.cwd()
    try:
        commit_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        commit = commit_res.stdout.strip()
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        is_clean = len(status_res.stdout.strip()) == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"Failed to query git environment: {exc}") from exc

    return CandidateCCodeEnvironment(commit=commit, worktree_clean=is_clean)


def verify_adr_preregistration(
    adr_path: Path | str,
    protocol: str = "v2",
) -> None:
    """Verify that the ADR preregistration document matches the immutable hash."""
    path = Path(adr_path)
    if not path.is_file():
        adr_name = "0008" if protocol == "v1" else "0009"
        raise FileNotFoundError(f"ADR {adr_name} file not found at {path}")
    content = path.read_bytes()
    computed_sha = hashlib.sha256(content).hexdigest()
    expected_sha = CANDIDATE_C_ADR_SHA256 if protocol == "v1" else CANDIDATE_C_V2_ADR_SHA256
    adr_name = "0008" if protocol == "v1" else "0009"
    if computed_sha != expected_sha:
        raise ValueError(
            f"ADR {adr_name} SHA256 mismatch: expected {expected_sha}, got {computed_sha}"
        )


def load_direct_d1_datasets(
    root: Path | str,
    start: datetime = CANDIDATE_C_START,
    end: datetime = CANDIDATE_C_END,
    pairs: Sequence[str] = CANDIDATE_C_PAIRS,
) -> dict[str, BarDataset]:
    """Load validated Direct-D1 datasets from local disk."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"Direct-D1 root directory not found at {root_path}")
    transport = DukascopyDirectD1DirectoryTransport(root_path)
    provider = DukascopyDirectD1HistoricalBarsProvider(transport)
    datasets: dict[str, BarDataset] = {}
    for pair in pairs:
        query = BarQuery(
            instrument=CanonicalInstrument(pair),
            timeframe="D1",
            start=start,
            end=end,
            as_of=end,
        )
        result = provider.fetch_bars(query)
        if isinstance(result, ProviderFailure):
            raise RuntimeError(
                f"Direct-D1 fetch failed for {pair}: {result.reason} ({result.category.value})"
            )
        datasets[pair] = result
    return datasets


def format_text_report(result: CandidateCMeasurementResult) -> str:
    """Format human-readable Candidate C measurement report."""
    version_label = "v2" if "v2" in result.schema else "v1"
    lines = [
        "==================================================",
        f"FXLab — Candidate C {version_label} Measurement Report",
        "==================================================",
        f"Run ID:        {result.run_id}",
        f"Policy ID:     {result.policy_id}",
        f"Decision:      {result.decision.value}",
        f"Meaning:       {result.decision_meaning}",
        f"Result ID:     {result.result_id}",
        "",
        "Reasons:",
    ]
    for reason in result.reasons:
        lines.append(f"  - {reason}")
    lines.append("")
    lines.append("Execution State Counts:")
    for state, count in result.execution_state_counts:
        lines.append(f"  {state}: {count}")
    lines.append("")

    if result.train is not None:
        lines.append("--- Train Split Metrics ---")
        lines.append(f"  Sharpe Ratio (annualized): {result.train.headline.sharpe:.4f}")
        lines.append(f"  Annualized Net Return:     {result.train.headline.annualized_return:.4f}")
        lines.append(f"  Max Drawdown:              {result.train.headline.max_drawdown:.4f}")
        lines.append(f"  Net Expectancy:            {result.train.headline.net_expectancy:.6f}")
        lines.append(f"  Headline LCB:              {result.train.headline_lcb:.6f}")
        lines.append(f"  Stress Sharpe:             {result.train.stress.sharpe:.4f}")
        lines.append(f"  Stress Annualized Return:  {result.train.stress.annualized_return:.4f}")
        lines.append(f"  Stress Max Drawdown:       {result.train.stress.max_drawdown:.4f}")
        lines.append(f"  Stress LCB:                {result.train.stress_lcb:.6f}")
        lines.append("")

    if result.validation is not None:
        lines.append("--- Validation Split Metrics ---")
        lines.append(f"  Sharpe Ratio (annualized): {result.validation.headline.sharpe:.4f}")
        lines.append(
            f"  Annualized Net Return:     {result.validation.headline.annualized_return:.4f}"
        )
        lines.append(f"  Max Drawdown:              {result.validation.headline.max_drawdown:.4f}")
        lines.append(
            f"  Net Expectancy:            {result.validation.headline.net_expectancy:.6f}"
        )
        lines.append(f"  Headline LCB:              {result.validation.headline_lcb:.6f}")
        lines.append(f"  Stress Sharpe:             {result.validation.stress.sharpe:.4f}")
        lines.append(
            f"  Stress Annualized Return:  {result.validation.stress.annualized_return:.4f}"
        )
        lines.append(f"  Stress Max Drawdown:       {result.validation.stress.max_drawdown:.4f}")
        lines.append(f"  Stress LCB:                {result.validation.stress_lcb:.6f}")
        lines.append("")

    return "\n".join(lines) + "\n"


def save_measurement_artifacts(
    result: CandidateCMeasurementResult,
    results_root: Path | str,
) -> Path:
    """Save canonical JSON result and text report in a run_id directory."""
    out_dir = Path(results_root) / result.run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    json_path = out_dir / "candidate_c_measurement_result.json"
    report_path = out_dir / "candidate_c_report.txt"

    json_bytes = canonical_candidate_c_result(result)
    json_path.write_bytes(json_bytes)

    report_text = format_text_report(result)
    report_path.write_text(report_text, encoding="utf-8")

    return out_dir


@dataclass(frozen=True)
class CandidateCRunnerConfig:
    protocol: str | None = None
    direct_d1_root: Path = DEFAULT_DIRECT_D1_ROOT
    execution_evidence_root: Path = DEFAULT_EXECUTION_EVIDENCE_ROOT
    results_root: Path = DEFAULT_RESULTS_ROOT
    adr_path: Path | None = None
    repo_root: Path | None = None


def execute_candidate_c_measurement(config: CandidateCRunnerConfig) -> CandidateCMeasurementResult:
    """Execute preflight checks, evidence loading, measurement, and artifact persistence."""
    if config.protocol not in ("v1", "v2"):
        raise ValueError(
            "Candidate C measurement requires explicit protocol selection: --protocol {v1,v2}"
        )

    protocol_version = 1 if config.protocol == "v1" else 2
    adr_path = config.adr_path or (
        DEFAULT_V1_ADR_PATH if protocol_version == 1 else DEFAULT_V2_ADR_PATH
    )

    # 1. ADR Check
    verify_adr_preregistration(adr_path, protocol=config.protocol)

    # 2. Git Check
    code_env = get_git_environment(config.repo_root)
    if not code_env.worktree_clean:
        raise RuntimeError(
            "Git working tree is dirty. Candidate C measurement requires a clean repository."
        )

    # 3. Direct-D1 Datasets
    datasets = load_direct_d1_datasets(config.direct_d1_root)

    # 4. Execution Manifest
    manifest = build_candidate_c_execution_evidence_manifest(
        root=config.execution_evidence_root,
        start=CANDIDATE_C_START,
        end=CANDIDATE_C_END,
        pairs=CANDIDATE_C_PAIRS,
    )

    # 5. Measure
    result = measure_candidate_c(
        datasets=datasets,
        execution_manifest=manifest,
        code_environment=code_env,
        version=protocol_version,
    )

    # 6. Save Artifacts
    save_measurement_artifacts(result, config.results_root)

    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for Candidate C measurement runner."""
    parser = argparse.ArgumentParser(
        description="FXLab Candidate C Real-Data Measurement Runner",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Execute Candidate C measurement against local evidence.",
    )
    parser.add_argument(
        "--protocol",
        choices=["v1", "v2"],
        default=None,
        help="Candidate C preregistration protocol version (required with --run: v1 or v2)",
    )
    parser.add_argument(
        "--direct-d1-root",
        type=Path,
        default=DEFAULT_DIRECT_D1_ROOT,
        help=f"Directory for Dukascopy Direct-D1 bar data (default: {DEFAULT_DIRECT_D1_ROOT})",
    )
    parser.add_argument(
        "--execution-evidence-root",
        type=Path,
        default=DEFAULT_EXECUTION_EVIDENCE_ROOT,
        help=(
            "Directory for Candidate C hourly execution evidence "
            f"(default: {DEFAULT_EXECUTION_EVIDENCE_ROOT})"
        ),
    )
    parser.add_argument(
        "--results-root",
        type=Path,
        default=DEFAULT_RESULTS_ROOT,
        help=f"Directory to write measurement artifacts (default: {DEFAULT_RESULTS_ROOT})",
    )
    parser.add_argument(
        "--adr-path",
        type=Path,
        default=None,
        help="Path to preregistration ADR markdown (default: ADR 0008 for v1, ADR 0009 for v2)",
    )

    args = parser.parse_args(argv)

    if not args.run:
        print("Candidate C Real-Data Measurement Runner")
        print("Use --run with --protocol {v1,v2} to execute measurement against local evidence.")
        print("Use --help for options.")
        return 0

    if args.protocol is None:
        print(
            "ERROR: Candidate C measurement requires explicit protocol selection: "
            "--protocol {v1,v2}",
            file=sys.stderr,
        )
        return 1

    config = CandidateCRunnerConfig(
        protocol=args.protocol,
        direct_d1_root=args.direct_d1_root,
        execution_evidence_root=args.execution_evidence_root,
        results_root=args.results_root,
        adr_path=args.adr_path,
    )

    try:
        result = execute_candidate_c_measurement(config)
        print(f"Candidate C Measurement Completed: {result.decision.value}")
        print(f"Run ID: {result.run_id}")
        print(f"Result ID: {result.result_id}")
        return 0
    except Exception as exc:
        print(f"ERROR: Candidate C measurement failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
