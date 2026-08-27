"""Focused tests for the Phase 14 foreground paper application."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from fxlab.config import load_config
from fxlab.data.store import save_bars
from fxlab.execution.app import (
    OBSERVE_ONLY_POLICY_ID,
    AppExitCode,
    PaperAppError,
    ReplayRequest,
    assemble_observation_replay,
    inspect_events,
    monitor_recovered,
    recover_snapshot,
    run_foreground_replay,
)
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import AuditEventType
from fxlab.execution.paper_session import CycleKind, PaperCycleResult, PaperTradingSession
from fxlab.execution.recovery import RecoveryState, create_checkpoint


def _bars() -> pd.DataFrame:
    index = pd.date_range("2026-01-01 00:00", periods=3, freq="5min", tz="UTC")
    close = np.array([1.1000, 1.1010, 1.1020], dtype="float64")
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.001,
            "low": close - 0.001,
            "close": close,
            "volume": np.ones(3, dtype="float64"),
        },
        index=index,
        dtype="float64",
    )
    frame.index.name = "ts_open"
    frame.attrs.update(symbol="EURUSD", timeframe="M5")
    return frame


def _request(tmp_path, *, session_id: str = "phase14-test") -> ReplayRequest:
    save_bars(_bars(), tmp_path / "data", "EURUSD", "M5")
    return ReplayRequest(
        session_id=session_id,
        store_path=tmp_path / f"{session_id}.sqlite",
        data_dir=tmp_path / "data",
        symbol="EURUSD",
        timeframe="M5",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
        observe_only=True,
    )


def test_request_requires_observation_only_safe_identity_and_aware_bounds(tmp_path) -> None:
    request = _request(tmp_path)
    assert request.symbol == "EURUSD"
    assert request.start.tzinfo is UTC
    for change in (
        {"observe_only": False},
        {"session_id": "bad:id"},
        {"start": datetime(2026, 1, 1)},
    ):
        values = {**request.__dict__, **change}
        with pytest.raises(ValueError):
            ReplayRequest(**values)


def test_application_contracts_are_exported() -> None:
    from fxlab.execution import AppExitCode as ExportedExitCode
    from fxlab.execution import ReplayRequest as ExportedRequest

    assert ExportedExitCode is AppExitCode
    assert ExportedRequest is ReplayRequest


def test_observation_assembly_uses_configured_equity_and_never_signals(tmp_path) -> None:
    request = _request(tmp_path)
    app = assemble_observation_replay(request, load_config(), fresh=True)
    try:
        assert app.execution_policy_id == OBSERVE_ONLY_POLICY_ID
        assert app.session.broker.get_account_info().balance == 10.0
        assert app.session.risk_engine.limits.starting_equity == 10.0
        assert app.session.risk_engine.pip_size_resolver.pip_size_for("EURUSD") == 0.0001
        app.session.start()
        cycle = app.session.poll_once()
        assert cycle.signals == ()
        assert app.session.risk_engine.daily_trades == 0
        assert app.session.risk_engine.reserved_position_count == 0
        assert app.session.order_manager.snapshot_state()["records"] == []
    finally:
        app.close()


def test_fresh_assembly_rejects_initialized_store_without_recovery(tmp_path) -> None:
    request = _request(tmp_path)
    store = SQLiteEventStore(request.store_path, request.session_id)
    store.close()
    with pytest.raises(PaperAppError) as exc:
        assemble_observation_replay(request, load_config(), fresh=True)
    assert exc.value.exit_code is AppExitCode.USAGE


def test_foreground_replay_exhausts_checkpoints_and_closes_store(tmp_path) -> None:
    request = _request(tmp_path)
    result = run_foreground_replay(request, load_config())
    assert result.exit_code is AppExitCode.SUCCESS
    assert result.state == "exhausted"
    reopened = SQLiteEventStore(request.store_path, request.session_id)
    try:
        assert reopened.load_latest_checkpoint() is not None
        event_types = [event.event_type for event in reopened.load_events()]
        assert event_types[0] is AuditEventType.SESSION_STARTED
        assert event_types[-1] is AuditEventType.SESSION_STOPPED
    finally:
        reopened.close()


def test_recovery_is_side_effect_free_and_returns_recovered_snapshot(tmp_path) -> None:
    request = _request(tmp_path)
    app = assemble_observation_replay(request, load_config(), fresh=True)
    app.session.start()
    app.session.poll_once()
    create_checkpoint(
        app.session,
        app.store,
        software_version=app.software_version,
        execution_policy_id=app.execution_policy_id,
    )
    before = app.store.last_sequence()
    app.close()

    snapshot = recover_snapshot(request, load_config())
    assert snapshot.recovery.state is RecoveryState.RECOVERED
    assert snapshot.label == "RECOVERED SNAPSHOT"
    assert snapshot.status["latest_event_sequence"] == before
    assert snapshot.status["balance"] == 10.0
    assert snapshot.orders == ()
    assert snapshot.positions == ()


def test_event_inspection_filters_and_limits_without_appending(tmp_path) -> None:
    request = _request(tmp_path)
    assert run_foreground_replay(request, load_config()).exit_code is AppExitCode.SUCCESS
    before = inspect_events(request.store_path, request.session_id)
    stopped = inspect_events(
        request.store_path,
        request.session_id,
        event_type=AuditEventType.SESSION_STOPPED,
        limit=1,
    )
    after = inspect_events(request.store_path, request.session_id)
    assert len(stopped) == 1
    assert stopped[0]["event_type"] == "session_stopped"
    assert len(after) == len(before)
    with pytest.raises(ValueError):
        inspect_events(request.store_path, request.session_id, limit=0)


def test_keyboard_interrupt_uses_graceful_shutdown_and_code_130(
    tmp_path, monkeypatch
) -> None:
    request = _request(tmp_path)

    def interrupt(_session) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(PaperTradingSession, "poll_once", interrupt)
    result = run_foreground_replay(request, load_config())
    assert result.exit_code is AppExitCode.INTERRUPTED
    reopened = SQLiteEventStore(request.store_path, request.session_id)
    try:
        assert reopened.load_latest_checkpoint() is not None
        assert reopened.load_events()[-1].event_type is AuditEventType.SESSION_STOPPED
    finally:
        reopened.close()


def test_reconciliation_takes_precedence_over_interruption(tmp_path, monkeypatch) -> None:
    request = _request(tmp_path)

    def interrupt(session: PaperTradingSession) -> None:
        session.require_reconciliation()
        raise KeyboardInterrupt

    monkeypatch.setattr(PaperTradingSession, "poll_once", interrupt)
    result = run_foreground_replay(request, load_config())
    assert result.exit_code is AppExitCode.RECONCILIATION_REQUIRED


def test_failed_cycle_stops_foreground_runner(tmp_path, monkeypatch) -> None:
    request = _request(tmp_path)
    calls = 0

    def failed_then_exhausted(_session) -> PaperCycleResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return PaperCycleResult(CycleKind.FAILED, None, reason="cycle_failed")
        return PaperCycleResult(CycleKind.EXHAUSTED, None, reason="replay_exhausted")

    monkeypatch.setattr(PaperTradingSession, "poll_once", failed_then_exhausted)
    result = run_foreground_replay(request, load_config())
    assert result.exit_code is AppExitCode.RUNTIME_FAILURE
    assert result.reason == "cycle_failed"
    assert calls == 1


def test_recovered_monitor_is_read_only_and_never_labelled_live(tmp_path) -> None:
    request = _request(tmp_path, session_id="monitor-app")
    assert run_foreground_replay(request, load_config()).exit_code is AppExitCode.SUCCESS
    store = SQLiteEventStore(request.store_path, request.session_id)
    try:
        before_events = store.load_events()
        before_checkpoint = store.load_latest_checkpoint()
    finally:
        store.close()
    result = monitor_recovered(request, load_config(), event_limit=3)
    assert result.available
    assert result.snapshot is not None
    assert result.snapshot.source.value == "recovered_snapshot"
    assert result.snapshot.source.value != "live_runtime"
    assert len(result.snapshot.recent_events) <= 3
    store = SQLiteEventStore(request.store_path, request.session_id)
    try:
        assert store.load_events() == before_events
        assert store.load_latest_checkpoint() == before_checkpoint
    finally:
        store.close()
