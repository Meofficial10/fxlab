"""Deterministic offline Historical Execution Replay V1C test suite.

Tests existing FXLab production lifecycle, failure, recovery, reconciliation,
and risk shutdown paths across 10 deterministic scenarios without MT5 or network calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from fxlab.execution.broker import (
    AccountInfo,
    BrokerOrderRejected,
    OrderRequest,
    OrderStatus,
    Tick,
)
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
)
from fxlab.execution.margin import UnmodeledPaperMargin
from fxlab.execution.market_data import MarketDataStream
from fxlab.execution.order_manager import (
    ExecutionIntent,
    ExecutionResultKind,
    OrderManager,
)
from fxlab.execution.paper_broker import PaperBroker
from fxlab.execution.paper_session import (
    CycleKind,
    HistoricalBarReplay,
    PaperTradingSession,
)
from fxlab.execution.recovery import (
    RecoveryState,
    create_checkpoint,
    recover,
)
from fxlab.execution.runtime_control import (
    RuntimeState,
)
from fxlab.execution.signal_engine import SignalEngine, SignalEvent
from fxlab.execution.valuation import (
    FxInstrumentCatalog,
    InstrumentSpec,
)
from fxlab.risk import (
    KillSwitchReason,
    RiskEngine,
    RiskLimits,
)

NOW = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)
CLIENT_ID = "setup-EURUSD-M5-20260825T100000000000Z-LONG"

CATALOG = FxInstrumentCatalog(
    (
        InstrumentSpec("EURUSD", "fx", "EUR", "USD", 0.0001, 100_000, "1"),
        InstrumentSpec("GBPUSD", "fx", "GBP", "USD", 0.0001, 100_000, "1"),
        InstrumentSpec("USDJPY", "fx", "USD", "JPY", 0.01, 100_000, "1"),
    )
)


class PipSizesResolver:
    def pip_size_for(self, symbol: str) -> float:
        return 0.0001


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


def sample_bars(periods: int = 2) -> pd.DataFrame:
    index = pd.date_range("2026-08-25 10:00", periods=periods, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.1000, 1.1010][:periods],
            "high": [1.1010, 1.1020][:periods],
            "low": [1.0990, 1.1000][:periods],
            "close": [1.1000, 1.1010][:periods],
            "volume": [1.0, 1.0][:periods],
        },
        index=index,
    )


def make_signal(**overrides: object) -> SignalEvent:
    values = {
        "setup_name": "model_a_sweep_reversal",
        "symbol": "EURUSD",
        "timeframe": "M5",
        "side": 1,
        "signal_time": NOW,
        "signal_bar_index": 100,
    }
    values.update(overrides)
    return SignalEvent(**values)  # type: ignore[arg-type]


def make_intent(event: SignalEvent | None = None, **overrides: object) -> ExecutionIntent:
    values = {"signal": event or make_signal(), "sl_price": 1.0900, "tp_price": 1.1200}
    values.update(overrides)
    return ExecutionIntent(**values)  # type: ignore[arg-type]


def make_account(balance: float = 10_000.0) -> AccountInfo:
    return AccountInfo(
        balance=balance,
        equity=balance,
        margin_used=0.0,
        margin_available=balance,
        currency="USD",
    )


def make_tick(
    symbol: str = "EURUSD",
    when: datetime = NOW,
    *,
    bid: float = 1.1000,
    ask: float = 1.1002,
) -> Tick:
    return Tick(symbol=symbol, timestamp=when, bid=bid, ask=ask, mid=(bid + ask) / 2.0)


def make_paper_session(
    tmp_path: object,
    session_id: str = "test-session",
    *,
    setup: object = None,
    limits: RiskLimits | None = None,
    frame: pd.DataFrame | None = None,
) -> tuple[PaperTradingSession, SQLiteEventStore, RiskEngine, PaperBroker]:
    bars_df = frame if frame is not None else sample_bars(2)
    db_path = tmp_path / f"{session_id}.sqlite"  # type: ignore[operator]
    store = SQLiteEventStore(db_path, session_id)
    ledger = EventLedger(session_id, time_provider=lambda: NOW, durable_store=store)
    broker = PaperBroker(
        "USD",
        CATALOG,
        timedelta(minutes=5),
        "fx-point-in-time-v1",
        UnmodeledPaperMargin("USD"),
        "USD",
        historical_bars={("EURUSD", "M5"): bars_df},
    )
    replay = HistoricalBarReplay({"EURUSD": bars_df}, "M5")
    market = MarketDataStream(
        broker, ["EURUSD"], time_provider=lambda: datetime(1990, 1, 1, tzinfo=UTC)
    )
    risk = RiskEngine(
        limits or RiskLimits(max_open_positions=5, max_trades_per_day=5),
        PipSizesResolver(),
    )
    order_mgr = OrderManager(broker, risk, ledger)
    sig_setup = setup or NoSignalSetup()
    sig_engine = SignalEngine(sig_setup, market, "M5")  # type: ignore[arg-type]
    session = PaperTradingSession(
        broker=broker,
        replay=replay,
        market_data=market,
        signal_engine=sig_engine,
        order_manager=order_mgr,
        risk_engine=risk,
        execution_policy=lambda signal, ctx: ExecutionIntent(signal, sl_price=1.0900),
        event_ledger=ledger,
    )
    return session, store, risk, broker


# ---------------------------------------------------------------------------
# Scenario 11: Broker Entry Rejection
# ---------------------------------------------------------------------------
def test_replay_scenario_11_broker_entry_rejection() -> None:
    """Scenario 11: Authoritative broker rejection releases reservation without retry."""
    ledger = EventLedger("scenario-11-rejection")
    risk = RiskEngine(
        limits=RiskLimits(max_open_positions=2, max_trades_per_day=5),
        pip_size_resolver=PipSizesResolver(),
    )

    class RejectingBroker(PaperBroker):
        def __init__(self) -> None:
            super().__init__(
                "USD",
                CATALOG,
                timedelta(minutes=5),
                "fx-point-in-time-v1",
                UnmodeledPaperMargin("USD"),
                "USD",
            )
            self.connect()
            self.subscribe_market_data(["EURUSD"])
            self.accept_tick(make_tick())
            self.submit_attempts = 0

        def submit_order(self, order: OrderRequest) -> str:
            self.submit_attempts += 1
            raise BrokerOrderRejected("insufficient_margin", rejection_transaction_id="tx-rej-11")

    broker = RejectingBroker()
    order_mgr = OrderManager(broker=broker, risk_engine=risk, event_ledger=ledger)

    intent = make_intent()
    result = order_mgr.submit(intent, current_time=NOW)

    # 1. Exactly one submission attempt
    assert broker.submit_attempts == 1
    # 2. Rejected state is explicit
    assert result.kind is ExecutionResultKind.EXECUTION_REJECTED
    assert result.reason == "broker_order_rejected"
    assert result.record is not None
    assert result.record.status is OrderStatus.REJECTED
    # 3. No position created
    assert len(broker.get_account_info().open_positions) == 0
    # 4. Reservation is released
    assert result.record.reservation_released is True
    assert risk.reserved_position_count == 0
    assert risk.daily_trades == 1
    assert risk.kill_switch_active is False
    # 5. Ledger audit events recorded
    events = ledger.events()
    assert [e.event_type for e in events] == [
        AuditEventType.RISK_APPROVED,
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        AuditEventType.ORDER_REJECTED,
        AuditEventType.RESERVATION_RELEASED,
    ]


# ---------------------------------------------------------------------------
# Scenario 12: Ambiguous Submission
# ---------------------------------------------------------------------------
def test_replay_scenario_12_ambiguous_submission() -> None:
    """Scenario 12: Unhandled submission exception latches kill switch and marks indeterminate."""
    ledger = EventLedger("scenario-12-ambiguous")
    risk = RiskEngine(
        limits=RiskLimits(max_open_positions=2, max_trades_per_day=5),
        pip_size_resolver=PipSizesResolver(),
    )

    class TransportErrorBroker(PaperBroker):
        def __init__(self) -> None:
            super().__init__(
                "USD",
                CATALOG,
                timedelta(minutes=5),
                "fx-point-in-time-v1",
                UnmodeledPaperMargin("USD"),
                "USD",
            )
            self.connect()
            self.subscribe_market_data(["EURUSD"])
            self.accept_tick(make_tick())
            self.submit_attempts = 0

        def submit_order(self, order: OrderRequest) -> str:
            self.submit_attempts += 1
            raise RuntimeError("transport_timeout_during_submission")

    broker = TransportErrorBroker()
    order_mgr = OrderManager(broker=broker, risk_engine=risk, event_ledger=ledger)

    intent = make_intent()
    result = order_mgr.submit(intent, current_time=NOW)

    # 1. Exactly one attempt; no blind retry
    assert broker.submit_attempts == 1
    # 2. Uncertainty is explicit
    assert result.kind is ExecutionResultKind.INDETERMINATE
    assert result.reason == "broker_submission_exception"
    # 3. Reservation is preserved (not released blindly)
    assert risk.reserved_position_count == 1
    # 4. Reconciliation kill switch latches fail-closed
    assert risk.kill_switch_active is True
    assert risk.kill_switch_reason is KillSwitchReason.POSITION_RECONCILIATION_FAILED


# ---------------------------------------------------------------------------
# Scenario 13: Controlled-Close Rejection
# ---------------------------------------------------------------------------
def test_replay_scenario_13_controlled_close_rejection() -> None:
    """Scenario 13: Rejection on position close leaves position open without duplicate mutation."""
    broker = PaperBroker(
        "USD",
        CATALOG,
        timedelta(minutes=5),
        "fx-point-in-time-v1",
        UnmodeledPaperMargin("USD"),
        "USD",
    )
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])
    broker.accept_tick(make_tick())

    # Create owned open position
    req = OrderRequest("EURUSD", 1, 0.1, "market", "close-rej-pos-1", sl_price=1.0900)
    broker_id = broker.submit_order(req)
    assert broker_id is not None
    assert len(broker.get_account_info().open_positions) == 1
    pos_id = broker.get_account_info().open_positions[0].position_id

    # Attempt to close non-existent or invalid position
    close_res = broker.close_position("non_existent_position_id")
    assert close_res is None

    # Owned open position remains intact
    assert len(broker.get_account_info().open_positions) == 1
    assert broker.get_account_info().open_positions[0].position_id == pos_id


# ---------------------------------------------------------------------------
# Scenario 14: Delayed Fill / History Visibility
# ---------------------------------------------------------------------------
def test_replay_scenario_14_delayed_fill_visibility() -> None:
    """Scenario 14: Pending order preserves reservation until authoritative status arrives."""
    ledger = EventLedger("scenario-14-delayed-fill")
    risk = RiskEngine(
        limits=RiskLimits(max_open_positions=2, max_trades_per_day=5),
        pip_size_resolver=PipSizesResolver(),
    )

    class DelayedFillBroker(PaperBroker):
        def __init__(self) -> None:
            super().__init__(
                "USD",
                CATALOG,
                timedelta(minutes=5),
                "fx-point-in-time-v1",
                UnmodeledPaperMargin("USD"),
                "USD",
            )
            self.connect()
            self.subscribe_market_data(["EURUSD"])
            self.accept_tick(make_tick())
            self.current_status = OrderStatus.PENDING

        def submit_order(self, order: OrderRequest) -> str:
            return "delayed-broker-id-1"

        def get_order_status(self, order_id: str) -> dict:
            return {
                "status": self.current_status.value,
                "client_order_id": "delayed-client-1",
                "broker_order_id": order_id,
            }

    broker = DelayedFillBroker()
    order_mgr = OrderManager(broker=broker, risk_engine=risk, event_ledger=ledger)

    intent = make_intent()
    result = order_mgr.submit(intent, current_time=NOW)
    assert result.kind is ExecutionResultKind.SUBMITTED
    client_id = result.record.client_order_id  # type: ignore[union-attr]

    # Poll status while still PENDING -> reservation remains held
    res1 = order_mgr.refresh_order_status(client_id)
    assert res1.kind is ExecutionResultKind.STATUS_UPDATED
    assert res1.record.status is OrderStatus.PENDING  # type: ignore[union-attr]
    assert res1.record.reservation_released is False  # type: ignore[union-attr]
    assert risk.reserved_position_count == 1

    # Broker confirms FILLED -> status updated, reservation still active
    broker.current_status = OrderStatus.FILLED
    res2 = order_mgr.refresh_order_status(client_id)
    assert res2.record.status is OrderStatus.FILLED  # type: ignore[union-attr]
    assert res2.record.reservation_released is False  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Scenario 15: Session Boundary with Owned Open Position
# ---------------------------------------------------------------------------
def test_replay_scenario_15_session_boundary_with_open_position(tmp_path: object) -> None:
    """Scenario 15: Stopping session with open exposure preserves state without auto-liquidation."""
    session, store, risk, broker = make_paper_session(
        tmp_path,
        session_id="session-15",
        setup=FirstSignalSetup(),
        frame=sample_bars(2),
    )
    session.start()
    cycle = session.poll_once()
    assert cycle.executions
    assert len(broker.get_account_info().open_positions) == 1
    pos_id = broker.get_account_info().open_positions[0].position_id

    # Stop session at cycle boundary
    session.stop()
    assert not broker.is_connected()
    # Production contract: session stop does not auto-close positions; open exposure persists
    assert len(broker.get_account_info().open_positions) == 1
    assert broker.get_account_info().open_positions[0].position_id == pos_id
    # Further polling fails closed
    assert session.poll_once().reason == "session_not_running"


# ---------------------------------------------------------------------------
# Scenario 16: Restart with Owned Open Position
# ---------------------------------------------------------------------------
def test_replay_scenario_16_restart_with_open_position(tmp_path: object) -> None:
    """Scenario 16: Durable checkpoint preserves open position and restores cleanly."""
    session1, store, _, broker1 = make_paper_session(
        tmp_path,
        session_id="session-16",
        setup=FirstSignalSetup(),
        frame=sample_bars(2),
    )
    session1.start()
    session1.poll_once()
    assert len(broker1.get_account_info().open_positions) == 1
    orig_pos = broker1.get_account_info().open_positions[0]

    # Create safe checkpoint directly at the current safe point
    create_checkpoint(
        session1,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
        created_at=NOW,
    )

    # Instantiate fresh session 2 pointing to the same SQLite store
    session2, _, risk2, broker2 = make_paper_session(
        tmp_path,
        session_id="session-16",
        setup=NoSignalSetup(),
        frame=sample_bars(2),
    )
    rec_result = recover(
        session2,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
    )
    assert rec_result.recovered is True
    assert rec_result.state is RecoveryState.RECOVERED

    # Reconstructed broker has exact position with original ID
    restored_positions = broker2.get_account_info().open_positions
    assert len(restored_positions) == 1
    assert restored_positions[0].position_id == orig_pos.position_id
    assert restored_positions[0].entry_price == orig_pos.entry_price


# ---------------------------------------------------------------------------
# Scenario 17: Restart with Unresolved / In-Flight Order
# ---------------------------------------------------------------------------
def test_replay_scenario_17_restart_with_unresolved_order(tmp_path: object) -> None:
    """Scenario 17: Unresolved events after checkpoint require reconciliation on recovery."""
    session1, store, _, _ = make_paper_session(
        tmp_path,
        session_id="session-17",
        setup=NoSignalSetup(),
        frame=sample_bars(2),
    )
    # Create safe checkpoint
    create_checkpoint(
        session1,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
        created_at=NOW,
    )

    # Append uncommitted/unresolved submission attempt to event ledger
    session1.event_ledger.append(
        AuditEventType.ORDER_SUBMISSION_ATTEMPTED,
        occurred_at=NOW + timedelta(seconds=5),
        component=AuditComponent.ORDER_MANAGER,
        correlation=EventCorrelation(signal_id=CLIENT_ID, client_order_id=CLIENT_ID),
        payload={"symbol": "EURUSD", "side": 1, "size": 0.1},
    )

    # Instantiate new session 2 and attempt recovery
    session2, _, _, _ = make_paper_session(
        tmp_path,
        session_id="session-17",
        setup=NoSignalSetup(),
        frame=sample_bars(2),
    )
    rec_result = recover(
        session2,
        store,
        software_version="1.0",
        execution_policy_id="policy-v1",
    )

    # Must fail closed: RECONCILIATION_REQUIRED, never silently FILLED/REJECTED
    assert rec_result.state is RecoveryState.RECONCILIATION_REQUIRED
    assert session2.recovery_required is True
    with pytest.raises(RuntimeError, match="reconciliation is required"):
        session2.start()


# ---------------------------------------------------------------------------
# Scenario 18: Disconnect -> Reconciliation -> Safe Resume
# ---------------------------------------------------------------------------
def test_replay_scenario_18_disconnect_reconciliation_safe_resume(tmp_path: object) -> None:
    """Scenario 18: Reconnect cannot resume trading while reconciliation is unresolved."""
    session, store, risk, broker = make_paper_session(
        tmp_path,
        session_id="session-18",
        setup=NoSignalSetup(),
        frame=sample_bars(2),
    )
    session.start()
    session.poll_once()

    # Trigger pause
    paused = session.pause()
    assert paused.accepted and paused.changed
    assert session.runtime_status().state is RuntimeState.PAUSED

    # Mark reconciliation required
    session.require_reconciliation()
    assert session.recovery_required is True
    assert session.runtime_status().state is RuntimeState.RECONCILIATION_REQUIRED

    # Resume must be rejected while reconciliation is unresolved
    resume_result = session.resume()
    assert resume_result.accepted is False
    assert session.runtime_status().state is RuntimeState.RECONCILIATION_REQUIRED


# ---------------------------------------------------------------------------
# Scenario 19: Consecutive Losses / Risk Exhaustion
# ---------------------------------------------------------------------------
def test_replay_scenario_19_consecutive_losses_risk_exhaustion() -> None:
    """Scenario 19: Configured consecutive losses trigger kill switch and block trading."""
    limits = RiskLimits(
        max_open_positions=5,
        max_trades_per_day=10,
        max_consecutive_losses=2,
    )
    risk = RiskEngine(limits, PipSizesResolver())
    broker = PaperBroker(
        "USD",
        CATALOG,
        timedelta(minutes=5),
        "fx-point-in-time-v1",
        UnmodeledPaperMargin("USD"),
        "USD",
    )
    broker.connect()
    broker.subscribe_market_data(["EURUSD"])
    broker.accept_tick(make_tick())

    order_mgr = OrderManager(broker=broker, risk_engine=risk)

    # Loss 1
    risk.on_trade_closed(-25.0)
    assert risk.consecutive_losses == 1
    assert risk.kill_switch_active is False

    # Loss 2 (hits threshold 2)
    risk.on_trade_closed(-30.0)
    assert risk.consecutive_losses == 2
    assert risk.kill_switch_active is True
    assert risk.kill_switch_reason is KillSwitchReason.MAX_CONSECUTIVE_LOSSES

    # Subsequent submission is denied by risk engine
    intent = make_intent()
    result = order_mgr.submit(intent, current_time=NOW)
    assert result.kind is ExecutionResultKind.RISK_REJECTED
    assert result.risk_rejection is not None
    assert result.risk_rejection.reason == "kill_switch_active"


# ---------------------------------------------------------------------------
# Scenario 20: Kill-Switch Activation -> Further Trading Blocked
# ---------------------------------------------------------------------------
def test_replay_scenario_20_kill_switch_blocks_further_trading(tmp_path: object) -> None:
    """Scenario 20: Manual emergency stop sets KILL_SWITCHED state and blocks execution."""
    session, _, risk, broker = make_paper_session(
        tmp_path,
        session_id="session-20",
        setup=FirstSignalSetup(),
        frame=sample_bars(2),
    )
    session.start()

    # Trigger emergency stop
    em_result = session.emergency_stop()
    assert em_result.accepted and em_result.changed
    assert risk.kill_switch_reason is KillSwitchReason.MANUAL
    assert session.runtime_status().state is RuntimeState.KILL_SWITCHED

    # Subsequent poll fails closed with FAILED cycle kind and kill switch reason
    poll_result = session.poll_once()
    assert poll_result.kind is CycleKind.FAILED
    assert poll_result.reason == "kill_switch_active"
    assert poll_result.signals == ()
    assert not broker.get_account_info().open_positions

    # Resume is rejected
    assert not session.resume().accepted
