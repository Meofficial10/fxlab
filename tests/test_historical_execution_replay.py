"""Deterministic offline Historical Execution Replay V1B test suite.

Tests existing FXLab production execution, broker, market data, and order layers
across 10 deterministic market and execution scenarios without MT5 or network calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from fxlab.config import CostConfig, CostDefaults
from fxlab.execution.broker import (
    OrderRequest,
    OrderStatus,
    Tick,
)
from fxlab.execution.margin import UnmodeledPaperMargin
from fxlab.execution.market_data import MarketDataStream
from fxlab.execution.paper_broker import CloseReason, PaperBroker, PositionClose
from fxlab.execution.valuation import (
    ConversionQuote,
    FxInstrumentCatalog,
    FxValuationEngine,
    InstrumentSpec,
    ValuationFailure,
)

NOW = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)

CATALOG = FxInstrumentCatalog(
    (
        InstrumentSpec("EURUSD", "fx", "EUR", "USD", 0.0001, 100_000, "1"),
        InstrumentSpec("GBPUSD", "fx", "GBP", "USD", 0.0001, 100_000, "1"),
        InstrumentSpec("USDJPY", "fx", "USD", "JPY", 0.01, 100_000, "1"),
    )
)


def zero_cost_config() -> CostConfig:
    """Return CostConfig with zero spread, commission, and slippage."""
    return CostConfig(
        default=CostDefaults(
            spread_pips=0.0,
            commission_per_lot_roundturn=0.0,
            slippage_pips_base=0.0,
            slippage_vol_coeff=0.0,
            latency_bars=0,
        )
    )


def make_paper_broker(**overrides: object) -> PaperBroker:
    """Create a connected and subscribed test PaperBroker instance."""
    params: dict[str, object] = {
        "account_currency": "USD",
        "instrument_catalog": CATALOG,
        "valuation_max_age": timedelta(minutes=5),
        "valuation_policy_version": "fx-point-in-time-v1",
        "margin_model": UnmodeledPaperMargin("USD"),
        "commission_currency": "USD",
        "cost_config": zero_cost_config(),
    }
    params.update(overrides)
    broker = PaperBroker(**params)  # type: ignore[arg-type]
    broker.connect()
    broker.subscribe_market_data(["EURUSD", "GBPUSD", "USDJPY"])
    return broker


def make_tick(
    symbol: str = "EURUSD",
    when: datetime = NOW,
    *,
    bid: float = 1.1000,
    ask: float = 1.1002,
    mid: float | None = None,
) -> Tick:
    """Create a test Tick."""
    computed_mid = (bid + ask) / 2.0 if mid is None else mid
    return Tick(symbol=symbol, timestamp=when, bid=bid, ask=ask, mid=computed_mid)


def make_order(
    client_order_id: str = "order-test-1",
    side: int = 1,
    *,
    symbol: str = "EURUSD",
    size: float = 0.1,
    sl_price: float | None = 1.0950,
    tp_price: float | None = 1.1050,
) -> OrderRequest:
    """Create a test OrderRequest."""
    return OrderRequest(
        symbol=symbol,
        side=side,
        size=size,
        order_type="market",
        order_id=client_order_id,
        sl_price=sl_price,
        tp_price=tp_price,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Entry -> Take Profit
# ---------------------------------------------------------------------------
def test_replay_scenario_01_entry_take_profit() -> None:
    """Scenario 1: Long position opens and closes on reaching take profit."""
    broker = make_paper_broker()
    t1 = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    broker.accept_tick(t1)

    req = make_order(
        client_order_id="s1-tp",
        side=1,
        sl_price=1.0950,
        tp_price=1.1050,
    )
    broker_order_id = broker.submit_order(req)
    status_info = broker.get_order_status(broker_order_id)
    assert status_info["status"] == OrderStatus.FILLED.value
    assert len(broker.get_account_info().open_positions) == 1

    # Tick 2 reaches TP threshold (bid >= 1.1050 for long exit)
    t2 = make_tick(when=NOW + timedelta(seconds=10), bid=1.1052, ask=1.1054)
    accepted = broker.accept_tick(t2)
    assert accepted is True

    closes = broker.drain_close_events()
    assert len(closes) == 1
    close_event: PositionClose = closes[0]
    assert close_event.client_entry_order_id == "s1-tp"
    assert close_event.reason == CloseReason.TAKE_PROFIT
    assert close_event.exit_price == pytest.approx(1.1052)
    assert len(broker.get_account_info().open_positions) == 0


# ---------------------------------------------------------------------------
# Scenario 2: Entry -> Stop Loss
# ---------------------------------------------------------------------------
def test_replay_scenario_02_entry_stop_loss() -> None:
    """Scenario 2: Long position opens and closes on reaching stop loss."""
    broker = make_paper_broker()
    t1 = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    broker.accept_tick(t1)

    req = make_order(
        client_order_id="s2-sl",
        side=1,
        sl_price=1.0950,
        tp_price=1.1050,
    )
    broker_order_id = broker.submit_order(req)
    status_info = broker.get_order_status(broker_order_id)
    assert status_info["status"] == OrderStatus.FILLED.value
    assert len(broker.get_account_info().open_positions) == 1

    # Tick 2 breaches SL threshold (bid <= 1.0950 for long exit)
    t2 = make_tick(when=NOW + timedelta(seconds=10), bid=1.0948, ask=1.0950)
    accepted = broker.accept_tick(t2)
    assert accepted is True

    closes = broker.drain_close_events()
    assert len(closes) == 1
    close_event: PositionClose = closes[0]
    assert close_event.client_entry_order_id == "s2-sl"
    assert close_event.reason == CloseReason.STOP_LOSS
    assert close_event.exit_price == pytest.approx(1.0948)
    assert len(broker.get_account_info().open_positions) == 0


# ---------------------------------------------------------------------------
# Scenario 3: Gap Through Stop Loss
# ---------------------------------------------------------------------------
def test_replay_scenario_03_gap_through_stop() -> None:
    """Scenario 3: Gap through SL fills at the gap market price, not nominal SL."""
    broker = make_paper_broker()
    t1 = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    broker.accept_tick(t1)

    req = make_order(
        client_order_id="s3-gap-sl",
        side=1,
        sl_price=1.0950,
        tp_price=1.1050,
    )
    broker.submit_order(req)
    assert len(broker.get_account_info().open_positions) == 1

    # Weekend / event gap down far below SL (bid = 1.0900 vs SL = 1.0950)
    gap_tick = make_tick(when=NOW + timedelta(minutes=15), bid=1.0900, ask=1.0902)
    accepted = broker.accept_tick(gap_tick)
    assert accepted is True

    closes = broker.drain_close_events()
    assert len(closes) == 1
    close_event: PositionClose = closes[0]
    assert close_event.reason == CloseReason.STOP_LOSS
    # Production invariant: exit fill must be actual executable market quote (1.0900),
    # NOT the nominal stop price (1.0950), proving adverse gap execution.
    assert close_event.exit_price == pytest.approx(1.0900)
    assert close_event.exit_price < 1.0950
    assert len(broker.get_account_info().open_positions) == 0


# ---------------------------------------------------------------------------
# Scenario 4: Gap Through Take Profit
# ---------------------------------------------------------------------------
def test_replay_scenario_04_gap_through_take_profit() -> None:
    """Scenario 4: Gap through TP fills at favorable gap quote, not capped at TP."""
    broker = make_paper_broker()
    t1 = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    broker.accept_tick(t1)

    req = make_order(
        client_order_id="s4-gap-tp",
        side=1,
        sl_price=1.0950,
        tp_price=1.1050,
    )
    broker.submit_order(req)
    assert len(broker.get_account_info().open_positions) == 1

    # Gap up far above TP (bid = 1.1100 vs TP = 1.1050)
    gap_tick = make_tick(when=NOW + timedelta(minutes=15), bid=1.1100, ask=1.1102)
    accepted = broker.accept_tick(gap_tick)
    assert accepted is True

    closes = broker.drain_close_events()
    assert len(closes) == 1
    close_event: PositionClose = closes[0]
    assert close_event.reason == CloseReason.TAKE_PROFIT
    # Production invariant: exit fill is the favorable market quote (1.1100),
    # NOT artificially capped at nominal TP (1.1050).
    assert close_event.exit_price == pytest.approx(1.1100)
    assert close_event.exit_price > 1.1050
    assert len(broker.get_account_info().open_positions) == 0


# ---------------------------------------------------------------------------
# Scenario 5: Spread Widening
# ---------------------------------------------------------------------------
def test_replay_scenario_05_spread_widening() -> None:
    """Scenario 5: Widened spread directly impacts entry fills for buy and sell."""
    broker = make_paper_broker()

    # Normal spread tick (spread = 2 pips = 0.0002)
    normal_tick = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    broker.accept_tick(normal_tick)

    # Wide spread tick (spread = 20 pips = 0.0020)
    wide_tick = make_tick(
        when=NOW + timedelta(seconds=5),
        bid=1.0990,
        ask=1.1010,
    )
    broker.accept_tick(wide_tick)

    # Buy order fills at wide ask
    buy_order = make_order(client_order_id="s5-buy-wide", side=1)
    broker.submit_order(buy_order)
    acct = broker.get_account_info()
    buy_pos = next(p for p in acct.open_positions if p.side == 1)
    assert buy_pos.entry_price == pytest.approx(1.1010)

    # Sell order fills at wide bid
    sell_order = make_order(client_order_id="s5-sell-wide", side=-1)
    broker.submit_order(sell_order)
    acct = broker.get_account_info()
    sell_pos = next(p for p in acct.open_positions if p.side == -1)
    assert sell_pos.entry_price == pytest.approx(1.0990)


# ---------------------------------------------------------------------------
# Scenario 6: Slippage Modeling
# ---------------------------------------------------------------------------
def test_replay_scenario_06_slippage() -> None:
    """Scenario 6: CostConfig with slippage properly shifts fill price beyond raw quote."""
    # Cost model with 1.5 pips of slippage on EURUSD (0.00015)
    cost_cfg = CostConfig(
        default=CostDefaults(
            spread_pips=0.0,
            commission_per_lot_roundturn=0.0,
            slippage_pips_base=1.5,
            slippage_vol_coeff=0.0,
            latency_bars=0,
        )
    )
    broker = make_paper_broker(cost_config=cost_cfg)
    t1 = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    broker.accept_tick(t1)

    # Long order: raw ask is 1.1002 + 0.00015 slippage = 1.10035
    buy_req = make_order(client_order_id="s6-slip-buy", side=1)
    broker.submit_order(buy_req)
    acct = broker.get_account_info()
    buy_pos = next(p for p in acct.open_positions if p.side == 1)
    assert buy_pos.entry_price == pytest.approx(1.10035)

    # Short order: raw bid is 1.1000 - 0.00015 slippage = 1.09985
    sell_req = make_order(client_order_id="s6-slip-sell", side=-1)
    broker.submit_order(sell_req)
    acct = broker.get_account_info()
    sell_pos = next(p for p in acct.open_positions if p.side == -1)
    assert sell_pos.entry_price == pytest.approx(1.09985)


# ---------------------------------------------------------------------------
# Scenario 7: Stale Market Data
# ---------------------------------------------------------------------------
def test_replay_scenario_07_stale_market_data() -> None:
    """Scenario 7: Quotes older than valuation max_age fail freshness check."""
    engine = FxValuationEngine(catalog=CATALOG, max_age=timedelta(seconds=10))
    conversion_quote = ConversionQuote(
        canonical_instrument="USDJPY",
        bid=150.00,
        ask=150.02,
        observation_time=NOW,
        source_identity="test-source",
    )

    # Fresh valuation query at NOW + 5s (<= 10s max_age) succeeds
    fresh_val = engine.pip_valuation(
        symbol="USDJPY",
        account_currency="USD",
        as_of=NOW + timedelta(seconds=5),
        quotes=[conversion_quote],
    )
    assert fresh_val.pip_value_per_lot > 0.0

    # Stale valuation query at NOW + 20s (> 10s max_age) fails closed
    with pytest.raises(ValuationFailure, match="stale_conversion_quote"):
        engine.pip_valuation(
            symbol="USDJPY",
            account_currency="USD",
            as_of=NOW + timedelta(seconds=20),
            quotes=[conversion_quote],
        )


# ---------------------------------------------------------------------------
# Scenario 8: Out-of-Order Market Data
# ---------------------------------------------------------------------------
def test_replay_scenario_08_out_of_order_market_data() -> None:
    """Scenario 8: Out-of-order ticks are rejected by PaperBroker and MarketDataStream."""
    broker = make_paper_broker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    t1 = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    assert broker.accept_tick(t1) is not False
    stream.on_tick(t1)
    assert stream.get_latest_tick("EURUSD") == t1

    # Out-of-order tick (earlier timestamp)
    t_old = make_tick(when=NOW - timedelta(seconds=10), bid=1.0990, ask=1.0992)
    # PaperBroker rejects tick
    broker_accepted = broker.accept_tick(t_old)
    assert broker_accepted is False

    # MarketDataStream ignores tick and preserves latest
    stream.on_tick(t_old)
    assert stream.get_latest_tick("EURUSD") == t1


# ---------------------------------------------------------------------------
# Scenario 9: Duplicate Market Event
# ---------------------------------------------------------------------------
def test_replay_scenario_09_duplicate_market_event() -> None:
    """Scenario 9: Tick aggregation and historical bar combination deduplicate identical bars."""
    broker = make_paper_broker()
    stream = MarketDataStream(broker=broker, symbols=["EURUSD"])

    # Provide closed historical bars for EURUSD at 10:00:00
    idx = pd.to_datetime(["2026-08-25 10:00:00+00:00"])
    bars = pd.DataFrame(
        {
            "open": [1.1000],
            "high": [1.1005],
            "low": [1.0995],
            "close": [1.1002],
            "volume": [10.0],
        },
        index=idx,
    )
    broker.get_historical_bars = lambda sym, tf, count: bars  # type: ignore[assignment]

    # Ingest multiple ticks in the same 10:00:00 bucket
    t1 = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    t2 = make_tick(when=NOW + timedelta(seconds=30), bid=1.1003, ask=1.1005)
    t3 = make_tick(when=NOW + timedelta(seconds=60), bid=1.1001, ask=1.1003)
    stream.on_tick(t1)
    stream.on_tick(t2)
    stream.on_tick(t3)

    # Ingest tick at 10:06:00 to advance authoritative watermark past bar closure
    t_close = make_tick(when=NOW + timedelta(minutes=6), bid=1.1002, ask=1.1004)
    stream.on_tick(t_close)

    # Closed bars fetch combines tick bars and historical bars, deduplicating ts_open
    closed = stream.get_closed_bars("EURUSD", "M5", count=10)

    # Exactly 1 closed bar for 10:00:00 exists, with zero duplicate rows
    assert len(closed) == 1
    assert not closed.index.has_duplicates
    assert closed.index[0] == pd.Timestamp("2026-08-25 10:00:00+00:00")


# ---------------------------------------------------------------------------
# Scenario 10: Duplicate Logical Order
# ---------------------------------------------------------------------------
def test_replay_scenario_10_duplicate_logical_order() -> None:
    """Scenario 10: Duplicate client_order_id is rejected without duplicate mutation."""
    broker = make_paper_broker()
    t1 = make_tick(when=NOW, bid=1.1000, ask=1.1002)
    broker.accept_tick(t1)

    req1 = make_order(client_order_id="dup-order-001")
    broker_order_id = broker.submit_order(req1)
    status_info = broker.get_order_status(broker_order_id)
    assert status_info["status"] == OrderStatus.FILLED.value
    assert len(broker.get_account_info().open_positions) == 1

    # Attempt to submit same client_order_id a second time
    req2 = make_order(client_order_id="dup-order-001")
    with pytest.raises(ValueError, match="duplicate client order ID"):
        broker.submit_order(req2)

    # Position count remains exactly 1 and state is intact
    assert len(broker.get_account_info().open_positions) == 1
