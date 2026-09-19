#!/usr/bin/env python3
"""Fail-closed real-data runner for frozen Candidate D v1.

The runner has local filesystem authority only. It deliberately imports no HTTP,
broker, MT5, or execution surface. Real execution requires an explicit protocol,
``--run``, an exact preregistration hash, and a clean Git worktree.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fxlab.data.dukascopy_direct_d1 import (
    DukascopyDirectD1DirectoryTransport,
    DukascopyDirectD1HistoricalBarsProvider,
)
from fxlab.data.policy_rates import canonical_json, canonical_sha256
from fxlab.data.provider import BarDataset, BarQuery, CanonicalInstrument, ProviderFailure
from fxlab.research.candidate_c_execution_evidence import (
    CandidateCExecutionEvidenceManifest,
    build_candidate_c_execution_evidence_manifest,
)
from fxlab.research.candidate_d_measurement import (
    CANDIDATE_D_ADR_SHA256,
    CANDIDATE_D_END,
    CANDIDATE_D_PAIRS,
    CANDIDATE_D_PROTOCOL_ID,
    CANDIDATE_D_START,
    CandidateDCodeEnvironment,
    CandidateDMeasurementResult,
    CandidateDSplitResult,
    measure_candidate_d,
)

DEFAULT_DIRECT_D1_ROOT = Path("E:/jarvis-data/raw_dukascopy_direct_d1")
DEFAULT_EXECUTION_EVIDENCE_ROOT = Path(
    "E:/jarvis-data/candidate_c_execution_2014_2023"
)
DEFAULT_RESULTS_ROOT = Path("E:/jarvis-data/candidate-d-results")
DEFAULT_ADR_PATH = Path(
    "docs/adr/0011-candidate-d-time-series-momentum-preregistration.md"
)
RESULT_FILENAME = "candidate_d_measurement_result.json"


def validate_candidate_d_scope(
    start: datetime,
    end: datetime,
    pairs: Sequence[str],
) -> None:
    """Require the one frozen, canonical, sealed Candidate D input scope."""
    if start != CANDIDATE_D_START or end != CANDIDATE_D_END:
        raise ValueError("Candidate D sealed research scope must be exactly [2014, 2024)")
    if tuple(pairs) != CANDIDATE_D_PAIRS:
        raise ValueError("Candidate D pairs must use exact canonical seven-pair order")


def get_git_environment(cwd: Path | str | None = None) -> CandidateDCodeEnvironment:
    """Return the exact HEAD and whether tracked, staged, and untracked state is clean."""
    repo_dir = Path(cwd) if cwd is not None else Path.cwd()
    try:
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        if status_result.stdout.strip():
            raise RuntimeError("Candidate D real measurement rejected: Git worktree is dirty")
        commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
    except RuntimeError:
        raise
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Candidate D Git environment is unavailable") from exc
    return CandidateDCodeEnvironment(
        commit=commit_result.stdout.strip(),
        worktree_clean=True,
    )


def verify_adr_preregistration(adr_path: Path | str = DEFAULT_ADR_PATH) -> None:
    """Verify exact frozen ADR 0011 bytes before touching research evidence."""
    path = Path(adr_path)
    if not path.is_file():
        raise FileNotFoundError("Candidate D frozen ADR 0011 is unavailable")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != CANDIDATE_D_ADR_SHA256:
        raise ValueError(
            f"Candidate D ADR SHA256 mismatch: expected {CANDIDATE_D_ADR_SHA256}, "
            f"got {actual}"
        )


def load_direct_d1_datasets(
    root: Path | str,
    *,
    start: datetime = CANDIDATE_D_START,
    end: datetime = CANDIDATE_D_END,
    pairs: Sequence[str] = CANDIDATE_D_PAIRS,
) -> dict[str, BarDataset]:
    """Load exactly the seven sealed Direct-D1 datasets using the offline transport."""
    validate_candidate_d_scope(start, end, pairs)
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError("Candidate D Direct-D1 root is unavailable")
    provider = DukascopyDirectD1HistoricalBarsProvider(
        DukascopyDirectD1DirectoryTransport(root_path)
    )
    datasets: dict[str, BarDataset] = {}
    for pair in CANDIDATE_D_PAIRS:
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
                f"Candidate D Direct-D1 evidence invalid for {pair}: "
                f"{result.reason} ({result.category.value})"
            )
        datasets[pair] = result
    return datasets


def load_execution_manifest(
    root: Path | str,
    *,
    start: datetime = CANDIDATE_D_START,
    end: datetime = CANDIDATE_D_END,
    pairs: Sequence[str] = CANDIDATE_D_PAIRS,
) -> CandidateCExecutionEvidenceManifest:
    """Build Candidate D's deterministic manifest from local 00h evidence only."""
    validate_candidate_d_scope(start, end, pairs)
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError("Candidate D execution-evidence root is unavailable")
    return build_candidate_c_execution_evidence_manifest(
        root=root_path,
        start=start,
        end=end,
        pairs=CANDIDATE_D_PAIRS,
    )


@dataclass(frozen=True)
class CandidateDResultArtifact:
    schema: str
    protocol_id: str
    run_id: str
    result_id: str
    measurement_result_id: str
    policy_id: str
    code_revision: str
    adr_sha256: str
    decision: str
    decision_reasons: tuple[str, ...]
    dataset_identities: tuple[tuple[object, ...], ...]
    execution_manifest_id: str
    execution_state_counts: tuple[tuple[str, int], ...]
    train: CandidateDSplitResult | None
    validation: CandidateDSplitResult | None
    integrity_status: tuple[str, ...]


def _dataset_identities(datasets: Mapping[str, object]) -> tuple[tuple[object, ...], ...]:
    if tuple(datasets) != CANDIDATE_D_PAIRS:
        raise ValueError("Candidate D datasets must use canonical seven-pair order")
    identities: list[tuple[object, ...]] = []
    for pair in CANDIDATE_D_PAIRS:
        provenance = getattr(datasets[pair], "provenance", None)
        if provenance is None:
            raise ValueError("Candidate D dataset provenance is unavailable")
        identities.append(
            (
                pair,
                provenance.dataset_id,
                provenance.revision,
                provenance.content_hash,
                provenance.query_fingerprint,
                provenance.provider_id,
                provenance.provider_version,
                provenance.normalization_version,
            )
        )
    return tuple(identities)


def _artifact_payload(artifact: CandidateDResultArtifact) -> dict[str, object]:
    return {
        "schema": artifact.schema,
        "protocol_id": artifact.protocol_id,
        "run_id": artifact.run_id,
        "measurement_result_id": artifact.measurement_result_id,
        "policy_id": artifact.policy_id,
        "code_revision": artifact.code_revision,
        "adr_sha256": artifact.adr_sha256,
        "decision": artifact.decision,
        "decision_reasons": artifact.decision_reasons,
        "dataset_identities": artifact.dataset_identities,
        "execution_manifest_id": artifact.execution_manifest_id,
        "execution_state_counts": artifact.execution_state_counts,
        "train": artifact.train,
        "validation": artifact.validation,
        "integrity_status": artifact.integrity_status,
    }


def build_result_artifact(
    measurement: CandidateDMeasurementResult,
    datasets: Mapping[str, object],
    execution_manifest: object,
    code_environment: CandidateDCodeEnvironment,
) -> CandidateDResultArtifact:
    """Bind the measured result to its decision-relevant immutable input identities."""
    if not code_environment.worktree_clean:
        raise ValueError("Candidate D result requires a clean worktree")
    manifest_id = getattr(execution_manifest, "manifest_id", None)
    state_counts = getattr(execution_manifest, "state_counts", None)
    if not isinstance(manifest_id, str) or len(manifest_id) != 64:
        raise ValueError("Candidate D execution manifest identity is invalid")
    if not isinstance(state_counts, tuple):
        raise ValueError("Candidate D execution state counts are invalid")
    values = {
        "schema": "candidate_d_real_measurement_result.v1",
        "protocol_id": CANDIDATE_D_PROTOCOL_ID,
        "run_id": measurement.run_id,
        "result_id": "",
        "measurement_result_id": measurement.result_id,
        "policy_id": measurement.policy_id,
        "code_revision": code_environment.commit,
        "adr_sha256": CANDIDATE_D_ADR_SHA256,
        "decision": measurement.decision_meaning,
        "decision_reasons": measurement.reasons,
        "dataset_identities": _dataset_identities(datasets),
        "execution_manifest_id": manifest_id,
        "execution_state_counts": state_counts,
        "train": measurement.train,
        "validation": measurement.validation,
        "integrity_status": (
            "adr_sha256_verified",
            "clean_worktree_verified",
            "sealed_window_enforced",
            "engine_chronology_verified",
        ),
    }
    provisional = CandidateDResultArtifact(**values)
    values["result_id"] = canonical_sha256(_artifact_payload(provisional))
    return CandidateDResultArtifact(**values)


def canonical_result_artifact(artifact: CandidateDResultArtifact) -> bytes:
    """Return the canonical, deterministic persisted result representation."""
    expected = canonical_sha256(_artifact_payload(artifact))
    if artifact.result_id != expected:
        raise ValueError("Candidate D result identity does not match canonical content")
    return canonical_json(artifact).encode("utf-8")


def _verify_existing_result(directory: Path, expected: bytes) -> None:
    expected_names = {RESULT_FILENAME}
    if not directory.is_dir() or {item.name for item in directory.iterdir()} != expected_names:
        raise FileExistsError("Candidate D result directory conflict")
    if (directory / RESULT_FILENAME).read_bytes() != expected:
        raise FileExistsError("Candidate D result directory conflict")


def save_result_artifact(
    artifact: CandidateDResultArtifact,
    results_root: Path | str,
) -> Path:
    """Atomically publish an immutable run directory, accepting only exact duplicates."""
    root = Path(results_root)
    root.mkdir(parents=True, exist_ok=True)
    final_directory = root / artifact.run_id
    content = canonical_result_artifact(artifact)
    if final_directory.exists():
        _verify_existing_result(final_directory, content)
        return final_directory

    staging = Path(tempfile.mkdtemp(prefix=f".{artifact.run_id}.", dir=root))
    try:
        result_path = staging / RESULT_FILENAME
        with result_path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            staging.rename(final_directory)
        except FileExistsError:
            _verify_existing_result(final_directory, content)
        return final_directory
    finally:
        if staging.exists():
            shutil.rmtree(staging)


@dataclass(frozen=True)
class CandidateDRunnerConfig:
    protocol: str | None = None
    direct_d1_root: Path = field(default=DEFAULT_DIRECT_D1_ROOT)
    execution_evidence_root: Path = field(default=DEFAULT_EXECUTION_EVIDENCE_ROOT)
    results_root: Path = field(default=DEFAULT_RESULTS_ROOT)
    adr_path: Path = field(default=DEFAULT_ADR_PATH)
    repo_root: Path | None = None


def execute_candidate_d_measurement(config: CandidateDRunnerConfig) -> CandidateDResultArtifact:
    """Run the guarded local-evidence orchestration and publish its canonical result."""
    if config.protocol != CANDIDATE_D_PROTOCOL_ID:
        raise ValueError("Candidate D requires the exact frozen protocol")
    verify_adr_preregistration(config.adr_path)
    code_environment = get_git_environment(config.repo_root)
    if not code_environment.worktree_clean:
        raise RuntimeError("Candidate D real measurement rejected: Git worktree is dirty")
    validate_candidate_d_scope(CANDIDATE_D_START, CANDIDATE_D_END, CANDIDATE_D_PAIRS)

    datasets = load_direct_d1_datasets(config.direct_d1_root)
    execution_manifest = load_execution_manifest(config.execution_evidence_root)
    measurement = measure_candidate_d(
        datasets=datasets,
        execution_manifest=execution_manifest,
        code_environment=code_environment,
    )
    artifact = build_result_artifact(
        measurement,
        datasets,
        execution_manifest,
        code_environment,
    )
    save_result_artifact(artifact, config.results_root)
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FXLab Candidate D v1 measurement runner")
    parser.add_argument("--run", action="store_true", help="Execute against sealed local data")
    parser.add_argument("--protocol", default=None, help="Exact frozen Candidate D protocol ID")
    parser.add_argument("--direct-d1-root", type=Path, default=DEFAULT_DIRECT_D1_ROOT)
    parser.add_argument(
        "--execution-evidence-root",
        type=Path,
        default=DEFAULT_EXECUTION_EVIDENCE_ROOT,
    )
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--adr-path", type=Path, default=DEFAULT_ADR_PATH)
    args = parser.parse_args(argv)

    if not args.run:
        print("Candidate D v1 measurement runner: no measurement performed.")
        print(f"Use --run --protocol {CANDIDATE_D_PROTOCOL_ID} for explicit execution.")
        return 0
    if args.protocol != CANDIDATE_D_PROTOCOL_ID:
        print("ERROR: Candidate D requires the exact frozen protocol", file=sys.stderr)
        return 1

    config = CandidateDRunnerConfig(
        protocol=args.protocol,
        direct_d1_root=args.direct_d1_root,
        execution_evidence_root=args.execution_evidence_root,
        results_root=args.results_root,
        adr_path=args.adr_path,
    )
    try:
        artifact = execute_candidate_d_measurement(config)
    except Exception as exc:
        print(f"ERROR: Candidate D measurement failed: {exc}", file=sys.stderr)
        return 1

    print(f"Decision: {artifact.decision}")
    print(f"Run ID: {artifact.run_id}")
    print(f"Result ID: {artifact.result_id}")
    if artifact.decision_reasons:
        print("Reasons:")
        for reason in artifact.decision_reasons:
            print(f"- {reason}")
    if artifact.decision == "GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST":
        print(
            "GO does not authorize 2024 access, ML, MT5 deployment, live trading, "
            "or real-money trading."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
