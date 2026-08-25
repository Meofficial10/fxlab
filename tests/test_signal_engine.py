"""Unit tests for SignalEngine (Phase 3)."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from fxlab.execution.broker import AccountInfo, OrderRequest, Tick
from fxlab.execution.market_data import MarketDataStream
from fxlab.execution.signal_engine import SignalEngine, SignalEvent


class DummyBroker:
    """Mock broker adapter for testing SignalEngine."""

    def __init__(self) -> None:
        self.historical_bars_mock: pd.DataFrame = pd.DataFrame()

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def subscribe_market_data(self, symbols: list[str]) -> None:
        pass

    def get_latest_tick(self, symbol: str) -> Tick | None:
        return None

    def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            balance=10000.0, equity=10000.0, margin_used=0.0, margin_available=10000.0
        )

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


def _authoritative_time() -> datetime:
    return datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def _make_market_data(
    broker: DummyBroker,
    symbols: list[str] | None = None,
) -> MarketDataStream:
    return MarketDataStream(
        broker=broker,
        symbols=symbols or ["EURUSD"],
        time_provider=_authoritative_time,
    )


class MockSetup:
    """Mock setup generator for unit testing."""

    def __init__(self, name: str = "mock_setup", signals: dict[int, int] | None = None) -> None:
        self.name = name
        self.signals = signals or {}  # mapping from bar index -> side (+1 / -1)

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        idxs = []
        sides = []
        for idx in sorted(self.signals.keys()):
            if idx < len(bars):
                idxs.append(idx)
                sides.append(self.signals[idx])
        return np.array(idxs, dtype=int), np.array(sides, dtype=int)


def _make_closed_bars(count: int = 10, start_time: str = "2026-08-25 10:00:00") -> pd.DataFrame:
    idx = pd.date_range(start_time, periods=count, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [1.0850] * count,
            "high": [1.0860] * count,
            "low": [1.0840] * count,
            "close": [1.0855] * count,
            "volume": [100.0] * count,
        },
        index=idx,
    )
    df.index.name = "ts_open"
    df.attrs["symbol"] = "EURUSD"
    df.attrs["timeframe"] = "M5"
    return df


def test_newest_closed_bar_no_signal():
    broker = DummyBroker()
    broker.historical_bars_mock = _make_closed_bars(10)
    market_data = _make_market_data(broker)
    setup = MockSetup(signals={})  # No signals

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    event = engine.process_symbol("EURUSD")
    assert event is None


def test_newest_closed_bar_long_signal():
    broker = DummyBroker()
    bars = _make_closed_bars(10)
    broker.historical_bars_mock = bars
    market_data = _make_market_data(broker)
    # Signal on the last bar index (9) -> LONG (+1)
    setup = MockSetup(signals={9: 1})

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    event = engine.process_symbol("EURUSD")

    assert event is not None
    assert event.setup_name == "mock_setup"
    assert event.symbol == "EURUSD"
    assert event.timeframe == "M5"
    assert event.side == 1
    assert event.signal_bar_index == 9
    # Signal time is close of last bar: 10:45 open + 5min = 10:50 UTC
    assert event.signal_time == pd.Timestamp("2026-08-25 10:50:00", tz="UTC")


def test_newest_closed_bar_short_signal():
    broker = DummyBroker()
    bars = _make_closed_bars(10)
    broker.historical_bars_mock = bars
    market_data = _make_market_data(broker)
    # Signal on the last bar index (9) -> SHORT (-1)
    setup = MockSetup(signals={9: -1})

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    event = engine.process_symbol("EURUSD")

    assert event is not None
    assert event.side == -1


def test_older_signal_is_not_emitted_as_current():
    broker = DummyBroker()
    bars = _make_closed_bars(10)
    broker.historical_bars_mock = bars
    market_data = _make_market_data(broker)
    # Signal on older bar index (5), NOT newest bar (9)
    setup = MockSetup(signals={5: 1})

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    event = engine.process_symbol("EURUSD")
    assert event is None


def test_duplicate_polling_protection():
    broker = DummyBroker()
    bars = _make_closed_bars(10)
    broker.historical_bars_mock = bars
    market_data = _make_market_data(broker)
    setup = MockSetup(signals={9: 1})

    received_events: list[SignalEvent] = []

    def callback(evt: SignalEvent) -> None:
        received_events.append(evt)

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5", on_signal=callback)

    # First poll -> emits event
    evt1 = engine.process_symbol("EURUSD")
    assert evt1 is not None
    assert len(received_events) == 1

    # Second poll with same closed bars -> emits nothing
    evt2 = engine.process_symbol("EURUSD")
    assert evt2 is None
    assert len(received_events) == 1


def test_new_closed_bar_emits_again():
    broker = DummyBroker()
    bars10 = _make_closed_bars(10)
    broker.historical_bars_mock = bars10
    market_data = _make_market_data(broker)
    setup = MockSetup(signals={9: 1, 10: -1})

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")

    # Poll 1 (10 bars)
    evt1 = engine.process_symbol("EURUSD")
    assert evt1 is not None
    assert evt1.side == 1
    assert evt1.signal_bar_index == 9

    # New bar arrives (11 bars total)
    bars11 = _make_closed_bars(11)
    broker.historical_bars_mock = bars11

    # Poll 2 (11 bars) -> new bar 10 fires short signal
    evt2 = engine.process_symbol("EURUSD")
    assert evt2 is not None
    assert evt2.side == -1
    assert evt2.signal_bar_index == 10


def test_multi_symbol_isolation():
    broker = DummyBroker()
    bars_eur = _make_closed_bars(10, start_time="2026-08-25 10:00:00")
    bars_gbp = _make_closed_bars(10, start_time="2026-08-25 10:00:00")
    bars_gbp.attrs["symbol"] = "GBPUSD"

    def mock_hist(symbol: str, tf: str, count: int) -> pd.DataFrame:
        if symbol == "EURUSD":
            return bars_eur.copy()
        return bars_gbp.copy()

    broker.get_historical_bars = mock_hist  # type: ignore

    market_data = _make_market_data(broker, ["EURUSD", "GBPUSD"])
    setup = MockSetup(signals={9: 1})

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")

    events = engine.process_all_symbols()
    assert len(events) == 2
    assert events[0].symbol == "EURUSD"
    assert events[1].symbol == "GBPUSD"

    # Separate last processed timestamps maintained in engine
    assert engine._last_processed_bar_time[("EURUSD", "M5")] == bars_eur.index[-1]
    assert engine._last_processed_bar_time[("GBPUSD", "M5")] == bars_gbp.index[-1]


def test_multi_timeframe_isolation():
    broker = DummyBroker()
    bars_m5 = _make_closed_bars(10, start_time="2026-08-25 10:00:00")

    broker.historical_bars_mock = bars_m5
    market_data = _make_market_data(broker)
    setup = MockSetup(signals={9: 1})

    engine_m5 = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    engine_h1 = SignalEngine(setup=setup, market_data=market_data, timeframe="H1")

    evt_m5 = engine_m5.process_symbol("EURUSD")
    evt_h1 = engine_h1.process_symbol("EURUSD")

    assert evt_m5 is not None
    assert evt_h1 is not None
    assert ("EURUSD", "M5") in engine_m5._last_processed_bar_time
    assert ("EURUSD", "H1") in engine_h1._last_processed_bar_time


def test_optional_callback():
    broker = DummyBroker()
    broker.historical_bars_mock = _make_closed_bars(10)
    market_data = _make_market_data(broker)
    setup = MockSetup(signals={9: 1})

    called: list[SignalEvent] = []
    engine_with_cb = SignalEngine(
        setup=setup,
        market_data=market_data,
        timeframe="M5",
        on_signal=lambda e: called.append(e),
    )
    evt = engine_with_cb.process_symbol("EURUSD")
    assert evt is not None
    assert len(called) == 1
    assert called[0] == evt

    # An independent engine without a callback still returns its event.
    engine_no_cb = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    evt2 = engine_no_cb.process_symbol("EURUSD")
    assert evt2 is not None
    assert evt2 == evt


def test_duplicate_state_is_independent_across_engine_instances():
    broker = DummyBroker()
    broker.historical_bars_mock = _make_closed_bars(10)
    market_data = _make_market_data(broker)
    setup = MockSetup(signals={9: 1})

    first_engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    second_engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")

    first_event = first_engine.process_symbol("EURUSD")
    second_event = second_engine.process_symbol("EURUSD")

    assert first_event is not None
    assert second_event == first_event


def test_empty_bars_graceful_handling():
    broker = DummyBroker()
    broker.historical_bars_mock = pd.DataFrame()  # Empty
    market_data = _make_market_data(broker)
    setup = MockSetup(signals={0: 1})

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    evt = engine.process_symbol("EURUSD")
    assert evt is None


def test_setup_not_mutated():
    broker = DummyBroker()
    broker.historical_bars_mock = _make_closed_bars(10)
    market_data = _make_market_data(broker)
    setup = MockSetup(name="immutable_setup", signals={9: 1})

    engine = SignalEngine(setup=setup, market_data=market_data, timeframe="M5")
    engine.process_symbol("EURUSD")

    assert setup.name == "immutable_setup"
    assert setup.signals == {9: 1}
