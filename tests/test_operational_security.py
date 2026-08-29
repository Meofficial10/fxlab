from __future__ import annotations

import dataclasses
import os
import stat
from pathlib import Path

import pytest

from fxlab.operations.security import FileSecretResolver, _has_windows_reparse_attribute
from fxlab.operations.service import (
    InstanceLock,
    OperationalConfig,
    OperationalLogger,
    load_operational_config,
)


def _write_config(tmp_path: Path, **overrides: object) -> Path:
    state_dir = tmp_path / "state"
    secret = tmp_path / "control.secret"
    secret.write_bytes(b"x" * 32)
    values = {
        "format_version": 1,
        "state_directory": str(state_dir.resolve()).replace("\\", "/"),
        "runtime_id": "runtime-19",
        "operator_id": "operator-1",
        "control_secret_file": str(secret.resolve()).replace("\\", "/"),
        "endpoint_id": "local-control-1",
        "log_filename": "runtime-19.jsonl",
    }
    values.update(overrides)
    lines = [
        f"format_version = {values['format_version']}",
        f"state_directory = \"{values['state_directory']}\"",
        f"runtime_id = \"{values['runtime_id']}\"",
        f"operator_id = \"{values['operator_id']}\"",
        f"control_secret_file = \"{values['control_secret_file']}\"",
        f"endpoint_id = \"{values['endpoint_id']}\"",
        f"log_filename = \"{values['log_filename']}\"",
    ]
    path = tmp_path / "operations.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_operational_config_is_frozen_versioned_and_derives_safe_paths(tmp_path: Path) -> None:
    config = load_operational_config(_write_config(tmp_path))

    assert isinstance(config, OperationalConfig)
    assert config.format_version == 1
    assert config.store_path == config.state_directory / "runtime-19.sqlite3"
    assert config.lock_path == config.state_directory / "runtime-19.lock"
    assert config.log_path == config.state_directory / "runtime-19.jsonl"
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.runtime_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", 2),
        ("runtime_id", "../escape"),
        ("operator_id", "operator with spaces"),
        ("endpoint_id", "bad/endpoint"),
        ("log_filename", "../outside.jsonl"),
    ],
)
def test_operational_config_rejects_unsupported_or_unsafe_values(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        load_operational_config(_write_config(tmp_path, **{field: value}))


def test_operational_config_rejects_relative_state_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="state_directory"):
        load_operational_config(_write_config(tmp_path, state_directory="relative/state"))


def test_operational_config_rejects_lexical_absolute_traversal(tmp_path: Path) -> None:
    traversing = str(tmp_path.resolve() / "configured-root" / ".." / "outside").replace(
        "\\", "/"
    )
    with pytest.raises(ValueError, match="state_directory"):
        load_operational_config(_write_config(tmp_path, state_directory=traversing))


def test_operational_config_rejects_external_broker_selection_before_secret_use(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        FileSecretResolver,
        "resolve",
        lambda self, path: calls.append(str(path)),
    )
    from fxlab.execution.oanda_demo_broker import OandaDemoBroker

    monkeypatch.setattr(
        OandaDemoBroker,
        "__init__",
        lambda self, *args, **kwargs: calls.append("oanda_constructed"),
    )
    path = _write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8") + '\nbroker = "oanda"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fields"):
        load_operational_config(path)
    assert calls == []


def test_file_secret_resolver_returns_redacted_non_dataclass_material(tmp_path: Path) -> None:
    path = tmp_path / "control.secret"
    raw = b"super-private-control-material-123456"
    path.write_bytes(raw)

    secret = FileSecretResolver().resolve(path.resolve())

    assert secret.key_bytes() == raw
    assert raw.decode() not in repr(secret)
    assert "REDACTED" in repr(secret)
    assert not dataclasses.is_dataclass(secret)
    with pytest.raises(TypeError):
        vars(secret)


@pytest.mark.parametrize("size", [0, 31, 4097])
def test_file_secret_resolver_rejects_empty_under_or_oversized_material(
    tmp_path: Path, size: int
) -> None:
    path = tmp_path / "control.secret"
    path.write_bytes(b"x" * size)
    with pytest.raises(ValueError):
        FileSecretResolver().resolve(path.resolve())


def test_file_secret_resolver_rejects_missing_nonfile_and_relative(
    tmp_path: Path,
) -> None:
    resolver = FileSecretResolver()
    with pytest.raises(ValueError):
        resolver.resolve(tmp_path / "missing")
    with pytest.raises(ValueError):
        resolver.resolve(Path("relative.secret"))
    with pytest.raises(ValueError):
        resolver.resolve(tmp_path)



def test_file_secret_resolver_rejects_symlink_when_platform_can_create_one(
    tmp_path: Path,
) -> None:
    resolver = FileSecretResolver()
    target = tmp_path / "target.secret"
    target.write_bytes(b"x" * 32)
    link = tmp_path / "link.secret"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")
    with pytest.raises(ValueError):
        resolver.resolve(link)


def test_windows_reparse_attribute_detection_does_not_require_link_privilege() -> None:
    assert _has_windows_reparse_attribute(stat.FILE_ATTRIBUTE_REPARSE_POINT)
    assert not _has_windows_reparse_attribute(0)


def test_file_secret_resolver_rejects_unc_path_without_reading() -> None:
    with pytest.raises(ValueError, match="local"):
        FileSecretResolver().resolve(Path(r"\\server\share\control.secret"))


def test_instance_lock_rejects_second_holder_and_stale_file_is_reusable(tmp_path: Path) -> None:
    path = tmp_path / "service.lock"
    first = InstanceLock(path, "runtime-one")
    second = InstanceLock(path, "runtime-two")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
    finally:
        first.release()

    path.write_text("stale informational metadata", encoding="utf-8")
    second.acquire()
    second.release()
    assert path.exists()


def test_operational_logger_writes_only_allowlisted_canonical_json(tmp_path: Path) -> None:
    path = tmp_path / "service.jsonl"
    logger = OperationalLogger(path, runtime_id="runtime-one", session_id="session-one")
    logger.open()
    try:
        logger.write(
            severity="info",
            reason_code="control_action",
            service_state="running",
            actor_id="operator-one",
            action="pause",
            result="accepted",
        )
        with pytest.raises(ValueError):
            logger.write(
                severity="info",
                reason_code="bad",
                service_state="running",
                token="must-not-be-accepted",  # type: ignore[call-arg]
            )
    finally:
        logger.close()
    document = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert document["runtime_id"] == "runtime-one"
    assert document["session_id"] == "session-one"
    assert document["actor_id"] == "operator-one"
    assert "token" not in document
