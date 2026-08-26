"""Immutable broker capability declarations and fail-closed compatibility checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .broker import OrderRequest

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SENSITIVE = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|authorization|credential|https?://)"
)
CAPABILITY_CONTRACT_VERSION = 1


class BrokerCapability(StrEnum):
    MARKET_ORDERS = "market_orders"
    LIMIT_ORDERS = "limit_orders"
    STOP_ORDERS = "stop_orders"
    NATIVE_SL_TP = "native_sl_tp"
    HEDGING = "hedging"
    NETTING = "netting"
    PARTIAL_CLOSE = "partial_close"
    CLIENT_ORDER_IDS = "client_order_ids"


class BrokerEnvironment(StrEnum):
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


def _safe_identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not (result := value.strip())
        or not _SAFE_ID.fullmatch(result)
        or _SENSITIVE.search(result)
    ):
        raise ValueError(f"{field_name} must be a safe non-empty identifier")
    return result


@dataclass(frozen=True)
class BrokerDescriptor:
    broker_id: str
    implementation_version: str
    environment: BrokerEnvironment
    capabilities: frozenset[BrokerCapability]
    deterministic: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "broker_id", _safe_identifier(self.broker_id, "broker_id"))
        object.__setattr__(
            self,
            "implementation_version",
            _safe_identifier(self.implementation_version, "implementation_version"),
        )
        if not isinstance(self.environment, BrokerEnvironment):
            raise ValueError("environment must be a BrokerEnvironment")
        try:
            capabilities = frozenset(self.capabilities)
        except TypeError as exc:
            raise ValueError("capabilities must contain BrokerCapability values") from exc
        if any(not isinstance(item, BrokerCapability) for item in capabilities):
            raise ValueError("capabilities must contain BrokerCapability values")
        modes = capabilities & {
            BrokerCapability.HEDGING,
            BrokerCapability.NETTING,
        }
        if len(modes) != 1:
            raise ValueError("descriptor must declare exactly one position mode")
        if not isinstance(self.deterministic, bool):
            raise ValueError("deterministic must be a bool")
        object.__setattr__(self, "capabilities", capabilities)

    @property
    def fingerprint(self) -> str:
        document = {
            "contract_version": CAPABILITY_CONTRACT_VERSION,
            "broker_id": self.broker_id,
            "implementation_version": self.implementation_version,
            "environment": self.environment.value,
            "capabilities": sorted(item.value for item in self.capabilities),
            "deterministic": self.deterministic,
        }
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def compatibility_snapshot(self) -> dict[str, object]:
        return {
            "contract_version": CAPABILITY_CONTRACT_VERSION,
            "broker_id": self.broker_id,
            "implementation_version": self.implementation_version,
            "environment": self.environment.value,
            "capabilities": sorted(item.value for item in self.capabilities),
            "position_mode": (
                BrokerCapability.HEDGING.value
                if BrokerCapability.HEDGING in self.capabilities
                else BrokerCapability.NETTING.value
            ),
            "deterministic": self.deterministic,
            "descriptor_fingerprint": self.fingerprint,
        }


@runtime_checkable
class BrokerCapabilityProvider(Protocol):
    @property
    def broker_descriptor(self) -> BrokerDescriptor: ...


@dataclass(frozen=True)
class BrokerCompatibility:
    compatible: bool
    reason: str
    required: tuple[BrokerCapability, ...]
    missing: tuple[BrokerCapability, ...]
    descriptor: BrokerDescriptor | None = None


def required_capabilities_for_order(order: OrderRequest) -> frozenset[BrokerCapability]:
    """Derive broker requirements without invoking or mutating a broker."""
    if not isinstance(order, OrderRequest):
        raise ValueError("order must be an OrderRequest")
    order_capability = {
        "market": BrokerCapability.MARKET_ORDERS,
        "limit": BrokerCapability.LIMIT_ORDERS,
        "stop": BrokerCapability.STOP_ORDERS,
    }[order.order_type]
    required = {order_capability, BrokerCapability.CLIENT_ORDER_IDS}
    if order.sl_price is not None or order.tp_price is not None:
        required.add(BrokerCapability.NATIVE_SL_TP)
    return frozenset(required)


def inspect_broker_capabilities(
    broker: object,
    required: frozenset[BrokerCapability],
    *,
    environment: BrokerEnvironment | None = None,
    deterministic: bool | None = None,
    require_hedging: bool = False,
) -> BrokerCompatibility:
    """Read one explicit descriptor and classify compatibility without side effects."""
    ordered_required = tuple(sorted(required, key=lambda item: item.value))
    try:
        descriptor = broker.broker_descriptor  # type: ignore[attr-defined]
    except Exception:
        return BrokerCompatibility(
            False,
            "broker_capabilities_unavailable",
            ordered_required,
            ordered_required,
        )
    if not isinstance(descriptor, BrokerDescriptor):
        return BrokerCompatibility(
            False,
            "broker_capabilities_invalid",
            ordered_required,
            ordered_required,
        )
    missing = set(required) - descriptor.capabilities
    if require_hedging and BrokerCapability.HEDGING not in descriptor.capabilities:
        missing.add(BrokerCapability.HEDGING)
    incompatible = (
        bool(missing)
        or (environment is not None and descriptor.environment is not environment)
        or (deterministic is not None and descriptor.deterministic is not deterministic)
    )
    return BrokerCompatibility(
        not incompatible,
        "broker_capability_unsupported" if incompatible else "compatible",
        ordered_required,
        tuple(sorted(missing, key=lambda item: item.value)),
        descriptor,
    )


CURRENT_ORDER_MANAGER_REQUIREMENTS = frozenset(
    {
        BrokerCapability.MARKET_ORDERS,
        BrokerCapability.CLIENT_ORDER_IDS,
        BrokerCapability.NATIVE_SL_TP,
    }
)

CURRENT_PAPER_SESSION_REQUIREMENTS = CURRENT_ORDER_MANAGER_REQUIREMENTS | {BrokerCapability.HEDGING}
