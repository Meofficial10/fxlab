"""Real-Time Market Data Stream (Phase 2 Paper Trading).

Streams real-time market ticks from a `BrokerAdapter`, buffers them in thread-safe queues,
and aggregates ticks into closed OHLCV candles following strict closed-candle discipline:
strategy/downstream engines NEVER see currently-forming candles.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import Lock

import pandas as pd

from ..data.schema import OHLCV, timeframe_to_offset, timeframe_to_timedelta
from .broker import BrokerAdapter, Tick


@dataclass
class MarketDataStream:
    """Real-time market data stream and closed-bar manager.

    Maintains thread-safe tick buffers per symbol and aggregates ticks into
    closed candles for downstream signal generation.
    """

    broker: BrokerAdapter
    symbols: list[str]
    tick_buffer_size: int = 1000

    # Master stream lock for dynamic symbol registration
    _stream_lock: Lock = field(default_factory=Lock, init=False)

    # Thread-safe tick buffers and locks per symbol
    _tick_buffers: dict[str, deque[Tick]] = field(default_factory=dict, init=False)
    _locks: dict[str, Lock] = field(default_factory=dict, init=False)

    # Cache for recent closed bars per (symbol, timeframe)
    _bar_cache: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._stream_lock = Lock()
        for sym in self.symbols:
            self._tick_buffers[sym] = deque(maxlen=self.tick_buffer_size)
            self._locks[sym] = Lock()

    def start(self) -> None:
        """Subscribe to real-time market ticks for all tracked symbols."""
        self.broker.subscribe_market_data(self.symbols)

    def on_tick(self, tick: Tick) -> None:
        """Process incoming market tick in a thread-safe manner."""
        if tick.symbol not in self._tick_buffers:
            with self._stream_lock:
                if tick.symbol not in self._tick_buffers:
                    self._tick_buffers[tick.symbol] = deque(maxlen=self.tick_buffer_size)
                    self._locks[tick.symbol] = Lock()
                    if tick.symbol not in self.symbols:
                        self.symbols.append(tick.symbol)

        with self._locks[tick.symbol]:
            # Maintain strict chronological ordering
            if self._tick_buffers[tick.symbol]:
                last_ts = self._tick_buffers[tick.symbol][-1].timestamp
                if tick.timestamp < last_ts:
                    # Ignore out-of-order tick
                    return
            self._tick_buffers[tick.symbol].append(tick)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        """Return the most recent tick for symbol, or None if buffer is empty."""
        if symbol not in self._tick_buffers:
            return None
        with self._locks[symbol]:
            if not self._tick_buffers[symbol]:
                return None
            return self._tick_buffers[symbol][-1]

    def _aggregate_ticks_to_bars(self, ticks: list[Tick], tf: str, symbol: str) -> pd.DataFrame:
        """Aggregate a list of ticks into canonical OHLCV bars.

        Returns a DataFrame indexed by `ts_open` (UTC) with float64 OHLCV columns.
        """
        if not ticks:
            empty = pd.DataFrame(columns=OHLCV, dtype="float64")
            empty.index = pd.DatetimeIndex([], name="ts_open", tz="UTC")
            empty.attrs["symbol"] = symbol
            empty.attrs["timeframe"] = tf
            return empty

        offset_str = timeframe_to_offset(tf)

        records: list[dict] = []
        for t in ticks:
            ts = pd.Timestamp(t.timestamp)
            if ts.tz is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")

            ts_open = ts.floor(offset_str)
            records.append({
                "ts_open": ts_open,
                "price": t.mid,
            })

        df_ticks = pd.DataFrame(records)
        grouped = df_ticks.groupby("ts_open")["price"]

        ohlc = grouped.agg(
            open="first",
            high="max",
            low="min",
            close="last",
            volume="count",
        )

        ohlc["volume"] = ohlc["volume"].astype("float64")
        ohlc = ohlc[OHLCV].astype("float64")
        ohlc.attrs["symbol"] = symbol
        ohlc.attrs["timeframe"] = tf
        return ohlc

    def get_closed_bars(self, symbol: str, tf: str, count: int = 500) -> pd.DataFrame:
        """Fetch N recent CLOSED bars for symbol and timeframe.

        Combines historical bars from the broker with real-time aggregated tick bars,
        filtering out any currently-forming (unclosed) bar.
        """
        hist_bars = self.broker.get_historical_bars(symbol, tf, count)
        if not hist_bars.empty:
            hist_bars = hist_bars.copy()
            if hist_bars.index.tz is None:
                hist_bars.index = hist_bars.index.tz_localize("UTC")
            else:
                hist_bars.index = hist_bars.index.tz_convert("UTC")

        with self._locks.get(symbol, Lock()):
            ticks = list(self._tick_buffers.get(symbol, []))

        tf_delta = timeframe_to_timedelta(tf)

        if ticks:
            last_tick_ts = pd.Timestamp(ticks[-1].timestamp)
            if last_tick_ts.tz is None:
                last_tick_ts = last_tick_ts.tz_localize("UTC")
            else:
                last_tick_ts = last_tick_ts.tz_convert("UTC")

            tick_bars = self._aggregate_ticks_to_bars(ticks, tf, symbol)

            # Filter out forming bar: a bar is closed only if ts_open + tf_delta <= last_tick_ts
            closed_tick_bars = tick_bars[tick_bars.index + tf_delta <= last_tick_ts]

            # Historical bars must also obey closed-candle discipline when last_tick_ts is known
            if not hist_bars.empty:
                closed_hist_bars = hist_bars[hist_bars.index + tf_delta <= last_tick_ts]
            else:
                closed_hist_bars = hist_bars

            if not closed_hist_bars.empty and not closed_tick_bars.empty:
                combined = pd.concat([closed_hist_bars, closed_tick_bars])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
            elif not closed_hist_bars.empty:
                combined = closed_hist_bars
            else:
                combined = closed_tick_bars
        else:
            combined = hist_bars

        if combined.empty:
            empty = pd.DataFrame(columns=OHLCV, dtype="float64")
            empty.index = pd.DatetimeIndex([], name="ts_open", tz="UTC")
            empty.attrs["symbol"] = symbol
            empty.attrs["timeframe"] = tf
            return empty

        res = combined.tail(count).copy()
        res.attrs["symbol"] = symbol
        res.attrs["timeframe"] = tf
        return res

    def get_latest_closed_bar(self, symbol: str, tf: str) -> pd.Series | None:
        """Return the single most recent CLOSED bar as a Series, or None if no closed bar exists."""
        closed_bars = self.get_closed_bars(symbol, tf, count=1)
        if closed_bars.empty:
            return None
        return closed_bars.iloc[-1]
