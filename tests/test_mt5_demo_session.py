"""Tests for persistent unattended MT5 Demo session runner (Phase 2A)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from fxlab.execution.broker import Tick
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
)
from fxlab.execution.mt5_demo_broker import Mt5DemoBroker, _mt5_comment
from fxlab.execution.mt5_demo_session import (
    Mt5DemoSession,
    Mt5SessionCycleKind,
    _phase_2a_test_execution_permit,
    _validate_strategy_gate,
)
from fxlab.execution.runtime_control import RuntimeControlReason, RuntimeState
from fxlab.execution.signal_engine import SignalEvent
from fxlab.risk.engine import KillSwitchReason, RiskLimits

NOW = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


class SessionFakeMt5Api:
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
        if "ticket" in query:
            return tuple(
                d for d in self.history_deals if getattr(d, "ticket", None) == query["ticket"]
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
            self.account.balance += self.realized_pnl
            self.account.equity += self.realized_pnl
            self.history_deals.append(
                SimpleNamespace(
                    ticket=result.deal,
                    order=result.order,
                    position_id=ticket,
                    entry=self.DEAL_ENTRY_OUT,
                    symbol="EURUSD",
                    magic=request["magic"],
                    volume=request["volume"],
                    reason=self.DEAL_REASON_CLIENT,
                    profit=self.realized_pnl,
                )
            )
            self.positions = [
                item for item in self.positions if getattr(item, "ticket", None) != ticket
            ]
        return result


def _session_fixture(
    tmp_path, api: SessionFakeMt5Api | None = None, **kwargs
) -> tuple[Mt5DemoSession, SessionFakeMt5Api, SQLiteEventStore]:
    time_ref = kwargs.get("time_ref")
    clock_fn = (lambda: time_ref[0]) if time_ref is not None else kwargs.get("clock", lambda: NOW)
    selected_api = api or SessionFakeMt5Api(clock=clock_fn)
    store = SQLiteEventStore(tmp_path / "mt5-demo-session.sqlite", "mt5-session-test")
    ledger = EventLedger(store.session_id, time_provider=clock_fn, durable_store=store)

    mono_time = [0.0]

    def default_monotonic():
        if time_ref is not None:
            return max(0.0, (time_ref[0] - NOW).total_seconds()) + mono_time[0]
        mono_time[0] += 0.001
        return mono_time[0]

    broker = Mt5DemoBroker(
        api=selected_api,
        clock=clock_fn,
        monotonic=kwargs.get("monotonic", default_monotonic),
        sleeper=kwargs.get("sleeper", lambda _: None),
        max_quote_age=kwargs.get("max_quote_age", timedelta(seconds=5)),
    )
    permit = kwargs.get("execution_permit", _phase_2a_test_execution_permit())
    session = Mt5DemoSession(
        broker=broker,
        event_ledger=ledger,
        max_loss_usd=kwargs.get("max_loss_usd", 1.0),
        max_quote_age=kwargs.get("max_quote_age", timedelta(seconds=5)),
        execution_permit=permit,
        risk_limits=kwargs.get("risk_limits"),
        clock=clock_fn,
    )
    return session, selected_api, store


def test_strategy_gate_strictly_prohibits_unvalidated_strategies() -> None:
    spoofed_signal = SignalEvent(
        setup_name="test_something",
        symbol="EURUSD",
        timeframe="M1",
        side=1,
        signal_time=NOW,
        signal_bar_index=0,
    )
    with pytest.raises(RuntimeError, match="unvalidated_strategy_execution_prohibited"):
        _validate_strategy_gate(spoofed_signal, None)
    _validate_strategy_gate(spoofed_signal, _phase_2a_test_execution_permit())


def test_spoofed_setup_name_cannot_execute_without_structural_permit(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path, execution_permit=None)
    try:
        session.start()
        with pytest.raises(RuntimeError, match="unvalidated_strategy_execution_prohibited"):
            session.poll_cycle(
                SignalEvent(
                    setup_name="test_something",
                    symbol="EURUSD",
                    timeframe="M1",
                    side=1,
                    signal_time=NOW,
                    signal_bar_index=0,
                )
            )
        assert api.order_send_count == 0
    finally:
        store.close()


def test_session_requires_explicit_positive_quote_freshness(tmp_path) -> None:
    api = SessionFakeMt5Api()
    store = SQLiteEventStore(tmp_path / "freshness.sqlite", "freshness")
    ledger = EventLedger(store.session_id, time_provider=lambda: NOW, durable_store=store)
    broker = Mt5DemoBroker(api=api, clock=lambda: NOW)
    try:
        with pytest.raises(TypeError):
            Mt5DemoSession(  # type: ignore[call-arg]
                broker=broker, event_ledger=ledger, max_loss_usd=1.0
            )
        for invalid in (timedelta(0), timedelta(microseconds=-1)):
            with pytest.raises(ValueError, match="max_quote_age must be positive"):
                Mt5DemoSession(
                    broker=broker,
                    event_ledger=ledger,
                    max_loss_usd=1.0,
                    max_quote_age=invalid,
                )
    finally:
        store.close()


def test_session_lifecycle_start_pause_resume_stop(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        res = session.start()
        assert res.current_state == RuntimeState.RUNNING

        pause_res = session.pause(RuntimeControlReason.OPERATOR_PAUSED)
        assert pause_res.current_state == RuntimeState.PAUSED

        resume_res = session.resume()
        assert resume_res.current_state == RuntimeState.RUNNING

        stop_res = session.stop()
        assert stop_res.current_state == RuntimeState.STOPPED
    finally:
        store.close()


def test_multiple_sequential_successful_trades(tmp_path) -> None:
    sim_time = [NOW]
    session, api, store = _session_fixture(tmp_path, time_ref=sim_time)
    try:
        session.start()

        # Trade 1: BUY
        signal1 = SignalEvent(
            setup_name="test_signal_1",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=0,
        )
        c1 = session.poll_cycle(signal1)
        assert c1.kind == Mt5SessionCycleKind.PROCESSED
        assert session.active_position_id is not None

        # Explicit close of Trade 1
        c1_close = session.poll_cycle(force_close=True)
        assert c1_close.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert session.active_position_id is None

        # Advance simulated time
        sim_time[0] = NOW + timedelta(minutes=1)

        # Trade 2: SELL
        api.entry_result = SimpleNamespace(
            retcode=api.TRADE_RETCODE_DONE,
            order=7003,
            deal=8003,
            volume=0.01,
            price=1.10000,
        )
        api.close_result = SimpleNamespace(
            retcode=api.TRADE_RETCODE_DONE,
            order=7004,
            deal=8004,
            volume=0.01,
            price=1.10020,
        )
        signal2 = SignalEvent(
            setup_name="test_signal_2",
            symbol="EURUSD",
            timeframe="M1",
            side=-1,
            signal_time=sim_time[0],
            signal_bar_index=1,
        )
        c2 = session.poll_cycle(signal2, current_time=sim_time[0])
        assert c2.kind == Mt5SessionCycleKind.PROCESSED
        assert session.active_position_id is not None

        # Advance time and close Trade 2
        sim_time[0] = NOW + timedelta(minutes=2)
        c2_close = session.poll_cycle(force_close=True, current_time=sim_time[0])
        assert c2_close.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert session.active_position_id is None
    finally:
        store.close()


def test_position_held_across_multiple_poll_cycles(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        session.start()
        signal = SignalEvent(
            setup_name="test_hold",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        c1 = session.poll_cycle(signal)
        assert c1.kind == Mt5SessionCycleKind.PROCESSED
        pos_id = session.active_position_id
        assert pos_id is not None

        # Poll cycles 2, 3, 4 with no force close -> POSITION_HELD
        for i in range(1, 4):
            ch = session.poll_cycle(current_time=NOW + timedelta(seconds=i))
            assert ch.kind == Mt5SessionCycleKind.POSITION_HELD
            assert session.active_position_id == pos_id

        # Cycle 5: explicit close
        c_close = session.poll_cycle(force_close=True, current_time=NOW + timedelta(seconds=5))
        assert c_close.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert session.active_position_id is None
    finally:
        store.close()


def test_native_sl_during_hold(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        session.start()
        signal = SignalEvent(
            setup_name="test_sl_hold",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        c1 = session.poll_cycle(signal)
        assert c1.kind == Mt5SessionCycleKind.PROCESSED
        pos_id = session.active_position_id
        assert pos_id is not None

        # Simulate position hitting native SL on broker:
        # active position disappears, history deal records DEAL_REASON_SL
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
                reason=api.DEAL_REASON_SL,
                profit=0.0,
            )
        ]

        # Next poll cycle detects native SL exit
        c_sl = session.poll_cycle(current_time=NOW + timedelta(seconds=1))
        assert c_sl.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert c_sl.exit_reason == "SL"
        assert c_sl.close_order_id == "7099"
        assert c_sl.close_deal_id == "8099"
        assert session.active_position_id is None
    finally:
        store.close()


def test_native_tp_during_hold(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        session.start()
        signal = SignalEvent(
            setup_name="test_tp_hold",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        c1 = session.poll_cycle(signal)
        assert c1.kind == Mt5SessionCycleKind.PROCESSED
        pos_id = session.active_position_id
        assert pos_id is not None

        # Simulate position hitting native TP on broker:
        api.positions = []
        api.history_deals = [
            SimpleNamespace(
                ticket=8088,
                order=7088,
                position_id=int(pos_id),
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=1180191810,
                volume=0.01,
                reason=api.DEAL_REASON_TP,
                profit=0.0,
            )
        ]

        # Next poll cycle detects native TP exit
        c_tp = session.poll_cycle(current_time=NOW + timedelta(seconds=1))
        assert c_tp.kind == Mt5SessionCycleKind.POSITION_CLOSED
        assert c_tp.exit_reason == "TP"
        assert c_tp.close_order_id == "7088"
        assert c_tp.close_deal_id == "8088"
        assert session.active_position_id is None
    finally:
        store.close()


def test_disconnect_and_reconnect_resync(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        session.start()
        signal = SignalEvent(
            setup_name="test_disconnect",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        c1 = session.poll_cycle(signal)
        assert c1.kind == Mt5SessionCycleKind.PROCESSED
        pos_id = session.active_position_id
        assert pos_id is not None

        # Broker terminal disconnects
        api.terminal.connected = False
        c_disc = session.poll_cycle(current_time=NOW + timedelta(seconds=1))
        assert c_disc.kind == Mt5SessionCycleKind.PAUSED
        assert c_disc.reason == "broker_unavailable"
        assert session.runtime_controller.status().state == RuntimeState.PAUSED

        # Terminal reconnects
        api.terminal.connected = True
        c_rec = session.poll_cycle(current_time=NOW + timedelta(seconds=2))
        assert c_rec.kind == Mt5SessionCycleKind.POSITION_HELD
        assert session.active_position_id == pos_id
    finally:
        store.close()


def test_stale_quote_blocks_entry(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path, max_quote_age=timedelta(seconds=5))
    try:
        session.start()
        # Mock broker tick emitted at NOW
        api.custom_ticks = [
            SimpleNamespace(time_msc=int(NOW.timestamp() * 1000), bid=1.10000, ask=1.10020),
            SimpleNamespace(time_msc=int(NOW.timestamp() * 1000) + 10, bid=1.10000, ask=1.10020),
        ]
        signal = SignalEvent(
            setup_name="test_stale",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        # Pass current_time 10s after quote timestamp
        c_stale = session.poll_cycle(signal, current_time=NOW + timedelta(seconds=10))
        assert c_stale.kind == Mt5SessionCycleKind.PAUSED
        assert c_stale.reason == "data_stale"
        assert session.active_position_id is None
    finally:
        store.close()


def test_startup_reconciliation_clean_broker_and_ledger(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        state = session.reconcile_startup()
        assert state == "clean"
    finally:
        store.close()


def test_startup_reconciliation_with_matching_open_position(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        # Pre-seed ledger with POSITION_OPENED
        client_order_id = "startup-client"
        session.event_ledger.append(
            AuditEventType.POSITION_OPENED,
            occurred_at=NOW,
            component=AuditComponent.BROKER_ADAPTER,
            correlation=EventCorrelation(
                client_order_id=client_order_id,
                broker_order_id="7001",
                position_id="9001",
            ),
            payload={
                "symbol": "EURUSD",
                "side": "buy",
                "volume": 0.01,
                "entry_order_id": "7001",
                "entry_deal_id": "8001",
                "position_id": "9001",
            },
        )
        # Seed broker with matching position ticket 9001
        api.positions = [
            SimpleNamespace(
                ticket=9001,
                identifier=7001,
                symbol="EURUSD",
                type=api.POSITION_TYPE_BUY,
                magic=1180191810,
                comment=_mt5_comment(client_order_id),
                volume=0.01,
                price_open=1.10020,
                sl=1.09920,
                tp=0.0,
                profit=0.0,
                time=int(NOW.timestamp()),
            )
        ]

        state = session.reconcile_startup()
        assert state == "reconnected_open_position"
        assert session.active_position_id == "9001"
    finally:
        store.close()


def test_startup_reconciliation_with_orphan_mt5_position_fails_closed(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        # Clean ledger, but broker has an unknown orphan position
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
        assert session.risk_engine.kill_switch_active is True
    finally:
        store.close()


def test_kill_switch_blocks_cycles(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        session.start()
        session.risk_engine.trigger_kill_switch(KillSwitchReason.MANUAL)

        signal = SignalEvent(
            setup_name="test_kill_switch",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        c = session.poll_cycle(signal)
        assert c.kind == Mt5SessionCycleKind.FAILED
        assert c.reason == "kill_switch_active"
    finally:
        store.close()


def test_daily_trade_count_limit_rejected_by_risk_engine(tmp_path) -> None:
    limits = RiskLimits(
        starting_equity=10000.0,
        max_trades_per_day=1,
        max_open_positions=1,
        max_exposure_per_symbol_lots=0.01,
    )
    sim_time = [NOW]
    session, api, store = _session_fixture(tmp_path, risk_limits=limits, time_ref=sim_time)
    try:
        session.start()

        # Trade 1 succeeds
        signal1 = SignalEvent(
            setup_name="test_trade_1",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=0,
        )
        c1 = session.poll_cycle(signal1)
        assert c1.kind == Mt5SessionCycleKind.PROCESSED
        session.poll_cycle(force_close=True)

        # Advance time on same day
        sim_time[0] = NOW + timedelta(minutes=5)

        # Trade 2 on same day is rejected by max_trades_per_day
        signal2 = SignalEvent(
            setup_name="test_trade_2",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=sim_time[0],
            signal_bar_index=1,
        )
        c2 = session.poll_cycle(signal2, current_time=sim_time[0])
        assert c2.kind == Mt5SessionCycleKind.RISK_REJECTED
        assert c2.reason == "max_daily_trades"
    finally:
        store.close()


def test_clean_shutdown_and_audit_ordering(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        session.start()
        signal = SignalEvent(
            setup_name="test_shutdown",
            symbol="EURUSD",
            timeframe="M1",
            side=1,
            signal_time=NOW,
            signal_bar_index=0,
        )
        session.poll_cycle(signal)
        session.poll_cycle(force_close=True)
        session.stop()

        events = session.event_ledger.events()
        assert [e.sequence for e in events] == list(range(1, len(events) + 1))
        event_types = [e.event_type for e in events]
        assert event_types[0] == AuditEventType.SESSION_STARTED
        assert AuditEventType.POSITION_OPENED in event_types
        assert AuditEventType.POSITION_CLOSED in event_types
        assert event_types[-1] == AuditEventType.SESSION_STOPPED
    finally:
        store.close()


def _test_signal(name: str = "arbitrary_name", *, side: int = 1, index: int = 0) -> SignalEvent:
    return SignalEvent(
        setup_name=name,
        symbol="EURUSD",
        timeframe="M1",
        side=side,
        signal_time=NOW,
        signal_bar_index=index,
    )


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(seconds=5), Mt5SessionCycleKind.PROCESSED),
        (timedelta(seconds=5, microseconds=1), Mt5SessionCycleKind.PAUSED),
        (timedelta(microseconds=-1), Mt5SessionCycleKind.PAUSED),
    ],
)
def test_quote_freshness_boundary_and_future_fail_closed(
    tmp_path, monkeypatch, offset, expected
) -> None:
    session, api, store = _session_fixture(tmp_path, max_quote_age=timedelta(seconds=5))
    try:
        session.start()
        tick = Tick("EURUSD", NOW, 1.1, 1.1002, 1.1001)
        monkeypatch.setattr(Mt5DemoBroker, "get_latest_tick", lambda self, symbol: tick)
        result = session.poll_cycle(_test_signal(), current_time=NOW + offset)
        assert result.kind is expected
        if expected is Mt5SessionCycleKind.PAUSED:
            assert result.reason == "data_stale"
            assert api.order_send_count == 0
    finally:
        store.close()


def test_reconnect_clean_state_reconciles_before_resuming_entries(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        session.start()
        api.terminal.connected = False
        assert session.poll_cycle().kind is Mt5SessionCycleKind.PAUSED
        api.terminal.connected = True
        before = api.order_send_count
        result = session.poll_cycle(_test_signal(), current_time=NOW + timedelta(seconds=1))
        assert result.kind is Mt5SessionCycleKind.NO_SIGNAL
        assert result.reason == "broker_reconnected_clean"
        assert api.order_send_count == before
        assert session.runtime_controller.status().state is RuntimeState.RUNNING
    finally:
        store.close()


def test_reconnect_native_exit_reconciles_before_resume(tmp_path) -> None:
    session, api, store = _session_fixture(tmp_path)
    try:
        session.start()
        assert session.poll_cycle(_test_signal()).kind is Mt5SessionCycleKind.PROCESSED
        position_id = session.active_position_id
        assert position_id is not None
        api.terminal.connected = False
        assert (
            session.poll_cycle(current_time=NOW + timedelta(seconds=1)).kind
            is Mt5SessionCycleKind.PAUSED
        )
        api.positions = []
        api.realized_pnl = -2.5
        api.account.balance += api.realized_pnl
        api.account.equity += api.realized_pnl
        api.history_deals = [
            SimpleNamespace(
                ticket=8099,
                order=7099,
                position_id=int(position_id),
                entry=api.DEAL_ENTRY_OUT,
                symbol="EURUSD",
                magic=1180191810,
                volume=0.01,
                reason=api.DEAL_REASON_SL,
                profit=api.realized_pnl,
            )
        ]
        api.terminal.connected = True
        result = session.poll_cycle(
            _test_signal("must_not_duplicate"), current_time=NOW + timedelta(seconds=2)
        )
        assert result.kind is Mt5SessionCycleKind.POSITION_CLOSED
        assert result.exit_reason == "SL"
        assert session.active_position_id is None
        assert session.risk_engine.consecutive_losses == 1
        assert api.order_send_count == 1
        assert session.runtime_controller.status().state is RuntimeState.RUNNING
    finally:
        store.close()


def test_reconnect_orphan_and_contradictory_positions_require_reconciliation(tmp_path) -> None:
    for contradictory in (False, True):
        case_path = tmp_path / ("contradictory" if contradictory else "orphan")
        case_path.mkdir()
        session, api, store = _session_fixture(case_path)
        try:
            session.start()
            assert session.poll_cycle(_test_signal()).kind is Mt5SessionCycleKind.PROCESSED
            original = api.positions[0]
            api.terminal.connected = False
            assert session.poll_cycle().kind is Mt5SessionCycleKind.PAUSED
            api.positions = [
                SimpleNamespace(
                    **{
                        **vars(original),
                        "ticket": original.ticket if contradictory else 9999,
                        "identifier": 999999 if contradictory else original.identifier,
                    }
                )
            ]
            api.terminal.connected = True
            result = session.poll_cycle(_test_signal("test_spoof"))
            assert result.kind is Mt5SessionCycleKind.RECONCILIATION_REQUIRED
            assert api.order_send_count == 1
        finally:
            store.close()


def test_realized_losses_update_risk_and_release_reservations(tmp_path) -> None:
    limits = RiskLimits(
        starting_equity=10000.0,
        max_consecutive_losses=2,
        max_open_positions=1,
        max_exposure_per_symbol_lots=0.01,
    )
    session, api, store = _session_fixture(tmp_path, risk_limits=limits)
    try:
        session.start()
        api.realized_pnl = -10.0
        first = session.poll_cycle(_test_signal("loss_one"))
        assert first.kind is Mt5SessionCycleKind.PROCESSED
        assert session.risk_engine.reserved_position_count == 0
        closed = session.poll_cycle(force_close=True)
        assert closed.kind is Mt5SessionCycleKind.POSITION_CLOSED
        assert session.risk_engine.consecutive_losses == 1
        assert session.risk_engine.daily_start_equity == 10000.0
        assert session.risk_engine.peak_equity == 10000.0
        assert session.risk_engine.reserved_position_count == 0

        api.entry_result = SimpleNamespace(
            retcode=api.TRADE_RETCODE_DONE, order=7003, deal=8003, volume=0.01, price=1.1002
        )
        api.close_result = SimpleNamespace(
            retcode=api.TRADE_RETCODE_DONE, order=7004, deal=8004, volume=0.01, price=1.1
        )
        assert (
            session.poll_cycle(_test_signal("loss_two", index=1)).kind
            is Mt5SessionCycleKind.PROCESSED
        )
        assert session.poll_cycle(force_close=True).kind is Mt5SessionCycleKind.POSITION_CLOSED
        assert session.risk_engine.consecutive_losses == 2
        blocked = session.poll_cycle(_test_signal("third_trade", index=2))
        assert blocked.kind is Mt5SessionCycleKind.FAILED
        assert blocked.reason == "kill_switch_active"
        assert api.order_send_count == 4
        assert session.risk_engine.reserved_position_count == 0
    finally:
        store.close()
