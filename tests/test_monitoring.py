"""Focused contracts for read-only Phase 15 operational monitoring."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from fxlab.config import load_config
from fxlab.data.store import save_bars
from fxlab.execution.app import ReplayRequest, assemble_observation_replay
from fxlab.execution.event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
)
from fxlab.execution.monitoring import (
    AccountMonitoringView,
    AuditEventMonitoringView,
    MonitoringSource,
    OrderMonitoringView,
    monitoring_to_dict,
    project_audit_events,
)


def _request(tmp_path) -> ReplayRequest:
    index = pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC")
    close = np.array([1.1, 1.101], dtype="float64")
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.001,
            "low": close - 0.001,
            "close": close,
            "volume": np.ones(2, dtype="float64"),
        },
        index=index,
        dtype="float64",
    )
    bars.index.name = "ts_open"
    bars.attrs.update(symbol="EURUSD", timeframe="M5")
    save_bars(bars, tmp_path / "data", "EURUSD", "M5")
    return ReplayRequest(
        "monitor-session",
        tmp_path / "monitor.sqlite",
        tmp_path / "data",
        "EURUSD",
        "M5",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        True,
    )


def test_live_snapshot_is_frozen_allow_listed_and_side_effect_free(tmp_path) -> None:
    request = _request(tmp_path)
    app = assemble_observation_replay(request, load_config(), fresh=True)
    try:
        before_events = len(app.session.event_ledger.events())
        snapshot = app.session.monitoring_snapshot()
        assert snapshot.source is MonitoringSource.LIVE_RUNTIME
        assert snapshot.session_id == request.session_id
        assert snapshot.runtime.state == "stopped"
        assert snapshot.account.balance == 10.0
        assert snapshot.account.realized_pnl == 0.0
        assert snapshot.account.realized_pnl_basis == "paper_broker_accounting_projection"
        assert snapshot.account.margin_model == "unmodeled_paper_margin"
        assert snapshot.risk.reserved_exposure_by_symbol == ()
        assert snapshot.provider.provider_id == "local-parquet"
        assert snapshot.provider.canonical_symbols == ("EURUSD",)
        assert snapshot.broker.environment == "paper"
        assert snapshot.broker.connected is False
        assert len(app.session.event_ledger.events()) == before_events
        assert not app.session.broker.is_connected()
        with pytest.raises(FrozenInstanceError):
            snapshot.session_id = "changed"  # type: ignore[misc]
    finally:
        app.close()


def test_live_snapshot_tracks_runtime_replay_and_risk_without_mutation(tmp_path) -> None:
    request = _request(tmp_path)
    app = assemble_observation_replay(request, load_config(), fresh=True)
    try:
        app.session.start()
        app.session.poll_once()
        app.session.pause()
        before = app.session.risk_engine.snapshot_state()
        snapshot = app.session.monitoring_snapshot()
        assert snapshot.runtime.state == "paused"
        assert snapshot.runtime.execution_enabled is False
        assert snapshot.runtime.market_maintenance_enabled is True
        assert snapshot.provider.replay_cursor == 1
        assert snapshot.provider.last_timestamp == "2026-01-01T00:05:00+00:00"
        assert snapshot.risk.daily_trade_count == 0
        assert snapshot.risk.approved_order_count == 0
        assert app.session.risk_engine.snapshot_state() == before
    finally:
        app.session.stop()
        app.close()


def test_event_projection_filters_after_integrity_input_and_freezes_payload(tmp_path) -> None:
    request = _request(tmp_path)
    app = assemble_observation_replay(request, load_config(), fresh=True)
    try:
        ledger = app.session.event_ledger
        ledger.append(
            AuditEventType.RUNTIME_STATE_CHANGED,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            component=AuditComponent.PAPER_SESSION,
            correlation=EventCorrelation(client_order_id="client-1"),
            payload={"state": "paused", "nested": ["safe"]},
        )
        ledger.append(
            AuditEventType.DATA_PROVIDER_FAILED,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            component=AuditComponent.MARKET_DATA_PROVIDER,
            payload={"reason": "unavailable"},
        )
        views = project_audit_events(
            ledger.events(),
            component=AuditComponent.PAPER_SESSION,
            correlation_id="client-1",
            after_sequence=0,
            limit=1,
        )
        assert len(views) == 1
        assert views[0].event_type == "runtime_state_changed"
        assert views[0].client_order_id == "client-1"
        assert views[0].payload == (("nested", ("safe",)), ("state", "paused"))
        with pytest.raises(ValueError):
            project_audit_events(ledger.events(), limit=0)
        with pytest.raises(ValueError):
            project_audit_events(ledger.events(), after_sequence=-1)
    finally:
        app.close()


def test_explicit_json_projection_contains_no_runtime_objects(tmp_path) -> None:
    request = _request(tmp_path)
    app = assemble_observation_replay(request, load_config(), fresh=True)
    try:
        payload = monitoring_to_dict(app.session.monitoring_snapshot())
        assert payload["source"] == "live_runtime"
        assert payload["provider"]["provider_id"] == "local-parquet"
        text = repr(payload).lower()
        for forbidden in ("papertradingsession", "riskengine", "order_manager", "_lock"):
            assert forbidden not in text
    finally:
        app.close()


def test_monitoring_contract_rejects_nonfinite_values_and_malformed_ids() -> None:
    with pytest.raises(ValueError, match="finite"):
        AccountMonitoringView(
            float("nan"), 1.0, 0.0, 1.0, 0.0,
            "paper_broker_accounting_projection", 0.0, 0,
            "unmodeled_paper_margin",
        )
    with pytest.raises(ValueError, match="client_order_id"):
        OrderMonitoringView(
            None, " ", None, "EURUSD", 1, 0.1, "market", "filled",
            1.0, None, True,
        )


def test_event_monitoring_contract_rejects_sensitive_nested_keys() -> None:
    with pytest.raises(ValueError, match="sensitive"):
        AuditEventMonitoringView(
            1,
            "2026-01-01T00:00:00+00:00",
            "runtime_failure",
            "paper_session",
            None,
            None,
            None,
            None,
            None,
            (("nested", (("api_key", "not-allowed"),)),),
        )
