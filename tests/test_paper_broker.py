"""Tests for the deterministic Phase 6 paper broker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import inf, nan

import pandas as pd
import pytest

from fxlab.config import CostConfig, CostDefaults
from fxlab.execution.broker import BrokerAdapter, OrderRequest, OrderStatus, Tick
from fxlab.execution.paper_broker import CloseReason, PaperBroker

NOW = datetime(2026, 8, 25, 10, 5, tzinfo=UTC)


def tick(
    symbol: str = "EURUSD",
    when: datetime = NOW,
    *,
    bid: float = 1.0998,
    ask: float = 1.1000,
    mid: float | None = None,
) -> Tick:
    return Tick(symbol, when, bid=bid, ask=ask, mid=(bid + ask) / 2 if mid is None else mid)


def order(
    order_id: str = "client-1",
    side: int = 1,
    *,
    symbol: str = "EURUSD",
    size: float = 0.25,
    sl_price: float | None = 1.09,
    tp_price: float | None = None,
) -> OrderRequest:
    return OrderRequest(
        symbol,
        side,
        size,
        "market",
        order_id,
        sl_price=sl_price,
        tp_price=tp_price,
    )


def connected_broker() -> PaperBroker:
    broker = PaperBroker()
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])
    return broker


def cost_config(
    *, spread: float = 0.0, commission: float = 0.0, slippage: float = 0.0,
    stress: float = 1.5,
) -> CostConfig:
    return CostConfig(
        default=CostDefaults(
            spread_pips=spread,
            commission_per_lot_roundturn=commission,
            slippage_pips_base=slippage,
            slippage_vol_coeff=0.0,
            latency_bars=1,
        ),
        stress_factor=stress,
    )


def accounting_broker(
    *, initial_balance: float = 10_000.0, costs: CostConfig | None = None
) -> PaperBroker:
    broker = PaperBroker(
        initial_balance=initial_balance,
        cost_config=costs or cost_config(),
    )
    broker.connect()
    broker.subscribe_market_data(["EURUSD", "GBPUSD"])
    return broker


def bars() -> pd.DataFrame:
    index = pd.date_range("2026-08-25 10:00", periods=2, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.09, 1.10],
            "high": [1.11, 1.12],
            "low": [1.08, 1.09],
            "close": [1.10, 1.11],
            "volume": [10.0, 11.0],
        },
        index=index,
    )


def test_connection_lifecycle_and_protocol() -> None:
    broker = PaperBroker()
    assert isinstance(broker, BrokerAdapter)
    assert not broker.is_connected()
    broker.connect()
    assert broker.is_connected()
    broker.disconnect()
    assert not broker.is_connected()


def test_accept_tick_requires_connection_and_subscription() -> None:
    broker = PaperBroker()
    with pytest.raises(RuntimeError):
        broker.accept_tick(tick())
    broker.connect()
    with pytest.raises(ValueError, match="not subscribed"):
        broker.accept_tick(tick())


def test_latest_tick_and_out_of_order_protection() -> None:
    broker = connected_broker()
    newest = tick(when=NOW)
    assert broker.accept_tick(newest)
    assert not broker.accept_tick(tick(when=NOW - timedelta(seconds=1)))
    assert broker.get_latest_tick("eurusd") == newest


@pytest.mark.parametrize(("side", "expected"), [(1, 1.10002), (-1, 1.09978)])
def test_market_fill_uses_executable_side(side: int, expected: float) -> None:
    broker = connected_broker()
    broker.accept_tick(tick())
    broker_id = broker.submit_order(order(side=side))
    position = broker.get_account_info().open_positions[0]
    assert position.entry_price == pytest.approx(expected)
    assert position.side == side
    assert broker.get_order_status(broker_id)["status"] == OrderStatus.FILLED.value


def test_missing_quote_rejects_order() -> None:
    broker = connected_broker()
    with pytest.raises(ValueError, match="no executable quote"):
        broker.submit_order(order())


def test_duplicate_client_order_id_rejected() -> None:
    broker = connected_broker()
    broker.accept_tick(tick())
    broker.submit_order(order())
    with pytest.raises(ValueError, match="duplicate"):
        broker.submit_order(order())


def test_deterministic_order_position_correlation() -> None:
    broker = connected_broker()
    broker.accept_tick(tick())
    broker_id = broker.submit_order(order("alpha"))
    correlation = broker.get_correlation("alpha")
    assert broker_id == "paper-order::alpha"
    assert correlation is not None
    assert correlation.broker_order_id == "paper-order::alpha"
    assert correlation.position_id == "paper-position::alpha"
    assert broker.get_account_info().open_positions[0].position_id == correlation.position_id


def test_account_snapshot_is_not_mutable_broker_state() -> None:
    broker = connected_broker()
    broker.accept_tick(tick())
    broker.submit_order(order())
    snapshot = broker.get_account_info()
    snapshot.open_positions.clear()
    assert len(broker.get_account_info().open_positions) == 1


def test_historical_bars_never_expose_future_rows() -> None:
    broker = PaperBroker(historical_bars={("EURUSD", "M5"): bars()})
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])
    assert broker.get_historical_bars("EURUSD", "M5", 10).empty
    broker.accept_tick(tick(when=NOW))
    available = broker.get_historical_bars("EURUSD", "M5", 10)
    assert list(available.index) == [pd.Timestamp("2026-08-25 10:00", tz="UTC")]


def test_broker_has_no_persistence_or_network_surface() -> None:
    broker = PaperBroker()
    assert not hasattr(broker, "save")
    assert not hasattr(broker, "load")
    assert not hasattr(broker, "request")
    assert broker.close_position("unknown") is None


@pytest.mark.parametrize(
    ("side", "next_bid", "next_ask", "expected"),
    [
        (1, 1.1010, 1.1012, 25.0),
        (1, 1.0990, 1.0992, -25.0),
        (-1, 1.0986, 1.0988, 25.0),
        (-1, 1.1008, 1.1010, -30.0),
    ],
)
def test_long_and_short_mark_to_market(
    side: int, next_bid: float, next_ask: float, expected: float
) -> None:
    broker = accounting_broker()
    broker.accept_tick(tick())
    broker.submit_order(order(side=side, sl_price=None))
    broker.accept_tick(
        tick(when=NOW + timedelta(minutes=5), bid=next_bid, ask=next_ask)
    )
    account = broker.get_account_info()
    assert account.open_positions[0].unrealized_pnl == pytest.approx(expected)
    assert account.equity == pytest.approx(account.balance + expected)
    assert account.margin_used == 0.0
    assert account.margin_available == account.equity


@pytest.mark.parametrize(("side", "expected_exit"), [(1, 1.1010), (-1, 1.1012)])
def test_manual_close_uses_bid_for_long_and_ask_for_short(
    side: int, expected_exit: float
) -> None:
    broker = accounting_broker()
    broker.accept_tick(tick())
    broker.submit_order(order(side=side, sl_price=None))
    broker.accept_tick(tick(when=NOW + timedelta(minutes=5), bid=1.1010, ask=1.1012))
    position_id = broker.get_account_info().open_positions[0].position_id
    close_id = broker.close_position(position_id)
    event = broker.drain_close_events()[0]
    assert close_id == f"paper-close::{position_id}"
    assert event.exit_price == expected_exit
    assert event.reason is CloseReason.MANUAL
    assert broker.get_account_info().open_positions == []
    assert broker.close_position(position_id) is None
    assert broker.drain_close_events() == ()


def test_multiple_position_equity_and_unrelated_symbol_last_mark() -> None:
    broker = accounting_broker()
    broker.accept_tick(tick(bid=1.1, ask=1.1))
    broker.accept_tick(tick("GBPUSD", bid=1.2, ask=1.2))
    broker.submit_order(order("eur", size=0.1, sl_price=None))
    broker.submit_order(
        order("gbp", symbol="GBPUSD", size=0.2, sl_price=None)
    )
    broker.accept_tick(
        tick(when=NOW + timedelta(minutes=5), bid=1.101, ask=1.101)
    )
    account = broker.get_account_info()
    pnl = {position.symbol: position.unrealized_pnl for position in account.open_positions}
    assert pnl["EURUSD"] == pytest.approx(10.0)
    assert pnl["GBPUSD"] == pytest.approx(0.0)
    assert account.equity == pytest.approx(10_010.0)


def test_realized_win_loss_and_negative_balance_are_recorded() -> None:
    winner = accounting_broker()
    winner.accept_tick(tick(bid=1.1, ask=1.1))
    winner.submit_order(order(size=1.0, sl_price=None))
    winner.accept_tick(tick(when=NOW + timedelta(minutes=5), bid=1.101, ask=1.101))
    position_id = winner.get_account_info().open_positions[0].position_id
    winner.close_position(position_id)
    win = winner.drain_close_events()[0]
    assert win.gross_pnl == pytest.approx(100.0)
    assert win.net_realized_pnl == pytest.approx(100.0)
    assert winner.get_account_info().balance == pytest.approx(10_100.0)

    loser = accounting_broker(initial_balance=10.0)
    loser.accept_tick(tick(bid=1.1, ask=1.1))
    loser.submit_order(order(size=1.0, sl_price=None))
    loser.accept_tick(tick(when=NOW + timedelta(minutes=5), bid=0.5, ask=0.5))
    position_id = loser.get_account_info().open_positions[0].position_id
    loser.close_position(position_id)
    loss = loser.drain_close_events()[0]
    assert loss.net_realized_pnl == pytest.approx(-60_000.0)
    assert loser.get_account_info().balance == pytest.approx(-59_990.0)
    assert loser.get_account_info().equity == pytest.approx(-59_990.0)


def test_commission_is_charged_exactly_once() -> None:
    broker = accounting_broker(costs=cost_config(commission=7.0))
    broker.accept_tick(tick(bid=1.1, ask=1.1))
    broker.submit_order(order(size=2.0, sl_price=None))
    position_id = broker.get_account_info().open_positions[0].position_id
    broker.close_position(position_id)
    event = broker.drain_close_events()[0]
    assert event.gross_pnl == pytest.approx(0.0)
    assert event.commission == pytest.approx(14.0)
    assert event.net_realized_pnl == pytest.approx(-14.0)
    assert broker.get_account_info().balance == pytest.approx(9_986.0)


def test_cost_model_does_not_double_count_real_spread() -> None:
    broker = accounting_broker(costs=cost_config(spread=100.0))
    broker.accept_tick(tick(bid=1.0998, ask=1.1))
    broker.submit_order(order(sl_price=None))
    assert broker.get_account_info().open_positions[0].entry_price == 1.1


def test_zero_spread_uses_fallback_and_base_slippage_without_stress() -> None:
    costs = cost_config(spread=2.0, slippage=0.5, stress=100.0)
    broker = accounting_broker(costs=costs)
    broker.accept_tick(tick(bid=1.1, ask=1.1))
    broker.submit_order(order(sl_price=None))
    position = broker.get_account_info().open_positions[0]
    assert position.entry_price == pytest.approx(1.10015)


@pytest.mark.parametrize(
    ("side", "sl", "tp", "bid", "ask", "reason"),
    [
        (1, 1.09, 1.12, 1.09, 1.0902, CloseReason.STOP_LOSS),
        (1, 1.09, 1.12, 1.12, 1.1202, CloseReason.TAKE_PROFIT),
        (-1, 1.11, 1.08, 1.1098, 1.11, CloseReason.STOP_LOSS),
        (-1, 1.11, 1.08, 1.0798, 1.08, CloseReason.TAKE_PROFIT),
    ],
)
def test_close_only_sl_tp_equality_boundaries(
    side: int,
    sl: float,
    tp: float,
    bid: float,
    ask: float,
    reason: CloseReason,
) -> None:
    broker = accounting_broker()
    broker.accept_tick(tick(bid=1.1, ask=1.1))
    broker.submit_order(order(side=side, sl_price=sl, tp_price=tp))
    assert broker.drain_close_events() == ()
    broker.accept_tick(tick(when=NOW + timedelta(minutes=5), bid=bid, ask=ask))
    event = broker.drain_close_events()[0]
    assert event.reason is reason


def test_gap_through_stop_fills_at_worse_current_quote() -> None:
    broker = accounting_broker()
    broker.accept_tick(tick(bid=1.1, ask=1.1))
    broker.submit_order(order(sl_price=1.09))
    broker.accept_tick(tick(when=NOW + timedelta(minutes=5), bid=1.08, ask=1.0802))
    event = broker.drain_close_events()[0]
    assert event.reason is CloseReason.STOP_LOSS
    assert event.exit_price == 1.08


def test_new_position_is_not_closed_on_its_entry_tick() -> None:
    broker = accounting_broker()
    broker.accept_tick(tick(bid=1.1, ask=1.1))
    broker.submit_order(order(sl_price=1.11))
    assert broker.drain_close_events() == ()
    assert len(broker.get_account_info().open_positions) == 1
    broker.accept_tick(tick(when=NOW + timedelta(minutes=5), bid=1.1, ask=1.1))
    assert broker.drain_close_events()[0].reason is CloseReason.STOP_LOSS


def test_historical_high_low_do_not_trigger_close_without_quote_crossing() -> None:
    frame = bars()
    frame.iloc[1, frame.columns.get_loc("high")] = 1.5
    frame.iloc[1, frame.columns.get_loc("low")] = 0.5
    broker = PaperBroker(
        historical_bars={("EURUSD", "M5"): frame},
        cost_config=cost_config(),
    )
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])
    broker.accept_tick(tick(bid=1.1, ask=1.1))
    broker.submit_order(order(sl_price=1.09, tp_price=1.11))
    broker.accept_tick(tick(when=NOW + timedelta(minutes=5), bid=1.1, ask=1.1))
    assert broker.drain_close_events() == ()
    assert len(broker.get_account_info().open_positions) == 1


@pytest.mark.parametrize("bad", [nan, inf])
def test_non_finite_tick_rejected_without_account_mutation(bad: float) -> None:
    broker = accounting_broker()
    before = broker.get_account_info()
    with pytest.raises(ValueError, match="finite and positive"):
        broker.accept_tick(tick(bid=bad, ask=bad, mid=bad))
    after = broker.get_account_info()
    assert after == before
