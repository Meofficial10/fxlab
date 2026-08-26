"""Tests for the deterministic Phase 6 paper broker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from fxlab.execution.broker import BrokerAdapter, OrderRequest, OrderStatus, Tick
from fxlab.execution.paper_broker import PaperBroker

NOW = datetime(2026, 8, 25, 10, 5, tzinfo=UTC)


def tick(symbol: str = "EURUSD", when: datetime = NOW) -> Tick:
    return Tick(symbol, when, bid=1.0998, ask=1.1000, mid=1.0999)


def order(order_id: str = "client-1", side: int = 1) -> OrderRequest:
    return OrderRequest("EURUSD", side, 0.25, "market", order_id, sl_price=1.09)


def connected_broker() -> PaperBroker:
    broker = PaperBroker()
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])
    return broker


def bars() -> pd.DataFrame:
    index = pd.date_range("2026-08-25 10:00", periods=2, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.09, 1.10],
            "high": [1.11, 1.12],
            "low": [1.08, 1.09],
            "close": [1.10, 1.11],
            "volume": [10.0, 11.0],
        },
        index=index,
    )


def test_connection_lifecycle_and_protocol() -> None:
    broker = PaperBroker()
    assert isinstance(broker, BrokerAdapter)
    assert not broker.is_connected()
    broker.connect()
    assert broker.is_connected()
    broker.disconnect()
    assert not broker.is_connected()


def test_accept_tick_requires_connection_and_subscription() -> None:
    broker = PaperBroker()
    with pytest.raises(RuntimeError):
        broker.accept_tick(tick())
    broker.connect()
    with pytest.raises(ValueError, match="not subscribed"):
        broker.accept_tick(tick())


def test_latest_tick_and_out_of_order_protection() -> None:
    broker = connected_broker()
    newest = tick(when=NOW)
    assert broker.accept_tick(newest)
    assert not broker.accept_tick(tick(when=NOW - timedelta(seconds=1)))
    assert broker.get_latest_tick("eurusd") == newest


@pytest.mark.parametrize(("side", "expected"), [(1, 1.1000), (-1, 1.0998)])
def test_market_fill_uses_executable_side(side: int, expected: float) -> None:
    broker = connected_broker()
    broker.accept_tick(tick())
    broker_id = broker.submit_order(order(side=side))
    position = broker.get_account_info().open_positions[0]
    assert position.entry_price == expected
    assert position.side == side
    assert broker.get_order_status(broker_id)["status"] == OrderStatus.FILLED.value


def test_missing_quote_rejects_order() -> None:
    broker = connected_broker()
    with pytest.raises(ValueError, match="no executable quote"):
        broker.submit_order(order())


def test_duplicate_client_order_id_rejected() -> None:
    broker = connected_broker()
    broker.accept_tick(tick())
    broker.submit_order(order())
    with pytest.raises(ValueError, match="duplicate"):
        broker.submit_order(order())


def test_deterministic_order_position_correlation() -> None:
    broker = connected_broker()
    broker.accept_tick(tick())
    broker_id = broker.submit_order(order("alpha"))
    correlation = broker.get_correlation("alpha")
    assert broker_id == "paper-order::alpha"
    assert correlation is not None
    assert correlation.broker_order_id == "paper-order::alpha"
    assert correlation.position_id == "paper-position::alpha"
    assert broker.get_account_info().open_positions[0].position_id == correlation.position_id


def test_account_snapshot_is_not_mutable_broker_state() -> None:
    broker = connected_broker()
    broker.accept_tick(tick())
    broker.submit_order(order())
    snapshot = broker.get_account_info()
    snapshot.open_positions.clear()
    assert len(broker.get_account_info().open_positions) == 1


def test_historical_bars_never_expose_future_rows() -> None:
    broker = PaperBroker(historical_bars={("EURUSD", "M5"): bars()})
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])
    assert broker.get_historical_bars("EURUSD", "M5", 10).empty
    broker.accept_tick(tick(when=NOW))
    available = broker.get_historical_bars("EURUSD", "M5", 10)
    assert list(available.index) == [pd.Timestamp("2026-08-25 10:00", tz="UTC")]


def test_broker_has_no_persistence_or_network_surface() -> None:
    broker = PaperBroker()
    assert not hasattr(broker, "save")
    assert not hasattr(broker, "load")
    assert not hasattr(broker, "request")
    assert broker.close_position("unknown") is None

