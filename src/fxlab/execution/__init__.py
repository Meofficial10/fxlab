"""Public execution-layer contracts."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .broker_capabilities import (
        BrokerCapability,
        BrokerCapabilityProvider,
        BrokerCompatibility,
        BrokerDescriptor,
        BrokerEnvironment,
        inspect_broker_capabilities,
        required_capabilities_for_order,
    )
    from .durable_event_store import SQLiteEventStore, StoredCheckpoint
    from .event_ledger import (
        AuditComponent,
        AuditEvent,
        AuditEventType,
        AuditLedgerError,
        EventCorrelation,
        EventLedger,
    )
    from .order_manager import (
        ExecutionIntent,
        ExecutionResult,
        ExecutionResultKind,
        OrderManager,
        OrderRecord,
    )
    from .paper_broker import CloseReason, OrderCorrelation, PaperBroker, PositionClose
    from .paper_session import (
        CycleKind,
        ExecutionPolicy,
        HistoricalBarReplay,
        MarketContext,
        PaperCycleResult,
        PaperTradingSession,
    )
    from .reconciliation import (
        ReconciliationEngine,
        ReconciliationPlan,
        ReconciliationResult,
        ReconciliationStatus,
    )
    from .recovery import (
        RecoveryResult,
        RecoveryState,
        UnsafeCheckpointError,
        create_checkpoint,
        create_reconciliation_checkpoint,
        recover,
        validate_reconciliation_safe_point,
    )
    from .runtime_control import (
        RuntimeController,
        RuntimeControlReason,
        RuntimeControlResult,
        RuntimeState,
        RuntimeStatus,
    )

__all__ = [
    "AuditComponent",
    "BrokerCapability",
    "BrokerCapabilityProvider",
    "BrokerCompatibility",
    "BrokerDescriptor",
    "BrokerEnvironment",
    "AuditEvent",
    "AuditEventType",
    "AuditLedgerError",
    "EventCorrelation",
    "EventLedger",
    "SQLiteEventStore",
    "StoredCheckpoint",
    "RecoveryResult",
    "RecoveryState",
    "UnsafeCheckpointError",
    "create_checkpoint",
    "create_reconciliation_checkpoint",
    "recover",
    "validate_reconciliation_safe_point",
    "ReconciliationEngine",
    "ReconciliationPlan",
    "ReconciliationResult",
    "ReconciliationStatus",
    "ExecutionIntent",
    "ExecutionResult",
    "ExecutionResultKind",
    "OrderManager",
    "OrderRecord",
    "OrderCorrelation",
    "PaperBroker",
    "CloseReason",
    "PositionClose",
    "CycleKind",
    "ExecutionPolicy",
    "HistoricalBarReplay",
    "MarketContext",
    "PaperCycleResult",
    "PaperTradingSession",
    "RuntimeControlReason",
    "RuntimeControlResult",
    "RuntimeController",
    "RuntimeState",
    "RuntimeStatus",
    "inspect_broker_capabilities",
    "required_capabilities_for_order",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import (
            broker_capabilities,
            durable_event_store,
            event_ledger,
            order_manager,
            paper_broker,
            paper_session,
            reconciliation,
            recovery,
            runtime_control,
        )

        for module in (
            broker_capabilities,
            durable_event_store,
            event_ledger,
            order_manager,
            paper_broker,
            paper_session,
            reconciliation,
            recovery,
            runtime_control,
        ):
            if hasattr(module, name):
                return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
