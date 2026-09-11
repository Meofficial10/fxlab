"""One explicitly authorized, audited MT5 demo smoke-order round trip."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from .event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
)
from .mt5_demo_preflight import _load_mt5

MT5_DEMO_SMOKE_CONFIRMATION = "I_AUTHORIZE_MT5_DEMO_ORDER_SEND_AND_CLOSE"
MT5_DEMO_SMOKE_SYMBOL = "EURUSD"
MT5_DEMO_SMOKE_SIDE = "buy"
MT5_DEMO_SMOKE_MAGIC = 0x46584C42
MT5_DEMO_SMOKE_MAX_QUOTE_AGE = timedelta(seconds=5)
MT5_DEMO_SMOKE_POLL_TIMEOUT = timedelta(seconds=5)
MT5_DEMO_SMOKE_POLL_INTERVAL = timedelta(milliseconds=50)
_SYMBOL_FILLING_FOK_FLAG = 1
_SYMBOL_FILLING_IOC_FLAG = 2


@dataclass(frozen=True, slots=True)
class ObservedMt5Tick:
    raw_tick: object
    initial_tick_time_msc: int
    fresh_tick_time_msc: int
    observed_at_utc: datetime
    observed_at_monotonic: float
    raw_tick_clock_delta_seconds: float
    bid: float
    ask: float

    @property
    def time_msc(self) -> int:
        return self.fresh_tick_time_msc

    def assert_locally_fresh(
        self,
        maximum_age: timedelta,
        *,
        current_monotonic: float,
    ) -> float:
        age_seconds = current_monotonic - self.observed_at_monotonic
        if age_seconds < 0.0 or age_seconds > maximum_age.total_seconds():
            raise RuntimeError("mt5_smoke_quote_invalid")
        return age_seconds


@dataclass(frozen=True, slots=True)
class Mt5DemoSmokeResult:
    status: str
    account: str
    symbol: str
    side: str
    volume: float
    entry_order_id: str
    entry_deal_id: str
    position_id: str
    close_order_id: str
    close_deal_id: str


@dataclass(slots=True)
class Mt5DemoSmokeOrder:
    """Run one fixed BUY/minimum-volume demo entry and correlated close."""

    api: object = field(default_factory=_load_mt5, repr=False)
    clock: object = field(default=lambda: datetime.now(UTC), repr=False)
    monotonic: object = field(default=time.monotonic, repr=False)
    sleeper: object = field(default=time.sleep, repr=False)
    max_quote_age: timedelta = MT5_DEMO_SMOKE_MAX_QUOTE_AGE
    poll_timeout: timedelta = MT5_DEMO_SMOKE_POLL_TIMEOUT
    poll_interval: timedelta = MT5_DEMO_SMOKE_POLL_INTERVAL

    def run(
        self,
        *,
        confirmation: str,
        ledger: EventLedger,
    ) -> Mt5DemoSmokeResult:
        if confirmation != MT5_DEMO_SMOKE_CONFIRMATION:
            raise ValueError("mt5_demo_smoke_confirmation_required")
        if not isinstance(ledger, EventLedger) or ledger.durable_store is None:
            raise ValueError("mt5_durable_audit_required")

        initialized = False
        active_failure = False
        try:
            initialize = getattr(self.api, "initialize", None)
            if not callable(initialize) or initialize() is not True:
                raise RuntimeError("mt5_initialize_failed")
            initialized = True

            terminal, account = self._verified_mutation_authority()
            masked_account = _masked_account(account)
            correlation = EventCorrelation(client_order_id=_smoke_comment(ledger.session_id))
            self._audit(
                ledger,
                AuditEventType.ACCOUNT_OBSERVED,
                correlation,
                {
                    "environment": "demo",
                    "account": masked_account,
                    "currency": "USD",
                    "hedging_enabled": True,
                    "account_trading_enabled": True,
                    "expert_trading_enabled": True,
                    "terminal_trading_enabled": True,
                },
            )
            self._audit(
                ledger,
                AuditEventType.OPERATOR_CONTROL_ACTION,
                correlation,
                {"action": "authorized_demo_smoke_order", "confirmation": "accepted"},
            )

            metadata = self._validated_symbol()
            volume = _minimum_volume(metadata)
            filling = _order_filling(self.api, metadata)
            self._require_clean_symbol_state()
            entry_tick = self._observe_tick()
            entry_latency = entry_tick.assert_locally_fresh(
                self.max_quote_age, current_monotonic=self._monotonic_now()
            )
            stop_loss = _protective_stop(metadata, entry_tick.bid)
            self._audit(
                ledger,
                AuditEventType.BROKER_CAPABILITIES_BOUND,
                correlation,
                {
                    "symbol": MT5_DEMO_SMOKE_SYMBOL,
                    "side": MT5_DEMO_SMOKE_SIDE,
                    "volume": volume,
                    "stop_loss": stop_loss,
                    "symbol_trade_mode": metadata.trade_mode,
                    "initial_tick_time_msc": entry_tick.initial_tick_time_msc,
                    "fresh_tick_time_msc": entry_tick.fresh_tick_time_msc,
                    "local_observation_latency_seconds": round(entry_latency, 4),
                    "raw_tick_clock_delta_seconds": entry_tick.raw_tick_clock_delta_seconds,
                },
            )

            # The demo/account/terminal authority is checked again immediately before
            # each mutation. No result of the earlier preflight is trusted for mutation.
            self._verified_mutation_authority()
            entry_request = {
                "action": self.api.TRADE_ACTION_DEAL,
                "symbol": MT5_DEMO_SMOKE_SYMBOL,
                "volume": volume,
                "type": self.api.ORDER_TYPE_BUY,
                "price": entry_tick.ask,
                "sl": stop_loss,
                "magic": MT5_DEMO_SMOKE_MAGIC,
                "comment": correlation.client_order_id,
                "type_time": self.api.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            self._audit(
                ledger,
                AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
                correlation,
                {"phase": "entry", "symbol": MT5_DEMO_SMOKE_SYMBOL, "volume": volume},
            )
            entry_result = self._send_once(
                entry_request,
                ledger=ledger,
                correlation=correlation,
                phase="entry",
            )
            entry_order, entry_deal = _accepted_result(
                self.api, entry_result, volume, phase="entry"
            )
            entry_correlation = EventCorrelation(
                client_order_id=correlation.client_order_id,
                broker_order_id=str(entry_order),
            )
            self._audit(
                ledger,
                AuditEventType.ORDER_SUBMITTED,
                entry_correlation,
                {"phase": "entry", "broker_result": "accepted"},
                mutation_phase="entry",
            )
            self._audit(
                ledger,
                AuditEventType.ORDER_FILLED,
                entry_correlation,
                {"phase": "entry", "deal_id": str(entry_deal)},
                mutation_phase="entry",
            )
            position = self._correlated_position(
                entry_order=entry_order,
                volume=volume,
                stop_loss=stop_loss,
                comment=correlation.client_order_id or "",
            )
            position_ticket = _positive_int(getattr(position, "ticket", None))
            if position_ticket is None:
                raise RuntimeError("mt5_entry_reconciliation_required")
            position_correlation = EventCorrelation(
                client_order_id=correlation.client_order_id,
                broker_order_id=str(entry_order),
                position_id=str(position_ticket),
            )
            self._audit(
                ledger,
                AuditEventType.POSITION_OPENED,
                position_correlation,
                {"symbol": MT5_DEMO_SMOKE_SYMBOL, "volume": volume},
                mutation_phase="entry",
            )

            self._verified_mutation_authority()
            close_tick = self._observe_tick()
            close_tick.assert_locally_fresh(
                self.max_quote_age, current_monotonic=self._monotonic_now()
            )
            close_request = {
                "action": self.api.TRADE_ACTION_DEAL,
                "symbol": MT5_DEMO_SMOKE_SYMBOL,
                "volume": volume,
                "type": self.api.ORDER_TYPE_SELL,
                "position": position_ticket,
                "price": close_tick.bid,
                "magic": MT5_DEMO_SMOKE_MAGIC,
                "comment": correlation.client_order_id,
                "type_time": self.api.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            self._audit(
                ledger,
                AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
                position_correlation,
                {"phase": "close", "position_id": str(position_ticket)},
            )
            close_result = self._send_once(
                close_request,
                ledger=ledger,
                correlation=position_correlation,
                phase="close",
            )
            close_order, close_deal = _accepted_result(
                self.api, close_result, volume, phase="close"
            )
            close_result_correlation = EventCorrelation(
                client_order_id=correlation.client_order_id,
                broker_order_id=str(entry_order),
                position_id=str(position_ticket),
                close_order_id=str(close_order),
            )
            self._audit(
                ledger,
                AuditEventType.ORDER_SUBMITTED,
                close_result_correlation,
                {"phase": "close", "broker_result": "accepted"},
                mutation_phase="close",
            )
            self._audit(
                ledger,
                AuditEventType.ORDER_FILLED,
                close_result_correlation,
                {"phase": "close", "deal_id": str(close_deal)},
                mutation_phase="close",
            )
            closed = self._positions_get(ticket=position_ticket)
            if closed:
                raise RuntimeError("mt5_close_reconciliation_required")
            close_correlation = EventCorrelation(
                client_order_id=correlation.client_order_id,
                broker_order_id=str(entry_order),
                position_id=str(position_ticket),
                close_order_id=str(close_order),
            )
            self._audit(
                ledger,
                AuditEventType.POSITION_CLOSED,
                close_correlation,
                {"symbol": MT5_DEMO_SMOKE_SYMBOL, "close_deal_id": str(close_deal)},
                mutation_phase="close",
            )
            return Mt5DemoSmokeResult(
                status="successful_round_trip",
                account=masked_account,
                symbol=MT5_DEMO_SMOKE_SYMBOL,
                side=MT5_DEMO_SMOKE_SIDE,
                volume=volume,
                entry_order_id=str(entry_order),
                entry_deal_id=str(entry_deal),
                position_id=str(position_ticket),
                close_order_id=str(close_order),
                close_deal_id=str(close_deal),
            )
        except BaseException:
            active_failure = True
            raise
        finally:
            if initialized:
                shutdown = getattr(self.api, "shutdown", None)
                try:
                    if callable(shutdown):
                        shutdown()
                    elif not active_failure:
                        raise RuntimeError("mt5_shutdown_failed")
                except Exception:
                    if not active_failure:
                        raise RuntimeError("mt5_shutdown_failed") from None

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

    def _query_tick(
        self, symbol: str = MT5_DEMO_SMOKE_SYMBOL
    ) -> tuple[object, int, float, float]:
        method = getattr(self.api, "symbol_info_tick", None)
        if not callable(method):
            raise RuntimeError("mt5_smoke_quote_invalid")
        try:
            tick = method(symbol)
        except Exception:
            raise RuntimeError("mt5_smoke_quote_invalid") from None
        if tick is None:
            raise RuntimeError("mt5_smoke_quote_invalid")
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
            raise RuntimeError("mt5_smoke_quote_invalid")
        return tick, time_msc, bid, ask

    def _observe_tick(
        self, symbol: str = MT5_DEMO_SMOKE_SYMBOL
    ) -> ObservedMt5Tick:
        _, initial_time_msc, _, _ = self._query_tick(symbol)
        start_mono = self._monotonic_now()
        deadline = start_mono + self.poll_timeout.total_seconds()
        interval_seconds = self.poll_interval.total_seconds()

        while True:
            self._sleep(interval_seconds)
            current_mono = self._monotonic_now()
            if current_mono > deadline:
                raise RuntimeError("mt5_smoke_quote_invalid")

            tick, tick_msc, bid, ask = self._query_tick(symbol)
            if tick_msc < initial_time_msc:
                raise RuntimeError("mt5_smoke_quote_invalid")
            if tick_msc > initial_time_msc:
                observed_utc = self._now()
                try:
                    tick_time_utc = datetime.fromtimestamp(tick_msc / 1000.0, tz=UTC)
                except (OSError, OverflowError, ValueError):
                    raise RuntimeError("mt5_smoke_quote_invalid") from None
                raw_delta = (tick_time_utc - observed_utc).total_seconds()
                return ObservedMt5Tick(
                    raw_tick=tick,
                    initial_tick_time_msc=initial_time_msc,
                    fresh_tick_time_msc=tick_msc,
                    observed_at_utc=observed_utc,
                    observed_at_monotonic=current_mono,
                    raw_tick_clock_delta_seconds=raw_delta,
                    bid=bid,
                    ask=ask,
                )
            if current_mono >= deadline:
                raise RuntimeError("mt5_smoke_quote_invalid")

    def _verified_mutation_authority(self) -> tuple[object, object]:
        terminal = _call_no_args(self.api, "terminal_info", "mt5_mutation_not_permitted")
        account = _call_no_args(self.api, "account_info", "mt5_demo_account_required")
        demo_mode = getattr(self.api, "ACCOUNT_TRADE_MODE_DEMO", None)
        hedging_mode = getattr(self.api, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", None)
        if demo_mode is None or getattr(account, "trade_mode", None) != demo_mode:
            raise RuntimeError("mt5_demo_account_required")
        if (
            getattr(terminal, "connected", None) is not True
            or getattr(terminal, "trade_allowed", None) is not True
            or getattr(account, "trade_allowed", None) is not True
            or getattr(account, "trade_expert", None) is not True
        ):
            raise RuntimeError("mt5_mutation_not_permitted")
        if (
            getattr(account, "currency", None) != "USD"
            or hedging_mode is None
            or getattr(account, "margin_mode", None) != hedging_mode
        ):
            raise RuntimeError("mt5_account_incompatible")
        _masked_account(account)
        return terminal, account

    def _validated_symbol(self) -> object:
        method = getattr(self.api, "symbol_info", None)
        if not callable(method):
            raise RuntimeError("mt5_smoke_symbol_invalid")
        try:
            metadata = method(MT5_DEMO_SMOKE_SYMBOL)
        except Exception:
            raise RuntimeError("mt5_smoke_symbol_invalid") from None
        full_mode = getattr(self.api, "SYMBOL_TRADE_MODE_FULL", None)
        if (
            metadata is None
            or getattr(metadata, "name", None) != MT5_DEMO_SMOKE_SYMBOL
            or getattr(metadata, "visible", None) is not True
            or full_mode is None
            or getattr(metadata, "trade_mode", None) != full_mode
        ):
            raise RuntimeError("mt5_smoke_symbol_invalid")
        return metadata

    def _require_clean_symbol_state(self) -> None:
        positions = self._positions_get(symbol=MT5_DEMO_SMOKE_SYMBOL)
        orders = _query(
            self.api,
            "orders_get",
            "mt5_smoke_state_not_clean",
            symbol=MT5_DEMO_SMOKE_SYMBOL,
        )
        if positions or orders:
            raise RuntimeError("mt5_smoke_state_not_clean")

    def _positions_get(self, **query: object) -> tuple[object, ...]:
        return _query(self.api, "positions_get", "mt5_entry_reconciliation_required", **query)

    def _correlated_position(
        self, *, entry_order: int, volume: float, stop_loss: float, comment: str
    ) -> object:
        positions = self._positions_get(symbol=MT5_DEMO_SMOKE_SYMBOL)
        if len(positions) != 1:
            raise RuntimeError("mt5_entry_reconciliation_required")
        position = positions[0]
        if (
            getattr(position, "symbol", None) != MT5_DEMO_SMOKE_SYMBOL
            or getattr(position, "type", None) != getattr(self.api, "POSITION_TYPE_BUY", None)
            or getattr(position, "magic", None) != MT5_DEMO_SMOKE_MAGIC
            or getattr(position, "comment", None) != comment
            or _positive_int(getattr(position, "identifier", None)) != entry_order
            or not _same_number(getattr(position, "volume", None), volume)
            or not _same_number(getattr(position, "sl", None), stop_loss)
        ):
            raise RuntimeError("mt5_entry_reconciliation_required")
        return position

    def _send_once(
        self,
        request: dict[str, object],
        *,
        ledger: EventLedger,
        correlation: EventCorrelation,
        phase: str,
    ) -> object:
        method = getattr(self.api, "order_send", None)
        if not callable(method):
            raise RuntimeError(f"mt5_{phase}_reconciliation_required")
        try:
            result = method(request)
        except Exception:
            self._audit_indeterminate(ledger, correlation, phase)
            raise RuntimeError(f"mt5_{phase}_reconciliation_required") from None
        if result is None or not isinstance(getattr(result, "retcode", None), int):
            self._audit_indeterminate(ledger, correlation, phase)
            raise RuntimeError(f"mt5_{phase}_reconciliation_required")
        if result.retcode != getattr(self.api, "TRADE_RETCODE_DONE", None):
            self._audit(
                ledger,
                AuditEventType.ORDER_REJECTED,
                correlation,
                {"phase": phase, "broker_result": "rejected"},
                mutation_phase=phase,
            )
            raise RuntimeError(f"mt5_{phase}_broker_rejected")
        return result

    def _audit_indeterminate(
        self, ledger: EventLedger, correlation: EventCorrelation, phase: str
    ) -> None:
        try:
            self._audit(
                ledger,
                AuditEventType.ORDER_SUBMISSION_INDETERMINATE,
                correlation,
                {"phase": phase, "result": "reconciliation_required"},
            )
        except Exception:
            pass

    def _audit(
        self,
        ledger: EventLedger,
        event_type: AuditEventType,
        correlation: EventCorrelation,
        payload: dict[str, object],
        *,
        mutation_phase: str | None = None,
    ) -> None:
        try:
            ledger.append(
                event_type,
                occurred_at=ledger.now(),
                component=AuditComponent.BROKER_ADAPTER,
                correlation=correlation,
                payload=payload,
            )
        except Exception:
            if mutation_phase is not None:
                raise RuntimeError(
                    f"mt5_{mutation_phase}_reconciliation_required"
                ) from None
            raise RuntimeError("mt5_durable_audit_failed") from None


def _call_no_args(api: object, name: str, reason: str) -> object:
    method = getattr(api, name, None)
    if not callable(method):
        raise RuntimeError(reason)
    try:
        result = method()
    except Exception:
        raise RuntimeError(reason) from None
    if result is None:
        raise RuntimeError(reason)
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


def _masked_account(account: object) -> str:
    login = getattr(account, "login", None)
    if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
        raise RuntimeError("mt5_account_incompatible")
    return f"****{str(login)[-4:]}"


def _smoke_comment(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"fxlab-demo-{digest}"


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


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
        raise RuntimeError("mt5_smoke_volume_invalid")
    return float(minimum)


def _order_filling(api: object, metadata: object) -> int:
    mode = getattr(metadata, "filling_mode", None)
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise RuntimeError("mt5_smoke_symbol_invalid")
    order_ioc = getattr(api, "ORDER_FILLING_IOC", None)
    order_fok = getattr(api, "ORDER_FILLING_FOK", None)
    if mode & _SYMBOL_FILLING_IOC_FLAG and _plain_int(order_ioc):
        return order_ioc
    if mode & _SYMBOL_FILLING_FOK_FLAG and _plain_int(order_fok):
        return order_fok
    raise RuntimeError("mt5_smoke_symbol_invalid")


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _protective_stop(metadata: object, bid: object) -> float:
    point = _decimal(getattr(metadata, "point", None))
    bid_value = _decimal(bid)
    digits = getattr(metadata, "digits", None)
    level = getattr(metadata, "trade_stops_level", None)
    if (
        point is None
        or point <= 0
        or bid_value is None
        or bid_value <= 0
        or isinstance(digits, bool)
        or not isinstance(digits, int)
        or not 0 <= digits <= 10
        or isinstance(level, bool)
        or not isinstance(level, int)
        or level < 0
    ):
        raise RuntimeError("mt5_smoke_sl_invalid")
    quantum = Decimal(1).scaleb(-digits)
    stop = (bid_value - point * (level + 1)).quantize(quantum, rounding=ROUND_FLOOR)
    if stop <= 0 or stop >= bid_value or bid_value - stop <= point * level:
        raise RuntimeError("mt5_smoke_sl_invalid")
    return float(stop)


def _accepted_result(api: object, result: object, volume: float, *, phase: str) -> tuple[int, int]:
    retcode = getattr(result, "retcode", None)
    if retcode != getattr(api, "TRADE_RETCODE_DONE", None):
        raise RuntimeError(f"mt5_{phase}_broker_rejected")
    order_id = _positive_int(getattr(result, "order", None))
    deal_id = _positive_int(getattr(result, "deal", None))
    if (
        order_id is None
        or deal_id is None
        or not _same_number(getattr(result, "volume", None), volume)
        or _positive_float(getattr(result, "price", None)) is None
    ):
        raise RuntimeError(f"mt5_{phase}_reconciliation_required")
    return order_id, deal_id


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
