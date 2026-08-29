from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Client
from pathlib import Path

import pytest

from fxlab.execution.durable_event_store import DurableStoreError, SQLiteEventStore
from fxlab.execution.valuation import (
    ConversionQuote,
    FxInstrumentCatalog,
    FxValuationEngine,
    InstrumentSpec,
    ValuationFailure,
)
from fxlab.operations.control import LocalControlServer
from fxlab.operations.security import FileSecretResolver
from fxlab.operations.service import InstanceLock, OperationalConfig
from fxlab.readiness.audit import (
    FAULT_INJECTION_EVIDENCE,
    FaultEvidenceClassification,
    build_current_readiness_report,
)
from fxlab.readiness.model import ReadinessStatus

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


def _config(tmp_path: Path) -> OperationalConfig:
    state = (tmp_path / "state").resolve()
    state.mkdir()
    secret = (tmp_path / "control.secret").resolve()
    secret.write_bytes(b"x" * 32)
    return OperationalConfig(
        1,
        state,
        "phase20-runtime",
        "phase20-operator",
        secret,
        "phase20-control",
        "phase20.jsonl",
    )


def test_sqlite_corruption_is_detected_without_fresh_fallback(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path, "phase20-session")
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE metadata SET value = '999' WHERE key = 'store_format_version'")

    with pytest.raises(DurableStoreError):
        SQLiteEventStore(path, "phase20-session")
    assert path.exists()


def test_physical_disk_full_remains_honestly_unverified() -> None:
    report = build_current_readiness_report(as_of=NOW)
    check = next(item for item in report.checks if item.check_id == "storage_production")

    assert check.status is ReadinessStatus.UNVERIFIED
    assert check.reason_code == "disk_full_backup_restore_unverified"


@pytest.mark.parametrize(
    ("observation_time", "reason"),
    [
        (NOW + timedelta(microseconds=1), "future_conversion_quote"),
        (NOW - timedelta(minutes=5, microseconds=1), "stale_conversion_quote"),
    ],
)
def test_future_and_stale_conversion_data_fail_closed(
    observation_time: datetime, reason: str
) -> None:
    catalog = FxInstrumentCatalog(
        (
            InstrumentSpec("USDJPY", "fx", "USD", "JPY", 0.01, 100_000, "1"),
        )
    )
    engine = FxValuationEngine(catalog, max_age=timedelta(minutes=5))
    quote = ConversionQuote("USDJPY", 150.0, 150.02, observation_time, "phase20-fake")

    with pytest.raises(ValuationFailure, match=reason):
        engine.pip_valuation("USDJPY", "USD", NOW, (quote,))


def test_secret_read_failure_prevents_credential_material_creation(tmp_path: Path) -> None:
    missing = (tmp_path / "missing.secret").resolve()

    with pytest.raises(ValueError, match="unavailable"):
        FileSecretResolver().resolve(missing)


def test_instance_lock_contention_fails_before_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "phase20.lock"
    first = InstanceLock(path, "first")
    second = InstanceLock(path, "second")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
        assert second._handle is None
    finally:
        first.release()


def test_stalled_authentication_does_not_prevent_bounded_control_shutdown(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    secret = FileSecretResolver().resolve(config.control_secret_file)
    server = LocalControlServer(config, secret, lambda request: pytest.fail(str(request)))
    server.start()
    stalled = Client(config.control_address, family=config.control_family, authkey=None)
    completed = threading.Event()
    closer = threading.Thread(target=lambda: (server.close(), completed.set()))
    try:
        closer.start()
        assert completed.wait(1.5)
    finally:
        stalled.close()
        closer.join(6)
        if not completed.is_set():
            server.close()


def test_approved_fault_matrix_has_named_evidence_or_honest_unverified_status() -> None:
    required = {
        "submission_before_acknowledgement",
        "fill_before_reflection",
        "close_accounting_interruption",
        "audit_failure",
        "checkpoint_failure",
        "sqlite_corruption",
        "disk_store_failure",
        "stale_future_corrupt_data",
        "provider_failure",
        "broker_timeout_uncertainty",
        "control_failure",
        "logging_failure",
        "secret_read_failure",
        "instance_lock_contention",
        "shutdown_during_serialized_cycle",
        "stalled_authentication",
    }
    by_id = {item.scenario_id: item for item in FAULT_INJECTION_EVIDENCE}

    assert set(by_id) == required
    assert all(
        item.evidence_reference.identifier.startswith("tests/") for item in by_id.values()
    )
    assert by_id["disk_store_failure"].status is ReadinessStatus.UNVERIFIED
    assert (
        by_id["disk_store_failure"].classification
        is FaultEvidenceClassification.UNVERIFIED
    )
    assert by_id["disk_store_failure"].reason_code == "physical_disk_full_not_validated"
    assert by_id["fill_before_reflection"].evidence_reference.canonical_text.endswith(
        "test_filled_order_repairs_exact_record_reflection_and_reservation"
    )
    assert by_id["stale_future_corrupt_data"].status is ReadinessStatus.UNVERIFIED
    assert by_id["shutdown_during_serialized_cycle"].status is ReadinessStatus.UNVERIFIED
    assert by_id["logging_failure"].evidence_reference.canonical_text.endswith(
        "test_runtime_logging_failure_requests_fail_closed_shutdown"
    )
    assert (
        by_id["logging_failure"].classification
        is FaultEvidenceClassification.PREEXISTING_TEST_EVIDENCE
    )
