"""Persistent MT5 Demo session runner with position monitoring and crash recovery."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock

from ..risk.engine import KillSwitchReason, RiskEngine, RiskLimits
from .broker import Tick
from .broker_capabilities import BrokerCapability
from .event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
)
from .mt5_demo_broker import (
    _MT5_DEMO_RESOLVER,
    MT5_DEMO_MAGIC,
    MT5_DEMO_SYMBOL,
    Mt5DemoBroker,
    _minimum_volume,
    _mt5_comment,
    _protective_stop_buy,
    _protective_stop_sell,
    _query,
    _validated_symbol,
    _verified_demo_authority,
)
from .mt5_demo_runtime import _operator_comment
from .order_manager import (
    ExecutionIntent,
    ExecutionResult,
    ExecutionResultKind,
    OrderManager,
)
from .runtime_control import (
    RuntimeController,
    RuntimeControlReason,
    RuntimeControlResult,
    RuntimeState,
)
from .signal_engine import SignalEvent

_PHASE_2A_TEST_PERMIT_TOKEN = object()
_SOAK_PERMIT_TOKEN = object()


@dataclass(frozen=True, slots=True)
class _Mt5DemoExecutionPermit:
    """Unforgeable in-process capability for the deterministic Phase 2A test harness."""

    _token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _Mt5DemoSoakExecutionPermit:
    """Unforgeable in-process capability strictly for the Phase 2B soak harness."""

    _token: object = field(repr=False, compare=False)


def _phase_2a_test_execution_permit() -> _Mt5DemoExecutionPermit:
    """Create the private structural permit used only by the Phase 2A test harness."""
    return _Mt5DemoExecutionPermit(_PHASE_2A_TEST_PERMIT_TOKEN)


def _issue_soak_execution_permit_internal() -> _Mt5DemoSoakExecutionPermit:
    """Private factory creating the unforgeable Phase 2B soak capability."""
    return _Mt5DemoSoakExecutionPermit(_SOAK_PERMIT_TOKEN)


def _validate_strategy_gate(
    signal: SignalEvent,
    execution_permit: _Mt5DemoExecutionPermit | _Mt5DemoSoakExecutionPermit | None,
) -> None:
    """Reject every signal unless a separately injected structural capability is valid."""
    del signal
    if (
        isinstance(execution_permit, _Mt5DemoExecutionPermit)
        and execution_permit._token is _PHASE_2A_TEST_PERMIT_TOKEN
    ):
        return
    if (
        isinstance(execution_permit, _Mt5DemoSoakExecutionPermit)
        and execution_permit._token is _SOAK_PERMIT_TOKEN
    ):
        return
    raise RuntimeError("unvalidated_strategy_execution_prohibited")


class Mt5SessionCycleKind(StrEnum):
    PROCESSED = "processed"
    POSITION_HELD = "position_held"
    POSITION_CLOSED = "position_closed"
    NO_SIGNAL = "no_signal"
    POLICY_DECLINED = "policy_declined"
    RISK_REJECTED = "risk_rejected"
    PAUSED = "paused"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class Mt5SessionCycleResult:
    kind: Mt5SessionCycleKind
    current_time: datetime
    tick: Tick | None = None
    signal: SignalEvent | None = None
    execution: ExecutionResult | None = None
    position_id: str | None = None
    close_order_id: str | None = None
    close_deal_id: str | None = None
    exit_reason: str | None = None
    reason: str = ""
    message: str = ""


@dataclass
class Mt5DemoSession:
    """Persistent, unattended MT5 Demo session with position monitoring and crash recovery."""

    broker: Mt5DemoBroker
    event_ledger: EventLedger
    max_loss_usd: float
    max_quote_age: timedelta
    execution_permit: _Mt5DemoExecutionPermit | _Mt5DemoSoakExecutionPermit | None = field(
        default=None, repr=False
    )
    risk_limits: RiskLimits | None = None
    clock: object = field(default=None)

    _risk_engine: RiskEngine = field(init=False)
    _order_manager: OrderManager = field(init=False)
    _runtime_controller: RuntimeController = field(default_factory=RuntimeController, init=False)
    _active_position_id: str | None = field(default=None, init=False)
    _active_client_order_id: str | None = field(default=None, init=False)
    _active_broker_order_id: str | None = field(default=None, init=False)
    _reconciliation_required: bool = field(default=False, init=False)
    _failed_reason: RuntimeControlReason | None = field(default=None, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.event_ledger, EventLedger)
            or self.event_ledger.durable_store is None
        ):
            raise ValueError("mt5_durable_audit_required")
        if (
            not isinstance(self.max_loss_usd, (int, float))
            or isinstance(self.max_loss_usd, bool)
            or not math.isfinite(self.max_loss_usd)
            or self.max_loss_usd <= 0
        ):
            raise ValueError("invalid_max_loss_usd")
        if not isinstance(self.max_quote_age, timedelta) or self.max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be positive")

        limits = self.risk_limits or RiskLimits(
            starting_equity=10000.0,
            max_open_positions=1,
            max_exposure_per_symbol_lots=0.01,
            max_risk_per_trade_pct=1.0,
        )
        self._risk_engine = RiskEngine(
            limits=limits,
            pip_size_resolver=_MT5_DEMO_RESOLVER,
            lot_step=0.01,
        )
        self._order_manager = OrderManager(
            broker=self.broker,
            risk_engine=self._risk_engine,
            event_ledger=self.event_ledger,
            required_capabilities=frozenset(
                {
                    BrokerCapability.MARKET_ORDERS,
                    BrokerCapability.NATIVE_SL_TP,
                }
            ),
        )

    @property
    def risk_engine(self) -> RiskEngine:
        return self._risk_engine

    @property
    def order_manager(self) -> OrderManager:
        return self._order_manager

    @property
    def runtime_controller(self) -> RuntimeController:
        return self._runtime_controller

    @property
    def active_position_id(self) -> str | None:
        with self._lock:
            return self._active_position_id

    @property
    def reconciliation_required(self) -> bool:
        with self._lock:
            return self._reconciliation_required

    @property
    def failed_reason(self) -> RuntimeControlReason | None:
        with self._lock:
            return self._failed_reason

    def start(self) -> RuntimeControlResult:
        """Connect to MT5, run startup reconciliation, and start session."""
        now = self._now()
        self.broker.connect()
        self.broker.subscribe_market_data([MT5_DEMO_SYMBOL])

        terminal, account = _verified_demo_authority(self.broker.api)
        correlation = EventCorrelation(
            client_order_id=_operator_comment(self.event_ledger.session_id)
        )

        self.event_ledger.append(
            AuditEventType.SESSION_STARTED,
            occurred_at=now,
            component=AuditComponent.PAPER_SESSION,
            correlation=correlation,
            payload={"environment": "demo", "symbol": MT5_DEMO_SYMBOL},
        )

        # Run startup reconciliation against broker state vs durable ledger
        reconciliation_state = self._startup_reconciliation(now)
        if reconciliation_state == "reconciliation_required":
            self._reconciliation_required = True
            return self._runtime_controller.status(reconciliation_required=True)  # type: ignore[return-value]

        return self._runtime_controller.start()

    def reconcile_startup(self, current_time: datetime | None = None) -> str:
        """Inspect durable SQLite ledger vs MT5 broker state upon startup."""
        now = current_time or self._now()
        return self._startup_reconciliation(now)

    def _startup_reconciliation(self, now: datetime) -> str:
        """Reconcile authoritative MT5 state against the durable session ledger."""
        positions_raw = _query(
            self.broker.api,
            "positions_get",
            "mt5_position_query_failed",
            symbol=MT5_DEMO_SYMBOL,
        )
        events = (
            self.event_ledger.durable_store.load_events()
            if self.event_ledger.durable_store is not None
            else self.event_ledger.events()
        )
        opened_events = [e for e in events if e.event_type == AuditEventType.POSITION_OPENED]
        closed_events = [e for e in events if e.event_type == AuditEventType.POSITION_CLOSED]
        closed_pos_ids = {
            str(e.payload.get("position_id") or e.correlation.position_id)
            for e in closed_events
            if (e.payload.get("position_id") or e.correlation.position_id)
        }
        unclosed_opened = [
            e
            for e in opened_events
            if str(e.payload.get("position_id") or e.correlation.position_id) not in closed_pos_ids
        ]

        if len(positions_raw) == 0 and len(unclosed_opened) == 0:
            self._clear_active_position()
            self._reconciliation_required = False
            return "clean"

        if len(positions_raw) == 1 and len(unclosed_opened) == 1:
            opened = unclosed_opened[0]
            pos = positions_raw[0]
            broker_ticket = str(getattr(pos, "ticket", ""))
            ledger_ticket = str(opened.payload.get("position_id") or opened.correlation.position_id)
            client_order_id = opened.correlation.client_order_id
            broker_order_id = opened.correlation.broker_order_id or opened.payload.get(
                "entry_order_id"
            )
            expected_side = opened.payload.get("side")
            expected_type = (
                getattr(self.broker.api, "POSITION_TYPE_BUY", 0)
                if expected_side == "buy"
                else getattr(self.broker.api, "POSITION_TYPE_SELL", 1)
                if expected_side == "sell"
                else None
            )
            volume = opened.payload.get("volume")
            exact_match = (
                bool(broker_ticket)
                and broker_ticket == ledger_ticket
                and isinstance(client_order_id, str)
                and bool(client_order_id)
                and broker_order_id is not None
                and str(getattr(pos, "identifier", "")) == str(broker_order_id)
                and getattr(pos, "magic", None) == MT5_DEMO_MAGIC
                and getattr(pos, "symbol", None) == MT5_DEMO_SYMBOL
                and getattr(pos, "comment", None) == _mt5_comment(client_order_id)
                and getattr(pos, "type", None) == expected_type
                and isinstance(volume, (int, float))
                and not isinstance(volume, bool)
                and math.isfinite(float(volume))
                and math.isclose(
                    float(getattr(pos, "volume", float("nan"))),
                    float(volume),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            )
            if exact_match:
                with self._lock:
                    self._active_position_id = broker_ticket
                    self._active_client_order_id = client_order_id
                    self._active_broker_order_id = str(broker_order_id)
                self._reconciliation_required = False
                return "reconnected_open_position"

        if len(positions_raw) == 0 and len(unclosed_opened) == 1:
            opened = unclosed_opened[0]
            ledger_ticket = str(opened.payload.get("position_id") or opened.correlation.position_id)
            ticket_int = int(ledger_ticket) if ledger_ticket.isdigit() else None
            if ticket_int is not None:
                history_deals = _query(
                    self.broker.api,
                    "history_deals_get",
                    "mt5_close_history_query_failed",
                    position=ticket_int,
                )
                exit_deals = [
                    d
                    for d in history_deals
                    if getattr(d, "position_id", None) == ticket_int
                    and getattr(d, "entry", None) == getattr(self.broker.api, "DEAL_ENTRY_OUT", 1)
                    and getattr(d, "symbol", None) == MT5_DEMO_SYMBOL
                    and getattr(d, "magic", None) == MT5_DEMO_MAGIC
                ]
                if len(exit_deals) == 1:
                    closing_deal = exit_deals[0]
                    reason_val = getattr(closing_deal, "reason", None)
                    reasons = {
                        getattr(self.broker.api, "DEAL_REASON_SL", 4): "SL",
                        getattr(self.broker.api, "DEAL_REASON_TP", 5): "TP",
                    }
                    exit_reason = reasons.get(reason_val)
                    close_order_id = str(getattr(closing_deal, "order", ""))
                    close_deal_id = str(getattr(closing_deal, "ticket", ""))
                    expected_volume = opened.payload.get("volume")
                    profit = getattr(closing_deal, "profit", None)
                    exact_exit = (
                        exit_reason is not None
                        and close_order_id.isdigit()
                        and int(close_order_id) > 0
                        and close_deal_id.isdigit()
                        and int(close_deal_id) > 0
                        and isinstance(expected_volume, (int, float))
                        and not isinstance(expected_volume, bool)
                        and math.isclose(
                            float(getattr(closing_deal, "volume", float("nan"))),
                            float(expected_volume),
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                        and self._record_realized_close(profit, now)
                    )
                    if exact_exit:
                        self.event_ledger.append(
                            AuditEventType.POSITION_CLOSED,
                            occurred_at=now,
                            component=AuditComponent.BROKER_ADAPTER,
                            correlation=EventCorrelation(
                                client_order_id=opened.correlation.client_order_id,
                                broker_order_id=opened.correlation.broker_order_id,
                                position_id=ledger_ticket,
                                close_order_id=close_order_id,
                            ),
                            payload={
                                "symbol": MT5_DEMO_SYMBOL,
                                "position_id": ledger_ticket,
                                "close_order_id": close_order_id,
                                "close_deal_id": close_deal_id,
                                "exit_reason": exit_reason,
                                "realized_pnl": float(profit),
                                "reconciliation": "STARTUP_HISTORICAL_MATCH",
                            },
                        )
                        self._clear_active_position()
                        self._reconciliation_required = False
                        return "reconciled_closed_position"

        self._reconciliation_required = True
        self._risk_engine.trigger_kill_switch(KillSwitchReason.POSITION_RECONCILIATION_FAILED)
        return "reconciliation_required"

    def _clear_active_position(self) -> None:
        with self._lock:
            self._active_position_id = None
            self._active_client_order_id = None
            self._active_broker_order_id = None

    def _record_realized_close(self, realized_pnl: object, now: datetime) -> bool:
        if (
            not isinstance(realized_pnl, (int, float))
            or isinstance(realized_pnl, bool)
            or not math.isfinite(float(realized_pnl))
        ):
            return False
        self._risk_engine.on_trade_closed(float(realized_pnl))
        account = self.broker.get_account_info()
        self._risk_engine.check_account_state(account, now)
        return True

    def _reconcile_close_deal(
        self,
        *,
        position_id: str,
        close_order_id: str,
        close_deal_id: str,
        now: datetime,
    ) -> float | None:
        if not (position_id.isdigit() and close_order_id.isdigit() and close_deal_id.isdigit()):
            return None
        deals = _query(
            self.broker.api,
            "history_deals_get",
            "mt5_close_history_query_failed",
            ticket=int(close_deal_id),
        )
        matches = [
            deal
            for deal in deals
            if str(getattr(deal, "ticket", "")) == close_deal_id
            and str(getattr(deal, "order", "")) == close_order_id
            and str(getattr(deal, "position_id", "")) == position_id
            and getattr(deal, "entry", None) == getattr(self.broker.api, "DEAL_ENTRY_OUT", 1)
            and getattr(deal, "symbol", None) == MT5_DEMO_SYMBOL
            and getattr(deal, "magic", None) == MT5_DEMO_MAGIC
        ]
        if len(matches) != 1:
            return None
        realized_pnl = getattr(matches[0], "profit", None)
        if not self._record_realized_close(realized_pnl, now):
            return None
        return float(realized_pnl)

    def pause(
        self,
        reason: RuntimeControlReason = RuntimeControlReason.OPERATOR_PAUSED,
        current_time: datetime | None = None,
    ) -> RuntimeControlResult:
        now = current_time or self._now()
        res = self._runtime_controller.pause(now, reason=reason)
        if res.changed:
            self._append_runtime_transition(
                res.previous_state, res.current_state, reason, now, operator_requested=True
            )
        return res

    def resume(self, current_time: datetime | None = None) -> RuntimeControlResult:
        now = current_time or self._now()
        res = self._runtime_controller.resume(
            now,
            reconciliation_required=self._reconciliation_required,
            kill_switch_active=self._risk_engine.kill_switch_active,
        )
        if res.changed:
            self._append_runtime_transition(
                res.previous_state,
                res.current_state,
                RuntimeControlReason.OPERATOR_PAUSED,
                now,
                operator_requested=True,
            )
        return res

    def stop(self, current_time: datetime | None = None) -> RuntimeControlResult:
        now = current_time or self._now()
        self._runtime_controller.request_stop()
        res = self._runtime_controller.complete_stop()
        self.event_ledger.append(
            AuditEventType.SESSION_STOPPED,
            occurred_at=now,
            component=AuditComponent.PAPER_SESSION,
            payload={"status": "stopped"},
        )
        self.broker.disconnect()
        return res

    def poll_cycle(
        self,
        signal: SignalEvent | None = None,
        *,
        force_close: bool = False,
        current_time: datetime | None = None,
    ) -> Mt5SessionCycleResult:
        """Run one atomic polling cycle."""
        now = current_time or self._now()

        # Check runtime controller status
        status = self._runtime_controller.status(
            reconciliation_required=self._reconciliation_required,
            failed_reason=self._failed_reason,
            kill_switch_active=self._risk_engine.kill_switch_active,
        )
        if status.state is RuntimeState.RECONCILIATION_REQUIRED:
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                current_time=now,
                reason="reconciliation_required",
                message="session requires manual reconciliation",
            )
        if status.state is RuntimeState.KILL_SWITCHED:
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.FAILED,
                current_time=now,
                reason="kill_switch_active",
                message="kill switch is active",
            )
        if status.state is RuntimeState.PAUSED:
            # Check if broker has recovered
            if status.reason == RuntimeControlReason.BROKER_UNAVAILABLE:
                try:
                    _verified_demo_authority(self.broker.api)
                    self.broker.connect()
                    self.broker.subscribe_market_data([MT5_DEMO_SYMBOL])
                    reconciliation_state = self._startup_reconciliation(now)
                    if reconciliation_state == "reconciliation_required":
                        return Mt5SessionCycleResult(
                            kind=Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                            current_time=now,
                            reason="reconciliation_required",
                            message="broker state could not be reconciled",
                        )
                    self._runtime_controller.resume(
                        now,
                        reconciliation_required=False,
                        kill_switch_active=self._risk_engine.kill_switch_active,
                    )
                    if reconciliation_state == "reconnected_open_position":
                        return Mt5SessionCycleResult(
                            kind=Mt5SessionCycleKind.POSITION_HELD,
                            current_time=now,
                            position_id=self.active_position_id,
                            reason="broker_reconnected_position_reconciled",
                            message="broker position reconciled before resume",
                        )
                    if reconciliation_state == "reconciled_closed_position":
                        closed = self.event_ledger.events()[-1]
                        return Mt5SessionCycleResult(
                            kind=Mt5SessionCycleKind.POSITION_CLOSED,
                            current_time=now,
                            position_id=str(closed.payload.get("position_id", "")),
                            close_order_id=str(closed.payload.get("close_order_id", "")),
                            close_deal_id=str(closed.payload.get("close_deal_id", "")),
                            exit_reason=str(closed.payload.get("exit_reason", "")),
                            reason="broker_reconnected_exit_reconciled",
                            message="broker exit reconciled before resume",
                        )
                    return Mt5SessionCycleResult(
                        kind=Mt5SessionCycleKind.NO_SIGNAL,
                        current_time=now,
                        reason="broker_reconnected_clean",
                        message="clean broker state reconciled before resume",
                    )
                except Exception:
                    return Mt5SessionCycleResult(
                        kind=Mt5SessionCycleKind.PAUSED,
                        current_time=now,
                        reason="broker_unavailable",
                        message="broker is unavailable",
                    )
            if status.state is RuntimeState.PAUSED:
                return Mt5SessionCycleResult(
                    kind=Mt5SessionCycleKind.PAUSED,
                    current_time=now,
                    reason=str(status.reason.value if status.reason else "paused"),
                    message="session is paused",
                )
        if status.state is RuntimeState.STOPPED:
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.STOPPED,
                current_time=now,
                reason="stopped",
                message="session is stopped",
            )

        # Verify broker authority and connectivity
        try:
            _verified_demo_authority(self.broker.api)
        except Exception:
            self._runtime_controller.pause(now, reason=RuntimeControlReason.BROKER_UNAVAILABLE)
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.PAUSED,
                current_time=now,
                reason="broker_unavailable",
                message="broker terminal or account is not ready",
            )

        # Ingest and validate market tick
        try:
            tick = self.broker.get_latest_tick(MT5_DEMO_SYMBOL)
        except Exception:
            self._runtime_controller.pause(now, reason=RuntimeControlReason.BROKER_UNAVAILABLE)
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.PAUSED,
                current_time=now,
                reason="broker_unavailable",
                message="broker quote query failed",
            )

        if tick is None:
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.PAUSED,
                current_time=now,
                reason="quote_unavailable",
                message="no quote received",
            )

        # Enforce quote freshness contract
        quote_age_sec = (now - tick.timestamp).total_seconds()
        if quote_age_sec < 0 or quote_age_sec > self.max_quote_age.total_seconds():
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.PAUSED,
                current_time=now,
                tick=tick,
                reason="data_stale",
                message="quote age exceeds freshness contract",
            )

        # Position Monitoring Path (when active position is open)
        with self._lock:
            active_pos_id = self._active_position_id

        if active_pos_id is not None:
            ticket_int = int(active_pos_id)
            try:
                positions_active = _query(
                    self.broker.api,
                    "positions_get",
                    "mt5_position_query_failed",
                    ticket=ticket_int,
                )
            except Exception:
                self._runtime_controller.pause(now, reason=RuntimeControlReason.BROKER_UNAVAILABLE)
                return Mt5SessionCycleResult(
                    kind=Mt5SessionCycleKind.PAUSED,
                    current_time=now,
                    tick=tick,
                    position_id=active_pos_id,
                    reason="broker_unavailable",
                    message="failed to query open position",
                )

            if len(positions_active) == 1:
                # Position is still active on broker
                if force_close:
                    close_order_id, close_deal_id = self.broker.close_position(active_pos_id)
                    realized_pnl = self._reconcile_close_deal(
                        position_id=active_pos_id,
                        close_order_id=close_order_id,
                        close_deal_id=close_deal_id,
                        now=now,
                    )
                    if realized_pnl is None:
                        self._reconciliation_required = True
                        return Mt5SessionCycleResult(
                            kind=Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                            current_time=now,
                            position_id=active_pos_id,
                            reason="close_outcome_reconciliation_required",
                            message="close deal outcome could not be reconciled",
                        )
                    correlation = EventCorrelation(
                        client_order_id=self._active_client_order_id,
                        broker_order_id=self._active_broker_order_id,
                        position_id=active_pos_id,
                        close_order_id=close_order_id,
                    )
                    self.event_ledger.append(
                        AuditEventType.POSITION_CLOSED,
                        occurred_at=now,
                        component=AuditComponent.BROKER_ADAPTER,
                        correlation=correlation,
                        payload={
                            "symbol": MT5_DEMO_SYMBOL,
                            "position_id": active_pos_id,
                            "close_order_id": close_order_id,
                            "close_deal_id": close_deal_id,
                            "exit_reason": "MANUAL",
                            "realized_pnl": realized_pnl,
                        },
                    )
                    with self._lock:
                        self._active_position_id = None
                        self._active_client_order_id = None
                        self._active_broker_order_id = None

                    return Mt5SessionCycleResult(
                        kind=Mt5SessionCycleKind.POSITION_CLOSED,
                        current_time=now,
                        tick=tick,
                        position_id=active_pos_id,
                        close_order_id=close_order_id,
                        close_deal_id=close_deal_id,
                        exit_reason="MANUAL",
                        reason="position_closed",
                        message="position closed successfully",
                    )

                # Monitored: position held
                return Mt5SessionCycleResult(
                    kind=Mt5SessionCycleKind.POSITION_HELD,
                    current_time=now,
                    tick=tick,
                    position_id=active_pos_id,
                    reason="position_held",
                    message="position active and monitored",
                )

            if len(positions_active) == 0:
                # Native SL/TP exit occurred while holding!
                close_order_id, close_deal_id = self.broker.close_position(active_pos_id)
                order_status = self.broker.get_order_status(self._active_client_order_id or "")
                exit_reason = str(order_status.get("exit_reason", "SL"))
                realized_pnl = self._reconcile_close_deal(
                    position_id=active_pos_id,
                    close_order_id=close_order_id,
                    close_deal_id=close_deal_id,
                    now=now,
                )
                if realized_pnl is None:
                    self._reconciliation_required = True
                    return Mt5SessionCycleResult(
                        kind=Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                        current_time=now,
                        position_id=active_pos_id,
                        reason="close_outcome_reconciliation_required",
                        message="native close outcome could not be reconciled",
                    )

                correlation = EventCorrelation(
                    client_order_id=self._active_client_order_id,
                    broker_order_id=self._active_broker_order_id,
                    position_id=active_pos_id,
                    close_order_id=close_order_id,
                )
                self.event_ledger.append(
                    AuditEventType.POSITION_CLOSED,
                    occurred_at=now,
                    component=AuditComponent.BROKER_ADAPTER,
                    correlation=correlation,
                    payload={
                        "symbol": MT5_DEMO_SYMBOL,
                        "position_id": active_pos_id,
                        "close_order_id": close_order_id,
                        "close_deal_id": close_deal_id,
                        "exit_reason": exit_reason,
                        "realized_pnl": realized_pnl,
                        "reconciliation": "HOLDING_NATIVE_PROTECTIVE_EXIT",
                    },
                )
                with self._lock:
                    self._active_position_id = None
                    self._active_client_order_id = None
                    self._active_broker_order_id = None

                return Mt5SessionCycleResult(
                    kind=Mt5SessionCycleKind.POSITION_CLOSED,
                    current_time=now,
                    tick=tick,
                    position_id=active_pos_id,
                    close_order_id=close_order_id,
                    close_deal_id=close_deal_id,
                    exit_reason=exit_reason,
                    reason="native_protective_exit",
                    message="position exited natively via protective stop",
                )

        # No active position — Signal Ingestion & Execution Path
        if signal is None:
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.NO_SIGNAL,
                current_time=now,
                tick=tick,
                reason="no_signal",
                message="no trade signal provided",
            )

        # Enforce strategy gate
        _validate_strategy_gate(signal, self.execution_permit)

        # Validate symbol and volume metadata
        metadata = _validated_symbol(self.broker.api)
        volume = _minimum_volume(metadata)

        # Derive protective stop loss based on explicit monetary risk budget
        if signal.side == 1:
            sl_price = _protective_stop_buy(
                metadata,
                tick.bid,
                tick.ask,
                max_loss_usd=self.max_loss_usd,
                volume=volume,
            )
        elif signal.side == -1:
            sl_price = _protective_stop_sell(
                metadata,
                tick.bid,
                tick.ask,
                max_loss_usd=self.max_loss_usd,
                volume=volume,
            )
        else:
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.FAILED,
                current_time=now,
                tick=tick,
                signal=signal,
                reason="invalid_signal_side",
                message="signal side must be 1 or -1",
            )

        intent = ExecutionIntent(signal=signal, sl_price=sl_price)
        exec_result = self._order_manager.submit(intent, current_time=now)

        if exec_result.kind is ExecutionResultKind.RISK_REJECTED:
            risk_reason = (
                exec_result.risk_rejection.reason
                if exec_result.risk_rejection is not None
                else exec_result.reason
            )
            risk_message = (
                exec_result.risk_rejection.message
                if exec_result.risk_rejection is not None
                else exec_result.message
            )
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.RISK_REJECTED,
                current_time=now,
                tick=tick,
                signal=signal,
                execution=exec_result,
                reason=risk_reason,
                message=risk_message,
            )

        if exec_result.kind is not ExecutionResultKind.SUBMITTED:
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.FAILED,
                current_time=now,
                tick=tick,
                signal=signal,
                execution=exec_result,
                reason=exec_result.reason,
                message=exec_result.message,
            )

        record = exec_result.record
        if record is None or record.broker_order_id is None:
            self._reconciliation_required = True
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                current_time=now,
                tick=tick,
                signal=signal,
                execution=exec_result,
                reason="missing_broker_order_id",
                message="order record contains no broker order identity",
            )

        order_status = self.broker.get_order_status(record.client_order_id)
        entry_deal_id = str(order_status.get("deal_id", ""))
        position_ticket = str(order_status.get("position_id", ""))
        if not position_ticket:
            self._reconciliation_required = True
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                current_time=now,
                tick=tick,
                signal=signal,
                execution=exec_result,
                reason="missing_position_id",
                message="order status contains no position identity",
            )

        # Mark order as FILLED via refresh and confirm reservation release in RiskEngine
        self._order_manager.refresh_order_status(record.client_order_id, current_time=now)
        self._order_manager.confirm_position_reflected(record.client_order_id, current_time=now)

        entry_correlation = EventCorrelation(
            client_order_id=record.client_order_id,
            broker_order_id=record.broker_order_id,
            position_id=position_ticket,
        )
        self.event_ledger.append(
            AuditEventType.POSITION_OPENED,
            occurred_at=now,
            component=AuditComponent.BROKER_ADAPTER,
            correlation=entry_correlation,
            payload={
                "symbol": MT5_DEMO_SYMBOL,
                "side": "buy" if signal.side == 1 else "sell",
                "volume": record.request.size,
                "entry_order_id": record.broker_order_id,
                "entry_deal_id": entry_deal_id,
                "position_id": position_ticket,
            },
        )

        if order_status.get("closed_at_entry_reconciliation") is True:
            # Position exited immediately upon entry
            close_order_id = str(order_status.get("close_order_id", ""))
            close_deal_id = str(order_status.get("close_deal_id", ""))
            exit_reason = str(order_status.get("exit_reason", "SL"))
            realized_pnl = self._reconcile_close_deal(
                position_id=position_ticket,
                close_order_id=close_order_id,
                close_deal_id=close_deal_id,
                now=now,
            )
            if realized_pnl is None:
                self._reconciliation_required = True
                return Mt5SessionCycleResult(
                    kind=Mt5SessionCycleKind.RECONCILIATION_REQUIRED,
                    current_time=now,
                    position_id=position_ticket,
                    reason="close_outcome_reconciliation_required",
                    message="entry-close outcome could not be reconciled",
                )

            close_correlation = EventCorrelation(
                client_order_id=record.client_order_id,
                broker_order_id=record.broker_order_id,
                position_id=position_ticket,
                close_order_id=close_order_id,
            )
            self.event_ledger.append(
                AuditEventType.POSITION_CLOSED,
                occurred_at=now,
                component=AuditComponent.BROKER_ADAPTER,
                correlation=close_correlation,
                payload={
                    "symbol": MT5_DEMO_SYMBOL,
                    "position_id": position_ticket,
                    "close_order_id": close_order_id,
                    "close_deal_id": close_deal_id,
                    "exit_reason": exit_reason,
                    "realized_pnl": realized_pnl,
                    "reconciliation": "ENTRY_FILLED_THEN_NATIVE_PROTECTIVE_EXIT",
                },
            )
            return Mt5SessionCycleResult(
                kind=Mt5SessionCycleKind.POSITION_CLOSED,
                current_time=now,
                tick=tick,
                signal=signal,
                execution=exec_result,
                position_id=position_ticket,
                close_order_id=close_order_id,
                close_deal_id=close_deal_id,
                exit_reason=exit_reason,
                reason="closed_at_entry",
                message="position filled and natively closed at entry",
            )

        with self._lock:
            self._active_position_id = position_ticket
            self._active_client_order_id = record.client_order_id
            self._active_broker_order_id = record.broker_order_id

        return Mt5SessionCycleResult(
            kind=Mt5SessionCycleKind.PROCESSED,
            current_time=now,
            tick=tick,
            signal=signal,
            execution=exec_result,
            position_id=position_ticket,
            reason="order_submitted_and_opened",
            message="position opened and actively monitored",
        )

    def _now(self) -> datetime:
        if callable(self.clock):
            val = self.clock()
            if isinstance(val, datetime) and val.tzinfo is not None:
                return val.astimezone(UTC)
        return datetime.now(UTC)

    def _append_runtime_transition(
        self,
        previous: RuntimeState,
        current: RuntimeState,
        reason: RuntimeControlReason,
        occurred_at: datetime,
        *,
        operator_requested: bool,
    ) -> None:
        self.event_ledger.append(
            AuditEventType.RUNTIME_STATE_CHANGED,
            occurred_at=occurred_at,
            component=AuditComponent.PAPER_SESSION,
            payload={
                "previous_state": previous.value,
                "current_state": current.value,
                "reason": reason.value,
                "operator_requested": operator_requested,
            },
        )
