"""Thin application assembly for deterministic, observation-only paper replay."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxlab import __version__

from ..config import AppConfig
from ..data.provider import (
    BarQuery,
    CanonicalInstrument,
    ProviderCapability,
    ProviderDescriptor,
    ProviderRoute,
)
from ..data.providers import LocalParquetProvider, ProviderGateway, ProviderRegistry
from ..risk.engine import RiskEngine, RiskLimits
from .durable_event_store import DurableStoreError, SQLiteEventStore
from .event_ledger import AuditEvent, AuditEventType, EventLedger
from .margin import UnmodeledPaperMargin
from .market_data import MarketDataStream
from .monitoring import (
    MonitoringResult,
    MonitoringSource,
    monitoring_to_dict,
    project_audit_events,
    project_recovered_session,
)
from .order_manager import ExecutionIntent, OrderManager
from .paper_broker import PaperBroker
from .paper_session import (
    CycleKind,
    HistoricalBarReplay,
    MarketContext,
    PaperTradingSession,
)
from .recovery import (
    RecoveryResult,
    RecoveryState,
    UnsafeCheckpointError,
    create_checkpoint,
    recover,
)
from .runtime_control import RuntimeState
from .signal_engine import SignalEngine, SignalEvent
from .valuation import ValuationFailure, approved_fx_instrument_catalog

OBSERVE_ONLY_POLICY_ID = "observe-only-v1"
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class AppExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    RUNTIME_FAILURE = 3
    RECONCILIATION_REQUIRED = 4
    RECOVERY_FAILURE = 5
    INTERRUPTED = 130


class PaperAppError(RuntimeError):
    """Sanitized application-layer failure with a stable process exit code."""

    def __init__(self, reason: str, message: str, exit_code: AppExitCode) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class ReplayRequest:
    session_id: str
    store_path: Path | str
    data_dir: Path | str
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    as_of: datetime
    observe_only: bool

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not _SESSION_ID.fullmatch(
            self.session_id.strip()
        ):
            raise ValueError("session_id must be a safe non-empty identifier")
        if self.observe_only is not True:
            raise ValueError("Phase 14 replay requires explicit observation-only mode")
        instrument = CanonicalInstrument(self.symbol)
        query = BarQuery(instrument, self.timeframe, self.start, self.end, self.as_of)
        object.__setattr__(self, "session_id", self.session_id.strip())
        object.__setattr__(self, "store_path", Path(self.store_path))
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "symbol", instrument.symbol)
        object.__setattr__(self, "start", query.start)
        object.__setattr__(self, "end", query.end)
        object.__setattr__(self, "as_of", query.as_of)

    @property
    def query(self) -> BarQuery:
        return BarQuery(
            CanonicalInstrument(self.symbol),
            self.timeframe,
            self.start,
            self.end,
            self.as_of,
        )


@dataclass(frozen=True)
class PaperAppResult:
    exit_code: AppExitCode
    state: str
    reason: str
    session_id: str
    cycles: int = 0
    checkpoint_sequence: int | None = None


@dataclass
class PaperApplication:
    session: PaperTradingSession
    store: SQLiteEventStore
    execution_policy_id: str = OBSERVE_ONLY_POLICY_ID
    software_version: str = __version__

    def close(self) -> None:
        self.store.close()


@dataclass(frozen=True)
class RecoveredSnapshot:
    recovery: RecoveryResult
    label: str
    status: dict[str, object]
    orders: tuple[dict[str, object], ...]
    positions: tuple[dict[str, object], ...]


class _NoSignalSetup:
    name = "observation_only"

    def generate(self, bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        return np.array([], dtype=int), np.array([], dtype=int)


def _decline_only_policy(
    signal: SignalEvent, market_context: MarketContext
) -> ExecutionIntent | None:
    del signal, market_context
    return None


def assemble_observation_replay(
    request: ReplayRequest,
    config: AppConfig,
    *,
    fresh: bool,
    runtime_id: str = "foreground-1",
) -> PaperApplication:
    """Build the real paper component graph without enabling any trading policy."""
    if not isinstance(request, ReplayRequest) or not isinstance(config, AppConfig):
        raise ValueError("validated replay request and application config are required")
    path = request.store_path
    if fresh and path.exists() and path.stat().st_size > 0:
        raise PaperAppError(
            "store_already_initialized",
            "fresh replay refuses an existing initialized store",
            AppExitCode.USAGE,
        )
    if not fresh and (not path.exists() or path.stat().st_size == 0):
        raise PaperAppError(
            "store_missing", "recovery store does not exist", AppExitCode.RECOVERY_FAILURE
        )

    descriptor = ProviderDescriptor(
        "local-parquet",
        "1",
        frozenset(
            {ProviderCapability.HISTORICAL_BARS, ProviderCapability.POINT_IN_TIME}
        ),
        supported_symbols=frozenset({CanonicalInstrument(request.symbol)}),
        supported_timeframes=frozenset({request.timeframe}),
        deterministic=True,
        normalization_version="1",
    )
    registry = ProviderRegistry()
    registry.register(LocalParquetProvider(descriptor, request.data_dir))
    registry.freeze()
    route = ProviderRoute(
        descriptor.provider_id,
        ProviderCapability.HISTORICAL_BARS,
        normalization_version=descriptor.normalization_version,
    )
    try:
        dataset = ProviderGateway(registry).fetch_bars(route, request.query)
    except Exception as exc:
        raise PaperAppError(
            "provider_preflight_failed",
            "local replay dataset failed provider validation",
            AppExitCode.USAGE if fresh else AppExitCode.RECOVERY_FAILURE,
        ) from exc

    frame = dataset.frame
    replay = HistoricalBarReplay(
        {request.symbol: frame},
        request.timeframe,
        provider_id=descriptor.provider_id,
        provider_version=descriptor.implementation_version,
        normalization_version=descriptor.normalization_version,
        provenance_quality=dataset.provenance.provenance_quality,
    )
    catalog = approved_fx_instrument_catalog()
    try:
        catalog.specification(request.symbol)
    except ValuationFailure as exc:
        raise PaperAppError(
            "instrument_unsupported",
            "symbol is outside the approved Phase 18 FX instrument catalog",
            AppExitCode.USAGE if fresh else AppExitCode.RECOVERY_FAILURE,
        ) from exc
    broker = PaperBroker(
        account_currency="USD",
        instrument_catalog=catalog,
        valuation_max_age=timedelta(minutes=5),
        valuation_policy_version="fx-point-in-time-v1",
        margin_model=UnmodeledPaperMargin("USD"),
        commission_currency="USD",
        initial_balance=config.risk.starting_equity,
        historical_bars={(request.symbol, request.timeframe): frame},
        cost_config=config.costs,
    )
    risk = RiskEngine(
        RiskLimits(
            max_risk_per_trade_pct=config.risk.max_risk_per_trade_pct,
            max_daily_loss_pct=config.risk.max_daily_loss_pct,
            max_consecutive_losses=config.risk.max_consecutive_losses,
            max_drawdown_pct=config.risk.max_drawdown_pct,
            max_trades_per_day=config.risk.max_trades_per_day,
            starting_equity=config.risk.starting_equity,
            ruin_threshold_pct=config.risk.ruin_threshold_pct,
        ),
        pip_size_resolver=config.costs,
        lot_step=0.01,
    )
    try:
        store = SQLiteEventStore(path, request.session_id)
        ledger = EventLedger(request.session_id, durable_store=store)
    except (DurableStoreError, OSError, ValueError) as exc:
        raise PaperAppError(
            "store_open_failed",
            "durable paper store could not be opened safely",
            AppExitCode.USAGE if fresh else AppExitCode.RECOVERY_FAILURE,
        ) from exc
    market = MarketDataStream(
        broker, [request.symbol], time_provider=lambda: request.as_of
    )
    signals = SignalEngine(_NoSignalSetup(), market, request.timeframe)
    manager = OrderManager(broker, risk, ledger)
    session = PaperTradingSession(
        broker,
        replay,
        market,
        signals,
        manager,
        risk,
        _decline_only_policy,
        ledger,
        runtime_id=runtime_id,
    )
    return PaperApplication(session, store)


def run_foreground_replay(request: ReplayRequest, config: AppConfig) -> PaperAppResult:
    app = assemble_observation_replay(request, config, fresh=True)
    cycles = 0
    checkpoint_sequence: int | None = None
    started = False
    interrupted = False
    outcome = PaperAppResult(
        AppExitCode.RUNTIME_FAILURE, "failed", "runtime_failed", request.session_id
    )
    try:
        app.session.start()
        started = True
        while True:
            cycle = app.session.poll_once()
            if cycle.kind is CycleKind.EXHAUSTED:
                outcome = PaperAppResult(
                    AppExitCode.SUCCESS,
                    "exhausted",
                    cycle.reason,
                    request.session_id,
                    cycles,
                    checkpoint_sequence,
                )
                break
            cycles += 1
            status = app.session.runtime_status()
            if status.state is RuntimeState.RECONCILIATION_REQUIRED:
                outcome = PaperAppResult(
                    AppExitCode.RECONCILIATION_REQUIRED,
                    status.state.value,
                    status.reason.value if status.reason else "reconciliation_required",
                    request.session_id,
                    cycles,
                    checkpoint_sequence,
                )
                break
            if cycle.kind is CycleKind.FAILED:
                outcome = PaperAppResult(
                    AppExitCode.RUNTIME_FAILURE,
                    status.state.value,
                    cycle.reason or "runtime_failed",
                    request.session_id,
                    cycles,
                    checkpoint_sequence,
                )
                break
            if status.state in {RuntimeState.FAILED, RuntimeState.STOPPING, RuntimeState.STOPPED}:
                outcome = PaperAppResult(
                    AppExitCode.RUNTIME_FAILURE,
                    status.state.value,
                    status.reason.value if status.reason else cycle.reason,
                    request.session_id,
                    cycles,
                    checkpoint_sequence,
                )
                break
            checkpoint = create_checkpoint(
                app.session,
                app.store,
                software_version=app.software_version,
                execution_policy_id=app.execution_policy_id,
            )
            checkpoint_sequence = checkpoint.last_event_sequence
    except KeyboardInterrupt:
        interrupted = True
    except UnsafeCheckpointError as exc:
        outcome = PaperAppResult(
            AppExitCode.RUNTIME_FAILURE,
            "failed",
            str(exc),
            request.session_id,
            cycles,
            checkpoint_sequence,
        )
    except Exception:
        outcome = PaperAppResult(
            AppExitCode.RUNTIME_FAILURE,
            "failed",
            "runtime_failed",
            request.session_id,
            cycles,
            checkpoint_sequence,
        )
    finally:
        if started:
            try:
                app.session.request_stop()
                app.session.complete_stop(
                    checkpoint_store=app.store,
                    software_version=app.software_version,
                    execution_policy_id=app.execution_policy_id,
                )
                latest = app.store.load_latest_checkpoint()
                checkpoint_sequence = (
                    latest.last_event_sequence if latest is not None else checkpoint_sequence
                )
            except Exception:
                if app.session.recovery_required:
                    outcome = PaperAppResult(
                        AppExitCode.RECONCILIATION_REQUIRED,
                        "reconciliation_required",
                        "reconciliation_required",
                        request.session_id,
                        cycles,
                        checkpoint_sequence,
                    )
                elif outcome.exit_code is AppExitCode.SUCCESS:
                    outcome = PaperAppResult(
                        AppExitCode.RUNTIME_FAILURE,
                        "failed",
                        "shutdown_failed",
                        request.session_id,
                        cycles,
                        checkpoint_sequence,
                    )
        app.close()
    if interrupted:
        code = (
            AppExitCode.RECONCILIATION_REQUIRED
            if app.session.recovery_required
            else AppExitCode.INTERRUPTED
        )
        return PaperAppResult(
            code,
            "interrupted",
            "operator_interrupted",
            request.session_id,
            cycles,
            checkpoint_sequence,
        )
    return PaperAppResult(
        outcome.exit_code,
        outcome.state,
        outcome.reason,
        outcome.session_id,
        outcome.cycles,
        checkpoint_sequence,
    )


def recover_snapshot(request: ReplayRequest, config: AppConfig) -> RecoveredSnapshot:
    app = assemble_observation_replay(request, config, fresh=False)
    try:
        result = recover(
            app.session,
            app.store,
            software_version=app.software_version,
            execution_policy_id=app.execution_policy_id,
        )
        status = _status_snapshot(app, result)
        return RecoveredSnapshot(
            result,
            "RECOVERED SNAPSHOT" if result.recovered else "CHECKPOINT STATE",
            status,
            _orders_snapshot(app),
            _positions_snapshot(app),
        )
    finally:
        app.close()


def inspect_events(
    store_path: Path | str,
    session_id: str,
    *,
    event_type: AuditEventType | None = None,
    limit: int | None = None,
) -> tuple[dict[str, object], ...]:
    path = Path(store_path)
    if not _SESSION_ID.fullmatch(session_id.strip()):
        raise ValueError("session_id must be a safe non-empty identifier")
    if not path.exists() or path.stat().st_size == 0:
        raise PaperAppError(
            "store_missing", "event store does not exist", AppExitCode.RECOVERY_FAILURE
        )
    if event_type is not None and not isinstance(event_type, AuditEventType):
        raise ValueError("event_type must be an AuditEventType")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    try:
        store = SQLiteEventStore(path, session_id)
    except (DurableStoreError, OSError, ValueError) as exc:
        raise PaperAppError(
            "store_integrity_failed",
            "durable event store could not be verified",
            AppExitCode.RECOVERY_FAILURE,
        ) from exc
    try:
        try:
            store.verify_integrity()
            events = store.load_events()
        except (DurableStoreError, OSError, ValueError) as exc:
            raise PaperAppError(
                "store_integrity_failed",
                "durable event store could not be verified",
                AppExitCode.RECOVERY_FAILURE,
            ) from exc
        if event_type is not None:
            events = tuple(item for item in events if item.event_type is event_type)
        if limit is not None:
            events = events[-limit:]
        return tuple(_event_snapshot(event) for event in events)
    finally:
        store.close()


def monitor_recovered(
    request: ReplayRequest,
    config: AppConfig,
    *,
    event_limit: int = 10,
) -> MonitoringResult:
    """Build one verified read-only operational snapshot from durable state."""
    if isinstance(event_limit, bool) or not isinstance(event_limit, int) or event_limit < 1:
        raise ValueError("event_limit must be a positive integer")
    app = assemble_observation_replay(request, config, fresh=False)
    try:
        result = recover(
            app.session,
            app.store,
            software_version=app.software_version,
            execution_policy_id=app.execution_policy_id,
        )
        if result.state is RecoveryState.FAILED:
            return MonitoringResult(
                False,
                MonitoringSource.UNAVAILABLE,
                None,
                result.reason,
                result.message,
            )
        try:
            app.store.verify_integrity()
            events = app.store.load_events()
        except (DurableStoreError, OSError, ValueError) as exc:
            raise PaperAppError(
                "store_integrity_failed",
                "durable event store could not be verified",
                AppExitCode.RECOVERY_FAILURE,
            ) from exc
        snapshot = project_recovered_session(
            app.session,
            result,
            latest_event_sequence=app.store.last_sequence(),
            recent_events=project_audit_events(events, limit=event_limit),
        )
        return MonitoringResult(True, snapshot.source, snapshot)
    finally:
        app.close()


def monitoring_result_to_dict(result: MonitoringResult) -> dict[str, object]:
    """Serialize an explicit monitoring result without generic traversal."""
    if result.snapshot is None:
        return {
            "available": result.available,
            "source": result.source.value,
            "reason": result.reason,
            "message": result.message,
        }
    return {"available": result.available, **monitoring_to_dict(result.snapshot)}


def _status_snapshot(app: PaperApplication, result: RecoveryResult) -> dict[str, object]:
    runtime = app.session.runtime_status()
    replay_state = app.session.replay.snapshot_state()
    account = app.session.broker.get_account_info()
    risk = app.session.risk_engine.snapshot_state()
    orders = app.session.order_manager.snapshot_state()["records"]
    return {
        "session_id": app.store.session_id,
        "recovery_state": result.state.value,
        "recovery_reason": result.reason,
        "checkpoint_sequence": result.checkpoint_sequence,
        "latest_event_sequence": app.store.last_sequence(),
        "runtime_state": runtime.state.value,
        "replay_cursor": replay_state["cursor"],
        "last_consumed_timestamp": replay_state["last_consumed_timestamp"],
        "balance": account.balance,
        "equity": account.equity,
        "open_position_count": len(account.open_positions),
        "order_count": len(orders),
        "kill_switch_active": risk["kill_switch_active"],
        "kill_switch_reason": risk["kill_switch_reason"],
        "reservation_count": len(risk["reservations"]),
        "reconciliation_required": app.session.recovery_required,
    }


def _orders_snapshot(app: PaperApplication) -> tuple[dict[str, object], ...]:
    records = app.session.order_manager.snapshot_state()["records"]
    return tuple(
        {
            "client_order_id": item["client_order_id"],
            "broker_order_id": item["broker_order_id"],
            "symbol": item["request"]["symbol"],
            "side": item["request"]["side"],
            "size": item["request"]["size"],
            "status": item["status"],
            "reservation_released": item["reservation_released"],
        }
        for item in records
    )


def _positions_snapshot(app: PaperApplication) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "side": position.side,
            "size": position.size,
            "entry_price": position.entry_price,
            "entry_time": position.entry_time.astimezone(UTC).isoformat(),
            "unrealized_pnl": position.unrealized_pnl,
        }
        for position in app.session.broker.get_account_info().open_positions
    )


def _event_snapshot(event: AuditEvent) -> dict[str, object]:
    correlation = event.correlation
    return {
        "session_id": event.session_id,
        "sequence": event.sequence,
        "timestamp": event.occurred_at.astimezone(UTC).isoformat(),
        "event_type": event.event_type.value,
        "component": event.component.value,
        "correlation": {
            "signal_id": correlation.signal_id,
            "client_order_id": correlation.client_order_id,
            "broker_order_id": correlation.broker_order_id,
            "position_id": correlation.position_id,
            "close_order_id": correlation.close_order_id,
        },
        "payload": _plain(event.payload),
    }


def _plain(value: object) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _plain(item) for key, item in value.items()}  # type: ignore[union-attr]
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value
