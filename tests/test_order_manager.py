"""Focused tests for the Phase 5 execution coordinator."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from math import inf, nan

import pandas as pd
import pytest

from fxlab.execution import (
    ExecutionIntent,
    ExecutionResultKind,
    OrderManager,
)
from fxlab.execution.broker import (
    AccountInfo,
    OrderRequest,
    OrderStatus,
    Tick,
)
from fxlab.execution.signal_engine import SignalEvent
from fxlab.risk import (
    KillSwitchReason,
    RiskDecision,
    RiskEngine,
    RiskLimits,
    RiskRejection,
)

NOW = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
SIGNAL_TIME = NOW - timedelta(minutes=5)


def signal(**changes: object) -> SignalEvent:
    values = {
        "setup_name": "model_a_sweep_reversal",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "side": 1,
        "signal_time": SIGNAL_TIME,
        "signal_bar_index": 100,
    }
    values.update(changes)
    return SignalEvent(**values)  # type: ignore[arg-type]


def account() -> AccountInfo:
    return AccountInfo(
        balance=10_000.0,
        equity=10_000.0,
        margin_used=0.0,
        margin_available=10_000.0,
    )


def tick(**changes: object) -> Tick:
    values = {
        "symbol": "EURUSD",
        "timestamp": SIGNAL_TIME + timedelta(seconds=1),
        "bid": 1.0998,
        "ask": 1.1000,
        "mid": 1.0999,
    }
    values.update(changes)
    return Tick(**values)  # type: ignore[arg-type]


def decision(event: SignalEvent | None = None, **changes: object) -> RiskDecision:
    event = event or signal()
    values = {
        "signal": event,
        "order_id": "client-risk-id",
        "size_lots": 0.37,
        "entry_price": 1.1000,
        "sl_price": 1.0900,
        "tp_price": 1.1200,
        "pip_size": 0.0001,
        "stop_pips": 100.0,
        "monetary_risk_budget": 400.0,
        "modeled_monetary_risk": 370.0,
        "approved_at": NOW,
    }
    values.update(changes)
    return RiskDecision(**values)  # type: ignore[arg-type]


class FakeBroker:
    def __init__(self) -> None:
        self.latest_tick: object = tick()
        self.account = account()
        self.account_error: Exception | None = None
        self.tick_error: Exception | None = None
        self.submit_error: Exception | None = None
        self.status_error: Exception | None = None
        self.broker_order_id: object = "broker-id-7"
        self.status_result: object = {"status": "pending"}
        self.submitted: list[OrderRequest] = []
        self.status_queries: list[str] = []

    def get_latest_tick(self, symbol: str) -> Tick | None:
        if self.tick_error is not None:
            raise self.tick_error
        return self.latest_tick  # type: ignore[return-value]

    def get_account_info(self) -> AccountInfo:
        if self.account_error is not None:
            raise self.account_error
        return self.account

    def submit_order(self, order: OrderRequest) -> str:
        self.submitted.append(order)
        if self.submit_error is not None:
            raise self.submit_error
        return self.broker_order_id  # type: ignore[return-value]

    def get_order_status(self, order_id: str) -> dict:
        self.status_queries.append(order_id)
        if self.status_error is not None:
            raise self.status_error
        return self.status_result  # type: ignore[return-value]

    def connect(self) -> None:
        raise AssertionError("OrderManager must not connect brokers")

    def disconnect(self) -> None:
        raise AssertionError("OrderManager must not disconnect brokers")

    def close_position(self, position_id: str) -> str | None:
        raise AssertionError("OrderManager must not close positions")

    def is_connected(self) -> bool:
        return True

    def subscribe_market_data(self, symbols: list[str]) -> None:
        pass

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_historical_bars(self, symbol: str, tf: str, count: int) -> pd.DataFrame:
        return pd.DataFrame()


class RiskSpy:
    def __init__(self, result: RiskDecision | RiskRejection) -> None:
        self.result = result
        self.calls: list[tuple[SignalEvent, dict[str, object]]] = []
        self.released: list[str] = []
        self.kill_reasons: list[KillSwitchReason] = []

    def evaluate(self, event: SignalEvent, **kwargs: object):
        self.calls.append((event, kwargs))
        return self.result

    def release_approval(self, order_id: str) -> bool:
        self.released.append(order_id)
        return True

    def trigger_kill_switch(self, reason: KillSwitchReason) -> bool:
        self.kill_reasons.append(reason)
        return True


def manager(
    risk_result: RiskDecision | RiskRejection | None = None,
) -> tuple[OrderManager, FakeBroker, RiskSpy]:
    broker = FakeBroker()
    risk = RiskSpy(risk_result or decision())
    return OrderManager(broker=broker, risk_engine=risk), broker, risk  # type: ignore[arg-type]


def intent(event: SignalEvent | None = None, **changes: object) -> ExecutionIntent:
    values = {"signal": event or signal(), "sl_price": 1.09, "tp_price": 1.12}
    values.update(changes)
    return ExecutionIntent(**values)  # type: ignore[arg-type]


def submit_success(
    status: str | OrderStatus = OrderStatus.PENDING,
) -> tuple[OrderManager, FakeBroker, RiskSpy, str]:
    order_manager, broker, risk = manager()
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.kind is ExecutionResultKind.SUBMITTED
    assert result.record is not None
    broker.status_result = {"status": status}
    return order_manager, broker, risk, result.record.client_order_id


def test_execution_intent_is_immutable_and_does_not_own_entry_price():
    execution_intent = intent()
    assert not hasattr(execution_intent, "entry_price")
    with pytest.raises(AttributeError):
        execution_intent.sl_price = 1.08  # type: ignore[misc]


def test_long_uses_ask_and_forwards_explicit_prices_and_account_unchanged():
    order_manager, broker, risk = manager()
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.kind is ExecutionResultKind.SUBMITTED
    event, kwargs = risk.calls[0]
    assert event == signal()
    assert kwargs["entry_price"] == pytest.approx(1.1000)
    assert kwargs["sl_price"] == 1.09
    assert kwargs["tp_price"] == 1.12
    assert kwargs["account"] is broker.account
    assert kwargs["current_time"] == NOW


def test_short_uses_bid():
    event = signal(side=-1)
    approved = decision(event, entry_price=1.0998, sl_price=1.11, tp_price=1.08)
    order_manager, _, risk = manager(approved)
    result = order_manager.submit(
        intent(event, sl_price=1.11, tp_price=1.08), current_time=NOW
    )
    assert result.kind is ExecutionResultKind.SUBMITTED
    assert risk.calls[0][1]["entry_price"] == pytest.approx(1.0998)


def test_exact_signal_tick_time_boundary_is_accepted():
    order_manager, broker, _ = manager()
    broker.latest_tick = tick(timestamp=SIGNAL_TIME)
    assert order_manager.submit(intent(), current_time=NOW).kind is ExecutionResultKind.SUBMITTED


@pytest.mark.parametrize(
    ("quote", "reason"),
    [
        (None, "missing_quote"),
        (object(), "invalid_quote"),
        (tick(symbol="GBPUSD"), "quote_symbol_mismatch"),
        (tick(timestamp=datetime(2026, 8, 25, 10, 56)), "invalid_quote_time"),
        (tick(timestamp=SIGNAL_TIME - timedelta(microseconds=1)), "stale_quote"),
        (tick(timestamp=NOW + timedelta(microseconds=1)), "future_quote"),
    ],
)
def test_invalid_quote_conditions_reject_before_risk(quote: object, reason: str):
    order_manager, broker, risk = manager()
    broker.latest_tick = quote
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.kind is ExecutionResultKind.EXECUTION_REJECTED
    assert result.reason == reason
    assert risk.calls == []
    assert broker.submitted == []


@pytest.mark.parametrize("value", [0.0, -1.0, nan, inf, -inf, True])
@pytest.mark.parametrize("field", ["bid", "ask"])
def test_malformed_quote_prices_reject(value: object, field: str):
    order_manager, broker, risk = manager()
    malformed = tick()
    object.__setattr__(malformed, field, value)
    broker.latest_tick = malformed
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.reason == "invalid_quote_price"
    assert risk.calls == []


def test_inverted_quote_spread_rejects():
    order_manager, broker, risk = manager()
    malformed = tick()
    object.__setattr__(malformed, "ask", 1.09)
    broker.latest_tick = malformed
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.reason == "invalid_quote_spread"
    assert risk.calls == []


def test_naive_current_time_and_invalid_side_reject_before_quote():
    order_manager, broker, risk = manager()
    naive = order_manager.submit(intent(), current_time=datetime(2026, 8, 25, 11))
    assert naive.reason == "invalid_current_time"
    invalid_side = order_manager.submit(intent(signal(side=0)), current_time=NOW)
    assert invalid_side.reason == "invalid_signal_side"
    assert risk.calls == []
    assert broker.submitted == []


def test_quote_and_account_exceptions_are_structured_and_do_not_submit():
    order_manager, broker, risk = manager()
    broker.tick_error = RuntimeError("quote failed")
    quote_failure = order_manager.submit(intent(), current_time=NOW)
    assert quote_failure.reason == "quote_unavailable"
    broker.tick_error = None
    broker.account_error = RuntimeError("account failed")
    account_failure = order_manager.submit(intent(), current_time=NOW)
    assert account_failure.reason == "account_unavailable"
    assert risk.calls == []
    assert broker.submitted == []


def test_optional_take_profit_is_forwarded():
    approved = decision(tp_price=None)
    order_manager, broker, risk = manager(approved)
    result = order_manager.submit(intent(tp_price=None), current_time=NOW)
    assert result.kind is ExecutionResultKind.SUBMITTED
    assert risk.calls[0][1]["tp_price"] is None
    assert broker.submitted[0].tp_price is None


def test_risk_rejection_prevents_submission_and_is_exposed():
    rejection = RiskRejection("max_daily_trades", "limit reached", signal(), None, None)
    order_manager, broker, _ = manager(rejection)
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.kind is ExecutionResultKind.RISK_REJECTED
    assert result.risk_rejection is rejection
    assert broker.submitted == []


def test_approved_decision_maps_exactly_to_one_market_order():
    approved = decision()
    order_manager, broker, _ = manager(approved)
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.kind is ExecutionResultKind.SUBMITTED
    assert len(broker.submitted) == 1
    request = broker.submitted[0]
    assert request.symbol == approved.signal.symbol
    assert request.side == approved.signal.side
    assert request.size == approved.size_lots
    assert request.order_type == "market"
    assert request.order_id == approved.order_id
    assert request.price is None
    assert request.sl_price == approved.sl_price
    assert request.tp_price == approved.tp_price
    assert result.record is not None
    assert result.record.broker_order_id == "broker-id-7"
    assert result.record.broker_order_id != result.record.client_order_id
    assert result.record.reservation_released is False


def test_local_order_construction_failure_releases_without_broker_call(monkeypatch):
    order_manager, broker, risk = manager()

    class BrokenOrderRequest:
        def __init__(self, **kwargs: object) -> None:
            raise ValueError("cannot construct")

    monkeypatch.setattr("fxlab.execution.order_manager.OrderRequest", BrokenOrderRequest)
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.reason == "order_construction_failed"
    assert risk.released == ["client-risk-id"]
    assert broker.submitted == []


def test_submission_exception_is_indeterminate_retains_reservation_and_latches_kill():
    order_manager, broker, risk = manager()
    broker.submit_error = RuntimeError("timeout")
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.kind is ExecutionResultKind.INDETERMINATE
    assert result.reason == "broker_submission_exception"
    assert risk.released == []
    assert risk.kill_reasons == [KillSwitchReason.POSITION_RECONCILIATION_FAILED]
    assert order_manager.get_order("client-risk-id") is not None


@pytest.mark.parametrize("broker_id", [None, "", "   ", 123])
def test_invalid_broker_id_is_indeterminate_and_retains_reservation(broker_id: object):
    order_manager, broker, risk = manager()
    broker.broker_order_id = broker_id
    result = order_manager.submit(intent(), current_time=NOW)
    assert result.kind is ExecutionResultKind.INDETERMINATE
    assert result.reason == "invalid_broker_order_id"
    assert risk.released == []
    assert risk.kill_reasons == [KillSwitchReason.POSITION_RECONCILIATION_FAILED]


@pytest.mark.parametrize(
    ("status", "released"),
    [
        (OrderStatus.PENDING, False),
        (OrderStatus.FILLED, False),
        (OrderStatus.REJECTED, True),
        (OrderStatus.CANCELLED, True),
    ],
)
def test_status_updates_apply_reservation_lifecycle(
    status: OrderStatus, released: bool
):
    order_manager, broker, risk, client_id = submit_success(status)
    result = order_manager.refresh_order_status(client_id)
    assert result.kind is ExecutionResultKind.STATUS_UPDATED
    assert result.record is not None
    assert result.record.status is status
    assert result.record.reservation_released is released
    assert risk.released == ([client_id] if released else [])
    assert broker.status_queries == ["broker-id-7"]


def test_status_accepts_enum_value_directly():
    order_manager, _, _, client_id = submit_success(OrderStatus.FILLED)
    result = order_manager.refresh_order_status(client_id)
    assert result.record is not None
    assert result.record.status is OrderStatus.FILLED


@pytest.mark.parametrize("malformed", [None, {}, {"status": "unknown"}, {"status": 1}])
def test_malformed_status_retains_reservation(malformed: object):
    order_manager, broker, risk, client_id = submit_success()
    broker.status_result = malformed
    result = order_manager.refresh_order_status(client_id)
    assert result.kind is ExecutionResultKind.STATUS_FAILURE
    assert result.reason == "malformed_broker_status"
    assert risk.released == []
    assert order_manager.get_order(client_id).reservation_released is False  # type: ignore[union-attr]


def test_status_exception_and_unknown_id_retain_reservation():
    order_manager, broker, risk, client_id = submit_success()
    broker.status_error = RuntimeError("temporary")
    failure = order_manager.refresh_order_status(client_id)
    assert failure.reason == "status_poll_exception"
    unknown = order_manager.refresh_order_status("unknown")
    assert unknown.reason == "unknown_client_order_id"
    assert risk.released == []


def test_indeterminate_submission_cannot_be_polled_without_broker_id():
    order_manager, broker, _, _ = submit_success()
    record = order_manager.get_order("client-risk-id")
    assert record is not None
    object.__setattr__(record, "broker_order_id", None)
    result = order_manager.refresh_order_status("client-risk-id")
    assert result.reason == "broker_order_id_unavailable"
    assert broker.status_queries == []


def test_filled_position_confirmation_releases_once_and_repeated_is_harmless():
    order_manager, _, risk, client_id = submit_success(OrderStatus.FILLED)
    order_manager.refresh_order_status(client_id)
    assert order_manager.confirm_position_reflected(client_id) is True
    assert order_manager.confirm_position_reflected(client_id) is True
    assert risk.released == [client_id]
    record = order_manager.get_order(client_id)
    assert record is not None
    assert record.reservation_released is True
    assert order_manager.confirm_position_reflected("unknown") is False


def test_confirmation_requires_filled_status():
    order_manager, _, risk, client_id = submit_success(OrderStatus.PENDING)
    order_manager.refresh_order_status(client_id)
    assert order_manager.confirm_position_reflected(client_id) is False
    assert risk.released == []


def test_concurrent_confirmation_releases_exactly_once():
    order_manager, _, risk, client_id = submit_success(OrderStatus.FILLED)
    order_manager.refresh_order_status(client_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: order_manager.confirm_position_reflected(client_id), range(16))
        )
    assert all(results)
    assert risk.released == [client_id]


def test_concurrent_terminal_refresh_releases_exactly_once():
    order_manager, _, risk, client_id = submit_success(OrderStatus.REJECTED)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: order_manager.refresh_order_status(client_id), range(16))
        )
    assert all(result.kind is ExecutionResultKind.STATUS_UPDATED for result in results)
    assert risk.released == [client_id]


def test_same_client_id_is_submitted_only_once_through_manager_state():
    order_manager, broker, _ = manager()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: order_manager.submit(intent(), current_time=NOW), range(16))
        )
    assert len(broker.submitted) == 1
    assert sum(result.kind is ExecutionResultKind.SUBMITTED for result in results) == 1
    assert sum(result.reason == "duplicate_manager_order" for result in results) == 15


class Resolver:
    def pip_size_for(self, symbol: str) -> float:
        return 0.0001


def test_real_risk_reservation_release_preserves_daily_and_duplicate_history():
    broker = FakeBroker()
    risk = RiskEngine(
        limits=RiskLimits(max_open_positions=2, max_trades_per_day=2),
        pip_size_resolver=Resolver(),
    )
    order_manager = OrderManager(broker=broker, risk_engine=risk)
    submitted = order_manager.submit(intent(), current_time=NOW)
    assert submitted.kind is ExecutionResultKind.SUBMITTED
    assert submitted.record is not None
    assert risk.reserved_position_count == 1
    broker.status_result = {"status": "filled"}
    order_manager.refresh_order_status(submitted.record.client_order_id)
    assert order_manager.confirm_position_reflected(submitted.record.client_order_id)
    assert risk.reserved_position_count == 0
    assert risk.daily_trades == 1
    duplicate = order_manager.submit(intent(), current_time=NOW)
    assert duplicate.kind is ExecutionResultKind.RISK_REJECTED
    assert duplicate.risk_rejection is not None
    assert duplicate.risk_rejection.reason == "duplicate_approval"
    assert len(broker.submitted) == 1


def test_order_manager_has_no_out_of_scope_operations_or_sl_tp_generation():
    assert not hasattr(OrderManager, "connect")
    assert not hasattr(OrderManager, "disconnect")
    assert not hasattr(OrderManager, "close_position")
    assert not hasattr(OrderManager, "calculate_stop_loss")
    assert not hasattr(OrderManager, "calculate_take_profit")
    assert not hasattr(OrderManager, "save")
