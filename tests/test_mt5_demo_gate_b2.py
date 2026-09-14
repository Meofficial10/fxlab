"""Offline tests for Gate B2 Repeated MT5 DEMO Reliability (Gate B2)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.test_mt5_demo_broker import FakeMt5Api
from typer.testing import CliRunner

from fxlab.cli import app
from fxlab.execution.durable_event_store import SQLiteEventStore
from fxlab.execution.event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
)
from fxlab.execution.mt5_demo_broker import Mt5AccountExposure, Mt5DemoBroker
from fxlab.execution.mt5_demo_gate_b2 import (
    GATE_B2_CONFIRM_TEXT,
    GateB2AuditValidationError,
    Mt5DemoGateB2Config,
    Mt5DemoGateB2Orchestrator,
    validate_run_audit,
)
from fxlab.execution.mt5_demo_preflight import Mt5DemoPreflight, Mt5PreflightResult
from fxlab.execution.mt5_demo_soak import Mt5DemoSoakConfig, Mt5DemoSoakResult

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _make_valid_config(tmp_path: Path, **overrides: Any) -> Mt5DemoGateB2Config:
    params = {
        "confirm_text": GATE_B2_CONFIRM_TEXT,
        "account_id": "12345678",
        "account_server": "Pepperstone-Demo",
        "account_company": "Pepperstone Group Limited",
        "audit_db_dir": tmp_path / "gate_b2_audits",
        "total_runs": 5,
        "max_loss_usd": 1.0,
        "max_entries_per_run": 2,
        "max_duration_seconds": 300.0,
        "drain_timeout_seconds": 300.0,
        "max_quote_age_seconds": 5.0,
        "poll_interval_seconds": 1.0,
        "cooldown_seconds": 30.0,
        "inter_run_cooldown_seconds": 30.0,
    }
    params.update(overrides)
    return Mt5DemoGateB2Config(**params)


class MockSoakRunner:
    """Mock soak runner simulating soak runs with audit trail recording."""

    def __init__(
        self,
        status: str = "completed",
        stop_reason: str = "max_entries_reached",
        entries_completed: int = 2,
        duration_seconds: float = 10.0,
        error_message: str | None = None,
        custom_behavior: Any = None,
    ) -> None:
        self.status = status
        self.stop_reason = stop_reason
        self.entries_completed = entries_completed
        self.duration_seconds = duration_seconds
        self.error_message = error_message
        self.custom_behavior = custom_behavior
        self.run_calls: list[dict[str, Any]] = []

    def run(
        self,
        config: Mt5DemoSoakConfig,
        broker: Mt5DemoBroker | None = None,
        ledger: EventLedger | None = None,
    ) -> Mt5DemoSoakResult:
        self.run_calls.append({"config": config, "broker": broker, "ledger": ledger})

        if self.custom_behavior is not None:
            return self.custom_behavior(config, broker, ledger)

        if ledger is not None and ledger.session_id:
            ledger.append(
                AuditEventType.SESSION_STARTED,
                occurred_at=NOW,
                component=AuditComponent.PAPER_SESSION,
                payload={"environment": "demo"},
            )
            for i in range(1, self.entries_completed + 1):
                corr = EventCorrelation(
                    client_order_id=f"cli_{i}",
                    broker_order_id=f"ord_{i}",
                    position_id=f"pos_{i}",
                    close_order_id=f"close_ord_{i}",
                )
                ledger.append(
                    AuditEventType.RISK_APPROVED,
                    occurred_at=NOW,
                    component=AuditComponent.RISK_ENGINE,
                    correlation=corr,
                    payload={"symbol": "EURUSD", "volume": 0.01},
                )
                ledger.append(
                    AuditEventType.ORDER_SUBMITTED,
                    occurred_at=NOW,
                    component=AuditComponent.ORDER_MANAGER,
                    correlation=corr,
                    payload={"symbol": "EURUSD", "volume": 0.01},
                )
                ledger.append(
                    AuditEventType.POSITION_OPENED,
                    occurred_at=NOW,
                    component=AuditComponent.BROKER_ADAPTER,
                    correlation=corr,
                    payload={"symbol": "EURUSD", "position_id": f"pos_{i}"},
                )
                ledger.append(
                    AuditEventType.POSITION_CLOSED,
                    occurred_at=NOW,
                    component=AuditComponent.BROKER_ADAPTER,
                    correlation=corr,
                    payload={
                        "symbol": "EURUSD",
                        "position_id": f"pos_{i}",
                        "close_order_id": f"close_ord_{i}",
                        "close_deal_id": f"close_deal_{i}",
                        "exit_reason": "MANUAL",
                        "realized_pnl": 0.50,
                    },
                )
            ledger.append(
                AuditEventType.SESSION_STOPPED,
                occurred_at=NOW,
                component=AuditComponent.PAPER_SESSION,
                payload={"reason": self.stop_reason},
            )

        return Mt5DemoSoakResult(
            status=self.status,
            stop_reason=self.stop_reason,
            entries_completed=self.entries_completed,
            duration_seconds=self.duration_seconds,
            active_position_id=None,
            error_message=self.error_message,
        )


def test_gate_b2_preserves_exact_gate_b1_soak_policy(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "success"
    assert len(mock_runner.run_calls) == 5

    for call in mock_runner.run_calls:
        soak_cfg: Mt5DemoSoakConfig = call["config"]
        assert soak_cfg.max_loss_usd == 1.0
        assert soak_cfg.max_entries == 2
        assert soak_cfg.max_duration_seconds == 300.0
        assert soak_cfg.drain_timeout_seconds == 300.0
        assert soak_cfg.max_quote_age_seconds == 5.0
        assert soak_cfg.poll_interval_seconds == 1.0
        assert soak_cfg.cooldown_seconds == 30.0


def test_gate_b2_invokes_preflight_before_each_attempted_run(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    preflight_calls = 0

    class TrackingPreflight:
        def run(self, quote: str | None = None) -> Mt5PreflightResult:
            nonlocal preflight_calls
            preflight_calls += 1
            return Mt5PreflightResult(
                environment="demo",
                broker="Pepperstone",
                terminal_version="5.0",
                account="12345678",
                server="Pepperstone-Demo",
                company="Pepperstone Group Limited",
                currency="USD",
                hedging_enabled=True,
                account_trading_enabled=True,
                expert_trading_enabled=True,
                terminal_trading_enabled=True,
            )

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=TrackingPreflight(),  # type: ignore[arg-type]
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "success"
    assert preflight_calls == 5
    assert len(mock_runner.run_calls) == 5


def test_gate_b2_preflight_failure_halts_run_and_later_runs(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    call_count = 0

    class FailingOnRun3Preflight:
        def run(self, quote: str | None = None) -> Mt5PreflightResult:
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise RuntimeError("mt5_terminal_unavailable")
            return Mt5PreflightResult(
                environment="demo",
                broker="Pepperstone",
                terminal_version="5.0",
                account="12345678",
                server="Pepperstone-Demo",
                company="Pepperstone Group Limited",
                currency="USD",
                hedging_enabled=True,
                account_trading_enabled=True,
                expert_trading_enabled=True,
                terminal_trading_enabled=True,
            )

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=FailingOnRun3Preflight(),  # type: ignore[arg-type]
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "failed"
    assert batch_res.runs_completed == 3
    assert batch_res.runs_passed == 2
    assert "preflight_failed_run_3" in (batch_res.failure_reason or "")
    assert len(mock_runner.run_calls) == 2


@pytest.mark.parametrize(
    ("overrides", "expected_err"),
    [
        ({"account": "88888888"}, "preflight_account_mismatch"),
        ({"server": "Other-Server"}, "preflight_server_mismatch"),
        ({"environment": "real"}, "preflight_non_demo_environment"),
        ({"expert_trading_enabled": False}, "preflight_trading_disabled"),
        ({"hedging_enabled": False}, "preflight_trading_disabled"),
    ],
)
def test_gate_b2_preflight_identity_drift_fails_closed(
    tmp_path: Path, overrides: dict[str, Any], expected_err: str
) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    class DriftPreflight:
        def run(self, quote: str | None = None) -> Mt5PreflightResult:
            defaults = {
                "environment": "demo",
                "broker": "Pepperstone",
                "terminal_version": "5.0",
                "account": "12345678",
                "server": "Pepperstone-Demo",
                "company": "Pepperstone Group Limited",
                "currency": "USD",
                "hedging_enabled": True,
                "account_trading_enabled": True,
                "expert_trading_enabled": True,
                "terminal_trading_enabled": True,
            }
            defaults.update(overrides)
            return Mt5PreflightResult(**defaults)

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner()
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=DriftPreflight(),  # type: ignore[arg-type]
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "failed"
    assert expected_err in (batch_res.failure_reason or "")
    assert len(mock_runner.run_calls) == 0


def test_gate_b2_final_exposure_query_error_fails_batch(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    original_positions_get = api.positions_get
    call_count = 0

    def query_with_final_fail(**query: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count > 10:
            raise RuntimeError("mt5_final_query_connection_lost")
        return original_positions_get(**query)

    api.positions_get = query_with_final_fail  # type: ignore[method-assign]

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "failed"
    assert "final_exposure_check_failed" in (batch_res.failure_reason or "")
    assert batch_res.runs_passed == 5


def test_gate_b2_final_exposure_residual_position_fails_batch(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    original_positions_get = api.positions_get
    call_count = 0

    def query_with_residual_position(**query: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count > 10:
            return (
                SimpleNamespace(
                    ticket=9999,
                    symbol="EURUSD",
                    volume=0.01,
                    type=0,
                    magic=None,
                    comment="",
                ),
            )
        return original_positions_get(**query)

    api.positions_get = query_with_residual_position  # type: ignore[method-assign]

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "failed"
    assert "final_exposure_not_flat" in (batch_res.failure_reason or "")
    assert batch_res.runs_passed == 5
    assert batch_res.account_exposure_at_end is not None
    assert len(batch_res.account_exposure_at_end.open_positions) == 1


def test_gate_b2_final_exposure_residual_pending_order_fails_batch(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    original_orders_get = api.orders_get
    call_count = 0

    def query_with_residual_order(**query: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count > 10:
            return (
                SimpleNamespace(
                    ticket=7777,
                    symbol="EURUSD",
                    volume_initial=0.01,
                    type=0,
                    magic=None,
                    comment="",
                ),
            )
        return original_orders_get(**query)

    api.orders_get = query_with_residual_order  # type: ignore[method-assign]

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "failed"
    assert "final_exposure_not_flat" in (batch_res.failure_reason or "")
    assert batch_res.runs_passed == 5
    assert batch_res.account_exposure_at_end is not None
    assert len(batch_res.account_exposure_at_end.pending_orders) == 1


def test_validate_run_audit_accepts_exact_client_order_id_correlation(tmp_path: Path) -> None:
    db_path = tmp_path / "exact_corr.db"
    session_id = "test_exact_corr"

    store = SQLiteEventStore(db_path, session_id)
    ledger = EventLedger(session_id, durable_store=store)

    ledger.append(
        AuditEventType.SESSION_STARTED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    corr = EventCorrelation(
        client_order_id="client_exact_1",
        broker_order_id="ord_1",
        position_id="pos_1",
        close_order_id="close_ord_1",
    )
    ledger.append(
        AuditEventType.RISK_APPROVED,
        occurred_at=NOW,
        component=AuditComponent.RISK_ENGINE,
        correlation=corr,
    )
    ledger.append(
        AuditEventType.ORDER_SUBMITTED,
        occurred_at=NOW,
        component=AuditComponent.ORDER_MANAGER,
        correlation=corr,
    )
    ledger.append(
        AuditEventType.POSITION_OPENED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr,
        payload={"symbol": "EURUSD", "position_id": "pos_1"},
    )
    ledger.append(
        AuditEventType.POSITION_CLOSED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr,
        payload={
            "symbol": "EURUSD",
            "position_id": "pos_1",
            "close_order_id": "close_ord_1",
            "close_deal_id": "close_deal_1",
            "exit_reason": "MANUAL",
            "realized_pnl": 0.50,
        },
    )
    ledger.append(
        AuditEventType.SESSION_STOPPED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    store.close()

    pnl = validate_run_audit(db_path, session_id, 1)
    assert pnl == pytest.approx(0.50)


def test_validate_run_audit_rejects_mismatched_client_order_id_correlation(tmp_path: Path) -> None:
    db_path = tmp_path / "mismatched_client.db"
    session_id = "test_mismatched_client"

    store = SQLiteEventStore(db_path, session_id)
    ledger = EventLedger(session_id, durable_store=store)

    ledger.append(
        AuditEventType.SESSION_STARTED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    ledger.append(
        AuditEventType.RISK_APPROVED,
        occurred_at=NOW,
        component=AuditComponent.RISK_ENGINE,
        correlation=EventCorrelation(client_order_id="client_A"),
    )
    ledger.append(
        AuditEventType.ORDER_SUBMITTED,
        occurred_at=NOW,
        component=AuditComponent.ORDER_MANAGER,
        correlation=EventCorrelation(client_order_id="client_B"),
    )
    ledger.append(
        AuditEventType.SESSION_STOPPED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    store.close()

    with pytest.raises(
        GateB2AuditValidationError, match="order_submitted_without_matching_risk_approval"
    ):
        validate_run_audit(db_path, session_id, 0)


def test_validate_run_audit_rejects_mismatched_position_identities(tmp_path: Path) -> None:
    db_path = tmp_path / "mismatched_pos_ids.db"
    session_id = "test_pos_id_mismatch"

    store = SQLiteEventStore(db_path, session_id)
    ledger = EventLedger(session_id, durable_store=store)

    ledger.append(
        AuditEventType.SESSION_STARTED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    corr_a = EventCorrelation(client_order_id="c1", position_id="pos_1")
    ledger.append(
        AuditEventType.RISK_APPROVED,
        occurred_at=NOW,
        component=AuditComponent.RISK_ENGINE,
        correlation=corr_a,
    )
    ledger.append(
        AuditEventType.ORDER_SUBMITTED,
        occurred_at=NOW,
        component=AuditComponent.ORDER_MANAGER,
        correlation=corr_a,
    )
    ledger.append(
        AuditEventType.POSITION_OPENED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr_a,
        payload={"position_id": "pos_1"},
    )
    corr_b = EventCorrelation(client_order_id="c1", position_id="pos_2")
    ledger.append(
        AuditEventType.POSITION_CLOSED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr_b,
        payload={
            "position_id": "pos_2",
            "close_order_id": "close_1",
            "close_deal_id": "deal_1",
            "exit_reason": "MANUAL",
            "realized_pnl": 0.50,
        },
    )
    ledger.append(
        AuditEventType.SESSION_STOPPED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    store.close()

    with pytest.raises(GateB2AuditValidationError, match="close_without_matching_open"):
        validate_run_audit(db_path, session_id, 1)


def test_validate_run_audit_rejects_duplicate_position_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "dup_close.db"
    session_id = "test_dup_close"

    store = SQLiteEventStore(db_path, session_id)
    ledger = EventLedger(session_id, durable_store=store)

    ledger.append(
        AuditEventType.SESSION_STARTED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    corr = EventCorrelation(client_order_id="c1", position_id="pos_1")
    ledger.append(
        AuditEventType.RISK_APPROVED,
        occurred_at=NOW,
        component=AuditComponent.RISK_ENGINE,
        correlation=corr,
    )
    ledger.append(
        AuditEventType.ORDER_SUBMITTED,
        occurred_at=NOW,
        component=AuditComponent.ORDER_MANAGER,
        correlation=corr,
    )
    ledger.append(
        AuditEventType.POSITION_OPENED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr,
        payload={"position_id": "pos_1"},
    )
    ledger.append(
        AuditEventType.POSITION_CLOSED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr,
        payload={
            "position_id": "pos_1",
            "close_order_id": "close_1",
            "close_deal_id": "deal_1",
            "exit_reason": "MANUAL",
            "realized_pnl": 0.50,
        },
    )
    ledger.append(
        AuditEventType.POSITION_CLOSED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr,
        payload={
            "position_id": "pos_1",
            "close_order_id": "close_2",
            "close_deal_id": "deal_2",
            "exit_reason": "MANUAL",
            "realized_pnl": 0.50,
        },
    )
    ledger.append(
        AuditEventType.SESSION_STOPPED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    store.close()

    with pytest.raises(GateB2AuditValidationError, match="duplicate_position_closed"):
        validate_run_audit(db_path, session_id, 1)


def test_validate_run_audit_rejects_duplicate_position_opened(tmp_path: Path) -> None:
    db_path = tmp_path / "dup_open.db"
    session_id = "test_dup_open"

    store = SQLiteEventStore(db_path, session_id)
    ledger = EventLedger(session_id, durable_store=store)

    ledger.append(
        AuditEventType.SESSION_STARTED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    corr = EventCorrelation(client_order_id="c1", position_id="pos_1")
    ledger.append(
        AuditEventType.RISK_APPROVED,
        occurred_at=NOW,
        component=AuditComponent.RISK_ENGINE,
        correlation=corr,
    )
    ledger.append(
        AuditEventType.ORDER_SUBMITTED,
        occurred_at=NOW,
        component=AuditComponent.ORDER_MANAGER,
        correlation=corr,
    )
    ledger.append(
        AuditEventType.POSITION_OPENED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr,
        payload={"position_id": "pos_1"},
    )
    ledger.append(
        AuditEventType.POSITION_OPENED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr,
        payload={"position_id": "pos_1"},
    )
    ledger.append(
        AuditEventType.SESSION_STOPPED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    store.close()

    with pytest.raises(GateB2AuditValidationError, match="duplicate_position_opened"):
        validate_run_audit(db_path, session_id, 0)


def test_validate_run_audit_rejects_close_without_open(tmp_path: Path) -> None:
    db_path = tmp_path / "close_no_open.db"
    session_id = "test_close_no_open"

    store = SQLiteEventStore(db_path, session_id)
    ledger = EventLedger(session_id, durable_store=store)

    ledger.append(
        AuditEventType.SESSION_STARTED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    corr = EventCorrelation(client_order_id="c1", position_id="pos_unknown")
    ledger.append(
        AuditEventType.POSITION_CLOSED,
        occurred_at=NOW,
        component=AuditComponent.BROKER_ADAPTER,
        correlation=corr,
        payload={
            "position_id": "pos_unknown",
            "close_order_id": "close_1",
            "close_deal_id": "deal_1",
            "exit_reason": "MANUAL",
            "realized_pnl": 0.50,
        },
    )
    ledger.append(
        AuditEventType.SESSION_STOPPED,
        occurred_at=NOW,
        component=AuditComponent.PAPER_SESSION,
    )
    store.close()

    with pytest.raises(GateB2AuditValidationError, match="close_without_matching_open"):
        validate_run_audit(db_path, session_id, 1)


def test_gate_b2_company_switch_between_runs_fails_closed(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    call_count = 0

    class CompanySwitchPreflight:
        def run(self, quote: str | None = None) -> Mt5PreflightResult:
            nonlocal call_count
            call_count += 1
            company = "Pepperstone Group Limited" if call_count < 3 else "Different Broker Company"
            return Mt5PreflightResult(
                environment="demo",
                broker="Pepperstone",
                terminal_version="5.0",
                account="12345678",
                server="Pepperstone-Demo",
                company=company,
                currency="USD",
                hedging_enabled=True,
                account_trading_enabled=True,
                expert_trading_enabled=True,
                terminal_trading_enabled=True,
            )

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=CompanySwitchPreflight(),  # type: ignore[arg-type]
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "failed"
    assert batch_res.runs_completed == 3
    assert batch_res.runs_passed == 2
    assert "preflight_company_mismatch_run_3" in (batch_res.failure_reason or "")


def test_gate_b2_five_sequential_successful_runs(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "success"
    assert batch_res.runs_completed == 5
    assert batch_res.runs_passed == 5
    assert batch_res.failure_reason is None
    assert len(batch_res.runs) == 5
    assert batch_res.total_realized_pnl_usd == pytest.approx(5.0)

    audit_paths = [r.audit_db_path for r in batch_res.runs]
    session_ids = [r.session_id for r in batch_res.runs]
    assert len(set(audit_paths)) == 5
    assert len(set(session_ids)) == 5
    for p in audit_paths:
        assert p.exists()


def test_gate_b2_pre_run_residual_position_halt(tmp_path: Path) -> None:
    api = FakeMt5Api()
    api.positions = [
        SimpleNamespace(
            ticket=9001,
            symbol="EURUSD",
            volume=0.01,
            type=0,
            magic=None,
            comment="",
        )
    ]
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner()
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "failed"
    assert "pre_run_exposure_not_flat" in (batch_res.failure_reason or "")
    assert len(mock_runner.run_calls) == 0


def test_gate_b2_run_2_failure_runs_3_to_5_never_start(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    call_count = 0

    def behavior(cfg: Any, b: Any, ledger_obj: Any) -> Mt5DemoSoakResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            ledger_obj.append(
                AuditEventType.SESSION_STARTED,
                occurred_at=NOW,
                component=AuditComponent.PAPER_SESSION,
                payload={"environment": "demo"},
            )
            ledger_obj.append(
                AuditEventType.SESSION_STOPPED,
                occurred_at=NOW,
                component=AuditComponent.PAPER_SESSION,
                payload={"reason": "completed"},
            )
            return Mt5DemoSoakResult(
                status="completed",
                stop_reason="max_entries_reached",
                entries_completed=0,
                duration_seconds=5.0,
            )
        else:
            return Mt5DemoSoakResult(
                status="failed",
                stop_reason="risk_limit_exceeded",
                entries_completed=0,
                duration_seconds=2.0,
                error_message="daily loss exceeded",
            )

    mock_runner = MockSoakRunner(custom_behavior=behavior)
    config = _make_valid_config(tmp_path)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "failed"
    assert batch_res.runs_completed == 2
    assert batch_res.runs_passed == 1
    assert call_count == 2
    assert "soak_run_2_failed" in (batch_res.failure_reason or "")


def test_gate_b2_keyboard_interrupt_halt_batch(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    def interrupt_behavior(cfg: Any, b: Any, ledger_obj: Any) -> Mt5DemoSoakResult:
        raise KeyboardInterrupt()

    mock_runner = MockSoakRunner(custom_behavior=interrupt_behavior)
    config = _make_valid_config(tmp_path)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "aborted"
    assert batch_res.runs_completed == 1
    assert batch_res.runs_passed == 0
    assert batch_res.failure_reason == "interrupted_by_user"


def test_gate_b2_exact_confirmation_text_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid_confirmation"):
        _make_valid_config(tmp_path, confirm_text="WRONG_CONFIRM")


def test_gate_b2_no_direct_broker_mutation_from_orchestrator(tmp_path: Path) -> None:
    api = FakeMt5Api()
    broker = Mt5DemoBroker(api=api)
    broker.connect()

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=0)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,
        config=config,
        preflight=Mt5DemoPreflight(api=api),
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    order_send_calls_before = [c for c in api.calls if getattr(c, "action", None) == "order_send"]
    orchestrator.run_batch()
    order_send_calls_after = [c for c in api.calls if getattr(c, "action", None) == "order_send"]

    assert len(order_send_calls_before) == len(order_send_calls_after) == 0


def test_gate_b2_operates_with_broker_without_api_attribute(tmp_path: Path) -> None:
    class ApiLessBroker:
        def get_account_exposure(self) -> Mt5AccountExposure:
            return Mt5AccountExposure(
                account_id="12345678",
                server="Pepperstone-Demo",
                company="Pepperstone Group Limited",
                environment="demo",
                open_positions=(),
                pending_orders=(),
            )

    class FixedPreflight:
        def run(self, quote: str | None = None) -> Mt5PreflightResult:
            return Mt5PreflightResult(
                environment="demo",
                broker="Pepperstone",
                terminal_version="5.0",
                account="12345678",
                server="Pepperstone-Demo",
                company="Pepperstone Group Limited",
                currency="USD",
                hedging_enabled=True,
                account_trading_enabled=True,
                expert_trading_enabled=True,
                terminal_trading_enabled=True,
            )

    broker = ApiLessBroker()
    assert not hasattr(broker, "api")
    assert not hasattr(broker, "_api")

    config = _make_valid_config(tmp_path)
    mock_runner = MockSoakRunner(entries_completed=2)
    orchestrator = Mt5DemoGateB2Orchestrator(
        broker=broker,  # type: ignore[arg-type]
        config=config,
        preflight=FixedPreflight(),  # type: ignore[arg-type]
        runner_factory=lambda: mock_runner,
        sleep_fn=lambda _: None,
    )

    batch_res = orchestrator.run_batch()
    assert batch_res.status == "success"
    assert batch_res.runs_passed == 5


def test_gate_b2_source_has_no_broker_api_or_raw_mt5_reach_through() -> None:
    gate_b2_source_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "fxlab"
        / "execution"
        / "mt5_demo_gate_b2.py"
    )
    content = gate_b2_source_path.read_text(encoding="utf-8")
    assert "broker.api" not in content
    assert "self._broker.api" not in content
    assert 'getattr(broker, "api"' not in content
    assert "getattr(broker, 'api'" not in content
    assert "broker._api" not in content
    assert "import MetaTrader5" not in content
    assert "_load_mt5" not in content


def test_cli_demo_gate_b2_connects_broker_before_first_exposure_and_disconnects_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = FakeMt5Api()
    monkeypatch.setitem(sys.modules, "MetaTrader5", api)

    def fake_soak_run(
        self: Any,
        config: Mt5DemoSoakConfig,
        broker: Mt5DemoBroker | None = None,
        ledger: EventLedger | None = None,
    ) -> Mt5DemoSoakResult:
        assert broker is not None
        assert broker._connected is True
        if ledger is not None and ledger.session_id:
            ledger.append(
                AuditEventType.SESSION_STARTED,
                occurred_at=NOW,
                component=AuditComponent.PAPER_SESSION,
                payload={"environment": "demo"},
            )
            ledger.append(
                AuditEventType.SESSION_STOPPED,
                occurred_at=NOW,
                component=AuditComponent.PAPER_SESSION,
                payload={"reason": "completed"},
            )
        return Mt5DemoSoakResult(
            status="completed",
            stop_reason="max_entries_reached",
            entries_completed=0,
            duration_seconds=1.0,
        )

    monkeypatch.setattr(
        "fxlab.execution.mt5_demo_soak.Mt5DemoSoakRunner.run",
        fake_soak_run,
    )

    cli_runner = CliRunner()
    result = cli_runner.invoke(
        app,
        [
            "mt5",
            "demo-gate-b2",
            "--confirm",
            GATE_B2_CONFIRM_TEXT,
            "--audit-db-dir",
            str(tmp_path / "audits"),
            "--account-id",
            "12345678",
            "--account-server",
            "Pepperstone-Demo",
        ],
    )

    assert result.exit_code == 0
    assert "initialize" in api.calls
    assert "shutdown" in api.calls


def test_cli_demo_gate_b2_connection_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = FakeMt5Api()
    api.initialize_result = False
    monkeypatch.setitem(sys.modules, "MetaTrader5", api)

    cli_runner = CliRunner()
    result = cli_runner.invoke(
        app,
        [
            "mt5",
            "demo-gate-b2",
            "--confirm",
            GATE_B2_CONFIRM_TEXT,
            "--audit-db-dir",
            str(tmp_path / "audits"),
            "--account-id",
            "12345678",
            "--account-server",
            "Pepperstone-Demo",
        ],
    )

    assert result.exit_code == 1
    assert "MT5 demo Gate B2 failed" in result.output
    assert "mt5_initialize_failed" in result.output


def test_cli_demo_gate_b2_no_order_mutation_during_connection_and_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = FakeMt5Api()
    monkeypatch.setitem(sys.modules, "MetaTrader5", api)

    def fake_soak_run(
        self: Any,
        config: Mt5DemoSoakConfig,
        broker: Mt5DemoBroker | None = None,
        ledger: EventLedger | None = None,
    ) -> Mt5DemoSoakResult:
        if ledger is not None and ledger.session_id:
            ledger.append(
                AuditEventType.SESSION_STARTED,
                occurred_at=NOW,
                component=AuditComponent.PAPER_SESSION,
                payload={"environment": "demo"},
            )
            ledger.append(
                AuditEventType.SESSION_STOPPED,
                occurred_at=NOW,
                component=AuditComponent.PAPER_SESSION,
                payload={"reason": "completed"},
            )
        return Mt5DemoSoakResult(
            status="completed",
            stop_reason="max_entries_reached",
            entries_completed=0,
            duration_seconds=1.0,
        )

    monkeypatch.setattr(
        "fxlab.execution.mt5_demo_soak.Mt5DemoSoakRunner.run",
        fake_soak_run,
    )

    cli_runner = CliRunner()
    result = cli_runner.invoke(
        app,
        [
            "mt5",
            "demo-gate-b2",
            "--confirm",
            GATE_B2_CONFIRM_TEXT,
            "--audit-db-dir",
            str(tmp_path / "audits"),
            "--account-id",
            "12345678",
            "--account-server",
            "Pepperstone-Demo",
        ],
    )

    assert result.exit_code == 0
    order_sends = [c for c in api.calls if getattr(c, "action", None) == "order_send"]
    assert len(order_sends) == 0
