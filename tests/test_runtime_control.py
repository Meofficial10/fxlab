"""Tests for the Phase 13 runtime safety-control contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from fxlab.execution.runtime_control import (
    RuntimeController,
    RuntimeControlReason,
    RuntimeState,
)

NOW = datetime(2026, 8, 25, 10, 5, tzinfo=UTC)


def test_runtime_contract_values_are_stable() -> None:
    assert [item.value for item in RuntimeState] == [
        "running",
        "paused",
        "kill_switched",
        "reconciliation_required",
        "failed",
        "stopping",
        "stopped",
    ]
    assert {item.value for item in RuntimeControlReason} == {
        "operator_paused",
        "emergency_stopped",
        "risk_kill_switch",
        "reconciliation_required",
        "audit_integrity_failed",
        "broker_incompatible",
        "broker_unavailable",
        "data_stale",
        "data_unavailable",
        "data_invalid",
        "runtime_failed",
        "shutdown_in_progress",
        "session_stopped",
    }


def test_runtime_status_is_frozen_and_start_enables_execution() -> None:
    controller = RuntimeController()
    stopped = controller.status()
    assert stopped.state is RuntimeState.STOPPED
    assert not stopped.execution_enabled
    assert not stopped.market_maintenance_enabled
    with pytest.raises(FrozenInstanceError):
        stopped.state = RuntimeState.RUNNING  # type: ignore[misc]

    result = controller.start()
    assert result.accepted and result.changed
    assert controller.status().state is RuntimeState.RUNNING
    assert controller.status().execution_enabled
    assert controller.status().reason is None


def test_pause_resume_tracks_strict_entry_watermark() -> None:
    controller = RuntimeController()
    controller.start()
    paused = controller.pause(NOW)
    assert paused.accepted and paused.changed
    status = controller.status()
    assert status.state is RuntimeState.PAUSED
    assert status.market_maintenance_enabled
    assert not status.execution_enabled

    resumed = controller.resume(NOW)
    assert resumed.accepted and resumed.changed
    assert controller.status().reason is None
    assert controller.status().entry_enable_watermark == NOW
    assert not controller.signal_is_eligible(NOW)
    assert controller.signal_is_eligible(
        datetime(2026, 8, 25, 10, 5, 0, 1, tzinfo=UTC)
    )


def test_effective_state_precedence_projects_external_truth() -> None:
    controller = RuntimeController()
    controller.start()
    controller.pause(NOW)
    assert controller.status(kill_switch_active=True).state is RuntimeState.KILL_SWITCHED
    assert (
        controller.status(kill_switch_active=True, failed_reason="runtime_failed").state
        is RuntimeState.FAILED
    )
    assert (
        controller.status(
            kill_switch_active=True,
            failed_reason="runtime_failed",
            reconciliation_required=True,
        ).state
        is RuntimeState.RECONCILIATION_REQUIRED
    )
    controller.request_stop()
    assert (
        controller.status(reconciliation_required=True).state is RuntimeState.STOPPING
    )
    controller.complete_stop()
    assert controller.status(reconciliation_required=True).state is RuntimeState.STOPPED


def test_resume_is_rejected_when_external_blocker_exists() -> None:
    controller = RuntimeController()
    controller.start()
    controller.pause(NOW)
    result = controller.resume(NOW, kill_switch_active=True)
    assert not result.accepted
    assert not result.changed
    assert controller.status(kill_switch_active=True).state is RuntimeState.KILL_SWITCHED


def test_idempotent_transitions_do_not_change_generation() -> None:
    controller = RuntimeController()
    controller.start()
    controller.pause(NOW)
    generation = controller.status().generation
    assert not controller.pause(NOW).changed
    assert controller.status().generation == generation
    controller.resume(NOW)
    generation = controller.status().generation
    assert not controller.resume(NOW).changed
    assert controller.status().generation == generation
    controller.request_stop()
    generation = controller.status().generation
    assert not controller.request_stop().changed
    assert controller.status().generation == generation


def test_distinct_health_pause_replaces_operator_pause_without_opening_gate() -> None:
    controller = RuntimeController()
    controller.start()
    controller.pause(NOW)
    changed = controller.pause(NOW, reason=RuntimeControlReason.BROKER_UNAVAILABLE)
    assert changed.changed
    assert controller.status().state is RuntimeState.PAUSED
    assert controller.status().reason is RuntimeControlReason.BROKER_UNAVAILABLE
    generation = controller.status().generation
    assert not controller.pause(
        NOW, reason=RuntimeControlReason.BROKER_UNAVAILABLE
    ).changed
    assert controller.status().generation == generation


def test_snapshot_restore_round_trip_and_invalid_restore_is_atomic() -> None:
    controller = RuntimeController()
    controller.start()
    controller.pause(NOW, reason=RuntimeControlReason.DATA_STALE)
    snapshot = controller.snapshot_state()

    restored = RuntimeController()
    restored.restore_state(snapshot)
    assert restored.snapshot_state() == snapshot

    before = restored.snapshot_state()
    malformed = dict(before)
    malformed["state"] = "unknown"
    with pytest.raises(ValueError):
        restored.restore_state(malformed)
    assert restored.snapshot_state() == before


def test_naive_watermarks_are_rejected() -> None:
    controller = RuntimeController()
    controller.start()
    with pytest.raises(ValueError, match="timezone-aware"):
        controller.pause(datetime(2026, 8, 25, 10, 5))
