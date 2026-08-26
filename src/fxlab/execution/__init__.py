"""Public execution-layer contracts."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

__all__ = [
    "AuditComponent",
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
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import (
            durable_event_store,
            event_ledger,
            order_manager,
            paper_broker,
            paper_session,
            reconciliation,
            recovery,
        )

        for module in (
            durable_event_store,
            event_ledger,
            order_manager,
            paper_broker,
            paper_session,
            reconciliation,
            recovery,
        ):
            if hasattr(module, name):
                return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
