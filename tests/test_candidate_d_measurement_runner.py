from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from scripts.run_candidate_d_measurement import (
    CandidateDRunnerConfig,
    build_result_artifact,
    canonical_result_artifact,
    execute_candidate_d_measurement,
    get_git_environment,
    load_direct_d1_datasets,
    load_execution_manifest,
    main,
    save_result_artifact,
    validate_candidate_d_scope,
    verify_adr_preregistration,
)

from fxlab.research.candidate_c_execution_evidence import CandidateCExecutionState
from fxlab.research.candidate_d_measurement import (
    CANDIDATE_D_ADR_SHA256,
    CANDIDATE_D_END,
    CANDIDATE_D_PAIRS,
    CANDIDATE_D_PROTOCOL_ID,
    CANDIDATE_D_START,
    CandidateDCodeEnvironment,
    CandidateDDecision,
    CandidateDMeasurementResult,
)


def _measurement(
    *,
    decision: CandidateDDecision = CandidateDDecision.NOT_EVALUABLE,
    reasons: tuple[str, ...] = ("missing_local_partition",),
) -> CandidateDMeasurementResult:
    return CandidateDMeasurementResult(
        schema="candidate_d_measurement_result.v1",
        policy_id="1" * 64,
        run_id="2" * 64,
        decision=decision,
        decision_meaning=(
            "GO_TO_SEPARATELY_AUTHORIZED_SEALED_TEST"
            if decision is CandidateDDecision.GO
            else decision.value
        ),
        reasons=reasons,
        train=None,
        validation=None,
        execution_state_counts=(("available", 1),),
        result_id="3" * 64,
    )


def _datasets(*, suffix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    for index, pair in enumerate(CANDIDATE_D_PAIRS):
        digest = hashlib.sha256(f"{pair}{suffix}".encode()).hexdigest()
        provenance = SimpleNamespace(
            dataset_id=digest,
            revision=f"revision:{digest}",
            content_hash=hashlib.sha256(f"content-{pair}{suffix}".encode()).hexdigest(),
            query_fingerprint=hashlib.sha256(f"query-{pair}{suffix}".encode()).hexdigest(),
            provider_id="dukascopy_direct_d1",
            provider_version="dukascopy_direct_d1_v1",
            normalization_version="dukascopy_direct_bid_d1_v1",
        )
        result[pair] = SimpleNamespace(provenance=provenance, marker=index)
    return result


def _manifest(*, suffix: str = "", state: str = "available") -> object:
    return SimpleNamespace(
        manifest_id=hashlib.sha256(f"manifest{suffix}".encode()).hexdigest(),
        state_counts=((state, 25_564),),
    )


def _clean_environment() -> CandidateDCodeEnvironment:
    return CandidateDCodeEnvironment("a" * 40, worktree_clean=True)


def test_cli_without_run_is_informational_and_does_not_measure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("scripts.run_candidate_d_measurement.execute_candidate_d_measurement") as execute:
        assert main([]) == 0
    execute.assert_not_called()
    assert "--run" in capsys.readouterr().out


@pytest.mark.parametrize("protocol", [None, "candidate_d_time_series_momentum.v2"])
def test_cli_requires_exact_frozen_protocol(
    protocol: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--run"] + ([] if protocol is None else ["--protocol", protocol])
    with patch("scripts.run_candidate_d_measurement.execute_candidate_d_measurement") as execute:
        assert main(argv) == 1
    execute.assert_not_called()
    assert "exact frozen protocol" in capsys.readouterr().err


def test_dirty_worktree_fails_before_local_evidence_is_touched(tmp_path: Path) -> None:
    config = CandidateDRunnerConfig(
        protocol=CANDIDATE_D_PROTOCOL_ID,
        direct_d1_root=tmp_path / "d1",
        execution_evidence_root=tmp_path / "execution",
        results_root=tmp_path / "results",
    )
    with patch("scripts.run_candidate_d_measurement.verify_adr_preregistration"):
        with patch(
            "scripts.run_candidate_d_measurement.get_git_environment",
            return_value=CandidateDCodeEnvironment("a" * 40, worktree_clean=False),
        ):
            with patch("scripts.run_candidate_d_measurement.load_direct_d1_datasets") as load:
                with pytest.raises(RuntimeError, match="dirty"):
                    execute_candidate_d_measurement(config)
    load.assert_not_called()


def test_git_environment_binds_clean_head() -> None:
    status = SimpleNamespace(stdout="")
    head = SimpleNamespace(stdout="a" * 40 + "\n")
    with patch("subprocess.run", side_effect=[status, head]) as run:
        environment = get_git_environment(Path("."))
    assert environment == CandidateDCodeEnvironment("a" * 40, True)
    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == ["git", "status", "--porcelain"]
    assert run.call_args_list[1].args[0] == ["git", "rev-parse", "HEAD"]


def test_dirty_git_state_is_rejected_without_resolving_head() -> None:
    dirty = SimpleNamespace(stdout="?? untracked.py\n")
    with patch("subprocess.run", return_value=dirty) as run:
        with pytest.raises(RuntimeError, match="dirty"):
            get_git_environment(Path("."))
    assert run.call_count == 1
    assert run.call_args.args[0] == ["git", "status", "--porcelain"]


def test_execute_orders_adr_and_clean_guard_before_scope_or_evidence(tmp_path: Path) -> None:
    events: list[str] = []
    config = CandidateDRunnerConfig(
        protocol=CANDIDATE_D_PROTOCOL_ID,
        direct_d1_root=tmp_path / "d1",
        execution_evidence_root=tmp_path / "execution",
        results_root=tmp_path / "results",
    )
    with patch(
        "scripts.run_candidate_d_measurement.verify_adr_preregistration",
        side_effect=lambda *_args: events.append("adr"),
    ):
        with patch(
            "scripts.run_candidate_d_measurement.get_git_environment",
            side_effect=lambda *_args: events.append("git")
            or CandidateDCodeEnvironment("a" * 40, False),
        ):
            with patch(
                "scripts.run_candidate_d_measurement.validate_candidate_d_scope",
                side_effect=lambda *_args: events.append("scope"),
            ):
                with patch(
                    "scripts.run_candidate_d_measurement.load_direct_d1_datasets"
                ) as load:
                    with pytest.raises(RuntimeError, match="dirty"):
                        execute_candidate_d_measurement(config)
    assert events == ["adr", "git"]
    load.assert_not_called()


def test_exact_adr_hash_is_required(tmp_path: Path) -> None:
    adr = tmp_path / "0011.md"
    adr.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        verify_adr_preregistration(adr)
    assert CANDIDATE_D_ADR_SHA256 == (
        "36a4fa7c2c8b88cce48b0dd0162177e991d5ca26a3a96599ec7cbc9bcb653e52"
    )


def test_scope_requires_exact_canonical_universe_and_sealed_window() -> None:
    validate_candidate_d_scope(CANDIDATE_D_START, CANDIDATE_D_END, CANDIDATE_D_PAIRS)
    with pytest.raises(ValueError, match="canonical"):
        validate_candidate_d_scope(
            CANDIDATE_D_START, CANDIDATE_D_END, tuple(reversed(CANDIDATE_D_PAIRS))
        )
    with pytest.raises(ValueError, match="canonical"):
        validate_candidate_d_scope(
            CANDIDATE_D_START, CANDIDATE_D_END, CANDIDATE_D_PAIRS[:-1]
        )


def test_2024_scope_rejected_before_d1_provider_is_constructed(tmp_path: Path) -> None:
    with patch(
        "scripts.run_candidate_d_measurement.DukascopyDirectD1DirectoryTransport"
    ) as transport:
        with pytest.raises(ValueError, match="sealed"):
            load_direct_d1_datasets(
                tmp_path,
                start=CANDIDATE_D_START,
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )
    transport.assert_not_called()


def test_2024_scope_rejected_before_execution_builder_is_touched(tmp_path: Path) -> None:
    with patch(
        "scripts.run_candidate_d_measurement.build_candidate_c_execution_evidence_manifest"
    ) as builder:
        with pytest.raises(ValueError, match="sealed"):
            load_execution_manifest(
                tmp_path,
                start=CANDIDATE_D_START,
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )
    builder.assert_not_called()


def test_direct_d1_loader_uses_exact_order_and_preserves_provider_dataset(
    tmp_path: Path,
) -> None:
    sentinel = {
        pair: SimpleNamespace(pair=pair, zero_volume_flat_row=True)
        for pair in CANDIDATE_D_PAIRS
    }

    def fetch(query: object) -> object:
        return sentinel[query.instrument.symbol]

    with patch(
        "scripts.run_candidate_d_measurement.DukascopyDirectD1DirectoryTransport"
    ):
        with patch(
            "scripts.run_candidate_d_measurement.DukascopyDirectD1HistoricalBarsProvider"
        ) as provider_type:
            provider_type.return_value.fetch_bars.side_effect = fetch
            loaded = load_direct_d1_datasets(tmp_path)
    assert tuple(loaded) == CANDIDATE_D_PAIRS
    assert loaded["AUDUSD"] is sentinel["AUDUSD"]
    assert loaded["AUDUSD"].zero_volume_flat_row is True


def test_malformed_direct_d1_provider_result_fails_closed(tmp_path: Path) -> None:
    failure = SimpleNamespace(category=SimpleNamespace(value="invalid_data"), reason="bad_row")
    with patch(
        "scripts.run_candidate_d_measurement.DukascopyDirectD1DirectoryTransport"
    ):
        with patch(
            "scripts.run_candidate_d_measurement.DukascopyDirectD1HistoricalBarsProvider"
        ) as provider_type:
            with patch("scripts.run_candidate_d_measurement.ProviderFailure", type(failure)):
                provider_type.return_value.fetch_bars.return_value = failure
                with pytest.raises(RuntimeError, match="bad_row"):
                    load_direct_d1_datasets(tmp_path)


def test_execution_adapter_preserves_typed_states_without_d1_substitution(tmp_path: Path) -> None:
    manifest = _manifest(state=CandidateCExecutionState.MISSING_LOCAL_PARTITION.value)
    with patch(
        "scripts.run_candidate_d_measurement.build_candidate_c_execution_evidence_manifest",
        return_value=manifest,
    ) as builder:
        assert load_execution_manifest(tmp_path) is manifest
    assert builder.call_args.kwargs["start"] == CANDIDATE_D_START
    assert builder.call_args.kwargs["end"] == CANDIDATE_D_END
    assert builder.call_args.kwargs["pairs"] == CANDIDATE_D_PAIRS


def test_result_artifact_identity_is_deterministic_and_semantic() -> None:
    first = build_result_artifact(_measurement(), _datasets(), _manifest(), _clean_environment())
    second = build_result_artifact(_measurement(), _datasets(), _manifest(), _clean_environment())
    changed_data = build_result_artifact(
        _measurement(), _datasets(suffix="changed"), _manifest(), _clean_environment()
    )
    changed_execution = build_result_artifact(
        _measurement(), _datasets(), _manifest(suffix="changed"), _clean_environment()
    )
    assert first == second
    assert first.result_id == second.result_id
    assert first.result_id != changed_data.result_id
    assert first.result_id != changed_execution.result_id
    assert first.run_id == _measurement().run_id
    assert first.code_revision == "a" * 40
    assert first.adr_sha256 == CANDIDATE_D_ADR_SHA256
    assert tuple(item[0] for item in first.dataset_identities) == CANDIDATE_D_PAIRS


def test_result_id_changes_with_decision_but_run_id_does_not() -> None:
    no_go = _measurement(decision=CandidateDDecision.NO_GO, reasons=("validation_net",))
    not_evaluable = _measurement()
    first = build_result_artifact(no_go, _datasets(), _manifest(), _clean_environment())
    second = build_result_artifact(not_evaluable, _datasets(), _manifest(), _clean_environment())
    assert first.run_id == second.run_id
    assert first.result_id != second.result_id


def test_canonical_result_is_byte_deterministic() -> None:
    artifact = build_result_artifact(_measurement(), _datasets(), _manifest(), _clean_environment())
    first = canonical_result_artifact(artifact)
    assert first == canonical_result_artifact(artifact)
    assert json.loads(first)["result_id"] == artifact.result_id


def test_result_publication_is_immutable_idempotent_and_atomic(tmp_path: Path) -> None:
    artifact = build_result_artifact(_measurement(), _datasets(), _manifest(), _clean_environment())
    out = save_result_artifact(artifact, tmp_path)
    assert out == tmp_path / artifact.run_id
    assert (out / "candidate_d_measurement_result.json").read_bytes() == (
        canonical_result_artifact(artifact)
    )
    assert save_result_artifact(artifact, tmp_path) == out
    assert not tuple(tmp_path.glob(f".{artifact.run_id}.*"))


def test_conflicting_existing_result_fails_closed(tmp_path: Path) -> None:
    artifact = build_result_artifact(_measurement(), _datasets(), _manifest(), _clean_environment())
    out = tmp_path / artifact.run_id
    out.mkdir(parents=True)
    (out / "candidate_d_measurement_result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="conflict"):
        save_result_artifact(artifact, tmp_path)


def test_execute_propagates_exact_decision_and_not_evaluable_reason(tmp_path: Path) -> None:
    config = CandidateDRunnerConfig(
        protocol=CANDIDATE_D_PROTOCOL_ID,
        direct_d1_root=tmp_path / "d1",
        execution_evidence_root=tmp_path / "execution",
        results_root=tmp_path / "results",
        adr_path=tmp_path / "adr.md",
    )
    measurement = _measurement()
    with patch("scripts.run_candidate_d_measurement.verify_adr_preregistration"):
        with patch(
            "scripts.run_candidate_d_measurement.get_git_environment",
            return_value=_clean_environment(),
        ):
            with patch(
                "scripts.run_candidate_d_measurement.load_direct_d1_datasets",
                return_value=_datasets(),
            ):
                with patch(
                    "scripts.run_candidate_d_measurement.load_execution_manifest",
                    return_value=_manifest(),
                ):
                    with patch(
                        "scripts.run_candidate_d_measurement.measure_candidate_d",
                        return_value=measurement,
                    ):
                        artifact = execute_candidate_d_measurement(config)
    assert artifact.decision == CandidateDDecision.NOT_EVALUABLE.value
    assert artifact.decision_reasons == ("missing_local_partition",)
    assert (config.results_root / artifact.run_id).is_dir()


def test_runner_has_no_network_mt5_or_execution_authority() -> None:
    import scripts.run_candidate_d_measurement as runner

    forbidden = {
        "requests",
        "httpx",
        "MetaTrader5",
        "order_send",
        "send_order",
        "close_position",
    }
    assert not forbidden.intersection(vars(runner))
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "DukascopyDirectD1HttpTransport" not in source
    assert "order_send" not in source
    assert "import MetaTrader5" not in source
    assert "from fxlab.execution" not in source
    assert not {"requests", "httpx", "MetaTrader5"}.intersection(sys.modules)


def test_adr_mismatch_fails_before_evidence_access(tmp_path: Path) -> None:
    config = CandidateDRunnerConfig(
        protocol=CANDIDATE_D_PROTOCOL_ID,
        direct_d1_root=tmp_path / "d1",
        execution_evidence_root=tmp_path / "execution",
        results_root=tmp_path / "results",
    )
    with patch(
        "scripts.run_candidate_d_measurement.verify_adr_preregistration",
        side_effect=ValueError("ADR SHA256 mismatch"),
    ):
        with patch("scripts.run_candidate_d_measurement.get_git_environment") as git_env:
            with patch("scripts.run_candidate_d_measurement.load_direct_d1_datasets") as load:
                with pytest.raises(ValueError, match="SHA256 mismatch"):
                    execute_candidate_d_measurement(config)
    git_env.assert_not_called()
    load.assert_not_called()


def test_direct_d1_exclusive_end_never_requests_2024() -> None:
    import lzma
    import struct

    import fxlab.data.dukascopy_direct_d1 as direct

    records = b"".join(
        struct.pack(">IIIIIf", day * 86_400, 100_000, 100_000, 100_000, 100_000, 0.0)
        for day in range(365)
    )
    body = lzma.compress(records, format=lzma.FORMAT_ALONE)

    class SentinelTransport:
        def __init__(self) -> None:
            self.years: list[int] = []

        def fetch_year(self, *, pair: str, year: int, **_kwargs: object) -> object:
            if year >= 2024:
                raise AssertionError("sealed year touched")
            self.years.append(year)
            url = direct.dukascopy_direct_d1_url(pair, year)
            return direct.DukascopyDirectD1Year(
                pair, year, body, url, url, "application/octet-stream"
            )

    transport = SentinelTransport()
    query = direct.BarQuery(
        direct.CanonicalInstrument("AUDUSD"),
        "D1",
        datetime(2023, 1, 1, tzinfo=UTC),
        CANDIDATE_D_END,
        CANDIDATE_D_END,
    )
    result = direct.DukascopyDirectD1HistoricalBarsProvider(transport).fetch_bars(query)
    assert not isinstance(result, direct.ProviderFailure)
    assert transport.years == [2023]


def test_execution_manifest_exclusive_end_never_inspects_2024(tmp_path: Path) -> None:
    import fxlab.research.candidate_c_execution_evidence as evidence

    boundaries: list[datetime] = []

    def inspect(_transport: object, pair: str, boundary: datetime) -> object:
        if boundary >= CANDIDATE_D_END:
            raise AssertionError("sealed boundary touched")
        boundaries.append(boundary)
        return evidence._make_record(
            pair=pair,
            boundary=boundary,
            state=CandidateCExecutionState.ABSENT_EVIDENCED,
            partition_state="absent_evidenced",
            absence_evidence_type="http_404",
        )

    with patch.object(evidence, "_inspect_partition", side_effect=inspect):
        manifest = evidence.build_candidate_c_execution_evidence_manifest(
            root=tmp_path,
            start=datetime(2023, 12, 31, tzinfo=UTC),
            end=CANDIDATE_D_END,
            pairs=("AUDUSD",),
        )
    assert boundaries == [datetime(2023, 12, 31, tzinfo=UTC)]
    assert all(record.intended_boundary < CANDIDATE_D_END for record in manifest.records)


def test_metric_change_changes_result_id_but_not_run_id() -> None:
    from dataclasses import replace

    from fxlab.research.candidate_d_measurement import CandidateDMetrics, CandidateDSplitResult

    def split(net_return: float) -> CandidateDSplitResult:
        metrics = CandidateDMetrics(
            trade_count=1,
            per_pair_trade_counts=tuple(
                (pair, int(pair == "AUDUSD")) for pair in CANDIDATE_D_PAIRS
            ),
            non_entry_reason_counts=(),
            gross_return=net_return + 0.01,
            net_return=net_return,
            gross_expectancy=net_return + 0.01,
            net_expectancy=net_return,
            gross_annualized_return=net_return + 0.01,
            annualized_return=net_return,
            sharpe=1.0,
            max_drawdown=0.1,
            cost_drag_return=0.01,
            cost_drag_expectancy=0.01,
            pair_contributions=tuple(
                (pair, net_return if pair == "AUDUSD" else 0.0)
                for pair in CANDIDATE_D_PAIRS
            ),
        )
        return CandidateDSplitResult(metrics, metrics, 0.001, 0.001, (), ())

    baseline = replace(
        _measurement(decision=CandidateDDecision.NO_GO, reasons=()), train=split(0.1)
    )
    changed = replace(baseline, train=split(0.2))
    first = build_result_artifact(baseline, _datasets(), _manifest(), _clean_environment())
    second = build_result_artifact(changed, _datasets(), _manifest(), _clean_environment())
    assert first.run_id == second.run_id
    assert first.result_id != second.result_id


def test_provenance_change_changes_frozen_engine_run_id() -> None:
    from fxlab.research.candidate_d_measurement import (
        build_candidate_d_policy,
        build_candidate_d_run_id,
    )

    environment = _clean_environment()
    first = build_result_artifact(_measurement(), _datasets(), _manifest(), environment)
    changed = build_result_artifact(
        _measurement(), _datasets(suffix="changed"), _manifest(), environment
    )
    common = {
        "policy": build_candidate_d_policy(),
        "code_environment": environment,
        "execution_manifest_id": _manifest().manifest_id,
        "execution_record_ids": ("4" * 64,),
        "execution_state_counts": _manifest().state_counts,
    }
    first_run = build_candidate_d_run_id(
        dataset_semantics=first.dataset_identities, **common
    )
    changed_run = build_candidate_d_run_id(
        dataset_semantics=changed.dataset_identities, **common
    )
    assert first_run != changed_run


@pytest.mark.parametrize("extra_name", [None, "unexpected.txt"])
def test_partial_or_additional_existing_result_fails_closed(
    tmp_path: Path, extra_name: str | None
) -> None:
    artifact = build_result_artifact(
        _measurement(), _datasets(), _manifest(), _clean_environment()
    )
    out = tmp_path / artifact.run_id
    out.mkdir(parents=True)
    if extra_name is not None:
        (out / "candidate_d_measurement_result.json").write_bytes(
            canonical_result_artifact(artifact)
        )
        (out / extra_name).write_text("unexpected", encoding="utf-8")
    with pytest.raises(FileExistsError, match="conflict"):
        save_result_artifact(artifact, tmp_path)