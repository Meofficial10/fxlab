"""Small deterministic session runner for historical paper-trading replay."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

import pandas as pd

from ..data.schema import OHLCV, timeframe_to_timedelta
from ..risk.engine import KillSwitchReason, RiskEngine
from .broker import OrderStatus, Tick
from .market_data import MarketDataStream
from .order_manager import ExecutionIntent, ExecutionResult, ExecutionResultKind, OrderManager
from .paper_broker import PaperBroker
from .signal_engine import SignalEngine, SignalEvent


@dataclass(frozen=True)
class MarketContext:
    """Market information available when an execution policy sees a signal."""

    tick: Tick
    current_time: datetime
    closed_bars: pd.DataFrame


class ExecutionPolicy(Protocol):
    """Caller-owned SL/TP policy; Phase 6 supplies no default strategy."""

    def __call__(
        self, signal: SignalEvent, market_context: MarketContext
    ) -> ExecutionIntent | None: ...


class CycleKind(StrEnum):
    PROCESSED = "processed"
    NO_SIGNAL = "no_signal"
    POLICY_DECLINED = "policy_declined"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class PaperCycleResult:
    kind: CycleKind
    current_time: datetime | None
    tick: Tick | None = None
    signals: tuple[SignalEvent, ...] = ()
    executions: tuple[ExecutionResult, ...] = ()
    reason: str = ""
    message: str = ""


@dataclass
class HistoricalBarReplay:
    """Emit one zero-spread close quote when each historical bar becomes known.

    Canonical OHLCV does not contain tick microstructure. This replay therefore emits
    exactly one quote at bar close with bid == ask == close; it does not synthesize an
    intrabar path or spread. Future events remain unexposed until ``next_tick`` advances.
    """

    bars_by_symbol: Mapping[str, pd.DataFrame]
    timeframe: str
    _events: tuple[Tick, ...] = field(init=False, repr=False)
    _cursor: int = field(default=0, init=False)
    _stopped: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        delta = timeframe_to_timedelta(self.timeframe)
        events: list[Tick] = []
        for raw_symbol, raw_bars in self.bars_by_symbol.items():
            symbol = raw_symbol.strip().upper()
            if not symbol or not isinstance(raw_bars, pd.DataFrame):
                raise ValueError("replay requires non-empty symbols and DataFrames")
            bars = raw_bars.copy()
            if not isinstance(bars.index, pd.DatetimeIndex):
                raise ValueError("replay bars must use a DatetimeIndex")
            bars.index = (
                bars.index.tz_localize("UTC")
                if bars.index.tz is None
                else bars.index.tz_convert("UTC")
            )
            missing = [column for column in OHLCV if column not in bars.columns]
            if missing:
                raise ValueError(f"replay bars missing columns: {missing}")
            for ts_open, row in bars.sort_index().iterrows():
                close = _positive_float(row["close"])
                if close is None:
                    raise ValueError("replay close prices must be finite and positive")
                timestamp = (ts_open + delta).to_pydatetime()
                events.append(Tick(symbol, timestamp, close, close, close))
        events.sort(key=lambda tick: (tick.timestamp.astimezone(UTC), tick.symbol))
        self._events = tuple(events)

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._events)

    def next_tick(self, *, until: datetime | None = None) -> Tick | None:
        if self._stopped or self.exhausted:
            return None
        boundary = _aware_utc(until) if until is not None else None
        if until is not None and boundary is None:
            raise ValueError("until must be timezone-aware")
        candidate = self._events[self._cursor]
        if boundary is not None and candidate.timestamp.astimezone(UTC) > boundary:
            return None
        self._cursor += 1
        return candidate

    def stop(self) -> None:
        self._stopped = True


@dataclass
class PaperTradingSession:
    """Connect replay, signals, explicit execution policy, and Phase 5 execution."""

    broker: PaperBroker
    replay: HistoricalBarReplay
    market_data: MarketDataStream
    signal_engine: SignalEngine
    order_manager: OrderManager
    risk_engine: RiskEngine
    execution_policy: ExecutionPolicy

    _started: bool = field(default=False, init=False)
    _stopped: bool = field(default=False, init=False)
    _tracked_orders: set[str] = field(default_factory=set, init=False)

    def start(self) -> None:
        if self._started:
            return
        if self._stopped:
            raise RuntimeError("a stopped paper session cannot be restarted")
        self.broker.connect()
        try:
            self.market_data.start()
        except Exception:
            self.broker.disconnect()
            raise
        self._started = True

    def poll_once(self, *, until: datetime | None = None) -> PaperCycleResult:
        if not self._started or self._stopped:
            return _cycle_failure("session_not_running", "paper session is not running")
        try:
            tick = self.replay.next_tick(until=until)
        except Exception:
            return _cycle_failure("replay_failure", "historical replay could not advance")
        if tick is None:
            kind = CycleKind.EXHAUSTED if self.replay.exhausted else CycleKind.NO_SIGNAL
            reason = "replay_exhausted" if kind is CycleKind.EXHAUSTED else "no_event_available"
            return PaperCycleResult(kind, None, reason=reason)

        current_time = tick.timestamp.astimezone(UTC)
        try:
            accepted = self.broker.accept_tick(tick)
            if not accepted:
                return _cycle_failure(
                    "out_of_order_tick",
                    "paper broker rejected an out-of-order replay tick",
                    current_time,
                )
            self.market_data.on_tick(tick)
            account = self.broker.get_account_info()
        except Exception:
            return _cycle_failure(
                "market_data_failure", "replay market state could not be accepted", current_time
            )

        account_rejection = self.risk_engine.check_account_state(account, current_time)
        if account_rejection is not None:
            return PaperCycleResult(
                kind=CycleKind.FAILED,
                current_time=current_time,
                tick=tick,
                reason=account_rejection.reason,
                message=account_rejection.message,
            )

        try:
            signals = tuple(self.signal_engine.process_all_symbols([tick.symbol]))
        except Exception:
            return _cycle_failure(
                "signal_processing_failure", "signal processing failed", current_time, tick
            )

        executions: list[ExecutionResult] = []
        declined = False
        for signal in signals:
            try:
                bars = self.market_data.get_closed_bars(
                    signal.symbol, signal.timeframe, count=self.signal_engine.lookback_count
                )
                context = MarketContext(
                    tick=tick,
                    current_time=current_time,
                    closed_bars=bars.copy(),
                )
                intent = self.execution_policy(signal, context)
            except Exception:
                return _cycle_failure(
                    "execution_policy_failure",
                    "execution policy failed",
                    current_time,
                    tick,
                    signals,
                )
            if intent is None:
                declined = True
                continue
            if not isinstance(intent, ExecutionIntent) or intent.signal != signal:
                return _cycle_failure(
                    "invalid_execution_intent",
                    "execution policy must return an intent for the supplied signal",
                    current_time,
                    tick,
                    signals,
                )
            result = self.order_manager.submit(intent, current_time=current_time)
            executions.append(result)
            if result.kind is ExecutionResultKind.SUBMITTED and result.record is not None:
                self._tracked_orders.add(result.record.client_order_id)

        executions.extend(self._refresh_and_reconcile())
        if executions:
            return PaperCycleResult(
                CycleKind.PROCESSED, current_time, tick, signals, tuple(executions)
            )
        if declined:
            return PaperCycleResult(CycleKind.POLICY_DECLINED, current_time, tick, signals)
        return PaperCycleResult(CycleKind.NO_SIGNAL, current_time, tick, signals)

    def stop(self) -> None:
        if self._stopped:
            return
        self.replay.stop()
        if self.broker.is_connected():
            self.broker.disconnect()
        self._stopped = True

    def _refresh_and_reconcile(self) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        for client_id in tuple(sorted(self._tracked_orders)):
            result = self.order_manager.refresh_order_status(client_id)
            results.append(result)
            record = result.record
            if record is None or record.status is not OrderStatus.FILLED:
                continue
            correlation = self.broker.get_correlation(client_id)
            if correlation is None or correlation.broker_order_id != record.broker_order_id:
                self.risk_engine.trigger_kill_switch(
                    KillSwitchReason.POSITION_RECONCILIATION_FAILED
                )
                results.append(_reconciliation_failure(result))
                continue
            try:
                account = self.broker.get_account_info()
            except Exception:
                self.risk_engine.trigger_kill_switch(
                    KillSwitchReason.POSITION_RECONCILIATION_FAILED
                )
                results.append(_reconciliation_failure(result))
                continue
            if any(
                position.position_id == correlation.position_id
                for position in account.open_positions
            ):
                self.order_manager.confirm_position_reflected(client_id)
                self._tracked_orders.discard(client_id)
            else:
                self.risk_engine.trigger_kill_switch(
                    KillSwitchReason.POSITION_RECONCILIATION_FAILED
                )
                results.append(_reconciliation_failure(result))
        return results


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) and converted > 0 else None


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except Exception:
        return None


def _cycle_failure(
    reason: str,
    message: str,
    current_time: datetime | None = None,
    tick: Tick | None = None,
    signals: tuple[SignalEvent, ...] = (),
) -> PaperCycleResult:
    return PaperCycleResult(
        CycleKind.FAILED,
        current_time,
        tick,
        signals,
        reason=reason,
        message=message,
    )


def _reconciliation_failure(status_result: ExecutionResult) -> ExecutionResult:
    return ExecutionResult(
        kind=ExecutionResultKind.STATUS_FAILURE,
        reason="position_reconciliation_failed",
        message="filled paper order was not exactly correlated to an account position",
        record=status_result.record,
    )
