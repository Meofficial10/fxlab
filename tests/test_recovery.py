"""Focused safe-checkpoint and crash-recovery tests for the paper application."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from fxlab.execution.broker_capabilities import (
    BrokerCapability,
    BrokerDescriptor,
    BrokerEnvironment,
)
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import AuditComponent, AuditEventType, EventLedger
from fxlab.execution.market_data import MarketDataStream
from fxlab.execution.order_manager import ExecutionIntent, OrderManager
from fxlab.execution.paper_broker import PaperBroker
from fxlab.execution.paper_session import HistoricalBarReplay, MarketContext, PaperTradingSession
from fxlab.execution.recovery import (
    RecoveryState,
    UnsafeCheckpointError,
    create_checkpoint,
    recover,
)
from fxlab.execution.runtime_control import RuntimeState
from fxlab.execution.signal_engine import SignalEngine, SignalEvent
from fxlab.risk import RiskEngine, RiskLimits


class PipSizes:
    def pip_size_for(self, symbol: str) -> float:
        return 0.0001


class FirstSignal:
    name = "recovery_setup"

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        if len(bars) == 1:
            return np.array([0]), np.array([1])
        return np.array([], dtype=int), np.array([], dtype=int)


def bars(closes: tuple[float, ...] = (1.1, 1.101)) -> pd.DataFrame:
    index = pd.date_range("2026-08-25 10:00", periods=len(closes), freq="5min", tz="UTC")
    values = np.asarray(closes)
    return pd.DataFrame(
        {
            "open": values,
            "high": values + 0.0005,
            "low": values - 0.0005,
            "close": values,
            "volume": np.ones(len(values)),
        },
        index=index,
    )


def policy(signal: SignalEvent, context: MarketContext) -> ExecutionIntent:
    return ExecutionIntent(signal, sl_price=context.tick.bid - 0.005)


def make_session(
    path,
    *,
    source: pd.DataFrame | None = None,
    execution_policy=policy,
    policy_limits: RiskLimits | None = None,
) -> tuple[PaperTradingSession, SQLiteEventStore]:
    frame = source if source is not None else bars()
    store = SQLiteEventStore(path, "recovery-session")
    ledger = EventLedger("recovery-session", durable_store=store)
    broker = PaperBroker(historical_bars={("EURUSD", "M5"): frame})
    replay = HistoricalBarReplay({"EURUSD": frame}, "M5")
    market = MarketDataStream(
        broker,
        ["EURUSD"],
        time_provider=lambda: datetime(1990, 1, 1, tzinfo=UTC),
    )
    signals = SignalEngine(FirstSignal(), market, "M5")
    risk = RiskEngine(
        policy_limits or RiskLimits(max_open_positions=5, max_trades_per_day=5),
        PipSizes(),
    )
    manager = OrderManager(broker, risk, ledger)
    session = PaperTradingSession(
        broker, replay, market, signals, manager, risk, execution_policy, ledger
    )
    return session, store


def checkpoint_live_session(tmp_path):
    path = tmp_path / "recovery.sqlite"
    session, store = make_session(path)
    session.start()
    session.poll_once()
    checkpoint = create_checkpoint(
        session,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
        created_at=datetime(2026, 8, 25, 10, 6, tzinfo=UTC),
    )
    return path, session, store, checkpoint


def test_clean_recovery_restores_all_operational_state_without_start(tmp_path) -> None:
    path, original, store, checkpoint = checkpoint_live_session(tmp_path)
    original_account = original.broker.get_account_info()
    original_risk = original.risk_engine.snapshot_state()
    store.close()
    restored, reopened = make_session(path)
    before_events = reopened.last_sequence()
    result = recover(
        restored,
        reopened,
        software_version="1.0",
        execution_policy_id="policy-v1",
    )
    assert result.state is RecoveryState.RECOVERED
    assert result.checkpoint_sequence == checkpoint.last_event_sequence
    assert not restored.broker.is_connected()
    assert restored.risk_engine.snapshot_state() == original_risk
    assert restored.broker.get_account_info() == original_account
    assert restored.replay.snapshot_state() == original.replay.snapshot_state()
    assert reopened.last_sequence() == before_events


def test_repeated_recovery_is_idempotent(tmp_path) -> None:
    path, _, store, _ = checkpoint_live_session(tmp_path)
    store.close()
    restored, reopened = make_session(path)
    first = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    state = (
        restored.risk_engine.snapshot_state(),
        restored.order_manager.snapshot_state(),
        restored.broker.snapshot_state(),
        restored.replay.snapshot_state(),
        reopened.last_sequence(),
    )
    second = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert first == second
    assert state == (
        restored.risk_engine.snapshot_state(),
        restored.order_manager.snapshot_state(),
        restored.broker.snapshot_state(),
        restored.replay.snapshot_state(),
        reopened.last_sequence(),
    )


def test_replay_continues_after_last_consumed_event(tmp_path) -> None:
    path, _, store, _ = checkpoint_live_session(tmp_path)
    store.close()
    restored, reopened = make_session(path)
    assert recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    ).recovered
    restored.start()
    result = restored.poll_once()
    assert result.tick is not None
    assert result.tick.timestamp == datetime(2026, 8, 25, 10, 10, tzinfo=UTC)


def test_paused_runtime_restores_paused_and_requires_explicit_resume(tmp_path) -> None:
    path, original, store, _ = checkpoint_live_session(tmp_path)
    original.pause()
    create_checkpoint(
        original,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
    )
    store.close()
    restored, reopened = make_session(path)
    assert recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    ).recovered
    assert restored.runtime_status().state is RuntimeState.PAUSED
    restored.start()
    assert restored.runtime_status().state is RuntimeState.PAUSED
    assert not restored.runtime_status().execution_enabled


def test_stopping_runtime_restores_blocked_and_cannot_start(tmp_path) -> None:
    path, original, store, _ = checkpoint_live_session(tmp_path)
    original.request_stop()
    create_checkpoint(
        original,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
    )
    store.close()
    restored, reopened = make_session(path)
    assert recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    ).recovered
    assert restored.runtime_status().state is RuntimeState.STOPPING
    with pytest.raises(RuntimeError, match="stopping"):
        restored.start()


def test_stopped_runtime_restores_terminal(tmp_path) -> None:
    path, original, store, _ = checkpoint_live_session(tmp_path)
    original.stop()
    create_checkpoint(
        original,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
    )
    store.close()
    restored, reopened = make_session(path)
    assert recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    ).recovered
    assert restored.runtime_status().state is RuntimeState.STOPPED
    with pytest.raises(RuntimeError, match="stopped"):
        restored.start()


def test_complete_stop_can_commit_stopping_and_terminal_checkpoints(tmp_path) -> None:
    _, session, store, _ = checkpoint_live_session(tmp_path)
    session.request_stop()
    result = session.complete_stop(
        checkpoint_store=store,
        software_version="1.0",
        execution_policy_id="policy-v1",
    )
    assert result.changed
    checkpoint = store.load_latest_checkpoint()
    assert checkpoint is not None
    assert checkpoint.last_event_sequence == store.last_sequence()
    assert checkpoint.state["session"]["runtime_control"]["state"] == "stopped"


@pytest.mark.parametrize(
    ("software", "policy_id", "reason"),
    [
        ("2.0", "policy-v1", "software_version_mismatch"),
        ("1.0", "policy-v2", "configuration_mismatch"),
    ],
)
def test_compatibility_mismatch_fails_closed(
    tmp_path, software: str, policy_id: str, reason: str
) -> None:
    path, _, store, _ = checkpoint_live_session(tmp_path)
    store.close()
    restored, reopened = make_session(path)
    result = recover(
        restored, reopened, software_version=software, execution_policy_id=policy_id
    )
    assert result.state is RecoveryState.FAILED
    assert result.reason == reason
    assert restored.recovery_required
    assert restored.risk_engine.kill_switch_active


def test_changed_dataset_fails_closed(tmp_path) -> None:
    path, _, store, _ = checkpoint_live_session(tmp_path)
    store.close()
    restored, reopened = make_session(path, source=bars((1.1, 1.102)))
    result = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.state is RecoveryState.FAILED
    assert result.reason == "replay_dataset_mismatch"
    assert restored.recovery_required


@pytest.mark.parametrize(
    "event_type",
    [
        AuditEventType.RISK_APPROVED,
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        AuditEventType.ORDER_SUBMISSION_INDETERMINATE,
        AuditEventType.ORDER_SUBMITTED,
        AuditEventType.ORDER_FILLED,
        AuditEventType.POSITION_OPENED,
        AuditEventType.POSITION_CLOSED,
        AuditEventType.RESERVATION_RELEASED,
    ],
)
def test_unsafe_event_tail_requires_reconciliation(tmp_path, event_type) -> None:
    path, session, store, _ = checkpoint_live_session(tmp_path)
    session.event_ledger.append(
        event_type,
        occurred_at=datetime(2026, 8, 25, 10, 7, tzinfo=UTC),
        component=AuditComponent.ORDER_MANAGER,
        payload={"crash_window": event_type.value},
    )
    store.close()
    restored, reopened = make_session(path)
    result = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.state is RecoveryState.RECONCILIATION_REQUIRED
    assert restored.recovery_required
    with pytest.raises(RuntimeError, match="reconciliation"):
        restored.start()


def test_resolved_audit_without_checkpoint_is_not_a_recovery_commit(tmp_path) -> None:
    path, session, store, _ = checkpoint_live_session(tmp_path)
    session.event_ledger.append(
        AuditEventType.RECONCILIATION_RESOLVED,
        occurred_at=datetime(2026, 8, 25, 10, 7, tzinfo=UTC),
        component=AuditComponent.RECONCILIATION_ENGINE,
        payload={
            "reconciliation_id": "reconcile-interrupted",
            "reason": "state_reconciled",
            "applied_actions": (),
            "new_session_id": "new-session",
        },
    )
    store.close()
    restored, reopened = make_session(path)
    result = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.state is RecoveryState.RECONCILIATION_REQUIRED
    assert restored.recovery_required


def test_missing_checkpoint_fails_without_fresh_start(tmp_path) -> None:
    session, store = make_session(tmp_path / "empty.sqlite")
    result = recover(
        session, store, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.state is RecoveryState.FAILED
    assert result.reason == "state_missing"
    assert session.recovery_required


def test_safe_checkpoint_rejects_undrained_close_event(tmp_path) -> None:
    path, session, store, _ = checkpoint_live_session(tmp_path)
    position = session.broker.get_account_info().open_positions[0]
    session.broker.accept_tick(
        session.broker.get_latest_tick("EURUSD").__class__(
            "EURUSD",
            datetime(2026, 8, 25, 10, 6, tzinfo=UTC),
            position.entry_price - 0.01,
            position.entry_price - 0.01,
            position.entry_price - 0.01,
        )
    )
    with pytest.raises(UnsafeCheckpointError, match="undrained_close_events"):
        create_checkpoint(
            session,
            store,
            software_version="1.0",
            execution_policy_id="policy-v1",
        )


def test_risk_snapshot_invalid_restore_is_atomic() -> None:
    risk = RiskEngine(RiskLimits(), PipSizes())
    before = risk.snapshot_state()
    invalid = dict(before)
    invalid["daily_trades"] = -1
    with pytest.raises(ValueError):
        risk.restore_state(invalid)
    assert risk.snapshot_state() == before


def test_risk_snapshot_restores_kill_counters_ids_and_reservations() -> None:
    risk = RiskEngine(RiskLimits(), PipSizes())
    state = {
        "kill_switch_active": True,
        "kill_switch_reason": "manual_shutdown",
        "consecutive_losses": 2,
        "daily_trades": 1,
        "last_reset_date": "2026-08-25",
        "daily_start_equity": 10000.0,
        "peak_equity": 10100.0,
        "approved_order_ids": ["order-1"],
        "reservations": [
            {"order_id": "order-1", "symbol": "EURUSD", "size_lots": "0.25"}
        ],
    }
    risk.restore_state(state)
    assert risk.snapshot_state() == state
    assert risk.kill_switch_reason.value == "manual_shutdown"  # type: ignore[union-attr]
    assert risk.reserved_position_count == 1


def test_order_and_broker_invalid_restore_are_atomic(tmp_path) -> None:
    session, _ = make_session(tmp_path / "atomic.sqlite")
    order_before = session.order_manager.snapshot_state()
    with pytest.raises(ValueError):
        session.order_manager.restore_state(
            {"audit_failed": False, "records": [{"client_order_id": "bad"}]}
        )
    assert session.order_manager.snapshot_state() == order_before

    broker_before = session.broker.snapshot_state()
    invalid_broker = dict(broker_before)
    invalid_broker["balance"] = float("nan")
    with pytest.raises(ValueError):
        session.broker.restore_state(invalid_broker)
    assert session.broker.snapshot_state() == broker_before


def test_recovery_does_not_invoke_policy(tmp_path) -> None:
    path, _, store, _ = checkpoint_live_session(tmp_path)
    store.close()
    calls = 0

    def forbidden_policy(signal, context):
        nonlocal calls
        calls += 1
        raise AssertionError("policy must not run during recovery")

    restored, reopened = make_session(path, execution_policy=forbidden_policy)
    restored.signal_engine.process_all_symbols = lambda symbols: (_ for _ in ()).throw(
        AssertionError("signal engine must not run during recovery")
    )
    restored.risk_engine.evaluate = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("risk evaluation must not run during recovery")
    )
    restored.broker.submit_order = lambda order: (_ for _ in ()).throw(
        AssertionError("broker submission must not run during recovery")
    )
    result = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.recovered
    assert calls == 0


@pytest.mark.parametrize(
    "change",
    [
        "broker_id",
        "implementation_version",
        "demo",
        "live",
        "capability_removed",
        "capability_added",
        "netting",
        "non_deterministic",
    ],
)
def test_recovery_rejects_broker_capability_identity_changes(
    tmp_path, monkeypatch, change
) -> None:
    path, _, store, _ = checkpoint_live_session(tmp_path)
    store.close()
    restored, reopened = make_session(path)
    base = restored.broker.broker_descriptor
    broker_id = "different-paper" if change == "broker_id" else base.broker_id
    version = "2" if change == "implementation_version" else base.implementation_version
    environment = (
        BrokerEnvironment.DEMO
        if change == "demo"
        else BrokerEnvironment.LIVE
        if change == "live"
        else base.environment
    )
    capabilities = set(base.capabilities)
    if change == "capability_removed":
        capabilities.remove(BrokerCapability.NATIVE_SL_TP)
    if change == "capability_added":
        capabilities.add(BrokerCapability.LIMIT_ORDERS)
    if change == "netting":
        capabilities.remove(BrokerCapability.HEDGING)
        capabilities.add(BrokerCapability.NETTING)
    replacement = BrokerDescriptor(
        broker_id,
        version,
        environment,
        frozenset(capabilities),
        change != "non_deterministic",
    )
    monkeypatch.setattr(
        PaperBroker, "broker_descriptor", property(lambda self: replacement)
    )
    result = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.state is RecoveryState.FAILED
    assert result.reason == "configuration_mismatch"


def test_broker_connection_state_does_not_change_recovery_identity(tmp_path) -> None:
    path, original, store, _ = checkpoint_live_session(tmp_path)
    assert original.broker.is_connected()
    store.close()
    restored, reopened = make_session(path)
    assert not restored.broker.is_connected()
    result = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.recovered


def test_recovery_rejects_provider_mapping_identity_change(tmp_path) -> None:
    path, _, store, _ = checkpoint_live_session(tmp_path)
    store.close()
    restored, reopened = make_session(path)
    restored.replay.mapping_identity = "b" * 64
    result = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.state is RecoveryState.FAILED
    assert result.reason == "configuration_mismatch"
