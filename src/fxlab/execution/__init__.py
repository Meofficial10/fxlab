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
    from .recovery import (
        RecoveryResult,
        RecoveryState,
        UnsafeCheckpointError,
        create_checkpoint,
        recover,
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
    "recover",
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
            recovery,
        )

        for module in (
            durable_event_store,
            event_ledger,
            order_manager,
            paper_broker,
            paper_session,
            recovery,
        ):
            if hasattr(module, name):
                return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
