"""Deterministic, fail-closed risk decisions for paper-trading execution intents.

The engine owns no broker and performs no execution. Its mutable controls are local to
one in-memory session: process restart loses kill-switch state, counters, peak equity,
consecutive losses, and approved identities. Phase 4 provides no durability guarantee.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from threading import Lock
from typing import Literal, Protocol, runtime_checkable

from ..execution.broker import AccountInfo, Position
from ..execution.signal_engine import SignalEvent

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


@runtime_checkable
class PipSizeResolver(Protocol):
    """Structural dependency for resolving a symbol's price units per pip."""

    def pip_size_for(self, symbol: str) -> float: ...


class KillSwitchReason(StrEnum):
    """Stable reasons that can latch a risk engine's session kill switch."""

    MAX_DRAWDOWN = "max_drawdown_breached"
    MAX_DAILY_LOSS = "max_daily_loss_breached"
    MAX_CONSECUTIVE_LOSSES = "max_consecutive_losses_breached"
    RUIN_THRESHOLD = "ruin_threshold_breached"
    POSITION_RECONCILIATION_FAILED = "position_sync_failed"
    MANUAL = "manual_shutdown"


@dataclass(frozen=True)
class RiskLimits:
    """Validated risk limits using whole percentage points (``1.0`` means 1%)."""

    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 5
    max_drawdown_pct: float = 20.0
    max_trades_per_day: int = 2
    starting_equity: float = 10.0
    ruin_threshold_pct: float = 50.0
    max_open_positions: int = 1
    max_exposure_per_symbol_lots: float = 1.0

    def __post_init__(self) -> None:
        percentage_fields = (
            "max_risk_per_trade_pct",
            "max_daily_loss_pct",
            "max_drawdown_pct",
            "ruin_threshold_pct",
        )
        for name in percentage_fields:
            value = getattr(self, name)
            if not _is_finite_number(value) or not 0 < float(value) <= 100:
                raise ValueError(f"{name} must be finite and in (0, 100]")

        count_fields = (
            "max_consecutive_losses",
            "max_trades_per_day",
            "max_open_positions",
        )
        for name in count_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an integer >= 1")

        if not _is_positive_finite(self.starting_equity):
            raise ValueError("starting_equity must be finite and > 0")
        if not _is_positive_finite(self.max_exposure_per_symbol_lots):
            raise ValueError("max_exposure_per_symbol_lots must be finite and > 0")


@dataclass(frozen=True)
class RiskRejection:
    """Structured, machine-readable rejection of a proposed risk approval."""

    reason: str
    message: str
    signal: SignalEvent | None
    order_id: str | None
    kill_switch_reason: KillSwitchReason | None
    approved: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")


@dataclass(frozen=True)
class RiskDecision:
    """Risk approval for caller-owned execution inputs; this is not an order."""

    signal: SignalEvent
    order_id: str
    size_lots: float
    entry_price: float
    sl_price: float
    tp_price: float | None
    pip_size: float
    stop_pips: float
    monetary_risk_budget: float
    modeled_monetary_risk: float
    approved_at: datetime
    approved: Literal[True] = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.signal, SignalEvent):
            raise ValueError("signal must be a SignalEvent")
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise ValueError("order_id must be a non-empty string")
        positive_fields = (
            "size_lots",
            "entry_price",
            "sl_price",
            "pip_size",
            "stop_pips",
            "monetary_risk_budget",
            "modeled_monetary_risk",
        )
        for name in positive_fields:
            if not _is_positive_finite(getattr(self, name)):
                raise ValueError(f"{name} must be finite and > 0")
        if self.tp_price is not None and not _is_positive_finite(self.tp_price):
            raise ValueError("tp_price must be finite and > 0 when supplied")
        if _aware_utc(self.approved_at) is None:
            raise ValueError("approved_at must be timezone-aware")
        if self.modeled_monetary_risk > self.monetary_risk_budget:
            raise ValueError("modeled_monetary_risk cannot exceed its budget")


@dataclass(frozen=True)
class _ApprovalReservation:
    symbol: str
    size_lots: Decimal


@dataclass
class RiskEngine:
    """Make atomic, deterministic risk decisions using session-local state only."""

    limits: RiskLimits = field(default_factory=RiskLimits)
    pip_size_resolver: PipSizeResolver | None = None
    lot_step: float = 0.01
    pip_value_per_lot: float = 10.0

    _kill_switch_active: bool = field(default=False, init=False)
    _kill_switch_reason: KillSwitchReason | None = field(default=None, init=False)
    _consecutive_losses: int = field(default=0, init=False)
    _daily_trades: int = field(default=0, init=False)
    _last_reset_date: date | None = field(default=None, init=False)
    _daily_start_equity: float | None = field(default=None, init=False)
    _peak_equity: float | None = field(default=None, init=False)
    _approved_order_ids: set[str] = field(default_factory=set, init=False)
    _approval_reservations: dict[str, _ApprovalReservation] = field(
        default_factory=dict, init=False
    )
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _configuration_locked: bool = field(default=False, init=False, repr=False)

    def __setattr__(self, name: str, value: object) -> None:
        configuration_fields = {
            "limits",
            "pip_size_resolver",
            "lot_step",
            "pip_value_per_lot",
        }
        if name in configuration_fields and getattr(self, "_configuration_locked", False):
            raise AttributeError(f"{name} is immutable after RiskEngine construction")
        super().__setattr__(name, value)

    def __post_init__(self) -> None:
        if not isinstance(self.limits, RiskLimits):
            raise ValueError("limits must be a RiskLimits instance")
        if not _is_positive_finite(self.lot_step):
            raise ValueError("lot_step must be finite and > 0")
        if not _is_positive_finite(self.pip_value_per_lot):
            raise ValueError("pip_value_per_lot must be finite and > 0")
        self._configuration_locked = True

    @property
    def kill_switch_active(self) -> bool:
        with self._lock:
            return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> KillSwitchReason | None:
        with self._lock:
            return self._kill_switch_reason

    @property
    def consecutive_losses(self) -> int:
        with self._lock:
            return self._consecutive_losses

    @property
    def daily_trades(self) -> int:
        with self._lock:
            return self._daily_trades

    @property
    def last_reset_date(self) -> date | None:
        with self._lock:
            return self._last_reset_date

    @property
    def daily_start_equity(self) -> float | None:
        with self._lock:
            return self._daily_start_equity

    @property
    def peak_equity(self) -> float | None:
        with self._lock:
            return self._peak_equity

    @property
    def approved_order_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._approved_order_ids)

    @property
    def reserved_position_count(self) -> int:
        with self._lock:
            return len(self._approval_reservations)

    def release_approval(self, order_id: str) -> bool:
        """Release an approval's exposure reservation without erasing its history.

        The caller must release a reservation before evaluating against a refreshed
        account snapshot that contains the resulting position. RiskEngine has no broker
        visibility and cannot reconcile fills automatically.
        """
        if not isinstance(order_id, str):
            return False
        with self._lock:
            return self._approval_reservations.pop(order_id, None) is not None

    def evaluate(
        self,
        signal: SignalEvent,
        *,
        entry_price: float,
        sl_price: float | None,
        tp_price: float | None,
        account: AccountInfo | None,
        current_time: datetime,
    ) -> RiskDecision | RiskRejection:
        """Evaluate caller-owned execution prices without performing broker operations."""
        signal_error = _validate_signal(signal)
        if signal_error is not None:
            return signal_error

        current_utc = _aware_utc(current_time)
        if current_utc is None:
            return _reject("invalid_current_time", "current_time must be timezone-aware", signal)
        signal_utc = signal.signal_time.astimezone(UTC)
        if signal_utc > current_utc:
            return _reject("future_signal_time", "signal_time cannot be in the future", signal)

        account_error = _validate_account(account, signal)
        if account_error is not None:
            return account_error
        assert isinstance(account, AccountInfo)

        with self._lock:
            state_error = self._update_and_check_state_locked(
                account, current_utc, signal, None
            )
            if state_error is not None:
                return state_error

        price_result = _validate_prices(signal, entry_price, sl_price, tp_price)
        if isinstance(price_result, RiskRejection):
            return price_result
        entry, stop, take_profit = price_result

        symbol = _canonical_symbol(signal.symbol)
        order_id = _order_id(signal, symbol, signal_utc)
        pip_result = self._resolve_pip_size(symbol, signal, order_id)
        if isinstance(pip_result, RiskRejection):
            return pip_result
        pip_size = pip_result

        entry_decimal = Decimal(str(entry))
        stop_decimal = Decimal(str(stop))
        pip_decimal = Decimal(str(pip_size))
        risk_budget_decimal = (
            Decimal(str(account.equity))
            * Decimal(str(self.limits.max_risk_per_trade_pct))
            / Decimal(100)
        )
        stop_pips_decimal = abs(entry_decimal - stop_decimal) / pip_decimal
        denominator_decimal = stop_pips_decimal * Decimal(str(self.pip_value_per_lot))
        raw_lots_decimal = risk_budget_decimal / denominator_decimal
        stop_pips = float(stop_pips_decimal)
        risk_budget = float(risk_budget_decimal)
        denominator = float(denominator_decimal)
        raw_lots = float(raw_lots_decimal)
        calculated = (stop_pips, risk_budget, denominator, raw_lots)
        if not all(math.isfinite(value) and value > 0 for value in calculated):
            return _reject(
                "invalid_risk_calculation",
                "position-sizing inputs produced an invalid result",
                signal,
                order_id,
            )
        existing_lots_decimal = sum(
            (
                abs(Decimal(str(item.size)))
                for item in account.open_positions
                if _canonical_symbol(item.symbol) == symbol
            ),
            start=Decimal(0),
        )
        existing_lots = float(existing_lots_decimal)
        if not math.isfinite(existing_lots):
            return _reject(
                "invalid_position",
                "existing symbol exposure is not finite",
                signal,
                order_id,
            )

        exposure_limit_decimal = Decimal(
            str(self.limits.max_exposure_per_symbol_lots)
        )
        lot_step_decimal = Decimal(str(self.lot_step))

        with self._lock:
            # Account state is checked again because validation and pip resolution occur
            # outside the lock; a concurrent reset or kill-switch must win before approval.
            state_error = self._update_and_check_state_locked(
                account, current_utc, signal, order_id
            )
            if state_error is not None:
                return state_error
            if order_id in self._approved_order_ids:
                return _reject(
                    "duplicate_approval",
                    "this deterministic execution intent was already approved",
                    signal,
                    order_id,
                )
            effective_position_count = len(account.open_positions) + len(
                self._approval_reservations
            )
            if effective_position_count >= self.limits.max_open_positions:
                return _reject(
                    "max_open_positions",
                    "maximum open-position count has been reached",
                    signal,
                    order_id,
                )
            if self._daily_trades >= self.limits.max_trades_per_day:
                return _reject(
                    "max_daily_trades",
                    "maximum daily trade count has been reached",
                    signal,
                    order_id,
                )

            reserved_symbol_lots = sum(
                (
                    reservation.size_lots
                    for reservation in self._approval_reservations.values()
                    if reservation.symbol == symbol
                ),
                start=Decimal(0),
            )
            effective_symbol_lots = existing_lots_decimal + reserved_symbol_lots
            available_lots_decimal = exposure_limit_decimal - effective_symbol_lots
            if available_lots_decimal < lot_step_decimal:
                return _reject(
                    "symbol_exposure_exhausted",
                    "remaining gross symbol exposure is below one lot step",
                    signal,
                    order_id,
                )
            capped_decimal = min(raw_lots_decimal, available_lots_decimal)
            size_decimal = _floor_decimal_to_step(capped_decimal, lot_step_decimal)
            size_lots = float(size_decimal)
            tolerance = max(1e-12, risk_budget * 1e-12)
            modeled_risk = float(size_decimal * denominator_decimal)
            if (
                not math.isfinite(size_lots)
                or size_decimal < lot_step_decimal
                or size_decimal > raw_lots_decimal
                or size_decimal > available_lots_decimal
            ):
                return _reject(
                    "size_below_minimum",
                    "permitted position size is below one lot step",
                    signal,
                    order_id,
                )
            if not math.isfinite(modeled_risk) or modeled_risk > risk_budget + tolerance:
                return _reject(
                    "risk_budget_exceeded",
                    "quantized position size exceeds the monetary risk budget",
                    signal,
                    order_id,
                )

            self._approved_order_ids.add(order_id)
            self._approval_reservations[order_id] = _ApprovalReservation(
                symbol=symbol, size_lots=size_decimal
            )
            self._daily_trades += 1
            return RiskDecision(
                signal=signal,
                order_id=order_id,
                size_lots=size_lots,
                entry_price=entry,
                sl_price=stop,
                tp_price=take_profit,
                pip_size=pip_size,
                stop_pips=stop_pips,
                monetary_risk_budget=risk_budget,
                modeled_monetary_risk=modeled_risk,
                approved_at=current_utc,
            )

    def check_account_state(
        self, account: AccountInfo | None, current_time: datetime
    ) -> RiskRejection | None:
        """Update UTC/peak state and latch any account-driven kill switch."""
        current_utc = _aware_utc(current_time)
        if current_utc is None:
            return _reject("invalid_current_time", "current_time must be timezone-aware")
        account_error = _validate_account(account, None)
        if account_error is not None:
            return account_error
        assert isinstance(account, AccountInfo)
        with self._lock:
            return self._update_and_check_state_locked(account, current_utc, None, None)

    def on_trade_closed(self, realized_pnl: float) -> RiskRejection | None:
        """Record a realized result and latch the consecutive-loss control if reached."""
        if not _is_finite_number(realized_pnl):
            return _reject(
                "invalid_realized_pnl", "realized_pnl must be a finite numeric value"
            )
        with self._lock:
            if float(realized_pnl) < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0
            if self._consecutive_losses >= self.limits.max_consecutive_losses:
                self._trigger_locked(KillSwitchReason.MAX_CONSECUTIVE_LOSSES)
            return self._active_kill_rejection_locked(None, None)

    def trigger_kill_switch(self, reason: KillSwitchReason) -> bool:
        """Latch a kill-switch reason; return true only for the first trigger."""
        if not isinstance(reason, KillSwitchReason):
            raise ValueError("reason must be a KillSwitchReason")
        with self._lock:
            return self._trigger_locked(reason)

    def _resolve_pip_size(
        self, symbol: str, signal: SignalEvent, order_id: str
    ) -> float | RiskRejection:
        if self.pip_size_resolver is None:
            return _reject(
                "pip_size_unavailable", "no pip-size resolver is configured", signal, order_id
            )
        try:
            resolver = self.pip_size_resolver.pip_size_for
            raw_value = resolver(symbol)
        except Exception:
            return _reject(
                "pip_size_unavailable",
                "the configured resolver could not provide a pip size",
                signal,
                order_id,
            )
        try:
            pip_size = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            return _reject(
                "invalid_pip_size", "resolved pip size is not numeric", signal, order_id
            )
        if not math.isfinite(pip_size) or pip_size <= 0:
            return _reject(
                "invalid_pip_size",
                "resolved pip size must be finite and positive",
                signal,
                order_id,
            )
        return pip_size

    def _update_and_check_state_locked(
        self,
        account: AccountInfo,
        current_utc: datetime,
        signal: SignalEvent | None,
        order_id: str | None,
    ) -> RiskRejection | None:
        current_date = current_utc.date()
        equity = float(account.equity)
        if self._last_reset_date is not None and current_date < self._last_reset_date:
            return _reject(
                "out_of_order_time",
                "current UTC date precedes the active accounting date",
                signal,
                order_id,
            )
        if self._last_reset_date is None or current_date > self._last_reset_date:
            self._last_reset_date = current_date
            self._daily_start_equity = equity
            self._daily_trades = 0
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity

        active = self._active_kill_rejection_locked(signal, order_id)
        if active is not None:
            return active

        assert self._peak_equity is not None
        assert self._daily_start_equity is not None
        equity_decimal = Decimal(str(equity))
        peak_decimal = Decimal(str(self._peak_equity))
        daily_start_decimal = Decimal(str(self._daily_start_equity))
        hundred = Decimal(100)
        ruin_level = (
            Decimal(str(self.limits.starting_equity))
            * Decimal(str(self.limits.ruin_threshold_pct))
            / hundred
        )
        drawdown_loss = max(Decimal(0), peak_decimal - equity_decimal)
        daily_loss = max(Decimal(0), daily_start_decimal - equity_decimal)
        drawdown_breached = drawdown_loss * hundred >= (
            peak_decimal * Decimal(str(self.limits.max_drawdown_pct))
        )
        daily_loss_breached = daily_loss * hundred >= (
            daily_start_decimal * Decimal(str(self.limits.max_daily_loss_pct))
        )

        reason = None
        if equity_decimal <= ruin_level:
            reason = KillSwitchReason.RUIN_THRESHOLD
        elif drawdown_breached:
            reason = KillSwitchReason.MAX_DRAWDOWN
        elif daily_loss_breached:
            reason = KillSwitchReason.MAX_DAILY_LOSS
        elif self._consecutive_losses >= self.limits.max_consecutive_losses:
            reason = KillSwitchReason.MAX_CONSECUTIVE_LOSSES
        if reason is not None:
            self._trigger_locked(reason)
            return self._active_kill_rejection_locked(signal, order_id)
        return None

    def _trigger_locked(self, reason: KillSwitchReason) -> bool:
        if self._kill_switch_active:
            return False
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        return True

    def _active_kill_rejection_locked(
        self, signal: SignalEvent | None, order_id: str | None
    ) -> RiskRejection | None:
        if not self._kill_switch_active:
            return None
        return _reject(
            "kill_switch_active",
            "risk engine kill switch is latched for this session",
            signal,
            order_id,
            self._kill_switch_reason,
        )


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _is_positive_finite(value: object) -> bool:
    return _is_finite_number(value) and float(value) > 0


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        offset = value.utcoffset()
    except Exception:
        return None
    if offset is None:
        return None
    try:
        return value.astimezone(UTC)
    except Exception:
        return None


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and bool(_TOKEN_PATTERN.fullmatch(value))


def _canonical_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _validate_signal(signal: object) -> RiskRejection | None:
    if not isinstance(signal, SignalEvent):
        return _reject("invalid_signal", "signal must be a SignalEvent")
    if not _valid_token(signal.setup_name):
        return _reject("invalid_setup_name", "setup_name must be a safe non-empty token", signal)
    if not _valid_token(signal.symbol):
        return _reject(
            "invalid_signal_symbol", "symbol must be a safe non-empty token", signal
        )
    if not _valid_token(signal.timeframe):
        return _reject("invalid_timeframe", "timeframe must be a safe non-empty token", signal)
    if (
        isinstance(signal.side, bool)
        or not isinstance(signal.side, int)
        or signal.side not in (1, -1)
    ):
        return _reject("invalid_signal_side", "signal side must be exactly +1 or -1", signal)
    if _aware_utc(signal.signal_time) is None:
        return _reject("invalid_signal_time", "signal_time must be timezone-aware", signal)
    if (
        isinstance(signal.signal_bar_index, bool)
        or not isinstance(signal.signal_bar_index, int)
        or signal.signal_bar_index < 0
    ):
        return _reject(
            "invalid_signal_bar_index", "signal_bar_index must be an integer >= 0", signal
        )
    return None


def _validate_account(
    account: object, signal: SignalEvent | None
) -> RiskRejection | None:
    if not isinstance(account, AccountInfo):
        return _reject("invalid_account", "account must be an AccountInfo snapshot", signal)
    if not _is_positive_finite(account.equity):
        return _reject("invalid_equity", "account equity must be finite and positive", signal)
    if not isinstance(account.open_positions, list):
        return _reject("invalid_open_positions", "open_positions must be a list", signal)
    for item in account.open_positions:
        if not _valid_position(item):
            return _reject(
                "invalid_position", "open_positions contains a malformed Position", signal
            )
    return None


def _valid_position(item: object) -> bool:
    if not isinstance(item, Position):
        return False
    return (
        _valid_token(item.symbol)
        and not isinstance(item.side, bool)
        and isinstance(item.side, int)
        and item.side in (1, -1)
        and _is_positive_finite(item.size)
        and _is_positive_finite(item.entry_price)
        and _aware_utc(item.entry_time) is not None
        and _is_finite_number(item.unrealized_pnl)
        and isinstance(item.position_id, str)
        and bool(item.position_id.strip())
    )


def _validate_prices(
    signal: SignalEvent,
    entry_price: object,
    sl_price: object,
    tp_price: object,
) -> tuple[float, float, float | None] | RiskRejection:
    if not _is_positive_finite(entry_price):
        return _reject("invalid_entry_price", "entry_price must be finite and positive", signal)
    entry = float(entry_price)
    if sl_price is None:
        return _reject("missing_stop_loss", "sl_price is required", signal)
    if not _is_positive_finite(sl_price):
        return _reject("invalid_stop_loss", "sl_price must be finite and positive", signal)
    stop = float(sl_price)
    if (signal.side == 1 and stop >= entry) or (signal.side == -1 and stop <= entry):
        return _reject(
            "invalid_stop_loss_direction",
            "stop loss must be strictly adverse to entry for the signal side",
            signal,
        )

    take_profit = None
    if tp_price is not None:
        if not _is_positive_finite(tp_price):
            return _reject(
                "invalid_take_profit", "tp_price must be finite and positive", signal
            )
        take_profit = float(tp_price)
        if (signal.side == 1 and take_profit <= entry) or (
            signal.side == -1 and take_profit >= entry
        ):
            return _reject(
                "invalid_take_profit_direction",
                "take profit must be strictly favorable to entry for the signal side",
                signal,
            )
    return entry, stop, take_profit


def _order_id(signal: SignalEvent, symbol: str, signal_utc: datetime) -> str:
    timestamp = signal_utc.strftime("%Y%m%dT%H%M%S%fZ")
    side = "LONG" if signal.side == 1 else "SHORT"
    timeframe = signal.timeframe.upper()
    return f"{signal.setup_name}-{symbol}-{timeframe}-{timestamp}-{side}"


def _floor_decimal_to_step(value: Decimal, step: Decimal) -> Decimal:
    try:
        units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
        return units * step
    except (InvalidOperation, OverflowError, ValueError):
        return Decimal("NaN")


def _reject(
    reason: str,
    message: str,
    signal: SignalEvent | None = None,
    order_id: str | None = None,
    kill_switch_reason: KillSwitchReason | None = None,
) -> RiskRejection:
    return RiskRejection(reason, message, signal, order_id, kill_switch_reason)
