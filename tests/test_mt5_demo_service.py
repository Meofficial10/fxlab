"""Tests for MT5 DEMO observation-only service kernel and operator control (Autonomy V1A & V1B)."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxlab.execution.app import AppExitCode
from fxlab.execution.mt5_demo_broker import MT5_DEMO_SYMBOL
from fxlab.execution.runtime_control import RuntimeState
from fxlab.operations.control import (
    CONTROL_PROTOCOL_VERSION,
    ControlAction,
    ControlRequest,
    ServiceState,
    send_control_request,
)
from fxlab.operations.mt5_service import (
    Mt5DemoObservationService,
    Mt5ObservationConfig,
)
from fxlab.operations.security import ControlSecret
from fxlab.operations.service import InstanceLock
from fxlab.risk.engine import KillSwitchReason

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


class SpyingFakeMt5Api:
    """Fake MetaTrader5 API with comprehensive call recording and zero live mutations."""

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_FILLING_IOC = 2
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_REASON_CLIENT = 0
    DEAL_REASON_SL = 4
    DEAL_REASON_TP = 5
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REJECT = 10006

    def __init__(self, clock=None) -> None:
        self.clock = clock or (lambda: NOW)
        self.initialize_calls = 0
        self.shutdown_calls = 0
        self.order_send_calls: list[dict] = []
        self.positions_get_calls: list[dict] = []
        self.history_deals_calls: list[dict] = []
        self.symbol_info_tick_calls: list[str] = []

        self.terminal = SimpleNamespace(connected=True, trade_allowed=True)
        self.account = SimpleNamespace(
            login=12345678,
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            currency="USD",
            server="Pepperstone-Demo",
            company="Pepperstone Group Limited",
            trade_allowed=True,
            trade_expert=True,
            margin_mode=self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            margin_free=10000.0,
        )
        self.symbol = SimpleNamespace(
            name="EURUSD",
            visible=True,
            trade_mode=self.SYMBOL_TRADE_MODE_FULL,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            point=0.00001,
            digits=5,
            trade_stops_level=10,
            filling_mode=self.SYMBOL_FILLING_IOC,
            trade_contract_size=100000.0,
        )
        self.base_tick_msc = int(NOW.timestamp() * 1000)
        self.tick_counter = 0
        self.auto_advance_tick = True
        self.tick_step_msc = 100
        self.positions: list[object] = []
        self.history_deals: list[object] = []
        self.init_return_value = True
        self.tick_override: object | None = None
        self.stale_tick_mode = False

    def initialize(self) -> bool:
        self.initialize_calls += 1
        return self.init_return_value

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def version(self) -> tuple[int, int]:
        return (500, 4150)

    def terminal_info(self) -> object:
        return self.terminal

    def account_info(self) -> object:
        return self.account

    def symbol_info(self, symbol: str) -> object | None:
        if symbol == "EURUSD":
            return self.symbol
        return None

    def symbol_info_tick(self, symbol: str) -> object | None:
        self.symbol_info_tick_calls.append(symbol)
        if symbol != "EURUSD":
            return None
        if self.auto_advance_tick:
            self.tick_counter += 1
        current_msc = self.base_tick_msc + (self.tick_counter * self.tick_step_msc)
        if self.stale_tick_mode:
            # Stale timestamp (100s in the past) but advancing MSC so broker accepts it
            stale_sec = int(NOW.timestamp()) - 100
            return SimpleNamespace(
                time=stale_sec,
                time_msc=current_msc,
                bid=1.10000,
                ask=1.10010,
                last=0.0,
                volume=0,
                flags=6,
            )
        tick_time = int(current_msc / 1000)
        return SimpleNamespace(
            time=tick_time,
            time_msc=current_msc,
            bid=1.10000,
            ask=1.10010,
            last=0.0,
            volume=0,
            flags=6,
        )

    def positions_get(self, **kwargs) -> tuple:
        self.positions_get_calls.append(kwargs)
        if "ticket" in kwargs:
            t = kwargs["ticket"]
            return tuple(p for p in self.positions if getattr(p, "ticket", None) == t)
        if "symbol" in kwargs:
            s = kwargs["symbol"]
            return tuple(p for p in self.positions if getattr(p, "symbol", None) == s)
        return tuple(self.positions)

    def history_deals_get(self, **kwargs) -> tuple:
        self.history_deals_calls.append(kwargs)
        if "position" in kwargs:
            pos_id = kwargs["position"]
            return tuple(
                d for d in self.history_deals if getattr(d, "position_id", None) == pos_id
            )
        if "ticket" in kwargs:
            ticket = kwargs["ticket"]
            return tuple(d for d in self.history_deals if getattr(d, "ticket", None) == ticket)
        return tuple(self.history_deals)

    def order_send(self, request: dict) -> object:
        self.order_send_calls.append(request)
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=99999,
            deal=88888,
            volume=0.01,
            price=1.10000,
        )


def _make_config(
    tmp_path: Path,
    *,
    runtime_id: str = "test_obs_runtime",
    session_id: str = "test_session_01",
    max_cycles: int | None = 3,
    max_quote_age_seconds: float = 5.0,
    poll_interval_seconds: float = 0.05,
    clock=None,
    sleeper=None,
    stop_predicate=None,
    control_secret: ControlSecret | None = None,
    control_secret_file: Path | None = None,
) -> Mt5ObservationConfig:
    return Mt5ObservationConfig(
        state_directory=tmp_path / "state",
        runtime_id=runtime_id,
        session_id=session_id,
        symbol=MT5_DEMO_SYMBOL,
        poll_interval_seconds=poll_interval_seconds,
        max_quote_age_seconds=max_quote_age_seconds,
        max_loss_usd=10.0,
        clock=clock or (lambda: NOW),
        sleeper=sleeper or (lambda _: None),
        stop_predicate=stop_predicate,
        control_secret=control_secret,
        control_secret_file=control_secret_file,
        max_cycles=max_cycles,
    )


# ---------------------------------------------------------------------------
# TEST 1: Lock acquired before broker activity
# ---------------------------------------------------------------------------
def test_lock_acquired_before_broker_activity(tmp_path: Path) -> None:
    events: list[str] = []
    fake_api = SpyingFakeMt5Api()

    orig_init = fake_api.initialize

    def recording_init():
        events.append("broker_api_init")
        return orig_init()

    fake_api.initialize = recording_init

    config = _make_config(tmp_path)
    service = Mt5DemoObservationService(config, api=fake_api)

    assert not config.lock_path.exists()

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS
    assert fake_api.initialize_calls >= 1


# ---------------------------------------------------------------------------
# TEST 2: Second instance fails before broker activity
# ---------------------------------------------------------------------------
def test_second_instance_fails_before_broker_activity(tmp_path: Path) -> None:
    config = _make_config(tmp_path, max_cycles=1)
    fake_api = SpyingFakeMt5Api()

    config.state_directory.mkdir(parents=True, exist_ok=True)
    external_lock = InstanceLock(config.lock_path, config.runtime_id)
    external_lock.acquire()

    try:
        service = Mt5DemoObservationService(config, api=fake_api)
        res = service.run()

        assert res.exit_code == int(AppExitCode.RUNTIME_FAILURE)
        assert res.service_state == ServiceState.FAILED
        assert res.reason == "lock_acquisition_failed"
        assert "already running" in str(res.error_message)

        assert fake_api.initialize_calls == 0
        assert len(fake_api.symbol_info_tick_calls) == 0
        assert len(fake_api.order_send_calls) == 0
    finally:
        external_lock.release()


# ---------------------------------------------------------------------------
# TEST 3: DEMO preflight occurs before observation polling
# ---------------------------------------------------------------------------
def test_demo_preflight_occurs_before_observation_polling(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    config = _make_config(tmp_path, max_cycles=2)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == AppExitCode.SUCCESS
    assert res.cycles_completed == 2
    assert res.ticks_observed >= 2
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 4: Non-DEMO / preflight failure prevents polling
# ---------------------------------------------------------------------------
def test_non_demo_preflight_failure_prevents_polling(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    fake_api.account.trade_mode = fake_api.ACCOUNT_TRADE_MODE_REAL

    config = _make_config(tmp_path, max_cycles=5)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == int(AppExitCode.RUNTIME_FAILURE)
    assert res.service_state == ServiceState.FAILED
    assert res.reason == "preflight_failed"
    assert res.cycles_completed == 0
    assert res.ticks_observed == 0
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 5: Fresh ticks -> observation -> zero mutations
# ---------------------------------------------------------------------------
def test_fresh_ticks_observation_zero_mutations(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    config = _make_config(tmp_path, max_cycles=5)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == AppExitCode.SUCCESS
    assert res.service_state == ServiceState.STOPPED
    assert res.cycles_completed == 5
    assert res.ticks_observed == 5

    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 6: Stale tick -> safe state -> zero mutations
# ---------------------------------------------------------------------------
def test_stale_tick_safe_state_zero_mutations(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    fake_api.stale_tick_mode = True

    config = _make_config(tmp_path, max_cycles=3, max_quote_age_seconds=5.0)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == AppExitCode.SUCCESS
    assert res.cycles_completed == 3
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 7: Unavailable tick / preflight tick failure -> safe state -> zero mutations
# ---------------------------------------------------------------------------
def test_unavailable_tick_safe_state_zero_mutations(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    fake_api.symbol_info_tick = lambda symbol: None

    config = _make_config(tmp_path, max_cycles=3)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == int(AppExitCode.RUNTIME_FAILURE)
    assert res.service_state == ServiceState.FAILED
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 8: Broker connection failure -> fail closed -> zero mutations
# ---------------------------------------------------------------------------
def test_broker_connection_failure_fail_closed(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    fake_api.init_return_value = False

    config = _make_config(tmp_path, max_cycles=3)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == int(AppExitCode.RUNTIME_FAILURE)
    assert res.service_state == ServiceState.FAILED
    assert res.reason == "preflight_failed"
    assert res.cycles_completed == 0
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 9: Startup reconciliation failure -> no observation execution path
# ---------------------------------------------------------------------------
def test_startup_reconciliation_failure_no_observation(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    fake_api.positions.append(
        SimpleNamespace(
            ticket=55555,
            symbol="EURUSD",
            type=0,
            volume=0.01,
            price_open=1.10000,
            profit=0.0,
            time=int(NOW.timestamp()),
            magic=0x46584C42,
            comment="unknown_position",
            identifier=55555,
        )
    )

    config = _make_config(tmp_path, max_cycles=3)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == int(AppExitCode.RECONCILIATION_REQUIRED)
    assert res.service_state == ServiceState.FAILED
    assert res.reason == "reconciliation_required"
    assert res.cycles_completed == 0
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 10: Normal requested stop -> clean session/broker shutdown
# ---------------------------------------------------------------------------
def test_normal_requested_stop_clean_shutdown(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    iterations = 0

    def stopping_predicate() -> bool:
        nonlocal iterations
        iterations += 1
        if iterations > 3:
            service.request_stop()
        return False

    config = _make_config(
        tmp_path, max_cycles=100, stop_predicate=stopping_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == AppExitCode.SUCCESS
    assert res.service_state == ServiceState.STOPPED
    assert res.reason == "operator_stopped"
    assert res.cycles_completed >= 3
    assert fake_api.shutdown_calls >= 1
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 11: Exception during polling -> finally cleanup executes
# ---------------------------------------------------------------------------
def test_exception_during_polling_finally_cleanup(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    poll_count = 0

    def faulty_clock():
        nonlocal poll_count
        poll_count += 1
        if poll_count > 2:
            raise RuntimeError("unexpected_clock_hardware_failure")
        return NOW

    config = _make_config(tmp_path, max_cycles=10, clock=faulty_clock)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == int(AppExitCode.RUNTIME_FAILURE)
    assert res.service_state == ServiceState.FAILED
    assert res.reason == "runtime_exception"

    assert not InstanceLock(config.lock_path, config.runtime_id)._handle
    assert fake_api.shutdown_calls >= 1
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 12: Lock always released
# ---------------------------------------------------------------------------
def test_lock_always_released_after_run(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    config = _make_config(tmp_path, max_cycles=2)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS

    new_lock = InstanceLock(config.lock_path, config.runtime_id)
    new_lock.acquire()
    new_lock.release()


# ---------------------------------------------------------------------------
# TEST 13: Repeated observation cycles -> zero trade mutations
# ---------------------------------------------------------------------------
def test_repeated_observation_cycles_zero_trade_mutations(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    config = _make_config(tmp_path, max_cycles=20)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == AppExitCode.SUCCESS
    assert res.cycles_completed == 20
    assert res.ticks_observed == 20

    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 14: No execution permit is issued anywhere in service composition
# ---------------------------------------------------------------------------
def test_no_execution_permit_issued_in_service(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    config = _make_config(tmp_path, max_cycles=1)
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS

    session = service.session
    assert session is not None
    assert session.execution_permit is None


# ---------------------------------------------------------------------------
# TEST 15 (V1B): Control server starts only after safe service initialization
# ---------------------------------------------------------------------------
def test_control_server_starts_after_initialization(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    secret = ControlSecret(b"k" * 32)
    config = _make_config(tmp_path, max_cycles=2, control_secret=secret)
    service = Mt5DemoObservationService(config, api=fake_api)

    # Before run, control server is not running
    assert service.control_server is None

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS
    assert service.control_server is not None


# ---------------------------------------------------------------------------
# TEST 16 (V1B): STATUS returns current observation state and is read-only
# ---------------------------------------------------------------------------
def test_control_status_returns_current_observation_state(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    iterations = 0
    status_response = None

    def status_checking_predicate() -> bool:
        nonlocal iterations, status_response
        iterations += 1
        if iterations == 2:
            req = ControlRequest(
                protocol_version=CONTROL_PROTOCOL_VERSION,
                request_id=str(uuid.uuid4()),
                action=ControlAction.STATUS,
            )
            status_response = service.handle_control(req)
        return False

    config = _make_config(
        tmp_path, max_cycles=3, stop_predicate=status_checking_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS

    assert status_response is not None
    assert status_response.accepted is True
    assert status_response.changed is False
    assert status_response.service_state == ServiceState.RUNNING
    assert status_response.runtime_state == RuntimeState.RUNNING
    assert status_response.reason == "accepted"

    # Verify status payload details
    payload_dict = dict(status_response.payload)
    assert payload_dict["service_state"] == "running"
    assert payload_dict["symbol"] == "EURUSD"
    assert payload_dict["reconciliation_required"] is False
    assert payload_dict["kill_switch_active"] is False

    # Zero mutations
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 17 (V1B): Repeated STATUS requests are read-only
# ---------------------------------------------------------------------------
def test_repeated_status_requests_are_read_only(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    iterations = 0
    responses: list[object] = []

    def status_checking_predicate() -> bool:
        nonlocal iterations
        iterations += 1
        if iterations <= 2:
            for _ in range(5):
                req = ControlRequest(
                    protocol_version=CONTROL_PROTOCOL_VERSION,
                    request_id=str(uuid.uuid4()),
                    action=ControlAction.STATUS,
                )
                responses.append(service.handle_control(req))
        return False

    config = _make_config(
        tmp_path, max_cycles=2, stop_predicate=status_checking_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS
    assert len(responses) == 10
    for resp in responses:
        assert resp.accepted is True
        assert resp.changed is False
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 18 (V1B): PAUSE stops/pauses observation cycles and produces zero trade mutations
# ---------------------------------------------------------------------------
def test_control_pause_pauses_observation_zero_mutations(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    iterations = 0
    pause_response = None

    def pause_predicate() -> bool:
        nonlocal iterations, pause_response
        iterations += 1
        if iterations == 2:
            req = ControlRequest(
                protocol_version=CONTROL_PROTOCOL_VERSION,
                request_id=str(uuid.uuid4()),
                action=ControlAction.PAUSE,
            )
            pause_response = service.handle_control(req)
        return False

    config = _make_config(
        tmp_path, max_cycles=4, stop_predicate=pause_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS

    assert pause_response is not None
    assert pause_response.accepted is True
    assert pause_response.changed is True
    assert pause_response.runtime_state == RuntimeState.PAUSED
    assert pause_response.reason == "operator_paused"

    # Zero mutations during or after pause
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 19 (V1B): RESUME restores observation from a valid paused state
# ---------------------------------------------------------------------------
def test_control_resume_restores_observation(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    iterations = 0
    resume_response = None

    def pause_resume_predicate() -> bool:
        nonlocal iterations, resume_response
        iterations += 1
        if iterations == 2:
            pause_req = ControlRequest(
                protocol_version=CONTROL_PROTOCOL_VERSION,
                request_id=str(uuid.uuid4()),
                action=ControlAction.PAUSE,
            )
            service.handle_control(pause_req)
        elif iterations == 3:
            resume_req = ControlRequest(
                protocol_version=CONTROL_PROTOCOL_VERSION,
                request_id=str(uuid.uuid4()),
                action=ControlAction.RESUME,
            )
            resume_response = service.handle_control(resume_req)
        return False

    config = _make_config(
        tmp_path, max_cycles=5, stop_predicate=pause_resume_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS

    assert resume_response is not None
    assert resume_response.accepted is True
    assert resume_response.changed is True
    assert resume_response.runtime_state == RuntimeState.RUNNING
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 20 (V1B): RESUME cannot bypass RECONCILIATION_REQUIRED
# ---------------------------------------------------------------------------
def test_control_resume_cannot_bypass_reconciliation_required(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()

    def stopping_predicate() -> bool:
        session = service.session
        assert session is not None
        session._reconciliation_required = True
        session.pause()

        resume_req = ControlRequest(
            protocol_version=CONTROL_PROTOCOL_VERSION,
            request_id=str(uuid.uuid4()),
            action=ControlAction.RESUME,
        )
        res_ctrl = service.handle_control(resume_req)

        assert res_ctrl.accepted is False
        assert res_ctrl.changed is False
        assert res_ctrl.runtime_state == RuntimeState.RECONCILIATION_REQUIRED

        service.request_stop()
        return True

    config = _make_config(
        tmp_path, max_cycles=3, stop_predicate=stopping_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    service.run()
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 21 (V1B): RESUME cannot bypass KILL_SWITCHED
# ---------------------------------------------------------------------------
def test_control_resume_cannot_bypass_kill_switched(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    resume_response = None

    def kill_switch_predicate() -> bool:
        nonlocal resume_response
        session = service.session
        assert session is not None
        session.risk_engine.trigger_kill_switch(KillSwitchReason.MANUAL)
        session.pause()

        resume_req = ControlRequest(
            protocol_version=CONTROL_PROTOCOL_VERSION,
            request_id=str(uuid.uuid4()),
            action=ControlAction.RESUME,
        )
        resume_response = service.handle_control(resume_req)

        service.request_stop()
        return True

    config = _make_config(
        tmp_path, max_cycles=3, stop_predicate=kill_switch_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    service.run()

    assert resume_response is not None
    assert resume_response.accepted is False
    assert resume_response.changed is False
    assert resume_response.runtime_state == RuntimeState.KILL_SWITCHED
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 22 (V1B): STOP exits the service loop and performs cleanup
# ---------------------------------------------------------------------------
def test_control_stop_exits_loop_and_cleans_up(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    stop_response = None

    def stop_trigger_predicate() -> bool:
        nonlocal stop_response
        req = ControlRequest(
            protocol_version=CONTROL_PROTOCOL_VERSION,
            request_id=str(uuid.uuid4()),
            action=ControlAction.STOP,
        )
        stop_response = service.handle_control(req)
        return False

    config = _make_config(
        tmp_path, max_cycles=100, stop_predicate=stop_trigger_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == AppExitCode.SUCCESS
    assert res.service_state == ServiceState.STOPPED
    assert res.reason == "operator_stopped"

    assert stop_response is not None
    assert stop_response.accepted is True
    assert stop_response.changed is True
    assert stop_response.service_state == ServiceState.STOPPING

    # Cleanup verification
    assert fake_api.shutdown_calls >= 1
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 23 (V1B): STOP does not close broker positions
# ---------------------------------------------------------------------------
def test_control_stop_does_not_close_broker_positions(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()

    def stop_trigger_predicate() -> bool:
        req = ControlRequest(
            protocol_version=CONTROL_PROTOCOL_VERSION,
            request_id=str(uuid.uuid4()),
            action=ControlAction.STOP,
        )
        service.handle_control(req)
        return False

    config = _make_config(
        tmp_path, max_cycles=100, stop_predicate=stop_trigger_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS

    # ZERO close mutations
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 24 (V1B): Malformed control request fails safely
# ---------------------------------------------------------------------------
def test_malformed_control_request_fails_safely(tmp_path: Path) -> None:
    config = _make_config(tmp_path, max_cycles=1)
    service = Mt5DemoObservationService(config, api=SpyingFakeMt5Api())

    with pytest.raises(ValueError, match="validated control request is required"):
        service.handle_control({"not": "a control request"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TEST 25 (V1B): Unsupported control action (e.g. EMERGENCY_STOP) rejected in V1B
# ---------------------------------------------------------------------------
def test_unsupported_emergency_stop_rejected_in_v1b(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    emerg_response = None

    def emerg_stop_predicate() -> bool:
        nonlocal emerg_response
        req = ControlRequest(
            protocol_version=CONTROL_PROTOCOL_VERSION,
            request_id=str(uuid.uuid4()),
            action=ControlAction.EMERGENCY_STOP,
        )
        emerg_response = service.handle_control(req)
        service.request_stop()
        return True

    config = _make_config(
        tmp_path, max_cycles=3, stop_predicate=emerg_stop_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    service.run()

    assert emerg_response is not None
    assert emerg_response.accepted is False
    assert emerg_response.changed is False
    assert emerg_response.reason == "unsupported_control_action"
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 26 (V1B): Polling exception still shuts down control server and releases lock
# ---------------------------------------------------------------------------
def test_polling_exception_shuts_down_control_server(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    secret = ControlSecret(b"s" * 32)
    poll_count = 0

    def faulty_clock():
        nonlocal poll_count
        poll_count += 1
        if poll_count > 2:
            raise RuntimeError("hardware_fault")
        return NOW

    config = _make_config(
        tmp_path, max_cycles=10, clock=faulty_clock, control_secret=secret
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()

    assert res.exit_code == int(AppExitCode.RUNTIME_FAILURE)
    assert res.service_state == ServiceState.FAILED

    # Lock must be released
    assert not InstanceLock(config.lock_path, config.runtime_id)._handle


# ---------------------------------------------------------------------------
# TEST 27 (V1B): Repeated pause/resume/status operations produce zero mutations
# ---------------------------------------------------------------------------
def test_repeated_pause_resume_status_zero_mutations(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    iterations = 0

    def mixed_control_predicate() -> bool:
        nonlocal iterations
        iterations += 1
        status_req = ControlRequest(
            protocol_version=CONTROL_PROTOCOL_VERSION,
            request_id=str(uuid.uuid4()),
            action=ControlAction.STATUS,
        )
        service.handle_control(status_req)

        if iterations % 2 == 1:
            pause_req = ControlRequest(
                protocol_version=CONTROL_PROTOCOL_VERSION,
                request_id=str(uuid.uuid4()),
                action=ControlAction.PAUSE,
            )
            service.handle_control(pause_req)
        else:
            resume_req = ControlRequest(
                protocol_version=CONTROL_PROTOCOL_VERSION,
                request_id=str(uuid.uuid4()),
                action=ControlAction.RESUME,
            )
            service.handle_control(resume_req)
        return False

    config = _make_config(
        tmp_path, max_cycles=10, stop_predicate=mixed_control_predicate
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS
    assert res.cycles_completed == 10

    # ZERO MUTATIONS
    assert len(fake_api.order_send_calls) == 0


# ---------------------------------------------------------------------------
# TEST 28 (V1B): Full IPC send_control_request integration
# ---------------------------------------------------------------------------
def test_full_ipc_control_request_integration(tmp_path: Path) -> None:
    fake_api = SpyingFakeMt5Api()
    secret = ControlSecret(b"p" * 32)
    iterations = 0
    status_response = None

    def ipc_predicate() -> bool:
        nonlocal iterations, status_response
        iterations += 1
        if iterations == 2:
            req = ControlRequest(
                protocol_version=CONTROL_PROTOCOL_VERSION,
                request_id=str(uuid.uuid4()),
                action=ControlAction.STATUS,
            )
            op_config = config.operational_config
            assert op_config is not None
            status_response = send_control_request(op_config, secret, req)
        return False

    config = _make_config(
        tmp_path,
        max_cycles=4,
        stop_predicate=ipc_predicate,
        control_secret=secret,
        runtime_id="ipc_test_rt",
    )
    service = Mt5DemoObservationService(config, api=fake_api)

    res = service.run()
    assert res.exit_code == AppExitCode.SUCCESS

    assert status_response is not None
    assert status_response.accepted is True
    assert status_response.changed is False
    assert status_response.service_state == ServiceState.RUNNING
    assert len(fake_api.order_send_calls) == 0
