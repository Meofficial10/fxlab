"""Focused tests for exact, fail-closed PaperBroker reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from fxlab.execution.broker import OrderRequest, OrderStatus, Tick
from fxlab.execution.durable_event_store import DurableStoreError, SQLiteEventStore
from fxlab.execution.event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
)
from fxlab.execution.margin import UnmodeledPaperMargin
from fxlab.execution.market_data import MarketDataStream
from fxlab.execution.order_manager import OrderManager
from fxlab.execution.paper_broker import PaperBroker
from fxlab.execution.paper_session import HistoricalBarReplay, PaperTradingSession
from fxlab.execution.reconciliation import ReconciliationEngine, ReconciliationStatus
from fxlab.execution.recovery import RecoveryState, create_checkpoint, recover
from fxlab.execution.signal_engine import SignalEngine
from fxlab.execution.valuation import approved_fx_instrument_catalog
from fxlab.risk import KillSwitchReason, RiskEngine, RiskLimits

NOW = datetime(2026, 8, 25, 10, 10, tzinfo=UTC)
CLIENT_ID = "setup-EURUSD-M5-20260825T100000000000Z-LONG"


class PipSizes:
    def pip_size_for(self, symbol: str) -> float:
        return 0.0001


class NoSignal:
    name = "no_signal"

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        return np.array([], dtype=int), np.array([], dtype=int)


def bars() -> pd.DataFrame:
    index = pd.date_range("2026-08-25 10:00", periods=2, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.1, 1.101],
            "high": [1.101, 1.102],
            "low": [1.099, 1.1],
            "close": [1.1, 1.101],
            "volume": [1.0, 1.0],
        },
        index=index,
    )


def make_session(path, session_id: str) -> tuple[PaperTradingSession, SQLiteEventStore]:
    frame = bars()
    store = SQLiteEventStore(path, session_id)
    ledger = EventLedger(session_id, time_provider=lambda: NOW, durable_store=store)
    broker = PaperBroker(
        "USD",
        approved_fx_instrument_catalog(),
        timedelta(minutes=5),
        "fx-point-in-time-v1",
        UnmodeledPaperMargin("USD"),
        "USD",
        historical_bars={("EURUSD", "M5"): frame},
    )
    replay = HistoricalBarReplay({"EURUSD": frame}, "M5")
    market = MarketDataStream(
        broker, ["EURUSD"], time_provider=lambda: datetime(1990, 1, 1, tzinfo=UTC)
    )
    risk = RiskEngine(
        RiskLimits(max_open_positions=5, max_trades_per_day=5), PipSizes()
    )
    manager = OrderManager(broker, risk, ledger)
    session = PaperTradingSession(
        broker,
        replay,
        market,
        SignalEngine(NoSignal(), market, "M5"),
        manager,
        risk,
        lambda signal, context: None,
        ledger,
    )
    return session, store


def checkpointed(tmp_path) -> tuple[PaperTradingSession, SQLiteEventStore]:
    session, store = make_session(tmp_path / "old.sqlite", "old-session")
    create_checkpoint(
        session,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
        created_at=NOW,
    )
    return session, store


def new_target(tmp_path) -> tuple[PaperTradingSession, SQLiteEventStore]:
    return make_session(tmp_path / "new.sqlite", "new-session")


def set_approval(session: PaperTradingSession) -> None:
    state = session.risk_engine.snapshot_state()
    state["daily_trades"] = 1
    state["last_reset_date"] = "2026-08-25"
    state["daily_start_equity"] = 10.0
    state["peak_equity"] = 10.0
    state["approved_order_ids"] = [CLIENT_ID]
    state["reservations"] = [
        {"order_id": CLIENT_ID, "symbol": "EURUSD", "size_lots": "0.1"}
    ]
    session.risk_engine.restore_state(state)


def append_approval(session: PaperTradingSession) -> None:
    session.event_ledger.append(
        AuditEventType.RISK_APPROVED,
        occurred_at=NOW,
        component=AuditComponent.RISK_ENGINE,
        correlation=EventCorrelation(signal_id=CLIENT_ID, client_order_id=CLIENT_ID),
        payload={"size_lots": 0.1},
    )


def append_attempt(session: PaperTradingSession) -> None:
    session.event_ledger.append(
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        occurred_at=NOW,
        component=AuditComponent.ORDER_MANAGER,
        correlation=EventCorrelation(signal_id=CLIENT_ID, client_order_id=CLIENT_ID),
        payload={
            "symbol": "EURUSD",
            "side": 1,
            "size": 0.1,
            "order_type": "market",
            "sl_price": 1.09,
            "tp_price": None,
        },
    )


def prepare_filled(session: PaperTradingSession) -> str:
    set_approval(session)
    append_approval(session)
    append_attempt(session)
    session.broker.connect()
    session.broker.subscribe_market_data(["EURUSD"])
    session.broker.accept_tick(Tick("EURUSD", NOW, 1.1, 1.1002, 1.1001))
    broker_id = session.broker.submit_order(
        OrderRequest("EURUSD", 1, 0.1, "market", CLIENT_ID, sl_price=1.09)
    )
    correlation = session.broker.get_correlation(CLIENT_ID)
    assert correlation is not None
    session.event_ledger.append(
        AuditEventType.ORDER_SUBMITTED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_BROKER,
        correlation=EventCorrelation(
            signal_id=CLIENT_ID,
            client_order_id=CLIENT_ID,
            broker_order_id=broker_id,
        ),
        payload={"status": "pending"},
    )
    session.event_ledger.append(
        AuditEventType.ORDER_FILLED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_BROKER,
        correlation=EventCorrelation(
            signal_id=CLIENT_ID,
            client_order_id=CLIENT_ID,
            broker_order_id=broker_id,
            position_id=correlation.position_id,
        ),
        payload={"status": "filled"},
    )
    session.require_reconciliation()
    return broker_id


def engine(session, store) -> ReconciliationEngine:
    return ReconciliationEngine(session, store, "1.0", "policy-v1")


def test_reconciliation_id_is_deterministic_and_inspection_is_read_only(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    append_attempt(session)
    session.require_reconciliation()
    before = session.risk_engine.snapshot_state(), store.last_sequence()
    first = engine(session, store).inspect()
    second = engine(session, store).inspect()
    assert first == second
    assert first.reconciliation_id.startswith("reconcile-")
    assert before == (session.risk_engine.snapshot_state(), store.last_sequence())


def test_attempt_without_ack_and_indeterminate_remain_unresolved(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    append_attempt(session)
    session.event_ledger.append(
        AuditEventType.ORDER_SUBMISSION_INDETERMINATE,
        occurred_at=NOW,
        component=AuditComponent.ORDER_MANAGER,
        correlation=EventCorrelation(client_order_id=CLIENT_ID),
        payload={"reason": "broker_submission_exception"},
    )
    session.require_reconciliation()
    result = engine(session, store).reconcile(occurred_at=NOW)
    assert result.status is ReconciliationStatus.UNRESOLVED
    assert result.reason == "submission_outcome_unknown"
    assert session.recovery_required
    assert [event.event_type for event in session.event_ledger.events()][-2:] == [
        AuditEventType.RECONCILIATION_STARTED,
        AuditEventType.RECONCILIATION_UNRESOLVED,
    ]


def test_approval_without_attempt_requires_live_reservation(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    append_approval(session)
    session.require_reconciliation()
    assert engine(session, store).inspect(
        authoritative_broker_state=False
    ).reason == "audit_evidence_incomplete"
    assert engine(session, store).inspect(
        authoritative_broker_state=True
    ).reason == "audit_evidence_incomplete"


def test_live_approval_without_attempt_releases_only_reservation(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    set_approval(session)
    append_approval(session)
    session.require_reconciliation()
    target, target_store = new_target(tmp_path)
    before_daily = session.risk_engine.daily_trades
    result = engine(session, store).reconcile(
        new_session=target,
        new_store=target_store,
        authoritative_broker_state=True,
        occurred_at=NOW,
    )
    assert result.status is ReconciliationStatus.RESOLVED
    assert session.risk_engine.kill_switch_active
    assert target.risk_engine.daily_trades == before_daily
    assert CLIENT_ID in target.risk_engine.approved_order_ids
    assert target.risk_engine.reserved_position_count == 0
    assert not target.risk_engine.kill_switch_active
    assert target_store.load_latest_checkpoint() is not None
    assert target_store.last_sequence() == 0
    assert not target.broker.is_connected()
    old_types = [event.event_type for event in session.event_ledger.events()]
    assert old_types.index(AuditEventType.RECONCILIATION_STARTED) < old_types.index(
        AuditEventType.RECONCILIATION_RESOLVED
    )
    assert store.load_latest_checkpoint().last_event_sequence == store.last_sequence()  # type: ignore[union-attr]
    target.start()
    assert target.event_ledger.events()[0].sequence == 1
    assert target.event_ledger.events()[0].event_type is AuditEventType.SESSION_STARTED
    target.stop()


def test_filled_order_repairs_exact_record_reflection_and_reservation(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    broker_id = prepare_filled(session)
    target, target_store = new_target(tmp_path)
    result = engine(session, store).reconcile(
        new_session=target,
        new_store=target_store,
        authoritative_broker_state=True,
        occurred_at=NOW,
    )
    assert result.status is ReconciliationStatus.RESOLVED
    record = target.order_manager.get_order(CLIENT_ID)
    assert record is not None
    assert record.broker_order_id == broker_id
    assert record.status is OrderStatus.FILLED
    assert record.reservation_released
    position = target.broker.get_account_info().open_positions[0]
    session_state = target.snapshot_state()
    assert session_state["position_correlations"][0]["position_id"] == position.position_id
    assert target.risk_engine.daily_trades == 1
    assert target.risk_engine.reserved_position_count == 0


@pytest.mark.parametrize("status", [OrderStatus.REJECTED, OrderStatus.CANCELLED])
def test_exact_terminal_nonfill_releases_reservation(tmp_path, status) -> None:
    session, store = checkpointed(tmp_path)
    broker_id = prepare_filled(session)
    broker_state = session.broker.snapshot_state()
    broker_state["positions"] = []
    broker_state["statuses"] = {broker_id: status.value}
    broker_state["equity"] = broker_state["balance"]
    session.broker.restore_state(broker_state)
    event_type = (
        AuditEventType.ORDER_REJECTED
        if status is OrderStatus.REJECTED
        else AuditEventType.ORDER_CANCELLED
    )
    session.event_ledger.append(
        event_type,
        occurred_at=NOW,
        component=AuditComponent.PAPER_BROKER,
        correlation=EventCorrelation(
            client_order_id=CLIENT_ID, broker_order_id=broker_id
        ),
        payload={"status": status.value},
    )
    target, target_store = new_target(tmp_path)
    result = engine(session, store).reconcile(
        new_session=target,
        new_store=target_store,
        authoritative_broker_state=True,
        occurred_at=NOW,
    )
    assert result.status is ReconciliationStatus.RESOLVED
    assert target.order_manager.get_order(CLIENT_ID).status is status  # type: ignore[union-attr]
    assert target.broker.get_account_info().open_positions == []
    assert target.risk_engine.reserved_position_count == 0


def test_exact_identity_is_required_and_heuristic_similarity_is_ignored(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    prepare_filled(session)
    state = session.broker.snapshot_state()
    state["correlations"][0]["client_order_id"] = "different-client"
    with pytest.raises(ValueError):
        session.broker.restore_state(state)
    # The production restore validator itself rejects a correlation rewrite even
    # though all economic order fields still look alike.


def test_checkpoint_restored_broker_is_not_post_checkpoint_authority(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    prepare_filled(session)
    plan = engine(session, store).inspect(authoritative_broker_state=False)
    assert not plan.resolvable
    assert plan.reason == "audit_evidence_incomplete"


def test_terminal_status_with_surviving_position_is_account_mismatch(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    broker_id = prepare_filled(session)
    state = session.broker.snapshot_state()
    state["statuses"] = {broker_id: OrderStatus.REJECTED.value}
    session.broker.restore_state(state)
    session.event_ledger.append(
        AuditEventType.ORDER_REJECTED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_BROKER,
        correlation=EventCorrelation(
            client_order_id=CLIENT_ID, broker_order_id=broker_id
        ),
        payload={"status": "rejected"},
    )
    plan = engine(session, store).inspect(authoritative_broker_state=True)
    assert not plan.resolvable
    assert plan.reason == "account_mismatch"


def test_close_tail_is_never_replayed(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    session.event_ledger.append(
        AuditEventType.POSITION_CLOSED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_BROKER,
        correlation=EventCorrelation(
            client_order_id=CLIENT_ID,
            position_id=f"paper-position::{CLIENT_ID}",
            close_order_id=f"paper-close::paper-position::{CLIENT_ID}",
        ),
        payload={"net_realized_pnl": -1.0},
    )
    session.require_reconciliation()
    losses = session.risk_engine.consecutive_losses
    result = engine(session, store).reconcile(occurred_at=NOW)
    assert result.status is ReconciliationStatus.UNRESOLVED
    assert result.reason == "close_accounting_uncertain"
    assert session.risk_engine.consecutive_losses == losses


def test_reconcile_does_not_execute_trading_side_effects(tmp_path, monkeypatch) -> None:
    session, store = checkpointed(tmp_path)
    prepare_filled(session)
    target, target_store = new_target(tmp_path)
    monkeypatch.setattr(
        session.risk_engine,
        "evaluate",
        lambda *args, **kwargs: pytest.fail("evaluate must not run"),
    )
    monkeypatch.setattr(
        session.broker,
        "submit_order",
        lambda order: pytest.fail("submit must not run"),
    )
    monkeypatch.setattr(
        session.broker,
        "close_position",
        lambda position_id: pytest.fail("close must not run"),
    )
    result = engine(session, store).reconcile(
        new_session=target,
        new_store=target_store,
        authoritative_broker_state=True,
        occurred_at=NOW,
    )
    assert result.status is ReconciliationStatus.RESOLVED


def test_resolved_commit_is_idempotent(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    set_approval(session)
    append_approval(session)
    session.require_reconciliation()
    target, target_store = new_target(tmp_path)
    reconciler = engine(session, store)
    first = reconciler.reconcile(
        new_session=target,
        new_store=target_store,
        authoritative_broker_state=True,
        occurred_at=NOW,
    )
    count = store.last_sequence()
    second = reconciler.reconcile(new_session=target, new_store=target_store)
    assert first.status is ReconciliationStatus.RESOLVED
    assert second.status is ReconciliationStatus.RESOLVED
    assert second.reason == "already_reconciled"
    assert store.last_sequence() == count


def test_reconciled_old_checkpoint_never_recovers_as_runnable(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    set_approval(session)
    append_approval(session)
    session.require_reconciliation()
    target, target_store = new_target(tmp_path)
    assert engine(session, store).reconcile(
        new_session=target,
        new_store=target_store,
        authoritative_broker_state=True,
        occurred_at=NOW,
    ).status is ReconciliationStatus.RESOLVED
    store.close()
    restored, reopened = make_session(tmp_path / "old.sqlite", "old-session")
    result = recover(
        restored, reopened, software_version="1.0", execution_policy_id="policy-v1"
    )
    assert result.state is RecoveryState.RECONCILIATION_REQUIRED
    assert restored.recovery_required
    with pytest.raises(RuntimeError, match="reconciliation"):
        restored.start()


def test_checkpoint_failure_rolls_back_component_state(tmp_path, monkeypatch) -> None:
    session, store = checkpointed(tmp_path)
    set_approval(session)
    append_approval(session)
    session.require_reconciliation()
    target, target_store = new_target(tmp_path)
    before = session.risk_engine.snapshot_state()
    original = store.store_checkpoint

    def fail(checkpoint) -> None:
        raise DurableStoreError("forced failure")

    monkeypatch.setattr(store, "store_checkpoint", fail)
    result = engine(session, store).reconcile(
        new_session=target,
        new_store=target_store,
        authoritative_broker_state=True,
        occurred_at=NOW,
    )
    monkeypatch.setattr(store, "store_checkpoint", original)
    assert result.status is ReconciliationStatus.FAILED
    assert result.reason == "checkpoint_failed"
    assert session.risk_engine.snapshot_state() == before
    assert target_store.load_latest_checkpoint() is None
    assert target.recovery_required


def test_non_reconciliation_kill_switch_is_carried_forward(tmp_path) -> None:
    session, store = checkpointed(tmp_path)
    set_approval(session)
    session.risk_engine.trigger_kill_switch(KillSwitchReason.MANUAL)
    append_approval(session)
    session.require_reconciliation()
    target, target_store = new_target(tmp_path)
    result = engine(session, store).reconcile(
        new_session=target,
        new_store=target_store,
        authoritative_broker_state=True,
        occurred_at=NOW,
    )
    assert result.status is ReconciliationStatus.RESOLVED
    assert target.risk_engine.kill_switch_reason is KillSwitchReason.MANUAL
    with pytest.raises(RuntimeError, match="kill switch"):
        # The session lifecycle can start, but risk remains fail-closed. Verify the
        # carried state directly instead of weakening it for reconciliation.
        if target.risk_engine.kill_switch_active:
            raise RuntimeError("kill switch remains active")
