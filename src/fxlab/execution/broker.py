"""Abstract Broker Interface and Data Transfer Objects (Phase 1 Paper Trading).

Defines the core `BrokerAdapter` protocol and data structures for broker-agnostic
market data retrieval and order execution. Allows paper trading (simulated fills) and
future live broker implementations to be swapped seamlessly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

import pandas as pd


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Tick:
    """Real-time market tick with bid, ask, and mid prices."""

    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    mid: float

    def __post_init__(self) -> None:
        if self.bid < 0 or self.ask < 0:
            raise ValueError(f"Prices must be non-negative, got bid={self.bid}, ask={self.ask}")
        if self.ask < self.bid:
            raise ValueError(f"Ask price ({self.ask}) cannot be less than bid price ({self.bid})")


@dataclass
class Position:
    """Open position record."""

    symbol: str
    side: int  # +1 for long, -1 for short
    size: float  # size in standard lots
    entry_price: float
    entry_time: datetime
    unrealized_pnl: float
    position_id: str

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError(f"Position side must be +1 (long) or -1 (short), got {self.side}")
        if self.size <= 0:
            raise ValueError(f"Position size must be positive, got {self.size}")


@dataclass
class AccountInfo:
    """Account balance and margin snapshot."""

    balance: float
    equity: float
    margin_used: float
    margin_available: float
    open_positions: list[Position] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.balance < 0:
            raise ValueError(f"Account balance cannot be negative, got {self.balance}")


@dataclass
class OrderRequest:
    """Order submission request."""

    symbol: str
    side: int  # +1 for long, -1 for short
    size: float  # size in standard lots
    order_type: str  # "market", "limit", or "stop"
    order_id: str  # client-assigned unique order ID
    price: float | None = None  # target price for limit/stop
    sl_price: float | None = None  # stop-loss price
    tp_price: float | None = None  # take-profit price

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError(f"Order side must be +1 (long) or -1 (short), got {self.side}")
        if self.size <= 0:
            raise ValueError(f"Order size must be positive, got {self.size}")
        if self.order_type not in ("market", "limit", "stop"):
            raise ValueError(f"Invalid order_type '{self.order_type}'. Expected 'market', 'limit', or 'stop'")


@dataclass
class OrderFill:
    """Order fill execution confirmation."""

    order_id: str
    fill_price: float
    fill_time: datetime
    fill_size: float
    commission: float
    slippage_pips: float

    def __post_init__(self) -> None:
        if self.fill_price <= 0:
            raise ValueError(f"Fill price must be positive, got {self.fill_price}")
        if self.fill_size <= 0:
            raise ValueError(f"Fill size must be positive, got {self.fill_size}")


@runtime_checkable
class BrokerAdapter(Protocol):
    """Abstract broker interface for paper or live execution."""

    def connect(self) -> None:
        """Establish connection to broker (no-op for paper broker)."""
        ...

    def disconnect(self) -> None:
        """Close connection to broker."""
        ...

    def is_connected(self) -> bool:
        """Check connection status."""
        ...

    def subscribe_market_data(self, symbols: list[str]) -> None:
        """Subscribe to real-time market data ticks for given symbols."""
        ...

    def get_latest_tick(self, symbol: str) -> Tick | None:
        """Fetch the most recent market tick for symbol."""
        ...

    def get_account_info(self) -> AccountInfo:
        """Fetch current account balance, equity, and open positions."""
        ...

    def submit_order(self, order: OrderRequest) -> str:
        """Submit an order request to the broker. Returns order_id."""
        ...

    def get_order_status(self, order_id: str) -> dict:
        """Query status of submitted order (pending, filled, rejected, cancelled)."""
        ...

    def cancel_order(self, order_id: str) -> bool:
        """Attempt to cancel a pending order."""
        ...

    def close_position(self, position_id: str) -> str | None:
        """Close an open position by position_id."""
        ...

    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """Fetch historical bars for strategy warm-up (e.g. ATR calculation)."""
        ...
