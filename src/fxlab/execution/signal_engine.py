"""Signal Engine (Phase 3 Paper Trading).

Bridges MarketDataStream closed OHLCV bars and pure research Setup.generate() signals,
emitting validated SignalEvents for downstream risk and execution processing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from ..data.schema import timeframe_to_timedelta
from .market_data import MarketDataStream


@runtime_checkable
class SetupProtocol(Protocol):
    """Minimal protocol for Setup interface matching research layer."""

    name: str

    def generate(self, bars: pd.DataFrame) -> tuple: ...


@dataclass(frozen=True)
class SignalEvent:
    """Signal event generated from a newly closed bar."""

    setup_name: str
    symbol: str
    timeframe: str
    side: int  # +1 for long, -1 for short
    signal_time: datetime  # timestamp when the signal bar closed
    signal_bar_index: int  # positional index in the bars DataFrame
    confidence: float = 1.0


@dataclass
class SignalEngine:
    """Evaluates research Setup.generate() on closed bars and emits SignalEvents.

    Maintains duplicate signal protection per (symbol, timeframe) and guarantees that
    signals are only emitted for newly closed candles.
    """

    setup: SetupProtocol
    market_data: MarketDataStream
    timeframe: str
    lookback_count: int = 500
    on_signal: Callable[[SignalEvent], None] | None = None

    # Track last processed bar open timestamp per (symbol, timeframe)
    _last_processed_bar_time: dict[tuple[str, str], datetime] = field(
        default_factory=dict, init=False
    )

    def process_symbol(self, symbol: str) -> SignalEvent | None:
        """Evaluate closed bars and emit a SignalEvent when the newest bar fires."""
        bars = self.market_data.get_closed_bars(symbol, self.timeframe, count=self.lookback_count)
        if bars.empty:
            return None

        latest_bar_ts = bars.index[-1]
        key = (symbol, self.timeframe)

        # Duplicate protection: do not re-evaluate the same closed bar timestamp
        if (
            key in self._last_processed_bar_time
            and latest_bar_ts <= self._last_processed_bar_time[key]
        ):
            return None

        self._last_processed_bar_time[key] = latest_bar_ts

        # Execute pure research setup generator
        signal_idx, sides = self.setup.generate(bars)

        # Check if a signal fired on the newest closed bar (last row in bars)
        if len(signal_idx) > 0 and signal_idx[-1] == len(bars) - 1:
            tf_delta = timeframe_to_timedelta(self.timeframe)
            signal_close_time = latest_bar_ts + tf_delta

            event = SignalEvent(
                setup_name=self.setup.name,
                symbol=symbol,
                timeframe=self.timeframe,
                side=int(sides[-1]),
                signal_time=signal_close_time,
                signal_bar_index=int(signal_idx[-1]),
            )

            if self.on_signal is not None:
                self.on_signal(event)

            return event

        return None

    def process_all_symbols(self, symbols: list[str] | None = None) -> list[SignalEvent]:
        """Process given symbols, or every tracked symbol, and return generated signals."""
        target_symbols = symbols if symbols is not None else self.market_data.symbols
        events: list[SignalEvent] = []

        for symbol in target_symbols:
            event = self.process_symbol(symbol)
            if event is not None:
                events.append(event)

        return events
