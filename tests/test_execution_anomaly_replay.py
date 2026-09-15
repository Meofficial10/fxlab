"""Deterministic offline Execution Anomaly Replay V1A test suite.

Tests existing FXLab production execution, risk, and broker layers against
high-value anomaly scenarios without live MT5 or external network access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from fxlab.execution.broker import (
    BrokerOrderRejected,
    BrokerPreSubmissionRejected,
    OrderRequest,
)
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import EventLedger
from fxlab.execution.mt5_demo_broker import MT5_DEMO_MAGIC, MT5_DEMO_SYMBOL, Mt5DemoBroker
from fxlab.execution.mt5_demo_session import (
    Mt5DemoSession,
    Mt5SessionCycleKind,
)
from fxlab.execution.mt5_demo_soak import (
    MT5_DEMO_SOAK_CONFIRMATION,
    Mt5DemoSoakConfig,
    _issue_soak_execution_permit,
)
from fxlab.execution.signal_engine import SignalEvent

NOW = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)


class ReplayFakeMt5Api:
    """Deterministic fake MT5 API for offline execution anomaly replay."""

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
    DEAL_REASON_EXPERT = 3
    DEAL_REASON_SL = 4
    DEAL_REASON_TP = 5
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REJECT = 10006

    def __init__(self, clock=None) -> None:
        self.calls: list[Any] = []
        self.clock = clock or (lambda: NOW)
        self.terminal = SimpleNamespace(connected=True, trade_allowed=True, company="Pepperstone")
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
            name=MT5_DEMO_SYMBOL,
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
        self.custom_ticks: list[Any] | None = None
        self.positions: list[Any] = []
        self.orders: list[Any] = []
        self.history_deals: list[Any] = []
        self.history_orders: list[Any] = []
        self.entry_result: Any = SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=7001,
            deal=8001,
            volume=0.01,
            price=1.10020,
        )
        self.close_result: Any = SimpleNamespace(
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

    def terminal_info(self) -> Any:
        self.calls.append("terminal_info")
        return self.terminal

    def account_info(self) -> Any:
        self.calls.append("account_info")
        return self.account

    def symbol_info(self, symbol: str) -> Any:
        self.calls.append(("symbol_info", symbol))
        return self.symbol

    def symbol_info_tick(self, symbol: str) -> Any:
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

    def positions_get(self, **query: Any) -> tuple[Any, ...] | None:
        self.calls.append(("positions_get", tuple(sorted(query.items()))))
        if "ticket" in query:
            return tuple(item for item in self.positions if item.ticket == query["ticket"])
        if "symbol" in query and query["symbol"] is not None:
            return tuple(item for item in self.positions if item.symbol == query["symbol"])
        return tuple(self.positions)

    def orders_get(self, **query: Any) -> tuple[Any, ...] | None:
        self.calls.append(("orders_get", tuple(sorted(query.items()))))
        if "ticket" in query:
            return tuple(item for item in self.orders if item.ticket == query["ticket"])
        if "symbol" in query and query["symbol"] is not None:
            return tuple(item for item in self.orders if item.symbol == query["symbol"])
        return tuple(self.orders)

    def history_deals_get(self, **query: Any) -> tuple[Any, ...] | None:
        self.calls.append(("history_deals_get", tuple(sorted(query.items()))))
        if "position" in query:
            return tuple(
                d
                for d in self.history_deals
                if getattr(d, "position_id", None) == query["position"]
            )
        return tuple(self.history_deals)

    def history_orders_get(self, **query: Any) -> tuple[Any, ...] | None:
        self.calls.append(("history_orders_get", tuple(sorted(query.items()))))
        return tuple(self.history_orders)

    def order_send(self, request: dict[str, Any]) -> Any:
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
                    symbol=MT5_DEMO_SYMBOL,
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
                    symbol=MT5_DEMO_SYMBOL,
                    magic=MT5_DEMO_MAGIC,
                    volume=request["volume"],
                    profit=self.realized_pnl,
                    reason=self.DEAL_REASON_CLIENT,
                )
            )
        return result


def _make_broker(
    api: ReplayFakeMt5Api,
    mono_ref: list[float] | None = None,
    clock_fn: Any = None,
    max_quote_age_s: float = 5.0,
) -> Mt5DemoBroker:
    clock = clock_fn or (lambda: NOW)
    mono_time = mono_ref if mono_ref is not None else [100.0]

    def monotonic() -> float:
        mono_time[0] += 0.001
        return mono_time[0]

    broker = Mt5DemoBroker(
        api=api,
        clock=clock,
        monotonic=monotonic,
        sleeper=lambda _: None,
        max_quote_age=timedelta(seconds=max_quote_age_s),
    )
    broker.connect()
    broker.subscribe_market_data([MT5_DEMO_SYMBOL])
    return broker


def _make_session(
    broker: Mt5DemoBroker,
    tmp_path: Any,
    clock_fn: Any = None,
) -> tuple[Mt5DemoSession, EventLedger]:
    clock = clock_fn or (lambda: NOW)
    store = SQLiteEventStore(tmp_path / "replay-session.sqlite", "replay-session")
    ledger = EventLedger(store.session_id, time_provider=clock, durable_store=store)
    config = Mt5DemoSoakConfig(
        confirmation=MT5_DEMO_SOAK_CONFIRMATION,
        max_loss_usd=1.0,
        max_entries=1,
        max_duration_seconds=10.0,
        drain_timeout_seconds=10.0,
        max_quote_age_seconds=5.0,
    )
    permit = _issue_soak_execution_permit(config)
    session = Mt5DemoSession(
        broker=broker,
        event_ledger=ledger,
        max_loss_usd=1.0,
        max_quote_age=timedelta(seconds=5.0),
        execution_permit=permit,
        clock=clock,
    )
    return session, ledger


# ---------------------------------------------------------------------------
# Scenario 1: Stale quote before entry
# ---------------------------------------------------------------------------
def test_replay_01_stale_quote_before_entry() -> None:
    """A stale quote exceeding max_quote_age on frozen feed must reject before order_send."""
    api = ReplayFakeMt5Api()
    mono_ref = [100.0]
    broker = _make_broker(api, mono_ref=mono_ref, max_quote_age_s=5.0)

    # Initial tick establishes baseline
    tick = broker.get_latest_tick(MT5_DEMO_SYMBOL)
    assert tick is not None

    # Freeze tick feed and advance monotonic time beyond max_quote_age (+6s)
    api.auto_advance_tick = False
    mono_ref[0] += 6.0

    order = OrderRequest(
        order_id="REPLAY-STALE-001",
        symbol=MT5_DEMO_SYMBOL,
        side=1,
        order_type="market",
        size=0.01,
        sl_price=1.09500,
    )

    with pytest.raises(BrokerPreSubmissionRejected) as exc_info:
        broker.submit_order(order)
    assert "mt5_smoke_quote_invalid" in str(exc_info.value)
    assert api.order_send_count == 0
    exposure = broker.get_account_exposure()
    assert exposure.is_flat


# ---------------------------------------------------------------------------
# Scenario 2: Out-of-order execution quote / signal chronology
# ---------------------------------------------------------------------------
def test_replay_02_out_of_order_quote_chronology() -> None:
    """Quotes arriving with backwards/out-of-order time_msc must be rejected."""
    api = ReplayFakeMt5Api()
    base_msc = int(NOW.timestamp() * 1000)
    mono_ref = [100.0]
    # Inject a newer tick then an older tick
    api.custom_ticks = [
        SimpleNamespace(time_msc=base_msc + 2000, bid=1.10000, ask=1.10020),
        SimpleNamespace(time_msc=base_msc + 1000, bid=1.10000, ask=1.10020),  # Backwards
    ]
    broker = _make_broker(api, mono_ref=mono_ref)

    # Initial polling encounters backward timestamp and rejects immediately
    with pytest.raises(BrokerPreSubmissionRejected) as exc_info:
        broker.get_latest_tick(MT5_DEMO_SYMBOL)
    assert "mt5_smoke_quote_invalid" in str(exc_info.value)
    assert api.order_send_count == 0
    exposure = broker.get_account_exposure()
    assert exposure.is_flat


# ---------------------------------------------------------------------------
# Scenario 3: Duplicate logical submission
# ---------------------------------------------------------------------------
def test_replay_03_duplicate_logical_submission() -> None:
    """Submitting the same client order ID twice must reject the duplicate without mutation."""
    api = ReplayFakeMt5Api()
    broker = _make_broker(api)
    broker.get_latest_tick(MT5_DEMO_SYMBOL)

    order = OrderRequest(
        order_id="REPLAY-DUP-001",
        symbol=MT5_DEMO_SYMBOL,
        side=1,
        order_type="market",
        size=0.01,
        sl_price=1.09500,
    )

    # First submission succeeds
    broker_ord_id = broker.submit_order(order)
    assert broker_ord_id == "7001"
    assert api.order_send_count == 1

    # Second submission with open position / same state is blocked pre-submission
    broker.get_latest_tick(MT5_DEMO_SYMBOL)
    with pytest.raises(BrokerPreSubmissionRejected) as exc_info:
        broker.submit_order(order)
    assert "mt5_state_not_clean" in str(exc_info.value)
    # Mutation count MUST NOT increase
    assert api.order_send_count == 1
    exposure = broker.get_account_exposure()
    assert len(exposure.open_positions) == 1


# ---------------------------------------------------------------------------
# Scenario 4: Broker entry rejection
# ---------------------------------------------------------------------------
def test_replay_04_broker_entry_rejection() -> None:
    """Broker returning TRADE_RETCODE_REJECT must raise BrokerOrderRejected and leave flat."""
    api = ReplayFakeMt5Api()
    api.entry_result = SimpleNamespace(
        retcode=api.TRADE_RETCODE_REJECT,
        order=0,
        deal=0,
        comment="Market Closed",
    )
    broker = _make_broker(api)
    broker.get_latest_tick(MT5_DEMO_SYMBOL)

    order = OrderRequest(
        order_id="REPLAY-REJECT-001",
        symbol=MT5_DEMO_SYMBOL,
        side=1,
        order_type="market",
        size=0.01,
        sl_price=1.09500,
    )

    with pytest.raises(BrokerOrderRejected) as exc_info:
        broker.submit_order(order)
    assert "mt5_entry_broker_rejected" in str(exc_info.value)
    assert api.order_send_count == 1
    exposure = broker.get_account_exposure()
    assert exposure.is_flat


# ---------------------------------------------------------------------------
# Scenario 5: Ambiguous order_send failure / exception
# ---------------------------------------------------------------------------
def test_replay_05_ambiguous_order_send_exception() -> None:
    """An unhandled network exception during order_send must fail closed without retry."""
    api = ReplayFakeMt5Api()
    api.entry_result = ConnectionResetError("MT5 IPC pipe disconnected")
    broker = _make_broker(api)
    broker.get_latest_tick(MT5_DEMO_SYMBOL)

    order = OrderRequest(
        order_id="REPLAY-EXC-001",
        symbol=MT5_DEMO_SYMBOL,
        side=1,
        order_type="market",
        size=0.01,
        sl_price=1.09500,
    )

    with pytest.raises(RuntimeError) as exc_info:
        broker.submit_order(order)
    assert "mt5_order_send_exception" in str(exc_info.value)
    # Exactly one attempt made, no blind retry
    assert api.order_send_count == 1
    exposure = broker.get_account_exposure()
    assert exposure.is_flat


# ---------------------------------------------------------------------------
# Scenario 6: Delayed history / deal visibility
# ---------------------------------------------------------------------------
def test_replay_06_delayed_history_deal_visibility() -> None:
    """When order_send succeeds but position & history are delayed/missing, fail closed."""
    api = ReplayFakeMt5Api()
    # order_send returns DONE, but we suppress immediate position and history creation
    original_order_send = api.order_send

    def delayed_order_send(req: dict[str, Any]) -> Any:
        res = original_order_send(req)
        # Clear positions and history so query finds nothing
        api.positions = []
        api.history_orders = []
        api.history_deals = []
        return res

    api.order_send = delayed_order_send  # type: ignore[assignment]
    broker = _make_broker(api)
    broker.get_latest_tick(MT5_DEMO_SYMBOL)

    order = OrderRequest(
        order_id="REPLAY-DELAYED-001",
        symbol=MT5_DEMO_SYMBOL,
        side=1,
        order_type="market",
        size=0.01,
        sl_price=1.09500,
    )

    with pytest.raises(RuntimeError) as exc_info:
        broker.submit_order(order)
    assert "mt5_entry_history_order_missing" in str(exc_info.value)
    assert api.order_send_count == 1


# ---------------------------------------------------------------------------
# Scenario 7: Controlled-close rejection
# ---------------------------------------------------------------------------
def test_replay_07_controlled_close_rejection() -> None:
    """When close order_send returns REJECT, fail closed and retain position tracking."""
    api = ReplayFakeMt5Api()
    broker = _make_broker(api)
    broker.get_latest_tick(MT5_DEMO_SYMBOL)

    order = OrderRequest(
        order_id="REPLAY-CLOSE-REJ-001",
        symbol=MT5_DEMO_SYMBOL,
        side=1,
        order_type="market",
        size=0.01,
        sl_price=1.09500,
    )
    broker.submit_order(order)
    pos_id = str(api.ticket_seq)
    assert api.order_send_count == 1

    # Program close rejection
    api.close_result = SimpleNamespace(
        retcode=api.TRADE_RETCODE_REJECT,
        order=0,
        deal=0,
        comment="Off quotes",
    )

    with pytest.raises(BrokerOrderRejected) as exc_info:
        broker.close_position(pos_id)
    assert "mt5_close_broker_rejected" in str(exc_info.value)
    assert api.order_send_count == 2
    # Position remains tracked and open in broker
    exposure = broker.get_account_exposure()
    assert len(exposure.open_positions) == 1
    assert exposure.open_positions[0].position_id == pos_id


# ---------------------------------------------------------------------------
# Scenario 8: Duplicate close prevention
# ---------------------------------------------------------------------------
def test_replay_08_duplicate_close_prevention() -> None:
    """Closing an already-closed position must reject without emitting a second close order."""
    api = ReplayFakeMt5Api()
    broker = _make_broker(api)
    broker.get_latest_tick(MT5_DEMO_SYMBOL)

    order = OrderRequest(
        order_id="REPLAY-DUP-CLOSE-001",
        symbol=MT5_DEMO_SYMBOL,
        side=1,
        order_type="market",
        size=0.01,
        sl_price=1.09500,
    )
    broker.submit_order(order)
    pos_id = str(api.ticket_seq)
    assert api.order_send_count == 1

    # First close succeeds
    close_ord, close_deal = broker.close_position(pos_id)
    assert close_ord == "7002"
    assert close_deal == "8002"
    assert api.order_send_count == 2

    # Second close attempt must be rejected immediately as untracked/already closed
    with pytest.raises(RuntimeError) as exc_info:
        broker.close_position(pos_id)
    assert "mt5_close_untracked_position" in str(exc_info.value)
    # Crucial invariant: No second close order sent to broker
    assert api.order_send_count == 2
    exposure = broker.get_account_exposure()
    assert exposure.is_flat


# ---------------------------------------------------------------------------
# Scenario 9: Malformed / None exposure snapshot
# ---------------------------------------------------------------------------
def test_replay_09_malformed_none_exposure_snapshot() -> None:
    """When MT5 returns None or malformed objects for exposure, fail closed."""
    api = ReplayFakeMt5Api()
    broker = _make_broker(api)

    # Simulate account_info returning None
    api.account = None  # type: ignore[assignment]
    with pytest.raises(Exception) as exc_info:
        broker.get_account_exposure()
    err_str = str(exc_info.value)
    assert "mt5_demo_account_required" in err_str or "mt5_account_info_unavailable" in err_str

    # Restore account, simulate positions_get returning None
    api.account = SimpleNamespace(
        login=12345678,
        trade_mode=api.ACCOUNT_TRADE_MODE_DEMO,
        currency="USD",
        server="Pepperstone-Demo",
        company="Pepperstone Group Limited",
        trade_allowed=True,
        trade_expert=True,
        margin_mode=api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        balance=10000.0,
        equity=10000.0,
        margin=0.0,
        margin_free=10000.0,
    )
    api.positions_get = lambda **_: None  # type: ignore[assignment]
    with pytest.raises(RuntimeError) as exc_info2:
        broker.get_account_exposure()
    assert "mt5_position_query_failed" in str(exc_info2.value)


# ---------------------------------------------------------------------------
# Scenario 10: Unknown / pre-existing position must never be auto-closed
# ---------------------------------------------------------------------------
def test_replay_10_unknown_position_never_auto_closed() -> None:
    """An unknown/foreign position not tracked by this broker instance must not be closed."""
    api = ReplayFakeMt5Api()
    # Inject foreign position into MT5 terminal
    api.positions = [
        SimpleNamespace(
            ticket=55555,
            identifier=55555,
            symbol=MT5_DEMO_SYMBOL,
            type=api.POSITION_TYPE_BUY,
            magic=999999,  # Foreign magic
            comment="manual_trade",
            volume=0.10,
            price_open=1.10000,
            sl=1.09000,
            tp=0.0,
            profit=50.0,
            time=int(NOW.timestamp()),
        )
    ]
    broker = _make_broker(api)

    # Attempting to close foreign position via broker must fail closed
    with pytest.raises(RuntimeError) as exc_info:
        broker.close_position("55555")
    assert "mt5_close_untracked_position" in str(exc_info.value)
    # Zero orders sent to MT5
    assert api.order_send_count == 0
    # Position remains untouched in MT5
    assert len(api.positions) == 1
    assert api.positions[0].ticket == 55555


# ---------------------------------------------------------------------------
# Scenario 11: Owned position + unknown position ownership isolation
# ---------------------------------------------------------------------------
def test_replay_11_owned_and_unknown_position_isolation() -> None:
    """Closing an owned position must strictly isolate and leave foreign positions untouched."""
    api = ReplayFakeMt5Api()
    broker = _make_broker(api)
    broker.get_latest_tick(MT5_DEMO_SYMBOL)

    # Open owned position when state is clean
    order = OrderRequest(
        order_id="REPLAY-OWNED-001",
        symbol=MT5_DEMO_SYMBOL,
        side=1,
        order_type="market",
        size=0.01,
        sl_price=1.09500,
    )
    broker.submit_order(order)
    owned_pos_id = str(api.ticket_seq)
    assert len(api.positions) == 1

    # Now inject a foreign position concurrently appearing in terminal
    api.positions.append(
        SimpleNamespace(
            ticket=55555,
            identifier=55555,
            symbol=MT5_DEMO_SYMBOL,
            type=api.POSITION_TYPE_BUY,
            magic=999999,
            comment="manual_trade",
            volume=0.10,
            price_open=1.10000,
            sl=1.09000,
            tp=0.0,
            profit=50.0,
            time=int(NOW.timestamp()),
        )
    )
    assert len(api.positions) == 2

    # Close owned position
    broker.close_position(owned_pos_id)

    # Verify owned position is closed, but foreign position 55555 remains untouched
    assert len(api.positions) == 1
    assert api.positions[0].ticket == 55555
    assert api.positions[0].magic == 999999


# ---------------------------------------------------------------------------
# Scenario 12: Session end / drain with residual owned exposure
# ---------------------------------------------------------------------------
def test_replay_12_session_end_drain_with_residual_exposure(tmp_path: Any) -> None:
    """When a session stops with an unclosed owned position, preserve residual exposure."""
    api = ReplayFakeMt5Api()
    broker = _make_broker(api)
    session, ledger = _make_session(broker, tmp_path)

    # Start session
    session.start()

    # Open position in session via poll_cycle
    sig_factory = lambda tick: SignalEvent(  # noqa: E731
        setup_name="synthetic_soak",
        symbol=MT5_DEMO_SYMBOL,
        timeframe="M1",
        side=1,
        signal_time=NOW,
        signal_bar_index=0,
    )
    res = session.poll_cycle(
        synthetic_signal_factory=sig_factory,
        current_time=NOW,
    )
    assert res.kind == Mt5SessionCycleKind.PROCESSED
    assert session.active_position_id is not None
    assert len(api.positions) == 1

    # Close attempts fail on broker
    api.close_result = SimpleNamespace(
        retcode=api.TRADE_RETCODE_REJECT,
        order=0,
        deal=0,
    )

    # Stop session - active position cannot be closed
    session.stop(current_time=NOW + timedelta(seconds=1.0))

    # Invariant: session must NOT claim clean completion; active_position_id remains set
    assert session.active_position_id is not None
    assert len(api.positions) == 1
    broker.connect()
    exposure = broker.get_account_exposure()
    assert not exposure.is_flat
    assert len(exposure.open_positions) == 1
