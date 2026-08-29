"""CLI contract tests for the Phase 14 paper application shell."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

import fxlab.cli as cli_module
from fxlab.cli import app
from fxlab.config import load_config
from fxlab.data import ProviderFailure, ProviderFailureCategory, ProviderGatewayError
from fxlab.data.store import save_bars
from fxlab.execution.app import ReplayRequest, run_foreground_replay
from fxlab.execution.runtime_control import RuntimeState
from fxlab.operations.control import ControlResponse, ServiceState

runner = CliRunner()


def _bars() -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC")
    close = np.array([1.1, 1.101], dtype="float64")
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.001,
            "low": close - 0.001,
            "close": close,
            "volume": np.ones(2, dtype="float64"),
        },
        index=index,
        dtype="float64",
    )
    frame.index.name = "ts_open"
    frame.attrs.update(symbol="EURUSD", timeframe="M5")
    return frame


def _base(tmp_path) -> list[str]:
    save_bars(_bars(), tmp_path / "data", "EURUSD", "M5")
    return [
        "--session-id", "cli-session",
        "--store", str(tmp_path / "session.sqlite"),
        "--data-dir", str(tmp_path / "data"),
        "--symbol", "EURUSD",
        "--timeframe", "M5",
        "--start", "2026-01-01T00:00:00+00:00",
        "--end", "2026-01-01T00:10:00+00:00",
        "--as-of", "2026-01-01T00:10:00+00:00",
    ]


def test_existing_commands_and_paper_commands_are_registered() -> None:
    root = runner.invoke(app, ["--help"])
    paper = runner.invoke(app, ["paper", "--help"])
    assert root.exit_code == 0
    for command in ("info", "ingest", "validate-data", "label", "split", "backtest"):
        assert command in root.output
    for command in ("replay", "recover", "events", "status", "orders", "positions", "monitor"):
        assert command in paper.output
    for invalid in ("start", "pause", "resume", "stop", "emergency-stop", "reconcile"):
        assert f" {invalid} " not in paper.output


def test_service_commands_are_registered_without_daemon_start() -> None:
    root = runner.invoke(app, ["--help"])
    service = runner.invoke(app, ["service", "--help"])
    assert root.exit_code == 0
    assert "service" in root.output
    assert service.exit_code == 0
    for command in ("run", "status", "pause", "resume", "emergency-stop", "stop"):
        assert command in service.output
    assert " start " not in service.output


def _operations_file(tmp_path) -> Path:
    state = (tmp_path / "service-state").resolve()
    secret = (tmp_path / "service.secret").resolve()
    secret.write_bytes(b"s" * 32)
    path = (tmp_path / "operations.toml").resolve()
    path.write_text(
        "\n".join(
            [
                "format_version = 1",
                f'state_directory = "{str(state).replace(chr(92), "/")}"',
                'runtime_id = "cli-service"',
                'operator_id = "cli-operator"',
                f'control_secret_file = "{str(secret).replace(chr(92), "/")}"',
                f'endpoint_id = "cli-{uuid.uuid4().hex}"',
                'log_filename = "cli-service.jsonl"',
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_service_run_requires_observe_only_and_exact_startup_mode(tmp_path) -> None:
    operations = _operations_file(tmp_path)
    replay_args = _base(tmp_path)
    store_index = replay_args.index("--store")
    del replay_args[store_index : store_index + 2]
    args = ["--operations-config", str(operations), *replay_args]
    without_observe = runner.invoke(app, ["service", "run", *args, "--fresh"])
    assert without_observe.exit_code == 2
    assert "observation-only" in without_observe.output.lower()
    no_mode = runner.invoke(app, ["service", "run", *args, "--observe-only"])
    assert no_mode.exit_code == 2
    both = runner.invoke(
        app,
        ["service", "run", *args, "--observe-only", "--fresh", "--recover"],
    )
    assert both.exit_code == 2


@pytest.mark.parametrize(
    ("command", "action"),
    [
        ("status", "status"),
        ("pause", "pause"),
        ("resume", "resume"),
        ("emergency-stop", "emergency_stop"),
        ("stop", "stop"),
    ],
)
def test_service_client_commands_use_config_secret_and_explicit_json(
    tmp_path, monkeypatch: pytest.MonkeyPatch, command: str, action: str
) -> None:
    operations = _operations_file(tmp_path)
    seen = []

    def send(config, secret, request):
        seen.append((config.operator_id, repr(secret), request.action.value))
        return ControlResponse(
            1,
            request.request_id,
            True,
            False,
            ServiceState.RUNNING,
            RuntimeState.PAUSED,
            "accepted",
            (("source", "live_runtime"),),
        )

    monkeypatch.setattr(cli_module, "send_control_request", send)
    result = runner.invoke(
        app,
        ["service", command, "--operations-config", str(operations), "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["payload"]["source"] == "live_runtime"
    assert seen == [("cli-operator", "ControlSecret(<REDACTED>)", action)]


def test_dukascopy_ingest_failure_is_concise_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    config = load_config().model_copy(update={"data_dir": str(tmp_path)})
    monkeypatch.setattr(cli_module, "load_config", lambda: config)

    def fail(*_args: object, **_kwargs: object) -> pd.DataFrame:
        raise ProviderGatewayError(
            ProviderFailure(
                ProviderFailureCategory.TRANSIENT,
                "network_unavailable",
                "dukascopy",
                retryable=True,
            )
        )

    monkeypatch.setattr(cli_module, "ingest_data", fail)
    result = runner.invoke(
        app,
        [
            "ingest",
            "--pair",
            "EURUSD",
            "--tf",
            "M5",
            "--source",
            "dukascopy",
            "--from",
            "2026-01-01T00:00:00+00:00",
            "--to",
            "2026-01-02T00:00:00+00:00",
        ],
    )
    assert result.exit_code == 2
    assert "transient:network_unavailable" in result.output
    assert "traceback" not in result.output.lower()


def test_all_paper_command_help_is_available() -> None:
    for command in ("replay", "recover", "events", "status", "orders", "positions", "monitor"):
        result = runner.invoke(app, ["paper", command, "--help"])
        assert result.exit_code == 0, result.output


def test_replay_requires_explicit_observation_only(tmp_path) -> None:
    result = runner.invoke(app, ["paper", "replay", *_base(tmp_path)])
    assert result.exit_code == 2
    assert "observation-only" in result.output.lower()


def test_replay_rejects_naive_time_and_unsafe_session_id(tmp_path) -> None:
    args = _base(tmp_path)
    args[args.index("cli-session")] = "bad:id"
    result = runner.invoke(app, ["paper", "replay", *args, "--observe-only"])
    assert result.exit_code == 2
    args = _base(tmp_path)
    args[args.index("2026-01-01T00:00:00+00:00")] = "2026-01-01T00:00:00"
    result = runner.invoke(app, ["paper", "replay", *args, "--observe-only"])
    assert result.exit_code == 2


def test_replay_json_and_existing_store_rejection(tmp_path) -> None:
    args = _base(tmp_path)
    first = runner.invoke(
        app, ["paper", "replay", *args, "--observe-only", "--json"]
    )
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["state"] == "exhausted"
    second = runner.invoke(app, ["paper", "replay", *args, "--observe-only"])
    assert second.exit_code == 2
    assert "existing initialized store" in second.output


def test_recover_status_and_read_only_snapshots_are_labelled(tmp_path) -> None:
    args = _base(tmp_path)
    request = ReplayRequest(
        "cli-session",
        tmp_path / "session.sqlite",
        tmp_path / "data",
        "EURUSD",
        "M5",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        True,
    )
    assert run_foreground_replay(request, load_config()).exit_code == 0
    for command in ("recover", "status", "orders", "positions"):
        result = runner.invoke(app, ["paper", command, *args, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["label"] == "RECOVERED SNAPSHOT"
        assert payload["session_id"] == "cli-session"


def test_events_filter_limit_and_json_are_read_only(tmp_path) -> None:
    args = _base(tmp_path)
    replay = runner.invoke(app, ["paper", "replay", *args, "--observe-only"])
    assert replay.exit_code == 0
    event_args = [
        "--session-id", "cli-session",
        "--store", str(tmp_path / "session.sqlite"),
        "--event-type", "session_stopped",
        "--limit", "1",
        "--json",
    ]
    result = runner.invoke(app, ["paper", "events", *event_args])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["events"][0]["event_type"] == "session_stopped"
    invalid = runner.invoke(
        app,
        ["paper", "events", "--session-id", "cli-session", "--store",
         str(tmp_path / "session.sqlite"), "--limit", "0"],
    )
    assert invalid.exit_code == 2

    wrong_session = runner.invoke(
        app,
        [
            "paper", "events", "--session-id", "another-session", "--store",
            str(tmp_path / "session.sqlite"),
        ],
    )
    assert wrong_session.exit_code == 5
    assert "store" in wrong_session.output.lower()


def test_help_exposes_no_policy_import_or_strategy_selection() -> None:
    result = runner.invoke(app, ["paper", "replay", "--help"])
    lowered = result.output.lower()
    assert "--policy" not in lowered
    assert "--setup" not in lowered
    assert "--source" not in lowered


def test_monitor_json_is_recovered_snapshot_never_live(tmp_path) -> None:
    args = _base(tmp_path)
    replay = runner.invoke(app, ["paper", "replay", *args, "--observe-only"])
    assert replay.exit_code == 0, replay.output
    result = runner.invoke(app, ["paper", "monitor", *args, "--event-limit", "2", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source"] == "recovered_snapshot"
    assert payload["label"] == "RECOVERED SNAPSHOT"
    assert len(payload["recent_events"]) <= 2
    assert "live" not in payload["label"].lower()
