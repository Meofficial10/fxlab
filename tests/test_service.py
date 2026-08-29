from __future__ import annotations

import dataclasses
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

import fxlab.operations.service as service_module
from fxlab.config import load_config
from fxlab.data.store import save_bars
from fxlab.execution.app import ReplayRequest, assemble_observation_replay, recover_snapshot
from fxlab.execution.event_ledger import AuditEventType
from fxlab.execution.paper_session import PaperTradingSession
from fxlab.execution.recovery import create_checkpoint
from fxlab.execution.runtime_control import RuntimeState
from fxlab.operations.control import (
    ControlAction,
    ControlRequest,
    LocalControlServer,
    ServiceState,
    send_control_request,
)
from fxlab.operations.security import FileSecretResolver
from fxlab.operations.service import (
    InstanceLock,
    ObservationService,
    OperationalLogger,
    ServiceResult,
    StartupMode,
)


def _bars(periods: int = 8) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=periods, freq="5min", tz="UTC")
    close = np.arange(periods, dtype="float64") * 0.0001 + 1.1
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.0002,
            "low": close - 0.0002,
            "close": close,
            "volume": np.ones(periods, dtype="float64"),
        },
        index=index,
        dtype="float64",
    )
    frame.index.name = "ts_open"
    frame.attrs.update(symbol="EURUSD", timeframe="M5")
    return frame


def _inputs(tmp_path: Path, *, runtime_id: str = "service-runtime"):
    from fxlab.operations.service import OperationalConfig

    state = (tmp_path / "state").resolve()
    state.mkdir()
    secret_path = (tmp_path / "control.secret").resolve()
    secret_path.write_bytes(b"q" * 32)
    config = OperationalConfig(
        format_version=1,
        state_directory=state,
        runtime_id=runtime_id,
        operator_id="operator-one",
        control_secret_file=secret_path,
        endpoint_id=f"endpoint-{uuid.uuid4().hex}",
        log_filename=f"{runtime_id}.jsonl",
    )
    data_dir = tmp_path / "data"
    save_bars(_bars(), data_dir, "EURUSD", "M5")
    request = ReplayRequest(
        session_id="service-session",
        store_path=config.store_path,
        data_dir=data_dir,
        symbol="EURUSD",
        timeframe="M5",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 40, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, 0, 40, tzinfo=UTC),
        observe_only=True,
    )
    return config, request


def _control(config, action: ControlAction):
    secret = FileSecretResolver().resolve(config.control_secret_file)
    return send_control_request(
        config,
        secret,
        ControlRequest(1, str(uuid.uuid4()), action),
    )


def test_service_contracts_are_frozen_and_process_state_is_separate() -> None:
    result = ServiceResult(0, ServiceState.STOPPED, "replay_exhausted", "session", 3)
    assert StartupMode.FRESH.value == "fresh"
    assert StartupMode.RECOVER.value == "recover"
    assert result.service_state is ServiceState.STOPPED
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        result.cycles = 4  # type: ignore[misc]


def test_fresh_service_exhausts_checkpoints_logs_and_releases_lock(tmp_path: Path) -> None:
    config, request = _inputs(tmp_path)
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)

    result = service.run()

    assert result.exit_code == 0
    assert result.service_state is ServiceState.STOPPED
    assert result.reason == "replay_exhausted"
    assert config.store_path.exists()
    assert config.log_path.exists()
    snapshot = recover_snapshot(request, load_config())
    assert snapshot.orders == ()
    assert snapshot.positions == ()
    lock = InstanceLock(config.lock_path, "post-service-check")
    lock.acquire()
    lock.release()


def test_pause_resume_service_remains_structurally_observation_only(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="pause-resume-service")
    first_cycle = threading.Event()
    allow_second = threading.Event()
    second_cycle = threading.Event()
    allow_finish = threading.Event()
    original = PaperTradingSession.poll_once
    calls = 0

    def gated_poll(session: PaperTradingSession, *, until=None):
        nonlocal calls
        result = original(session, until=until)
        calls += 1
        if calls == 1:
            first_cycle.set()
            assert allow_second.wait(5)
        elif calls == 2:
            second_cycle.set()
            assert allow_finish.wait(5)
        return result

    monkeypatch.setattr(PaperTradingSession, "poll_once", gated_poll)
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)
    outcome: list[ServiceResult] = []
    thread = threading.Thread(target=lambda: outcome.append(service.run()))
    thread.start()
    assert first_cycle.wait(5)

    secret = FileSecretResolver().resolve(config.control_secret_file)
    pause_request = ControlRequest(1, str(uuid.uuid4()), ControlAction.PAUSE)
    paused = send_control_request(config, secret, pause_request)
    duplicate_pause = send_control_request(config, secret, pause_request)
    conflicting_stop = send_control_request(
        config,
        secret,
        ControlRequest(1, pause_request.request_id, ControlAction.STOP),
    )
    assert paused.accepted and paused.changed
    assert duplicate_pause == paused
    assert not conflicting_stop.accepted
    assert conflicting_stop.reason == "request_id_conflict"
    allow_second.set()
    assert second_cycle.wait(5)
    resumed = _control(config, ControlAction.RESUME)
    assert resumed.accepted and resumed.changed
    status = _control(config, ControlAction.STATUS)
    assert dict(status.payload)["source"] == "live_runtime"
    allow_finish.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and outcome[0].exit_code == 0

    snapshot = recover_snapshot(request, load_config())
    assert snapshot.orders == ()
    assert snapshot.positions == ()
    assert snapshot.status["order_count"] == 0
    assert snapshot.status["reservation_count"] == 0
    events = service.last_events
    forbidden = {
        AuditEventType.SIGNAL_EMITTED,
        AuditEventType.EXECUTION_INTENT_CREATED,
        AuditEventType.RISK_APPROVED,
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        AuditEventType.ORDER_SUBMITTED,
    }
    assert not forbidden.intersection(event.event_type for event in events)
    operator_events = [
        event
        for event in events
        if event.event_type is AuditEventType.OPERATOR_CONTROL_ACTION
    ]
    assert [event.payload["action"] for event in operator_events] == ["pause", "resume"]
    assert all(event.payload["actor_id"] == "operator-one" for event in operator_events)


def test_recovered_running_session_is_paused_before_maintenance_and_resume(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="recover-service")
    app = assemble_observation_replay(
        request, load_config(), fresh=True, runtime_id=config.runtime_id
    )
    app.session.start()
    app.session.poll_once()
    create_checkpoint(
        app.session,
        app.store,
        software_version=app.software_version,
        execution_policy_id=app.execution_policy_id,
    )
    app.close()

    observed = threading.Event()
    allow_finish = threading.Event()
    original = PaperTradingSession.poll_once

    def gated_poll(session: PaperTradingSession, *, until=None):
        result = original(session, until=until)
        observed.set()
        assert allow_finish.wait(5)
        return result

    monkeypatch.setattr(PaperTradingSession, "poll_once", gated_poll)
    service = ObservationService(config, request, load_config(), StartupMode.RECOVER)
    outcome: list[ServiceResult] = []
    thread = threading.Thread(target=lambda: outcome.append(service.run()))
    thread.start()
    assert observed.wait(5)
    status = _control(config, ControlAction.STATUS)
    assert status.runtime_state is RuntimeState.PAUSED
    resumed = _control(config, ControlAction.RESUME)
    assert resumed.accepted and resumed.changed
    allow_finish.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and outcome[0].exit_code == 0


def test_recover_never_falls_back_to_fresh_store(tmp_path: Path) -> None:
    config, request = _inputs(tmp_path, runtime_id="missing-recovery")
    service = ObservationService(config, request, load_config(), StartupMode.RECOVER)
    result = service.run()
    assert result.exit_code == 5
    assert result.service_state is ServiceState.FAILED
    assert result.reason == "store_missing"
    assert not config.store_path.exists()
    assert service.state is ServiceState.FAILED


def test_authenticated_stop_enters_stopping_and_rejects_later_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="controlled-stop-service")
    cycle_finished = threading.Event()
    allow_return = threading.Event()
    original = PaperTradingSession.poll_once

    def gated_poll(session: PaperTradingSession, *, until=None):
        result = original(session, until=until)
        cycle_finished.set()
        assert allow_return.wait(5)
        return result

    monkeypatch.setattr(PaperTradingSession, "poll_once", gated_poll)
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)
    outcomes: list[ServiceResult] = []
    thread = threading.Thread(target=lambda: outcomes.append(service.run()))
    thread.start()
    assert cycle_finished.wait(5)

    stopped = _control(config, ControlAction.STOP)
    assert stopped.accepted and stopped.changed
    assert stopped.service_state is ServiceState.STOPPING
    rejected = _control(config, ControlAction.RESUME)
    assert not rejected.accepted
    assert rejected.reason == "service_not_running"

    allow_return.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcomes[0].exit_code == 0
    assert outcomes[0].reason == "operator_stop"
    assert service.state is ServiceState.STOPPED
    controls = [
        event
        for event in service.last_events
        if event.event_type is AuditEventType.OPERATOR_CONTROL_ACTION
    ]
    assert [event.payload["action"] for event in controls] == ["stop", "resume"]
    assert controls[1].payload["accepted"] is False
    assert controls[1].payload["reason"] == "service_not_running"


def test_recovery_of_stopped_checkpoint_is_blocked_without_runtime_mutation(
    tmp_path: Path,
) -> None:
    config, request = _inputs(tmp_path, runtime_id="stopped-recovery-service")
    fresh = ObservationService(config, request, load_config(), StartupMode.FRESH)
    assert fresh.run().exit_code == 0
    before = config.store_path.read_bytes()

    recovered = ObservationService(config, request, load_config(), StartupMode.RECOVER)
    result = recovered.run()

    assert result.exit_code == 5
    assert result.service_state is ServiceState.FAILED
    assert result.reason == "recovered_stopped_blocked"
    assert recovered.state is ServiceState.FAILED
    assert config.store_path.read_bytes() == before


def test_startup_logging_failure_fails_preflight_and_releases_instance_lock(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="log-preflight-failure")

    def fail_open(_logger: OperationalLogger) -> None:
        raise OSError("synthetic log failure")

    monkeypatch.setattr(OperationalLogger, "open", fail_open)
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)
    result = service.run()

    assert result.exit_code == 3
    assert result.service_state is ServiceState.FAILED
    assert result.reason == "service_failed"
    assert not config.store_path.exists()
    lock = InstanceLock(config.lock_path, "after-log-failure")
    lock.acquire()
    lock.release()


def test_runtime_logging_failure_requests_fail_closed_shutdown(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="runtime-log-failure")
    cycle_finished = threading.Event()
    allow_return = threading.Event()
    original_poll = PaperTradingSession.poll_once
    original_write = OperationalLogger.write

    def gated_poll(session: PaperTradingSession, *, until=None):
        result = original_poll(session, until=until)
        cycle_finished.set()
        assert allow_return.wait(5)
        return result

    def fail_control_log(logger: OperationalLogger, **fields) -> None:
        if fields.get("action") is not None:
            raise RuntimeError("synthetic runtime log failure")
        original_write(logger, **fields)

    monkeypatch.setattr(PaperTradingSession, "poll_once", gated_poll)
    monkeypatch.setattr(OperationalLogger, "write", fail_control_log)
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)
    outcomes: list[ServiceResult] = []
    thread = threading.Thread(target=lambda: outcomes.append(service.run()))
    thread.start()
    assert cycle_finished.wait(5)

    response = _control(config, ControlAction.PAUSE)
    assert response.accepted
    allow_return.set()
    thread.join(10)

    assert not thread.is_alive()
    assert outcomes[0].exit_code == 3
    assert outcomes[0].reason == "operational_log_failed"
    assert service.state is ServiceState.FAILED


def test_listener_failure_requests_fail_closed_shutdown(tmp_path: Path, monkeypatch) -> None:
    config, request = _inputs(tmp_path, runtime_id="listener-failure")

    def fail_listener(server: LocalControlServer) -> None:
        assert server._failure_handler is not None
        server._failure_handler()

    monkeypatch.setattr(LocalControlServer, "_serve", fail_listener)
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)
    result = service.run()

    assert result.exit_code == 3
    assert result.reason == "control_service_failed"
    assert result.service_state is ServiceState.FAILED
    assert service.state is ServiceState.FAILED


def test_signal_handler_never_enters_session_cycle_lock(tmp_path: Path) -> None:
    config, request = _inputs(tmp_path, runtime_id="signal-safe")
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)
    cycle_lock = threading.Lock()
    entered = threading.Event()

    class Session:
        def request_stop(self) -> None:
            entered.set()
            with cycle_lock:
                pass

    service._application = type("App", (), {"session": Session()})()
    cycle_lock.acquire()
    finished = threading.Event()
    worker = threading.Thread(
        target=lambda: (service._request_signal_shutdown(15, None), finished.set()),
        daemon=True,
    )
    worker.start()
    try:
        assert finished.wait(0.5)
        assert not entered.is_set()
    finally:
        cycle_lock.release()
        worker.join(1)


def test_status_is_unavailable_until_recovery_is_coherently_published(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="recovery-readiness")
    fresh = ObservationService(config, request, load_config(), StartupMode.FRESH)
    assert fresh.run().exit_code == 0

    entered = threading.Event()
    release = threading.Event()
    original_recover = service_module.recover
    monitoring_calls = 0
    original_monitoring = PaperTradingSession.monitoring_snapshot

    def blocked_recover(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_recover(*args, **kwargs)

    def counted_monitoring(session):
        nonlocal monitoring_calls
        monitoring_calls += 1
        return original_monitoring(session)

    monkeypatch.setattr(service_module, "recover", blocked_recover)
    monkeypatch.setattr(PaperTradingSession, "monitoring_snapshot", counted_monitoring)
    service = ObservationService(config, request, load_config(), StartupMode.RECOVER)
    outcome: list[ServiceResult] = []
    thread = threading.Thread(target=lambda: outcome.append(service.run()))
    thread.start()
    assert entered.wait(5)
    secret = FileSecretResolver().resolve(config.control_secret_file)
    with __import__("pytest").raises(RuntimeError, match="unavailable"):
        send_control_request(
            config,
            secret,
            ControlRequest(1, str(uuid.uuid4()), ControlAction.STATUS),
        )
    assert monitoring_calls == 0
    release.set()
    thread.join(10)
    assert not thread.is_alive()


def test_wrong_authentication_and_status_are_read_only_for_live_session(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="read-only-controls")
    cycle_done = threading.Event()
    release = threading.Event()
    original = PaperTradingSession.poll_once

    def gated_poll(session: PaperTradingSession, *, until=None):
        result = original(session, until=until)
        cycle_done.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(PaperTradingSession, "poll_once", gated_poll)
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)
    outcomes: list[ServiceResult] = []
    thread = threading.Thread(target=lambda: outcomes.append(service.run()))
    thread.start()
    assert cycle_done.wait(5)
    session = service._application.session
    before_runtime = session.runtime_status()
    before_events = session.event_ledger.events()
    before_risk = session.risk_engine.snapshot_state()
    before_checkpoints = service._application.store.load_latest_checkpoint()

    wrong_path = tmp_path / "wrong.secret"
    wrong_path.write_bytes(b"w" * 32)
    wrong = FileSecretResolver().resolve(wrong_path)
    with __import__("pytest").raises(RuntimeError, match="authentication"):
        send_control_request(
            config,
            wrong,
            ControlRequest(1, str(uuid.uuid4()), ControlAction.PAUSE),
        )
    status = _control(config, ControlAction.STATUS)
    assert status.accepted
    assert session.runtime_status() == before_runtime
    assert session.event_ledger.events() == before_events
    assert session.risk_engine.snapshot_state() == before_risk
    assert service._application.store.load_latest_checkpoint() == before_checkpoints
    release.set()
    thread.join(10)
    assert not thread.is_alive()


def test_shutdown_failures_do_not_prevent_remaining_resource_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="cleanup-failures")
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)
    calls: list[str] = []

    class Ledger:
        def events(self):
            calls.append("events")
            return ()

    class Session:
        event_ledger = Ledger()

        def request_stop(self):
            calls.append("request_stop")

        def complete_stop(self, **kwargs):
            calls.append("complete_stop")
            raise RuntimeError("credential-bearing checkpoint failure")

    class Application:
        session = Session()
        store = object()
        software_version = "test"
        execution_policy_id = "observe-only-v1"

        def close(self):
            calls.append("store_close")
            raise RuntimeError("sensitive store failure")

    class Logger:
        def write(self, **kwargs):
            calls.append("log_write")

        def close(self):
            calls.append("log_close")

    secret = FileSecretResolver().resolve(config.control_secret_file)
    server = LocalControlServer(config, secret, lambda request: None)  # type: ignore[arg-type]

    def fail_control_close(self):
        calls.append("control_close")
        raise RuntimeError("sensitive control failure")

    monkeypatch.setattr(LocalControlServer, "close", fail_control_close)
    service._application = Application()  # type: ignore[assignment]
    service._session_activated = True
    service._control_server = server
    service._logger = Logger()  # type: ignore[assignment]

    failure = service._shutdown(
        ServiceResult(0, ServiceState.STOPPED, "replay_exhausted", request.session_id)
    )

    assert failure == "session_shutdown_failed"
    assert calls == [
        "request_stop",
        "complete_stop",
        "events",
        "control_close",
        "store_close",
        "log_write",
        "log_close",
    ]


def test_shutdown_failure_result_is_failed_and_instance_lock_is_released(
    tmp_path: Path, monkeypatch
) -> None:
    config, request = _inputs(tmp_path, runtime_id="cleanup-result")
    released = threading.Event()
    original_release = InstanceLock.release

    def tracked_release(lock: InstanceLock) -> None:
        original_release(lock)
        released.set()

    monkeypatch.setattr(InstanceLock, "release", tracked_release)
    monkeypatch.setattr(
        ObservationService,
        "_shutdown",
        lambda self, outcome: "session_shutdown_failed",
    )
    service = ObservationService(config, request, load_config(), StartupMode.FRESH)

    result = service.run()

    assert result.exit_code == 3
    assert result.service_state is ServiceState.FAILED
    assert result.reason == "session_shutdown_failed"
    assert released.is_set()
