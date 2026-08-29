"""Operational configuration and service orchestration contracts."""

from __future__ import annotations

import json
import os
import re
import signal
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock, current_thread, main_thread
from types import TracebackType
from typing import IO

from fxlab.config import AppConfig
from fxlab.execution.app import (
    AppExitCode,
    PaperAppError,
    PaperApplication,
    ReplayRequest,
    assemble_observation_replay,
)
from fxlab.execution.monitoring import monitoring_to_dict
from fxlab.execution.paper_session import CycleKind
from fxlab.execution.recovery import RecoveryState, recover
from fxlab.execution.runtime_control import RuntimeState

from .control import ServiceState
from .security import is_safe_local_absolute_path

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_LOG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.jsonl$")
_CONFIG_FIELDS = {
    "format_version",
    "state_directory",
    "runtime_id",
    "operator_id",
    "control_secret_file",
    "endpoint_id",
    "log_filename",
}


@dataclass(frozen=True, slots=True)
class OperationalConfig:
    format_version: int
    state_directory: Path
    runtime_id: str
    operator_id: str
    control_secret_file: Path
    endpoint_id: str
    log_filename: str

    def __post_init__(self) -> None:
        if self.format_version != 1:
            raise ValueError("unsupported operational configuration version")
        state = Path(self.state_directory)
        secret = Path(self.control_secret_file)
        if not is_safe_local_absolute_path(state):
            raise ValueError("state_directory must be an absolute local path without aliases")
        if not is_safe_local_absolute_path(secret):
            raise ValueError("control_secret_file must be an absolute local path without aliases")
        for name in ("runtime_id", "operator_id", "endpoint_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
                raise ValueError(f"{name} must be a safe identifier")
        if not isinstance(self.log_filename, str) or not _SAFE_LOG.fullmatch(
            self.log_filename
        ):
            raise ValueError("log_filename must be a safe .jsonl filename")
        object.__setattr__(self, "state_directory", state)
        object.__setattr__(self, "control_secret_file", secret)

    @property
    def store_path(self) -> Path:
        return self.state_directory / f"{self.runtime_id}.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_directory / f"{self.runtime_id}.lock"

    @property
    def log_path(self) -> Path:
        return self.state_directory / self.log_filename

    @property
    def socket_path(self) -> Path:
        return self.state_directory / f"{self.endpoint_id}.sock"

    @property
    def control_family(self) -> str:
        return "AF_PIPE" if os.name == "nt" else "AF_UNIX"

    @property
    def control_address(self) -> str:
        if os.name == "nt":
            return rf"\\.\pipe\fxlab-{self.endpoint_id}"
        return str(self.socket_path)


class InstanceLock:
    """Held OS lock; persistent file contents are informational only."""

    def __init__(self, path: Path | str, runtime_id: str) -> None:
        self.path = Path(path)
        self.runtime_id = runtime_id
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            metadata = json.dumps(
                {"pid": os.getpid(), "runtime_id": self.runtime_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            handle.seek(1)
            handle.truncate()
            handle.write(metadata)
            handle.flush()
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError("service instance is already running") from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.release()


class OperationalLogger:
    """Synchronous allow-listed JSONL diagnostics, separate from audit."""

    _SEVERITIES = {"debug", "info", "warning", "error", "critical"}

    def __init__(self, path: Path | str, *, runtime_id: str, session_id: str) -> None:
        self.path = Path(path)
        self.runtime_id = runtime_id
        self.session_id = session_id
        self._handle: IO[str] | None = None

    def open(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def write(
        self,
        *,
        severity: str,
        reason_code: str,
        service_state: str,
        actor_id: str | None = None,
        action: str | None = None,
        result: str | None = None,
        failure_category: str | None = None,
        **unexpected: object,
    ) -> None:
        if unexpected:
            raise ValueError("unsupported operational log fields")
        if self._handle is None:
            raise RuntimeError("operational logger is not open")
        if severity not in self._SEVERITIES:
            raise ValueError("invalid operational log severity")
        values = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": severity,
            "reason_code": _safe_log_value(reason_code, "reason_code"),
            "service_state": _safe_log_value(service_state, "service_state"),
            "runtime_id": _safe_log_value(self.runtime_id, "runtime_id"),
            "session_id": _safe_log_value(self.session_id, "session_id"),
        }
        for key, value in (
            ("actor_id", actor_id),
            ("action", action),
            ("result", result),
            ("failure_category", failure_category),
        ):
            if value is not None:
                values[key] = _safe_log_value(value, key)
        try:
            self._handle.write(
                json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n"
            )
            self._handle.flush()
        except OSError as exc:
            raise RuntimeError("operational log write failed") from exc

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class StartupMode(StrEnum):
    FRESH = "fresh"
    RECOVER = "recover"


@dataclass(frozen=True, slots=True)
class ServiceResult:
    exit_code: int
    service_state: ServiceState
    reason: str
    session_id: str
    cycles: int = 0


class _ServiceFailure(RuntimeError):
    def __init__(self, exit_code: AppExitCode, reason: str) -> None:
        super().__init__(reason)
        self.exit_code = exit_code
        self.reason = reason


class ObservationService:
    """Foreground observation-only PaperBroker service with local controls."""

    def __init__(
        self,
        config: OperationalConfig,
        request: ReplayRequest,
        app_config: AppConfig,
        startup_mode: StartupMode,
    ) -> None:
        if not isinstance(config, OperationalConfig):
            raise ValueError("validated operational configuration is required")
        if not isinstance(request, ReplayRequest) or request.observe_only is not True:
            raise ValueError("service requires an observation-only replay request")
        if not isinstance(app_config, AppConfig):
            raise ValueError("validated application configuration is required")
        if not isinstance(startup_mode, StartupMode):
            raise ValueError("exactly one explicit startup mode is required")
        if request.store_path.resolve() != config.store_path.resolve():
            raise ValueError("replay store must be the derived operational store")
        self.config = config
        self.request = request
        self.app_config = app_config
        self.startup_mode = startup_mode
        self._state = ServiceState.STARTING
        self._state_lock = Lock()
        self._stop_requested = Event()
        self._application: PaperApplication | None = None
        self._logger: OperationalLogger | None = None
        self._control_server: object | None = None
        self._last_events: tuple[object, ...] = ()
        self._critical_reason: str | None = None
        self._shutdown_reason: str | None = None
        self._session_activated = False

    @property
    def state(self) -> ServiceState:
        with self._state_lock:
            return self._state

    @property
    def last_events(self) -> tuple[object, ...]:
        return self._last_events

    def run(self) -> ServiceResult:
        from .control import LocalControlServer
        from .security import FileSecretResolver

        lock = InstanceLock(self.config.lock_path, self.config.runtime_id)
        outcome = ServiceResult(
            int(AppExitCode.RUNTIME_FAILURE),
            ServiceState.FAILED,
            "service_failed",
            self.request.session_id,
        )
        cycles = 0
        signal_handlers = self._install_signal_handlers()
        self._set_state(ServiceState.PREFLIGHT)
        try:
            self.config.state_directory.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.config.state_directory, 0o700)
            lock.acquire()
            self._prepare_control_endpoint()
            logger = OperationalLogger(
                self.config.log_path,
                runtime_id=self.config.runtime_id,
                session_id=self.request.session_id,
            )
            logger.open()
            self._logger = logger
            logger.write(
                severity="info",
                reason_code="service_preflight",
                service_state=self.state.value,
            )
            secret = FileSecretResolver().resolve(self.config.control_secret_file)
            fresh = self.startup_mode is StartupMode.FRESH
            application = assemble_observation_replay(
                self.request,
                self.app_config,
                fresh=fresh,
                runtime_id=self.config.runtime_id,
            )
            self._application = application
            server = LocalControlServer(
                self.config,
                secret,
                self._handle_control,
                failure_handler=lambda: self._critical_failure("control_service_failed"),
            )
            self._control_server = server
            if fresh:
                application.session.start()
                self._session_activated = True
            else:
                recovered = recover(
                    application.session,
                    application.store,
                    software_version=application.software_version,
                    execution_policy_id=application.execution_policy_id,
                )
                if recovered.state is not RecoveryState.RECOVERED:
                    code = (
                        AppExitCode.RECONCILIATION_REQUIRED
                        if recovered.state is RecoveryState.RECONCILIATION_REQUIRED
                        else AppExitCode.RECOVERY_FAILURE
                    )
                    raise _ServiceFailure(code, recovered.reason)
                runtime = application.session.runtime_status().state
                if runtime is RuntimeState.RUNNING:
                    application.session.pause()
                    runtime = application.session.runtime_status().state
                if runtime not in {RuntimeState.PAUSED, RuntimeState.KILL_SWITCHED}:
                    raise _ServiceFailure(
                        AppExitCode.RECOVERY_FAILURE,
                        f"recovered_{runtime.value}_blocked",
                    )
                application.session.activate_recovered_maintenance()
                self._session_activated = True
            self._set_state(ServiceState.RUNNING)
            server.start()
            logger.write(
                severity="info",
                reason_code="service_running",
                service_state=self.state.value,
            )
            reason = "replay_exhausted"
            exit_code = AppExitCode.SUCCESS
            while not self._stop_requested.is_set():
                cycle = application.session.poll_once()
                if cycle.kind is CycleKind.EXHAUSTED:
                    break
                cycles += 1
                status = application.session.runtime_status()
                if status.state is RuntimeState.RECONCILIATION_REQUIRED:
                    exit_code = AppExitCode.RECONCILIATION_REQUIRED
                    reason = "reconciliation_required"
                    break
                if cycle.kind is CycleKind.FAILED or status.state is RuntimeState.FAILED:
                    exit_code = AppExitCode.RUNTIME_FAILURE
                    reason = cycle.reason or "runtime_failed"
                    break
                if status.state in {RuntimeState.STOPPING, RuntimeState.STOPPED}:
                    reason = status.reason.value if status.reason else "service_stopping"
                    break
                application.session.create_safe_checkpoint(
                    application.store,
                    software_version=application.software_version,
                    execution_policy_id=application.execution_policy_id,
                )
            if self._critical_reason is not None:
                exit_code = AppExitCode.RUNTIME_FAILURE
                reason = self._critical_reason
            elif self._shutdown_reason is not None:
                reason = self._shutdown_reason
            outcome = ServiceResult(
                int(exit_code),
                ServiceState.STOPPED if exit_code is AppExitCode.SUCCESS else ServiceState.FAILED,
                reason,
                self.request.session_id,
                cycles,
            )
        except KeyboardInterrupt:
            outcome = ServiceResult(
                int(AppExitCode.INTERRUPTED),
                ServiceState.STOPPED,
                "operator_interrupted",
                self.request.session_id,
                cycles,
            )
        except PaperAppError as exc:
            outcome = ServiceResult(
                int(exc.exit_code), ServiceState.FAILED, exc.reason, self.request.session_id, cycles
            )
        except _ServiceFailure as exc:
            outcome = ServiceResult(
                int(exc.exit_code),
                ServiceState.FAILED,
                exc.reason,
                self.request.session_id,
                cycles,
            )
        except Exception:
            outcome = ServiceResult(
                int(AppExitCode.RUNTIME_FAILURE),
                ServiceState.FAILED,
                "service_failed",
                self.request.session_id,
                cycles,
            )
        finally:
            cleanup_reason = self._shutdown(outcome)
            try:
                lock.release()
            except Exception:
                cleanup_reason = cleanup_reason or "instance_lock_release_failed"
            try:
                self._restore_signal_handlers(signal_handlers)
            except Exception:
                cleanup_reason = cleanup_reason or "signal_restore_failed"
            if cleanup_reason is not None:
                outcome = ServiceResult(
                    int(AppExitCode.RUNTIME_FAILURE),
                    ServiceState.FAILED,
                    cleanup_reason,
                    self.request.session_id,
                    cycles,
                )
        self._set_state(outcome.service_state)
        return outcome

    def _handle_control(self, request: object) -> object:
        from .control import (
            CONTROL_PROTOCOL_VERSION,
            ControlAction,
            ControlRequest,
            ControlResponse,
            freeze_payload,
        )

        if not isinstance(request, ControlRequest):
            raise ValueError("validated control request is required")
        application = self._application
        if application is None:
            return ControlResponse(
                CONTROL_PROTOCOL_VERSION,
                request.request_id,
                False,
                False,
                self.state,
                None,
                "service_unavailable",
            )
        if request.action is ControlAction.STATUS:
            if self.state is not ServiceState.RUNNING:
                return ControlResponse(
                    CONTROL_PROTOCOL_VERSION,
                    request.request_id,
                    False,
                    False,
                    self.state,
                    None,
                    "service_not_ready",
                )
            snapshot = application.session.monitoring_snapshot()
            payload = monitoring_to_dict(snapshot)
            payload["service_state"] = self.state.value
            return ControlResponse(
                CONTROL_PROTOCOL_VERSION,
                request.request_id,
                True,
                False,
                self.state,
                application.session.runtime_status().state,
                "accepted",
                freeze_payload(payload),
            )
        if self.state is not ServiceState.RUNNING:
            status = application.session.runtime_status()
            application.session.audit_rejected_operator_control(
                request.action.value,
                actor_id=self.config.operator_id,
                request_id=request.request_id,
                reason="service_not_running",
            )
            return ControlResponse(
                CONTROL_PROTOCOL_VERSION,
                request.request_id,
                False,
                False,
                self.state,
                status.state,
                "service_not_running",
            )
        result = application.session.apply_operator_control(
            request.action.value,
            actor_id=self.config.operator_id,
            request_id=request.request_id,
        )
        if request.action is ControlAction.STOP and result.accepted:
            self._set_state(ServiceState.STOPPING)
            self._shutdown_reason = "operator_stop"
            self._stop_requested.set()
        try:
            self._log_control(request.action.value, result.accepted, result.changed)
        except RuntimeError:
            self._critical_failure("operational_log_failed")
        return ControlResponse(
            CONTROL_PROTOCOL_VERSION,
            request.request_id,
            result.accepted,
            result.changed,
            self.state,
            application.session.runtime_status().state,
            result.reason.value if result.reason else "accepted",
        )

    def _shutdown(self, outcome: ServiceResult) -> str | None:
        from .control import LocalControlServer

        self._set_state(ServiceState.STOPPING)
        failure: str | None = None

        def record(reason: str) -> None:
            nonlocal failure
            failure = failure or reason

        application = self._application
        if application is not None and self._session_activated:
            try:
                application.session.request_stop()
            except Exception:
                record("session_shutdown_failed")
            try:
                application.session.complete_stop(
                    checkpoint_store=application.store,
                    software_version=application.software_version,
                    execution_policy_id=application.execution_policy_id,
                )
            except Exception:
                record("session_shutdown_failed")
            try:
                self._last_events = application.session.event_ledger.events()
            except Exception:
                record("audit_snapshot_failed")
        server = self._control_server
        if isinstance(server, LocalControlServer):
            try:
                server.close()
            except Exception:
                record("control_shutdown_failed")
        if application is not None:
            try:
                application.close()
            except Exception:
                record("store_close_failed")
        if self._logger is not None:
            try:
                self._logger.write(
                    severity=(
                        "info" if outcome.exit_code == 0 and failure is None else "error"
                    ),
                    reason_code=failure or outcome.reason,
                    service_state=(
                        ServiceState.STOPPED.value
                        if outcome.service_state is ServiceState.STOPPED and failure is None
                        else ServiceState.FAILED.value
                    ),
                )
            except Exception:
                record("operational_log_failed")
            try:
                self._logger.close()
            except Exception:
                record("operational_log_failed")
        return failure

    def _critical_failure(self, reason: str) -> None:
        self._critical_reason = reason
        self._stop_requested.set()
        application = self._application
        if application is not None:
            try:
                application.session.request_stop()
            except Exception:
                pass

    def _request_signal_shutdown(self, _signum: int, _frame: object) -> None:
        self._shutdown_reason = "termination_signal"
        self._stop_requested.set()

    def _prepare_control_endpoint(self) -> None:
        """Remove only a proven-stale POSIX socket after the instance lock is held."""
        if os.name == "nt" or not self.config.socket_path.exists():
            return
        if not self.config.socket_path.is_socket():
            raise RuntimeError("control endpoint path is occupied")
        import socket

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(str(self.config.socket_path))
        except ConnectionRefusedError:
            self.config.socket_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError("control endpoint ownership is unproven") from exc
        else:
            raise RuntimeError("control endpoint already has an active listener")
        finally:
            probe.close()

    def _install_signal_handlers(self) -> dict[int, object]:
        if current_thread() is not main_thread():
            return {}
        handlers: dict[int, object] = {}
        for signal_number in (signal.SIGTERM,):
            handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, self._request_signal_shutdown)
        return handlers

    @staticmethod
    def _restore_signal_handlers(handlers: dict[int, object]) -> None:
        for signal_number, handler in handlers.items():
            signal.signal(signal_number, handler)

    def _log_control(self, action: str, accepted: bool, changed: bool) -> None:
        if self._logger is None:
            raise RuntimeError("operational logger unavailable")
        self._logger.write(
            severity="info",
            reason_code="operator_control",
            service_state=self.state.value,
            actor_id=self.config.operator_id,
            action=action,
            result=("changed" if changed else "accepted" if accepted else "rejected"),
        )

    def _set_state(self, state: ServiceState) -> None:
        with self._state_lock:
            self._state = state


def load_operational_config(path: Path | str) -> OperationalConfig:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise ValueError("operational configuration path must be an existing absolute file")
    try:
        document = tomllib.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("operational configuration could not be loaded") from exc
    if set(document) != _CONFIG_FIELDS:
        raise ValueError("invalid operational configuration fields")
    try:
        state_directory = Path(document["state_directory"])
        control_secret_file = Path(document["control_secret_file"])
    except TypeError as exc:
        raise ValueError("operational configuration paths must be strings") from exc
    return OperationalConfig(
        format_version=document["format_version"],
        state_directory=state_directory,
        runtime_id=document["runtime_id"],
        operator_id=document["operator_id"],
        control_secret_file=control_secret_file,
        endpoint_id=document["endpoint_id"],
        log_filename=document["log_filename"],
    )


def _safe_log_value(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"invalid {field_name}")
    lowered = value.lower()
    if any(item in lowered for item in ("token", "secret", "authorization", "https://")):
        raise ValueError(f"sensitive {field_name} is not permitted")
    return value
