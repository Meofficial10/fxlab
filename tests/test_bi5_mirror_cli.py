"""Offline CLI contract tests for bounded Dukascopy BI5 mirror concurrency."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fxlab.cli import app
from fxlab.data import Bi5SyncReport
from fxlab.data import bi5_mirror as mirror_module

runner = CliRunner()


def test_mirror_bi5_cli_forwards_explicit_workers_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_sync_range(**kwargs: object) -> Bi5SyncReport:
        calls.append(dict(kwargs))
        return Bi5SyncReport(
            symbol="AUDUSD",
            start=datetime(2021, 1, 5, tzinfo=UTC),
            end=datetime(2021, 1, 5, 1, tzinfo=UTC),
            total_hours=1,
            present_staged=1,
            absent_evidenced=0,
            incomplete=0,
            conflict=0,
            corrupt_local=0,
        )

    monkeypatch.setattr(mirror_module, "sync_range", fake_sync_range)
    result = runner.invoke(
        app,
        [
            "mirror-bi5",
            "--pair",
            "AUDUSD",
            "--from",
            "2021-01-05T00:00:00Z",
            "--to",
            "2021-01-05T01:00:00Z",
            "--dest",
            str(tmp_path),
            "--workers",
            "4",
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["workers"] == 4


@pytest.mark.parametrize("workers", ["0", "-1", "5"])
def test_mirror_bi5_cli_rejects_invalid_workers_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: str
) -> None:
    called = False

    def forbidden_sync_range(**_kwargs: object) -> Bi5SyncReport:
        nonlocal called
        called = True
        raise AssertionError("invalid workers must be rejected by CLI validation")

    monkeypatch.setattr(mirror_module, "sync_range", forbidden_sync_range)
    result = runner.invoke(
        app,
        [
            "mirror-bi5",
            "--pair",
            "AUDUSD",
            "--from",
            "2021-01-05T00:00:00Z",
            "--to",
            "2021-01-05T01:00:00Z",
            "--dest",
            str(tmp_path),
            "--workers",
            workers,
        ],
    )

    assert result.exit_code == 2
    assert "workers must be between 1 and 4" in result.output
    assert called is False
