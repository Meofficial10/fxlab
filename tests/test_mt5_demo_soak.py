from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
)
from fxlab.execution.mt5_demo_broker import Mt5DemoBroker
from fxlab.execution.mt5_demo_session import (
    Mt5DemoSession,
    Mt5SessionCycleKind,
    _Mt5DemoExecutionPermit,
    _Mt5DemoSoakExecutionPermit,
    _validate_strategy_gate,
)
from fxlab.execution.mt5_demo_soak import (
    MAX_DURATION_SECONDS_UPPER_BOUND,
    MAX_ENTRIES_UPPER_BOUND,
    MT5_DEMO_SOAK_CONFIRMATION,
    Mt5DemoSoakConfig,
    Mt5DemoSoakRunner,
    SyntheticSoakSignalGenerator,
    _issue_soak_execution_permit,
)
from fxlab.execution.signal_engine import SignalEvent
from fxlab.risk.engine import KillSwitchReason, RiskLimits

NOW = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)


class SoakFakeMt5Api:
    """Mock MT5 API for offline soak testing."""

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_FILLING_IOC = 2
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
        self.calls: list[object] = []
        self.clock = clock or (lambda: NOW)
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
        self.ticket_seq = 9000
        self.realized_pnl = 0.0

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
        current_dt = self.clock()
        base_msc = int(current_dt.timestamp() * 1000)
        msc = base_msc + self.tick_counter * self.tick_step_msc
        if self.auto_advance_tick:
            self.tick_counter += 1
        return SimpleNamespace(time_msc=msc, bid=1.10000, ask=1.10020)

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
        return tuple(self.history_deals)

    def history_orders_get(self, **query: object) -> tuple[object, ...] | None:
        self.calls.append(("history_orders_get", tuple(sorted(query.items()))))
        return tuple(self.history_orders)

    def order_send(self, request: dict[str, object]) -> object:
        self.calls.append(("order_send", dict(request)))
        self.order_send_count += 1
        is_close = "position" in request
        result = self.close_result if is_close else self.entry_result
        if isinstance(result, BaseException):
            raise result
        if getattr(result, "retcode", None) != self.TRADE_RETCODE_DONE:
            return result

        if not is_close:
            self.ticket_seq += 1
            pos_ticket = self.ticket_seq
            self.positions = [
                item for item in self.positions if getattr(item, "ticket", None) != pos_ticket
            ] + [
                SimpleNamespace(
                    ticket=pos_ticket,
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
            self.history_deals.append(
                SimpleNamespace(
                    ticket=getattr(result, "deal", 8002),
                    order=getattr(result, "order", 7002),
                    position_id=ticket,
                    entry=self.DEAL_ENTRY_OUT,
                    symbol="EURUSD",
                    magic=1180191810,
                    volume=request["volume"],
                    profit=self.realized_pnl,
                    reason=self.DEAL_REASON_CLIENT,
                )
            )
        return result


def _soak_fixture(tmp_path, api: SoakFakeMt5Api | None = None, **kwargs):
    time_ref = kwargs.get("time_ref")
    clock_fn = (lambda: time_ref[0]) if time_ref is not None else kwargs.get("clock", lambda: NOW)
    selected_api = api or SoakFakeMt5Api(clock=clock_fn)
    store = SQLiteEventStore(tmp_path / "mt5-demo-soak.sqlite", "mt5-soak-test")
    ledger = EventLedger(store.session_id, time_provider=clock_fn, durable_store=store)

    mono_time = [0.0]

    def default_monotonic():
        mono_time[0] += 0.001
        if time_ref is not None:
            return (time_ref[0] - NOW).total_seconds() * 10.0 + mono_time[0]
        return mono_time[0]

    broker = Mt5DemoBroker(
        api=selected_api,
        clock=clock_fn,
        monotonic=kwargs.get("monotonic", default_monotonic),
        sleeper=kwargs.get("sleeper", lambda _: None),
        max_quote_age=timedelta(seconds=kwargs.get("max_quote_age_seconds", 5.0)),
    )
    return broker, ledger, selected_api, store, clock_fn


def _create_session(
    broker: Mt5DemoBroker | None = None,
    event_ledger: EventLedger | None = None,
    ledger: EventLedger | None = None,
    **kwargs,
) -> Mt5DemoSession:
    actual_broker = broker if broker is not None else kwargs.get("broker")
    actual_ledger = (
        event_ledger
        if event_ledger is not None
        else (ledger if ledger is not None else kwargs.get("event_ledger", kwargs.get("ledger")))
    )
    if "execution_permit" in kwargs:
        permit = kwargs["execution_permit"]
    else:
        dummy_config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=1,
            max_duration_seconds=10.0,
            drain_timeout_seconds=10.0,
            max_quote_age_seconds=5.0,
        )
        permit = _issue_soak_execution_permit(dummy_config)

    return Mt5DemoSession(
        broker=actual_broker,
        event_ledger=actual_ledger,
        max_loss_usd=kwargs.get("max_loss_usd", 1.0),
        max_quote_age=kwargs.get("max_quote_age", timedelta(seconds=5.0)),
        execution_permit=permit,
        risk_limits=kwargs.get("risk_limits"),
        clock=kwargs.get("clock"),
    )


# Test 1: Deterministic alternating BUY then SELL stimulus
def test_deterministic_alternating_buy_then_sell() -> None:
    generator = SyntheticSoakSignalGenerator("synthetic_soak_test")
    s1 = generator.next_signal(NOW)
    assert s1.side == 1  # BUY
    assert s1.signal_bar_index == 0
    assert s1.setup_name == "synthetic_soak_test"

    s2 = generator.next_signal(NOW + timedelta(seconds=1))
    assert s2.side == -1  # SELL
    assert s2.signal_bar_index == 1

    s3 = generator.next_signal(NOW + timedelta(seconds=2))
    assert s3.side == 1  # BUY
    assert s3.signal_bar_index == 2


# Test 2: max_entries stops loop exactly
def test_max_entries_stops_loop_exactly(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        def custom_sleeper(dt: float) -> None:
            if api.positions:
                pos = api.positions.pop(0)
                api.history_deals.append(
                    SimpleNamespace(
                        ticket=8099 + len(api.history_deals),
                        order=7099 + len(api.history_deals),
                        position_id=pos.ticket,
                        entry=api.DEAL_ENTRY_OUT,
                        symbol="EURUSD",
                        magic=1180191810,
                        volume=0.01,
                        profit=0.05,
                        reason=api.DEAL_REASON_SL,
                    )
                )
            sim_time[0] += timedelta(seconds=dt)

        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=2,
            max_duration_seconds=3600.0,
            drain_timeout_seconds=300.0,
            max_quote_age_seconds=5.0,
            poll_interval_seconds=0.1,
            cooldown_seconds=1.0,
            clock=clock_fn,
            sleeper=custom_sleeper,
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        assert res.status == "completed"
        assert res.stop_reason == "max_entries_reached"
        assert res.entries_completed == 2
        assert res.active_position_id is None
    finally:
        store.close()


# Test 3: max_duration stops loop while flat
def test_max_duration_stops_loop(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        def custom_sleeper(dt: float) -> None:
            if api.positions:
                pos = api.positions.pop(0)
                api.history_deals.append(
                    SimpleNamespace(
                        ticket=8099 + len(api.history_deals),
                        order=7099 + len(api.history_deals),
                        position_id=pos.ticket,
                        entry=api.DEAL_ENTRY_OUT,
                        symbol="EURUSD",
                        magic=1180191810,
                        volume=0.01,
                        profit=0.05,
                        reason=api.DEAL_REASON_SL,
                    )
                )
            sim_time[0] += timedelta(seconds=dt)

        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=10,
            max_duration_seconds=10.0,
            drain_timeout_seconds=300.0,
            max_quote_age_seconds=5.0,
            poll_interval_seconds=1.0,
            cooldown_seconds=1.0,
            clock=clock_fn,
            sleeper=custom_sleeper,
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        assert res.status == "completed"
        assert res.stop_reason == "max_duration_reached"
        assert res.active_position_id is None
    finally:
        store.close()


# Test 4: cooldown prevents early next entry
def test_cooldown_prevents_early_next_entry(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        def custom_sleeper(dt: float) -> None:
            if api.positions:
                pos = api.positions.pop(0)
                api.history_deals.append(
                    SimpleNamespace(
                        ticket=8099 + len(api.history_deals),
                        order=7099 + len(api.history_deals),
                        position_id=pos.ticket,
                        entry=api.DEAL_ENTRY_OUT,
                        symbol="EURUSD",
                        magic=1180191810,
                        volume=0.01,
                        profit=0.0,
                        reason=api.DEAL_REASON_SL,
                    )
                )
            sim_time[0] += timedelta(seconds=dt)

        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=2,
            max_duration_seconds=300.0,
            drain_timeout_seconds=300.0,
            max_quote_age_seconds=5.0,
            poll_interval_seconds=1.0,
            cooldown_seconds=30.0,
            clock=clock_fn,
            sleeper=custom_sleeper,
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        assert res.status == "completed"
        assert res.entries_completed == 2
    finally:
        store.close()


# Test 5: position remains held across multiple polls
def test_position_remains_held_across_multiple_polls(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=0,
        )
        c1 = session.poll_cycle(signal=sig)
        assert c1.kind == Mt5SessionCycleKind.PROCESSED
        assert session.active_position_id is not None

        # Multiple holding polls
        for _ in range(3):
            sim_time[0] += timedelta(seconds=1)
            c_hold = session.poll_cycle()
            assert c_hold.kind == Mt5SessionCycleKind.POSITION_HELD
            assert session.active_position_id is not None
    finally:
        store.close()


# Test 6: explicit close path
def test_explicit_close_path(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=0,
        )
        session.poll_cycle(signal=sig)
        c_close = session.poll_cycle(force_close=True)
        assert c_close.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert c_close.exit_reason == "MANUAL"
        assert session.active_position_id is None
    finally:
        store.close()


# Test 7: native SL path
def test_native_sl_path(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=0,
        )
        session.poll_cycle(signal=sig)
        pos_id = session.active_position_id

        # Native SL closes position on broker
        api.positions = []
        api.history_deals = [
            SimpleNamespace(
                ticket=8099,
                order=7099,
                position_id=int(pos_id),
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=1180191810,
                volume=0.01,
                profit=-0.05,
                reason=api.DEAL_REASON_SL,
            )
        ]
        c_sl = session.poll_cycle(current_time=sim_time[0] + timedelta(seconds=1))
        assert c_sl.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert c_sl.exit_reason == "SL"
        assert session.active_position_id is None
    finally:
        store.close()


# Test 8: native TP path
def test_native_tp_path(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=0,
        )
        session.poll_cycle(signal=sig)
        pos_id = session.active_position_id

        # Native TP closes position on broker
        api.positions = []
        api.history_deals = [
            SimpleNamespace(
                ticket=8099,
                order=7099,
                position_id=int(pos_id),
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=1180191810,
                volume=0.01,
                profit=0.10,
                reason=api.DEAL_REASON_TP,
            )
        ]
        c_tp = session.poll_cycle(current_time=sim_time[0] + timedelta(seconds=1))
        assert c_tp.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert c_tp.exit_reason == "TP"
        assert session.active_position_id is None
    finally:
        store.close()


# Test 9: stale quote causes no submission
def test_stale_quote_causes_no_submission(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=2.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        # Pass current_time 10s after quote timestamp (NOW)
        c = session.poll_cycle(signal=sig, current_time=NOW + timedelta(seconds=10))
        assert c.kind == Mt5SessionCycleKind.PAUSED
        assert c.reason == "data_stale"
        assert session.active_position_id is None
    finally:
        store.close()


# Test 10: future quote causes no submission
def test_future_quote_causes_no_submission(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=2.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        # Pass current_time 10s before quote timestamp (NOW)
        c = session.poll_cycle(signal=sig, current_time=NOW - timedelta(seconds=10))
        assert c.kind == Mt5SessionCycleKind.PAUSED
        assert c.reason == "data_stale"
        assert session.active_position_id is None
    finally:
        store.close()


# Test 11: missing quote causes no submission
def test_missing_quote_causes_no_submission(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=2.0),
            clock=clock_fn,
        )
        session.start()
        api.custom_ticks = []  # No ticks available
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        c = session.poll_cycle(signal=sig, current_time=NOW)
        assert c.kind == Mt5SessionCycleKind.PAUSED
        assert session.active_position_id is None
    finally:
        store.close()


# Test 12: disconnect before entry
def test_disconnect_before_entry(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        api.terminal.connected = False
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        c = session.poll_cycle(signal=sig, current_time=NOW)
        assert c.kind == Mt5SessionCycleKind.PAUSED
        assert c.reason == "broker_unavailable"
    finally:
        store.close()


# Test 13: disconnect while holding
def test_disconnect_while_holding(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        session.poll_cycle(signal=sig)
        pos_id = session.active_position_id
        assert pos_id is not None

        api.terminal.connected = False
        c_disc = session.poll_cycle(current_time=NOW + timedelta(seconds=1))
        assert c_disc.kind == Mt5SessionCycleKind.PAUSED
        assert c_disc.reason == "broker_unavailable"
        assert session.active_position_id == pos_id
    finally:
        store.close()


# Test 14: reconnect same position, no duplicate order
def test_reconnect_same_position_no_duplicate_order(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        session.poll_cycle(signal=sig)
        initial_order_count = api.order_send_count

        # Disconnect and reconnect
        api.terminal.connected = False
        session.poll_cycle(current_time=NOW + timedelta(seconds=1))
        api.terminal.connected = True
        c_rec = session.poll_cycle(current_time=NOW + timedelta(seconds=2))
        assert c_rec.kind == Mt5SessionCycleKind.POSITION_HELD
        assert api.order_send_count == initial_order_count
    finally:
        store.close()


# Test 15: reconnect after native protective exit
def test_reconnect_after_native_protective_exit(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        session.poll_cycle(signal=sig)
        pos_id = session.active_position_id

        # Disconnect -> Native exit on broker -> Reconnect
        api.terminal.connected = False
        session.poll_cycle(current_time=NOW + timedelta(seconds=1))

        api.positions = []
        api.history_deals = [
            SimpleNamespace(
                ticket=8099,
                order=7099,
                position_id=int(pos_id),
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=1180191810,
                volume=0.01,
                profit=-0.05,
                reason=api.DEAL_REASON_SL,
            )
        ]
        api.terminal.connected = True
        c_sl = session.poll_cycle(current_time=NOW + timedelta(seconds=2))
        assert c_sl.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert c_sl.exit_reason == "SL"
        assert session.active_position_id is None
    finally:
        store.close()


# Test 16: orphan position triggers RECONCILIATION_REQUIRED
def test_orphan_position_triggers_reconciliation_required(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        # Broker has orphan position unknown to ledger
        api.positions = [
            SimpleNamespace(
                ticket=9999,
                identifier=7999,
                symbol="EURUSD",
                type=api.POSITION_TYPE_BUY,
                magic=1180191810,
                comment="orphan",
                volume=0.01,
                price_open=1.10020,
                sl=1.09920,
                tp=0.0,
                profit=0.0,
                time=int(NOW.timestamp()),
            )
        ]
        state = session.reconcile_startup()
        assert state == "reconciliation_required"
        assert session.reconciliation_required is True
    finally:
        store.close()


# Test 17: contradictory identity triggers RECONCILIATION_REQUIRED
def test_contradictory_identity_triggers_reconciliation_required(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        # Ledger has ticket 9001, but broker has ticket 9999
        ledger.append(
            AuditEventType.POSITION_OPENED,
            occurred_at=NOW,
            component=AuditComponent.BROKER_ADAPTER,
            correlation=EventCorrelation(
                client_order_id="test_client_order",
                position_id="9001",
                broker_order_id="7001",
            ),
            payload={
                "position_id": "9001",
                "broker_order_id": "7001",
                "symbol": "EURUSD",
                "side": "BUY",
                "volume": 0.01,
            },
        )
        api.positions = [
            SimpleNamespace(
                ticket=9999,
                identifier=7999,
                symbol="EURUSD",
                type=api.POSITION_TYPE_BUY,
                magic=1180191810,
                volume=0.01,
                price_open=1.10020,
                sl=1.09920,
                tp=0.0,
                profit=0.0,
                time=int(NOW.timestamp()),
            )
        ]
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        state = session.reconcile_startup()
        assert state == "reconciliation_required"
    finally:
        store.close()


# Test 18: kill switch terminates soak
def test_kill_switch_terminates_soak(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=5,
            max_duration_seconds=300.0,
            drain_timeout_seconds=300.0,
            max_quote_age_seconds=5.0,
            clock=clock_fn,
            sleeper=lambda _: None,
        )
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        # Trigger kill switch in risk engine
        session.risk_engine.trigger_kill_switch(KillSwitchReason.MANUAL)
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, session=session)
        assert res.status == "failed"
        assert res.stop_reason == "kill_switch_triggered"
    finally:
        store.close()


# Test 19: risk rejection creates no order
def test_risk_rejection_creates_no_order(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        limits = RiskLimits(
            starting_equity=10000.0,
            max_trades_per_day=1,
            max_open_positions=1,
            max_exposure_per_symbol_lots=0.01,
        )
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            risk_limits=limits,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test_1",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=0,
        )
        c1 = session.poll_cycle(signal=sig)
        assert c1.kind == Mt5SessionCycleKind.PROCESSED
        # Close position
        pos_id = session.active_position_id
        api.positions = []
        api.history_deals = [
            SimpleNamespace(
                ticket=8099,
                order=7099,
                position_id=int(pos_id),
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=1180191810,
                volume=0.01,
                profit=0.0,
                reason=api.DEAL_REASON_SL,
            )
        ]
        sim_time[0] += timedelta(seconds=1)
        session.poll_cycle()

        # 2nd trade on same day is rejected by risk engine
        sim_time[0] += timedelta(minutes=1)
        sig2 = SignalEvent(
            setup_name="test_2",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=1,
        )
        initial_order_count = api.order_send_count
        c2 = session.poll_cycle(signal=sig2, current_time=sim_time[0])
        assert c2.kind == Mt5SessionCycleKind.RISK_REJECTED
        assert api.order_send_count == initial_order_count
    finally:
        store.close()


# Test 20: KeyboardInterrupt results in safe clean shutdown when flat
def test_keyboard_interrupt_safe_clean_shutdown(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=5,
            max_duration_seconds=300.0,
            drain_timeout_seconds=300.0,
            max_quote_age_seconds=5.0,
            clock=clock_fn,
            sleeper=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

        class _NoSignalGenerator(SyntheticSoakSignalGenerator):
            def next_signal(self, current_time: datetime) -> None:
                return None

        runner = Mt5DemoSoakRunner(signal_generator=_NoSignalGenerator())
        res = runner.run(config, broker=broker, ledger=ledger)
        assert res.status == "stopped"
        assert res.stop_reason == "operator_interrupted"
        assert res.active_position_id is None
    finally:
        store.close()


# Test 21: audit-store failure fails closed
def test_audit_store_failure_fails_closed(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        store.close()  # Close DB so subsequent writes fail
        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=2,
            max_duration_seconds=300.0,
            drain_timeout_seconds=300.0,
            max_quote_age_seconds=5.0,
            clock=clock_fn,
        )
        runner = Mt5DemoSoakRunner()
        with pytest.raises((RuntimeError, sqlite3.ProgrammingError)):
            runner.run(config, broker=broker, ledger=ledger)
    finally:
        pass


# Test 22: max_loss_usd invalid/missing rejected
def test_max_loss_usd_invalid_or_missing_rejected() -> None:
    for invalid_val in (0, -1.0, float("nan"), float("inf"), None, True):
        with pytest.raises(ValueError):
            Mt5DemoSoakConfig(
                confirmation=MT5_DEMO_SOAK_CONFIRMATION,
                max_loss_usd=invalid_val,  # type: ignore[arg-type]
                max_entries=5,
                max_duration_seconds=300.0,
                drain_timeout_seconds=300.0,
                max_quote_age_seconds=5.0,
            )


# Test 23: max_entries invalid/missing rejected
def test_max_entries_invalid_or_missing_rejected() -> None:
    for invalid_entries in (0, -1, MAX_ENTRIES_UPPER_BOUND + 1, 1.5, None, True):
        with pytest.raises(ValueError):
            Mt5DemoSoakConfig(
                confirmation=MT5_DEMO_SOAK_CONFIRMATION,
                max_loss_usd=1.0,
                max_entries=invalid_entries,  # type: ignore[arg-type]
                max_duration_seconds=300.0,
                drain_timeout_seconds=300.0,
                max_quote_age_seconds=5.0,
            )


# Test 24: max_duration invalid/missing rejected
def test_max_duration_invalid_or_missing_rejected() -> None:
    for invalid_dur in (0, -10.0, MAX_DURATION_SECONDS_UPPER_BOUND + 1, None, True):
        with pytest.raises(ValueError):
            Mt5DemoSoakConfig(
                confirmation=MT5_DEMO_SOAK_CONFIRMATION,
                max_loss_usd=1.0,
                max_entries=5,
                max_duration_seconds=invalid_dur,  # type: ignore[arg-type]
                drain_timeout_seconds=300.0,
                max_quote_age_seconds=5.0,
            )


# Test 25: max_quote_age invalid/missing rejected
def test_max_quote_age_invalid_or_missing_rejected() -> None:
    for invalid_age in (0, -5.0, 70.0, None, True):
        with pytest.raises(ValueError):
            Mt5DemoSoakConfig(
                confirmation=MT5_DEMO_SOAK_CONFIRMATION,
                max_loss_usd=1.0,
                max_entries=5,
                max_duration_seconds=300.0,
                drain_timeout_seconds=300.0,
                max_quote_age_seconds=invalid_age,  # type: ignore[arg-type]
            )


# Test 26: setup_name spoofing cannot bypass structural permit
def test_setup_name_spoofing_cannot_bypass_structural_permit() -> None:
    sig = SignalEvent(
        setup_name="synthetic_soak_test",
        symbol="EURUSD",
        timeframe="M1",
        side=1,
        signal_time=NOW,
        signal_bar_index=0,
    )
    # Call gate without permit -> must raise
    with pytest.raises(RuntimeError, match="unvalidated_strategy_execution_prohibited"):
        _validate_strategy_gate(sig, None)


# Test 27: Candidate C-like setup name cannot obtain authorization
def test_candidate_c_like_setup_name_cannot_obtain_authorization() -> None:
    for name in ("candidate_c", "smc_ict", "production_alpha"):
        sig = SignalEvent(
            setup_name=name,
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        with pytest.raises(RuntimeError, match="unvalidated_strategy_execution_prohibited"):
            _validate_strategy_gate(sig, None)


# Test 28: no second order while first position remains open
def test_no_second_order_while_first_position_remains_open(tmp_path) -> None:
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig1 = SignalEvent(
            setup_name="test_1",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        session.poll_cycle(signal=sig1)
        initial_order_count = api.order_send_count
        assert session.active_position_id is not None

        # Try to send second signal while position is open
        sig2 = SignalEvent(
            setup_name="test_2",
            symbol="EURUSD",
            timeframe="M1",
            side=-1,
            signal_time=NOW + timedelta(seconds=1),
            signal_bar_index=1,
        )
        c2 = session.poll_cycle(signal=sig2, current_time=NOW + timedelta(seconds=1))
        assert c2.kind == Mt5SessionCycleKind.POSITION_HELD
        assert api.order_send_count == initial_order_count
    finally:
        store.close()


# Test 29: final flat state after successful complete soak
def test_final_flat_state_after_successful_complete_soak(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        def custom_sleeper(dt: float) -> None:
            if api.positions:
                pos = api.positions.pop(0)
                api.history_deals.append(
                    SimpleNamespace(
                        ticket=8099 + len(api.history_deals),
                        order=7099 + len(api.history_deals),
                        position_id=pos.ticket,
                        entry=api.DEAL_ENTRY_OUT,
                        symbol="EURUSD",
                        magic=1180191810,
                        volume=0.01,
                        profit=0.0,
                        reason=api.DEAL_REASON_SL,
                    )
                )
            sim_time[0] += timedelta(seconds=dt)

        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=1,
            max_duration_seconds=300.0,
            drain_timeout_seconds=300.0,
            max_quote_age_seconds=5.0,
            poll_interval_seconds=0.1,
            cooldown_seconds=0.1,
            clock=clock_fn,
            sleeper=custom_sleeper,
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        assert res.status == "completed"
        assert res.active_position_id is None
        assert len(api.positions) == 0
    finally:
        store.close()


# Test 30: deterministic audit ordering using EXISTING canonical event types
def test_deterministic_audit_ordering_using_existing_canonical_event_types(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        session = _create_session(
            broker=broker,
            event_ledger=ledger,
            max_loss_usd=1.0,
            max_quote_age=timedelta(seconds=5.0),
            clock=clock_fn,
        )
        session.start()
        sig = SignalEvent(
            setup_name="test_audit",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=0,
        )
        session.poll_cycle(signal=sig)
        pos_id = session.active_position_id
        api.positions = []
        api.history_deals = [
            SimpleNamespace(
                ticket=8099,
                order=7099,
                position_id=int(pos_id),
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=1180191810,
                volume=0.01,
                profit=0.0,
                reason=api.DEAL_REASON_SL,
            )
        ]
        sim_time[0] += timedelta(seconds=1)
        session.poll_cycle()
        session.stop()

        events = store.load_events()
        event_types = [e.event_type for e in events]
        # Verify sequence contains canonical repository event types
        assert AuditEventType.SESSION_STARTED in event_types
        assert AuditEventType.RISK_APPROVED in event_types
        assert AuditEventType.ORDER_SUBMISSION_ATTEMPTED in event_types
        assert AuditEventType.ORDER_SUBMITTED in event_types
        assert AuditEventType.ORDER_FILLED in event_types
        assert AuditEventType.POSITION_OPENED in event_types
        assert AuditEventType.POSITION_CLOSED in event_types
        assert AuditEventType.SESSION_STOPPED in event_types

        # Verify monotonically increasing sequence IDs
        seq_ids = [e.sequence for e in events]
        assert seq_ids == sorted(seq_ids)
        assert len(seq_ids) == len(set(seq_ids))
    finally:
        store.close()


# Test 31: duration expires while holding prevents second entry
def test_duration_expires_while_holding_prevents_second_entry(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        # Duration 5s, drain 10s. Position remains open throughout
        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=10,
            max_duration_seconds=5.0,
            drain_timeout_seconds=10.0,
            max_quote_age_seconds=5.0,
            poll_interval_seconds=1.0,
            cooldown_seconds=1.0,
            clock=clock_fn,
            sleeper=lambda dt: sim_time.__setitem__(0, sim_time[0] + timedelta(seconds=dt)),
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        # Position was never closed, so drain timeout expired
        assert res.status == "stopped"
        assert res.stop_reason == "drain_timeout_with_open_position"
        assert res.entries_completed == 1
        # Exactly 1 order was sent, zero new entries attempted after duration expired
        assert api.order_send_count == 1
        assert res.active_position_id is not None
    finally:
        store.close()


# Test 32: held position closes during drain leads to clean final flat
def test_held_position_closes_during_drain_final_flat(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        # Position stays open during main duration (5s), but closes during drain (at t=8s)
        def custom_sleeper(dt: float) -> None:
            sim_time[0] += timedelta(seconds=dt)
            elapsed = (sim_time[0] - NOW).total_seconds()
            if elapsed >= 8.0 and api.positions:
                pos = api.positions.pop(0)
                api.history_deals.append(
                    SimpleNamespace(
                        ticket=8099,
                        order=7099,
                        position_id=pos.ticket,
                        entry=api.DEAL_ENTRY_OUT,
                        symbol="EURUSD",
                        magic=1180191810,
                        volume=0.01,
                        profit=0.05,
                        reason=api.DEAL_REASON_TP,
                    )
                )

        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=5,
            max_duration_seconds=5.0,
            drain_timeout_seconds=10.0,
            max_quote_age_seconds=5.0,
            poll_interval_seconds=1.0,
            cooldown_seconds=1.0,
            clock=clock_fn,
            sleeper=custom_sleeper,
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        assert res.status == "completed"
        assert res.stop_reason == "max_duration_reached"
        assert res.entries_completed == 1
        assert res.active_position_id is None
    finally:
        store.close()


# Test 33: held position does not close before drain deadline -> preserves position identity
def test_held_position_does_not_close_before_drain_deadline_preserves_position(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=5,
            max_duration_seconds=5.0,
            drain_timeout_seconds=5.0,
            max_quote_age_seconds=5.0,
            poll_interval_seconds=1.0,
            clock=clock_fn,
            sleeper=lambda dt: sim_time.__setitem__(0, sim_time[0] + timedelta(seconds=dt)),
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        assert res.status == "stopped"
        assert res.stop_reason == "drain_timeout_with_open_position"
        assert res.active_position_id is not None
        assert "remained open after drain timeout" in (res.error_message or "")
    finally:
        store.close()


# Test 34: no duplicate close or order mutation during drain
def test_no_duplicate_close_or_order_mutation_during_drain(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=5,
            max_duration_seconds=3.0,
            drain_timeout_seconds=5.0,
            max_quote_age_seconds=5.0,
            poll_interval_seconds=0.5,
            clock=clock_fn,
            sleeper=lambda dt: sim_time.__setitem__(0, sim_time[0] + timedelta(seconds=dt)),
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        # Exactly 1 order was sent for entry, zero mutations during drain
        assert api.order_send_count == 1
        assert res.active_position_id is not None
    finally:
        store.close()


# Test 35: KeyboardInterrupt while holding preserves exposure and non-success status
def test_keyboard_interrupt_while_holding_preserves_exposure(tmp_path) -> None:
    sim_time = [NOW]
    broker, ledger, api, store, clock_fn = _soak_fixture(tmp_path, time_ref=sim_time)
    try:
        # Interrupt immediately after the position is opened
        def interrupting_sleeper(dt: float) -> None:
            if api.positions:
                raise KeyboardInterrupt()

        config = Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=5,
            max_duration_seconds=300.0,
            drain_timeout_seconds=300.0,
            max_quote_age_seconds=5.0,
            clock=clock_fn,
            sleeper=interrupting_sleeper,
        )
        runner = Mt5DemoSoakRunner()
        res = runner.run(config, broker=broker, ledger=ledger)
        assert res.status == "stopped"
        assert res.stop_reason == "operator_interrupted_with_open_position"
        assert res.active_position_id is not None
        assert "operator interrupted while position" in (res.error_message or "")
    finally:
        store.close()


# Test 36: CLI sleeper injection and poll interval respected
def test_cli_sleeper_injection_and_poll_interval_respected() -> None:
    sleep_calls = []

    def mock_sleeper(sec: float) -> None:
        sleep_calls.append(sec)

    config = Mt5DemoSoakConfig(
        confirmation=MT5_DEMO_SOAK_CONFIRMATION,
        max_loss_usd=1.0,
        max_entries=1,
        max_duration_seconds=10.0,
        drain_timeout_seconds=10.0,
        max_quote_age_seconds=5.0,
        poll_interval_seconds=2.5,
        sleeper=mock_sleeper,
    )
    assert config.sleeper is mock_sleeper
    assert config.poll_interval_seconds == 2.5


# Test 37: drain_timeout_seconds validation
def test_drain_timeout_seconds_validation() -> None:
    for invalid in (
        0,
        -1.0,
        float("nan"),
        float("inf"),
        None,
        True,
        MAX_DURATION_SECONDS_UPPER_BOUND + 1,
    ):
        with pytest.raises(ValueError):
            Mt5DemoSoakConfig(
                confirmation=MT5_DEMO_SOAK_CONFIRMATION,
                max_loss_usd=1.0,
                max_entries=1,
                max_duration_seconds=10.0,
                max_quote_age_seconds=5.0,
                drain_timeout_seconds=invalid,  # type: ignore[arg-type]
            )


# Test 38: MAX_ENTRIES_UPPER_BOUND contract is exactly 100
def test_max_entries_upper_bound_contract() -> None:
    assert MAX_ENTRIES_UPPER_BOUND == 100
    # Boundary tests
    config_100 = Mt5DemoSoakConfig(
        confirmation=MT5_DEMO_SOAK_CONFIRMATION,
        max_loss_usd=1.0,
        max_entries=100,
        max_duration_seconds=10.0,
        drain_timeout_seconds=10.0,
        max_quote_age_seconds=5.0,
    )
    assert config_100.max_entries == 100

    with pytest.raises(ValueError, match="invalid_max_entries"):
        Mt5DemoSoakConfig(
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=101,
            max_duration_seconds=10.0,
            drain_timeout_seconds=10.0,
            max_quote_age_seconds=5.0,
        )


# Test 39: drain_timeout_seconds is mandatory with no default in Mt5DemoSoakConfig
def test_drain_timeout_seconds_mandatory_in_config() -> None:
    with pytest.raises(TypeError):
        Mt5DemoSoakConfig(  # type: ignore[call-arg]
            confirmation=MT5_DEMO_SOAK_CONFIRMATION,
            max_loss_usd=1.0,
            max_entries=5,
            max_duration_seconds=10.0,
            max_quote_age_seconds=5.0,
        )


# Test 40: Phase 2B soak authorization succeeds strictly through soak construction path
def test_phase_2b_soak_authorization_strictly_through_soak_path() -> None:
    config = Mt5DemoSoakConfig(
        confirmation=MT5_DEMO_SOAK_CONFIRMATION,
        max_loss_usd=1.0,
        max_entries=5,
        max_duration_seconds=10.0,
        drain_timeout_seconds=10.0,
        max_quote_age_seconds=5.0,
    )
    permit = _issue_soak_execution_permit(config)
    assert isinstance(permit, _Mt5DemoSoakExecutionPermit)
    assert not isinstance(permit, _Mt5DemoExecutionPermit)

    # Validates against gate
    sig = SignalEvent(
        setup_name="synthetic_soak_test",
        symbol="EURUSD",
        timeframe="M1",
        side=1,
        signal_time=NOW,
        signal_bar_index=0,
    )
    _validate_strategy_gate(sig, permit)

    # Invalid input to _issue_soak_execution_permit rejected
    with pytest.raises(TypeError, match="valid_soak_config_required"):
        _issue_soak_execution_permit("invalid_config")  # type: ignore[arg-type]


# Test 41: CLI provides no generic authorization bypass
def test_cli_provides_no_generic_authorization_bypass() -> None:
    from fxlab.cli import mt5_demo_soak

    sig = inspect.signature(mt5_demo_soak)
    param_names = set(sig.parameters.keys())
    # Verify no permit or strategy bypass parameter exists
    assert "permit" not in param_names
    assert "execution_permit" not in param_names
    assert "allow_strategy" not in param_names
    assert "bypass" not in param_names


# Test 42: CLI requires drain_timeout_seconds with no default
def test_cli_requires_drain_timeout_seconds_with_no_default() -> None:
    from fxlab.cli import mt5_demo_soak

    sig = inspect.signature(mt5_demo_soak)
    assert "drain_timeout_seconds" in sig.parameters
    drain_param = sig.parameters["drain_timeout_seconds"]
    # No default value
    assert drain_param.default is inspect.Parameter.empty
