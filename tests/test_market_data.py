"""Unit tests for MarketDataStream (Phase 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from fxlab.execution.broker import (
    AccountInfo,
    OrderRequest,
    Position,
    Tick,
)
from fxlab.execution.market_data import MarketDataStream


class MockBroker:
    """Mock broker adapter for testing MarketDataStream."""

    def __init__(self) -> None:
        self.subscribed_symbols: list[str] = []
        self.historical_bars_mock: pd.DataFrame = pd.DataFrame()

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def subscribe_market_data(self, symbols: list[str]) -> None:
        self.subscribed_symbols.extend(symbols)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        return None

    def get_account_info(self) -> AccountInfo:
        return AccountInfo(balance=10000.0, equity=10000.0, margin_used=0.0, margin_available=10000.0)

    def submit_order(self, order: OrderRequest) -> str:
        return order.order_id

    def get_order_status(self, order_id: str) -> dict:
        return {"status": "filled"}

    def cancel_order(self, order_id: str) -> bool:
        return True

    def close_position(self, position_id: str) -> str | None:
        return "close_1"

    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        return self.historical_bars_mock.copy()


def test_start_subscribes_market_data():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD", "GBPUSD"])
    stream.start()
    assert broker.subscribed_symbols == ["EURUSD", "GBPUSD"]


def test_tick_ingestion_and_get_latest_tick():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])
    assert stream.get_latest_tick("EURUSD") is None

    t1 = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)
    tick1 = Tick(symbol="EURUSD", timestamp=t1, bid=1.0850, ask=1.0852, mid=1.0851)
    stream.on_tick(tick1)

    latest = stream.get_latest_tick("EURUSD")
    assert latest is not None
    assert latest.mid == 1.0851

    t2 = datetime(2026, 8, 25, 10, 0, 1, tzinfo=UTC)
    tick2 = Tick(symbol="EURUSD", timestamp=t2, bid=1.0853, ask=1.0855, mid=1.0854)
    stream.on_tick(tick2)

    latest2 = stream.get_latest_tick("EURUSD")
    assert latest2 is not None
    assert latest2.mid == 1.0854


def test_chronological_ordering_rejects_out_of_order_ticks():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    t2 = datetime(2026, 8, 25, 10, 0, 5, tzinfo=UTC)
    tick_later = Tick(symbol="EURUSD", timestamp=t2, bid=1.0860, ask=1.0862, mid=1.0861)
    stream.on_tick(tick_later)

    t1 = datetime(2026, 8, 25, 10, 0, 1, tzinfo=UTC)
    tick_earlier = Tick(symbol="EURUSD", timestamp=t1, bid=1.0850, ask=1.0852, mid=1.0851)
    stream.on_tick(tick_earlier)

    latest = stream.get_latest_tick("EURUSD")
    assert latest is not None
    assert latest.timestamp == t2
    assert latest.mid == 1.0861


def test_same_timestamp_ticks_are_preserved():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    t_same = datetime(2026, 8, 25, 10, 0, 5, tzinfo=UTC)
    tick1 = Tick(symbol="EURUSD", timestamp=t_same, bid=1.0850, ask=1.0852, mid=1.0851)
    tick2 = Tick(symbol="EURUSD", timestamp=t_same, bid=1.0853, ask=1.0855, mid=1.0854)

    stream.on_tick(tick1)
    stream.on_tick(tick2)

    # Both ticks preserved in buffer
    assert len(stream._tick_buffers["EURUSD"]) == 2
    latest = stream.get_latest_tick("EURUSD")
    assert latest is not None
    assert latest.mid == 1.0854


def test_bounded_tick_buffer_size():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"], tick_buffer_size=5)

    start_time = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)
    for i in range(10):
        t = start_time + timedelta(seconds=i)
        tick = Tick(symbol="EURUSD", timestamp=t, bid=1.0850 + i * 0.0001, ask=1.0852 + i * 0.0001, mid=1.0851 + i * 0.0001)
        stream.on_tick(tick)

    # Buffer maxlen is 5, so buffer length must be 5
    assert len(stream._tick_buffers["EURUSD"]) == 5
    # The oldest kept tick should be i=5
    assert stream._tick_buffers["EURUSD"][0].timestamp == start_time + timedelta(seconds=5)


def test_closed_candle_discipline_no_forming_bar_leakage():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    # Ingest ticks for M5 bar starting at 10:00:00 (closes at 10:05:00)
    t1 = datetime(2026, 8, 25, 10, 0, 10, tzinfo=UTC)
    t2 = datetime(2026, 8, 25, 10, 2, 30, tzinfo=UTC)

    stream.on_tick(Tick(symbol="EURUSD", timestamp=t1, bid=1.0850, ask=1.0852, mid=1.0851))
    stream.on_tick(Tick(symbol="EURUSD", timestamp=t2, bid=1.0855, ask=1.0857, mid=1.0856))

    # At 10:02:30, the 10:00:00 M5 bar is still forming! Must return None.
    closed_bar = stream.get_latest_closed_bar("EURUSD", "M5")
    assert closed_bar is None

    # Now tick at 10:05:00 arrives -> 10:00:00 M5 bar is officially closed
    t3 = datetime(2026, 8, 25, 10, 5, 0, tzinfo=UTC)
    stream.on_tick(Tick(symbol="EURUSD", timestamp=t3, bid=1.0860, ask=1.0862, mid=1.0861))

    closed_bar = stream.get_latest_closed_bar("EURUSD", "M5")
    assert closed_bar is not None
    assert closed_bar.name == pd.Timestamp("2026-08-25 10:00:00", tz="UTC")
    assert closed_bar["open"] == 1.0851
    assert closed_bar["high"] == 1.0856
    assert closed_bar["low"] == 1.0851
    assert closed_bar["close"] == 1.0856
    assert closed_bar["volume"] == 2.0


def test_historical_forming_bar_is_filtered():
    broker = MockBroker()

    # Historical bars returned from broker: 09:55:00 (closed) and 10:00:00 (forming relative to 10:04:59)
    hist_index = pd.DatetimeIndex([
        pd.Timestamp("2026-08-25 09:55:00", tz="UTC"),
        pd.Timestamp("2026-08-25 10:00:00", tz="UTC"),
    ], name="ts_open")
    broker.historical_bars_mock = pd.DataFrame({
        "open": [1.0845, 1.0850],
        "high": [1.0850, 1.0858],
        "low": [1.0842, 1.0848],
        "close": [1.0849, 1.0855],
        "volume": [15.0, 8.0],
    }, index=hist_index)

    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    # Send tick at 10:04:59 -> 10:00:00 bar is NOT closed yet (requires 10:05:00)
    t_tick = datetime(2026, 8, 25, 10, 4, 59, tzinfo=UTC)
    stream.on_tick(Tick("EURUSD", t_tick, 1.0855, 1.0857, 1.0856))

    bars = stream.get_closed_bars("EURUSD", "M5")
    # The 10:00:00 historical bar MUST be filtered out!
    assert len(bars) == 1
    assert bars.index[0] == pd.Timestamp("2026-08-25 09:55:00", tz="UTC")

    # Boundary test: send tick at exactly 10:05:00 (ts_open + tf_delta == last_tick_ts)
    t_boundary = datetime(2026, 8, 25, 10, 5, 0, tzinfo=UTC)
    stream.on_tick(Tick("EURUSD", t_boundary, 1.0856, 1.0858, 1.0857))

    bars_boundary = stream.get_closed_bars("EURUSD", "M5")
    # The 10:00:00 bar MUST now be included!
    assert len(bars_boundary) == 2
    assert bars_boundary.index[-1] == pd.Timestamp("2026-08-25 10:00:00", tz="UTC")


def test_dynamic_symbol_registration():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    t = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)
    new_tick = Tick(symbol="USDJPY", timestamp=t, bid=155.00, ask=155.02, mid=155.01)

    stream.on_tick(new_tick)
    assert "USDJPY" in stream.symbols
    assert "USDJPY" in stream._tick_buffers
    assert stream.get_latest_tick("USDJPY") is not None
    assert stream.get_latest_tick("USDJPY").mid == 155.01


def test_bar_aggregation_ohlcv_accuracy():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    t_open = datetime(2026, 8, 25, 11, 0, 0, tzinfo=UTC)
    ticks = [
        Tick("EURUSD", t_open + timedelta(seconds=1), 1.0800, 1.0802, 1.0801),  # Open = 1.0801
        Tick("EURUSD", t_open + timedelta(seconds=30), 1.0820, 1.0822, 1.0821), # High = 1.0821
        Tick("EURUSD", t_open + timedelta(seconds=60), 1.0790, 1.0792, 1.0791), # Low = 1.0791
        Tick("EURUSD", t_open + timedelta(seconds=90), 1.0810, 1.0812, 1.0811), # Close = 1.0811
        Tick("EURUSD", t_open + timedelta(minutes=5), 1.0815, 1.0817, 1.0816),  # Triggers close of M5 bar 11:00:00
    ]

    for t in ticks:
        stream.on_tick(t)

    closed_bars = stream.get_closed_bars("EURUSD", "M5")
    assert len(closed_bars) == 1

    bar = closed_bars.iloc[0]
    assert bar.name == pd.Timestamp("2026-08-25 11:00:00", tz="UTC")
    assert bar["open"] == 1.0801
    assert bar["high"] == 1.0821
    assert bar["low"] == 1.0791
    assert bar["close"] == 1.0811
    assert bar["volume"] == 4.0


def test_get_closed_bars_combines_history_and_ticks():
    broker = MockBroker()

    # Setup historical bars
    hist_index = pd.DatetimeIndex([
        pd.Timestamp("2026-08-25 09:50:00", tz="UTC"),
        pd.Timestamp("2026-08-25 09:55:00", tz="UTC"),
    ], name="ts_open")
    broker.historical_bars_mock = pd.DataFrame({
        "open": [1.0840, 1.0845],
        "high": [1.0848, 1.0850],
        "low": [1.0838, 1.0842],
        "close": [1.0845, 1.0849],
        "volume": [10.0, 15.0],
    }, index=hist_index)

    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    # Ingest tick data for 10:00:00 bar and trigger tick at 10:05:00
    t1 = datetime(2026, 8, 25, 10, 1, 0, tzinfo=UTC)
    t2 = datetime(2026, 8, 25, 10, 5, 0, tzinfo=UTC)
    stream.on_tick(Tick("EURUSD", t1, 1.0850, 1.0852, 1.0851))
    stream.on_tick(Tick("EURUSD", t2, 1.0855, 1.0857, 1.0856))

    bars = stream.get_closed_bars("EURUSD", "M5")
    assert len(bars) == 3
    assert list(bars.index) == [
        pd.Timestamp("2026-08-25 09:50:00", tz="UTC"),
        pd.Timestamp("2026-08-25 09:55:00", tz="UTC"),
        pd.Timestamp("2026-08-25 10:00:00", tz="UTC"),
    ]


def test_empty_buffer_returns_gracefully():
    broker = MockBroker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    assert stream.get_latest_tick("EURUSD") is None
    assert stream.get_latest_closed_bar("EURUSD", "M5") is None

    empty_df = stream.get_closed_bars("EURUSD", "M5")
    assert empty_df.empty
    assert list(empty_df.columns) == ["open", "high", "low", "close", "volume"]
