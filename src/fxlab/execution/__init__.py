"""Public execution-layer contracts."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .order_manager import (
        ExecutionIntent,
        ExecutionResult,
        ExecutionResultKind,
        OrderManager,
        OrderRecord,
    )

__all__ = [
    "ExecutionIntent",
    "ExecutionResult",
    "ExecutionResultKind",
    "OrderManager",
    "OrderRecord",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import order_manager

        return getattr(order_manager, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
