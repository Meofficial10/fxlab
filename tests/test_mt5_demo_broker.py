"""Offline tests for MT5 Demo Broker adapter (Phase 1C)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from fxlab.execution.broker import (
    AccountInfo,
    BrokerOrderRejected,
    BrokerPreSubmissionRejected,
    OrderRequest,
    OrderStatus,
    Tick,
)
from fxlab.execution.broker_capabilities import (
    CURRENT_ORDER_MANAGER_REQUIREMENTS,
    BrokerCapability,
    BrokerEnvironment,
    inspect_broker_capabilities,
)
from fxlab.execution.mt5_demo_broker import Mt5DemoBroker, _mt5_comment
from fxlab.execution.valuation import PipValuation

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


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
        self.populate_position_on_order_send = True

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
            if self.populate_position_on_order_send:
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


def _broker(api: FakeMt5Api | None = None, **kwargs) -> Mt5DemoBroker:
    selected = api or FakeMt5Api()
    mono_time = [0.0]

    def default_monotonic():
        mono_time[0] += 0.001
        return mono_time[0]

    return Mt5DemoBroker(
        api=selected,
        clock=kwargs.get("clock", lambda: NOW),
        monotonic=kwargs.get("monotonic", default_monotonic),
        sleeper=kwargs.get("sleeper", lambda _: None),
        **{k: v for k, v in kwargs.items() if k not in ("clock", "monotonic", "sleeper")},
    )


def test_mt5_demo_broker_descriptor_and_capabilities() -> None:
    broker = _broker()
    descriptor = broker.broker_descriptor

    assert descriptor.broker_id == "mt5-pepperstone-demo"
    assert descriptor.environment == BrokerEnvironment.DEMO
    assert descriptor.deterministic is False
    assert BrokerCapability.MARKET_ORDERS in descriptor.capabilities
    assert BrokerCapability.NATIVE_SL_TP in descriptor.capabilities
    assert BrokerCapability.HEDGING in descriptor.capabilities
    assert BrokerCapability.CLIENT_ORDER_IDS not in descriptor.capabilities

    # Unadvertised CLIENT_ORDER_IDS correctly fails strict requirements
    check_strict = inspect_broker_capabilities(
        broker, CURRENT_ORDER_MANAGER_REQUIREMENTS, require_hedging=True
    )
    assert check_strict.compatible is False
    assert BrokerCapability.CLIENT_ORDER_IDS in check_strict.missing

    # Custom runtime requirements without CLIENT_ORDER_IDS succeed
    check_runtime = inspect_broker_capabilities(
        broker,
        frozenset({BrokerCapability.MARKET_ORDERS, BrokerCapability.NATIVE_SL_TP}),
        require_hedging=True,
    )
    assert check_runtime.compatible is True


@pytest.mark.parametrize("mode", [1, 2, 999, None])
def test_mt5_demo_broker_rejects_non_demo_account_on_connect(mode: object) -> None:
    api = FakeMt5Api()
    api.account.trade_mode = mode
    broker = _broker(api)

    with pytest.raises(RuntimeError, match="mt5_demo_account_required"):
        broker.connect()
    assert broker.is_connected() is False


def test_mt5_demo_broker_connect_and_disconnect() -> None:
    api = FakeMt5Api()
    broker = _broker(api)

    assert broker.is_connected() is False
    broker.connect()
    assert broker.is_connected() is True
    broker.disconnect()
    assert broker.is_connected() is False
    assert api.calls[-1] == "shutdown"


def test_mt5_demo_broker_market_data_subscription_and_tick_progression() -> None:
    broker = _broker()
    broker.connect()

    with pytest.raises(ValueError, match="unsupported_mt5_symbol"):
        broker.subscribe_market_data(["GBPUSD"])

    broker.subscribe_market_data(["EURUSD"])
    tick = broker.get_latest_tick("EURUSD")

    assert isinstance(tick, Tick)
    assert tick.symbol == "EURUSD"
    assert tick.bid == 1.10000
    assert tick.ask == 1.10020
    assert tick.mid == 1.10010


def test_mt5_demo_broker_get_latest_tick_fails_closed_when_feed_is_frozen() -> None:
    api = FakeMt5Api()
    api.auto_advance_tick = False
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    with pytest.raises(RuntimeError, match="mt5_smoke_quote_invalid"):
        broker.get_latest_tick("EURUSD")


def test_mt5_demo_broker_cached_fresh_tick_coherence() -> None:
    api = FakeMt5Api()
    mono_time = [100.0]

    def advancing_mono() -> float:
        return mono_time[0]

    broker = _broker(api, monotonic=advancing_mono)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    # First call polls and caches tick
    tick1 = broker.get_latest_tick("EURUSD")
    assert tick1 is not None
    assert api.tick_counter == 2  # initial tick + fresh tick queried

    # Second call within max_quote_age (e.g. +0.5s) returns cached tick without re-polling
    mono_time[0] += 0.5
    tick2 = broker.get_latest_tick("EURUSD")
    assert tick2 is tick1
    assert api.tick_counter == 2  # no new query

    # Third call after max_quote_age expired (+6.0s) polls for a new tick
    mono_time[0] += 6.0
    tick3 = broker.get_latest_tick("EURUSD")
    assert tick3 is not None
    assert tick3 is not tick1
    assert api.tick_counter == 4  # initial tick + fresh tick queried again


def test_mt5_demo_broker_account_info_and_pip_valuation() -> None:
    broker = _broker()
    broker.connect()

    account = broker.get_account_info()
    assert isinstance(account, AccountInfo)
    assert account.balance == 10000.0
    assert account.equity == 10000.0
    assert account.currency == "USD"
    assert account.open_positions == []

    valuation = broker.pip_valuation("EURUSD", "USD", NOW)
    assert isinstance(valuation, PipValuation)
    assert valuation.quote_currency_pip_amount_per_lot == 10.0
    assert valuation.pip_value_per_lot == 10.0


def test_mt5_demo_broker_order_submission_requires_minimum_volume_and_protective_sl() -> None:
    broker = _broker()
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    # Non-minimum volume rejected
    bad_vol = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.05,
        order_type="market",
        order_id="client-ord-1",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_smoke_volume_invalid"):
        broker.submit_order(bad_vol)

    # Missing SL rejected
    no_sl = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="client-ord-1",
        sl_price=None,
    )
    with pytest.raises(RuntimeError, match="mt5_smoke_sl_invalid"):
        broker.submit_order(no_sl)

    # Non-protective SL for BUY (SL >= ask) rejected
    bad_sl = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="client-ord-1",
        sl_price=1.10500,
    )
    with pytest.raises(RuntimeError, match="mt5_smoke_sl_invalid"):
        broker.submit_order(bad_sl)


def test_mt5_demo_broker_submit_order_and_close_position_lifecycle() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="fxlab-demo-test1",
        sl_price=1.09500,
    )
    broker_order_id = broker.submit_order(order)
    assert broker_order_id == "7001"
    assert api.order_send_count == 1

    # Position is tracked
    account = broker.get_account_info()
    assert len(account.open_positions) == 1
    pos = account.open_positions[0]
    assert pos.position_id == "9001"
    assert pos.symbol == "EURUSD"
    assert pos.side == 1
    assert pos.size == 0.01

    # Close position returns both close_order_id and close_deal_id
    close_order, close_deal = broker.close_position("9001")
    assert close_order == "7002"
    assert close_deal == "8002"
    assert api.order_send_count == 2

    # Account open positions flat
    account_after = broker.get_account_info()
    assert account_after.open_positions == []


def test_mt5_demo_broker_refuses_to_close_untracked_manual_position() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    # Position 9999 is not present on broker
    with pytest.raises(RuntimeError, match="mt5_close_untracked_position"):
        broker.close_position("9999")
    assert api.order_send_count == 0


def test_mt5_demo_broker_broker_rejection_raises_sanitized_exception() -> None:
    api = FakeMt5Api()
    api.entry_result = SimpleNamespace(retcode=api.TRADE_RETCODE_REJECT)
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="fxlab-demo-test2",
        sl_price=1.09500,
    )
    with pytest.raises(BrokerOrderRejected, match="mt5_entry_broker_rejected"):
        broker.submit_order(order)
    assert api.order_send_count == 1


def test_mt5_demo_broker_supports_reported_minimum_volume_other_than_0_01() -> None:
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

    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    # Hardcoded 0.01 rejected when broker reports 0.1
    bad_vol = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="client-ord-1",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_smoke_volume_invalid"):
        broker.submit_order(bad_vol)

    # Valid reported minimum 0.1 accepted
    valid_vol = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.1,
        order_type="market",
        order_id="client-ord-2",
        sl_price=1.09500,
    )
    order_id = broker.submit_order(valid_vol)
    assert order_id == "7001"


@pytest.mark.parametrize(
    ("vmin", "vstep", "vmax"),
    [
        (0.0, 0.01, 100.0),
        (-0.01, 0.01, 100.0),
        (0.01, 0.0, 100.0),
        (0.01, -0.01, 100.0),
        (100.0, 0.01, 10.0),
        (0.015, 0.01, 100.0),
        (None, 0.01, 100.0),
    ],
)
def test_mt5_demo_broker_rejects_malformed_volume_metadata(
    vmin: object, vstep: object, vmax: object
) -> None:
    api = FakeMt5Api()
    api.symbol.volume_min = vmin
    api.symbol.volume_step = vstep
    api.symbol.volume_max = vmax

    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="client-ord-bad-vol",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_smoke_volume_invalid"):
        broker.submit_order(order)


def test_mt5_demo_broker_authoritative_entry_correlation_rejects_ambiguity() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="client-ord-corr",
        sl_price=1.09500,
    )

    # Ambiguity case 1: Broker returned success but no matching position exists
    original_order_send = api.order_send

    def send_without_position(req: dict[str, object]) -> object:
        res = original_order_send(req)
        api.positions = []
        return res

    api.order_send = send_without_position
    with pytest.raises(RuntimeError, match="mt5_entry_history_order_missing"):
        broker.submit_order(order)


def test_mt5_demo_broker_rejects_dirty_symbol_state_on_entry() -> None:
    # Existing EURUSD position rejected without order_send
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
    broker1 = _broker(api1)
    broker1.connect()
    broker1.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="fxlab-demo-dirty-pos",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_state_not_clean"):
        broker1.submit_order(order)
    assert api1.order_send_count == 0

    # Existing EURUSD pending order rejected without order_send
    api2 = FakeMt5Api()
    api2.orders = [SimpleNamespace(ticket=6666, symbol="EURUSD")]
    broker2 = _broker(api2)
    broker2.connect()
    broker2.subscribe_market_data(["EURUSD"])

    with pytest.raises(RuntimeError, match="mt5_state_not_clean"):
        broker2.submit_order(order)
    assert api2.order_send_count == 0


def test_mt5_demo_broker_leaves_unrelated_positions_untouched() -> None:
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

    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="fxlab-demo-touch-test",
        sl_price=1.09500,
    )
    broker_order_id = broker.submit_order(order)
    assert broker_order_id == "7001"

    # Close the broker-opened position
    close_order, close_deal = broker.close_position("9001")
    assert close_order == "7002"
    assert close_deal == "8002"

    # Manual position 9999 is untouched
    remaining_tickets = [getattr(p, "ticket", None) for p in api.positions]
    assert 9999 in remaining_tickets
    assert 9001 not in remaining_tickets


@pytest.mark.parametrize(
    "tamper_field,tamper_val",
    [
        ("comment", "tampered-comment"),
        ("identifier", 99999),
        ("type", 1),  # SELL instead of BUY
        ("volume", 0.05),
        ("sl", 1.08000),
    ],
)
def test_mt5_demo_broker_close_fails_closed_on_property_mismatch_without_order_send(
    tamper_field: str, tamper_val: object
) -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="fxlab-demo-corr-test",
        sl_price=1.09500,
    )
    broker.submit_order(order)
    assert api.order_send_count == 1

    # Tamper with the broker position before close
    for p in api.positions:
        if p.ticket == 9001:
            setattr(p, tamper_field, tamper_val)

    with pytest.raises(RuntimeError, match="mt5_close_position_mismatch"):
        broker.close_position("9001")

    # Order send must NOT have been called for close
    assert api.order_send_count == 1


@pytest.mark.parametrize(
    "close_res",
    [
        SimpleNamespace(retcode=10009, order=None, deal=8002),
        SimpleNamespace(retcode=10009, order=7002, deal=None),
        SimpleNamespace(retcode=10009, order=0, deal=8002),
        SimpleNamespace(retcode=10009, order=7002, deal=0),
    ],
)
def test_mt5_demo_broker_close_requires_both_positive_order_and_deal_ids(close_res: object) -> None:
    api = FakeMt5Api()
    api.close_result = close_res
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="fxlab-demo-ids-test",
        sl_price=1.09500,
    )
    broker.submit_order(order)

    with pytest.raises(RuntimeError, match="mt5_close_missing_"):
        broker.close_position("9001")


def test_mt5_demo_broker_close_fails_closed_if_broker_shows_position_remaining() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="fxlab-demo-rem-test",
        sl_price=1.09500,
    )
    broker.submit_order(order)

    # Broker sends close order, but position is not removed from MT5
    def send_without_clearing(req: dict[str, object]) -> object:
        api.order_send_count += 1
        return api.close_result

    api.order_send = send_without_clearing
    with pytest.raises(RuntimeError, match="mt5_close_position_remaining"):
        broker.close_position("9001")


def test_mt5_demo_broker_long_order_id_comment_handling() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    long_order_id = "operator_demo_execution-EURUSD-M1-20260911T093000000000Z-LONG"
    assert len(long_order_id) > 31

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id=long_order_id,
        sl_price=1.09500,
    )
    broker_order_id = broker.submit_order(order)
    assert broker_order_id == "7001"

    # Verify that request sent to MT5 had a comment <= 27 chars
    sent_request = [c[1] for c in api.calls if isinstance(c, tuple) and c[0] == "order_send"][0]
    comment = sent_request["comment"]
    assert len(comment) <= 27
    assert comment.startswith(long_order_id[:15])

    # Position can be closed with the matching correlated comment
    close_order_id, close_deal_id = broker.close_position("9001")
    assert close_order_id == "7002"
    assert close_deal_id == "8002"


def test_mt5_comment_formatting() -> None:
    from fxlab.execution.mt5_demo_broker import _mt5_comment

    short_id = "short_id_123"
    assert _mt5_comment(short_id) == short_id

    exact_27 = "a" * 27
    assert _mt5_comment(exact_27) == exact_27

    long_id = "a" * 28
    formatted = _mt5_comment(long_id)
    assert len(formatted) <= 27
    assert formatted.startswith("a" * 15 + "_")



@pytest.mark.parametrize(
    ("setup_fn", "expected_reason"),
    [
        (lambda api: setattr(api.account, "trade_mode", 2), "mt5_demo_account_required"),
        (lambda api: setattr(api.terminal, "connected", False), "mt5_mutation_not_permitted"),
        (lambda api: setattr(api.account, "currency", "EUR"), "mt5_account_incompatible"),
        (
            lambda api: api.positions.append(SimpleNamespace(ticket=111, symbol="EURUSD")),
            "mt5_state_not_clean",
        ),
        (lambda api: setattr(api.symbol, "visible", False), "mt5_smoke_symbol_invalid"),
        (lambda api: setattr(api.symbol, "volume_min", 0.05), "mt5_smoke_volume_invalid"),
    ],
)
def test_mt5_demo_broker_pre_submission_validation_raises_explicit_rejection_without_order_send(
    setup_fn, expected_reason: str
) -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])
    setup_fn(api)

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-pre-sub-rej",
        sl_price=1.09500,
    )

    with pytest.raises(BrokerPreSubmissionRejected) as exc_info:
        broker.submit_order(order)

    assert exc_info.value.reason == expected_reason
    assert api.order_send_count == 0


def test_mt5_demo_broker_clean_state_with_empty_tuples_proceeds_to_order_send() -> None:
    api = FakeMt5Api()
    api.positions = []
    api.orders = []

    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-clean-empty-tuples",
        sl_price=1.09500,
    )
    order_id = broker.submit_order(order)
    assert order_id == "7001"
    assert api.order_send_count == 1


@pytest.mark.parametrize(
    ("bad_positions", "bad_orders"),
    [
        (lambda **q: None, lambda **q: ()),
        (lambda **q: (), lambda **q: None),
        (lambda **q: None, lambda **q: None),
    ],
)
def test_mt5_demo_broker_none_from_queries_fails_closed_as_state_query_failed(
    bad_positions, bad_orders
) -> None:
    api = FakeMt5Api()
    api.positions_get = bad_positions
    api.orders_get = bad_orders

    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-none-query-fails-closed",
        sl_price=1.09500,
    )
    with pytest.raises(BrokerPreSubmissionRejected) as exc_info:
        broker.submit_order(order)

    assert exc_info.value.reason == "mt5_state_query_failed"
    assert api.order_send_count == 0


def test_mt5_demo_broker_query_exception_in_clean_check_raises_state_query_failed() -> None:
    api = FakeMt5Api()

    def buggy_orders_get(**query: object) -> tuple[object, ...]:
        raise RuntimeError("network timeout during query")

    api.orders_get = buggy_orders_get
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-query-err",
        sl_price=1.09500,
    )
    with pytest.raises(BrokerPreSubmissionRejected) as exc_info:
        broker.submit_order(order)

    assert exc_info.value.reason == "mt5_state_query_failed"
    assert api.order_send_count == 0


def test_mt5_demo_broker_order_send_unavailable_raises_pre_mutation() -> None:
    api = FakeMt5Api()
    api.order_send = None  # type: ignore[assignment]
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-os-unavail",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_order_send_unavailable"):
        broker.submit_order(order)

    from fxlab.execution.broker import BrokerMutationPhase

    assert broker.mutation_phase == BrokerMutationPhase.PRE_MUTATION
    assert api.order_send_count == 0


def test_mt5_demo_broker_order_send_exception_retains_mutation_attempted() -> None:
    api = FakeMt5Api()

    def buggy_send(req):
        api.order_send_count += 1
        raise ConnectionResetError("socket dropped during order_send")

    api.order_send = buggy_send
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-os-exc",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_order_send_exception"):
        broker.submit_order(order)

    from fxlab.execution.broker import BrokerMutationPhase

    assert broker.mutation_phase == BrokerMutationPhase.MUTATION_ATTEMPTED
    assert api.order_send_count == 1


def test_mt5_demo_broker_order_send_none_retains_mutation_attempted() -> None:
    api = FakeMt5Api()
    api.entry_result = None  # type: ignore[assignment]
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-os-none",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_order_send_none"):
        broker.submit_order(order)

    from fxlab.execution.broker import BrokerMutationPhase

    assert broker.mutation_phase == BrokerMutationPhase.MUTATION_ATTEMPTED
    assert api.order_send_count == 1


def test_mt5_demo_broker_order_send_malformed_result_retains_mutation_attempted() -> None:
    api = FakeMt5Api()
    api.entry_result = SimpleNamespace(retcode=None)
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-os-malformed",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_order_send_malformed_result"):
        broker.submit_order(order)

    from fxlab.execution.broker import BrokerMutationPhase

    assert broker.mutation_phase == BrokerMutationPhase.MUTATION_ATTEMPTED
    assert api.order_send_count == 1


@pytest.mark.parametrize(
    ("order_val", "deal_val", "expected_err"),
    [
        (None, 8001, "mt5_entry_missing_order_id"),
        (7001, None, "mt5_entry_missing_deal_id"),
        (0, 8001, "mt5_entry_missing_order_id"),
        (7001, 0, "mt5_entry_missing_deal_id"),
    ],
)
def test_mt5_demo_broker_missing_order_or_deal_id_retains_mutation_attempted(
    order_val: object, deal_val: object, expected_err: str
) -> None:
    api = FakeMt5Api()
    api.entry_result = SimpleNamespace(
        retcode=api.TRADE_RETCODE_DONE,
        order=order_val,
        deal=deal_val,
    )
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-missing-id",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match=expected_err):
        broker.submit_order(order)

    from fxlab.execution.broker import BrokerMutationPhase

    assert broker.mutation_phase == BrokerMutationPhase.MUTATION_ATTEMPTED
    assert api.order_send_count == 1


def test_mt5_demo_broker_post_mutation_position_query_failed() -> None:
    api = FakeMt5Api()
    orig_pos_get = api.positions_get

    def fail_query_after_send(**q):
        if api.order_send_count > 0:
            return None
        return orig_pos_get(**q)

    api.positions_get = fail_query_after_send
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-pos-q-fail",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_position_query_failed"):
        broker.submit_order(order)

    from fxlab.execution.broker import BrokerMutationPhase

    assert broker.mutation_phase == BrokerMutationPhase.POST_MUTATION_RECONCILIATION
    assert api.order_send_count == 1


def test_mt5_demo_broker_post_mutation_multiple_correlated_positions() -> None:
    api = FakeMt5Api()
    orig_send = api.order_send

    def duplicate_position_send(req):
        res = orig_send(req)
        # Add duplicate identical position
        api.positions.append(
            SimpleNamespace(
                ticket=9002,
                identifier=res.order,
                symbol="EURUSD",
                type=api.POSITION_TYPE_BUY,
                magic=req["magic"],
                comment=req["comment"],
                volume=req["volume"],
                price_open=req["price"],
                sl=req["sl"],
                tp=req.get("tp", 0.0),
                profit=0.0,
                time=int(NOW.timestamp()),
            )
        )
        return res

    api.order_send = duplicate_position_send
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-pos-dup",
        sl_price=1.09500,
    )
    with pytest.raises(RuntimeError, match="mt5_position_correlation_multiple"):
        broker.submit_order(order)

    from fxlab.execution.broker import BrokerMutationPhase

    assert broker.mutation_phase == BrokerMutationPhase.POST_MUTATION_RECONCILIATION
    assert api.order_send_count == 1


def test_mt5_demo_broker_read_only_check_order_does_not_mutate_or_call_order_send() -> None:
    api = FakeMt5Api()
    api.order_check = lambda req: SimpleNamespace(retcode=0, comment="Done")  # type: ignore[attr-defined]

    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-check-order-diag",
        sl_price=1.09500,
    )

    result = broker.check_order(order)

    assert result["request_constructed"] is True
    assert result["symbol"] == "EURUSD"
    assert result["volume"] == 0.01
    assert result["order_type"] == "BUY"
    assert result["sl_present"] is True
    assert result["valid"] is True
    assert result["retcode"] == 0
    # ZERO order_send calls made
    assert api.order_send_count == 0


def test_mt5_demo_broker_protective_exit_sl_fires_before_explicit_close() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-sl-fired",
        sl_price=1.09500,
    )
    broker.submit_order(order)
    assert api.order_send_count == 1

    # Position is closed natively on broker by SL before FXLab calls close
    api.positions = []
    api.history_deals = [
        SimpleNamespace(
            ticket=297171018,
            order=7001,
            position_id=9001,
            entry=api.DEAL_ENTRY_IN,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            price=1.10020,
            comment="test-sl-fired",
            reason=api.DEAL_REASON_EXPERT,
        ),
        SimpleNamespace(
            ticket=297171020,
            order=7005,
            position_id=9001,
            entry=api.DEAL_ENTRY_OUT,
            type=api.ORDER_TYPE_SELL,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            price=1.09500,
            profit=-5.20,
            comment="[sl 1.09500]",
            reason=api.DEAL_REASON_SL,
        ),
    ]

    close_order_id, close_deal_id = broker.close_position("9001")
    assert close_order_id == "7005"
    assert close_deal_id == "297171020"
    # Protective exit path makes ZERO additional close order_send calls (exactly 1 from entry)
    assert api.order_send_count == 1

    status = broker.get_order_status("test-sl-fired")
    assert status.get("exit_reason") == "SL"


def test_mt5_demo_broker_protective_exit_tp_fires_before_explicit_close() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-tp-fired",
        sl_price=1.09500,
        tp_price=1.10500,
    )
    broker.submit_order(order)
    assert api.order_send_count == 1

    # Position closed natively by TP
    api.positions = []
    api.history_deals = [
        SimpleNamespace(
            ticket=297171018,
            order=7001,
            position_id=9001,
            entry=api.DEAL_ENTRY_IN,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            price=1.10020,
            comment="test-tp-fired",
            reason=api.DEAL_REASON_EXPERT,
        ),
        SimpleNamespace(
            ticket=297171021,
            order=7006,
            position_id=9001,
            entry=api.DEAL_ENTRY_OUT,
            type=api.ORDER_TYPE_SELL,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            price=1.10500,
            profit=4.80,
            comment="[tp 1.10500]",
            reason=api.DEAL_REASON_TP,
        ),
    ]

    close_order_id, close_deal_id = broker.close_position("9001")
    assert close_order_id == "7006"
    assert close_deal_id == "297171021"
    assert api.order_send_count == 1

    status = broker.get_order_status("test-tp-fired")
    assert status.get("exit_reason") == "TP"


@pytest.mark.parametrize(
    ("bad_deals", "expected_err"),
    [
        # Position disappears with no history
        ([], "mt5_close_history_missing"),
        # Wrong position_id in history
        (
            [
                SimpleNamespace(
                    ticket=297171020,
                    order=7005,
                    position_id=8888,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                    reason=4,
                    comment="[sl 1.09500]",
                )
            ],
            "mt5_close_history_missing",
        ),
        # Wrong magic in history
        (
            [
                SimpleNamespace(
                    ticket=297171020,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x999999,
                    volume=0.01,
                    reason=4,
                    comment="[sl 1.09500]",
                )
            ],
            "mt5_close_history_missing",
        ),
        # Wrong symbol in history
        (
            [
                SimpleNamespace(
                    ticket=297171020,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="GBPUSD",
                    magic=0x46584C42,
                    volume=0.01,
                    reason=4,
                    comment="[sl 1.09500]",
                )
            ],
            "mt5_close_history_missing",
        ),
        # Wrong volume (partial exit) in history -> volume mismatch
        (
            [
                SimpleNamespace(
                    ticket=297171020,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.005,
                    reason=4,
                    comment="[sl 1.09500]",
                )
            ],
            "mt5_close_history_volume_mismatch",
        ),
        # Two exit deals summing to full volume
        # -> rejected for Phase 1C (mt5_close_history_multiple)
        (
            [
                SimpleNamespace(
                    ticket=297171020,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.005,
                    reason=4,
                    comment="[sl 1.09500]",
                ),
                SimpleNamespace(
                    ticket=297171021,
                    order=7006,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.005,
                    reason=4,
                    comment="[sl 1.09500]",
                ),
            ],
            "mt5_close_history_multiple",
        ),
        # SL-looking comment but non-SL reason -> unsupported reason
        (
            [
                SimpleNamespace(
                    ticket=297171020,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                    reason=0,  # DEAL_REASON_CLIENT instead of DEAL_REASON_SL (4)
                    comment="[sl 1.09500]",
                )
            ],
            "mt5_close_history_unsupported_reason",
        ),
        # TP-looking comment but non-TP reason -> unsupported reason
        (
            [
                SimpleNamespace(
                    ticket=297171020,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                    reason=0,  # DEAL_REASON_CLIENT instead of DEAL_REASON_TP (5)
                    comment="[tp 1.10500]",
                )
            ],
            "mt5_close_history_unsupported_reason",
        ),
        # Missing order ID in history deal
        (
            [
                SimpleNamespace(
                    ticket=297171020,
                    order=None,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                    reason=4,
                    comment="[sl 1.09500]",
                )
            ],
            "mt5_close_missing_order_id",
        ),
        # Missing deal ticket in history deal
        (
            [
                SimpleNamespace(
                    ticket=None,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                    reason=4,
                    comment="[sl 1.09500]",
                )
            ],
            "mt5_close_missing_deal_id",
        ),
    ],
)
def test_mt5_demo_broker_protective_exit_anomalies_fail_closed(
    bad_deals: list[object], expected_err: str
) -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-anomaly",
        sl_price=1.09500,
    )
    broker.submit_order(order)

    # Position is absent from open positions
    api.positions = []
    api.history_deals = bad_deals

    with pytest.raises(RuntimeError, match=expected_err):
        broker.close_position("9001")

    # Order send not called on close (zero close order_send calls)
    assert api.order_send_count == 1


def test_mt5_demo_broker_history_query_failure_fails_closed() -> None:
    api = FakeMt5Api()
    api.history_deals_get = lambda **q: None  # type: ignore[assignment]
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-hist-fail",
        sl_price=1.09500,
    )
    broker.submit_order(order)
    api.positions = []

    with pytest.raises(RuntimeError, match="mt5_close_history_query_failed"):
        broker.close_position("9001")
    assert api.order_send_count == 1


def test_mt5_demo_broker_final_position_query_failure_fails_closed() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-final-query-fail",
        sl_price=1.09500,
    )
    broker.submit_order(order)

    # Disappeared on initial check, valid SL history deal
    api.positions = []
    api.history_deals = [
        SimpleNamespace(
            ticket=297171020,
            order=7005,
            position_id=9001,
            entry=api.DEAL_ENTRY_OUT,
            type=api.ORDER_TYPE_SELL,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            price=1.09500,
            profit=-5.20,
            comment="[sl 1.09500]",
            reason=api.DEAL_REASON_SL,
        )
    ]

    # Hook positions_get to fail on absence check
    query_count = [0]
    original_positions_get = api.positions_get

    def failing_positions_get(**query: object) -> object:
        query_count[0] += 1
        if query_count[0] > 1:
            return None  # Failure on final absence verification
        return original_positions_get(**query)

    api.positions_get = failing_positions_get  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="mt5_close_position_query_failed"):
        broker.close_position("9001")
    assert api.order_send_count == 1


def test_mt5_demo_broker_final_position_still_present_fails_closed() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-final-pos-present",
        sl_price=1.09500,
    )
    broker.submit_order(order)

    api.positions = []
    api.history_deals = [
        SimpleNamespace(
            ticket=297171020,
            order=7005,
            position_id=9001,
            entry=api.DEAL_ENTRY_OUT,
            type=api.ORDER_TYPE_SELL,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            price=1.09500,
            profit=-5.20,
            comment="[sl 1.09500]",
            reason=api.DEAL_REASON_SL,
        )
    ]

    query_count = [0]

    def residual_positions_get(**query: object) -> object:
        query_count[0] += 1
        if query_count[0] > 1:
            # Position reappears or remains on broker
            return (SimpleNamespace(ticket=9001, symbol="EURUSD"),)
        return ()

    api.positions_get = residual_positions_get  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="mt5_close_position_remaining"):
        broker.close_position("9001")
    assert api.order_send_count == 1


def test_submit_order_active_position_normal_path() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-normal-entry",
        sl_price=1.09500,
    )
    broker_order_id = broker.submit_order(order)
    assert broker_order_id == "7001"
    assert api.order_send_count == 1

    status = broker.get_order_status("test-normal-entry")
    assert status.get("status") == OrderStatus.FILLED
    assert status.get("position_id") == "9001"
    assert not status.get("closed_at_entry_reconciliation")


def test_submit_order_entry_reconciliation_exact_sl() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-entry-recon-sl",
        sl_price=1.09500,
    )

    # Position is already closed before positions_get runs
    api.populate_position_on_order_send = False
    api.positions = []
    api.history_orders = [
        SimpleNamespace(
            ticket=7001,
            position_id=9001,
            symbol="EURUSD",
            magic=0x46584C42,
            comment=_mt5_comment("test-entry-recon-sl"),
            volume_initial=0.01,
        )
    ]
    api.history_deals = [
        SimpleNamespace(
            ticket=8001,
            order=7001,
            position_id=9001,
            entry=api.DEAL_ENTRY_IN,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            type=api.POSITION_TYPE_BUY,
        ),
        SimpleNamespace(
            ticket=8005,
            order=7005,
            position_id=9001,
            entry=api.DEAL_ENTRY_OUT,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            reason=api.DEAL_REASON_SL,
        ),
    ]

    broker_order_id = broker.submit_order(order)
    assert broker_order_id == "7001"
    assert api.order_send_count == 1

    status = broker.get_order_status("test-entry-recon-sl")
    assert status.get("status") == OrderStatus.FILLED
    assert status.get("position_id") == "9001"
    assert status.get("closed_at_entry_reconciliation") is True
    assert status.get("exit_reason") == "SL"
    assert status.get("close_order_id") == "7005"
    assert status.get("close_deal_id") == "8005"


def test_submit_order_entry_reconciliation_exact_tp() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-entry-recon-tp",
        sl_price=1.09500,
        tp_price=1.10500,
    )

    api.populate_position_on_order_send = False
    api.positions = []
    api.history_orders = [
        SimpleNamespace(
            ticket=7001,
            position_id=9001,
            symbol="EURUSD",
            magic=0x46584C42,
            comment=_mt5_comment("test-entry-recon-tp"),
            volume_initial=0.01,
        )
    ]
    api.history_deals = [
        SimpleNamespace(
            ticket=8001,
            order=7001,
            position_id=9001,
            entry=api.DEAL_ENTRY_IN,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            type=api.POSITION_TYPE_BUY,
        ),
        SimpleNamespace(
            ticket=8006,
            order=7006,
            position_id=9001,
            entry=api.DEAL_ENTRY_OUT,
            symbol="EURUSD",
            magic=0x46584C42,
            volume=0.01,
            reason=api.DEAL_REASON_TP,
        ),
    ]

    broker_order_id = broker.submit_order(order)
    assert broker_order_id == "7001"
    assert api.order_send_count == 1

    status = broker.get_order_status("test-entry-recon-tp")
    assert status.get("status") == OrderStatus.FILLED
    assert status.get("closed_at_entry_reconciliation") is True
    assert status.get("exit_reason") == "TP"
    assert status.get("close_order_id") == "7006"
    assert status.get("close_deal_id") == "8006"


@pytest.mark.parametrize(
    ("hist_orders", "hist_deals", "expected_err"),
    [
        # Absent position with no history
        ([], [], "mt5_entry_history_order_missing"),
        # Wrong entry order ID
        (
            [
                SimpleNamespace(
                    ticket=9999,
                    position_id=9001,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    comment="comment",
                    volume_initial=0.01,
                )
            ],
            [],
            "mt5_entry_history_order_missing",
        ),
        # Wrong symbol in order
        (
            [
                SimpleNamespace(
                    ticket=7001,
                    position_id=9001,
                    symbol="GBPUSD",
                    magic=0x46584C42,
                    comment=_mt5_comment("test-recon-fail"),
                    volume_initial=0.01,
                )
            ],
            [],
            "mt5_entry_history_order_missing",
        ),
        # Wrong magic in order
        (
            [
                SimpleNamespace(
                    ticket=7001,
                    position_id=9001,
                    symbol="EURUSD",
                    magic=0x999999,
                    comment=_mt5_comment("test-recon-fail"),
                    volume_initial=0.01,
                )
            ],
            [],
            "mt5_entry_history_order_missing",
        ),
        # Wrong volume in order
        (
            [
                SimpleNamespace(
                    ticket=7001,
                    position_id=9001,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    comment=_mt5_comment("test-recon-fail"),
                    volume_initial=0.05,
                )
            ],
            [],
            "mt5_entry_history_order_missing",
        ),
        # Wrong entry deal ID
        (
            [
                SimpleNamespace(
                    ticket=7001,
                    position_id=9001,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    comment=_mt5_comment("test-recon-fail"),
                    volume_initial=0.01,
                )
            ],
            [
                SimpleNamespace(
                    ticket=9999,  # Expected 8001
                    order=7001,
                    position_id=9001,
                    entry=0,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                )
            ],
            "mt5_entry_history_in_deal_mismatch",
        ),
        # Wrong position_id in deal
        (
            [
                SimpleNamespace(
                    ticket=7001,
                    position_id=9001,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    comment=_mt5_comment("test-recon-fail"),
                    volume_initial=0.01,
                )
            ],
            [
                SimpleNamespace(
                    ticket=8001,
                    order=7001,
                    position_id=8888,
                    entry=0,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                )
            ],
            "mt5_entry_history_in_deal_mismatch",
        ),
        # Multiple exit deals
        (
            [
                SimpleNamespace(
                    ticket=7001,
                    position_id=9001,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    comment=_mt5_comment("test-recon-fail"),
                    volume_initial=0.01,
                )
            ],
            [
                SimpleNamespace(
                    ticket=8001,
                    order=7001,
                    position_id=9001,
                    entry=0,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                ),
                SimpleNamespace(
                    ticket=8005,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.005,
                    reason=4,
                ),
                SimpleNamespace(
                    ticket=8006,
                    order=7006,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.005,
                    reason=4,
                ),
            ],
            "mt5_entry_history_multiple_out_deals",
        ),
        # Partial volume exit deal
        (
            [
                SimpleNamespace(
                    ticket=7001,
                    position_id=9001,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    comment=_mt5_comment("test-recon-fail"),
                    volume_initial=0.01,
                )
            ],
            [
                SimpleNamespace(
                    ticket=8001,
                    order=7001,
                    position_id=9001,
                    entry=0,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                ),
                SimpleNamespace(
                    ticket=8005,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.005,
                    reason=4,
                ),
            ],
            "mt5_entry_history_volume_mismatch",
        ),
        # Unsupported exit reason (e.g. 0)
        (
            [
                SimpleNamespace(
                    ticket=7001,
                    position_id=9001,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    comment=_mt5_comment("test-recon-fail"),
                    volume_initial=0.01,
                )
            ],
            [
                SimpleNamespace(
                    ticket=8001,
                    order=7001,
                    position_id=9001,
                    entry=0,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                ),
                SimpleNamespace(
                    ticket=8005,
                    order=7005,
                    position_id=9001,
                    entry=1,
                    symbol="EURUSD",
                    magic=0x46584C42,
                    volume=0.01,
                    reason=0,
                ),
            ],
            "mt5_entry_history_unsupported_reason",
        ),
    ],
)
def test_submit_order_entry_reconciliation_anomalies_fail_closed(
    hist_orders: list[object], hist_deals: list[object], expected_err: str
) -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-recon-fail",
        sl_price=1.09500,
    )
    api.populate_position_on_order_send = False
    api.positions = []
    api.history_orders = hist_orders
    api.history_deals = hist_deals

    with pytest.raises(RuntimeError, match=expected_err):
        broker.submit_order(order)
    assert api.order_send_count == 1


def test_monetary_risk_derives_stop_distance_deterministically() -> None:
    from fxlab.execution.mt5_demo_broker import _protective_stop_buy, _protective_stop_sell

    meta = SimpleNamespace(
        point=0.00001, digits=5, trade_stops_level=0, trade_contract_size=100000.0
    )
    bid = 1.15000
    ask = 1.15020  # 20 points spread

    # BUY: Expected entry is ASK (1.15020).
    # $1.00 loss on 0.01 lot -> 0.00100 distance -> SL = 1.14920
    sl_buy = _protective_stop_buy(meta, bid, ask, max_loss_usd=1.0, volume=0.01)
    assert sl_buy == 1.14920
    # Modeled price loss from entry (ASK) = 1.15020 - 1.14920 = 0.00100 ($1.00)
    assert (ask - sl_buy) * 100000.0 * 0.01 == pytest.approx(1.00)

    # SELL: Expected entry is BID (1.15000).
    # $1.00 loss on 0.01 lot -> 0.00100 distance -> SL = 1.15100
    sl_sell = _protective_stop_sell(meta, bid, ask, max_loss_usd=1.0, volume=0.01)
    assert sl_sell == 1.15100
    # Modeled price loss from entry (BID) = 1.15100 - 1.15000 = 0.00100 ($1.00)
    assert (sl_sell - bid) * 100000.0 * 0.01 == pytest.approx(1.00)


def test_monetary_risk_reads_contract_size_from_metadata() -> None:
    from fxlab.execution.mt5_demo_broker import _protective_stop_buy

    # Custom contract size of 50,000 -> double the price distance for the same dollar loss
    meta_custom = SimpleNamespace(
        point=0.00001, digits=5, trade_stops_level=0, trade_contract_size=50000.0
    )
    bid = 1.15000
    ask = 1.15020

    # $1.00 loss on 0.01 lot with 50,000 contract size -> offset = 1.0 / (50000 * 0.01) = 0.00200
    # BUY entry at ASK (1.15020) - 0.00200 = 1.14820
    sl_buy = _protective_stop_buy(meta_custom, bid, ask, max_loss_usd=1.0, volume=0.01)
    assert sl_buy == 1.14820


def test_monetary_risk_respects_spread_plus_broker_minimum() -> None:
    from fxlab.execution.mt5_demo_broker import _protective_stop_buy, _protective_stop_sell

    # spread = 20 points (0.00020), trade_stops_level = 20
    # (min distance from trigger side is 21 points = 0.00021)
    # Total required budget on 0.01 lot = (0.00020 + 0.00021) * 1000 = $0.41
    meta = SimpleNamespace(
        point=0.00001, digits=5, trade_stops_level=20, trade_contract_size=100000.0
    )
    bid = 1.15000
    ask = 1.15020

    # Risk of $0.50 (> $0.41 minimum) -> target 0.00050 offset from ASK -> SL = 1.14970
    # Distance from BID = 1.15000 - 1.14970 = 0.00030 (30 points > 21 points level) -> legal
    sl_buy = _protective_stop_buy(meta, bid, ask, max_loss_usd=0.50, volume=0.01)
    assert sl_buy == 1.14970

    # SELL: entry at BID (1.15000) + 0.00050 -> SL = 1.15050
    # Distance from ASK = 1.15050 - 1.15020 = 0.00030 (30 points > 21 points level) -> legal
    sl_sell = _protective_stop_sell(meta, bid, ask, max_loss_usd=0.50, volume=0.01)
    assert sl_sell == 1.15050


def test_monetary_risk_too_small_for_spread_and_broker_minimum_rejected() -> None:
    from fxlab.execution.broker import BrokerPreSubmissionRejected
    from fxlab.execution.mt5_demo_broker import _protective_stop_buy, _protective_stop_sell

    meta = SimpleNamespace(
        point=0.00001, digits=5, trade_stops_level=20, trade_contract_size=100000.0
    )
    bid = 1.15000
    ask = 1.15020  # spread = 0.00020 ($0.20), min stop = 0.00021 ($0.21), min total = $0.41

    # max_loss_usd = 0.30 (< $0.41 needed) -> rejected before mutation
    with pytest.raises(BrokerPreSubmissionRejected, match="mt5_smoke_risk_budget_too_small"):
        _protective_stop_buy(meta, bid, ask, max_loss_usd=0.30, volume=0.01)

    with pytest.raises(BrokerPreSubmissionRejected, match="mt5_smoke_risk_budget_too_small"):
        _protective_stop_sell(meta, bid, ask, max_loss_usd=0.30, volume=0.01)


def test_rounding_quantizes_toward_entry_never_exceeding_budget() -> None:
    from fxlab.execution.mt5_demo_broker import _protective_stop_buy, _protective_stop_sell

    meta = SimpleNamespace(
        point=0.00001, digits=5, trade_stops_level=0, trade_contract_size=100000.0
    )
    bid = 1.15000
    ask = 1.15020

    # Non-round risk of $1.003 on 0.01 lot -> raw distance = 0.001003
    # BUY: ask (1.15020) - 0.001003 = 1.149197.
    # Quantizing toward entry (ROUND_CEILING) -> 1.14920
    sl_buy = _protective_stop_buy(meta, bid, ask, max_loss_usd=1.003, volume=0.01)
    assert sl_buy == 1.14920
    modeled_loss_buy = (ask - sl_buy) * 100000.0 * 0.01
    assert modeled_loss_buy <= 1.003

    # SELL: bid (1.15000) + 0.001003 = 1.151003.
    # Quantizing toward entry (ROUND_FLOOR) -> 1.15100
    sl_sell = _protective_stop_sell(meta, bid, ask, max_loss_usd=1.003, volume=0.01)
    assert sl_sell == 1.15100
    modeled_loss_sell = (sl_sell - bid) * 100000.0 * 0.01
    assert modeled_loss_sell <= 1.003


def test_exact_same_sl_reaches_risk_engine_and_broker_no_widening() -> None:
    from fxlab.execution.broker_capabilities import BrokerCapability
    from fxlab.execution.mt5_demo_broker import _MT5_DEMO_RESOLVER, _protective_stop_buy
    from fxlab.execution.order_manager import ExecutionIntent, ExecutionResultKind, OrderManager
    from fxlab.execution.signal_engine import SignalEvent
    from fxlab.risk.engine import RiskEngine, RiskLimits

    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    meta = api.symbol_info("EURUSD")
    tick = broker.get_latest_tick("EURUSD")
    assert tick is not None

    computed_sl = _protective_stop_buy(
        meta, tick.bid, tick.ask, max_loss_usd=1.0, volume=0.01
    )
    signal = SignalEvent(
        setup_name="test_risk_sl",
        symbol="EURUSD",
        timeframe="M1",
        side=1,
        signal_time=tick.timestamp,
        signal_bar_index=0,
    )
    intent = ExecutionIntent(signal=signal, sl_price=computed_sl)

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
            {BrokerCapability.MARKET_ORDERS, BrokerCapability.NATIVE_SL_TP}
        ),
    )

    result = order_manager.submit(intent, current_time=tick.timestamp)
    assert result.kind == ExecutionResultKind.SUBMITTED
    assert result.risk_decision is not None
    # 1. RiskEngine entry price == executable ASK for BUY
    assert result.risk_decision.entry_price == tick.ask
    # 2. RiskEngine modeled stop loss == computed_sl
    assert result.risk_decision.sl_price == computed_sl
    # 3. RiskEngine modeled monetary risk <= explicit max_loss_usd
    assert result.risk_decision.modeled_monetary_risk <= 1.0 + 1e-9

    # 4. Broker order_send request payload received EXACT same sl_price (no widening)
    send_calls = [c[1] for c in api.calls if isinstance(c, tuple) and c[0] == "order_send"]
    assert len(send_calls) == 1
    assert send_calls[0]["sl"] == float(computed_sl)

    # 5. Position in broker matches exact sl
    pos = api.positions[0]
    assert pos.sl == float(computed_sl)


def test_normal_explicit_close_causes_exactly_one_close_order_send() -> None:
    api = FakeMt5Api()
    broker = _broker(api)
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])

    order = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.01,
        order_type="market",
        order_id="test-explicit-close",
        sl_price=1.09500,
    )
    broker.submit_order(order)
    assert api.order_send_count == 1

    # Position is still active in open positions
    close_order_id, close_deal_id = broker.close_position("9001")
    assert close_order_id == "7002"
    assert close_deal_id == "8002"
    # Exactly one additional order_send was performed (total 2)
    assert api.order_send_count == 2
