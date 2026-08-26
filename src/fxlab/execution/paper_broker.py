"""Deterministic, in-memory broker for historical paper-trading replay."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from datetime import UTC
from threading import Lock

import pandas as pd

from ..data.schema import OHLCV, timeframe_to_timedelta
from .broker import AccountInfo, OrderRequest, OrderStatus, Position, Tick


@dataclass(frozen=True)
class OrderCorrelation:
    """Exact identities linking one client approval to its paper fill."""

    client_order_id: str
    broker_order_id: str
    position_id: str


@dataclass
class PaperBroker:
    """Immediate-fill paper broker driven only by externally accepted replay ticks.

    The broker models no extra spread, slippage, commission, margin, or persistence.
    Long market orders fill at the accepted ask and shorts at the accepted bid.
    """

    initial_balance: float = 10_000.0
    historical_bars: InitVar[Mapping[tuple[str, str], pd.DataFrame] | None] = None

    _connected: bool = field(default=False, init=False)
    _subscriptions: set[str] = field(default_factory=set, init=False)
    _latest_ticks: dict[str, Tick] = field(default_factory=dict, init=False)
    _positions: dict[str, Position] = field(default_factory=dict, init=False)
    _statuses: dict[str, OrderStatus] = field(default_factory=dict, init=False)
    _correlations: dict[str, OrderCorrelation] = field(default_factory=dict, init=False)
    _client_by_broker_id: dict[str, str] = field(default_factory=dict, init=False)
    _historical_bars: dict[tuple[str, str], pd.DataFrame] = field(
        default_factory=dict, init=False, repr=False
    )
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(
        self, historical_bars: Mapping[tuple[str, str], pd.DataFrame] | None
    ) -> None:
        if not _positive_finite(self.initial_balance):
            raise ValueError("initial_balance must be finite and positive")
        normalized: dict[tuple[str, str], pd.DataFrame] = {}
        for (symbol, timeframe), bars in (historical_bars or {}).items():
            canonical = _canonical_symbol(symbol)
            if not canonical or not isinstance(bars, pd.DataFrame):
                raise ValueError("historical bars require a non-empty symbol and DataFrame")
            frame = bars.copy()
            if not isinstance(frame.index, pd.DatetimeIndex):
                raise ValueError("historical bars must use a DatetimeIndex")
            frame.index = (
                frame.index.tz_localize("UTC")
                if frame.index.tz is None
                else frame.index.tz_convert("UTC")
            )
            missing = [column for column in OHLCV if column not in frame.columns]
            if missing:
                raise ValueError(f"historical bars missing columns: {missing}")
            normalized[(canonical, timeframe)] = frame[OHLCV].astype("float64").sort_index()
        self._historical_bars = normalized

    def connect(self) -> None:
        with self._lock:
            self._connected = True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def subscribe_market_data(self, symbols: list[str]) -> None:
        with self._lock:
            self._require_connected_locked()
            for symbol in symbols:
                canonical = _canonical_symbol(symbol)
                if not canonical:
                    raise ValueError("subscription symbols must be non-empty")
                self._subscriptions.add(canonical)

    def accept_tick(self, tick: Tick) -> bool:
        """Accept one chronological replay tick; return false for an older tick."""
        _validate_tick(tick)
        symbol = _canonical_symbol(tick.symbol)
        with self._lock:
            self._require_connected_locked()
            if symbol not in self._subscriptions:
                raise ValueError(f"symbol {symbol!r} is not subscribed")
            previous = self._latest_ticks.get(symbol)
            if (
                previous is not None
                and tick.timestamp.astimezone(UTC) < previous.timestamp.astimezone(UTC)
            ):
                return False
            self._latest_ticks[symbol] = tick
            return True

    def get_latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest_ticks.get(_canonical_symbol(symbol))

    def get_account_info(self) -> AccountInfo:
        with self._lock:
            positions = [_copy_position(position) for position in self._positions.values()]
            balance = float(self.initial_balance)
        return AccountInfo(
            balance=balance,
            equity=balance,
            margin_used=0.0,
            margin_available=balance,
            open_positions=positions,
        )

    def submit_order(self, order: OrderRequest) -> str:
        if not isinstance(order, OrderRequest):
            raise ValueError("order must be an OrderRequest")
        client_id = order.order_id.strip() if isinstance(order.order_id, str) else ""
        if not client_id:
            raise ValueError("client order ID must be non-empty")
        if order.order_type != "market":
            raise ValueError("PaperBroker supports market orders only")
        symbol = _canonical_symbol(order.symbol)
        with self._lock:
            self._require_connected_locked()
            if client_id in self._correlations:
                raise ValueError("duplicate client order ID")
            tick = self._latest_ticks.get(symbol)
            if tick is None:
                raise ValueError("no executable quote is available")
            _validate_tick(tick)
            fill_price = float(tick.ask if order.side == 1 else tick.bid)
            broker_id = f"paper-order::{client_id}"
            position_id = f"paper-position::{client_id}"
            correlation = OrderCorrelation(client_id, broker_id, position_id)
            self._correlations[client_id] = correlation
            self._client_by_broker_id[broker_id] = client_id
            self._statuses[broker_id] = OrderStatus.FILLED
            self._positions[position_id] = Position(
                symbol=symbol,
                side=order.side,
                size=float(order.size),
                entry_price=fill_price,
                entry_time=tick.timestamp.astimezone(UTC),
                unrealized_pnl=0.0,
                position_id=position_id,
            )
            return broker_id

    def get_order_status(self, order_id: str) -> dict:
        with self._lock:
            status = self._statuses.get(order_id)
            client_id = self._client_by_broker_id.get(order_id)
            correlation = self._correlations.get(client_id or "")
        if status is None or correlation is None:
            raise ValueError("unknown broker order ID")
        return {
            "status": status.value,
            "client_order_id": correlation.client_order_id,
            "broker_order_id": correlation.broker_order_id,
            "position_id": correlation.position_id,
        }

    def get_correlation(self, client_order_id: str) -> OrderCorrelation | None:
        with self._lock:
            return self._correlations.get(client_order_id)

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            return self._statuses.get(order_id) is OrderStatus.PENDING

    def close_position(self, position_id: str) -> str | None:
        """Position closing is outside the minimal Phase 6 paper lifecycle."""
        return None

    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        """Return only bars proven closed by the latest accepted replay tick."""
        canonical = _canonical_symbol(symbol)
        with self._lock:
            frame = self._historical_bars.get((canonical, tf))
            tick = self._latest_ticks.get(canonical)
            if frame is None or tick is None:
                return _empty_bars(canonical, tf)
            watermark = pd.Timestamp(tick.timestamp).tz_convert("UTC")
            available = frame[frame.index + timeframe_to_timedelta(tf) <= watermark].tail(count)
            result = available.copy()
        result.attrs["symbol"] = canonical
        result.attrs["timeframe"] = tf
        return result

    def _require_connected_locked(self) -> None:
        if not self._connected:
            raise RuntimeError("paper broker is not connected")


def _copy_position(position: Position) -> Position:
    return Position(
        symbol=position.symbol,
        side=position.side,
        size=position.size,
        entry_price=position.entry_price,
        entry_time=position.entry_time,
        unrealized_pnl=position.unrealized_pnl,
        position_id=position.position_id,
    )


def _empty_bars(symbol: str, timeframe: str) -> pd.DataFrame:
    frame = pd.DataFrame(columns=OHLCV, dtype="float64")
    frame.index = pd.DatetimeIndex([], name="ts_open", tz="UTC")
    frame.attrs["symbol"] = symbol
    frame.attrs["timeframe"] = timeframe
    return frame


def _canonical_symbol(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _positive_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted) and converted > 0


def _validate_tick(tick: object) -> None:
    if not isinstance(tick, Tick):
        raise ValueError("replay event must be a Tick")
    if not _canonical_symbol(tick.symbol):
        raise ValueError("tick symbol must be non-empty")
    if tick.timestamp.tzinfo is None or tick.timestamp.utcoffset() is None:
        raise ValueError("tick timestamp must be timezone-aware")
    if not _positive_finite(tick.bid) or not _positive_finite(tick.ask):
        raise ValueError("tick bid and ask must be finite and positive")
    if float(tick.ask) < float(tick.bid):
        raise ValueError("tick ask cannot be below bid")
