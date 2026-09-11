"""MetaTrader 5 Demo broker adapter (Phase 1C)."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from threading import Lock

import pandas as pd

from .broker import (
    AccountInfo,
    BrokerMutationPhase,
    BrokerOrderRejected,
    BrokerPreSubmissionRejected,
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
from .mt5_demo_preflight import _load_mt5
from .valuation import FxInstrumentCatalog, FxValuationEngine, InstrumentSpec, PipValuation

MT5_DEMO_SYMBOL = "EURUSD"
MT5_DEMO_MAGIC = 0x46584C42
MT5_DEMO_MAX_QUOTE_AGE = timedelta(seconds=5)
MT5_DEMO_POLL_TIMEOUT = timedelta(seconds=5)
MT5_DEMO_POLL_INTERVAL = timedelta(milliseconds=50)
_SYMBOL_FILLING_FOK_FLAG = 1
_SYMBOL_FILLING_IOC_FLAG = 2

_MT5_DEMO_CATALOG = FxInstrumentCatalog(
    (InstrumentSpec("EURUSD", "fx", "EUR", "USD", 0.0001, 100_000, "1"),)
)
_MT5_DEMO_VALUATION = FxValuationEngine(_MT5_DEMO_CATALOG, max_age=timedelta(seconds=5))


@dataclass(frozen=True, slots=True)
class _Mt5DemoPipResolver:
    catalog: FxInstrumentCatalog = _MT5_DEMO_CATALOG

    def pip_size_for(self, symbol: str) -> float:
        return self.catalog.specification(symbol).pip_size


_MT5_DEMO_RESOLVER = _Mt5DemoPipResolver()

_MT5_DEMO_DESCRIPTOR = BrokerDescriptor(
    broker_id="mt5-pepperstone-demo",
    implementation_version="1",
    environment=BrokerEnvironment.DEMO,
    capabilities=frozenset(
        {
            BrokerCapability.MARKET_ORDERS,
            BrokerCapability.NATIVE_SL_TP,
            BrokerCapability.HEDGING,
        }
    ),
    deterministic=False,
)


@dataclass(slots=True)
class Mt5DemoBroker:
    """Explicitly constrained MT5 DEMO broker adapter for EURUSD execution."""

    api: object = field(default_factory=_load_mt5, repr=False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    sleeper: Callable[[float], None] = field(default=time.sleep, repr=False)
    max_quote_age: timedelta = MT5_DEMO_MAX_QUOTE_AGE
    poll_timeout: timedelta = MT5_DEMO_POLL_TIMEOUT
    poll_interval: timedelta = MT5_DEMO_POLL_INTERVAL

    _connected: bool = field(default=False, init=False, repr=False)
    _subscriptions: set[str] = field(default_factory=set, init=False, repr=False)
    _orders: dict[str, dict[str, object]] = field(default_factory=dict, init=False, repr=False)
    _positions: dict[str, dict[str, object]] = field(default_factory=dict, init=False, repr=False)
    _latest_tick: Tick | None = field(default=None, init=False, repr=False)
    _latest_tick_mono: float | None = field(default=None, init=False, repr=False)
    _latest_tick_msc: int | None = field(default=None, init=False, repr=False)
    _mutation_phase: BrokerMutationPhase = field(
        default=BrokerMutationPhase.PRE_MUTATION, init=False, repr=False
    )
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def broker_descriptor(self) -> BrokerDescriptor:
        return _MT5_DEMO_DESCRIPTOR

    @property
    def mutation_phase(self) -> BrokerMutationPhase:
        with self._lock:
            return self._mutation_phase

    def connect(self) -> None:
        with self._lock:
            self._connected = False
            self._subscriptions.clear()
            self._latest_tick = None
            self._latest_tick_mono = None
            self._latest_tick_msc = None
        initialize = getattr(self.api, "initialize", None)
        if not callable(initialize) or initialize() is not True:
            raise RuntimeError("mt5_initialize_failed")
        _verified_demo_authority(self.api)
        _validated_symbol(self.api)
        with self._lock:
            self._connected = True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._subscriptions.clear()
            self._latest_tick = None
            self._latest_tick_mono = None
            self._latest_tick_msc = None
        shutdown = getattr(self.api, "shutdown", None)
        try:
            if callable(shutdown):
                shutdown()
        except Exception:
            pass

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def subscribe_market_data(self, symbols: list[str]) -> None:
        if not symbols or any(s != MT5_DEMO_SYMBOL for s in symbols):
            raise ValueError("unsupported_mt5_symbol")
        with self._lock:
            if not self._connected:
                raise RuntimeError("mt5_not_connected")
            self._subscriptions.add(MT5_DEMO_SYMBOL)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        if symbol != MT5_DEMO_SYMBOL:
            raise ValueError("unsupported_mt5_symbol")
        with self._lock:
            if not self._connected:
                raise BrokerPreSubmissionRejected("mt5_not_connected")
            if MT5_DEMO_SYMBOL not in self._subscriptions:
                raise BrokerPreSubmissionRejected("mt5_symbol_not_subscribed")
            if self._latest_tick is not None and self._latest_tick_mono is not None:
                current_mono = self._monotonic_now()
                elapsed = current_mono - self._latest_tick_mono
                if 0.0 <= elapsed <= self.max_quote_age.total_seconds():
                    return self._latest_tick

        _, initial_msc, _, _ = self._query_tick(symbol)
        start_mono = self._monotonic_now()
        deadline = start_mono + self.poll_timeout.total_seconds()
        interval_seconds = self.poll_interval.total_seconds()

        while True:
            self._sleep(interval_seconds)
            current_mono = self._monotonic_now()
            if current_mono > deadline:
                raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")

            _, tick_msc, bid, ask = self._query_tick(symbol)
            if tick_msc < initial_msc:
                raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")
            if tick_msc > initial_msc:
                observed_utc = self._now()
                elapsed = self._monotonic_now() - current_mono
                if elapsed < 0.0 or elapsed > self.max_quote_age.total_seconds():
                    raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")
                fresh_tick = Tick(
                    symbol=symbol,
                    timestamp=observed_utc,
                    bid=bid,
                    ask=ask,
                    mid=(bid + ask) / 2.0,
                )
                with self._lock:
                    self._latest_tick = fresh_tick
                    self._latest_tick_mono = current_mono
                    self._latest_tick_msc = tick_msc
                return fresh_tick
            if current_mono >= deadline:
                raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")

    def get_account_info(self) -> AccountInfo:
        with self._lock:
            if not self._connected:
                raise RuntimeError("mt5_not_connected")
        _, account = _verified_demo_authority(self.api)
        positions_raw = _query(
            self.api, "positions_get", "mt5_account_incompatible", symbol=MT5_DEMO_SYMBOL
        )
        open_positions: list[Position] = []
        for pos in positions_raw:
            ticket = _positive_int(getattr(pos, "ticket", None))
            if ticket is not None:
                pos_type = getattr(pos, "type", None)
                side = 1 if pos_type == getattr(self.api, "POSITION_TYPE_BUY", None) else -1
                size = float(getattr(pos, "volume", 0.0))
                price_open = float(getattr(pos, "price_open", 0.0))
                unrealized = float(getattr(pos, "profit", 0.0))
                pos_time = getattr(pos, "time", 0)
                try:
                    entry_time = datetime.fromtimestamp(pos_time, tz=UTC)
                except Exception:
                    entry_time = self._now()
                open_positions.append(
                    Position(
                        symbol=MT5_DEMO_SYMBOL,
                        side=side,
                        size=size,
                        entry_price=price_open,
                        entry_time=entry_time,
                        unrealized_pnl=unrealized,
                        position_id=str(ticket),
                    )
                )
        balance = float(getattr(account, "balance", 0.0))
        equity = float(getattr(account, "equity", 0.0))
        margin = float(getattr(account, "margin", 0.0))
        margin_free = float(getattr(account, "margin_free", 0.0))
        return AccountInfo(
            balance=balance,
            equity=equity,
            margin_used=margin,
            margin_available=margin_free,
            currency="USD",
            open_positions=open_positions,
        )

    def pip_valuation(
        self, symbol: str, account_currency: str, as_of: datetime
    ) -> PipValuation:
        if symbol != MT5_DEMO_SYMBOL:
            raise ValueError("unsupported_mt5_symbol")
        if account_currency != "USD":
            from .valuation import ValuationFailure

            raise ValuationFailure("account_currency_unsupported")
        with self._lock:
            if not self._connected:
                raise RuntimeError("mt5_not_connected")
        return _MT5_DEMO_VALUATION.pip_valuation(MT5_DEMO_SYMBOL, "USD", as_of, ())

    def submit_order(self, order: OrderRequest) -> str:
        with self._lock:
            if not self._connected:
                raise BrokerPreSubmissionRejected("mt5_not_connected")
            self._mutation_phase = BrokerMutationPhase.PRE_MUTATION
        if order.symbol != MT5_DEMO_SYMBOL:
            raise BrokerPreSubmissionRejected("unsupported_mt5_symbol")
        if order.order_type != "market":
            raise BrokerPreSubmissionRejected("unsupported_order_type")
        if order.side not in (1, -1):
            raise BrokerPreSubmissionRejected("invalid_order_side")

        _verified_demo_authority(self.api)
        _require_clean_symbol_state(self.api)
        metadata = _validated_symbol(self.api)
        volume = _minimum_volume(metadata)
        if not _same_number(order.size, volume):
            raise BrokerPreSubmissionRejected("mt5_smoke_volume_invalid")
        if order.sl_price is None:
            raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")

        tick = self.get_latest_tick(MT5_DEMO_SYMBOL)
        if tick is None:
            raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")

        if order.side == 1:
            if order.sl_price >= tick.bid:
                raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")
            order_type = getattr(self.api, "ORDER_TYPE_BUY", None)
            expected_pos_type = getattr(self.api, "POSITION_TYPE_BUY", 0)
            price = tick.ask
        else:
            if order.sl_price <= tick.ask:
                raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")
            order_type = getattr(self.api, "ORDER_TYPE_SELL", None)
            expected_pos_type = getattr(self.api, "POSITION_TYPE_SELL", 1)
            price = tick.bid

        order_comment = _mt5_comment(order.order_id)
        filling = _order_filling(self.api, metadata)
        request = {
            "action": getattr(self.api, "TRADE_ACTION_DEAL", 1),
            "symbol": MT5_DEMO_SYMBOL,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": float(order.sl_price),
            "tp": float(order.tp_price) if order.tp_price is not None else 0.0,
            "magic": MT5_DEMO_MAGIC,
            "comment": order_comment,
            "type_time": getattr(self.api, "ORDER_TIME_GTC", 0),
            "type_filling": filling,
        }

        method = getattr(self.api, "order_send", None)
        if not callable(method):
            raise RuntimeError("mt5_order_send_unavailable")
        with self._lock:
            self._mutation_phase = BrokerMutationPhase.MUTATION_ATTEMPTED
        try:
            result = method(request)
        except Exception:
            raise RuntimeError("mt5_order_send_exception") from None
        if result is None:
            raise RuntimeError("mt5_order_send_none")
        if not isinstance(getattr(result, "retcode", None), int):
            raise RuntimeError("mt5_order_send_malformed_result")
        if result.retcode != getattr(self.api, "TRADE_RETCODE_DONE", 10009):
            raise BrokerOrderRejected("mt5_entry_broker_rejected")

        order_id = _positive_int(getattr(result, "order", None))
        deal_id = _positive_int(getattr(result, "deal", None))
        if order_id is None:
            raise RuntimeError("mt5_entry_missing_order_id")
        if deal_id is None:
            raise RuntimeError("mt5_entry_missing_deal_id")

        with self._lock:
            self._mutation_phase = BrokerMutationPhase.POST_MUTATION_RECONCILIATION

        positions = _query(
            self.api, "positions_get", "mt5_position_query_failed", symbol=MT5_DEMO_SYMBOL
        )
        correlated_pos = [
            p
            for p in positions
            if getattr(p, "symbol", None) == MT5_DEMO_SYMBOL
            and getattr(p, "type", None) == expected_pos_type
            and getattr(p, "magic", None) == MT5_DEMO_MAGIC
            and getattr(p, "comment", None) == order_comment
            and _positive_int(getattr(p, "identifier", None)) == order_id
            and _same_number(getattr(p, "volume", None), volume)
            and _same_number(getattr(p, "sl", None), float(order.sl_price))
        ]
        if len(correlated_pos) == 0:
            # Active correlated position is absent.
            # Perform authoritative entry history reconciliation using exact broker identities.
            hist_orders = _query(
                self.api, "history_orders_get", "mt5_entry_history_query_failed", ticket=order_id
            )
            matching_orders = [
                o
                for o in hist_orders
                if _positive_int(getattr(o, "ticket", None)) == order_id
                and getattr(o, "symbol", None) == MT5_DEMO_SYMBOL
                and getattr(o, "magic", None) == MT5_DEMO_MAGIC
                and getattr(o, "comment", None) == order_comment
                and _same_number(getattr(o, "volume_initial", None), volume)
            ]
            if len(matching_orders) != 1:
                raise RuntimeError("mt5_entry_history_order_missing")

            entry_hist_order = matching_orders[0]
            pos_id = _positive_int(
                getattr(entry_hist_order, "position_id", getattr(entry_hist_order, "ticket", None))
            )
            if pos_id is None:
                raise RuntimeError("mt5_entry_history_missing_position_id")

            hist_deals = _query(
                self.api, "history_deals_get", "mt5_entry_history_query_failed", position=pos_id
            )
            deal_entry_in = getattr(self.api, "DEAL_ENTRY_IN", 0)
            deal_entry_out = getattr(self.api, "DEAL_ENTRY_OUT", 1)
            deal_reason_sl = getattr(self.api, "DEAL_REASON_SL", 4)
            deal_reason_tp = getattr(self.api, "DEAL_REASON_TP", 5)

            # Match entry deal
            matching_in_deals = [
                d
                for d in hist_deals
                if _positive_int(getattr(d, "ticket", None)) == deal_id
                and _positive_int(getattr(d, "order", None)) == order_id
                and _positive_int(getattr(d, "position_id", None)) == pos_id
                and getattr(d, "entry", None) == deal_entry_in
                and getattr(d, "symbol", None) == MT5_DEMO_SYMBOL
                and getattr(d, "magic", None) == MT5_DEMO_MAGIC
                and _same_number(getattr(d, "volume", None), volume)
            ]
            if len(matching_in_deals) != 1:
                raise RuntimeError("mt5_entry_history_in_deal_mismatch")

            # Match exit deals
            matching_out_deals = [
                d
                for d in hist_deals
                if _positive_int(getattr(d, "position_id", None)) == pos_id
                and getattr(d, "entry", None) == deal_entry_out
                and getattr(d, "symbol", None) == MT5_DEMO_SYMBOL
                and getattr(d, "magic", None) == MT5_DEMO_MAGIC
            ]
            if len(matching_out_deals) == 0:
                raise RuntimeError("mt5_entry_history_out_deal_missing")
            if len(matching_out_deals) > 1:
                raise RuntimeError("mt5_entry_history_multiple_out_deals")

            exit_deal = matching_out_deals[0]
            if not _same_number(getattr(exit_deal, "volume", None), volume):
                raise RuntimeError("mt5_entry_history_volume_mismatch")

            exit_reason_code = getattr(exit_deal, "reason", None)
            if exit_reason_code == deal_reason_sl:
                exit_reason = "SL"
            elif exit_reason_code == deal_reason_tp:
                exit_reason = "TP"
            else:
                raise RuntimeError("mt5_entry_history_unsupported_reason")

            exit_deal_id = _positive_int(getattr(exit_deal, "ticket", None))
            exit_order_id = _positive_int(getattr(exit_deal, "order", None))
            if exit_deal_id is None or exit_order_id is None:
                raise RuntimeError("mt5_entry_history_missing_exit_ids")

            # Verify final position absence on broker
            remaining = _query(
                self.api, "positions_get", "mt5_position_query_failed", ticket=pos_id
            )
            if len(remaining) != 0:
                raise RuntimeError("mt5_position_remaining_after_entry_exit")

            with self._lock:
                self._latest_tick = None
                self._latest_tick_mono = None
                self._latest_tick_msc = None
                self._orders[order.order_id] = {
                    "broker_order_id": str(order_id),
                    "deal_id": str(deal_id),
                    "position_id": str(pos_id),
                    "status": OrderStatus.FILLED,
                    "exit_reason": exit_reason,
                    "close_order_id": str(exit_order_id),
                    "close_deal_id": str(exit_deal_id),
                    "closed_at_entry_reconciliation": True,
                }
            return str(order_id)

        if len(correlated_pos) > 1:
            raise RuntimeError("mt5_position_correlation_multiple")

        pos_ticket = _positive_int(getattr(correlated_pos[0], "ticket", None))
        if pos_ticket is None:
            raise RuntimeError("mt5_position_ticket_mismatch")

        verified = _query(
            self.api, "positions_get", "mt5_position_ticket_query_failed", ticket=pos_ticket
        )
        if len(verified) != 1:
            raise RuntimeError("mt5_position_ticket_mismatch")

        with self._lock:
            self._latest_tick = None
            self._latest_tick_mono = None
            self._latest_tick_msc = None
            self._positions[str(pos_ticket)] = {
                "client_order_id": order.order_id,
                "order_comment": order_comment,
                "broker_order_id": str(order_id),
                "deal_id": str(deal_id),
                "volume": volume,
                "side": order.side,
                "sl": float(order.sl_price),
            }
            self._orders[order.order_id] = {
                "broker_order_id": str(order_id),
                "deal_id": str(deal_id),
                "position_id": str(pos_ticket),
                "status": OrderStatus.FILLED,
            }
        return str(order_id)

    def check_order(self, order: OrderRequest) -> dict[str, object]:
        """Read-only dry-run check using MetaTrader5.order_check without order_send."""
        with self._lock:
            if not self._connected:
                raise BrokerPreSubmissionRejected("mt5_not_connected")
        if order.symbol != MT5_DEMO_SYMBOL:
            raise BrokerPreSubmissionRejected("unsupported_mt5_symbol")
        if order.order_type != "market":
            raise BrokerPreSubmissionRejected("unsupported_order_type")
        if order.side not in (1, -1):
            raise BrokerPreSubmissionRejected("invalid_order_side")

        _verified_demo_authority(self.api)
        _require_clean_symbol_state(self.api)
        metadata = _validated_symbol(self.api)
        volume = _minimum_volume(metadata)
        if not _same_number(order.size, volume):
            raise BrokerPreSubmissionRejected("mt5_smoke_volume_invalid")
        if order.sl_price is None:
            raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")

        tick = self.get_latest_tick(MT5_DEMO_SYMBOL)
        if tick is None:
            raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")

        if order.side == 1:
            if order.sl_price >= tick.bid:
                raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")
            order_type = getattr(self.api, "ORDER_TYPE_BUY", None)
            price = tick.ask
        else:
            if order.sl_price <= tick.ask:
                raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")
            order_type = getattr(self.api, "ORDER_TYPE_SELL", None)
            price = tick.bid

        order_comment = _mt5_comment(order.order_id)
        filling = _order_filling(self.api, metadata)
        request = {
            "action": getattr(self.api, "TRADE_ACTION_DEAL", 1),
            "symbol": MT5_DEMO_SYMBOL,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": float(order.sl_price),
            "tp": float(order.tp_price) if order.tp_price is not None else 0.0,
            "magic": MT5_DEMO_MAGIC,
            "comment": order_comment,
            "type_time": getattr(self.api, "ORDER_TIME_GTC", 0),
            "type_filling": filling,
        }

        method = getattr(self.api, "order_check", None)
        if not callable(method):
            raise RuntimeError("mt5_order_check_unavailable")
        try:
            check_result = method(request)
        except Exception:
            raise RuntimeError("mt5_order_check_exception") from None
        if check_result is None:
            raise RuntimeError("mt5_order_check_none")

        retcode = getattr(check_result, "retcode", None)
        comment = getattr(check_result, "comment", None)
        return {
            "request_constructed": True,
            "symbol": MT5_DEMO_SYMBOL,
            "volume": volume,
            "side": order.side,
            "order_type": "BUY" if order.side == 1 else "SELL",
            "price": price,
            "sl_present": order.sl_price is not None,
            "filling_mode": filling,
            "retcode": retcode,
            "comment": str(comment) if comment is not None else "",
            "valid": retcode == 0 or retcode == getattr(self.api, "TRADE_RETCODE_DONE", 10009),
        }

    def close_position(self, position_id: str) -> tuple[str, str]:
        with self._lock:
            if not self._connected:
                raise RuntimeError("mt5_not_connected")
            self._mutation_phase = BrokerMutationPhase.PRE_MUTATION
            try:
                ticket_int = int(position_id)
            except (ValueError, TypeError):
                raise RuntimeError("mt5_close_untracked_position") from None
            pos_info = self._positions.get(position_id)
            if not isinstance(pos_info, dict):
                raise RuntimeError("mt5_close_untracked_position")

        _verified_demo_authority(self.api)
        positions = _query(
            self.api,
            "positions_get",
            "mt5_close_position_query_failed",
            ticket=ticket_int,
        )
        if len(positions) > 1:
            raise RuntimeError("mt5_close_position_multiple")

        if len(positions) == 0:
            # Position has disappeared from active positions.
            # Reconcile authoritatively from history.
            history_deals = _query(
                self.api,
                "history_deals_get",
                "mt5_close_history_query_failed",
                position=ticket_int,
            )
            deal_entry_out = getattr(self.api, "DEAL_ENTRY_OUT", 1)
            deal_reason_sl = getattr(self.api, "DEAL_REASON_SL", 4)
            deal_reason_tp = getattr(self.api, "DEAL_REASON_TP", 5)

            matching_deals = [
                d
                for d in history_deals
                if getattr(d, "position_id", None) == ticket_int
                and getattr(d, "entry", None) == deal_entry_out
                and getattr(d, "symbol", None) == MT5_DEMO_SYMBOL
                and getattr(d, "magic", None) == MT5_DEMO_MAGIC
            ]
            if len(matching_deals) == 0:
                raise RuntimeError("mt5_close_history_missing")
            if len(matching_deals) > 1:
                raise RuntimeError("mt5_close_history_multiple")

            closing_deal = matching_deals[0]
            deal_vol = getattr(closing_deal, "volume", None)
            if not _same_number(deal_vol, float(pos_info["volume"])):
                raise RuntimeError("mt5_close_history_volume_mismatch")

            deal_reason = getattr(closing_deal, "reason", None)
            if deal_reason == deal_reason_sl:
                exit_reason = "SL"
            elif deal_reason == deal_reason_tp:
                exit_reason = "TP"
            else:
                raise RuntimeError("mt5_close_history_unsupported_reason")

            close_deal_id = _positive_int(getattr(closing_deal, "ticket", None))
            close_order_id = _positive_int(getattr(closing_deal, "order", None))
            if close_deal_id is None:
                raise RuntimeError("mt5_close_missing_deal_id")
            if close_order_id is None:
                raise RuntimeError("mt5_close_missing_order_id")

            remaining = _query(
                self.api,
                "positions_get",
                "mt5_close_position_query_failed",
                ticket=ticket_int,
            )
            if len(remaining) != 0:
                raise RuntimeError("mt5_close_position_remaining")

            with self._lock:
                self._positions.pop(position_id, None)
                client_id = pos_info.get("client_order_id")
                if isinstance(client_id, str) and client_id in self._orders:
                    self._orders[client_id]["exit_reason"] = exit_reason
                    self._orders[client_id]["close_deal_id"] = str(close_deal_id)
                    self._orders[client_id]["close_order_id"] = str(close_order_id)
            return str(close_order_id), str(close_deal_id)

        position = positions[0]

        expected_side = pos_info["side"]
        if expected_side == 1:
            expected_pos_type = getattr(self.api, "POSITION_TYPE_BUY", 0)
            close_type = getattr(self.api, "ORDER_TYPE_SELL", 1)
        elif expected_side == -1:
            expected_pos_type = getattr(self.api, "POSITION_TYPE_SELL", 1)
            close_type = getattr(self.api, "ORDER_TYPE_BUY", 0)
        else:
            raise RuntimeError("mt5_close_position_mismatch")

        expected_comment = str(pos_info.get("order_comment", pos_info.get("client_order_id", "")))
        if (
            getattr(position, "symbol", None) != MT5_DEMO_SYMBOL
            or getattr(position, "magic", None) != MT5_DEMO_MAGIC
            or _positive_int(getattr(position, "ticket", None)) != ticket_int
            or getattr(position, "comment", None) != expected_comment
            or str(getattr(position, "identifier", None)) != pos_info["broker_order_id"]
            or getattr(position, "type", None) != expected_pos_type
            or not _same_number(getattr(position, "volume", None), float(pos_info["volume"]))
            or not _same_number(getattr(position, "sl", None), float(pos_info["sl"]))
        ):
            raise RuntimeError("mt5_close_position_mismatch")

        metadata = _validated_symbol(self.api)
        filling = _order_filling(self.api, metadata)
        tick = self.get_latest_tick(MT5_DEMO_SYMBOL)
        if tick is None:
            raise RuntimeError("mt5_smoke_quote_invalid")

        close_price = tick.bid if expected_side == 1 else tick.ask
        close_request = {
            "action": getattr(self.api, "TRADE_ACTION_DEAL", 1),
            "symbol": MT5_DEMO_SYMBOL,
            "volume": float(pos_info["volume"]),
            "type": close_type,
            "position": ticket_int,
            "price": close_price,
            "magic": MT5_DEMO_MAGIC,
            "comment": expected_comment,
            "type_time": getattr(self.api, "ORDER_TIME_GTC", 0),
            "type_filling": filling,
        }

        method = getattr(self.api, "order_send", None)
        if not callable(method):
            raise RuntimeError("mt5_close_order_send_unavailable")
        with self._lock:
            self._mutation_phase = BrokerMutationPhase.MUTATION_ATTEMPTED
        try:
            result = method(close_request)
        except Exception:
            raise RuntimeError("mt5_close_order_send_exception") from None
        if result is None:
            raise RuntimeError("mt5_close_order_send_none")
        if not isinstance(getattr(result, "retcode", None), int):
            raise RuntimeError("mt5_close_order_send_malformed_result")
        if result.retcode != getattr(self.api, "TRADE_RETCODE_DONE", 10009):
            raise BrokerOrderRejected("mt5_close_broker_rejected")

        close_order_id = _positive_int(getattr(result, "order", None))
        close_deal_id = _positive_int(getattr(result, "deal", None))
        if close_order_id is None:
            raise RuntimeError("mt5_close_missing_order_id")
        if close_deal_id is None:
            raise RuntimeError("mt5_close_missing_deal_id")

        with self._lock:
            self._mutation_phase = BrokerMutationPhase.POST_MUTATION_RECONCILIATION

        remaining = _query(
            self.api,
            "positions_get",
            "mt5_close_position_query_failed",
            ticket=ticket_int,
        )
        if remaining:
            raise RuntimeError("mt5_close_position_remaining")

        with self._lock:
            self._positions.pop(position_id, None)
        return str(close_order_id), str(close_deal_id)

    def get_order_status(self, order_id: str) -> dict[str, object]:
        with self._lock:
            record = self._orders.get(order_id)
            if record is not None:
                return dict(record)
        return {"status": OrderStatus.PENDING}

    def cancel_order(self, order_id: str) -> bool:
        raise RuntimeError("mt5_cancel_unsupported")

    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        return pd.DataFrame()

    def _now(self) -> datetime:
        if not callable(self.clock):
            raise RuntimeError("mt5_smoke_quote_invalid")
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("mt5_smoke_quote_invalid")
        return value.astimezone(UTC)

    def _monotonic_now(self) -> float:
        if callable(self.monotonic):
            return float(self.monotonic())
        return time.monotonic()

    def _sleep(self, seconds: float) -> None:
        if callable(self.sleeper):
            self.sleeper(seconds)
        else:
            time.sleep(seconds)

    def _query_tick(self, symbol: str) -> tuple[object, int, float, float]:
        method = getattr(self.api, "symbol_info_tick", None)
        if not callable(method):
            raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")
        try:
            tick = method(symbol)
        except Exception:
            raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid") from None
        if tick is None:
            raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")
        time_msc = getattr(tick, "time_msc", None)
        bid = _positive_float(getattr(tick, "bid", None))
        ask = _positive_float(getattr(tick, "ask", None))
        if (
            isinstance(time_msc, bool)
            or not isinstance(time_msc, int)
            or time_msc <= 0
            or bid is None
            or ask is None
            or ask < bid
        ):
            raise BrokerPreSubmissionRejected("mt5_smoke_quote_invalid")
        return tick, time_msc, bid, ask


def _verified_demo_authority(api: object) -> tuple[object, object]:
    terminal = _call_no_args(api, "terminal_info", "mt5_mutation_not_permitted")
    account = _call_no_args(api, "account_info", "mt5_demo_account_required")
    demo_mode = getattr(api, "ACCOUNT_TRADE_MODE_DEMO", None)
    hedging_mode = getattr(api, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", None)
    if demo_mode is None or getattr(account, "trade_mode", None) != demo_mode:
        raise BrokerPreSubmissionRejected("mt5_demo_account_required")
    if (
        getattr(terminal, "connected", None) is not True
        or getattr(terminal, "trade_allowed", None) is not True
        or getattr(account, "trade_allowed", None) is not True
        or getattr(account, "trade_expert", None) is not True
    ):
        raise BrokerPreSubmissionRejected("mt5_mutation_not_permitted")
    if (
        getattr(account, "currency", None) != "USD"
        or hedging_mode is None
        or getattr(account, "margin_mode", None) != hedging_mode
    ):
        raise BrokerPreSubmissionRejected("mt5_account_incompatible")
    return terminal, account


def _contract_size(metadata: object) -> Decimal:
    raw = getattr(metadata, "trade_contract_size", None)
    if raw is None:
        raw = getattr(metadata, "contract_size", None)
    dec = _decimal(raw)
    if dec is None or dec <= 0:
        raise BrokerPreSubmissionRejected("mt5_smoke_symbol_invalid")
    return dec


def _validated_symbol(api: object) -> object:
    method = getattr(api, "symbol_info", None)
    if not callable(method):
        raise BrokerPreSubmissionRejected("mt5_smoke_symbol_invalid")
    try:
        metadata = method(MT5_DEMO_SYMBOL)
    except Exception:
        raise BrokerPreSubmissionRejected("mt5_smoke_symbol_invalid") from None
    full_mode = getattr(api, "SYMBOL_TRADE_MODE_FULL", None)
    if (
        metadata is None
        or getattr(metadata, "name", None) != MT5_DEMO_SYMBOL
        or getattr(metadata, "visible", None) is not True
        or full_mode is None
        or getattr(metadata, "trade_mode", None) != full_mode
    ):
        raise BrokerPreSubmissionRejected("mt5_smoke_symbol_invalid")
    _contract_size(metadata)
    return metadata


def _require_clean_symbol_state(api: object) -> None:
    try:
        positions = _query(
            api,
            "positions_get",
            "mt5_state_query_failed",
            symbol=MT5_DEMO_SYMBOL,
        )
        orders = _query(
            api,
            "orders_get",
            "mt5_state_query_failed",
            symbol=MT5_DEMO_SYMBOL,
        )
    except Exception:
        raise BrokerPreSubmissionRejected("mt5_state_query_failed") from None
    if positions or orders:
        raise BrokerPreSubmissionRejected("mt5_state_not_clean")


def _call_no_args(api: object, name: str, reason: str) -> object:
    method = getattr(api, name, None)
    if not callable(method):
        raise BrokerPreSubmissionRejected(reason)
    try:
        result = method()
    except Exception:
        raise BrokerPreSubmissionRejected(reason) from None
    if result is None:
        raise BrokerPreSubmissionRejected(reason)
    return result


def _query(api: object, name: str, reason: str, **query: object) -> tuple[object, ...]:
    method = getattr(api, name, None)
    if not callable(method):
        raise RuntimeError(reason)
    try:
        result = method(**query)
    except Exception:
        raise RuntimeError(reason) from None
    if not isinstance(result, tuple):
        raise RuntimeError(reason)
    return result


def _order_filling(api: object, metadata: object) -> int:
    mode = getattr(metadata, "filling_mode", None)
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise BrokerPreSubmissionRejected("mt5_smoke_symbol_invalid")
    order_ioc = getattr(api, "ORDER_FILLING_IOC", None)
    order_fok = getattr(api, "ORDER_FILLING_FOK", None)
    if mode & _SYMBOL_FILLING_IOC_FLAG and _plain_int(order_ioc):
        return order_ioc
    if mode & _SYMBOL_FILLING_FOK_FLAG and _plain_int(order_fok):
        return order_fok
    raise BrokerPreSubmissionRejected("mt5_smoke_symbol_invalid")


def _minimum_volume(metadata: object) -> float:
    minimum = _decimal(getattr(metadata, "volume_min", None))
    step = _decimal(getattr(metadata, "volume_step", None))
    maximum = _decimal(getattr(metadata, "volume_max", None))
    if (
        minimum is None
        or step is None
        or maximum is None
        or minimum <= 0
        or step <= 0
        or maximum < minimum
        or minimum % step != 0
    ):
        raise BrokerPreSubmissionRejected("mt5_smoke_volume_invalid")
    return float(minimum)


def _protective_stop_buy(
    metadata: object,
    bid: object,
    ask: object,
    *,
    max_loss_usd: float | Decimal,
    volume: float | Decimal = 0.01,
    contract_size: float | Decimal | None = None,
) -> float:
    point = _decimal(getattr(metadata, "point", None))
    bid_value = _decimal(bid)
    ask_value = _decimal(ask)
    digits = getattr(metadata, "digits", None)
    level = getattr(metadata, "trade_stops_level", None)
    risk_dec = _decimal(max_loss_usd)
    vol_dec = _decimal(volume)
    contract_dec = (
        _decimal(contract_size)
        if contract_size is not None
        else _contract_size(metadata)
    )
    if (
        point is None
        or point <= 0
        or bid_value is None
        or bid_value <= 0
        or ask_value is None
        or ask_value <= 0
        or ask_value < bid_value
        or isinstance(digits, bool)
        or not isinstance(digits, int)
        or not 0 <= digits <= 10
        or isinstance(level, bool)
        or not isinstance(level, int)
        or level < 0
        or risk_dec is None
        or risk_dec <= 0
        or vol_dec is None
        or vol_dec <= 0
        or contract_dec is None
        or contract_dec <= 0
    ):
        raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")

    # Required broker minimum stop offset from trigger price (BID for BUY)
    min_points = max(level, 0) + 1
    min_stop_distance = point * min_points

    # Risk distance measured from expected entry (ASK for BUY)
    raw_offset = risk_dec / (contract_dec * vol_dec)
    quantum = Decimal(1).scaleb(-digits)
    # Quantize TOWARD entry (ROUND_CEILING) so modeled risk never exceeds max_loss_usd
    stop = (ask_value - raw_offset).quantize(quantum, rounding=ROUND_CEILING)

    # Prove modeled loss does not exceed explicit risk budget
    modeled_price_loss = ask_value - stop
    modeled_loss_usd = modeled_price_loss * contract_dec * vol_dec
    if modeled_loss_usd > risk_dec:
        raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")

    # Broker stop legality: SL must be at least min_stop_distance below current BID
    if stop <= 0 or bid_value - stop < min_stop_distance:
        raise BrokerPreSubmissionRejected("mt5_smoke_risk_budget_too_small")

    return float(stop)


def _protective_stop_sell(
    metadata: object,
    bid: object,
    ask: object,
    *,
    max_loss_usd: float | Decimal,
    volume: float | Decimal = 0.01,
    contract_size: float | Decimal | None = None,
) -> float:
    point = _decimal(getattr(metadata, "point", None))
    bid_value = _decimal(bid)
    ask_value = _decimal(ask)
    digits = getattr(metadata, "digits", None)
    level = getattr(metadata, "trade_stops_level", None)
    risk_dec = _decimal(max_loss_usd)
    vol_dec = _decimal(volume)
    contract_dec = (
        _decimal(contract_size)
        if contract_size is not None
        else _contract_size(metadata)
    )
    if (
        point is None
        or point <= 0
        or bid_value is None
        or bid_value <= 0
        or ask_value is None
        or ask_value <= 0
        or ask_value < bid_value
        or isinstance(digits, bool)
        or not isinstance(digits, int)
        or not 0 <= digits <= 10
        or isinstance(level, bool)
        or not isinstance(level, int)
        or level < 0
        or risk_dec is None
        or risk_dec <= 0
        or vol_dec is None
        or vol_dec <= 0
        or contract_dec is None
        or contract_dec <= 0
    ):
        raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")

    # Required broker minimum stop offset from trigger price (ASK for SELL)
    min_points = max(level, 0) + 1
    min_stop_distance = point * min_points

    # Risk distance measured from expected entry (BID for SELL)
    raw_offset = risk_dec / (contract_dec * vol_dec)
    quantum = Decimal(1).scaleb(-digits)
    # Quantize TOWARD entry (ROUND_FLOOR) so modeled risk never exceeds max_loss_usd
    stop = (bid_value + raw_offset).quantize(quantum, rounding=ROUND_FLOOR)

    # Prove modeled loss does not exceed explicit risk budget
    modeled_price_loss = stop - bid_value
    modeled_loss_usd = modeled_price_loss * contract_dec * vol_dec
    if modeled_loss_usd > risk_dec:
        raise BrokerPreSubmissionRejected("mt5_smoke_sl_invalid")

    # Broker stop legality: SL must be at least min_stop_distance above current ASK
    if stop - ask_value < min_stop_distance:
        raise BrokerPreSubmissionRejected("mt5_smoke_risk_budget_too_small")

    return float(stop)


def _mt5_comment(client_order_id: str) -> str:
    """Format client_order_id to fit MT5 Python API comment buffer limit (max 27 chars)."""
    if len(client_order_id) <= 27:
        return client_order_id
    hash_part = hashlib.sha256(client_order_id.encode("utf-8")).hexdigest()[:11]
    prefix = client_order_id[:15]
    return f"{prefix}_{hash_part}"


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _same_number(value: object, expected: float) -> bool:
    try:
        return math.isclose(float(value), expected, rel_tol=0.0, abs_tol=1e-12)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False
