"""Offline tests for MT5 Demo operator execution runtime (Phase 1C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import fxlab.cli as cli_module
from fxlab.cli import app
from fxlab.execution.broker_capabilities import BrokerCapability
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import AuditEventType, EventLedger
from fxlab.execution.mt5_demo_broker import _MT5_DEMO_RESOLVER, Mt5DemoBroker
from fxlab.execution.mt5_demo_runtime import (
    MT5_DEMO_EXECUTION_CONFIRMATION,
    Mt5DemoExecutionRunner,
)
from fxlab.execution.order_manager import ExecutionIntent, ExecutionResultKind, OrderManager
from fxlab.execution.signal_engine import SignalEvent
from fxlab.risk.engine import RiskEngine, RiskLimits

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
CONFIRMATION = MT5_DEMO_EXECUTION_CONFIRMATION
runner = CliRunner()


class FakeMt5Api:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_REASON_CLIENT = 0
    DEAL_REASON_EXPERT = 3
    DEAL_REASON_SL = 4
    DEAL_REASON_TP = 5
    DEAL_REASON_SO = 6
    TRADE_RETCODE_REJECT = 10006
    TRADE_RETCODE_DONE = 10009

    def __init__(self) -> None:
        self.calls: list[object] = []
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
        self.custom_ticks: list[object] | None = None
        self.positions: list[object] = []
        self.orders: list[object] = []
        self.history_deals: list[object] = []
        self.history_orders: list[object] = []
        self.entry_result: object | BaseException = SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=7001,
            deal=8001,
            volume=0.01,
            price=1.10020,
        )
        self.close_result: object | BaseException = SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=7002,
            deal=8002,
            volume=0.01,
            price=1.10000,
        )
        self.order_send_count = 0

    def initialize(self) -> bool:
        self.calls.append("initialize")
        return True

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def terminal_info(self) -> object:
        self.calls.append("terminal_info")
        return self.terminal

    def account_info(self) -> object:
        self.calls.append("account_info")
        return self.account

    def symbol_info(self, symbol: str) -> object:
        self.calls.append(("symbol_info", symbol))
        return self.symbol

    def symbol_info_tick(self, symbol: str) -> object:
        self.calls.append(("symbol_info_tick", symbol))
        if self.custom_ticks is not None:
            if not self.custom_ticks:
                return None
            return self.custom_ticks.pop(0)
        if self.auto_advance_tick:
            msc = self.base_tick_msc + self.tick_counter * self.tick_step_msc
            self.tick_counter += 1
            return SimpleNamespace(time_msc=msc, bid=1.10000, ask=1.10020)
        return SimpleNamespace(time_msc=self.base_tick_msc, bid=1.10000, ask=1.10020)

    def positions_get(self, **query: object) -> tuple[object, ...] | None:
        self.calls.append(("positions_get", tuple(sorted(query.items()))))
        if "ticket" in query:
            return tuple(item for item in self.positions if item.ticket == query["ticket"])
        return tuple(item for item in self.positions if item.symbol == query.get("symbol"))

    def orders_get(self, **query: object) -> tuple[object, ...] | None:
        self.calls.append(("orders_get", tuple(sorted(query.items()))))
        return tuple(item for item in self.orders if item.symbol == query.get("symbol"))

    def history_deals_get(self, **query: object) -> tuple[object, ...] | None:
        self.calls.append(("history_deals_get", tuple(sorted(query.items()))))
        if "position" in query:
            return tuple(
                d
                for d in self.history_deals
                if getattr(d, "position_id", None) == query["position"]
            )
        if "ticket" in query:
            return tuple(
                d for d in self.history_deals if getattr(d, "ticket", None) == query["ticket"]
            )
        return tuple(self.history_deals)

    def history_orders_get(self, **query: object) -> tuple[object, ...] | None:
        self.calls.append(("history_orders_get", tuple(sorted(query.items()))))
        if "position" in query:
            return tuple(
                o
                for o in self.history_orders
                if getattr(o, "position_id", None) == query["position"]
            )
        if "ticket" in query:
            return tuple(
                o for o in self.history_orders if getattr(o, "ticket", None) == query["ticket"]
            )
        return tuple(self.history_orders)

    def order_send(self, request: dict[str, object]) -> object:
        self.calls.append(("order_send", dict(request)))
        self.order_send_count += 1
        result = self.entry_result if self.order_send_count == 1 else self.close_result
        if isinstance(result, BaseException):
            raise result
        if getattr(result, "retcode", None) != self.TRADE_RETCODE_DONE:
            return result
        if self.order_send_count == 1:
            self.positions = [
                item for item in self.positions if getattr(item, "ticket", None) != 9001
            ] + [
                SimpleNamespace(
                    ticket=9001,
                    identifier=result.order,
                    symbol="EURUSD",
                    type=(
                        self.POSITION_TYPE_BUY
                        if request["type"] == self.ORDER_TYPE_BUY
                        else self.POSITION_TYPE_SELL
                    ),
                    magic=request["magic"],
                    comment=request["comment"],
                    volume=request["volume"],
                    price_open=request["price"],
                    sl=request["sl"],
                    tp=request.get("tp", 0.0),
                    profit=0.0,
                    time=int(NOW.timestamp()),
                )
            ]
        else:
            ticket = request.get("position")
            self.positions = [
                item for item in self.positions if getattr(item, "ticket", None) != ticket
            ]
        return result


def _ledger(tmp_path) -> tuple[EventLedger, SQLiteEventStore]:
    store = SQLiteEventStore(tmp_path / "mt5-demo-runtime.sqlite", "mt5-runtime-test")
    return EventLedger(store.session_id, time_provider=lambda: NOW, durable_store=store), store


def _runner(api: FakeMt5Api | None = None, **kwargs) -> tuple[Mt5DemoExecutionRunner, FakeMt5Api]:
    selected_api = api or FakeMt5Api()
    mono_time = [0.0]

    def default_monotonic():
        mono_time[0] += 0.001
        return mono_time[0]

    broker = Mt5DemoBroker(
        api=selected_api,
        clock=kwargs.get("clock", lambda: NOW),
        monotonic=kwargs.get("monotonic", default_monotonic),
        sleeper=kwargs.get("sleeper", lambda _: None),
    )
    exec_runner = Mt5DemoExecutionRunner(
        broker=broker,
        clock=kwargs.get("clock", lambda: NOW),
    )
    return exec_runner, selected_api


def test_mt5_demo_runtime_execution_runner_end_to_end(tmp_path) -> None:
    exec_runner, api = _runner()
    ledger, store = _ledger(tmp_path)

    try:
        result = exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            side="buy",
            max_loss_usd=1.0,
        )
    finally:
        events = ledger.events()
        store.close()

    assert result.status == "successful_round_trip"
    assert result.symbol == "EURUSD"
    assert result.side == "buy"
    assert result.volume == 0.01
    assert result.entry_order_id == "7001"
    assert result.entry_deal_id == "8001"
    assert result.position_id == "9001"
    assert result.close_order_id == "7002"
    assert result.close_deal_id == "8002"
    assert api.order_send_count == 2
    assert api.calls[-1] == "shutdown"

    event_types = {e.event_type for e in events}
    assert AuditEventType.BROKER_CAPABILITIES_BOUND in event_types
    assert AuditEventType.RISK_APPROVED in event_types
    assert AuditEventType.ORDER_SUBMITTED in event_types
    assert AuditEventType.POSITION_OPENED in event_types
    assert AuditEventType.POSITION_CLOSED in event_types

    opened_events = [e for e in events if e.event_type == AuditEventType.POSITION_OPENED]
    assert len(opened_events) == 1
    assert opened_events[0].correlation.broker_order_id == "7001"
    assert opened_events[0].correlation.position_id == "9001"
    assert opened_events[0].payload["entry_order_id"] == "7001"
    assert opened_events[0].payload["entry_deal_id"] == "8001"
    assert opened_events[0].payload["position_id"] == "9001"
    assert opened_events[0].payload["symbol"] == "EURUSD"
    assert opened_events[0].payload["side"] == "buy"
    assert opened_events[0].payload["volume"] == 0.01

    close_events = [e for e in events if e.event_type == AuditEventType.POSITION_CLOSED]
    assert len(close_events) == 1
    assert close_events[0].correlation.close_order_id == "7002"
    assert close_events[0].payload["close_order_id"] == "7002"
    assert close_events[0].payload["close_deal_id"] == "8002"


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_mt5_demo_runtime_supports_both_buy_and_sell(tmp_path, side: str) -> None:
    exec_runner, api = _runner()
    ledger, store = _ledger(tmp_path)

    try:
        result = exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            side=side,
            max_loss_usd=1.0,
        )
    finally:
        store.close()

    assert result.status == "successful_round_trip"
    assert result.side == side
    assert result.close_order_id == "7002"
    assert result.close_deal_id == "8002"
    assert api.order_send_count == 2


def test_mt5_demo_runtime_rejects_dirty_symbol_state_before_entry(tmp_path) -> None:
    # Existing EURUSD position rejected without any order_send
    api1 = FakeMt5Api()
    api1.positions = [
        SimpleNamespace(
            ticket=5555,
            identifier=5555,
            symbol="EURUSD",
            type=api1.POSITION_TYPE_BUY,
            magic=999999,
            comment="manual-trade",
            volume=0.01,
            price_open=1.10000,
            sl=1.09500,
            tp=0.0,
            profit=0.0,
            time=int(NOW.timestamp()),
        )
    ]
    exec_runner1, _ = _runner(api1)
    ledger1, store1 = _ledger(tmp_path / "dirty_pos")
    try:
        with pytest.raises(RuntimeError, match="mt5_state_not_clean"):
            exec_runner1.run(
                confirmation=CONFIRMATION,
                ledger=ledger1,
                side="buy",
                max_loss_usd=1.0,
            )
    finally:
        store1.close()
    assert api1.order_send_count == 0

    # Existing EURUSD pending order rejected without any order_send
    api2 = FakeMt5Api()
    api2.orders = [SimpleNamespace(ticket=6666, symbol="EURUSD")]
    exec_runner2, _ = _runner(api2)
    ledger2, store2 = _ledger(tmp_path / "dirty_order")
    try:
        with pytest.raises(RuntimeError, match="mt5_state_not_clean"):
            exec_runner2.run(
                confirmation=CONFIRMATION,
                ledger=ledger2,
                side="buy",
                max_loss_usd=1.0,
            )
    finally:
        store2.close()
    assert api2.order_send_count == 0


def test_mt5_demo_runtime_rejects_wrong_confirmation_before_initialization(tmp_path) -> None:
    exec_runner, api = _runner()
    ledger, store = _ledger(tmp_path)

    try:
        with pytest.raises(ValueError, match="mt5_demo_execution_confirmation_required"):
            exec_runner.run(
                confirmation="wrong_confirmation",
                ledger=ledger,
                max_loss_usd=1.0,
            )
    finally:
        store.close()

    assert api.calls == []
    assert api.order_send_count == 0


def test_mt5_demo_runtime_requires_durable_store(tmp_path) -> None:
    exec_runner, api = _runner()
    ledger = EventLedger("test-session", time_provider=lambda: NOW, durable_store=None)

    with pytest.raises(ValueError, match="mt5_durable_audit_required"):
        exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            max_loss_usd=1.0,
        )
    assert api.calls == []


def test_cli_mt5_demo_execute_command(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str, EventLedger, float]] = []

    class FakeExecutionRunner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(
            self,
            *,
            confirmation: str,
            ledger: EventLedger,
            side: str = "buy",
            max_loss_usd: float,
        ) -> object:
            calls.append((confirmation, side, ledger, max_loss_usd))
            return SimpleNamespace(
                status="successful_round_trip",
                account="****5678",
                symbol="EURUSD",
                side=side,
                volume=0.01,
                entry_order_id="7001",
                entry_deal_id="8001",
                position_id="9001",
                close_order_id="7002",
                close_deal_id="8002",
            )

    monkeypatch.setattr(cli_module, "Mt5DemoExecutionRunner", FakeExecutionRunner, raising=False)
    result = runner.invoke(
        app,
        [
            "mt5",
            "demo-execute",
            "--confirm",
            CONFIRMATION,
            "--audit-db",
            str(tmp_path / "audit.sqlite"),
            "--max-loss-usd",
            "1.50",
            "--side",
            "buy",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == CONFIRMATION
    assert calls[0][1] == "buy"
    assert calls[0][2].durable_store is not None
    assert calls[0][3] == 1.50


def test_cli_mt5_demo_execute_requires_explicit_max_loss_usd(tmp_path, monkeypatch) -> None:
    calls: list[object] = []

    class FakeExecutionRunner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, *args, **kwargs) -> object:
            calls.append((args, kwargs))
            return SimpleNamespace(status="successful_round_trip")

    monkeypatch.setattr(cli_module, "Mt5DemoExecutionRunner", FakeExecutionRunner, raising=False)
    # Omitting --max-loss-usd must fail before any execution/mutation
    result = runner.invoke(
        app,
        [
            "mt5",
            "demo-execute",
            "--confirm",
            CONFIRMATION,
            "--audit-db",
            str(tmp_path / "audit.sqlite"),
            "--side",
            "buy",
        ],
    )

    assert result.exit_code == 2
    assert len(calls) == 0


def test_cli_mt5_demo_execute_rejects_non_positive_max_loss(tmp_path, monkeypatch) -> None:
    calls: list[object] = []

    class FakeExecutionRunner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, *args, **kwargs) -> object:
            calls.append((args, kwargs))
            return SimpleNamespace(status="successful_round_trip")

    monkeypatch.setattr(cli_module, "Mt5DemoExecutionRunner", FakeExecutionRunner, raising=False)
    for invalid_val in ["0", "-1.0", "-5"]:
        result = runner.invoke(
            app,
            [
                "mt5",
                "demo-execute",
                "--confirm",
                CONFIRMATION,
                "--audit-db",
                str(tmp_path / "audit.sqlite"),
                "--max-loss-usd",
                invalid_val,
                "--side",
                "buy",
            ],
        )
        assert result.exit_code == 2
    assert len(calls) == 0


def test_mt5_demo_runtime_rejects_unsupported_account_currency(tmp_path) -> None:
    api = FakeMt5Api()
    api.account.currency = "EUR"  # Unsupported account currency (only USD supported)
    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    try:
        with pytest.raises(RuntimeError, match="mt5_account_incompatible"):
            exec_runner.run(
                confirmation=CONFIRMATION,
                ledger=ledger,
                side="buy",
                max_loss_usd=1.0,
            )
    finally:
        store.close()
    assert api.order_send_count == 0


def test_mt5_demo_runtime_rejects_invalid_max_loss_values(tmp_path) -> None:
    exec_runner, api = _runner()
    ledger, store = _ledger(tmp_path)

    try:
        for invalid_risk in [0, -1.0, float("nan"), float("inf"), -float("inf")]:
            with pytest.raises(ValueError, match="invalid_max_loss_usd"):
                exec_runner.run(
                    confirmation=CONFIRMATION,
                    ledger=ledger,
                    side="buy",
                    max_loss_usd=invalid_risk,
                )
    finally:
        store.close()
    assert api.order_send_count == 0


def test_mt5_demo_runtime_supports_reported_broker_minimum_volume(tmp_path) -> None:
    api = FakeMt5Api()
    api.symbol.volume_min = 0.1
    api.symbol.volume_step = 0.1
    api.symbol.volume_max = 100.0
    api.entry_result = SimpleNamespace(
        retcode=api.TRADE_RETCODE_DONE,
        order=7001,
        deal=8001,
        volume=0.1,
        price=1.10020,
    )
    api.close_result = SimpleNamespace(
        retcode=api.TRADE_RETCODE_DONE,
        order=7002,
        deal=8002,
        volume=0.1,
        price=1.10000,
    )

    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    try:
        result = exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            side="buy",
            max_loss_usd=10.0,
        )
    finally:
        store.close()

    assert result.status == "successful_round_trip"
    assert result.volume == 0.1
    assert api.order_send_count == 2


def test_mt5_demo_runtime_authoritative_correlation_rejects_ambiguous_broker_state(
    tmp_path,
) -> None:
    api = FakeMt5Api()
    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    original_order_send = api.order_send

    def send_without_position(req: dict[str, object]) -> object:
        res = original_order_send(req)
        api.positions = []
        return res

    api.order_send = send_without_position

    try:
        with pytest.raises(
            RuntimeError, match="mt5_execution_failed:broker_submission_exception"
        ):
            exec_runner.run(
                confirmation=CONFIRMATION,
                ledger=ledger,
                side="buy",
                max_loss_usd=1.0,
            )
    finally:
        store.close()


def test_mt5_demo_runtime_leaves_unrelated_positions_untouched(tmp_path) -> None:
    api = FakeMt5Api()
    manual_pos = SimpleNamespace(
        ticket=9999,
        identifier=5555,
        symbol="GBPUSD",
        type=api.POSITION_TYPE_BUY,
        magic=999999,
        comment="manual-trade",
        volume=0.05,
        price_open=1.09000,
        sl=1.08500,
        tp=0.0,
        profit=50.0,
        time=int(NOW.timestamp()),
    )
    api.positions.append(manual_pos)

    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    try:
        result = exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            side="buy",
            max_loss_usd=1.0,
        )
    finally:
        store.close()

    assert result.status == "successful_round_trip"
    assert result.position_id == "9001"
    remaining_tickets = [getattr(p, "ticket", None) for p in api.positions]
    assert 9999 in remaining_tickets
    assert 9001 not in remaining_tickets


def test_mt5_demo_runtime_fails_closed_if_position_remains_after_close(tmp_path) -> None:
    api = FakeMt5Api()
    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    def send_without_clearing(req: dict[str, object]) -> object:
        api.order_send_count += 1
        if api.order_send_count == 1:
            api.positions = [
                SimpleNamespace(
                    ticket=9001,
                    identifier=7001,
                    symbol="EURUSD",
                    type=api.POSITION_TYPE_BUY,
                    magic=0x46584C42,
                    comment=req["comment"],
                    volume=req["volume"],
                    price_open=req["price"],
                    sl=req["sl"],
                    tp=req.get("tp", 0.0),
                    profit=0.0,
                    time=int(NOW.timestamp()),
                )
            ]
            return api.entry_result
        # On close, do not remove position from api.positions
        return api.close_result

    api.order_send = send_without_clearing

    try:
        with pytest.raises(RuntimeError, match="mt5_close_position_remaining"):
            exec_runner.run(
                confirmation=CONFIRMATION,
                ledger=ledger,
                side="buy",
                max_loss_usd=1.0,
            )
    finally:
        store.close()


@pytest.mark.parametrize("mode", [1, 2, 999, None])
def test_mt5_demo_runtime_rejects_non_demo_account(tmp_path, mode: object) -> None:
    api = FakeMt5Api()
    api.account.trade_mode = mode
    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    try:
        with pytest.raises(RuntimeError, match="mt5_demo_account_required"):
            exec_runner.run(
                confirmation=CONFIRMATION,
                ledger=ledger,
                side="buy",
                max_loss_usd=1.0,
            )
    finally:
        store.close()

    assert api.order_send_count == 0


def test_mt5_demo_runtime_advancing_clock_round_trip(tmp_path) -> None:
    """Test runtime with real wall-clock-like advancing time per step."""
    base_dt = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    tick_count = [0]

    def advancing_clock() -> datetime:
        tick_count[0] += 1
        return base_dt + timedelta(milliseconds=tick_count[0] * 50)

    exec_runner, api = _runner(clock=advancing_clock)
    ledger, store = _ledger(tmp_path)

    try:
        result = exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            side="buy",
            max_loss_usd=1.0,
        )
    finally:
        store.close()

    assert result.status == "successful_round_trip"
    assert result.close_order_id == "7002"
    assert result.close_deal_id == "8002"
    assert api.order_send_count == 2


def test_mt5_demo_runtime_reproduce_future_quote_failure_with_earlier_timestamp() -> None:
    """Explicitly reproduces why earlier submit timestamp caused future_quote."""
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api, clock=lambda: NOW)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    tick = broker.get_latest_tick("EURUSD")
    assert tick is not None

    risk_engine = RiskEngine(
        RiskLimits(
            starting_equity=10000.0,
            max_open_positions=1,
            max_exposure_per_symbol_lots=0.01,
        ),
        pip_size_resolver=_MT5_DEMO_RESOLVER,
        lot_step=0.01,
    )
    order_manager = OrderManager(
        broker=broker,
        risk_engine=risk_engine,
        required_capabilities=frozenset(
            {
                BrokerCapability.MARKET_ORDERS,
                BrokerCapability.NATIVE_SL_TP,
            }
        ),
    )

    signal = SignalEvent(
        setup_name="test_operator",
        symbol="EURUSD",
        timeframe="M1",
        side=1,
        signal_time=tick.timestamp,
        signal_bar_index=0,
    )
    intent = ExecutionIntent(signal=signal, sl_price=1.09500)

    # When current_time precedes tick.timestamp, submission fails with future_quote
    earlier_current_time = tick.timestamp - timedelta(milliseconds=50)
    failed_result = order_manager.submit(intent, current_time=earlier_current_time)
    assert failed_result.kind is ExecutionResultKind.EXECUTION_REJECTED
    assert failed_result.reason == "future_quote"

    # With coherent point-in-time sequencing (current_time >= tick.timestamp), it succeeds
    valid_current_time = tick.timestamp + timedelta(milliseconds=10)
    success_result = order_manager.submit(intent, current_time=valid_current_time)
    assert success_result.kind is ExecutionResultKind.SUBMITTED


def test_mt5_demo_runtime_stale_quote_fails_closed() -> None:
    """Verify that a tick older than the signal is rejected as stale_quote."""
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api, clock=lambda: NOW)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    tick = broker.get_latest_tick("EURUSD")
    assert tick is not None

    risk_engine = RiskEngine(
        RiskLimits(
            starting_equity=10000.0,
            max_open_positions=1,
            max_exposure_per_symbol_lots=0.01,
        ),
        pip_size_resolver=_MT5_DEMO_RESOLVER,
        lot_step=0.01,
    )
    order_manager = OrderManager(
        broker=broker,
        risk_engine=risk_engine,
        required_capabilities=frozenset(
            {
                BrokerCapability.MARKET_ORDERS,
                BrokerCapability.NATIVE_SL_TP,
            }
        ),
    )

    # Signal is newer than the tick
    newer_signal_time = tick.timestamp + timedelta(seconds=1)
    signal = SignalEvent(
        setup_name="test_operator",
        symbol="EURUSD",
        timeframe="M1",
        side=1,
        signal_time=newer_signal_time,
        signal_bar_index=0,
    )
    intent = ExecutionIntent(signal=signal, sl_price=1.09500)

    result = order_manager.submit(intent, current_time=newer_signal_time)
    assert result.kind is ExecutionResultKind.EXECUTION_REJECTED
    assert result.reason == "stale_quote"


def test_mt5_demo_runtime_entry_filled_then_native_protective_exit(tmp_path) -> None:
    api = FakeMt5Api()

    # Override order_send to simulate instant SL fill and absence from active positions
    def instant_sl_order_send(request: dict[str, object]) -> object:
        api.calls.append(("order_send", dict(request)))
        api.order_send_count += 1
        order_id = 7001
        deal_id = 8001
        pos_id = 9001
        api.positions = []  # Absent from active positions
        api.history_orders = [
            SimpleNamespace(
                ticket=order_id,
                position_id=pos_id,
                symbol="EURUSD",
                magic=request["magic"],
                comment=request["comment"],
                volume_initial=request["volume"],
            )
        ]
        api.history_deals = [
            SimpleNamespace(
                ticket=deal_id,
                order=order_id,
                position_id=pos_id,
                entry=api.DEAL_ENTRY_IN,
                symbol="EURUSD",
                magic=request["magic"],
                volume=request["volume"],
                type=api.POSITION_TYPE_BUY,
            ),
            SimpleNamespace(
                ticket=8005,
                order=7005,
                position_id=pos_id,
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=request["magic"],
                volume=request["volume"],
                reason=api.DEAL_REASON_SL,
            ),
        ]
        return SimpleNamespace(
            retcode=api.TRADE_RETCODE_DONE,
            order=order_id,
            deal=deal_id,
            volume=request["volume"],
            price=request["price"],
        )

    api.order_send = instant_sl_order_send  # type: ignore[assignment]
    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    try:
        result = exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            side="buy",
            max_loss_usd=1.0,
        )
    finally:
        events = ledger.events()
        store.close()

    assert result.status == "ENTRY_FILLED_THEN_NATIVE_PROTECTIVE_EXIT"
    assert result.symbol == "EURUSD"
    assert result.side == "buy"
    assert result.volume == 0.01
    assert result.entry_order_id == "7001"
    assert result.entry_deal_id == "8001"
    assert result.position_id == "9001"
    assert result.close_order_id == "7005"
    assert result.close_deal_id == "8005"
    # ZERO additional close order_send calls (only the initial entry order_send)
    assert api.order_send_count == 1

    event_types = {e.event_type for e in events}
    assert AuditEventType.BROKER_CAPABILITIES_BOUND in event_types
    assert AuditEventType.RISK_APPROVED in event_types
    assert AuditEventType.ORDER_SUBMITTED in event_types
    assert AuditEventType.POSITION_OPENED in event_types
    assert AuditEventType.POSITION_CLOSED in event_types

    opened_events = [e for e in events if e.event_type == AuditEventType.POSITION_OPENED]
    assert len(opened_events) == 1
    assert opened_events[0].correlation.broker_order_id == "7001"
    assert opened_events[0].correlation.position_id == "9001"
    assert opened_events[0].payload["entry_order_id"] == "7001"
    assert opened_events[0].payload["entry_deal_id"] == "8001"
    assert opened_events[0].payload["position_id"] == "9001"
    assert opened_events[0].payload["symbol"] == "EURUSD"
    assert opened_events[0].payload["side"] == "buy"
    assert opened_events[0].payload["volume"] == 0.01

    close_events = [e for e in events if e.event_type == AuditEventType.POSITION_CLOSED]
    assert len(close_events) == 1
    assert close_events[0].correlation.close_order_id == "7005"
    assert close_events[0].payload["close_order_id"] == "7005"
    assert close_events[0].payload["close_deal_id"] == "8005"
    assert close_events[0].payload["exit_reason"] == "SL"
    assert close_events[0].payload["reconciliation"] == "ENTRY_FILLED_THEN_NATIVE_PROTECTIVE_EXIT"


def test_mt5_demo_runtime_normal_round_trip_durable_ledger_complete_identity(
    tmp_path,
) -> None:
    api = FakeMt5Api()
    api.entry_result = SimpleNamespace(
        retcode=api.TRADE_RETCODE_DONE,
        order=7001,
        deal=8001,
        volume=0.01,
        price=1.10000,
    )
    api.close_result = SimpleNamespace(
        retcode=api.TRADE_RETCODE_DONE,
        order=7002,
        deal=8002,
        volume=0.01,
        price=1.10020,
    )
    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    try:
        result = exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            side="sell",
            max_loss_usd=2.0,
        )
    finally:
        events = ledger.events()
        store.close()

    assert result.status == "successful_round_trip"

    # Verify complete identity fields on execution result
    assert result.entry_order_id == "7001"
    assert result.entry_deal_id == "8001"
    assert result.position_id == "9001"
    assert result.close_order_id == "7002"
    assert result.close_deal_id == "8002"
    assert result.symbol == "EURUSD"
    assert result.side == "sell"
    assert result.volume == 0.01

    # Verify exact deterministic sequence of audit events
    expected_sequence = [
        AuditEventType.ACCOUNT_OBSERVED,
        AuditEventType.OPERATOR_CONTROL_ACTION,
        AuditEventType.BROKER_CAPABILITIES_BOUND,
        AuditEventType.RISK_APPROVED,
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.POSITION_OPENED,
        AuditEventType.POSITION_CLOSED,
    ]
    actual_sequence = [e.event_type for e in events]
    assert actual_sequence == expected_sequence
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))

    # Verify POSITION_OPENED audit event contains full entry identity
    pos_opened = next(e for e in events if e.event_type == AuditEventType.POSITION_OPENED)
    assert pos_opened.correlation.broker_order_id == "7001"
    assert pos_opened.correlation.position_id == "9001"
    assert pos_opened.correlation.client_order_id is not None
    assert pos_opened.correlation.client_order_id.startswith("operator_demo_execution")
    assert pos_opened.payload["entry_order_id"] == "7001"
    assert pos_opened.payload["entry_deal_id"] == "8001"
    assert pos_opened.payload["position_id"] == "9001"
    assert pos_opened.payload["symbol"] == "EURUSD"
    assert pos_opened.payload["side"] == "sell"
    assert pos_opened.payload["volume"] == 0.01

    # Verify POSITION_CLOSED audit event contains full close identity
    pos_closed = next(e for e in events if e.event_type == AuditEventType.POSITION_CLOSED)
    assert pos_closed.correlation.broker_order_id == "7001"
    assert pos_closed.correlation.position_id == "9001"
    assert pos_closed.correlation.close_order_id == "7002"
    assert pos_closed.payload["position_id"] == "9001"
    assert pos_closed.payload["close_order_id"] == "7002"
    assert pos_closed.payload["close_deal_id"] == "8002"


def test_mt5_demo_runtime_protective_exit_durable_ledger_complete_identity(
    tmp_path,
) -> None:
    api = FakeMt5Api()

    def instant_tp_order_send(request: dict[str, object]) -> object:
        order_id = 7001
        deal_id = 8001
        pos_id = 9001
        api.positions = []
        api.history_orders = [
            SimpleNamespace(
                ticket=order_id,
                position_id=pos_id,
                symbol="EURUSD",
                magic=request["magic"],
                comment=request["comment"],
                volume_initial=request["volume"],
            )
        ]
        api.history_deals = [
            SimpleNamespace(
                ticket=deal_id,
                order=order_id,
                position_id=pos_id,
                entry=api.DEAL_ENTRY_IN,
                symbol="EURUSD",
                magic=request["magic"],
                volume=request["volume"],
                type=api.POSITION_TYPE_BUY,
            ),
            SimpleNamespace(
                ticket=8009,
                order=7009,
                position_id=pos_id,
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=request["magic"],
                volume=request["volume"],
                reason=api.DEAL_REASON_TP,
            ),
        ]
        return SimpleNamespace(
            retcode=api.TRADE_RETCODE_DONE,
            order=order_id,
            deal=deal_id,
            volume=request["volume"],
            price=request["price"],
        )

    api.order_send = instant_tp_order_send  # type: ignore[assignment]
    exec_runner, _ = _runner(api)
    ledger, store = _ledger(tmp_path)

    try:
        result = exec_runner.run(
            confirmation=CONFIRMATION,
            ledger=ledger,
            side="buy",
            max_loss_usd=1.0,
        )
    finally:
        events = ledger.events()
        store.close()

    assert result.status == "ENTRY_FILLED_THEN_NATIVE_PROTECTIVE_EXIT"
    assert result.entry_order_id == "7001"
    assert result.entry_deal_id == "8001"
    assert result.position_id == "9001"
    assert result.close_order_id == "7009"
    assert result.close_deal_id == "8009"
    assert result.symbol == "EURUSD"
    assert result.side == "buy"
    assert result.volume == 0.01

    # Verify exact deterministic sequence of audit events
    expected_sequence = [
        AuditEventType.ACCOUNT_OBSERVED,
        AuditEventType.OPERATOR_CONTROL_ACTION,
        AuditEventType.BROKER_CAPABILITIES_BOUND,
        AuditEventType.RISK_APPROVED,
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.POSITION_OPENED,
        AuditEventType.POSITION_CLOSED,
    ]
    actual_sequence = [e.event_type for e in events]
    assert actual_sequence == expected_sequence
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))

    # Verify POSITION_OPENED audit event contains full entry identity
    pos_opened = next(e for e in events if e.event_type == AuditEventType.POSITION_OPENED)
    assert pos_opened.correlation.broker_order_id == "7001"
    assert pos_opened.correlation.position_id == "9001"
    assert pos_opened.payload["entry_order_id"] == "7001"
    assert pos_opened.payload["entry_deal_id"] == "8001"
    assert pos_opened.payload["position_id"] == "9001"

    # Verify POSITION_CLOSED audit event contains full protective exit identity
    pos_closed = next(e for e in events if e.event_type == AuditEventType.POSITION_CLOSED)
    assert pos_closed.correlation.broker_order_id == "7001"
    assert pos_closed.correlation.position_id == "9001"
    assert pos_closed.correlation.close_order_id == "7009"
    assert pos_closed.payload["position_id"] == "9001"
    assert pos_closed.payload["close_order_id"] == "7009"
    assert pos_closed.payload["close_deal_id"] == "8009"
    assert pos_closed.payload["exit_reason"] == "TP"
    assert pos_closed.payload["reconciliation"] == "ENTRY_FILLED_THEN_NATIVE_PROTECTIVE_EXIT"
