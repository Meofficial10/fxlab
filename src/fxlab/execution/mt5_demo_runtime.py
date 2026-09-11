"""Operator-controlled MetaTrader 5 Demo execution runtime (Phase 1C)."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..risk.engine import RiskEngine, RiskLimits
from .broker_capabilities import BrokerCapability
from .event_ledger import (
    AuditComponent,
    AuditEventType,
    EventCorrelation,
    EventLedger,
)
from .mt5_demo_broker import (
    _MT5_DEMO_RESOLVER,
    MT5_DEMO_SYMBOL,
    Mt5DemoBroker,
    _minimum_volume,
    _protective_stop_buy,
    _protective_stop_sell,
    _query,
    _require_clean_symbol_state,
    _validated_symbol,
    _verified_demo_authority,
)
from .order_manager import ExecutionIntent, ExecutionResultKind, OrderManager
from .signal_engine import SignalEvent

MT5_DEMO_EXECUTION_CONFIRMATION = "I_AUTHORIZE_MT5_DEMO_EXECUTION"


def _redacted_account(account: object) -> str:
    login = getattr(account, "login", None)
    if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
        raise RuntimeError("mt5_account_incompatible")
    return f"****{str(login)[-4:]}"


def _operator_comment(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"fxlab-op-{digest}"


@dataclass(frozen=True, slots=True)
class Mt5DemoExecutionResult:
    status: str
    account: str
    symbol: str
    side: str
    volume: float
    entry_order_id: str
    entry_deal_id: str
    position_id: str
    close_order_id: str
    close_deal_id: str


@dataclass(slots=True)
class Mt5DemoExecutionRunner:
    """Run one explicit operator-authorized demo execution through the runtime."""

    broker: Mt5DemoBroker = field(default_factory=Mt5DemoBroker)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def run(
        self,
        *,
        confirmation: str,
        ledger: EventLedger,
        side: str = "buy",
        max_loss_usd: float,
    ) -> Mt5DemoExecutionResult:
        if confirmation != MT5_DEMO_EXECUTION_CONFIRMATION:
            raise ValueError("mt5_demo_execution_confirmation_required")
        if not isinstance(ledger, EventLedger) or ledger.durable_store is None:
            raise ValueError("mt5_durable_audit_required")
        if side not in ("buy", "sell"):
            raise ValueError("invalid_order_side")
        if (
            not isinstance(max_loss_usd, (int, float))
            or isinstance(max_loss_usd, bool)
            or not math.isfinite(max_loss_usd)
            or max_loss_usd <= 0
        ):
            raise ValueError("invalid_max_loss_usd")

        initialized = False
        try:
            self.broker.connect()
            initialized = True
            self.broker.subscribe_market_data([MT5_DEMO_SYMBOL])

            terminal, account = _verified_demo_authority(self.broker.api)
            masked_account = _redacted_account(account)
            correlation = EventCorrelation(client_order_id=_operator_comment(ledger.session_id))

            ledger.append(
                AuditEventType.ACCOUNT_OBSERVED,
                occurred_at=self._now(),
                component=AuditComponent.BROKER_ADAPTER,
                correlation=correlation,
                payload={
                    "environment": "demo",
                    "account": masked_account,
                    "currency": "USD",
                    "hedging_enabled": True,
                    "account_trading_enabled": True,
                    "expert_trading_enabled": True,
                    "terminal_trading_enabled": True,
                },
            )
            ledger.append(
                AuditEventType.OPERATOR_CONTROL_ACTION,
                occurred_at=self._now(),
                component=AuditComponent.ORDER_MANAGER,
                correlation=correlation,
                payload={"action": "authorized_demo_execution", "confirmation": "accepted"},
            )

            _require_clean_symbol_state(self.broker.api)
            metadata = _validated_symbol(self.broker.api)
            volume = _minimum_volume(metadata)
            tick = self.broker.get_latest_tick(MT5_DEMO_SYMBOL)
            if tick is None:
                raise RuntimeError("mt5_smoke_quote_invalid")

            if side == "buy":
                sl_price = _protective_stop_buy(
                    metadata,
                    tick.bid,
                    tick.ask,
                    max_loss_usd=max_loss_usd,
                    volume=volume,
                )
                direction = 1
            else:
                sl_price = _protective_stop_sell(
                    metadata,
                    tick.bid,
                    tick.ask,
                    max_loss_usd=max_loss_usd,
                    volume=volume,
                )
                direction = -1

            ledger.append(
                AuditEventType.BROKER_CAPABILITIES_BOUND,
                occurred_at=self._now(),
                component=AuditComponent.BROKER_ADAPTER,
                correlation=correlation,
                payload={
                    "symbol": MT5_DEMO_SYMBOL,
                    "side": side,
                    "volume": volume,
                    "stop_loss": sl_price,
                    "max_loss_usd": max_loss_usd,
                    "broker_id": self.broker.broker_descriptor.broker_id,
                },
            )

            risk_engine = RiskEngine(
                RiskLimits(
                    starting_equity=float(getattr(account, "equity", 10000.0)),
                    max_open_positions=1,
                    max_exposure_per_symbol_lots=volume,
                ),
                pip_size_resolver=_MT5_DEMO_RESOLVER,
                lot_step=volume,
            )
            order_manager = OrderManager(
                broker=self.broker,
                risk_engine=risk_engine,
                event_ledger=ledger,
                required_capabilities=frozenset(
                    {
                        BrokerCapability.MARKET_ORDERS,
                        BrokerCapability.NATIVE_SL_TP,
                    }
                ),
            )

            signal_time = tick.timestamp
            signal = SignalEvent(
                setup_name="operator_demo_execution",
                symbol=MT5_DEMO_SYMBOL,
                timeframe="M1",
                side=direction,
                signal_time=signal_time,
                signal_bar_index=0,
            )
            intent = ExecutionIntent(signal=signal, sl_price=sl_price)

            execution_time = self._now()
            if execution_time < signal_time:
                execution_time = signal_time

            exec_result = order_manager.submit(intent, current_time=execution_time)
            if exec_result.kind != ExecutionResultKind.SUBMITTED:
                raise RuntimeError(f"mt5_execution_failed:{exec_result.reason}")

            record = exec_result.record
            if record is None or record.broker_order_id is None:
                raise RuntimeError("mt5_entry_reconciliation_required")

            order_status = self.broker.get_order_status(record.client_order_id)
            entry_deal_id = str(order_status.get("deal_id", ""))
            position_ticket = str(order_status.get("position_id", ""))
            if not position_ticket:
                raise RuntimeError("mt5_entry_reconciliation_required")

            entry_correlation = EventCorrelation(
                client_order_id=record.client_order_id,
                broker_order_id=record.broker_order_id,
                position_id=position_ticket,
            )
            ledger.append(
                AuditEventType.POSITION_OPENED,
                occurred_at=self._now(),
                component=AuditComponent.BROKER_ADAPTER,
                correlation=entry_correlation,
                payload={
                    "symbol": MT5_DEMO_SYMBOL,
                    "side": side,
                    "volume": record.request.size,
                    "entry_order_id": record.broker_order_id,
                    "entry_deal_id": entry_deal_id,
                    "position_id": position_ticket,
                },
            )

            if order_status.get("closed_at_entry_reconciliation") is True:
                # Position exited natively by SL/TP before active position check
                close_order_id = str(order_status.get("close_order_id", ""))
                close_deal_id = str(order_status.get("close_deal_id", ""))
                exit_reason = str(order_status.get("exit_reason", "SL"))

                close_correlation = EventCorrelation(
                    client_order_id=record.client_order_id,
                    broker_order_id=record.broker_order_id,
                    position_id=position_ticket,
                    close_order_id=close_order_id,
                )
                ledger.append(
                    AuditEventType.POSITION_CLOSED,
                    occurred_at=self._now(),
                    component=AuditComponent.BROKER_ADAPTER,
                    correlation=close_correlation,
                    payload={
                        "symbol": MT5_DEMO_SYMBOL,
                        "position_id": position_ticket,
                        "close_order_id": close_order_id,
                        "close_deal_id": close_deal_id,
                        "exit_reason": exit_reason,
                        "reconciliation": "ENTRY_FILLED_THEN_NATIVE_PROTECTIVE_EXIT",
                    },
                )

                return Mt5DemoExecutionResult(
                    status="ENTRY_FILLED_THEN_NATIVE_PROTECTIVE_EXIT",
                    account=masked_account,
                    symbol=MT5_DEMO_SYMBOL,
                    side=side,
                    volume=record.request.size,
                    entry_order_id=record.broker_order_id,
                    entry_deal_id=entry_deal_id,
                    position_id=position_ticket,
                    close_order_id=close_order_id,
                    close_deal_id=close_deal_id,
                )

            # Normal path: position active and correlated
            close_order_id, close_deal_id = self.broker.close_position(position_ticket)
            if not close_order_id or not close_deal_id:
                raise RuntimeError("mt5_close_reconciliation_required")

            remaining = _query(
                self.broker.api,
                "positions_get",
                "mt5_close_reconciliation_required",
                ticket=int(position_ticket),
            )
            if len(remaining) != 0:
                raise RuntimeError("mt5_close_reconciliation_required")

            close_correlation = EventCorrelation(
                client_order_id=record.client_order_id,
                broker_order_id=record.broker_order_id,
                position_id=position_ticket,
                close_order_id=close_order_id,
            )
            ledger.append(
                AuditEventType.POSITION_CLOSED,
                occurred_at=self._now(),
                component=AuditComponent.BROKER_ADAPTER,
                correlation=close_correlation,
                payload={
                    "symbol": MT5_DEMO_SYMBOL,
                    "position_id": position_ticket,
                    "close_order_id": close_order_id,
                    "close_deal_id": close_deal_id,
                },
            )

            return Mt5DemoExecutionResult(
                status="successful_round_trip",
                account=masked_account,
                symbol=MT5_DEMO_SYMBOL,
                side=side,
                volume=record.request.size,
                entry_order_id=record.broker_order_id,
                entry_deal_id=entry_deal_id,
                position_id=position_ticket,
                close_order_id=close_order_id,
                close_deal_id=close_deal_id,
            )
        finally:
            if initialized:
                self.broker.disconnect()

    def _now(self) -> datetime:
        if not callable(self.clock):
            raise RuntimeError("mt5_smoke_quote_invalid")
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("mt5_smoke_quote_invalid")
        return value.astimezone(UTC)
