"""Focused tests for the constrained OANDA v20 practice adapter."""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Mapping
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError

import pytest

from fxlab.execution.broker import BrokerAdapter, BrokerOrderRejected, OrderRequest
from fxlab.execution.broker_capabilities import (
    BrokerCapability,
    BrokerCapabilityProvider,
    BrokerEnvironment,
)
from fxlab.execution.oanda_demo_broker import (
    OANDA_PRACTICE_AUTHORITY,
    OandaDemoBroker,
    OandaHttpTransport,
)


class FakeTransport:
    def __init__(
        self,
        responses: list[object],
        *,
        authority: str = OANDA_PRACTICE_AUTHORITY,
    ) -> None:
        self.authority = authority
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, path: str, **kwargs: object) -> object:
        self.calls.append({"method": method, "path": path, **kwargs})
        return self.responses.pop(0)


def account_payload(**changes: object) -> Mapping[str, object]:
    account: dict[str, object] = {
        "id": "practice-account",
        "currency": "USD",
        "hedgingEnabled": True,
        "tradingDisabled": False,
        "balance": "10000.00",
        "NAV": "10000.00",
        "marginUsed": "0.00",
        "marginAvailable": "10000.00",
        "openTradeCount": 0,
        "trades": [],
    }
    account.update(changes)
    return {"account": account}


def instrument_payload() -> Mapping[str, object]:
    return {
        "instruments": [
            {
                "name": native,
                "type": "CURRENCY",
                "pipLocation": -4,
                "tradeUnitsPrecision": 0,
                "minimumTradeSize": "1",
                "maximumOrderUnits": "100000000",
                "displayPrecision": 5,
            }
            for native in ("EUR_USD", "GBP_USD", "AUD_USD", "NZD_USD")
        ]
    }


def response(status: int, payload: Mapping[str, object]) -> object:
    module = importlib.import_module("fxlab.execution.oanda_demo_broker")
    response_type = getattr(module, "OandaResponse", None)
    assert response_type is not None
    return response_type(status, payload)


def connected_broker(*extra: object) -> tuple[OandaDemoBroker, FakeTransport]:
    transport = FakeTransport(
        [response(200, account_payload()), response(200, instrument_payload()), *extra]
    )
    broker = OandaDemoBroker("practice-account", "private-token", transport=transport)
    broker.connect()
    return broker, transport


def test_oanda_descriptor_is_demo_only_and_minimal() -> None:
    spec = importlib.util.find_spec("fxlab.execution.oanda_demo_broker")
    assert spec is not None
    module = importlib.import_module("fxlab.execution.oanda_demo_broker")

    broker = module.OandaDemoBroker("practice-account", "private-token")

    descriptor = broker.broker_descriptor
    assert descriptor.broker_id == "oanda-v20"
    assert descriptor.implementation_version == "2"
    assert descriptor.environment is BrokerEnvironment.DEMO
    assert descriptor.deterministic is False
    assert descriptor.capabilities == frozenset(
        {
            BrokerCapability.MARKET_ORDERS,
            BrokerCapability.NATIVE_SL_TP,
            BrokerCapability.HEDGING,
            BrokerCapability.CLIENT_ORDER_IDS,
        }
    )
    assert len(descriptor.fingerprint) == 64
    assert broker.broker_descriptor is descriptor


def test_oanda_adapter_structurally_conforms_to_broker_contracts() -> None:
    module = importlib.import_module("fxlab.execution.oanda_demo_broker")
    broker = module.OandaDemoBroker("practice-account", "private-token")
    assert isinstance(broker, BrokerAdapter)
    assert isinstance(broker, BrokerCapabilityProvider)


def test_transport_response_is_an_immutable_primitive_envelope() -> None:
    item = response(200, {"ok": True})
    assert item.status == 200
    assert item.payload == {"ok": True}


def test_connect_validates_complete_practice_account_before_marking_connected() -> None:
    broker, transport = connected_broker()
    assert broker.is_connected()
    assert [call["path"] for call in transport.calls] == [
        "/v3/accounts/practice-account",
        "/v3/accounts/practice-account/instruments",
    ]


@pytest.mark.parametrize("symbol", ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"])
def test_verified_practice_metadata_provides_explicit_usd_pip_valuation(
    symbol: str,
) -> None:
    broker, transport = connected_broker()
    valuation = broker.pip_valuation(
        symbol, "USD", datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
    )
    assert valuation.canonical_symbol == symbol
    assert valuation.account_currency == "USD"
    assert valuation.quote_currency_pip_amount_per_lot == pytest.approx(
        100_000 * 0.0001
    )
    assert valuation.pip_value_per_lot == pytest.approx(100_000 * 0.0001)
    assert valuation.route_identity == "quote-equals-account"
    assert len(transport.calls) == 2

    with pytest.raises(RuntimeError, match="not_connected"):
        OandaDemoBroker("practice-account", "private-token").pip_valuation(
            symbol, "USD", datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
        )


@pytest.mark.parametrize(
    "authority",
    ["https://api-fxtrade.oanda.com", "https://attacker.invalid"],
)
def test_non_practice_authority_is_rejected_before_transport_use(authority: str) -> None:
    transport = FakeTransport([], authority=authority)
    with pytest.raises(ValueError, match="authority"):
        OandaDemoBroker("practice-account", "private-token", transport=transport)
    assert transport.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "other-account"},
        {"currency": "EUR"},
        {"hedgingEnabled": False},
        {"tradingDisabled": True},
        {"mt4AccountID": 12345},
    ],
)
def test_failed_account_preflight_leaves_adapter_disconnected(
    changes: dict[str, object],
) -> None:
    transport = FakeTransport([response(200, account_payload(**changes))])
    broker = OandaDemoBroker("practice-account", "private-token", transport=transport)
    with pytest.raises(RuntimeError, match="account_incompatible"):
        broker.connect()
    assert not broker.is_connected()
    assert len(transport.calls) == 1


def test_adapter_repr_and_descriptor_do_not_expose_account_or_token() -> None:
    broker = OandaDemoBroker("secret-account-7", "secret-token-9")
    visible = f"{broker!r} {broker.broker_descriptor!r}"
    assert "secret-account-7" not in visible
    assert "secret-token-9" not in visible


def price_payload(**changes: object) -> Mapping[str, object]:
    price: dict[str, object] = {
        "instrument": "EUR_USD",
        "time": "2026-08-27T10:00:00Z",
        "tradeable": True,
        "bids": [{"price": "1.1000"}],
        "asks": [{"price": "1.1002"}],
    }
    price.update(changes)
    return {"prices": [price]}


def test_latest_tick_maps_exact_fresh_practice_bid_and_ask() -> None:
    broker, transport = connected_broker(response(200, price_payload()))
    broker.clock = lambda: datetime(2026, 8, 27, 10, 0, 2, tzinfo=UTC)
    broker.subscribe_market_data(["EURUSD"])

    tick = broker.get_latest_tick("EURUSD")

    assert tick is not None
    assert tick.symbol == "EURUSD"
    assert tick.timestamp == datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
    assert tick.bid == 1.1
    assert tick.ask == 1.1002
    assert tick.mid == pytest.approx(1.1001)
    assert transport.calls[-1]["query"] == {"instruments": "EUR_USD"}
    assert len(transport.calls) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"time": "2026-08-27T09:59:54Z"},
        {"time": "2026-08-27T10:00:03Z"},
        {"instrument": "GBP_USD"},
        {"tradeable": False},
        {"bids": [{"price": "nan"}]},
        {"asks": [{"price": "0"}]},
        {"bids": [{"price": "1.2"}], "asks": [{"price": "1.1"}]},
    ],
)
def test_invalid_or_stale_quote_is_rejected(changes: dict[str, object]) -> None:
    broker, _ = connected_broker(response(200, price_payload(**changes)))
    broker.clock = lambda: datetime(2026, 8, 27, 10, 0, 2, tzinfo=UTC)
    broker.subscribe_market_data(["EURUSD"])
    with pytest.raises(RuntimeError, match="quote_invalid"):
        broker.get_latest_tick("EURUSD")


def test_unsupported_quote_symbol_is_rejected_without_pricing_request() -> None:
    broker, transport = connected_broker()
    with pytest.raises(ValueError, match="unsupported_oanda_symbol"):
        broker.get_latest_tick("USDJPY")
    assert len(transport.calls) == 2


def trade_payload(**changes: object) -> dict[str, object]:
    trade: dict[str, object] = {
        "id": "trade-77",
        "instrument": "EUR_USD",
        "currentUnits": "-2500",
        "price": "1.1050",
        "openTime": "2026-08-27T09:30:00Z",
        "unrealizedPL": "12.50",
        "state": "OPEN",
        "clientExtensions": {"id": "client-77"},
        "stopLossOrder": {"id": "sl-77", "price": "1.1150", "state": "PENDING"},
    }
    trade.update(changes)
    return trade


def test_account_and_open_trades_map_to_canonical_account_and_positions() -> None:
    payload = account_payload(
        balance="10025.00",
        NAV="10037.50",
        marginUsed="250.00",
        marginAvailable="9787.50",
        openTradeCount=1,
        trades=[trade_payload()],
    )
    broker, _ = connected_broker(response(200, payload))

    account = broker.get_account_info()

    assert account.balance == 10025.0
    assert account.equity == 10037.5
    assert account.margin_used == 250.0
    assert account.margin_available == 9787.5
    assert account.currency == "USD"
    assert len(account.open_positions) == 1
    position = account.open_positions[0]
    assert position.position_id == "trade-77"
    assert position.symbol == "EURUSD"
    assert position.side == -1
    assert position.size == 0.025
    assert position.entry_price == 1.105
    assert position.entry_time == datetime(2026, 8, 27, 9, 30, tzinfo=UTC)
    assert position.unrealized_pnl == 12.5


@pytest.mark.parametrize(
    "changes",
    [
        {"balance": "nan"},
        {"NAV": "0"},
        {"marginUsed": "-1"},
        {"openTradeCount": 1, "trades": []},
        {"openTradeCount": 1, "trades": [trade_payload(currentUnits="0")]},
        {"openTradeCount": 1, "trades": [trade_payload(instrument="USD_JPY")]},
        {"openTradeCount": 1, "trades": [trade_payload(openTime="bad")]},
    ],
)
def test_invalid_account_or_trade_state_fails_closed(changes: dict[str, object]) -> None:
    broker, _ = connected_broker(response(200, account_payload(**changes)))
    with pytest.raises(RuntimeError):
        broker.get_account_info()


def market_order(**changes: object) -> OrderRequest:
    values: dict[str, object] = {
        "symbol": "EURUSD",
        "side": 1,
        "size": 0.01,
        "order_type": "market",
        "order_id": "client-1",
        "price": 1.10000,
        "sl_price": 1.09000,
        "tp_price": 1.12000,
    }
    values.update(changes)
    return OrderRequest(**values)  # type: ignore[arg-type]


def accepted_payload(order: OrderRequest, broker_id: str = "order-1") -> Mapping[str, object]:
    return {
        "orderCreateTransaction": {
            "id": broker_id,
            "instrument": "EUR_USD",
            "units": "1000" if order.side == 1 else "-1000",
            "clientExtensions": {"id": order.order_id},
        }
    }


def test_market_submission_uses_exact_units_identity_and_native_protection() -> None:
    order = market_order()
    broker, transport = connected_broker(response(201, accepted_payload(order)))
    assert broker.submit_order(order) == "order-1"
    sent = transport.calls[-1]["json_body"]
    assert sent == {
        "order": {
            "type": "MARKET",
            "timeInForce": "FOK",
            "positionFill": "OPEN_ONLY",
            "instrument": "EUR_USD",
            "units": "1000.00",
            "clientExtensions": {"id": "client-1"},
            "tradeClientExtensions": {"id": "client-1"},
            "stopLossOnFill": {"price": "1.09", "timeInForce": "GTC"},
            "takeProfitOnFill": {"price": "1.12", "timeInForce": "GTC"},
        }
    }


@pytest.mark.parametrize(
    "size, side, units, sl, tp",
    [
        (0.01, 1, "1000.00", 1.09, 1.12),
        (1.0, 1, "100000.0", 1.09, 1.12),
        (0.01, -1, "-1000.00", 1.11, 1.08),
    ],
)
def test_lot_to_unit_conversion_is_exact_and_directional(
    size: float, side: int, units: str, sl: float, tp: float
) -> None:
    order = market_order(size=size, side=side, sl_price=sl, tp_price=tp)
    accepted = accepted_payload(order)
    transaction = accepted["orderCreateTransaction"]
    assert isinstance(transaction, dict)
    transaction["units"] = units
    broker, transport = connected_broker(response(201, accepted))
    broker.submit_order(order)
    assert transport.calls[-1]["json_body"]["order"]["units"] == units  # type: ignore[index]


def test_order_manager_style_submission_uses_only_a_fresh_cached_quote() -> None:
    order = market_order(price=None)
    broker, transport = connected_broker(
        response(200, price_payload()), response(201, accepted_payload(order))
    )
    broker.clock = lambda: datetime(2026, 8, 27, 10, 0, 2, tzinfo=UTC)
    broker.subscribe_market_data(["EURUSD"])
    broker.get_latest_tick("EURUSD")
    assert broker.submit_order(order) == "order-1"

    stale, stale_transport = connected_broker(
        response(200, price_payload()), response(201, accepted_payload(order))
    )
    stale.clock = lambda: datetime(2026, 8, 27, 10, 0, 2, tzinfo=UTC)
    stale.subscribe_market_data(["EURUSD"])
    stale.get_latest_tick("EURUSD")
    stale.clock = lambda: datetime(2026, 8, 27, 10, 0, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="reference_required"):
        stale.submit_order(order)
    assert len(stale_transport.calls) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "USDJPY"},
        {"order_type": "limit"},
        {"order_id": "bad id"},
        {"order_id": "a" * 129},
        {"size": 0.000001},
        {"size": 1001.0},
        {"sl_price": None},
        {"sl_price": 1.100001},
        {"sl_price": 1.11, "tp_price": 1.12},
        {"side": -1, "sl_price": 1.09, "tp_price": 1.08},
    ],
)
def test_invalid_submission_is_rejected_before_network(changes: dict[str, object]) -> None:
    broker, transport = connected_broker()
    calls = len(transport.calls)
    with pytest.raises(ValueError):
        broker.submit_order(market_order(**changes))
    assert len(transport.calls) == calls


def test_authoritative_rejection_is_proof_bearing_but_duplicate_is_uncertain() -> None:
    proven = response(
        400,
        {"orderRejectTransaction": {"id": "reject-1", "rejectReason": "STOP_LOSS_INVALID"}},
    )
    broker, transport = connected_broker(proven)
    with pytest.raises(BrokerOrderRejected) as caught:
        broker.submit_order(market_order())
    assert caught.value.reason == "oanda_order_rejected"
    assert caught.value.rejection_transaction_id == "reject-1"
    assert len(transport.calls) == 3

    duplicate = response(
        400,
        {
            "orderRejectTransaction": {
                "id": "reject-2",
                "rejectReason": "CLIENT_ORDER_ID_ALREADY_EXISTS",
            }
        },
    )
    broker2, _ = connected_broker(duplicate)
    with pytest.raises(RuntimeError, match="uncertain"):
        broker2.submit_order(market_order())


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_rejection_shaped_non_authoritative_status_is_submission_uncertain(
    status: int,
) -> None:
    contradictory = response(
        status,
        {
            "orderRejectTransaction": {
                "id": "reject-contradictory",
                "rejectReason": "STOP_LOSS_INVALID",
            }
        },
    )
    broker, transport = connected_broker(contradictory)

    with pytest.raises(RuntimeError, match="oanda_submission_uncertain") as caught:
        broker.submit_order(market_order())

    assert not isinstance(caught.value, BrokerOrderRejected)
    assert len(transport.calls) == 3


@pytest.mark.parametrize("item", [response(503, {}), response(201, {}), TimeoutError()])
def test_submission_uncertainty_is_not_retried(item: object) -> None:
    class RaisingTransport(FakeTransport):
        def request(self, method: str, path: str, **kwargs: object) -> object:
            self.calls.append({"method": method, "path": path, **kwargs})
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

    transport = RaisingTransport(
        [response(200, account_payload()), response(200, instrument_payload()), item]
    )
    broker = OandaDemoBroker("practice-account", "private-token", transport=transport)
    broker.connect()
    with pytest.raises((RuntimeError, TimeoutError)):
        broker.submit_order(market_order())
    assert len(transport.calls) == 3


def order_status_payload(state: str, **changes: object) -> Mapping[str, object]:
    order: dict[str, object] = {
        "id": "order-1",
        "clientExtensions": {"id": "client-1"},
        "instrument": "EUR_USD",
        "units": "1000",
        "state": state,
    }
    order.update(changes)
    return {"order": order}


@pytest.mark.parametrize(
    "state, expected",
    [("PENDING", "pending"), ("REJECTED", "rejected"), ("CANCELLED", "cancelled")],
)
def test_order_status_maps_only_exact_supported_states(state: str, expected: str) -> None:
    request = market_order()
    broker, _ = connected_broker(
        response(201, accepted_payload(request)), response(200, order_status_payload(state))
    )
    order_id = broker.submit_order(request)
    assert broker.get_order_status(order_id) == {"status": expected}


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "other"},
        {"clientExtensions": {"id": "other"}},
        {"state": "PARTIALLY_FILLED"},
    ],
)
def test_ambiguous_or_partial_status_never_maps_to_filled(changes: dict[str, object]) -> None:
    request = market_order()
    state = str(changes.get("state", "PENDING"))
    fields = {key: value for key, value in changes.items() if key != "state"}
    broker, _ = connected_broker(
        response(201, accepted_payload(request)),
        response(200, order_status_payload(state, **fields)),
    )
    broker.submit_order(request)
    with pytest.raises(RuntimeError):
        broker.get_order_status("order-1")


def reflected_trade_payload(**changes: object) -> Mapping[str, object]:
    trade: dict[str, object] = {
        "id": "trade-1",
        "state": "OPEN",
        "instrument": "EUR_USD",
        "initialUnits": "1000",
        "currentUnits": "1000",
        "clientExtensions": {"id": "client-1"},
        "stopLossOrder": {"id": "sl-1", "tradeID": "trade-1", "state": "PENDING", "price": "1.09"},
        "takeProfitOrder": {
            "id": "tp-1",
            "tradeID": "trade-1",
            "state": "PENDING",
            "price": "1.12",
        },
    }
    trade.update(changes)
    return {"trade": trade}


def fill_transaction_payload(**changes: object) -> Mapping[str, object]:
    transaction: dict[str, object] = {
        "id": "fill-1",
        "orderID": "order-1",
        "clientOrderID": "client-1",
        "instrument": "EUR_USD",
        "units": "1000",
        "tradeOpened": {"tradeID": "trade-1", "units": "1000"},
    }
    transaction.update(changes)
    return {"transaction": transaction}


def test_filled_status_requires_exact_trade_and_protection_reflection() -> None:
    request = market_order()
    broker, _ = connected_broker(
        response(201, accepted_payload(request)),
        response(200, order_status_payload("FILLED", fillingTransactionID="fill-1")),
        response(200, fill_transaction_payload()),
        response(200, reflected_trade_payload()),
    )
    broker.submit_order(request)
    assert broker.get_order_status("order-1") == {"status": "filled"}


@pytest.mark.parametrize(
    "changes",
    [
        {"currentUnits": "500"},
        {"clientExtensions": {"id": "other"}},
        {"stopLossOrder": None},
        {
            "stopLossOrder": {
                "id": "sl-1",
                "tradeID": "trade-1",
                "state": "PENDING",
                "price": "1.08",
            }
        },
        {"stopLossOrder": {"id": "sl-1", "tradeID": "other", "state": "PENDING", "price": "1.09"}},
        {"takeProfitOrder": None},
    ],
)
def test_conflicting_trade_or_protection_reflection_fails_closed(
    changes: dict[str, object],
) -> None:
    request = market_order()
    broker, _ = connected_broker(
        response(201, accepted_payload(request)),
        response(200, order_status_payload("FILLED", fillingTransactionID="fill-1")),
        response(200, fill_transaction_payload()),
        response(200, reflected_trade_payload(**changes)),
    )
    broker.submit_order(request)
    with pytest.raises(RuntimeError, match="reflection"):
        broker.get_order_status("order-1")


@pytest.mark.parametrize(
    "changes",
    [
        {"units": "500"},
        {"tradeOpened": {"tradeID": "trade-1", "units": "500"}},
        {"tradeReduced": {"tradeID": "trade-old"}},
        {"tradesClosed": [{"tradeID": "trade-old"}]},
        {"residualUnits": "100"},
        {"clientOrderID": "other"},
        {"orderID": "other"},
    ],
)
def test_partial_or_conflicting_fill_transaction_never_becomes_filled(
    changes: dict[str, object],
) -> None:
    request = market_order()
    broker, _ = connected_broker(
        response(201, accepted_payload(request)),
        response(200, order_status_payload("FILLED", fillingTransactionID="fill-1")),
        response(200, fill_transaction_payload(**changes)),
    )
    broker.submit_order(request)
    with pytest.raises(RuntimeError):
        broker.get_order_status("order-1")


def test_close_is_full_trade_only_and_requires_authoritative_identity() -> None:
    open_trade = response(
        200,
        {
            "trade": {
                "id": "trade-1",
                "state": "OPEN",
                "instrument": "EUR_USD",
                "currentUnits": "1000",
            }
        },
    )
    success = response(
        200,
        {
            "orderFillTransaction": {
                "id": "close-1",
                "tradesClosed": [{"tradeID": "trade-1", "units": "1000"}],
            }
        },
    )
    broker, transport = connected_broker(open_trade, success)
    assert broker.close_position("trade-1") == "close-1"
    assert transport.calls[-1]["json_body"] == {"units": "ALL"}

    missing, _ = connected_broker(response(404, {"errorCode": "NO_SUCH_TRADE"}))
    assert missing.close_position("trade-1") is None

    uncertain, _ = connected_broker(open_trade, response(200, {}))
    with pytest.raises(RuntimeError, match="uncertain"):
        uncertain.close_position("trade-1")


def test_secret_material_is_not_in_repr_descriptor_or_public_object_dictionary() -> None:
    broker = OandaDemoBroker("secret-account-7", "secret-token-9")
    assert not hasattr(broker, "__dict__")
    visible = repr(broker) + repr(broker.broker_descriptor)
    assert "secret-account-7" not in visible
    assert "secret-token-9" not in visible


class HttpReply:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self, amount: int) -> bytes:
        return self.body[:amount]


def test_http_transport_is_practice_only_bounded_and_one_attempt() -> None:
    calls: list[object] = []

    def opener(request: object, *, timeout: float) -> HttpReply:
        calls.append((request, timeout))
        return HttpReply(b'{"ok":true}')

    transport = OandaHttpTransport("private-token", opener=opener)
    result = transport.request("GET", "/v3/test", timeout_seconds=2.5, max_response_bytes=100)
    assert result.status == 200
    assert result.payload == {"ok": True}
    assert transport.authority == OANDA_PRACTICE_AUTHORITY
    assert len(calls) == 1
    assert "private-token" not in repr(transport)


@pytest.mark.parametrize(
    "opener, reason",
    [
        (lambda request, timeout: HttpReply(b"x" * 11), "too_large"),
        (lambda request, timeout: HttpReply(b"not-json"), "invalid"),
        (
            lambda request, timeout: (_ for _ in ()).throw(TimeoutError("secret-token")),
            "unavailable",
        ),
    ],
)
def test_http_transport_sanitizes_failures_without_retry(opener, reason: str) -> None:
    calls = 0

    def counted(request: object, *, timeout: float) -> object:
        nonlocal calls
        calls += 1
        return opener(request, timeout)

    transport = OandaHttpTransport("secret-token", opener=counted)
    with pytest.raises(RuntimeError, match=reason) as caught:
        transport.request(
            "GET", "/private?token=secret-token", timeout_seconds=1, max_response_bytes=10
        )
    assert calls == 1
    assert "secret-token" not in str(caught.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_transport_preserves_status_without_exposing_error_body(status: int) -> None:
    body = b'{"error":"authorization secret-token"}'
    calls = 0

    def opener(request: object, *, timeout: float) -> object:
        nonlocal calls
        calls += 1
        raise HTTPError("https://secret.invalid", status, "secret-token", {}, BytesIO(body))

    result = OandaHttpTransport("secret-token", opener=opener).request(
        "GET", "/v3/test", timeout_seconds=1, max_response_bytes=100
    )
    assert result.status == status
    assert calls == 1
    assert "secret-token" not in repr(result)
