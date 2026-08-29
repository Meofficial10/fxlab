from __future__ import annotations

import dataclasses
import inspect
import json
import threading
import time
import uuid
from multiprocessing.connection import Client
from pathlib import Path

import pytest

from fxlab.execution.runtime_control import RuntimeState
from fxlab.operations.control import (
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_MESSAGE_BYTES,
    ControlAction,
    ControlRequest,
    ControlResponse,
    LocalControlServer,
    RequestResultCache,
    ServiceState,
    _authenticate_client,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    send_control_request,
)
from fxlab.operations.security import FileSecretResolver
from fxlab.operations.service import OperationalConfig


def _request(action: ControlAction = ControlAction.STATUS) -> ControlRequest:
    return ControlRequest(CONTROL_PROTOCOL_VERSION, str(uuid.uuid4()), action)


def test_control_contract_values_and_frozen_dtos_are_stable() -> None:
    assert [item.value for item in ServiceState] == [
        "starting",
        "preflight",
        "running",
        "stopping",
        "stopped",
        "failed",
    ]
    assert [item.value for item in ControlAction] == [
        "status",
        "pause",
        "resume",
        "emergency_stop",
        "stop",
    ]
    request = _request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.action = ControlAction.PAUSE  # type: ignore[misc]


def test_request_json_is_canonical_strict_and_round_trips() -> None:
    request = _request(ControlAction.PAUSE)
    encoded = encode_request(request)

    assert encoded == json.dumps(
        {
            "action": "pause",
            "protocol_version": 1,
            "request_id": request.request_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert decode_request(encoded) == request

    document = json.loads(encoded)
    document["actor_id"] = "attacker"
    with pytest.raises(ValueError, match="fields"):
        decode_request(json.dumps(document).encode())


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"protocol_version": 2, "request_id": str(uuid.uuid4()), "action": "status"},
        {"protocol_version": 1, "request_id": "not-a-uuid", "action": "status"},
        {"protocol_version": 1, "request_id": str(uuid.uuid4()), "action": "order"},
    ],
)
def test_request_parser_rejects_invalid_contracts(document: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        decode_request(json.dumps(document).encode())


def test_request_parser_rejects_oversized_or_non_object_json() -> None:
    with pytest.raises(ValueError, match="large"):
        decode_request(b"x" * (MAX_CONTROL_MESSAGE_BYTES + 1))
    with pytest.raises(ValueError):
        decode_request(b"[]")


def test_response_has_explicit_immutable_primitive_payload() -> None:
    request = _request()
    response = ControlResponse(
        CONTROL_PROTOCOL_VERSION,
        request.request_id,
        True,
        False,
        ServiceState.RUNNING,
        RuntimeState.PAUSED,
        "accepted",
        (("label", "LIVE_RUNTIME"), ("cycles", 3)),
    )

    assert decode_response(encode_response(response)) == response
    with pytest.raises(dataclasses.FrozenInstanceError):
        response.reason = "changed"  # type: ignore[misc]


def test_request_result_cache_is_bounded_and_returns_exact_cached_result() -> None:
    cache = RequestResultCache(capacity=2)
    first_request = _request(ControlAction.PAUSE)
    second_request = _request(ControlAction.RESUME)
    third_request = _request(ControlAction.STOP)
    first = ControlResponse(
        1, first_request.request_id, True, True, ServiceState.RUNNING, None, "a"
    )
    second = ControlResponse(
        1, second_request.request_id, True, True, ServiceState.RUNNING, None, "b"
    )
    third = ControlResponse(
        1, third_request.request_id, True, True, ServiceState.RUNNING, None, "c"
    )

    cache.put(first_request, first)
    cache.put(second_request, second)
    assert cache.get(first_request) is first
    cache.put(third_request, third)

    assert cache.get(first_request) is None
    assert cache.get(second_request) is second
    assert cache.get(third_request) is third


def test_request_id_reuse_with_different_action_is_rejected_without_dispatch(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    secret = FileSecretResolver().resolve(config.control_secret_file)
    calls: list[ControlAction] = []

    def handler(request: ControlRequest) -> ControlResponse:
        calls.append(request.action)
        return ControlResponse(
            1, request.request_id, True, True, ServiceState.RUNNING, RuntimeState.PAUSED, "accepted"
        )

    server = LocalControlServer(config, secret, handler)
    server.start()
    try:
        request_id = str(uuid.uuid4())
        first = send_control_request(
            config, secret, ControlRequest(1, request_id, ControlAction.PAUSE)
        )
        conflict = send_control_request(
            config, secret, ControlRequest(1, request_id, ControlAction.STOP)
        )
        assert first.accepted
        assert not conflict.accepted
        assert conflict.reason == "request_id_conflict"
        assert calls == [ControlAction.PAUSE]
    finally:
        server.close()


def _config(tmp_path: Path) -> OperationalConfig:
    secret_path = tmp_path / "control.secret"
    secret_path.write_bytes(b"z" * 32)
    return OperationalConfig(
        format_version=1,
        state_directory=tmp_path.resolve(),
        runtime_id="runtime-control-test",
        operator_id="operator-control-test",
        control_secret_file=secret_path.resolve(),
        endpoint_id=f"endpoint-{uuid.uuid4().hex}",
        log_filename="runtime-control-test.jsonl",
    )


def test_authenticated_local_server_uses_handler_once_for_duplicate_request(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    secret = FileSecretResolver().resolve(config.control_secret_file)
    calls: list[ControlRequest] = []
    called = threading.Event()

    def handler(request: ControlRequest) -> ControlResponse:
        calls.append(request)
        called.set()
        return ControlResponse(
            1,
            request.request_id,
            True,
            request.action is not ControlAction.STATUS,
            ServiceState.RUNNING,
            RuntimeState.PAUSED,
            "accepted",
        )

    server = LocalControlServer(config, secret, handler)
    server.start()
    try:
        request = _request(ControlAction.PAUSE)
        first = send_control_request(config, secret, request)
        second = send_control_request(config, secret, request)
        assert called.wait(2)
        assert first == second
        assert calls == [request]
    finally:
        server.close()


def test_wrong_authentication_never_reaches_handler(tmp_path: Path) -> None:
    config = _config(tmp_path)
    correct = FileSecretResolver().resolve(config.control_secret_file)
    wrong_path = tmp_path / "wrong.secret"
    wrong_path.write_bytes(b"w" * 32)
    wrong = FileSecretResolver().resolve(wrong_path)
    calls = 0

    def handler(request: ControlRequest) -> ControlResponse:
        nonlocal calls
        calls += 1
        raise AssertionError(request)

    server = LocalControlServer(config, correct, handler)
    server.start()
    try:
        with pytest.raises(RuntimeError, match="authentication"):
            send_control_request(config, wrong, _request())
        assert calls == 0
    finally:
        server.close()


def test_application_transport_uses_only_bounded_byte_messages() -> None:
    server_source = inspect.getsource(LocalControlServer._serve)
    client_source = inspect.getsource(send_control_request)

    assert ".recv_bytes(" in server_source
    assert ".send_bytes(" in server_source
    assert ".recv(" not in server_source
    assert ".send(" not in server_source
    assert ".recv_bytes(" in client_source
    assert ".send_bytes(" in client_source
    assert ".recv(" not in client_source
    assert ".send(" not in client_source


def test_stalled_authentication_peer_cannot_block_server_shutdown(tmp_path: Path) -> None:
    config = _config(tmp_path)
    secret = FileSecretResolver().resolve(config.control_secret_file)
    server = LocalControlServer(
        config,
        secret,
        lambda request: (_ for _ in ()).throw(AssertionError(request)),
    )
    server.start()
    stalled = Client(config.control_address, family=config.control_family, authkey=None)
    completed = threading.Event()
    closer = threading.Thread(target=lambda: (server.close(), completed.set()))
    try:
        closer.start()
        bounded = completed.wait(1.5)
    finally:
        stalled.close()
        closer.join(6)
        if not completed.is_set():
            server.close()
    assert bounded


def test_live_oversized_frame_is_rejected_before_dispatch_and_server_recovers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    secret = FileSecretResolver().resolve(config.control_secret_file)
    calls: list[ControlRequest] = []

    def handler(request: ControlRequest) -> ControlResponse:
        calls.append(request)
        return ControlResponse(
            1, request.request_id, True, False, ServiceState.RUNNING, None, "accepted"
        )

    server = LocalControlServer(config, secret, handler)
    server.start()
    try:
        connection = Client(config.control_address, family=config.control_family, authkey=None)
        with connection:
            _authenticate_client(connection, secret)
            try:
                connection.send_bytes(b"x" * (MAX_CONTROL_MESSAGE_BYTES + 1))
            except BrokenPipeError:
                pass
            time.sleep(0.1)
            try:
                readable = connection.poll(0.1)
            except BrokenPipeError:
                readable = False
            assert not readable
        assert calls == []
        valid = send_control_request(config, secret, _request())
        assert valid.accepted
        assert len(calls) == 1
    finally:
        server.close()
