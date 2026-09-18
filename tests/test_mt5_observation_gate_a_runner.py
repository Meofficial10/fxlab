"""Tests for MT5 DEMO Observation Gate A Runner (Offline Simulated & Mock Tests)."""

import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure scripts/ directory is in sys.path
scripts_dir = Path(__file__).parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

import run_mt5_observation_gate_a  # noqa: E402
from run_mt5_observation_gate_a import (  # noqa: E402
    GateAConfig,
    Mt5ObservationGateARunner,
    main,
)

from fxlab.execution.durable_event_store import SQLiteEventStore  # noqa: E402
from fxlab.execution.event_ledger import AuditComponent, AuditEventType, EventLedger  # noqa: E402
from fxlab.execution.mt5_demo_broker import MT5_DEMO_MAGIC, MT5_DEMO_SYMBOL  # noqa: E402
from fxlab.execution.runtime_control import RuntimeState  # noqa: E402
from fxlab.operations.control import (  # noqa: E402
    ControlAction,
    ControlResponse,
    ControlSecret,
    ServiceState,
    freeze_payload,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class MockMt5Api:
    """Mock MetaTrader 5 API for deterministic offline Gate A runner testing."""

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_FILLING_IOC = 2
    TRADE_RETCODE_DONE = 10009

    def __init__(self) -> None:
        self.initialized = False
        self.terminal_info_obj = SimpleNamespace(
            connected=True,
            trade_allowed=True,
            demo=True,
            name="MetaTrader 5",
            path="C:\\Program Files\\MetaTrader 5\\terminal64.exe",
        )
        self.account = SimpleNamespace(
            login=12345678,
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            server="Pepperstone-Demo",
            company="Pepperstone Group Limited",
            currency="USD",
            leverage=100,
            balance=10000.0,
            equity=10000.0,
            margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
            trade_allowed=True,
            trade_expert=True,
        )
        self.symbol_info_obj = SimpleNamespace(
            name=MT5_DEMO_SYMBOL,
            visible=True,
            trade_mode=self.SYMBOL_TRADE_MODE_FULL,
            filling_mode=self.SYMBOL_FILLING_IOC,
            bid=1.08500,
            ask=1.08520,
            point=0.00001,
            digits=5,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_contract_size=100000.0,
        )
        self.tick = SimpleNamespace(
            time=int(NOW.timestamp()),
            time_msc=int(NOW.timestamp() * 1000),
            bid=1.08500,
            ask=1.08520,
            last=0.0,
            volume=0,
            flags=6,
        )
        self.positions: list[object] = []
        self.orders: list[object] = []
        self.history_deals: list[object] = []
        self.history_orders: list[object] = []
        self.order_send_calls: list[dict] = []
        self.shutdown_called = False

    def initialize(self, **kwargs) -> bool:
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True
        self.initialized = False

    def version(self) -> tuple[int, int, str]:
        return (500, 4500, "24 May 2024")

    def terminal_info(self) -> object:
        return self.terminal_info_obj

    def account_info(self) -> object:
        return self.account

    def symbol_info(self, symbol: str) -> object:
        if symbol == MT5_DEMO_SYMBOL:
            return self.symbol_info_obj
        return None

    def symbol_info_tick(self, symbol: str) -> object:
        if symbol == MT5_DEMO_SYMBOL:
            self.tick.time_msc += 1
            return self.tick
        return None

    def positions_get(self, **kwargs) -> tuple:
        return tuple(self.positions)

    def orders_get(self, **kwargs) -> tuple:
        return tuple(self.orders)

    def history_deals_get(self, **kwargs) -> tuple:
        return tuple(self.history_deals)

    def history_orders_get(self, **kwargs) -> tuple:
        return tuple(self.history_orders)

    def order_send(self, request: dict) -> object:
        self.order_send_calls.append(request)
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=99999,
            deal=88888,
            volume=0.01,
            price=1.10000,
        )


def _make_runner_config(
    tmp_path: Path,
    api: MockMt5Api,
    *,
    runtime_id: str | None = None,
    session_id: str | None = None,
    sleeper=None,
    clock=None,
) -> GateAConfig:
    unique_id = uuid.uuid4().hex[:12]
    rt_id = runtime_id or f"rt_{unique_id}"
    sess_id = session_id or f"sess_{unique_id}"
    return GateAConfig(
        state_directory=(tmp_path / f"gate_a_{unique_id}").resolve(),
        runtime_id=rt_id,
        session_id=sess_id,
        symbol=MT5_DEMO_SYMBOL,
        observe_phase1_seconds=0.05,
        pause_phase_seconds=0.05,
        observe_phase2_seconds=0.05,
        poll_interval_seconds=0.01,
        max_quote_age_seconds=5.0,
        control_timeout_seconds=2.0,
        service_start_timeout_seconds=3.0,
        service_stop_timeout_seconds=3.0,
        api=api,
        sleeper=sleeper or time.sleep,
        clock=clock or (lambda: NOW),
    )


# ===========================================================================
# CLI TESTS (DEFECT A)
# ===========================================================================
def test_cli_help_exits_zero(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower() or "run_mt5_observation_gate_a" in captured.out


def test_cli_no_args_exits_zero_without_running(capsys: pytest.CaptureFixture) -> None:
    code = main([])
    assert code == 0
    captured = capsys.readouterr()
    assert "Notice: Pass '--run'" in captured.out


def test_cli_unknown_arg_exits_two(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--unknown-option-xyz"])
    assert exc_info.value.code == 2


# ===========================================================================
# OPERATIONAL & DIAGNOSTIC TESTS (DEFECT B & C)
# ===========================================================================
def test_initial_non_flat_positions_aborts(tmp_path: Path) -> None:
    api = MockMt5Api()
    api.positions.append(
        SimpleNamespace(
            ticket=12345,
            symbol="EURUSD",
            type=0,
            volume=0.01,
            magic=MT5_DEMO_MAGIC,
            comment="pre_existing",
        )
    )
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "initial_exposure_not_flat"
    assert res.initial_positions == 1
    assert len(api.order_send_calls) == 0


def test_initial_pending_orders_aborts(tmp_path: Path) -> None:
    api = MockMt5Api()
    api.orders.append(
        SimpleNamespace(
            ticket=67890,
            symbol="EURUSD",
            volume_initial=0.01,
            type=0,
            magic=MT5_DEMO_MAGIC,
            comment="pre_existing_order",
        )
    )
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "initial_exposure_not_flat"
    assert res.initial_pending_orders == 1
    assert len(api.order_send_calls) == 0


def test_status_response_none_gives_precise_diagnostic_and_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request
    started = False

    def mock_send(cfg, secret, req):
        nonlocal started
        resp = orig_send(cfg, secret, req)
        if (
            req.action == ControlAction.STATUS
            and resp is not None
            and resp.accepted
            and resp.service_state is ServiceState.RUNNING
        ):
            if started:
                raise RuntimeError("simulated_socket_disconnect")
            started = True
        return resp

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert "status_response_missing" in str(res.failure_reason)
    assert "simulated_socket_disconnect" in str(res.failure_reason)
    # Finalization must have run and proven zero mutations
    assert res.final_positions == 0
    assert res.final_pending_orders == 0
    assert res.control_server_stopped is True
    assert res.instance_lock_released is True
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_status_rejected_gives_precise_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request
    started = False

    def mock_send(cfg, secret, req):
        nonlocal started
        resp = orig_send(cfg, secret, req)
        if (
            req.action == ControlAction.STATUS
            and resp is not None
            and resp.accepted
            and resp.service_state is ServiceState.RUNNING
        ):
            if started:
                return ControlResponse(
                    protocol_version=req.protocol_version,
                    request_id=req.request_id,
                    accepted=False,
                    changed=False,
                    service_state=ServiceState.RUNNING,
                    runtime_state=RuntimeState.RUNNING,
                    reason="custom_rejection_reason",
                )
            started = True
        return resp

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert "status_request_rejected: custom_rejection_reason" in str(res.failure_reason)
    assert res.final_positions == 0
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_status_unexpected_runtime_state_gives_precise_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request
    started = False

    def mock_send(cfg, secret, req):
        nonlocal started
        resp = orig_send(cfg, secret, req)
        if (
            req.action == ControlAction.STATUS
            and resp is not None
            and resp.accepted
            and resp.service_state is ServiceState.RUNNING
        ):
            if started:
                return ControlResponse(
                    protocol_version=req.protocol_version,
                    request_id=req.request_id,
                    accepted=True,
                    changed=False,
                    service_state=ServiceState.RUNNING,
                    runtime_state=RuntimeState.PAUSED,
                    reason="status_ok",
                    payload=freeze_payload({"ticks_observed": 10}),
                )
            started = True
        return resp

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert "unexpected_runtime_state: paused" in str(res.failure_reason)
    assert res.final_positions == 0
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_status_malformed_payload_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request
    started = False

    def mock_send(cfg, secret, req):
        nonlocal started
        resp = orig_send(cfg, secret, req)
        if (
            req.action == ControlAction.STATUS
            and resp is not None
            and resp.accepted
            and resp.service_state is ServiceState.RUNNING
        ):
            if started:
                return ControlResponse(
                    protocol_version=req.protocol_version,
                    request_id=req.request_id,
                    accepted=True,
                    changed=False,
                    service_state=ServiceState.RUNNING,
                    runtime_state=RuntimeState.RUNNING,
                    reason="status_ok",
                    payload=freeze_payload({"wrong_key": "bad_data"}),
                )
            started = True
        return resp

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert "invalid_status_payload: ticks_observed_missing_or_invalid" in str(res.failure_reason)
    assert res.final_positions == 0
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_pause_failure_fails_gate_and_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request

    def mock_send(cfg, secret, req):
        if req.action == ControlAction.PAUSE:
            return ControlResponse(
                protocol_version=req.protocol_version,
                request_id=req.request_id,
                accepted=False,
                changed=False,
                service_state=ServiceState.RUNNING,
                runtime_state=RuntimeState.RUNNING,
                reason="pause_simulated_failure",
            )
        return orig_send(cfg, secret, req)

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "pause_request_rejected"
    assert res.final_positions == 0
    assert res.final_pending_orders == 0
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_ticks_changing_during_pause_fails_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request
    paused_status_count = 0

    def mock_send(cfg, secret, req):
        nonlocal paused_status_count
        resp = orig_send(cfg, secret, req)
        if (
            req.action == ControlAction.STATUS
            and resp is not None
            and resp.accepted
            and resp.runtime_state is RuntimeState.PAUSED
        ):
            paused_status_count += 1
            if paused_status_count == 2:
                corrupted = dict(resp.payload)
                corrupted["ticks_observed"] = int(corrupted.get("ticks_observed", 0)) + 10
                return ControlResponse(
                    protocol_version=resp.protocol_version,
                    request_id=resp.request_id,
                    accepted=resp.accepted,
                    changed=resp.changed,
                    service_state=resp.service_state,
                    runtime_state=resp.runtime_state,
                    reason=resp.reason,
                    payload=freeze_payload(corrupted),
                )
        return resp

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "ticks_observed_changed_while_paused"
    assert res.final_positions == 0
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_post_run_non_flat_exposure_fails_gate(tmp_path: Path) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    call_count = 0

    def mock_pos_get(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > 2:  # Post-run check
            return (
                SimpleNamespace(
                    ticket=77777,
                    symbol="EURUSD",
                    type=0,
                    volume=0.01,
                    magic=0,
                    comment="external_injected",
                ),
            )
        return ()

    api.positions_get = mock_pos_get

    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "final_exposure_not_flat"
    assert res.final_positions == 1
    assert res.zero_trade_mutation_proven is False
    assert len(api.order_send_calls) == 0


def test_attributable_mt5_order_fails_gate(tmp_path: Path) -> None:
    api = MockMt5Api()
    api.history_orders.append(
        SimpleNamespace(
            ticket=54321,
            magic=MT5_DEMO_MAGIC,
            symbol="EURUSD",
            type=0,
            volume_initial=0.01,
        )
    )
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "attributable_mt5_history_mutation_detected"
    assert res.mt5_attributable_orders == 1
    assert res.zero_trade_mutation_proven is False
    assert len(api.order_send_calls) == 0


def test_attributable_mt5_deal_fails_gate(tmp_path: Path) -> None:
    api = MockMt5Api()
    api.history_deals.append(
        SimpleNamespace(
            ticket=98765,
            magic=MT5_DEMO_MAGIC,
            symbol="EURUSD",
            type=0,
            volume=0.01,
            entry=0,
            position_id=98765,
        )
    )
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "attributable_mt5_history_mutation_detected"
    assert res.mt5_attributable_deals == 1
    assert res.zero_trade_mutation_proven is False
    assert len(api.order_send_calls) == 0


def test_audit_trade_mutation_event_fails_gate(tmp_path: Path) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    call_count = 0

    def injecting_pos_get(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > 2:
            store_path = config.state_directory / f"{config.runtime_id}.sqlite3"
            if store_path.exists():
                store = SQLiteEventStore(store_path, config.session_id)
                ledger = EventLedger(config.session_id, durable_store=store)
                ledger.append(
                    AuditEventType.ORDER_SUBMITTED,
                    occurred_at=NOW,
                    component=AuditComponent.PAPER_SESSION,
                )
                store.close()
        return ()

    api.positions_get = injecting_pos_get

    res = runner.run(config)

    assert res.status == "FAIL"
    assert "audit_contains_trade_mutation_events" in str(res.failure_reason)
    assert res.zero_trade_mutation_proven is False
    assert len(api.order_send_calls) == 0


def test_evidence_preserved_on_failure(tmp_path: Path) -> None:
    api = MockMt5Api()
    api.positions.append(
        SimpleNamespace(
            ticket=12345,
            symbol="EURUSD",
            type=0,
            volume=0.01,
            magic=0,
            comment="pre_existing",
        )
    )
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    result_json = config.state_directory / "gate_a_result.json"
    assert result_json.exists()
    assert res.status == "FAIL"
    assert "FAIL" in result_json.read_text(encoding="utf-8")


def test_service_exception_fails_gate(tmp_path: Path) -> None:
    api = MockMt5Api()
    poll_count = 0

    def faulty_clock():
        nonlocal poll_count
        poll_count += 1
        if poll_count > 3:
            raise RuntimeError("hardware_clock_fault")
        return NOW

    config = _make_runner_config(tmp_path, api, clock=faulty_clock)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    assert res.status == "FAIL"
    assert len(api.order_send_calls) == 0


def test_non_demo_account_rejected(tmp_path: Path) -> None:
    api = MockMt5Api()
    api.account.trade_mode = api.ACCOUNT_TRADE_MODE_REAL
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    assert res.status == "FAIL"
    assert "non_demo" in str(res.failure_reason) or "preflight" in str(res.failure_reason)
    assert len(api.order_send_calls) == 0


def test_resume_failure_fails_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request

    def mock_send(cfg, secret, req):
        if req.action == ControlAction.RESUME:
            return ControlResponse(
                protocol_version=req.protocol_version,
                request_id=req.request_id,
                accepted=False,
                changed=False,
                service_state=ServiceState.RUNNING,
                runtime_state=RuntimeState.PAUSED,
                reason="resume_simulated_failure",
            )
        return orig_send(cfg, secret, req)

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "resume_request_rejected"
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_ticks_not_increasing_after_resume_fails_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request
    resumed = False

    def mock_send(cfg, secret, req):
        nonlocal resumed
        resp = orig_send(cfg, secret, req)
        if req.action == ControlAction.RESUME:
            resumed = True
        elif (
            req.action == ControlAction.STATUS
            and resumed
            and resp is not None
            and resp.accepted
        ):
            corrupted = dict(resp.payload)
            corrupted["ticks_observed"] = 0
            return ControlResponse(
                protocol_version=resp.protocol_version,
                request_id=resp.request_id,
                accepted=resp.accepted,
                changed=resp.changed,
                service_state=resp.service_state,
                runtime_state=resp.runtime_state,
                reason=resp.reason,
                payload=freeze_payload(corrupted),
            )
        return resp

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "ticks_observed_did_not_increase_after_resume"
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_stop_failure_fails_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request

    def mock_send(cfg, secret, req):
        if req.action == ControlAction.STOP:
            return ControlResponse(
                protocol_version=req.protocol_version,
                request_id=req.request_id,
                accepted=False,
                changed=False,
                service_state=ServiceState.RUNNING,
                runtime_state=RuntimeState.RUNNING,
                reason="stop_simulated_failure",
            )
        return orig_send(cfg, secret, req)

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "stop_request_rejected"
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_control_authentication_failure_fails_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    orig_send = run_mt5_observation_gate_a.send_control_request

    def mock_send_bad_auth(cfg, secret, req):
        bad_secret = ControlSecret(b"0" * 32)
        return orig_send(cfg, bad_secret, req)

    monkeypatch.setattr(run_mt5_observation_gate_a, "send_control_request", mock_send_bad_auth)
    res = runner.run(config)

    assert res.status == "FAIL"
    assert res.failure_reason == "service_failed_to_start_or_control_unavailable"
    assert len(api.order_send_calls) == 0


def test_ctrl_c_cannot_pass(tmp_path: Path) -> None:
    api = MockMt5Api()

    def interrupt_sleeper(seconds: float) -> None:
        if seconds == 0.05:
            raise KeyboardInterrupt("simulated operator interrupt")
        time.sleep(0.01)

    config = _make_runner_config(tmp_path, api, sleeper=interrupt_sleeper)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    assert res.status == "INTERRUPTED"
    assert res.status != "PASS"
    assert res.failure_reason == "operator_interrupted"
    assert res.zero_trade_mutation_proven is True
    assert len(api.order_send_calls) == 0


def test_successful_simulated_sequence_passes_zero_mutations(tmp_path: Path) -> None:
    api = MockMt5Api()
    config = _make_runner_config(tmp_path, api)
    runner = Mt5ObservationGateARunner()

    res = runner.run(config)

    assert res.status == "PASS"
    assert res.failure_reason is None
    assert res.status_ok is True
    assert res.pause_ok is True
    assert res.paused_ticks_frozen is True
    assert res.resume_ok is True
    assert res.post_resume_ticks_increased is True
    assert res.stop_ok is True
    assert res.initial_positions == 0
    assert res.initial_pending_orders == 0
    assert res.final_positions == 0
    assert res.final_pending_orders == 0
    assert res.zero_trade_mutation_proven is True
    assert res.control_server_stopped is True
    assert res.instance_lock_released is True
    assert len(api.order_send_calls) == 0
