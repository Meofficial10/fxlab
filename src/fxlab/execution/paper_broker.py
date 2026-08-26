"""Deterministic, in-memory broker for historical paper-trading replay."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from threading import Lock

import pandas as pd

from ..config import CostConfig
from ..costs.model import CostModel
from ..data.schema import OHLCV, timeframe_to_timedelta
from .broker import AccountInfo, OrderRequest, OrderStatus, Position, Tick


@dataclass(frozen=True)
class OrderCorrelation:
    """Exact identities linking one client approval to its paper fill."""

    client_order_id: str
    broker_order_id: str
    position_id: str


class CloseReason(StrEnum):
    MANUAL = "manual"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


@dataclass(frozen=True)
class PositionClose:
    """Immutable economic result of one paper-position close."""

    close_order_id: str
    position_id: str
    client_entry_order_id: str
    broker_entry_order_id: str
    symbol: str
    side: int
    size: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    commission: float
    net_realized_pnl: float
    close_time: datetime
    reason: CloseReason


@dataclass
class _PaperPosition:
    position: Position
    client_entry_order_id: str
    broker_entry_order_id: str
    sl_price: float | None
    tp_price: float | None


@dataclass
class PaperBroker:
    """Immediate-fill paper broker driven only by externally accepted replay ticks.

    The configured CostModel supplies fallback spread, base slippage, and commission.
    The fixed monetary pip value is the same simplified convention used by RiskEngine;
    it is not universal account-currency conversion for JPY pairs or FX crosses.
    """

    initial_balance: float = 10_000.0
    historical_bars: InitVar[Mapping[tuple[str, str], pd.DataFrame] | None] = None
    cost_config: InitVar[CostConfig | None] = None
    pip_value_per_lot: float = 10.0

    _connected: bool = field(default=False, init=False)
    _subscriptions: set[str] = field(default_factory=set, init=False)
    _latest_ticks: dict[str, Tick] = field(default_factory=dict, init=False)
    _positions: dict[str, _PaperPosition] = field(default_factory=dict, init=False)
    _statuses: dict[str, OrderStatus] = field(default_factory=dict, init=False)
    _correlations: dict[str, OrderCorrelation] = field(default_factory=dict, init=False)
    _client_by_broker_id: dict[str, str] = field(default_factory=dict, init=False)
    _historical_bars: dict[tuple[str, str], pd.DataFrame] = field(
        default_factory=dict, init=False, repr=False
    )
    _cost_config: CostConfig = field(init=False, repr=False)
    _cost_models: dict[str, CostModel] = field(default_factory=dict, init=False, repr=False)
    _balance: float = field(init=False)
    _equity: float = field(init=False)
    _close_events: list[PositionClose] = field(default_factory=list, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(
        self,
        historical_bars: Mapping[tuple[str, str], pd.DataFrame] | None,
        cost_config: CostConfig | None,
    ) -> None:
        if not _positive_finite(self.initial_balance):
            raise ValueError("initial_balance must be finite and positive")
        if not _positive_finite(self.pip_value_per_lot):
            raise ValueError("pip_value_per_lot must be finite and positive")
        if cost_config is not None and not isinstance(cost_config, CostConfig):
            raise ValueError("cost_config must be a CostConfig")
        self._cost_config = cost_config or CostConfig()
        self._balance = float(self.initial_balance)
        self._equity = float(self.initial_balance)
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
            self._mark_symbol_locked(symbol, tick)
            return True

    def get_latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest_ticks.get(_canonical_symbol(symbol))

    def get_account_info(self) -> AccountInfo:
        with self._lock:
            positions = [
                _copy_position(record.position) for record in self._positions.values()
            ]
            balance = self._balance
            equity = self._equity
        return AccountInfo(
            balance=balance,
            equity=equity,
            margin_used=0.0,
            margin_available=equity,
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
            cost_model = self._cost_model_locked(symbol)
            fill_price = self._modeled_fill_locked(tick, order.side, entry=True)
            broker_id = f"paper-order::{client_id}"
            position_id = f"paper-position::{client_id}"
            correlation = OrderCorrelation(client_id, broker_id, position_id)
            self._correlations[client_id] = correlation
            self._client_by_broker_id[broker_id] = client_id
            self._statuses[broker_id] = OrderStatus.FILLED
            position = Position(
                symbol=symbol,
                side=order.side,
                size=float(order.size),
                entry_price=fill_price,
                entry_time=tick.timestamp.astimezone(UTC),
                unrealized_pnl=0.0,
                position_id=position_id,
            )
            exit_fill = self._modeled_fill_locked(tick, order.side, entry=False)
            position.unrealized_pnl = self._net_pnl_locked(
                position, exit_fill, cost_model
            )[2]
            self._positions[position_id] = _PaperPosition(
                position=position,
                client_entry_order_id=client_id,
                broker_entry_order_id=broker_id,
                sl_price=order.sl_price,
                tp_price=order.tp_price,
            )
            self._recompute_equity_locked()
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
        """Close an exact open position at its latest modeled executable quote."""
        with self._lock:
            record = self._positions.get(position_id)
            if record is None:
                return None
            self._require_connected_locked()
            tick = self._latest_ticks.get(record.position.symbol)
            if tick is None:
                raise ValueError("no executable quote is available for the position")
            return self._close_position_locked(position_id, tick, CloseReason.MANUAL)

    def drain_close_events(self) -> tuple[PositionClose, ...]:
        """Consume each completed-position event exactly once."""
        with self._lock:
            events = tuple(self._close_events)
            self._close_events.clear()
            return events

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

    def _cost_model_locked(self, symbol: str) -> CostModel:
        if symbol not in self._cost_models:
            model = CostModel.from_config(self._cost_config, symbol)
            values = (
                model.pip_size,
                model.spread_pips,
                model.commission_per_lot_roundturn,
                model.slippage_pips_base,
                model.slippage_vol_coeff,
            )
            if any(not _nonnegative_finite(value) for value in values) or model.pip_size <= 0:
                raise ValueError("cost model values must be finite and non-negative")
            self._cost_models[symbol] = model
        return self._cost_models[symbol]

    def _modeled_fill_locked(self, tick: Tick, side: int, *, entry: bool) -> float:
        model = self._cost_model_locked(_canonical_symbol(tick.symbol))
        if float(tick.ask) == float(tick.bid):
            fill = (
                model.entry_fill(float(tick.mid), side, 0.0)
                if entry
                else model.exit_fill(float(tick.mid), side, 0.0)
            )
        else:
            raw = float(tick.ask if (entry and side == 1) else tick.bid)
            if not entry:
                raw = float(tick.bid if side == 1 else tick.ask)
            slippage = model.slippage_price(0.0)
            fill = raw + side * slippage if entry else raw - side * slippage
        if not _positive_finite(fill):
            raise ValueError("modeled executable fill must be finite and positive")
        return float(fill)

    def _mark_symbol_locked(self, symbol: str, tick: Tick) -> None:
        for position_id in sorted(tuple(self._positions)):
            record = self._positions.get(position_id)
            if record is None or record.position.symbol != symbol:
                continue
            position = record.position
            raw_exit = float(tick.bid if position.side == 1 else tick.ask)
            reason: CloseReason | None = None
            if record.sl_price is not None and (
                (position.side == 1 and raw_exit <= record.sl_price)
                or (position.side == -1 and raw_exit >= record.sl_price)
            ):
                reason = CloseReason.STOP_LOSS
            elif record.tp_price is not None and (
                (position.side == 1 and raw_exit >= record.tp_price)
                or (position.side == -1 and raw_exit <= record.tp_price)
            ):
                reason = CloseReason.TAKE_PROFIT
            if reason is not None:
                self._close_position_locked(position_id, tick, reason)
                continue
            exit_fill = self._modeled_fill_locked(tick, position.side, entry=False)
            model = self._cost_model_locked(symbol)
            position.unrealized_pnl = self._net_pnl_locked(
                position, exit_fill, model
            )[2]
        self._recompute_equity_locked()

    def _close_position_locked(
        self, position_id: str, tick: Tick, reason: CloseReason
    ) -> str:
        record = self._positions[position_id]
        position = record.position
        model = self._cost_model_locked(position.symbol)
        exit_fill = self._modeled_fill_locked(tick, position.side, entry=False)
        gross, commission, net = self._net_pnl_locked(position, exit_fill, model)
        close_id = f"paper-close::{position_id}"
        del self._positions[position_id]
        self._balance = float(Decimal(str(self._balance)) + Decimal(str(net)))
        self._close_events.append(
            PositionClose(
                close_order_id=close_id,
                position_id=position_id,
                client_entry_order_id=record.client_entry_order_id,
                broker_entry_order_id=record.broker_entry_order_id,
                symbol=position.symbol,
                side=position.side,
                size=position.size,
                entry_price=position.entry_price,
                exit_price=exit_fill,
                gross_pnl=gross,
                commission=commission,
                net_realized_pnl=net,
                close_time=tick.timestamp.astimezone(UTC),
                reason=reason,
            )
        )
        self._recompute_equity_locked()
        return close_id

    def _net_pnl_locked(
        self, position: Position, exit_fill: float, model: CostModel
    ) -> tuple[float, float, float]:
        side = Decimal(position.side)
        price_move = Decimal(str(exit_fill)) - Decimal(str(position.entry_price))
        pnl_pips = side * price_move / Decimal(str(model.pip_size))
        gross = (
            pnl_pips
            * Decimal(str(self.pip_value_per_lot))
            * Decimal(str(position.size))
        )
        commission = Decimal(str(model.commission_cost(position.size)))
        net = gross - commission
        results = (float(gross), float(commission), float(net))
        if any(not math.isfinite(value) for value in results):
            raise ValueError("paper PnL must remain finite")
        return results

    def _recompute_equity_locked(self) -> None:
        unrealized = sum(
            Decimal(str(record.position.unrealized_pnl))
            for record in self._positions.values()
        )
        equity = Decimal(str(self._balance)) + unrealized
        converted = float(equity)
        if not math.isfinite(converted):
            raise ValueError("paper account equity must remain finite")
        self._equity = converted


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


def _nonnegative_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted) and converted >= 0


def _validate_tick(tick: object) -> None:
    if not isinstance(tick, Tick):
        raise ValueError("replay event must be a Tick")
    if not _canonical_symbol(tick.symbol):
        raise ValueError("tick symbol must be non-empty")
    if tick.timestamp.tzinfo is None or tick.timestamp.utcoffset() is None:
        raise ValueError("tick timestamp must be timezone-aware")
    if (
        not _positive_finite(tick.bid)
        or not _positive_finite(tick.ask)
        or not _positive_finite(tick.mid)
    ):
        raise ValueError("tick bid, ask, and mid must be finite and positive")
    if float(tick.ask) < float(tick.bid):
        raise ValueError("tick ask cannot be below bid")
