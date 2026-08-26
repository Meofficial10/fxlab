"""Tests for the deterministic, decision-only Phase 4 risk engine."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from math import inf, nan

import pytest

from fxlab.config import CostConfig
from fxlab.execution.broker import AccountInfo, Position
from fxlab.execution.signal_engine import SignalEvent
from fxlab.risk import (
    KillSwitchReason,
    PipSizeResolver,
    RiskDecision,
    RiskEngine,
    RiskLimits,
    RiskRejection,
)

NOW = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)


class Resolver:
    def pip_size_for(self, symbol: str) -> float:
        return 0.01 if symbol.endswith("JPY") else 0.0001


class RaisingResolver:
    def pip_size_for(self, symbol: str) -> float:
        raise ValueError("unavailable")


class ValueResolver:
    def __init__(self, value: object) -> None:
        self.value = value

    def pip_size_for(self, symbol: str) -> object:
        return self.value


def signal(**changes: object) -> SignalEvent:
    values = {
        "setup_name": "model_a_sweep_reversal",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "side": 1,
        "signal_time": NOW - timedelta(minutes=5),
        "signal_bar_index": 100,
    }
    values.update(changes)
    return SignalEvent(**values)  # type: ignore[arg-type]


def account(equity: float = 10_000.0, positions: list[Position] | None = None) -> AccountInfo:
    return AccountInfo(
        balance=10_000.0,
        equity=equity,
        margin_used=0.0,
        margin_available=equity,
        open_positions=[] if positions is None else positions,
    )


def position(**changes: object) -> Position:
    values = {
        "symbol": "EURUSD",
        "side": 1,
        "size": 0.2,
        "entry_price": 1.1,
        "entry_time": NOW - timedelta(hours=1),
        "unrealized_pnl": 0.0,
        "position_id": "position-1",
    }
    values.update(changes)
    return Position(**values)  # type: ignore[arg-type]


def engine(**changes: object) -> RiskEngine:
    values = {"pip_size_resolver": Resolver()}
    values.update(changes)
    return RiskEngine(**values)  # type: ignore[arg-type]


def evaluate(risk_engine: RiskEngine, event: SignalEvent | None = None, **changes: object):
    values = {
        "entry_price": 1.1000,
        "sl_price": 1.0950,
        "tp_price": 1.1100,
        "account": account(),
        "current_time": NOW,
    }
    values.update(changes)
    return risk_engine.evaluate(event or signal(), **values)


def assert_rejected(result: object, reason: str) -> RiskRejection:
    assert isinstance(result, RiskRejection)
    assert result.approved is False
    assert result.reason == reason
    return result


def test_public_contracts_are_frozen_and_resolver_is_structural():
    limits = RiskLimits()
    rejection = RiskRejection("invalid", "bad", None, None, None)
    assert isinstance(CostConfig(), PipSizeResolver)
    with pytest.raises(FrozenInstanceError):
        limits.max_trades_per_day = 3  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        rejection.reason = "changed"  # type: ignore[misc]


def test_kill_switch_reason_values_are_stable():
    assert KillSwitchReason.MAX_DRAWDOWN.value == "max_drawdown_breached"
    assert KillSwitchReason.MAX_DAILY_LOSS.value == "max_daily_loss_breached"
    assert KillSwitchReason.MAX_CONSECUTIVE_LOSSES.value == "max_consecutive_losses_breached"
    assert KillSwitchReason.RUIN_THRESHOLD.value == "ruin_threshold_breached"
    assert KillSwitchReason.POSITION_RECONCILIATION_FAILED.value == "position_sync_failed"
    assert KillSwitchReason.MANUAL.value == "manual_shutdown"


def test_normal_long_percentage_pip_sizing_and_stop_pips():
    result = evaluate(engine())
    assert isinstance(result, RiskDecision)
    assert result.approved is True
    assert result.pip_size == pytest.approx(0.0001)
    assert result.stop_pips == pytest.approx(50.0)
    assert result.monetary_risk_budget == pytest.approx(100.0)
    assert result.size_lots == pytest.approx(0.2)
    assert result.modeled_monetary_risk == pytest.approx(100.0)


def test_normal_short_and_jpy_pip_sizing():
    event = signal(symbol="USDJPY", side=-1)
    result = evaluate(
        engine(), event, entry_price=150.0, sl_price=150.5, tp_price=149.0
    )
    assert isinstance(result, RiskDecision)
    assert result.pip_size == pytest.approx(0.01)
    assert result.stop_pips == pytest.approx(50.0)
    assert result.size_lots == pytest.approx(0.2)


def test_optional_take_profit_can_be_omitted():
    result = evaluate(engine(), tp_price=None)
    assert isinstance(result, RiskDecision)
    assert result.tp_price is None


def test_lot_size_is_floored_and_never_exceeds_risk_budget():
    result = evaluate(engine(), sl_price=1.0967)
    assert isinstance(result, RiskDecision)
    assert result.size_lots == pytest.approx(0.3)
    assert result.size_lots <= result.monetary_risk_budget / (
        result.stop_pips * 10.0
    )
    assert result.modeled_monetary_risk <= result.monetary_risk_budget + 1e-10


def test_nearest_rounding_would_violate_budget_but_floor_does_not():
    result = evaluate(engine(), sl_price=1.09685)
    assert isinstance(result, RiskDecision)
    raw_lots = result.monetary_risk_budget / (result.stop_pips * 10.0)
    assert raw_lots == pytest.approx(0.31746031746)
    assert result.size_lots == pytest.approx(0.31)
    assert result.size_lots < raw_lots


@pytest.mark.parametrize(
    ("lot_step", "expected_size"), [(0.1, 0.3), (0.001, 0.317)]
)
def test_configurable_lot_steps_floor_deterministically(
    lot_step: float, expected_size: float
):
    result = evaluate(engine(lot_step=lot_step), sl_price=1.09685)
    assert isinstance(result, RiskDecision)
    assert result.size_lots == pytest.approx(expected_size)
    assert result.modeled_monetary_risk <= result.monetary_risk_budget


def test_below_minimum_lot_is_rejected():
    result = evaluate(engine(), sl_price=0.0001)
    assert_rejected(result, "size_below_minimum")


@pytest.mark.parametrize("side", [1, -1])
def test_gross_same_and_opposite_side_exposure(side: int):
    limits = RiskLimits(max_open_positions=3, max_exposure_per_symbol_lots=0.25)
    result = evaluate(engine(limits=limits), account=account(positions=[position(side=side)]))
    assert isinstance(result, RiskDecision)
    assert result.size_lots == pytest.approx(0.05)


def test_separate_symbol_does_not_consume_symbol_exposure():
    limits = RiskLimits(max_open_positions=2, max_exposure_per_symbol_lots=0.25)
    other = position(symbol="GBPUSD")
    result = evaluate(engine(limits=limits), account=account(positions=[other]))
    assert isinstance(result, RiskDecision)
    assert result.size_lots == pytest.approx(0.2)


def test_max_open_positions_is_checked_before_exposure():
    result = evaluate(engine(), account=account(positions=[position(symbol="GBPUSD")]))
    assert_rejected(result, "max_open_positions")


def test_configurable_multi_position_cap_and_zero_remaining_exposure():
    limits = RiskLimits(max_open_positions=3, max_exposure_per_symbol_lots=0.2)
    result = evaluate(engine(limits=limits), account=account(positions=[position(size=0.2)]))
    assert_rejected(result, "symbol_exposure_exhausted")


def test_concurrent_distinct_approvals_respect_open_position_reservations():
    limits = RiskLimits(
        max_open_positions=1,
        max_exposure_per_symbol_lots=10.0,
        max_trades_per_day=10,
    )
    risk_engine = engine(limits=limits)
    events = [
        signal(signal_time=NOW - timedelta(minutes=offset)) for offset in (5, 10)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda event: evaluate(risk_engine, event), events))
    assert sum(isinstance(result, RiskDecision) for result in results) == 1
    assert sum(
        isinstance(result, RiskRejection) and result.reason == "max_open_positions"
        for result in results
    ) == 1
    assert risk_engine.reserved_position_count == 1


def test_concurrent_distinct_approvals_respect_symbol_reservations():
    limits = RiskLimits(
        max_open_positions=10,
        max_exposure_per_symbol_lots=0.3,
        max_trades_per_day=10,
    )
    risk_engine = engine(limits=limits)
    events = [
        signal(signal_time=NOW - timedelta(minutes=offset)) for offset in (5, 10)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda event: evaluate(risk_engine, event), events))
    decisions = [result for result in results if isinstance(result, RiskDecision)]
    assert len(decisions) == 2
    assert sum(decision.size_lots for decision in decisions) == pytest.approx(0.3)


def test_sequential_distinct_approvals_respect_stale_snapshot_reservations():
    limits = RiskLimits(
        max_open_positions=2,
        max_exposure_per_symbol_lots=0.3,
        max_trades_per_day=3,
    )
    risk_engine = engine(limits=limits)
    first = evaluate(risk_engine)
    second = evaluate(risk_engine, signal(signal_time=NOW - timedelta(minutes=10)))
    assert isinstance(first, RiskDecision)
    assert isinstance(second, RiskDecision)
    assert first.size_lots + second.size_lots == pytest.approx(0.3)


def test_opposite_side_approval_consumes_gross_reserved_exposure():
    limits = RiskLimits(
        max_open_positions=2,
        max_exposure_per_symbol_lots=0.3,
        max_trades_per_day=3,
    )
    risk_engine = engine(limits=limits)
    first = evaluate(risk_engine)
    second = evaluate(
        risk_engine,
        signal(side=-1),
        sl_price=1.105,
        tp_price=1.09,
    )
    assert isinstance(first, RiskDecision)
    assert isinstance(second, RiskDecision)
    assert first.size_lots == pytest.approx(0.2)
    assert second.size_lots == pytest.approx(0.1)


def test_release_removes_reservation_but_preserves_history_and_daily_count():
    limits = RiskLimits(max_open_positions=1, max_trades_per_day=3)
    risk_engine = engine(limits=limits)
    first = evaluate(risk_engine)
    assert isinstance(first, RiskDecision)
    assert risk_engine.release_approval(first.order_id) is True
    assert risk_engine.reserved_position_count == 0
    assert risk_engine.daily_trades == 1
    assert_rejected(evaluate(risk_engine), "duplicate_approval")
    second = evaluate(risk_engine, signal(signal_time=NOW - timedelta(minutes=10)))
    assert isinstance(second, RiskDecision)
    assert risk_engine.daily_trades == 2


def test_unknown_reservation_release_returns_false():
    risk_engine = engine()
    assert risk_engine.release_approval("unknown-order") is False
    assert risk_engine.release_approval(123) is False  # type: ignore[arg-type]


def test_released_reservation_and_refreshed_snapshot_are_not_double_counted():
    limits = RiskLimits(
        max_open_positions=2,
        max_exposure_per_symbol_lots=0.3,
        max_trades_per_day=3,
    )
    risk_engine = engine(limits=limits)
    first = evaluate(risk_engine)
    assert isinstance(first, RiskDecision)
    assert risk_engine.release_approval(first.order_id)
    reflected = position(size=first.size_lots)
    second = evaluate(
        risk_engine,
        signal(signal_time=NOW - timedelta(minutes=10)),
        account=account(positions=[reflected]),
    )
    assert isinstance(second, RiskDecision)
    assert second.size_lots == pytest.approx(0.1)


@pytest.mark.parametrize("bad_account", [None, object()])
def test_invalid_account_object_is_rejected(bad_account: object):
    assert_rejected(evaluate(engine(), account=bad_account), "invalid_account")


@pytest.mark.parametrize("equity", [0.0, -1.0, nan, inf, -inf])
def test_invalid_equity_is_rejected(equity: float):
    assert_rejected(evaluate(engine(), account=account(equity)), "invalid_equity")


def test_open_positions_must_be_a_list():
    snapshot = account()
    snapshot.open_positions = ()  # type: ignore[assignment]
    assert_rejected(evaluate(engine(), account=snapshot), "invalid_open_positions")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", " "),
        ("side", 0),
        ("side", 1.0),
        ("size", 0.0),
        ("size", -0.1),
        ("size", nan),
        ("size", inf),
        ("entry_price", nan),
        ("entry_price", 0.0),
        ("entry_time", datetime(2026, 8, 25)),
        ("unrealized_pnl", nan),
        ("position_id", ""),
    ],
)
def test_malformed_position_contents_are_rejected(field: str, value: object):
    item = position()
    setattr(item, field, value)
    result = evaluate(
        engine(limits=RiskLimits(max_open_positions=2)), account=account(positions=[item])
    )
    assert_rejected(result, "invalid_position")


def test_non_position_in_snapshot_is_rejected():
    result = evaluate(
        engine(limits=RiskLimits(max_open_positions=2)),
        account=account(positions=[object()]),  # type: ignore[list-item]
    )
    assert_rejected(result, "invalid_position")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"side": 0}, "invalid_signal_side"),
        ({"side": 1.0}, "invalid_signal_side"),
        ({"symbol": ""}, "invalid_signal_symbol"),
        ({"setup_name": " "}, "invalid_setup_name"),
        ({"timeframe": ""}, "invalid_timeframe"),
        ({"signal_time": datetime(2026, 8, 25)}, "invalid_signal_time"),
        ({"signal_bar_index": -1}, "invalid_signal_bar_index"),
        ({"signal_bar_index": True}, "invalid_signal_bar_index"),
    ],
)
def test_invalid_signal_fields_are_rejected(changes: dict[str, object], reason: str):
    assert_rejected(evaluate(engine(), signal(**changes)), reason)


def test_invalid_signal_object_is_rejected():
    result = engine().evaluate(  # type: ignore[arg-type]
        object(),
        entry_price=1.1,
        sl_price=1.09,
        tp_price=None,
        account=account(),
        current_time=NOW,
    )
    assert_rejected(result, "invalid_signal")


def test_unsafe_order_identity_tokens_are_rejected_without_normalization_collision():
    assert_rejected(
        evaluate(engine(), signal(setup_name="model/a")), "invalid_setup_name"
    )
    assert_rejected(evaluate(engine(), signal(symbol="EUR/USD")), "invalid_signal_symbol")


def test_current_time_must_be_aware_and_signal_cannot_be_in_future():
    assert_rejected(
        evaluate(engine(), current_time=datetime(2026, 8, 25)), "invalid_current_time"
    )
    assert_rejected(
        evaluate(engine(), signal(signal_time=NOW + timedelta(seconds=1))),
        "future_signal_time",
    )


@pytest.mark.parametrize("entry", [0.0, -1.0, nan, inf, "bad", True])
def test_invalid_entry_is_rejected(entry: object):
    assert_rejected(evaluate(engine(), entry_price=entry), "invalid_entry_price")


@pytest.mark.parametrize("sl", [None, 0.0, -1.0, nan, inf, "bad", True])
def test_missing_or_invalid_sl_is_rejected(sl: object):
    reason = "missing_stop_loss" if sl is None else "invalid_stop_loss"
    assert_rejected(evaluate(engine(), sl_price=sl), reason)


@pytest.mark.parametrize(
    ("event", "sl"),
    [(signal(side=1), 1.11), (signal(side=-1), 1.09), (signal(side=1), 1.1)],
)
def test_wrong_side_or_zero_distance_sl_is_rejected(event: SignalEvent, sl: float):
    assert_rejected(evaluate(engine(), event, sl_price=sl), "invalid_stop_loss_direction")


@pytest.mark.parametrize("tp", [0.0, -1.0, nan, inf, "bad", True])
def test_invalid_tp_is_rejected(tp: object):
    assert_rejected(evaluate(engine(), tp_price=tp), "invalid_take_profit")


@pytest.mark.parametrize(
    ("event", "tp"), [(signal(side=1), 1.09), (signal(side=-1), 1.11)]
)
def test_wrong_side_tp_is_rejected(event: SignalEvent, tp: float):
    stop = 1.105 if event.side == -1 else 1.095
    assert_rejected(
        evaluate(engine(), event, sl_price=stop, tp_price=tp),
        "invalid_take_profit_direction",
    )


def test_missing_or_broken_pip_resolver_fails_closed():
    assert_rejected(evaluate(engine(pip_size_resolver=None)), "pip_size_unavailable")
    assert_rejected(evaluate(engine(pip_size_resolver=object())), "pip_size_unavailable")
    assert_rejected(evaluate(engine(pip_size_resolver=RaisingResolver())), "pip_size_unavailable")


@pytest.mark.parametrize("value", [None, "bad", 0.0, -0.1, nan, inf, -inf])
def test_invalid_pip_size_fails_closed(value: object):
    assert_rejected(
        evaluate(engine(pip_size_resolver=ValueResolver(value))), "invalid_pip_size"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_risk_per_trade_pct", 0.0),
        ("max_daily_loss_pct", nan),
        ("max_drawdown_pct", 101.0),
        ("ruin_threshold_pct", inf),
        ("max_consecutive_losses", True),
        ("max_trades_per_day", 0),
        ("max_open_positions", 1.0),
        ("starting_equity", 0.0),
        ("max_exposure_per_symbol_lots", inf),
    ],
)
def test_invalid_risk_limits_raise(field: str, value: object):
    with pytest.raises(ValueError):
        RiskLimits(**{field: value})


@pytest.mark.parametrize("field", ["lot_step", "pip_value_per_lot"])
@pytest.mark.parametrize("value", [0.0, -1.0, nan, inf, True])
def test_invalid_engine_numeric_configuration_raises(field: str, value: object):
    with pytest.raises(ValueError):
        engine(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["limits", "pip_size_resolver", "lot_step", "pip_value_per_lot"],
)
def test_engine_configuration_is_immutable_after_validation(field: str):
    risk_engine = engine()
    with pytest.raises(AttributeError):
        setattr(risk_engine, field, getattr(risk_engine, field))


def test_huge_integer_inputs_fail_closed_or_raise_configuration_value_error():
    huge = 10**10_000
    assert_rejected(evaluate(engine(), account=account(huge)), "invalid_equity")
    assert_rejected(evaluate(engine(), entry_price=huge), "invalid_entry_price")
    assert_rejected(engine().on_trade_closed(huge), "invalid_realized_pnl")
    with pytest.raises(ValueError):
        RiskLimits(max_risk_per_trade_pct=huge)
    with pytest.raises(ValueError):
        engine(lot_step=huge)


def test_first_observation_and_same_day_preserve_daily_state():
    risk_engine = engine(
        limits=RiskLimits(max_trades_per_day=3, max_open_positions=3)
    )
    first = evaluate(risk_engine)
    assert isinstance(first, RiskDecision)
    assert risk_engine.last_reset_date == NOW.date()
    assert risk_engine.daily_start_equity == 10_000.0
    assert risk_engine.daily_trades == 1
    second = evaluate(
        risk_engine,
        signal(signal_time=NOW - timedelta(minutes=10)),
        account=account(9_900.0),
    )
    assert isinstance(second, RiskDecision)
    assert risk_engine.daily_start_equity == 10_000.0
    assert risk_engine.daily_trades == 2


def test_utc_midnight_reset_and_timezone_normalization():
    risk_engine = engine(limits=RiskLimits(max_open_positions=2))
    local_tz = timezone(timedelta(hours=5, minutes=30))
    local_now = datetime(2026, 8, 26, 5, 29, tzinfo=local_tz)
    assert isinstance(
        evaluate(
            risk_engine,
            signal(signal_time=datetime(2026, 8, 25, 23, 54, tzinfo=UTC)),
            current_time=local_now,
        ),
        RiskDecision,
    )
    assert risk_engine.last_reset_date == datetime(2026, 8, 25, tzinfo=UTC).date()
    next_day = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
    result = evaluate(
        risk_engine,
        signal(signal_time=next_day),
        current_time=next_day,
        account=account(11_000.0),
    )
    assert isinstance(result, RiskDecision)
    assert risk_engine.last_reset_date == next_day.date()
    assert risk_engine.daily_start_equity == 11_000.0
    assert risk_engine.daily_trades == 1


def test_backward_utc_day_is_rejected_without_moving_state():
    risk_engine = engine()
    assert isinstance(evaluate(risk_engine), RiskDecision)
    old_date = risk_engine.last_reset_date
    result = evaluate(
        risk_engine,
        signal(signal_time=NOW - timedelta(days=1, minutes=5)),
        current_time=NOW - timedelta(days=1),
    )
    assert_rejected(result, "out_of_order_time")
    assert risk_engine.last_reset_date == old_date


def test_rejection_does_not_increment_but_approval_increments_once():
    risk_engine = engine()
    assert_rejected(evaluate(risk_engine, sl_price=None), "missing_stop_loss")
    assert risk_engine.daily_trades == 0
    assert risk_engine.last_reset_date == NOW.date()
    assert risk_engine.daily_start_equity == 10_000.0
    assert isinstance(evaluate(risk_engine), RiskDecision)
    assert risk_engine.daily_trades == 1


def test_max_daily_trades_rejects_new_identity():
    risk_engine = engine(
        limits=RiskLimits(max_trades_per_day=1, max_open_positions=2)
    )
    assert isinstance(evaluate(risk_engine), RiskDecision)
    result = evaluate(risk_engine, signal(signal_time=NOW - timedelta(minutes=10)))
    assert_rejected(result, "max_daily_trades")


def test_peak_equity_only_increases():
    risk_engine = engine(limits=RiskLimits(max_drawdown_pct=100.0))
    assert risk_engine.check_account_state(account(100.0), NOW) is None
    assert risk_engine.check_account_state(account(120.0), NOW) is None
    assert risk_engine.check_account_state(account(110.0), NOW) is None
    assert risk_engine.peak_equity == 120.0


def test_ruin_equality_has_highest_priority():
    limits = RiskLimits(starting_equity=100.0, ruin_threshold_pct=50.0, max_drawdown_pct=50.0)
    risk_engine = engine(limits=limits)
    result = risk_engine.check_account_state(account(50.0), NOW)
    rejection = assert_rejected(result, "kill_switch_active")
    assert rejection.kill_switch_reason is KillSwitchReason.RUIN_THRESHOLD


def test_drawdown_equality_triggers_kill_switch():
    risk_engine = engine(limits=RiskLimits(max_drawdown_pct=20.0))
    assert risk_engine.check_account_state(account(100.0), NOW) is None
    result = risk_engine.check_account_state(account(80.0), NOW)
    assert assert_rejected(result, "kill_switch_active").kill_switch_reason is (
        KillSwitchReason.MAX_DRAWDOWN
    )


def test_daily_loss_equality_triggers_kill_switch():
    limits = RiskLimits(max_daily_loss_pct=3.0, max_drawdown_pct=100.0)
    risk_engine = engine(limits=limits)
    assert risk_engine.check_account_state(account(100.0), NOW) is None
    result = risk_engine.check_account_state(account(97.0), NOW)
    assert assert_rejected(result, "kill_switch_active").kill_switch_reason is (
        KillSwitchReason.MAX_DAILY_LOSS
    )


def test_non_binary_friendly_daily_loss_equality_triggers():
    limits = RiskLimits(
        starting_equity=0.01,
        max_daily_loss_pct=3.0,
        max_drawdown_pct=100.0,
    )
    risk_engine = engine(limits=limits)
    assert risk_engine.check_account_state(account(1.5), NOW) is None
    result = risk_engine.check_account_state(account(1.455), NOW)
    assert assert_rejected(result, "kill_switch_active").kill_switch_reason is (
        KillSwitchReason.MAX_DAILY_LOSS
    )


def test_non_binary_friendly_drawdown_equality_triggers():
    limits = RiskLimits(
        starting_equity=0.001,
        max_daily_loss_pct=100.0,
        max_drawdown_pct=20.0,
    )
    risk_engine = engine(limits=limits)
    assert risk_engine.check_account_state(account(0.1), NOW) is None
    result = risk_engine.check_account_state(account(0.08), NOW)
    assert assert_rejected(result, "kill_switch_active").kill_switch_reason is (
        KillSwitchReason.MAX_DRAWDOWN
    )


def test_consecutive_losses_trigger_and_win_or_breakeven_reset():
    risk_engine = engine(limits=RiskLimits(max_consecutive_losses=2))
    assert risk_engine.on_trade_closed(-1.0) is None
    assert risk_engine.consecutive_losses == 1
    assert risk_engine.on_trade_closed(0.0) is None
    assert risk_engine.consecutive_losses == 0
    assert risk_engine.on_trade_closed(-1.0) is None
    result = risk_engine.on_trade_closed(-1.0)
    assert assert_rejected(result, "kill_switch_active").kill_switch_reason is (
        KillSwitchReason.MAX_CONSECUTIVE_LOSSES
    )


@pytest.mark.parametrize("bad_pnl", [nan, inf, -inf, "bad", True])
def test_trade_close_rejects_invalid_pnl(bad_pnl: object):
    assert_rejected(engine().on_trade_closed(bad_pnl), "invalid_realized_pnl")


def test_manual_and_reconciliation_switch_first_reason_is_latched():
    risk_engine = engine()
    assert risk_engine.trigger_kill_switch(KillSwitchReason.MANUAL) is True
    assert (
        risk_engine.trigger_kill_switch(KillSwitchReason.POSITION_RECONCILIATION_FAILED)
        is False
    )
    assert risk_engine.kill_switch_active is True
    assert risk_engine.kill_switch_reason is KillSwitchReason.MANUAL
    rejection = assert_rejected(evaluate(risk_engine), "kill_switch_active")
    assert rejection.kill_switch_reason is KillSwitchReason.MANUAL


@pytest.mark.parametrize(
    "reason",
    [KillSwitchReason.MANUAL, KillSwitchReason.POSITION_RECONCILIATION_FAILED],
)
def test_external_kill_switch_reasons_can_activate(reason: KillSwitchReason):
    risk_engine = engine()
    assert risk_engine.trigger_kill_switch(reason) is True
    assert risk_engine.kill_switch_reason is reason


def test_trigger_kill_switch_rejects_invalid_reason():
    with pytest.raises(ValueError):
        engine().trigger_kill_switch("manual_shutdown")  # type: ignore[arg-type]


def test_deterministic_id_and_duplicate_suppression():
    risk_engine = engine()
    first = evaluate(risk_engine)
    assert isinstance(first, RiskDecision)
    assert first.order_id == (
        "model_a_sweep_reversal-EURUSD-M5-20260825T105500000000Z-LONG"
    )
    duplicate = assert_rejected(evaluate(risk_engine), "duplicate_approval")
    assert duplicate.order_id == first.order_id
    assert risk_engine.daily_trades == 1


def test_timezone_equivalent_signal_has_same_id():
    risk_engine = engine()
    first = evaluate(risk_engine)
    assert isinstance(first, RiskDecision)
    equivalent_time = first.signal.signal_time.astimezone(timezone(timedelta(hours=2)))
    equivalent = signal(signal_time=equivalent_time)
    assert_rejected(evaluate(risk_engine, equivalent), "duplicate_approval")


@pytest.mark.parametrize(
    "changed",
    [
        {"setup_name": "model_b"},
        {"symbol": "GBPUSD"},
        {"timeframe": "M15"},
        {"side": -1},
        {"signal_time": NOW - timedelta(minutes=10)},
    ],
)
def test_identity_components_change_order_id(changed: dict[str, object]):
    base = signal()
    event = signal(**changed)
    if event.side == -1:
        kwargs = {"sl_price": 1.105, "tp_price": 1.09}
    else:
        kwargs = {}
    first = evaluate(engine(), base)
    second = evaluate(engine(), event, **kwargs)
    assert isinstance(first, RiskDecision)
    assert isinstance(second, RiskDecision)
    assert first.order_id != second.order_id


def test_rejected_signal_can_retry_and_engine_instances_are_independent():
    first_engine = engine()
    assert_rejected(evaluate(first_engine, sl_price=None), "missing_stop_loss")
    assert isinstance(evaluate(first_engine), RiskDecision)
    assert isinstance(evaluate(engine()), RiskDecision)


def test_concurrent_duplicate_evaluations_approve_exactly_once():
    risk_engine = engine()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: evaluate(risk_engine), range(16)))
    assert sum(isinstance(result, RiskDecision) for result in results) == 1
    assert sum(
        isinstance(result, RiskRejection) and result.reason == "duplicate_approval"
        for result in results
    ) == 15
    assert risk_engine.daily_trades == 1


def test_resolver_and_sizing_rejections_keep_approval_state_consistent():
    resolver_failure = engine(pip_size_resolver=None)
    assert_rejected(evaluate(resolver_failure), "pip_size_unavailable")
    assert resolver_failure.last_reset_date == NOW.date()
    assert resolver_failure.daily_start_equity == 10_000.0
    assert resolver_failure.peak_equity == 10_000.0
    assert resolver_failure.daily_trades == 0
    assert resolver_failure.approved_order_ids == frozenset()
    assert resolver_failure.reserved_position_count == 0

    sizing_failure = engine()
    assert_rejected(evaluate(sizing_failure, sl_price=0.0001), "size_below_minimum")
    assert sizing_failure.last_reset_date == NOW.date()
    assert sizing_failure.daily_trades == 0
    assert sizing_failure.approved_order_ids == frozenset()
    assert sizing_failure.reserved_position_count == 0


def test_decision_is_not_an_order_request_and_engine_has_no_broker_operations():
    result = evaluate(engine())
    assert isinstance(result, RiskDecision)
    assert not hasattr(RiskEngine, "submit_order")
    assert not hasattr(RiskEngine, "connect")
    assert not hasattr(RiskEngine, "disconnect")
    assert not hasattr(RiskEngine, "close_position")


def test_session_state_is_not_shared_between_instances():
    first = engine()
    second = engine()
    first.trigger_kill_switch(KillSwitchReason.MANUAL)
    assert first.kill_switch_active
    assert not second.kill_switch_active
    assert second.daily_trades == 0
    assert second.approved_order_ids == frozenset()


def test_risk_limits_replace_preserves_whole_percentage_convention():
    limits = replace(RiskLimits(), max_risk_per_trade_pct=3.0)
    result = evaluate(engine(limits=limits))
    assert isinstance(result, RiskDecision)
    assert result.monetary_risk_budget == pytest.approx(300.0)


def test_approval_discriminator_cannot_be_overridden_by_callers():
    decision = evaluate(engine())
    assert isinstance(decision, RiskDecision)
    with pytest.raises(TypeError):
        RiskDecision(
            signal=decision.signal,
            order_id=decision.order_id,
            size_lots=decision.size_lots,
            entry_price=decision.entry_price,
            sl_price=decision.sl_price,
            tp_price=decision.tp_price,
            pip_size=decision.pip_size,
            stop_pips=decision.stop_pips,
            monetary_risk_budget=decision.monetary_risk_budget,
            modeled_monetary_risk=decision.modeled_monetary_risk,
            approved_at=decision.approved_at,
            approved=False,  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        RiskRejection(
            reason="invalid",
            message="invalid input",
            signal=None,
            order_id=None,
            kill_switch_reason=None,
            approved=True,  # type: ignore[call-arg]
        )


def test_dto_high_value_invariants_are_enforced():
    decision = evaluate(engine())
    assert isinstance(decision, RiskDecision)
    with pytest.raises(ValueError):
        replace(decision, order_id=" ")
    with pytest.raises(ValueError):
        replace(decision, size_lots=0.0)
    with pytest.raises(ValueError):
        replace(decision, approved_at=datetime(2026, 8, 25))
    with pytest.raises(ValueError):
        replace(
            decision,
            modeled_monetary_risk=decision.monetary_risk_budget + 0.01,
        )

    rejection = RiskRejection("invalid", "invalid input", None, None, None)
    with pytest.raises(ValueError):
        replace(rejection, reason="")
    with pytest.raises(ValueError):
        replace(rejection, message=" ")
