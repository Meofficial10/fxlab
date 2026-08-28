"""Deterministic, in-memory broker for historical paper-trading replay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from threading import Lock

import pandas as pd

from ..config import CostConfig
from ..costs.model import CostModel
from ..data.schema import OHLCV, timeframe_to_timedelta
from .broker import (
    AccountInfo,
    BrokerOrderRejected,
    OrderRequest,
    OrderStatus,
    Position,
    Tick,
)
from .broker_capabilities import (
    BrokerCapability,
    BrokerDescriptor,
    BrokerEnvironment,
)
from .margin import MarginExposure, MarginResult, PaperMarginModel
from .valuation import (
    ConversionQuote,
    FxInstrumentCatalog,
    FxValuationEngine,
    PipValuation,
    ValuationFailure,
)

_PAPER_BROKER_DESCRIPTOR = BrokerDescriptor(
    broker_id="fxlab-paper",
    implementation_version="2",
    environment=BrokerEnvironment.PAPER,
    capabilities=frozenset(
        {
            BrokerCapability.MARKET_ORDERS,
            BrokerCapability.NATIVE_SL_TP,
            BrokerCapability.HEDGING,
            BrokerCapability.CLIENT_ORDER_IDS,
        }
    ),
    deterministic=True,
)


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
    account_currency: str
    valuation_id: str
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
    All monetary valuation is explicit, point-in-time, and account-currency aware.
    """

    account_currency: str
    instrument_catalog: InitVar[FxInstrumentCatalog]
    valuation_max_age: InitVar[timedelta]
    valuation_policy_version: str
    margin_model: InitVar[PaperMarginModel]
    commission_currency: str
    initial_balance: float = 10_000.0
    historical_bars: InitVar[Mapping[tuple[str, str], pd.DataFrame] | None] = None
    cost_config: InitVar[CostConfig | None] = None

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
    _valuation_engine: FxValuationEngine = field(init=False, repr=False)
    _margin_model: PaperMarginModel = field(init=False, repr=False)
    _cost_models: dict[str, CostModel] = field(default_factory=dict, init=False, repr=False)
    _balance: float = field(init=False)
    _equity: float = field(init=False)
    _close_events: list[PositionClose] = field(default_factory=list, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(
        self,
        instrument_catalog: FxInstrumentCatalog,
        valuation_max_age: timedelta,
        margin_model: PaperMarginModel,
        historical_bars: Mapping[tuple[str, str], pd.DataFrame] | None,
        cost_config: CostConfig | None,
    ) -> None:
        if (
            not isinstance(self.account_currency, str)
            or len(self.account_currency) != 3
            or not self.account_currency.isalpha()
            or not self.account_currency.isupper()
        ):
            raise ValueError("account_currency must be an uppercase three-letter code")
        if self.commission_currency != self.account_currency:
            raise ValueError("commission_currency must match account_currency")
        if not _positive_finite(self.initial_balance):
            raise ValueError("initial_balance must be finite and positive")
        if not isinstance(instrument_catalog, FxInstrumentCatalog):
            raise ValueError("instrument_catalog must be an FxInstrumentCatalog")
        if not isinstance(margin_model, PaperMarginModel):
            raise ValueError("margin_model must implement PaperMarginModel")
        if margin_model.descriptor.account_currency != self.account_currency:
            raise ValueError("margin model currency must match account_currency")
        if cost_config is not None and not isinstance(cost_config, CostConfig):
            raise ValueError("cost_config must be a CostConfig")
        self._valuation_engine = FxValuationEngine(
            instrument_catalog,
            max_age=valuation_max_age,
            policy_version=self.valuation_policy_version,
        )
        self._margin_model = margin_model
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

    @property
    def broker_descriptor(self) -> BrokerDescriptor:
        """Return the immutable capability declaration for this adapter."""
        return _PAPER_BROKER_DESCRIPTOR

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
            try:
                self._mark_symbol_locked(symbol, tick)
            except Exception:
                if previous is None:
                    self._latest_ticks.pop(symbol, None)
                else:
                    self._latest_ticks[symbol] = previous
                raise
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
            margin = self._margin_locked(equity)
        return AccountInfo(
            balance=balance,
            equity=equity,
            margin_used=margin.margin_used,
            margin_available=margin.margin_available,
            currency=self.account_currency,
            open_positions=positions,
        )

    def pip_valuation(
        self, symbol: str, account_currency: str, as_of: datetime
    ) -> PipValuation:
        with self._lock:
            if account_currency != self.account_currency:
                raise ValuationFailure("account_currency_unsupported")
            return self._valuation_engine.pip_valuation(
                _canonical_symbol(symbol),
                account_currency,
                as_of,
                self._conversion_quotes_locked(),
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
                position,
                exit_fill,
                cost_model,
                tick.timestamp.astimezone(UTC),
            )[2]
            projected = tuple(
                MarginExposure(
                    item.position.symbol,
                    item.position.size,
                    item.position.side,
                )
                for item in self._positions.values()
            ) + (MarginExposure(symbol, float(order.size), order.side),)
            projected_equity = float(
                Decimal(str(self._equity))
                + Decimal(str(position.unrealized_pnl))
            )
            try:
                margin = self._margin_model.calculate(
                    projected,
                    equity=projected_equity,
                    as_of=tick.timestamp,
                    valuation=self._valuation_engine,
                    quotes=self._conversion_quotes_locked(),
                )
            except (ValueError, ValuationFailure) as exc:
                raise BrokerOrderRejected("margin_valuation_unavailable") from exc
            if not margin.sufficient:
                raise BrokerOrderRejected("insufficient_margin")
            correlation = OrderCorrelation(client_id, broker_id, position_id)
            self._correlations[client_id] = correlation
            self._client_by_broker_id[broker_id] = client_id
            self._statuses[broker_id] = OrderStatus.FILLED
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

    @property
    def has_pending_close_events(self) -> bool:
        with self._lock:
            return bool(self._close_events)

    def snapshot_state(self) -> dict[str, object]:
        """Return primitive PaperBroker state; connection state is excluded."""
        with self._lock:
            return {
                "balance": self._balance,
                "equity": self._equity,
                "latest_ticks": [
                    {
                        "symbol": tick.symbol,
                        "timestamp": tick.timestamp.astimezone(UTC).isoformat(),
                        "bid": tick.bid,
                        "ask": tick.ask,
                        "mid": tick.mid,
                    }
                    for _, tick in sorted(self._latest_ticks.items())
                ],
                "positions": [
                    {
                        "symbol": item.position.symbol,
                        "side": item.position.side,
                        "size": item.position.size,
                        "entry_price": item.position.entry_price,
                        "entry_time": item.position.entry_time.astimezone(UTC).isoformat(),
                        "unrealized_pnl": item.position.unrealized_pnl,
                        "position_id": item.position.position_id,
                        "client_entry_order_id": item.client_entry_order_id,
                        "broker_entry_order_id": item.broker_entry_order_id,
                        "sl_price": item.sl_price,
                        "tp_price": item.tp_price,
                    }
                    for _, item in sorted(self._positions.items())
                ],
                "statuses": {key: value.value for key, value in sorted(self._statuses.items())},
                "correlations": [
                    {
                        "client_order_id": item.client_order_id,
                        "broker_order_id": item.broker_order_id,
                        "position_id": item.position_id,
                    }
                    for _, item in sorted(self._correlations.items())
                ],
            }

    def configuration_snapshot(self, symbols: list[str]) -> dict[str, object]:
        snapshot = {
            "broker_descriptor": self.broker_descriptor.compatibility_snapshot(),
            "initial_balance": self.initial_balance,
            "account_currency": self.account_currency,
            "commission_currency": self.commission_currency,
            "instrument_catalog_fingerprint": self._valuation_engine.catalog.fingerprint,
            "valuation_policy_version": self._valuation_engine.policy_version,
            "conversion_freshness_seconds": self._valuation_engine.max_age.total_seconds(),
            "margin_model_identity": self._margin_model.descriptor.fingerprint,
            "margin_model": self._margin_model.descriptor.model_id,
            "margin_quality": self._margin_model.descriptor.quality,
            "margin_leverage": self._margin_model.descriptor.leverage_by_symbol,
            "fill_timing_policy": "immediate-v1",
            "partial_fill_policy": "none-v1",
            "spread_policy": "tick-or-cost-fallback-v1",
            "slippage_policy": "deterministic-base-norm-vol-zero-v1",
            "cost_config": self._cost_config.model_dump(mode="json"),
            "pip_sizes": {
                symbol: self._cost_config.pip_size_for(symbol)
                for symbol in sorted({_canonical_symbol(item) for item in symbols})
            },
        }
        snapshot["execution_model_fingerprint"] = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return snapshot

    def economic_monitoring_snapshot(self) -> dict[str, object]:
        """Return only immutable primitive Phase 18 model identities and health."""
        with self._lock:
            latest = max(
                (tick.timestamp.astimezone(UTC) for tick in self._latest_ticks.values()),
                default=None,
            )
            symbols = sorted(
                set(self._valuation_engine.catalog.symbols)
                & (
                    set(self._latest_ticks)
                    | {item.position.symbol for item in self._positions.values()}
                )
            )
            configuration = self.configuration_snapshot(symbols)
            return {
                "account_currency": self.account_currency,
                "valuation_policy_version": self._valuation_engine.policy_version,
                "instrument_catalog_fingerprint": self._valuation_engine.catalog.fingerprint,
                "margin_model_identity": self._margin_model.descriptor.fingerprint,
                "margin_model": self._margin_model.descriptor.model_id,
                "margin_quality": self._margin_model.descriptor.quality,
                "margin_leverage": self._margin_model.descriptor.leverage_by_symbol,
                "latest_valuation_observation": latest.isoformat() if latest else None,
                "valuation_health": "available" if latest else "unobserved",
                "execution_model_fingerprint": configuration[
                    "execution_model_fingerprint"
                ],
            }

    def restore_state(self, state: Mapping[str, object]) -> None:
        """Atomically restore validated paper state without fills or PnL actions."""
        balance, equity, ticks, positions, statuses, correlations, reverse = (
            _parse_paper_broker_state(state)
        )
        with self._lock:
            self._balance = balance
            self._equity = equity
            self._latest_ticks = ticks
            self._positions = positions
            self._statuses = statuses
            self._correlations = correlations
            self._client_by_broker_id = reverse
            self._close_events = []
            self._cost_models = {}
            self._connected = False
            self._subscriptions = set()

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
            specification = self._valuation_engine.catalog.specification(symbol)
            if float(model.pip_size) != float(specification.pip_size):
                raise ValueError("cost pip size conflicts with instrument specification")
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
        prepared: list[
            tuple[
                str,
                CloseReason | None,
                float,
                float,
                float,
                float,
                str,
            ]
        ] = []
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
            exit_fill = self._modeled_fill_locked(tick, position.side, entry=False)
            model = self._cost_model_locked(symbol)
            gross, commission, net, valuation_id = self._net_pnl_locked(
                position,
                exit_fill,
                model,
                tick.timestamp.astimezone(UTC),
            )
            prepared.append(
                (
                    position_id,
                    reason,
                    exit_fill,
                    gross,
                    commission,
                    net,
                    valuation_id,
                )
            )
        for position_id, reason, exit_fill, gross, commission, net, valuation_id in prepared:
            if reason is None:
                self._positions[position_id].position.unrealized_pnl = net
            else:
                self._apply_close_locked(
                    position_id,
                    tick,
                    reason,
                    exit_fill,
                    gross,
                    commission,
                    net,
                    valuation_id,
                )
        self._recompute_equity_locked()

    def _close_position_locked(
        self, position_id: str, tick: Tick, reason: CloseReason
    ) -> str:
        record = self._positions[position_id]
        position = record.position
        model = self._cost_model_locked(position.symbol)
        exit_fill = self._modeled_fill_locked(tick, position.side, entry=False)
        gross, commission, net, valuation_id = self._net_pnl_locked(
            position,
            exit_fill,
            model,
            tick.timestamp.astimezone(UTC),
        )
        self._apply_close_locked(
            position_id,
            tick,
            reason,
            exit_fill,
            gross,
            commission,
            net,
            valuation_id,
        )
        self._recompute_equity_locked()
        return f"paper-close::{position_id}"

    def _apply_close_locked(
        self,
        position_id: str,
        tick: Tick,
        reason: CloseReason,
        exit_fill: float,
        gross: float,
        commission: float,
        net: float,
        valuation_id: str,
    ) -> None:
        record = self._positions[position_id]
        position = record.position
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
                account_currency=self.account_currency,
                valuation_id=valuation_id,
                close_time=tick.timestamp.astimezone(UTC),
                reason=reason,
            )
        )

    def _net_pnl_locked(
        self,
        position: Position,
        exit_fill: float,
        model: CostModel,
        as_of: datetime,
    ) -> tuple[float, float, float, str]:
        specification = self._valuation_engine.catalog.specification(position.symbol)
        side = Decimal(position.side)
        price_move = Decimal(str(exit_fill)) - Decimal(str(position.entry_price))
        quote_currency_gross = float(
            side
            * price_move
            * Decimal(str(specification.contract_units_per_lot))
            * Decimal(str(position.size))
        )
        valuation = self._valuation_engine.pip_valuation(
            position.symbol,
            self.account_currency,
            as_of,
            self._conversion_quotes_locked(),
        )
        gross = Decimal(str(valuation.convert_signed(quote_currency_gross)))
        commission = Decimal(str(model.commission_cost(position.size)))
        net = gross - commission
        results = (float(gross), float(commission), float(net))
        if any(not math.isfinite(value) for value in results):
            raise ValueError("paper PnL must remain finite")
        return (*results, valuation.valuation_id)

    def _conversion_quotes_locked(self) -> tuple[ConversionQuote, ...]:
        return tuple(
            ConversionQuote(
                symbol,
                tick.bid,
                tick.ask,
                tick.timestamp,
                "paper-replay",
            )
            for symbol, tick in sorted(self._latest_ticks.items())
            if symbol in self._valuation_engine.catalog.symbols
        )

    def _margin_locked(self, equity: float) -> MarginResult:
        exposures = tuple(
            MarginExposure(
                item.position.symbol,
                item.position.size,
                item.position.side,
            )
            for item in self._positions.values()
        )
        as_of = max(
            (tick.timestamp.astimezone(UTC) for tick in self._latest_ticks.values()),
            default=datetime(1970, 1, 1, tzinfo=UTC),
        )
        return self._margin_model.calculate(
            exposures,
            equity=equity,
            as_of=as_of,
            valuation=self._valuation_engine,
            quotes=self._conversion_quotes_locked(),
        )

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


def _parse_paper_broker_state(state: Mapping[str, object]) -> tuple:
    if not isinstance(state, Mapping):
        raise ValueError("paper-broker state must be a mapping")
    balance, expected_equity = state.get("balance"), state.get("equity")
    if not _finite_number(balance) or not _finite_number(expected_equity):
        raise ValueError("invalid paper account state")
    ticks_raw, positions_raw = state.get("latest_ticks"), state.get("positions")
    statuses_raw, correlations_raw = state.get("statuses"), state.get("correlations")
    if not isinstance(ticks_raw, list) or not isinstance(positions_raw, list):
        raise ValueError("paper ticks and positions must be lists")
    if not isinstance(statuses_raw, Mapping) or not isinstance(correlations_raw, list):
        raise ValueError("paper order state is malformed")
    ticks: dict[str, Tick] = {}
    for raw in ticks_raw:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid persisted tick")
        if not isinstance(raw.get("symbol"), str) or not raw.get("symbol"):
            raise ValueError("invalid persisted tick")
        try:
            tick = Tick(
                str(raw["symbol"]),
                datetime.fromisoformat(str(raw["timestamp"])),
                float(raw["bid"]),
                float(raw["ask"]),
                float(raw["mid"]),
            )
            _validate_tick(tick)
        except Exception as exc:
            raise ValueError("invalid persisted tick") from exc
        symbol = _canonical_symbol(tick.symbol)
        if symbol in ticks:
            raise ValueError("duplicate persisted tick symbol")
        ticks[symbol] = tick
    positions: dict[str, _PaperPosition] = {}
    for raw in positions_raw:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid persisted position")
        if (
            not isinstance(raw.get("symbol"), str)
            or not raw.get("symbol")
            or isinstance(raw.get("side"), bool)
            or not isinstance(raw.get("side"), int)
            or not isinstance(raw.get("position_id"), str)
            or not isinstance(raw.get("client_entry_order_id"), str)
            or not isinstance(raw.get("broker_entry_order_id"), str)
        ):
            raise ValueError("invalid persisted position")
        try:
            position = Position(
                symbol=str(raw["symbol"]),
                side=int(raw["side"]),
                size=float(raw["size"]),
                entry_price=float(raw["entry_price"]),
                entry_time=datetime.fromisoformat(str(raw["entry_time"])),
                unrealized_pnl=float(raw["unrealized_pnl"]),
                position_id=str(raw["position_id"]),
            )
            client_id, broker_id = str(raw["client_entry_order_id"]), str(
                raw["broker_entry_order_id"]
            )
        except Exception as exc:
            raise ValueError("invalid persisted position") from exc
        if (
            not position.position_id
            or position.position_id in positions
            or not client_id
            or not broker_id
            or not _positive_finite(position.entry_price)
            or not math.isfinite(position.unrealized_pnl)
            or position.entry_time.tzinfo is None
            or any(
                value is not None and not _positive_finite(value)
                for value in (raw.get("sl_price"), raw.get("tp_price"))
            )
        ):
            raise ValueError("invalid persisted position")
        positions[position.position_id] = _PaperPosition(
            position,
            client_id,
            broker_id,
            raw.get("sl_price"),
            raw.get("tp_price"),
        )
    statuses: dict[str, OrderStatus] = {}
    try:
        for key, value in statuses_raw.items():
            if not isinstance(key, str) or not key or key in statuses:
                raise ValueError
            statuses[key] = OrderStatus(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid persisted statuses") from exc
    correlations: dict[str, OrderCorrelation] = {}
    reverse: dict[str, str] = {}
    for raw in correlations_raw:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid persisted correlation")
        if any(
            not isinstance(raw.get(key), str) or not raw.get(key)
            for key in ("client_order_id", "broker_order_id", "position_id")
        ):
            raise ValueError("invalid persisted correlation")
        try:
            item = OrderCorrelation(
                str(raw["client_order_id"]),
                str(raw["broker_order_id"]),
                str(raw["position_id"]),
            )
        except KeyError as exc:
            raise ValueError("invalid persisted correlation") from exc
        if (
            not all((item.client_order_id, item.broker_order_id, item.position_id))
            or item.client_order_id in correlations
            or item.broker_order_id in reverse
            or item.broker_order_id not in statuses
        ):
            raise ValueError("invalid persisted correlation")
        correlations[item.client_order_id] = item
        reverse[item.broker_order_id] = item.client_order_id
    for item in positions.values():
        correlation = correlations.get(item.client_entry_order_id)
        if (
            correlation is None
            or correlation.broker_order_id != item.broker_entry_order_id
            or correlation.position_id != item.position.position_id
            or item.position.symbol not in ticks
        ):
            raise ValueError("persisted position correlation is inconsistent")
    equity = float(Decimal(str(balance)) + sum(
        (Decimal(str(item.position.unrealized_pnl)) for item in positions.values()),
        start=Decimal(0),
    ))
    if not math.isfinite(equity) or equity != float(expected_equity):
        raise ValueError("persisted paper equity is inconsistent")
    return float(balance), equity, ticks, positions, statuses, correlations, reverse


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


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
