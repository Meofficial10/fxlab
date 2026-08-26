"""Unit tests for broker abstraction and data transfer objects (Phase 1)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pandas as pd
import pytest

from fxlab.execution.broker import (
    AccountInfo,
    BrokerAdapter,
    OrderFill,
    OrderRequest,
    OrderStatus,
    Position,
    Tick,
)


class DummyBroker:
    """Mock class implementing the BrokerAdapter protocol for testing."""

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def subscribe_market_data(self, symbols: list[str]) -> None:
        pass

    def get_latest_tick(self, symbol: str) -> Tick | None:
        return None

    def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            balance=10000.0, equity=10000.0, margin_used=0.0, margin_available=10000.0
        )

    def submit_order(self, order: OrderRequest) -> str:
        return order.order_id

    def get_order_status(self, order_id: str) -> dict:
        return {"status": OrderStatus.FILLED.value}

    def cancel_order(self, order_id: str) -> bool:
        return True

    def close_position(self, position_id: str) -> str | None:
        return "close_order_1"

    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        return pd.DataFrame()


class NonBroker:
    """Class missing required methods to test protocol non-conformance."""

    def connect(self) -> None:
        pass


def test_order_status_enum_values():
    assert OrderStatus.PENDING.value == "pending"
    assert OrderStatus.FILLED.value == "filled"
    assert OrderStatus.REJECTED.value == "rejected"
    assert OrderStatus.CANCELLED.value == "cancelled"


def test_tick_valid_instantiation():
    now = datetime.now(UTC)
    tick = Tick(symbol="EURUSD", timestamp=now, bid=1.0850, ask=1.0851, mid=1.08505)
    assert tick.symbol == "EURUSD"
    assert tick.bid == 1.0850
    assert tick.ask == 1.0851
    assert tick.mid == 1.08505


def test_tick_immutability():
    now = datetime.now(UTC)
    tick = Tick(symbol="EURUSD", timestamp=now, bid=1.0850, ask=1.0851, mid=1.08505)
    with pytest.raises(FrozenInstanceError):
        tick.bid = 1.0900  # type: ignore


def test_tick_validation_invalid_prices():
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="Prices must be non-negative"):
        Tick(symbol="EURUSD", timestamp=now, bid=-1.0, ask=1.0850, mid=1.0850)

    with pytest.raises(ValueError, match="Ask price .* cannot be less than bid price"):
        Tick(symbol="EURUSD", timestamp=now, bid=1.0860, ask=1.0850, mid=1.0855)


def test_position_valid_instantiation_and_validation():
    now = datetime.now(UTC)
    pos = Position(
        symbol="EURUSD",
        side=1,
        size=0.1,
        entry_price=1.0850,
        entry_time=now,
        unrealized_pnl=5.0,
        position_id="pos_123",
    )
    assert pos.symbol == "EURUSD"
    assert pos.side == 1
    assert pos.size == 0.1

    with pytest.raises(ValueError, match="Position side must be \\+1 \\(long\\) or -1 \\(short\\)"):
        Position(
            "EURUSD",
            side=0,
            size=0.1,
            entry_price=1.0,
            entry_time=now,
            unrealized_pnl=0.0,
            position_id="p1",
        )

    with pytest.raises(ValueError, match="Position size must be positive"):
        Position(
            "EURUSD",
            side=1,
            size=0.0,
            entry_price=1.0,
            entry_time=now,
            unrealized_pnl=0.0,
            position_id="p1",
        )


def test_account_info_instantiation_and_validation():
    now = datetime.now(UTC)
    pos = Position(
        symbol="USDJPY",
        side=-1,
        size=1.0,
        entry_price=155.0,
        entry_time=now,
        unrealized_pnl=-10.0,
        position_id="pos_456",
    )
    acc = AccountInfo(
        balance=10000.0,
        equity=9990.0,
        margin_used=1000.0,
        margin_available=8990.0,
        open_positions=[pos],
    )
    assert acc.balance == 10000.0
    assert len(acc.open_positions) == 1
    assert acc.open_positions[0].position_id == "pos_456"

    negative = AccountInfo(
        balance=-10.0,
        equity=-12.0,
        margin_used=0.0,
        margin_available=-12.0,
    )
    assert negative.balance == -10.0
    assert negative.equity == -12.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["balance", "equity", "margin_used", "margin_available"])
def test_account_info_rejects_non_finite_values(field: str, value: float):
    values = {
        "balance": 100.0,
        "equity": 100.0,
        "margin_used": 0.0,
        "margin_available": 100.0,
    }
    values[field] = value
    with pytest.raises(ValueError, match="finite number"):
        AccountInfo(**values)


def test_account_info_default_open_positions():
    acc = AccountInfo(balance=5000.0, equity=5000.0, margin_used=0.0, margin_available=5000.0)
    assert acc.open_positions == []


def test_order_request_validation():
    req = OrderRequest(
        symbol="EURUSD",
        side=1,
        size=0.5,
        order_type="market",
        order_id="ord_001",
        sl_price=1.0800,
        tp_price=1.0900,
    )
    assert req.symbol == "EURUSD"
    assert req.side == 1
    assert req.order_type == "market"

    with pytest.raises(ValueError, match="Invalid order_type"):
        OrderRequest(
            symbol="EURUSD", side=1, size=0.5, order_type="invalid_type", order_id="ord_002"
        )

    with pytest.raises(ValueError, match="Order size must be positive"):
        OrderRequest(symbol="EURUSD", side=1, size=-0.1, order_type="market", order_id="ord_003")

    with pytest.raises(ValueError, match="Order side must be \\+1 \\(long\\) or -1 \\(short\\)"):
        OrderRequest(symbol="EURUSD", side=2, size=0.5, order_type="market", order_id="ord_004")


def test_order_request_optional_defaults():
    req = OrderRequest(symbol="EURUSD", side=1, size=0.5, order_type="market", order_id="ord_001")
    assert req.price is None
    assert req.sl_price is None
    assert req.tp_price is None


def test_order_fill_validation():
    now = datetime.now(UTC)
    fill = OrderFill(
        order_id="ord_001",
        fill_price=1.0852,
        fill_time=now,
        fill_size=0.5,
        commission=3.5,
        slippage_pips=0.2,
    )
    assert fill.order_id == "ord_001"
    assert fill.fill_price == 1.0852
    assert fill.fill_size == 0.5

    with pytest.raises(ValueError, match="Fill price must be positive"):
        OrderFill(
            "ord_001",
            fill_price=0.0,
            fill_time=now,
            fill_size=0.5,
            commission=0.0,
            slippage_pips=0.0,
        )

    with pytest.raises(ValueError, match="Fill size must be positive"):
        OrderFill(
            "ord_001",
            fill_price=1.08,
            fill_time=now,
            fill_size=-0.5,
            commission=0.0,
            slippage_pips=0.0,
        )


def test_broker_adapter_protocol_runtime_check():
    dummy = DummyBroker()
    assert isinstance(dummy, BrokerAdapter)

    non_broker = NonBroker()
    assert not isinstance(non_broker, BrokerAdapter)
