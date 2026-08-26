"""Focused tests for Phase 12 broker capability declarations and preflight."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from fxlab.execution.broker import OrderRequest
from fxlab.execution.broker_capabilities import (
    BrokerCapability,
    BrokerCapabilityProvider,
    BrokerDescriptor,
    BrokerEnvironment,
    inspect_broker_capabilities,
    required_capabilities_for_order,
)
from fxlab.execution.event_ledger import AuditEventType, EventLedger
from fxlab.execution.order_manager import ExecutionIntent, ExecutionResultKind, OrderManager
from fxlab.execution.paper_broker import PaperBroker
from fxlab.execution.signal_engine import SignalEvent
from test_order_manager import FakeBroker, RiskSpy, decision

PAPER_CAPABILITIES = frozenset(
    {
        BrokerCapability.MARKET_ORDERS,
        BrokerCapability.NATIVE_SL_TP,
        BrokerCapability.HEDGING,
        BrokerCapability.CLIENT_ORDER_IDS,
    }
)


def descriptor(
    capabilities: frozenset[BrokerCapability] = PAPER_CAPABILITIES,
    *,
    broker_id: str = "test-broker",
    version: str = "1",
    environment: BrokerEnvironment = BrokerEnvironment.PAPER,
    deterministic: bool = True,
) -> BrokerDescriptor:
    return BrokerDescriptor(broker_id, version, environment, capabilities, deterministic)


def test_stable_enum_values() -> None:
    assert [item.value for item in BrokerCapability] == [
        "market_orders",
        "limit_orders",
        "stop_orders",
        "native_sl_tp",
        "hedging",
        "netting",
        "partial_close",
        "client_order_ids",
    ]
    assert [item.value for item in BrokerEnvironment] == ["paper", "demo", "live"]


def test_descriptor_is_frozen_and_capabilities_are_immutable() -> None:
    value = descriptor()
    assert isinstance(value.capabilities, frozenset)
    with pytest.raises(FrozenInstanceError):
        value.broker_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("broker_id", "version"),
    [("", "1"), ("bad broker", "1"), ("broker", ""), ("broker", "https://secret")],
)
def test_descriptor_rejects_unsafe_identity(broker_id: str, version: str) -> None:
    with pytest.raises(ValueError):
        descriptor(broker_id=broker_id, version=version)


def test_descriptor_requires_exactly_one_position_mode() -> None:
    without = PAPER_CAPABILITIES - {BrokerCapability.HEDGING}
    both = PAPER_CAPABILITIES | {BrokerCapability.NETTING}
    with pytest.raises(ValueError, match="exactly one"):
        descriptor(without)
    with pytest.raises(ValueError, match="exactly one"):
        descriptor(both)


def test_fingerprint_is_stable_and_set_order_independent() -> None:
    first = descriptor(frozenset(PAPER_CAPABILITIES))
    second = descriptor(frozenset(reversed(tuple(PAPER_CAPABILITIES))))
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_runtime_protocol_requires_explicit_descriptor() -> None:
    assert isinstance(PaperBroker(), BrokerCapabilityProvider)
    assert not isinstance(object(), BrokerCapabilityProvider)


@pytest.mark.parametrize(
    ("order_type", "expected"),
    [
        ("market", BrokerCapability.MARKET_ORDERS),
        ("limit", BrokerCapability.LIMIT_ORDERS),
        ("stop", BrokerCapability.STOP_ORDERS),
    ],
)
def test_order_requirements_are_derived_purely(order_type, expected) -> None:
    request = OrderRequest("EURUSD", 1, 0.1, order_type, "client-1", sl_price=1.0)
    required = required_capabilities_for_order(request)
    assert expected in required
    assert BrokerCapability.NATIVE_SL_TP in required
    assert BrokerCapability.CLIENT_ORDER_IDS in required


def test_order_without_protective_prices_does_not_require_native_sl_tp() -> None:
    request = OrderRequest("EURUSD", 1, 0.1, "market", "client-1")
    assert BrokerCapability.NATIVE_SL_TP not in required_capabilities_for_order(request)


def test_paper_broker_declares_exact_stable_capabilities() -> None:
    broker = PaperBroker()
    initial = broker.broker_descriptor
    assert initial.capabilities == PAPER_CAPABILITIES
    assert initial.environment is BrokerEnvironment.PAPER
    assert initial.deterministic
    broker.connect()
    broker.disconnect()
    assert broker.broker_descriptor is initial


class CapabilityBroker(FakeBroker):
    def __init__(self, broker_descriptor: object = None, *, raises: bool = False) -> None:
        super().__init__()
        self._descriptor = broker_descriptor
        self.raises = raises
        self.quote_calls = 0
        self.account_calls = 0

    @property
    def broker_descriptor(self):
        if self.raises:
            raise RuntimeError("descriptor unavailable")
        return self._descriptor

    def get_latest_tick(self, symbol):
        self.quote_calls += 1
        return super().get_latest_tick(symbol)

    def get_account_info(self):
        self.account_calls += 1
        return super().get_account_info()


def _intent() -> ExecutionIntent:
    event = SignalEvent(
        "setup",
        "EURUSD",
        "M5",
        1,
        datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        1,
    )
    return ExecutionIntent(event, 1.09, 1.12)


@pytest.mark.parametrize(
    ("broker", "reason"),
    [
        (CapabilityBroker(), "broker_capabilities_invalid"),
        (CapabilityBroker(raises=True), "broker_capabilities_unavailable"),
        (
            CapabilityBroker(descriptor(PAPER_CAPABILITIES - {BrokerCapability.MARKET_ORDERS})),
            "broker_capability_unsupported",
        ),
        (
            CapabilityBroker(descriptor(PAPER_CAPABILITIES - {BrokerCapability.CLIENT_ORDER_IDS})),
            "broker_capability_unsupported",
        ),
        (
            CapabilityBroker(descriptor(PAPER_CAPABILITIES - {BrokerCapability.NATIVE_SL_TP})),
            "broker_capability_unsupported",
        ),
        (
            CapabilityBroker(
                descriptor(
                    (PAPER_CAPABILITIES - {BrokerCapability.HEDGING}) | {BrokerCapability.NETTING}
                )
            ),
            "broker_capability_unsupported",
        ),
    ],
)
def test_order_manager_fails_before_quote_account_risk_or_submit(broker, reason) -> None:
    risk = RiskSpy(decision())
    ledger = EventLedger("capability-test")
    manager = OrderManager(broker, risk, ledger)  # type: ignore[arg-type]
    result = manager.submit(_intent(), current_time=datetime(2026, 8, 25, 10, 5, tzinfo=UTC))
    assert result.kind is ExecutionResultKind.EXECUTION_REJECTED
    assert result.reason == reason
    assert broker.quote_calls == broker.account_calls == 0
    assert not risk.calls and not risk.released
    assert not broker.submitted
    assert ledger.last_event().event_type is AuditEventType.BROKER_CAPABILITY_REJECTED


def test_order_manager_rejects_missing_capability_provider_before_risk() -> None:
    risk = RiskSpy(decision())
    manager = OrderManager(object(), risk, EventLedger("missing-provider"))  # type: ignore[arg-type]
    result = manager.submit(
        _intent(), current_time=datetime(2026, 8, 25, 10, 5, tzinfo=UTC)
    )
    assert result.reason == "broker_capabilities_unavailable"
    assert not risk.calls and not risk.released


def test_compatibility_check_supports_environment_and_determinism() -> None:
    broker = CapabilityBroker(descriptor(environment=BrokerEnvironment.DEMO, deterministic=False))
    check = inspect_broker_capabilities(
        broker,
        PAPER_CAPABILITIES,
        environment=BrokerEnvironment.PAPER,
        deterministic=True,
        require_hedging=True,
    )
    assert not check.compatible
    assert check.reason == "broker_capability_unsupported"
