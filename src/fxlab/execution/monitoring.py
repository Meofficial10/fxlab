"""Immutable, read-only operational projections for paper trading."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from .event_ledger import AuditComponent, AuditEvent, AuditEventType

if TYPE_CHECKING:
    from .paper_session import PaperTradingSession
    from .recovery import RecoveryResult

type FrozenValue = (
    None
    | bool
    | int
    | float
    | str
    | tuple["FrozenValue", ...]
    | tuple[tuple[str, "FrozenValue"], ...]
)


class MonitoringSource(StrEnum):
    LIVE_RUNTIME = "live_runtime"
    RECOVERED_SNAPSHOT = "recovered_snapshot"
    CHECKPOINT_STATE = "checkpoint_state"
    DURABLE_EVENT_HISTORY = "durable_event_history"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RuntimeMonitoringView:
    state: str
    reason: str | None
    execution_enabled: bool
    market_maintenance_enabled: bool
    entry_watermark: str | None
    started: bool
    stopped: bool
    audit_integrity: bool
    reconciliation_required: bool
    latest_market_timestamp: str | None


@dataclass(frozen=True)
class AccountMonitoringView:
    balance: float
    equity: float
    margin_used: float
    margin_available: float
    realized_pnl: float
    realized_pnl_basis: str
    unrealized_pnl: float
    open_position_count: int
    account_currency: str
    margin_model: str
    margin_model_identity: str
    margin_quality: str
    leverage_by_symbol: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        _finite_fields(
            self,
            "balance",
            "equity",
            "margin_used",
            "margin_available",
            "realized_pnl",
            "unrealized_pnl",
        )


@dataclass(frozen=True)
class RiskMonitoringView:
    peak_equity: float | None
    drawdown_pct: float | None
    daily_start_equity: float | None
    daily_loss_pct: float | None
    daily_trade_count: int
    consecutive_losses: int
    kill_switch_active: bool
    kill_switch_reason: str | None
    reservation_count: int
    approved_order_count: int
    reserved_exposure_by_symbol: tuple[tuple[str, float], ...]
    open_exposure_by_symbol: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class OrderMonitoringView:
    signal_id: str | None
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: int
    size: float
    order_type: str
    status: str
    sl_price: float | None
    tp_price: float | None
    reservation_released: bool

    def __post_init__(self) -> None:
        _required_id(self.client_order_id, "client_order_id")
        _optional_id(self.signal_id, "signal_id")
        _optional_id(self.broker_order_id, "broker_order_id")
        _required_id(self.symbol, "symbol")
        if self.side not in {-1, 1}:
            raise ValueError("side must be -1 or 1")
        _positive_finite(self.size, "size")
        for name in ("sl_price", "tp_price"):
            value = getattr(self, name)
            if value is not None:
                _finite(value, name)


@dataclass(frozen=True)
class PositionMonitoringView:
    position_id: str
    client_order_id: str | None
    broker_order_id: str | None
    symbol: str
    side: int
    size: float
    entry_price: float
    entry_time: str
    unrealized_pnl: float

    def __post_init__(self) -> None:
        _required_id(self.position_id, "position_id")
        _optional_id(self.client_order_id, "client_order_id")
        _optional_id(self.broker_order_id, "broker_order_id")
        _required_id(self.symbol, "symbol")
        if self.side not in {-1, 1}:
            raise ValueError("side must be -1 or 1")
        _positive_finite(self.size, "size")
        _positive_finite(self.entry_price, "entry_price")
        _finite(self.unrealized_pnl, "unrealized_pnl")


@dataclass(frozen=True)
class ProviderMonitoringView:
    provider_id: str
    provider_version: str
    normalization_version: str
    dataset_id: str
    content_hash: str
    provenance_quality: str
    canonical_symbols: tuple[str, ...]
    timeframe: str
    replay_fingerprint: str
    replay_cursor: int
    last_timestamp: str | None
    freshness_policy: str | None
    fallback_policy: str | None
    data_health_reason: str | None


@dataclass(frozen=True)
class BrokerMonitoringView:
    broker_id: str
    implementation_version: str
    environment: str
    deterministic: bool
    position_mode: str
    capabilities: tuple[str, ...]
    descriptor_fingerprint: str
    connected: bool
    compatibility_state: str
    valuation_policy_version: str
    instrument_catalog_fingerprint: str
    latest_valuation_observation: str | None
    valuation_health: str
    execution_model_fingerprint: str


@dataclass(frozen=True)
class RecoveryMonitoringView:
    status: str | None
    reason: str | None
    checkpoint_sequence: int | None
    latest_event_sequence: int
    reconciliation_required: bool
    reconciliation_reason: str | None
    latest_reconciliation_id: str | None
    latest_reconciliation_status: str | None
    old_session_id: str | None
    new_session_id: str | None


@dataclass(frozen=True)
class AuditEventMonitoringView:
    sequence: int
    occurred_at: str
    event_type: str
    component: str
    signal_id: str | None
    client_order_id: str | None
    broker_order_id: str | None
    position_id: str | None
    close_order_id: str | None
    payload: tuple[tuple[str, FrozenValue], ...]

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        for name in (
            "signal_id",
            "client_order_id",
            "broker_order_id",
            "position_id",
            "close_order_id",
        ):
            _optional_id(getattr(self, name), name)
        _validate_safe_pairs(self.payload)


@dataclass(frozen=True)
class MonitoringSnapshot:
    source: MonitoringSource
    label: str
    session_id: str
    runtime: RuntimeMonitoringView
    account: AccountMonitoringView
    risk: RiskMonitoringView
    provider: ProviderMonitoringView
    broker: BrokerMonitoringView
    recovery: RecoveryMonitoringView
    orders: tuple[OrderMonitoringView, ...]
    positions: tuple[PositionMonitoringView, ...]
    recent_events: tuple[AuditEventMonitoringView, ...]


@dataclass(frozen=True)
class MonitoringResult:
    available: bool
    source: MonitoringSource
    snapshot: MonitoringSnapshot | None
    reason: str | None = None
    message: str | None = None


def project_live_session(session: PaperTradingSession) -> MonitoringSnapshot:
    """Project a session already serialized by its cycle lock."""
    return _project_session(session, source=MonitoringSource.LIVE_RUNTIME, recovery=None)


def project_recovered_session(
    session: PaperTradingSession,
    recovery: RecoveryResult,
    *,
    latest_event_sequence: int,
    recent_events: tuple[AuditEventMonitoringView, ...] = (),
) -> MonitoringSnapshot:
    source = (
        MonitoringSource.RECOVERED_SNAPSHOT
        if recovery.recovered
        else MonitoringSource.CHECKPOINT_STATE
    )
    return _project_session(
        session,
        source=source,
        recovery=recovery,
        latest_event_sequence=latest_event_sequence,
        recent_events=recent_events,
    )


def project_audit_events(
    events: tuple[AuditEvent, ...],
    *,
    event_type: AuditEventType | None = None,
    component: AuditComponent | None = None,
    correlation_id: str | None = None,
    after_sequence: int | None = None,
    limit: int | None = None,
) -> tuple[AuditEventMonitoringView, ...]:
    if after_sequence is not None and (
        isinstance(after_sequence, bool)
        or not isinstance(after_sequence, int)
        or after_sequence < 0
    ):
        raise ValueError("after_sequence must be a non-negative integer")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    if correlation_id is not None:
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError("correlation_id must be non-empty")
        correlation_id = correlation_id.strip()
    selected: list[AuditEventMonitoringView] = []
    for event in events:
        if event_type is not None and event.event_type is not event_type:
            continue
        if component is not None and event.component is not component:
            continue
        if after_sequence is not None and event.sequence <= after_sequence:
            continue
        corr = event.correlation
        ids = (
            corr.signal_id,
            corr.client_order_id,
            corr.broker_order_id,
            corr.position_id,
            corr.close_order_id,
        )
        if correlation_id is not None and correlation_id not in ids:
            continue
        selected.append(_event_view(event))
    if limit is not None:
        selected = selected[-limit:]
    return tuple(selected)


def monitoring_to_dict(snapshot: MonitoringSnapshot) -> dict[str, object]:
    """Serialize only the explicit monitoring contract."""
    return {
        "source": snapshot.source.value,
        "label": snapshot.label,
        "session_id": snapshot.session_id,
        "runtime": _dto_dict(snapshot.runtime),
        "account": _dto_dict(snapshot.account),
        "risk": _dto_dict(snapshot.risk),
        "provider": _dto_dict(snapshot.provider),
        "broker": _dto_dict(snapshot.broker),
        "recovery": _dto_dict(snapshot.recovery),
        "orders": [_dto_dict(item) for item in snapshot.orders],
        "positions": [_dto_dict(item) for item in snapshot.positions],
        "recent_events": [_event_dict(item) for item in snapshot.recent_events],
    }


def _project_session(
    session: PaperTradingSession,
    *,
    source: MonitoringSource,
    recovery: RecoveryResult | None,
    latest_event_sequence: int | None = None,
    recent_events: tuple[AuditEventMonitoringView, ...] | None = None,
) -> MonitoringSnapshot:
    runtime = session.runtime_status()
    account = session.account_snapshot()
    risk_state = dict(session.risk_state_snapshot())
    order_state = dict(session.orders_snapshot())
    session_state = session.snapshot_state()
    replay_state = session.replay.snapshot_state()
    provider_state = session.replay.provider_compatibility_snapshot()
    descriptor = session.broker.broker_descriptor
    descriptor_state = descriptor.compatibility_snapshot()
    economic = session.broker.economic_monitoring_snapshot()
    positions = tuple(account.open_positions)
    correlations = {
        item["position_id"]: item
        for item in session_state["position_correlations"]
        if isinstance(item, dict) and isinstance(item.get("position_id"), str)
    }
    orders = tuple(_order_view(item) for item in order_state["records"])
    position_views = tuple(
        _position_view(position, correlations.get(position.position_id)) for position in positions
    )
    unrealized = sum(float(position.unrealized_pnl) for position in positions)
    initial_balance = float(session.broker.initial_balance)
    peak = _optional_finite(risk_state.get("peak_equity"))
    daily_start = _optional_finite(risk_state.get("daily_start_equity"))
    reservations = tuple(risk_state.get("reservations", ()))
    reserved = _sum_exposure(reservations, size_key="size_lots")
    open_exposure = _sum_exposure(
        ({"symbol": item.symbol, "size": item.size} for item in positions),
        size_key="size",
    )
    events = session.event_ledger.events()
    event_views = recent_events if recent_events is not None else project_audit_events(events)
    reconciliation = _reconciliation_details(events)
    current_latest = len(events) if latest_event_sequence is None else latest_event_sequence
    reason = runtime.reason.value if runtime.reason else None
    data_reason = reason if reason in {"data_stale", "data_unavailable", "data_invalid"} else None
    bound = session_state.get("broker_descriptor_fingerprint")
    compatibility = (
        "unbound"
        if bound is None
        else ("compatible" if bound == descriptor.fingerprint else "incompatible")
    )
    return MonitoringSnapshot(
        source=source,
        label={
            MonitoringSource.LIVE_RUNTIME: "LIVE RUNTIME",
            MonitoringSource.RECOVERED_SNAPSHOT: "RECOVERED SNAPSHOT",
            MonitoringSource.CHECKPOINT_STATE: "CHECKPOINT STATE",
        }[source],
        session_id=session.event_ledger.session_id,
        runtime=RuntimeMonitoringView(
            runtime.state.value,
            reason,
            runtime.execution_enabled,
            runtime.market_maintenance_enabled,
            _iso(runtime.entry_enable_watermark),
            runtime.started,
            runtime.stopped,
            not bool(session_state["audit_failed"] or order_state["audit_failed"]),
            bool(session_state["recovery_required"]),
            session_state["last_market_time"],
        ),
        account=AccountMonitoringView(
            float(account.balance),
            float(account.equity),
            float(account.margin_used),
            float(account.margin_available),
            float(account.balance) - initial_balance,
            "paper_broker_accounting_projection",
            unrealized,
            len(positions),
            str(economic["account_currency"]),
            str(economic["margin_model"]),
            str(economic["margin_model_identity"]),
            str(economic["margin_quality"]),
            tuple(
                (str(symbol), float(leverage))
                for symbol, leverage in economic["margin_leverage"]
            ),
        ),
        risk=RiskMonitoringView(
            peak,
            _loss_pct(peak, float(account.equity)),
            daily_start,
            _loss_pct(daily_start, float(account.equity)),
            int(risk_state["daily_trades"]),
            int(risk_state["consecutive_losses"]),
            bool(risk_state["kill_switch_active"]),
            risk_state["kill_switch_reason"],
            len(reservations),
            len(tuple(risk_state["approved_order_ids"])),
            reserved,
            open_exposure,
        ),
        provider=ProviderMonitoringView(
            str(provider_state["provider_id"]),
            str(provider_state["provider_version"]),
            str(provider_state["normalization_version"]),
            str(provider_state["dataset_id"]),
            str(provider_state["content_hash"]),
            session.replay.provenance_quality.value,
            tuple(sorted(key.strip().upper() for key in session.replay.bars_by_symbol)),
            session.replay.timeframe,
            session.replay.dataset_fingerprint,
            int(replay_state["cursor"]),
            replay_state["last_consumed_timestamp"],
            str(provider_state.get("freshness_policy"))
            if provider_state.get("freshness_policy")
            else None,
            str(provider_state.get("fallback_policy"))
            if provider_state.get("fallback_policy")
            else None,
            data_reason,
        ),
        broker=BrokerMonitoringView(
            descriptor.broker_id,
            descriptor.implementation_version,
            descriptor.environment.value,
            descriptor.deterministic,
            str(descriptor_state["position_mode"]),
            tuple(str(item) for item in descriptor_state["capabilities"]),
            descriptor.fingerprint,
            session.broker.is_connected(),
            compatibility,
            str(economic["valuation_policy_version"]),
            str(economic["instrument_catalog_fingerprint"]),
            (
                str(economic["latest_valuation_observation"])
                if economic["latest_valuation_observation"] is not None
                else None
            ),
            str(economic["valuation_health"]),
            str(economic["execution_model_fingerprint"]),
        ),
        recovery=RecoveryMonitoringView(
            recovery.state.value if recovery is not None else None,
            recovery.reason if recovery is not None else None,
            recovery.checkpoint_sequence if recovery is not None else None,
            current_latest,
            bool(session_state["recovery_required"]),
            reason if runtime.state.value == "reconciliation_required" else None,
            reconciliation[0],
            reconciliation[1],
            reconciliation[2],
            reconciliation[3],
        ),
        orders=orders,
        positions=position_views,
        recent_events=event_views,
    )


def _order_view(item: object) -> OrderMonitoringView:
    if not isinstance(item, dict) or not isinstance(item.get("request"), dict):
        raise ValueError("invalid order monitoring state")
    request = item["request"]
    client_id = str(item["client_order_id"])
    return OrderMonitoringView(
        client_id,
        client_id,
        item.get("broker_order_id"),
        str(request["symbol"]),
        int(request["side"]),
        float(request["size"]),
        str(request["order_type"]),
        str(item["status"]),
        _optional_finite(request.get("sl_price")),
        _optional_finite(request.get("tp_price")),
        bool(item["reservation_released"]),
    )


def _position_view(position: object, correlation: object) -> PositionMonitoringView:
    corr = correlation if isinstance(correlation, dict) else {}
    return PositionMonitoringView(
        position.position_id,
        corr.get("client_order_id"),
        corr.get("broker_order_id"),
        position.symbol,
        position.side,
        float(position.size),
        float(position.entry_price),
        position.entry_time.astimezone(UTC).isoformat(),
        float(position.unrealized_pnl),
    )


def _sum_exposure(items: object, *, size_key: str) -> tuple[tuple[str, float], ...]:
    totals: dict[str, float] = {}
    for item in items:
        symbol = str(item["symbol"])
        size = float(item[size_key])
        if not math.isfinite(size):
            raise ValueError("exposure must be finite")
        totals[symbol] = totals.get(symbol, 0.0) + size
    return tuple(sorted(totals.items()))


def _loss_pct(baseline: float | None, equity: float) -> float | None:
    if baseline is None or baseline <= 0:
        return None
    return max(0.0, (baseline - equity) * 100.0 / baseline)


def _optional_finite(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("monitoring numeric values must be finite")
    return number


def _finite_fields(instance: object, *names: str) -> None:
    for name in names:
        value = getattr(instance, name)
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite")


def _finite(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")


def _positive_finite(value: object, field_name: str) -> None:
    _finite(value, field_name)
    if float(value) <= 0:
        raise ValueError(f"{field_name} must be positive")


def _required_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_id(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_id(value, field_name)


_SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "privatekey",
}


def _validate_safe_pairs(pairs: object) -> None:
    if not isinstance(pairs, tuple):
        raise ValueError("payload must be immutable")
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2 or not isinstance(pair[0], str):
            raise ValueError("payload mappings must contain string keys")
        normalized = "".join(character for character in pair[0].lower() if character.isalnum())
        if normalized in _SENSITIVE_KEYS:
            raise ValueError("payload contains a sensitive key")
        value = pair[1]
        if isinstance(value, tuple):
            if value and all(
                isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
                for item in value
            ):
                _validate_safe_pairs(value)
            else:
                for item in value:
                    if isinstance(item, tuple) and item and all(
                        isinstance(child, tuple)
                        and len(child) == 2
                        and isinstance(child[0], str)
                        for child in item
                    ):
                        _validate_safe_pairs(item)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _event_view(event: AuditEvent) -> AuditEventMonitoringView:
    correlation = event.correlation
    return AuditEventMonitoringView(
        event.sequence,
        event.occurred_at.astimezone(UTC).isoformat(),
        event.event_type.value,
        event.component.value,
        correlation.signal_id,
        correlation.client_order_id,
        correlation.broker_order_id,
        correlation.position_id,
        correlation.close_order_id,
        _freeze_mapping(event.payload),
    )


def _freeze_mapping(value: object) -> tuple[tuple[str, FrozenValue], ...]:
    if not hasattr(value, "items"):
        raise ValueError("monitoring payload must be a mapping")
    return tuple(sorted((str(key), _freeze_value(item)) for key, item in value.items()))


def _freeze_value(value: object) -> FrozenValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("monitoring payload values must be finite")
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_value(item) for item in value)
    if hasattr(value, "items"):
        return _freeze_mapping(value)
    raise ValueError("monitoring payload contains an unsupported value")


def _reconciliation_details(
    events: tuple[AuditEvent, ...],
) -> tuple[str | None, str | None, str | None, str | None]:
    for event in reversed(events):
        if event.event_type not in {
            AuditEventType.RECONCILIATION_STARTED,
            AuditEventType.RECONCILIATION_RESOLVED,
            AuditEventType.RECONCILIATION_UNRESOLVED,
        }:
            continue
        payload = event.payload
        return (
            str(payload.get("reconciliation_id")) if payload.get("reconciliation_id") else None,
            event.event_type.value,
            str(payload.get("old_session_id")) if payload.get("old_session_id") else None,
            str(payload.get("new_session_id")) if payload.get("new_session_id") else None,
        )
    return None, None, None, None


def _dto_dict(value: object) -> dict[str, object]:
    names = value.__dataclass_fields__  # type: ignore[attr-defined]
    return {name: _json_value(getattr(value, name)) for name in names}


def _event_dict(event: AuditEventMonitoringView) -> dict[str, object]:
    result = _dto_dict(event)
    result["payload"] = {key: _json_value(value) for key, value in event.payload}
    return result


def _json_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value
