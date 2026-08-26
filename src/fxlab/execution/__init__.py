"""Public execution-layer contracts."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

__all__ = [
    "AuditComponent",
    "AuditEvent",
    "AuditEventType",
    "AuditLedgerError",
    "EventCorrelation",
    "EventLedger",
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
        from . import event_ledger, order_manager, paper_broker, paper_session

        for module in (event_ledger, order_manager, paper_broker, paper_session):
            if hasattr(module, name):
                return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
