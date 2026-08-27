"""Tests for deterministic Phase 6 historical paper-trading orchestration."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from fxlab.config import CostConfig, CostDefaults
from fxlab.data.provider import ProviderFailure, ProviderFailureCategory
from fxlab.execution.broker import AccountInfo, Tick
from fxlab.execution.broker_capabilities import (
    BrokerCapability,
    BrokerDescriptor,
    BrokerEnvironment,
)
from fxlab.execution.event_ledger import AuditEventType, EventLedger
from fxlab.execution.market_data import MarketDataStream
from fxlab.execution.order_manager import ExecutionIntent, ExecutionResultKind, OrderManager
from fxlab.execution.paper_broker import PaperBroker
from fxlab.execution.paper_session import (
    CycleKind,
    HistoricalBarReplay,
    MarketContext,
    PaperTradingSession,
)
from fxlab.execution.runtime_control import RuntimeControlReason, RuntimeState
from fxlab.execution.signal_engine import SignalEngine, SignalEvent
from fxlab.risk import KillSwitchReason, RiskEngine, RiskLimits, RiskRejection


class PipSizes:
    def pip_size_for(self, symbol: str) -> float:
        return 0.0001


class LatestSignalSetup:
    name = "test_setup"

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        return np.array([len(bars) - 1]), np.array([1])


class NoSignalSetup:
    name = "no_signal"

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        return np.array([], dtype=int), np.array([], dtype=int)


class FirstSignalSetup:
    name = "first_signal"

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        if len(bars) == 1:
            return np.array([0]), np.array([1])
        return np.array([], dtype=int), np.array([], dtype=int)


def bars(periods: int = 2) -> pd.DataFrame:
    index = pd.date_range("2026-08-25 10:00", periods=periods, freq="5min", tz="UTC")
    close = np.arange(periods, dtype=float) * 0.001 + 1.1000
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.0005,
            "low": close - 0.0005,
            "close": close,
            "volume": np.ones(periods),
        },
        index=index,
    )


def bars_from_closes(closes: list[float]) -> pd.DataFrame:
    frame = bars(len(closes))
    frame["open"] = closes
    frame["high"] = np.asarray(closes) + 0.0005
    frame["low"] = np.asarray(closes) - 0.0005
    frame["close"] = closes
    return frame


def zero_costs() -> CostConfig:
    return CostConfig(
        default=CostDefaults(
            spread_pips=0.0,
            commission_per_lot_roundturn=0.0,
            slippage_pips_base=0.0,
            slippage_vol_coeff=0.0,
            latency_bars=1,
        )
    )


def policy(signal: SignalEvent, context: MarketContext) -> ExecutionIntent:
    assert context.closed_bars.index.max() + pd.Timedelta(minutes=5) <= context.current_time
    return ExecutionIntent(signal, sl_price=context.tick.bid - 0.001, tp_price=None)


def make_session(
    *,
    setup: object | None = None,
    execution_policy=policy,
    source: pd.DataFrame | None = None,
    broker_type=PaperBroker,
    limits: RiskLimits | None = None,
    costs: CostConfig | None = None,
) -> tuple[PaperTradingSession, RiskEngine, PaperBroker]:
    frame = source if source is not None else bars()
    broker = broker_type(
        historical_bars={("EURUSD", "M5"): frame},
        cost_config=costs,
    )
    replay = HistoricalBarReplay({"EURUSD": frame}, "M5")
    market_data = MarketDataStream(
        broker=broker,
        symbols=["EURUSD"],
        time_provider=lambda: datetime(1990, 1, 1, tzinfo=UTC),
    )
    signal_engine = SignalEngine(
        setup=setup or LatestSignalSetup(),  # type: ignore[arg-type]
        market_data=market_data,
        timeframe="M5",
    )
    risk = RiskEngine(
        limits or RiskLimits(max_open_positions=5, max_trades_per_day=5),
        PipSizes(),
    )
    ledger = EventLedger(
        "test-paper-session",
        time_provider=lambda: datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
    )
    manager = OrderManager(broker, risk, ledger)
    session = PaperTradingSession(
        broker,
        replay,
        market_data,
        signal_engine,
        manager,
        risk,
        execution_policy,
        ledger,
    )
    return session, risk, broker


def test_start_poll_stop_and_idempotent_lifecycle() -> None:
    session, _, broker = make_session(setup=NoSignalSetup())
    session.start()
    session.start()
    result = session.poll_once()
    assert result.kind is CycleKind.NO_SIGNAL
    assert broker.is_connected()
    session.stop()
    session.stop()
    assert not broker.is_connected()
    assert session.poll_once().reason == "session_not_running"


def test_monitoring_snapshot_preserves_exact_order_position_correlation() -> None:
    session, _, _ = make_session()
    session.start()
    cycle = session.poll_once()
    assert cycle.executions
    snapshot = session.monitoring_snapshot()
    assert len(snapshot.orders) == 1
    assert len(snapshot.positions) == 1
    order = snapshot.orders[0]
    position = snapshot.positions[0]
    assert order.client_order_id == position.client_order_id
    assert order.broker_order_id == position.broker_order_id
    assert position.position_id.startswith("paper-position::")
    assert order.sl_price is not None
    assert order.reservation_released is True
    assert snapshot.risk.reservation_count == 0
    assert snapshot.risk.approved_order_count == 1
    session.stop()


def test_pause_blocks_new_risk_but_continues_market_maintenance() -> None:
    policy_calls = 0

    def counted_policy(signal, context):
        nonlocal policy_calls
        policy_calls += 1
        return policy(signal, context)

    session, risk, broker = make_session(execution_policy=counted_policy)
    session.start()
    paused = session.pause()
    assert paused.accepted and paused.changed
    cycle = session.poll_once()
    assert cycle.kind is CycleKind.NO_SIGNAL
    assert cycle.current_time == datetime(2026, 8, 25, 10, 5, tzinfo=UTC)
    assert policy_calls == 0
    assert not risk.approved_order_ids
    assert not broker.get_account_info().open_positions
    assert session.runtime_status().state is RuntimeState.PAUSED


def test_resume_requires_observed_data_and_watermark_blocks_paused_signal() -> None:
    session, risk, broker = make_session()
    session.start()
    session.pause()
    assert not session.resume().accepted
    session.poll_once()
    resumed = session.resume()
    assert resumed.accepted and resumed.changed
    assert session.runtime_status().entry_enable_watermark == datetime(
        2026, 8, 25, 10, 5, tzinfo=UTC
    )
    session.poll_once()
    assert risk.approved_order_ids
    assert len(broker.get_account_info().open_positions) == 1


def test_pause_still_executes_protective_stop_and_notifies_risk() -> None:
    frame = bars_from_closes([1.1, 1.08])

    def stop_policy(signal: SignalEvent, context: MarketContext) -> ExecutionIntent:
        return ExecutionIntent(signal, sl_price=1.09)

    session, risk, broker = make_session(
        setup=FirstSignalSetup(),
        execution_policy=stop_policy,
        source=frame,
        costs=zero_costs(),
    )
    session.start()
    session.poll_once()
    session.pause()
    closed = session.poll_once()
    assert len(closed.closes) == 1
    assert risk.consecutive_losses == 1
    assert not broker.get_account_info().open_positions
    assert session.runtime_status().state is RuntimeState.PAUSED


def test_emergency_stop_latches_manual_kill_without_liquidation() -> None:
    session, risk, broker = make_session(setup=FirstSignalSetup(), source=bars(1))
    session.start()
    session.poll_once()
    position_count = len(broker.get_account_info().open_positions)
    result = session.emergency_stop()
    assert result.accepted and result.changed
    assert risk.kill_switch_reason is KillSwitchReason.MANUAL
    assert session.runtime_status().state is RuntimeState.KILL_SWITCHED
    assert len(broker.get_account_info().open_positions) == position_count
    assert not session.resume().accepted
    assert not session.emergency_stop().changed


def test_runtime_state_transition_audit_is_idempotent() -> None:
    session, _, _ = make_session(setup=NoSignalSetup())
    session.start()
    session.pause()
    session.pause()
    session.poll_once()
    session.resume()
    events = [
        event
        for event in session.event_ledger.events()
        if event.event_type is AuditEventType.RUNTIME_STATE_CHANGED
    ]
    transitions = [
        (event.payload["previous_state"], event.payload["current_state"])
        for event in events
    ]
    assert transitions == [
        (RuntimeState.STOPPED.value, RuntimeState.RUNNING.value),
        (RuntimeState.RUNNING.value, RuntimeState.PAUSED.value),
        (RuntimeState.PAUSED.value, RuntimeState.RUNNING.value),
    ]


def test_request_and_complete_stop_are_idempotent() -> None:
    session, _, broker = make_session(setup=NoSignalSetup())
    session.start()
    assert session.request_stop().changed
    assert not session.request_stop().changed
    assert session.runtime_status().state is RuntimeState.STOPPING
    assert session.poll_once().reason == RuntimeControlReason.SHUTDOWN_IN_PROGRESS.value
    assert session.complete_stop().changed
    assert not session.complete_stop().changed
    assert session.runtime_status().state is RuntimeState.STOPPED
    assert not broker.is_connected()
    stopped_events = [
        event
        for event in session.event_ledger.events()
        if event.event_type is AuditEventType.SESSION_STOPPED
    ]
    assert len(stopped_events) == 1


def test_stop_still_disconnects_after_audit_integrity_failure() -> None:
    session, _, broker = make_session(setup=NoSignalSetup())
    session.start()

    def fail_store(event) -> None:
        raise OSError("ledger unavailable")

    session.event_ledger._store_event = fail_store  # type: ignore[method-assign]
    session.stop()
    assert not broker.is_connected()
    assert session.runtime_status().state is RuntimeState.STOPPED


def test_broker_capabilities_bind_once_per_logical_session() -> None:
    session, _, _ = make_session(setup=NoSignalSetup())
    session.start()
    session.start()
    bound = [
        event
        for event in session.event_ledger.events()
        if event.event_type is AuditEventType.BROKER_CAPABILITIES_BOUND
    ]
    assert len(bound) == 1
    assert bound[0].payload["broker_id"] == "fxlab-paper"
    assert tuple(bound[0].payload["capabilities"]) == tuple(
        sorted(item.value for item in session.broker.broker_descriptor.capabilities)
    )


@pytest.mark.parametrize(
    "descriptor",
    [
        BrokerDescriptor(
            "wrong-environment",
            "1",
            BrokerEnvironment.DEMO,
            frozenset(
                {
                    BrokerCapability.MARKET_ORDERS,
                    BrokerCapability.NATIVE_SL_TP,
                    BrokerCapability.HEDGING,
                    BrokerCapability.CLIENT_ORDER_IDS,
                }
            ),
            True,
        ),
        BrokerDescriptor(
            "non-deterministic",
            "1",
            BrokerEnvironment.PAPER,
            frozenset(
                {
                    BrokerCapability.MARKET_ORDERS,
                    BrokerCapability.NATIVE_SL_TP,
                    BrokerCapability.HEDGING,
                    BrokerCapability.CLIENT_ORDER_IDS,
                }
            ),
            False,
        ),
    ],
)
def test_incompatible_broker_cannot_start(monkeypatch, descriptor) -> None:
    session, _, broker = make_session(setup=NoSignalSetup())
    monkeypatch.setattr(
        PaperBroker, "broker_descriptor", property(lambda self: descriptor)
    )
    with pytest.raises(RuntimeError, match="capabilities"):
        session.start()
    assert not broker.is_connected()
    assert session.event_ledger.last_event().event_type is (
        AuditEventType.BROKER_CAPABILITY_REJECTED
    )


def test_descriptor_mutation_during_runtime_fails_closed(monkeypatch) -> None:
    session, _, _ = make_session(setup=LatestSignalSetup())
    session.start()
    changed = BrokerDescriptor(
        "fxlab-paper",
        "2",
        BrokerEnvironment.PAPER,
        session.broker.broker_descriptor.capabilities,
        True,
    )
    monkeypatch.setattr(
        PaperBroker, "broker_descriptor", property(lambda self: changed)
    )
    result = session.poll_once()
    assert result.kind is CycleKind.FAILED
    assert result.reason == "broker_capability_unsupported"
    assert not session.broker.get_account_info().open_positions
    failure_events = [event.event_type for event in session.event_ledger.events()][-2:]
    assert failure_events == [
        AuditEventType.BROKER_CAPABILITY_REJECTED,
        AuditEventType.RUNTIME_STATE_CHANGED,
    ]


def test_audit_lifecycle_market_and_account_ordering() -> None:
    session, _, _ = make_session(setup=NoSignalSetup())
    session.start()
    session.poll_once()
    session.stop()
    types = [event.event_type for event in session.event_ledger.events()]
    assert types[0] is AuditEventType.SESSION_STARTED
    assert types[-1] is AuditEventType.SESSION_STOPPED
    assert types.index(AuditEventType.MARKET_EVENT) < types.index(
        AuditEventType.ACCOUNT_OBSERVED
    )


def test_audit_signal_policy_intent_and_order_chain() -> None:
    session, _, _ = make_session()
    session.start()
    result = session.poll_once()
    assert result.kind is CycleKind.PROCESSED
    types = [event.event_type for event in session.event_ledger.events()]
    ordered = [
        AuditEventType.MARKET_EVENT,
        AuditEventType.SIGNAL_EMITTED,
        AuditEventType.EXECUTION_INTENT_CREATED,
        AuditEventType.RISK_APPROVED,
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.ORDER_FILLED,
        AuditEventType.POSITION_OPENED,
        AuditEventType.RESERVATION_RELEASED,
    ]
    assert [types.index(item) for item in ordered] == sorted(
        types.index(item) for item in ordered
    )
    correlated = [
        event
        for event in session.event_ledger.events()
        if event.correlation.client_order_id is not None
    ]
    assert len({event.correlation.client_order_id for event in correlated}) == 1


def test_audit_policy_decline_and_failure() -> None:
    declined, _, _ = make_session(execution_policy=lambda signal, context: None)
    declined.start()
    assert declined.poll_once().kind is CycleKind.POLICY_DECLINED
    assert AuditEventType.SIGNAL_DECLINED in {
        event.event_type for event in declined.event_ledger.events()
    }

    def broken_policy(signal, context):
        raise RuntimeError("policy failed")

    failed, _, _ = make_session(execution_policy=broken_policy)
    failed.start()
    assert failed.poll_once().reason == "execution_policy_failure"
    assert AuditEventType.EXECUTION_POLICY_FAILED in {
        event.event_type for event in failed.event_ledger.events()
    }


def test_ledger_failure_disables_future_session_execution() -> None:
    session, _, broker = make_session()
    session.start()

    def fail_store(event) -> None:
        raise OSError("ledger unavailable")

    session.event_ledger._store_event = fail_store  # type: ignore[method-assign]
    first = session.poll_once()
    assert first.kind is CycleKind.FAILED
    assert first.reason == "audit_unavailable"
    submitted = len(broker.get_account_info().open_positions)
    second = session.poll_once()
    assert second.reason == "audit_unavailable"
    assert len(broker.get_account_info().open_positions) == submitted


def test_chronological_replay_and_no_lookahead() -> None:
    replay = HistoricalBarReplay({"EURUSD": bars()}, "M5")
    before_first_close = datetime(2026, 8, 25, 10, 4, 59, tzinfo=UTC)
    assert replay.next_tick(until=before_first_close) is None
    first = replay.next_tick(until=datetime(2026, 8, 25, 10, 5, tzinfo=UTC))
    second = replay.next_tick()
    assert first is not None and second is not None
    assert first.timestamp < second.timestamp
    assert first.bid == first.ask == bars().iloc[0].close


def test_session_never_exposes_future_bar_to_policy() -> None:
    observed: list[pd.DataFrame] = []

    def observing_policy(signal: SignalEvent, context: MarketContext) -> ExecutionIntent | None:
        observed.append(context.closed_bars)
        return None

    session, _, _ = make_session(execution_policy=observing_policy)
    session.start()
    result = session.poll_once()
    assert result.kind is CycleKind.POLICY_DECLINED
    assert len(observed[0]) == 1
    assert observed[0].index[0] == pd.Timestamp("2026-08-25 10:00", tz="UTC")


def test_signal_policy_submission_and_reflection_release() -> None:
    session, risk, broker = make_session()
    session.start()
    result = session.poll_once()
    assert result.kind is CycleKind.PROCESSED
    submitted = next(
        item for item in result.executions if item.kind is ExecutionResultKind.SUBMITTED
    )
    assert submitted.record is not None
    assert submitted.record.request.sl_price == pytest.approx(1.099)
    assert submitted.record.request.tp_price is None
    assert broker.get_correlation(submitted.record.client_order_id) is not None
    assert risk.reserved_position_count == 0
    assert len(broker.get_account_info().open_positions) == 1


def test_policy_decline_does_not_submit() -> None:
    session, risk, broker = make_session(execution_policy=lambda signal, context: None)
    session.start()
    assert session.poll_once().kind is CycleKind.POLICY_DECLINED
    assert not risk.approved_order_ids
    assert not broker.get_account_info().open_positions


def test_policy_exception_is_structured_failure() -> None:
    def broken(signal: SignalEvent, context: MarketContext) -> ExecutionIntent:
        raise ValueError("test policy failure")

    session, _, broker = make_session(execution_policy=broken)
    session.start()
    result = session.poll_once()
    assert result.kind is CycleKind.FAILED
    assert result.reason == "execution_policy_failure"
    assert not broker.get_account_info().open_positions


def test_risk_rejection_is_returned_without_submission() -> None:
    def bad_stop(signal: SignalEvent, context: MarketContext) -> ExecutionIntent:
        return ExecutionIntent(signal, sl_price=context.tick.ask + 0.001)

    session, _, broker = make_session(execution_policy=bad_stop)
    session.start()
    result = session.poll_once()
    assert result.kind is CycleKind.PROCESSED
    assert result.executions[0].kind is ExecutionResultKind.RISK_REJECTED
    assert not broker.get_account_info().open_positions


def test_replay_exhaustion() -> None:
    session, _, _ = make_session(setup=NoSignalSetup(), source=bars(1))
    session.start()
    session.poll_once()
    result = session.poll_once()
    assert result.kind is CycleKind.EXHAUSTED
    assert result.reason == "replay_exhausted"


def test_market_data_failure_is_structured() -> None:
    session, _, broker = make_session(setup=NoSignalSetup())
    session.start()
    broker.disconnect()
    result = session.poll_once()
    assert result.kind is CycleKind.FAILED
    assert result.reason == "market_data_failure"
    assert session.runtime_status().state is RuntimeState.PAUSED
    assert session.runtime_status().reason is RuntimeControlReason.BROKER_UNAVAILABLE


@pytest.mark.parametrize(
    "category",
    [
        ProviderFailureCategory.STALE_DATA,
        ProviderFailureCategory.TRANSIENT,
        ProviderFailureCategory.RATE_LIMIT,
        ProviderFailureCategory.NO_DATA,
    ],
)
def test_temporary_provider_failure_pauses_new_execution(category) -> None:
    session, _, _ = make_session(setup=NoSignalSetup())
    session.start()
    session.handle_provider_failure(
        ProviderFailure(category, "temporarily_unavailable", "replay", True)
    )
    assert session.runtime_status().state is RuntimeState.PAUSED


def test_temporary_provider_recovery_requires_new_valid_observation_and_resume() -> None:
    session, _, _ = make_session(setup=NoSignalSetup())
    session.start()
    session.poll_once()
    session.handle_provider_failure(
        ProviderFailure(
            ProviderFailureCategory.TRANSIENT,
            "temporarily_unavailable",
            "replay",
            True,
        )
    )
    assert not session.resume().accepted
    session.poll_once()
    assert session.runtime_status().state is RuntimeState.PAUSED
    assert session.resume().accepted


def test_resume_rechecks_bound_broker_descriptor(monkeypatch) -> None:
    session, _, _ = make_session(setup=NoSignalSetup())
    session.start()
    session.poll_once()
    session.pause()
    changed = BrokerDescriptor(
        "fxlab-paper",
        "2",
        BrokerEnvironment.PAPER,
        session.broker.broker_descriptor.capabilities,
        True,
    )
    monkeypatch.setattr(PaperBroker, "broker_descriptor", property(lambda self: changed))
    assert not session.resume().accepted
    assert session.runtime_status().state is RuntimeState.FAILED


@pytest.mark.parametrize(
    "category",
    [
        ProviderFailureCategory.INVALID_DATA,
        ProviderFailureCategory.INCOMPATIBLE_SCHEMA,
        ProviderFailureCategory.CONFIGURATION,
        ProviderFailureCategory.AUTHENTICATION,
        ProviderFailureCategory.UNSUPPORTED,
        ProviderFailureCategory.INTERNAL,
    ],
)
def test_permanent_provider_failure_fails_runtime(category) -> None:
    session, _, _ = make_session(setup=NoSignalSetup())
    session.start()
    session.handle_provider_failure(
        ProviderFailure(category, "invalid_provider_output", "replay", False)
    )
    assert session.runtime_status().state is RuntimeState.FAILED


def test_account_state_checked_each_market_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    session, risk, _ = make_session(setup=NoSignalSetup())
    calls = 0
    original = risk.check_account_state

    def checked(account, current_time):
        nonlocal calls
        calls += 1
        return original(account, current_time)

    monkeypatch.setattr(risk, "check_account_state", checked)
    session.start()
    session.poll_once()
    session.poll_once()
    assert calls == 2


class UncertainBroker(PaperBroker):
    def submit_order(self, order):
        raise RuntimeError("unknown submission outcome")


def test_submission_uncertainty_latches_reconciliation_switch() -> None:
    session, risk, _ = make_session(broker_type=UncertainBroker)
    session.start()
    result = session.poll_once()
    assert result.executions[0].kind is ExecutionResultKind.INDETERMINATE
    assert risk.kill_switch_reason is KillSwitchReason.POSITION_RECONCILIATION_FAILED
    assert session.runtime_status().state is RuntimeState.RECONCILIATION_REQUIRED


class MissingCorrelationBroker(PaperBroker):
    def get_correlation(self, client_order_id):
        return None


def test_filled_order_without_exact_correlation_latches_switch() -> None:
    session, risk, _ = make_session(broker_type=MissingCorrelationBroker)
    session.start()
    result = session.poll_once()
    assert any(item.reason == "position_reconciliation_failed" for item in result.executions)
    assert risk.kill_switch_reason is KillSwitchReason.POSITION_RECONCILIATION_FAILED
    assert risk.reserved_position_count == 1
    assert session.runtime_status().state is RuntimeState.RECONCILIATION_REQUIRED


class StatusUnavailableBroker(PaperBroker):
    def get_order_status(self, broker_order_id):
        raise RuntimeError("temporary status failure")


def test_status_failure_pauses_new_risk_without_releasing_reservation() -> None:
    session, risk, _ = make_session(broker_type=StatusUnavailableBroker)
    session.start()
    result = session.poll_once()
    assert any(item.kind is ExecutionResultKind.STATUS_FAILURE for item in result.executions)
    assert session.runtime_status().state is RuntimeState.PAUSED
    assert session.runtime_status().reason is RuntimeControlReason.BROKER_UNAVAILABLE
    assert risk.reserved_position_count == 1


def test_no_research_or_persistence_ownership() -> None:
    session, _, _ = make_session()
    assert not hasattr(session, "save")
    assert not hasattr(session, "load")
    assert not hasattr(session, "generate_stop_loss")


def test_replay_rejects_naive_until_time() -> None:
    replay = HistoricalBarReplay({"EURUSD": bars()}, "M5")
    with pytest.raises(ValueError, match="timezone-aware"):
        replay.next_tick(until=datetime(2026, 8, 25, 10, 5))


def test_replay_close_quote_is_zero_spread_without_microstructure() -> None:
    replay = HistoricalBarReplay({"EURUSD": bars(1)}, "M5")
    event = replay.next_tick()
    assert isinstance(event, Tick)
    assert event.bid == event.ask == event.mid


def test_session_forwards_automatic_net_loss_exactly_once() -> None:
    frame = bars_from_closes([1.1, 1.08, 1.07])

    def stop_policy(signal: SignalEvent, context: MarketContext) -> ExecutionIntent:
        return ExecutionIntent(signal, sl_price=1.09)

    session, risk, _ = make_session(
        setup=FirstSignalSetup(),
        execution_policy=stop_policy,
        source=frame,
        costs=zero_costs(),
    )
    session.start()
    session.poll_once()
    closed = session.poll_once()
    assert len(closed.closes) == 1
    assert closed.closes[0].net_realized_pnl < 0
    assert risk.consecutive_losses == 1
    session.poll_once()
    assert risk.consecutive_losses == 1
    close_events = [
        event
        for event in session.event_ledger.events()
        if event.event_type is AuditEventType.POSITION_CLOSED
    ]
    assert len(close_events) == 1
    assert close_events[0].payload["net_realized_pnl"] < 0
    assert close_events[0].correlation.close_order_id is not None


def test_manual_close_notifies_risk_and_is_idempotent() -> None:
    session, risk, broker = make_session(
        setup=FirstSignalSetup(),
        source=bars(1),
        costs=zero_costs(),
    )
    risk.on_trade_closed(-1.0)
    session.start()
    session.poll_once()
    position_id = broker.get_account_info().open_positions[0].position_id
    close = session.close_position(position_id)
    assert close is not None
    assert close.net_realized_pnl == pytest.approx(0.0)
    assert risk.consecutive_losses == 0
    assert session.close_position(position_id) is None
    assert risk.consecutive_losses == 0


def test_winning_close_resets_consecutive_losses() -> None:
    frame = bars_from_closes([1.1, 1.102])

    def target_policy(signal: SignalEvent, context: MarketContext) -> ExecutionIntent:
        return ExecutionIntent(signal, sl_price=1.09, tp_price=1.101)

    session, risk, _ = make_session(
        setup=FirstSignalSetup(),
        execution_policy=target_policy,
        source=frame,
        costs=zero_costs(),
    )
    risk.on_trade_closed(-1.0)
    session.start()
    session.poll_once()
    result = session.poll_once()
    assert result.closes[0].net_realized_pnl > 0
    assert risk.consecutive_losses == 0


def test_loss_kill_switch_blocks_same_cycle_signal() -> None:
    frame = bars_from_closes([1.1, 1.08])

    def stop_policy(signal: SignalEvent, context: MarketContext) -> ExecutionIntent:
        return ExecutionIntent(signal, sl_price=1.09)

    limits = RiskLimits(
        max_open_positions=5,
        max_trades_per_day=5,
        max_consecutive_losses=1,
    )
    session, risk, _ = make_session(
        execution_policy=stop_policy,
        source=frame,
        limits=limits,
        costs=zero_costs(),
    )
    session.start()
    session.poll_once()
    result = session.poll_once()
    assert result.kind is CycleKind.FAILED
    assert result.closes
    assert result.signals == ()
    assert risk.kill_switch_reason is KillSwitchReason.MAX_CONSECUTIVE_LOSSES
    assert risk.daily_trades == 1
    assert sum(
        event.event_type is AuditEventType.KILL_SWITCH_TRIGGERED
        for event in session.event_ledger.events()
    ) == 1


def test_non_positive_paper_equity_is_rejected_by_risk_engine() -> None:
    risk = RiskEngine(RiskLimits(), PipSizes())
    negative = AccountInfo(-1.0, -1.0, 0.0, -1.0)
    event = SignalEvent(
        setup_name="test",
        symbol="EURUSD",
        timeframe="M5",
        side=1,
        signal_time=datetime(2026, 8, 25, 10, 5, tzinfo=UTC),
        signal_bar_index=0,
    )
    result = risk.evaluate(
        event,
        entry_price=1.1,
        sl_price=1.09,
        tp_price=None,
        account=negative,
        current_time=event.signal_time,
    )
    assert isinstance(result, RiskRejection)
    assert result.reason == "invalid_equity"
