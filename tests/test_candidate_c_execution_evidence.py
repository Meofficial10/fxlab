"""Offline contracts for Candidate C hourly execution evidence."""

from __future__ import annotations

import hashlib
import importlib
import lzma
import os
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fxlab.cli import app
from fxlab.data.bi5_mirror import Bi5AbsenceRecord, Bi5PartitionState

PAIR_SET = (
    "AUDUSD",
    "EURUSD",
    "GBPUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
)
START = datetime(2021, 1, 5, tzinfo=UTC)
END = datetime(2021, 1, 6, tzinfo=UTC)
RECORD = struct.Struct(">IIIff")
runner = CliRunner()


def _module():
    return importlib.import_module("fxlab.research.candidate_c_execution_evidence")


def _body(*records: tuple[int, int, int, float, float]) -> bytes:
    raw = b"".join(RECORD.pack(*record) for record in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def _valid_body(*, second_offset_ms: int = 1_000) -> bytes:
    return _body(
        (second_offset_ms, 110_005, 110_000, 1.5, 2.5),
        (second_offset_ms + 1_000, 110_015, 110_010, 3.5, 4.5),
    )


def _hour_path(root: Path, pair: str, hour: datetime = START) -> Path:
    return (
        root
        / pair
        / f"{hour.year:04d}"
        / f"{hour.month - 1:02d}"
        / f"{hour.day:02d}"
        / f"{hour.hour:02d}h_ticks.bi5"
    )


def _write_hour(root: Path, pair: str, hour: datetime = START, body: bytes | None = None) -> bytes:
    payload = _valid_body() if body is None else body
    path = _hour_path(root, pair, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _write_absence(
    root: Path,
    pair: str,
    hour: datetime = START,
    *,
    empty_200: bool = False,
    retrieved_at: datetime = datetime(2021, 1, 7, tzinfo=UTC),
) -> None:
    path = _hour_path(root, pair, hour).with_name(f"{hour.hour:02d}h_ticks.absent.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    factory = Bi5AbsenceRecord.create_empty_response if empty_200 else Bi5AbsenceRecord.create
    path.write_bytes(factory(symbol=pair, hour=hour, retrieved_at=retrieved_at).to_bytes())


def _write_all_pairs(root: Path, hour: datetime = START) -> None:
    for pair in PAIR_SET:
        _write_hour(root, pair, hour)


def _record(manifest, pair: str = "AUDUSD", boundary: datetime = START):
    return next(
        item
        for item in manifest.records
        if item.pair == pair and item.intended_boundary == boundary
    )


def test_offline_record_selects_first_tick_and_binds_exact_raw_evidence(
    tmp_path: Path,
) -> None:
    execution = _module()
    payload = _write_hour(tmp_path, "AUDUSD")

    manifest = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path, start=START, end=END, pairs=("AUDUSD",)
    )
    item = manifest.records[0]

    assert item.schema_version == "candidate_c_execution_evidence.v1"
    assert item.state is execution.CandidateCExecutionState.AVAILABLE
    assert item.pair == "AUDUSD"
    assert item.intended_boundary == START
    assert item.window_start == START
    assert item.window_end == START + timedelta(hours=1)
    assert item.selected_tick_offset_ms == 1_000
    assert item.selected_tick_timestamp == START + timedelta(seconds=1)
    assert (item.ask, item.bid, item.ask_volume, item.bid_volume) == pytest.approx(
        (1.10005, 1.1, 1.5, 2.5)
    )
    assert item.raw_sha256 == hashlib.sha256(payload).hexdigest()
    assert item.raw_byte_count == len(payload)
    assert item.absence_evidence_type is None


def test_tick_selection_is_strictly_first_in_decoder_order(tmp_path: Path) -> None:
    execution = _module()
    _write_hour(tmp_path, "AUDUSD", body=_valid_body(second_offset_ms=17_000))

    item = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path, start=START, end=END, pairs=("AUDUSD",)
    ).records[0]

    assert item.selected_tick_offset_ms == 17_000
    assert item.selected_tick_timestamp == START + timedelta(seconds=17)
    assert item.ask >= item.bid > 0


@pytest.mark.parametrize(
    ("empty_200", "expected_state", "expected_kind"),
    [
        (False, "ABSENT_EVIDENCED", "http_404_absence"),
        (True, "EMPTY_EVIDENCED", "http_200_empty_body"),
    ],
)
def test_explicit_absence_evidence_types_remain_distinct(
    tmp_path: Path, empty_200: bool, expected_state: str, expected_kind: str
) -> None:
    execution = _module()
    _write_absence(tmp_path, "AUDUSD", empty_200=empty_200)

    item = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path, start=START, end=END, pairs=("AUDUSD",)
    ).records[0]

    assert item.state.name == expected_state
    assert item.absence_evidence_type == expected_kind
    assert item.raw_sha256 is None
    assert item.raw_byte_count is None
    assert item.selected_tick_timestamp is None


def test_missing_empty_and_corrupt_local_partitions_are_distinguished(tmp_path: Path) -> None:
    execution = _module()
    _write_hour(tmp_path, "EURUSD", body=_body())
    _write_hour(tmp_path, "GBPUSD", body=b"not-lzma")
    bad = _hour_path(tmp_path, "NZDUSD").with_name("00h_ticks.absent.json")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not-json", encoding="utf-8")

    manifest = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path,
        start=START,
        end=END,
        pairs=("AUDUSD", "EURUSD", "GBPUSD", "NZDUSD"),
    )

    assert (
        _record(manifest, "AUDUSD").state
        is execution.CandidateCExecutionState.MISSING_LOCAL_PARTITION
    )
    assert _record(manifest, "EURUSD").state is execution.CandidateCExecutionState.NO_VALID_TICK
    assert _record(manifest, "GBPUSD").state is execution.CandidateCExecutionState.INVALID_PARTITION
    assert _record(manifest, "NZDUSD").state is execution.CandidateCExecutionState.INVALID_PARTITION
    assert manifest.complete is False


def test_no_next_hour_rescue(tmp_path: Path) -> None:
    execution = _module()
    _write_hour(tmp_path, "AUDUSD", START + timedelta(hours=1))

    item = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path, start=START, end=END, pairs=("AUDUSD",)
    ).records[0]

    assert item.state is execution.CandidateCExecutionState.MISSING_LOCAL_PARTITION


def test_manifest_order_and_hash_are_canonical(tmp_path: Path) -> None:
    execution = _module()
    later = START + timedelta(days=1)
    for pair in ("EURUSD", "AUDUSD"):
        _write_hour(tmp_path, pair, START)
        _write_hour(tmp_path, pair, later)

    first = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path,
        start=START,
        end=later + timedelta(days=1),
        pairs=("EURUSD", "AUDUSD"),
    )
    second = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path,
        start=START,
        end=later + timedelta(days=1),
        pairs=("AUDUSD", "EURUSD"),
    )

    expected = [
        ("AUDUSD", START),
        ("AUDUSD", later),
        ("EURUSD", START),
        ("EURUSD", later),
    ]
    assert [(item.pair, item.intended_boundary) for item in first.records] == expected
    assert first.manifest_id == second.manifest_id
    assert first.records == second.records


def test_local_path_mtime_and_absence_retrieval_time_do_not_enter_identity(
    tmp_path: Path,
) -> None:
    execution = _module()
    one = tmp_path / "one"
    two = tmp_path / "two"
    payload = _write_hour(one, "AUDUSD")
    path_two = _hour_path(two, "AUDUSD")
    path_two.parent.mkdir(parents=True, exist_ok=True)
    path_two.write_bytes(payload)

    first = execution.build_candidate_c_execution_evidence_manifest(
        root=one, start=START, end=END, pairs=("AUDUSD",)
    )
    os.utime(_hour_path(one, "AUDUSD"), (1_700_000_000, 1_700_000_000))
    after_mtime = execution.build_candidate_c_execution_evidence_manifest(
        root=one, start=START, end=END, pairs=("AUDUSD",)
    )
    other_path = execution.build_candidate_c_execution_evidence_manifest(
        root=two, start=START, end=END, pairs=("AUDUSD",)
    )
    assert first.manifest_id == after_mtime.manifest_id == other_path.manifest_id

    absent_one = tmp_path / "absent-one"
    absent_two = tmp_path / "absent-two"
    _write_absence(absent_one, "AUDUSD", retrieved_at=datetime(2021, 1, 7, tzinfo=UTC))
    _write_absence(absent_two, "AUDUSD", retrieved_at=datetime(2022, 1, 7, tzinfo=UTC))
    assert (
        execution.build_candidate_c_execution_evidence_manifest(
            root=absent_one, start=START, end=END, pairs=("AUDUSD",)
        ).manifest_id
        == execution.build_candidate_c_execution_evidence_manifest(
            root=absent_two, start=START, end=END, pairs=("AUDUSD",)
        ).manifest_id
    )


@pytest.mark.parametrize(
    ("start", "end", "pairs", "reason"),
    [
        (datetime(2013, 12, 31, tzinfo=UTC), END, ("AUDUSD",), "research_window_violation"),
        (START, datetime(2024, 1, 2, tzinfo=UTC), ("AUDUSD",), "research_window_violation"),
        (START, END, ("XAUUSD",), "symbol_unsupported"),
        (START + timedelta(hours=1), END, ("AUDUSD",), "daily_boundary_required"),
    ],
)
def test_manifest_rejects_invalid_scope_before_filesystem_transport(
    tmp_path: Path,
    start: datetime,
    end: datetime,
    pairs: tuple[str, ...],
    reason: str,
) -> None:
    execution = _module()
    with pytest.raises(ValueError, match=reason):
        execution.build_candidate_c_execution_evidence_manifest(
            root=tmp_path, start=start, end=end, pairs=pairs
        )


def test_default_universe_is_exactly_seven_pairs(tmp_path: Path) -> None:
    execution = _module()
    manifest = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path, start=START, end=END
    )
    assert execution.CANDIDATE_C_EXECUTION_PAIRS == PAIR_SET
    assert tuple(item.pair for item in manifest.records) == PAIR_SET
    assert manifest.total_count == 7


def test_cohort_requires_entry_and_exit_for_all_seven_pairs(tmp_path: Path) -> None:
    execution = _module()
    exit_boundary = START + timedelta(days=1)
    _write_all_pairs(tmp_path, START)
    _write_all_pairs(tmp_path, exit_boundary)
    manifest = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path,
        start=START,
        end=exit_boundary + timedelta(days=1),
    )

    result = execution.assess_candidate_c_execution_cohort(
        manifest, entry_boundary=START, exit_boundary=exit_boundary
    )
    assert result.state is execution.CandidateCExecutionCohortState.AVAILABLE
    assert result.missing == ()

    _hour_path(tmp_path, "USDJPY", exit_boundary).unlink()
    incomplete = execution.build_candidate_c_execution_evidence_manifest(
        root=tmp_path,
        start=START,
        end=exit_boundary + timedelta(days=1),
    )
    result = execution.assess_candidate_c_execution_cohort(
        incomplete, entry_boundary=START, exit_boundary=exit_boundary
    )
    assert result.state is execution.CandidateCExecutionCohortState.EXECUTION_EVIDENCE_INCOMPLETE
    assert result.missing == (("USDJPY", exit_boundary, "missing_local_partition"),)


def test_acquisition_schedule_requests_only_00h_and_is_deterministic(tmp_path: Path) -> None:
    execution = _module()
    calls: list[tuple[str, datetime]] = []

    def downloader(**kwargs: object) -> Bi5PartitionState:
        calls.append((str(kwargs["symbol"]), kwargs["hour"]))  # type: ignore[arg-type]
        return Bi5PartitionState.PRESENT_STAGED

    report = execution.mirror_candidate_c_execution_partitions(
        start=START,
        end=START + timedelta(days=2),
        destination_root=tmp_path,
        pair="AUDUSD",
        downloader=downloader,
    )

    assert calls == [("AUDUSD", START), ("AUDUSD", START + timedelta(days=1))]
    assert all(hour.hour == 0 for _, hour in calls)
    assert report.scheduled_partitions == 2
    assert report.present_staged == 2
    assert report.ok is True


def test_acquisition_without_pair_schedules_all_pairs_not_ranked_subset(tmp_path: Path) -> None:
    execution = _module()
    calls: list[tuple[str, datetime]] = []

    def downloader(**kwargs: object) -> Bi5PartitionState:
        calls.append((str(kwargs["symbol"]), kwargs["hour"]))  # type: ignore[arg-type]
        return Bi5PartitionState.ABSENT_EVIDENCED

    report = execution.mirror_candidate_c_execution_partitions(
        start=START,
        end=END,
        destination_root=tmp_path,
        downloader=downloader,
    )

    assert calls == [(pair, START) for pair in PAIR_SET]
    assert report.absent_evidenced == 7


@pytest.mark.parametrize(
    ("start", "end", "pair", "reason"),
    [
        (datetime(2013, 12, 31, tzinfo=UTC), END, "AUDUSD", "research_window_violation"),
        (START, datetime(2024, 1, 2, tzinfo=UTC), "AUDUSD", "research_window_violation"),
        (START, END, "XAUUSD", "symbol_unsupported"),
    ],
)
def test_acquisition_scope_fails_before_downloader(
    tmp_path: Path,
    start: datetime,
    end: datetime,
    pair: str,
    reason: str,
) -> None:
    execution = _module()
    calls: list[object] = []

    def downloader(**kwargs: object) -> Bi5PartitionState:
        calls.append(kwargs)
        return Bi5PartitionState.PRESENT_STAGED

    with pytest.raises(ValueError, match=reason):
        execution.mirror_candidate_c_execution_partitions(
            start=start,
            end=end,
            destination_root=tmp_path,
            pair=pair,
            downloader=downloader,
        )
    assert calls == []


def test_acquisition_failures_remain_incomplete_without_fabrication(tmp_path: Path) -> None:
    execution = _module()

    def downloader(**_kwargs: object) -> Bi5PartitionState:
        return Bi5PartitionState.INCOMPLETE

    report = execution.mirror_candidate_c_execution_partitions(
        start=START,
        end=END,
        destination_root=tmp_path,
        pair="AUDUSD",
        downloader=downloader,
    )

    assert report.incomplete == 1
    assert report.absent_evidenced == 0
    assert report.ok is False


def test_cli_routes_exact_schedule_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _module()
    calls: list[dict[str, object]] = []

    def fake_mirror(**kwargs: object):
        calls.append(dict(kwargs))
        return execution.CandidateCExecutionMirrorReport(
            scheduled_partitions=7,
            present_staged=7,
            absent_evidenced=0,
            incomplete=0,
            conflict=0,
            corrupt_local=0,
        )

    monkeypatch.setattr(execution, "mirror_candidate_c_execution_partitions", fake_mirror)
    result = runner.invoke(
        app,
        [
            "mirror-candidate-c-execution",
            "--from",
            "2021-01-05T00:00:00Z",
            "--to",
            "2021-01-06T00:00:00Z",
            "--dest",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["pair"] is None
    assert calls[0]["start"] == START
    assert calls[0]["end"] == END


def test_module_has_no_candidate_c_measurement_surface() -> None:
    execution = _module()
    forbidden = {
        "calculate_returns",
        "calculate_signals",
        "rank_pairs",
        "portfolio_weights",
        "sharpe_ratio",
    }
    assert forbidden.isdisjoint(vars(execution))
