"""Strict immutable control messages for local authenticated IPC."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Client, Listener
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING

from fxlab.execution.runtime_control import RuntimeState

from .security import ControlSecret

if TYPE_CHECKING:
    from .service import OperationalConfig

CONTROL_PROTOCOL_VERSION = 1
MAX_CONTROL_MESSAGE_BYTES = 16_384
CONTROL_IO_TIMEOUT_SECONDS = 5.0
_AUTH_CHALLENGE_PREFIX = b"FXLAB-CONTROL-AUTH-V1:"
_AUTH_CHALLENGE_BYTES = 32
_AUTH_DIGEST_BYTES = 32
FrozenPrimitive = None | bool | int | float | str
FrozenValue = FrozenPrimitive | tuple["FrozenValue", ...] | tuple[tuple[str, "FrozenValue"], ...]


class ServiceState(StrEnum):
    STARTING = "starting"
    PREFLIGHT = "preflight"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ControlAction(StrEnum):
    STATUS = "status"
    PAUSE = "pause"
    RESUME = "resume"
    EMERGENCY_STOP = "emergency_stop"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class ControlRequest:
    protocol_version: int
    request_id: str
    action: ControlAction

    def __post_init__(self) -> None:
        if self.protocol_version != CONTROL_PROTOCOL_VERSION:
            raise ValueError("unsupported control protocol version")
        object.__setattr__(self, "request_id", _request_id(self.request_id))
        if not isinstance(self.action, ControlAction):
            raise ValueError("invalid control action")


@dataclass(frozen=True, slots=True)
class ControlResponse:
    protocol_version: int
    request_id: str
    accepted: bool
    changed: bool
    service_state: ServiceState
    runtime_state: RuntimeState | None
    reason: str
    payload: tuple[tuple[str, FrozenValue], ...] = ()

    def __post_init__(self) -> None:
        if self.protocol_version != CONTROL_PROTOCOL_VERSION:
            raise ValueError("unsupported control protocol version")
        object.__setattr__(self, "request_id", _request_id(self.request_id))
        if not isinstance(self.accepted, bool) or not isinstance(self.changed, bool):
            raise ValueError("control response flags must be booleans")
        if not isinstance(self.service_state, ServiceState):
            raise ValueError("invalid service state")
        if self.runtime_state is not None and not isinstance(self.runtime_state, RuntimeState):
            raise ValueError("invalid runtime state")
        if not isinstance(self.reason, str) or not self.reason or len(self.reason) > 128:
            raise ValueError("invalid control response reason")
        object.__setattr__(self, "payload", _freeze_pairs(self.payload))


class RequestResultCache:
    """Bounded FIFO cache; it is transport idempotency, never execution truth."""

    def __init__(self, *, capacity: int = 256) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("cache capacity must be positive")
        self._capacity = capacity
        self._items: OrderedDict[str, tuple[ControlRequest, ControlResponse]] = OrderedDict()
        self._lock = Lock()

    def get(self, request: ControlRequest) -> ControlResponse | None:
        with self._lock:
            cached = self._items.get(request.request_id)
            if cached is None:
                return None
            cached_request, response = cached
            if cached_request == request:
                return response
            return ControlResponse(
                CONTROL_PROTOCOL_VERSION,
                request.request_id,
                False,
                False,
                response.service_state,
                response.runtime_state,
                "request_id_conflict",
            )

    def put(self, request: ControlRequest, response: ControlResponse) -> None:
        if request.request_id != response.request_id:
            raise ValueError("cache key must match response request ID")
        with self._lock:
            if request.request_id in self._items:
                return
            self._items[request.request_id] = (request, response)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)


class LocalControlServer:
    """One sequential authenticated local listener using raw byte messages only."""

    def __init__(
        self,
        config: OperationalConfig,
        secret: ControlSecret,
        handler: Callable[[ControlRequest], ControlResponse],
        *,
        cache_capacity: int = 256,
        failure_handler: Callable[[], None] | None = None,
    ) -> None:
        from .service import OperationalConfig

        if not isinstance(config, OperationalConfig):
            raise ValueError("validated operational configuration is required")
        if not isinstance(secret, ControlSecret):
            raise ValueError("resolved control secret is required")
        if not callable(handler):
            raise ValueError("control handler must be callable")
        self._config = config
        self._secret = secret
        self._handler = handler
        self._failure_handler = failure_handler
        self._cache = RequestResultCache(capacity=cache_capacity)
        self._stop = Event()
        self._listener: Listener | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = Listener(
            self._config.control_address,
            family=self._config.control_family,
            authkey=None,
        )
        self._thread = Thread(
            target=self._serve,
            name=f"fxlab-control-{self._config.runtime_id}",
            daemon=False,
        )
        self._thread.start()

    def close(self) -> None:
        listener, thread = self._listener, self._thread
        if listener is None:
            return
        self._stop.set()
        try:
            wake = Client(
                self._config.control_address,
                family=self._config.control_family,
                authkey=None,
            )
            wake.close()
        except OSError:
            pass
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("control listener did not stop")
        listener.close()
        self._listener = None
        self._thread = None
        if os.name != "nt":
            try:
                self._config.socket_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _serve(self) -> None:
        assert self._listener is not None
        unexpected_failure = False
        try:
            while not self._stop.is_set():
                try:
                    connection = self._listener.accept()
                except EOFError:
                    if self._stop.is_set():
                        break
                    continue
                except OSError:
                    unexpected_failure = not self._stop.is_set()
                    break
                with connection:
                    if self._stop.is_set():
                        break
                    try:
                        _authenticate_server(connection, self._secret, self._stop)
                        if not connection.poll(CONTROL_IO_TIMEOUT_SECONDS):
                            continue
                        raw = connection.recv_bytes(maxlength=MAX_CONTROL_MESSAGE_BYTES)
                        request = decode_request(raw)
                        response = self._cache.get(request)
                        if response is None:
                            response = self._handler(request)
                            if response.request_id != request.request_id:
                                raise ValueError("handler returned a mismatched request ID")
                            self._cache.put(request, response)
                        encoded = encode_response(response)
                        connection.send_bytes(encoded)
                    except (EOFError, OSError, ValueError, _ControlAuthenticationError):
                        continue
        except Exception:
            unexpected_failure = True
        finally:
            if unexpected_failure and self._failure_handler is not None:
                self._failure_handler()


def send_control_request(
    config: OperationalConfig,
    secret: ControlSecret,
    request: ControlRequest,
) -> ControlResponse:
    """Send one authenticated local request without pickle serialization."""
    try:
        connection = Client(
            config.control_address,
            family=config.control_family,
            authkey=None,
        )
        with connection:
            _authenticate_client(connection, secret)
            connection.send_bytes(encode_request(request))
            if not connection.poll(CONTROL_IO_TIMEOUT_SECONDS):
                raise RuntimeError("control service unavailable")
            raw = connection.recv_bytes(maxlength=MAX_CONTROL_MESSAGE_BYTES)
        return decode_response(raw)
    except _ControlAuthenticationError as exc:
        raise RuntimeError("control authentication failed") from exc
    except (EOFError, OSError) as exc:
        raise RuntimeError("control service unavailable") from exc


class _ControlAuthenticationError(RuntimeError):
    pass


def _poll_until(connection: object, deadline: float, stop: Event | None = None) -> bool:
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return False
        remaining = deadline - time.monotonic()
        if connection.poll(min(0.05, max(0.0, remaining))):  # type: ignore[attr-defined]
            return True
    return False


def _authenticate_server(connection: object, secret: ControlSecret, stop: Event) -> None:
    if stop.is_set():
        raise _ControlAuthenticationError("authentication unavailable")
    challenge = _AUTH_CHALLENGE_PREFIX + os.urandom(_AUTH_CHALLENGE_BYTES)
    connection.send_bytes(challenge)  # type: ignore[attr-defined]
    deadline = time.monotonic() + CONTROL_IO_TIMEOUT_SECONDS
    if not _poll_until(connection, deadline, stop):
        raise _ControlAuthenticationError("authentication failed")
    response = connection.recv_bytes(maxlength=_AUTH_DIGEST_BYTES)  # type: ignore[attr-defined]
    expected = hmac.digest(secret.key_bytes(), challenge, hashlib.sha256)
    accepted = len(response) == _AUTH_DIGEST_BYTES and hmac.compare_digest(response, expected)
    connection.send_bytes(b"OK" if accepted else b"NO")  # type: ignore[attr-defined]
    if not accepted:
        raise _ControlAuthenticationError("authentication failed")


def _authenticate_client(connection: object, secret: ControlSecret) -> None:
    deadline = time.monotonic() + CONTROL_IO_TIMEOUT_SECONDS
    if not _poll_until(connection, deadline):
        raise _ControlAuthenticationError("authentication failed")
    challenge = connection.recv_bytes(  # type: ignore[attr-defined]
        maxlength=len(_AUTH_CHALLENGE_PREFIX) + _AUTH_CHALLENGE_BYTES
    )
    if not challenge.startswith(_AUTH_CHALLENGE_PREFIX) or len(challenge) != (
        len(_AUTH_CHALLENGE_PREFIX) + _AUTH_CHALLENGE_BYTES
    ):
        raise _ControlAuthenticationError("authentication failed")
    connection.send_bytes(  # type: ignore[attr-defined]
        hmac.digest(secret.key_bytes(), challenge, hashlib.sha256)
    )
    if not _poll_until(connection, deadline):
        raise _ControlAuthenticationError("authentication failed")
    if connection.recv_bytes(maxlength=2) != b"OK":  # type: ignore[attr-defined]
        raise _ControlAuthenticationError("authentication failed")


def encode_request(request: ControlRequest) -> bytes:
    if not isinstance(request, ControlRequest):
        raise ValueError("request must be a ControlRequest")
    return _encode(
        {
            "protocol_version": request.protocol_version,
            "request_id": request.request_id,
            "action": request.action.value,
        }
    )


def decode_request(value: bytes) -> ControlRequest:
    document = _decode(value)
    if set(document) != {"protocol_version", "request_id", "action"}:
        raise ValueError("invalid control request fields")
    try:
        action = ControlAction(document["action"])
        return ControlRequest(
            document["protocol_version"],  # type: ignore[arg-type]
            document["request_id"],  # type: ignore[arg-type]
            action,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid control request") from exc


def encode_response(response: ControlResponse) -> bytes:
    if not isinstance(response, ControlResponse):
        raise ValueError("response must be a ControlResponse")
    return _encode(
        {
            "protocol_version": response.protocol_version,
            "request_id": response.request_id,
            "accepted": response.accepted,
            "changed": response.changed,
            "service_state": response.service_state.value,
            "runtime_state": (
                response.runtime_state.value if response.runtime_state is not None else None
            ),
            "reason": response.reason,
            "payload": _plain(response.payload),
        }
    )


def decode_response(value: bytes) -> ControlResponse:
    document = _decode(value)
    required = {
        "protocol_version",
        "request_id",
        "accepted",
        "changed",
        "service_state",
        "runtime_state",
        "reason",
        "payload",
    }
    if set(document) != required:
        raise ValueError("invalid control response fields")
    raw_runtime = document["runtime_state"]
    raw_payload = document["payload"]
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid control response payload")
    try:
        return ControlResponse(
            document["protocol_version"],  # type: ignore[arg-type]
            document["request_id"],  # type: ignore[arg-type]
            document["accepted"],  # type: ignore[arg-type]
            document["changed"],  # type: ignore[arg-type]
            ServiceState(document["service_state"]),
            RuntimeState(raw_runtime) if raw_runtime is not None else None,
            document["reason"],  # type: ignore[arg-type]
            tuple((str(key), _freeze_value(item)) for key, item in sorted(raw_payload.items())),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid control response") from exc


def freeze_payload(value: dict[str, object]) -> tuple[tuple[str, FrozenValue], ...]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("control payload must be a string-keyed dictionary")
    return tuple((key, _freeze_value(value[key])) for key in sorted(value))


def response_to_dict(response: ControlResponse) -> dict[str, object]:
    if not isinstance(response, ControlResponse):
        raise ValueError("response must be a ControlResponse")
    return {
        "protocol_version": response.protocol_version,
        "request_id": response.request_id,
        "accepted": response.accepted,
        "changed": response.changed,
        "service_state": response.service_state.value,
        "runtime_state": (
            response.runtime_state.value if response.runtime_state is not None else None
        ),
        "reason": response.reason,
        "payload": _plain(response.payload),
    }


def _request_id(value: object) -> str:
    if not isinstance(value, str) or len(value) > 36:
        raise ValueError("request_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("request_id must be a canonical UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("request_id must be a canonical UUID4")
    return value


def _encode(document: dict[str, object]) -> bytes:
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > MAX_CONTROL_MESSAGE_BYTES:
        raise ValueError("control message is too large")
    return encoded


def _decode(value: bytes) -> dict[str, object]:
    if not isinstance(value, bytes) or len(value) > MAX_CONTROL_MESSAGE_BYTES:
        raise ValueError("control message is too large")
    try:
        document = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid control JSON") from exc
    if not isinstance(document, dict) or any(not isinstance(key, str) for key in document):
        raise ValueError("control JSON must be an object")
    return document


def _freeze_pairs(value: object) -> tuple[tuple[str, FrozenValue], ...]:
    if not isinstance(value, tuple):
        raise ValueError("payload must be an immutable tuple")
    keys: set[str] = set()
    result: list[tuple[str, FrozenValue]] = []
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str):
            raise ValueError("payload must contain string-keyed pairs")
        if item[0] in keys:
            raise ValueError("payload keys must be unique")
        keys.add(item[0])
        result.append((item[0], _freeze_value(item[1])))
    return tuple(sorted(result))


def _freeze_value(value: object) -> FrozenValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload numbers must be finite")
        return value
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("payload mappings require string keys")
        return tuple((key, _freeze_value(value[key])) for key in sorted(value))
    raise ValueError("payload contains unsupported value")


def _plain(value: FrozenValue | tuple[tuple[str, FrozenValue], ...]) -> object:
    if isinstance(value, tuple):
        if all(isinstance(item, tuple) and len(item) == 2 for item in value):
            return {str(item[0]): _plain(item[1]) for item in value}
        return [_plain(item) for item in value]
    return value
