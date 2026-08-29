"""Narrow file-backed control-secret resolution with redacted material."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class ControlSecret:
    """Opaque process-local credential material.

    The transport needs the bytes for its challenge-response handshake, but the
    object deliberately has no dataclass traversal or instance dictionary.
    """

    __slots__ = ("_value",)

    def __init__(self, value: bytes) -> None:
        self._value = bytes(value)

    def key_bytes(self) -> bytes:
        return self._value

    def __repr__(self) -> str:
        return "ControlSecret(<REDACTED>)"


class FileSecretResolver:
    """Read one bounded local regular file without following path aliases."""

    def __init__(self, *, minimum_bytes: int = 32, maximum_bytes: int = 4096) -> None:
        if minimum_bytes < 32 or maximum_bytes < minimum_bytes:
            raise ValueError("invalid control-secret size bounds")
        self.minimum_bytes = minimum_bytes
        self.maximum_bytes = maximum_bytes

    def resolve(self, path: Path | str) -> ControlSecret:
        candidate = Path(path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("control secret path must be absolute")
        if _is_network_path(candidate):
            raise ValueError("control secret must use a local filesystem")
        if _contains_path_alias(candidate):
            raise ValueError("control secret path aliases are not permitted")
        try:
            stat = candidate.stat()
        except OSError as exc:
            raise ValueError("control secret file is unavailable") from exc
        if not candidate.is_file():
            raise ValueError("control secret must be a regular file")
        if stat.st_size > self.maximum_bytes:
            raise ValueError("control secret file is too large")
        try:
            value = candidate.read_bytes().rstrip(b"\r\n")
        except OSError as exc:
            raise ValueError("control secret file is unavailable") from exc
        if len(value) < self.minimum_bytes:
            raise ValueError("control secret must contain at least 256 bits")
        if len(value) > self.maximum_bytes:
            raise ValueError("control secret file is too large")
        return ControlSecret(value)


def is_safe_local_absolute_path(path: Path) -> bool:
    return (
        path.is_absolute()
        and ".." not in path.parts
        and not _is_network_path(path)
        and not _contains_path_alias(path)
    )


def _is_network_path(path: Path) -> bool:
    raw = str(path)
    return raw.startswith(("\\\\", "//"))


def _contains_path_alias(path: Path) -> bool:
    current = path
    existing: list[Path] = []
    while True:
        if current.exists() or os.path.lexists(current):
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in existing:
        try:
            if item.is_symlink():
                return True
            is_junction = getattr(item, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
            if os.name == "nt" and _has_windows_reparse_attribute(
                getattr(item.lstat(), "st_file_attributes", 0)
            ):
                return True
        except OSError:
            return True
    return False


def _has_windows_reparse_attribute(file_attributes: int) -> bool:
    return bool(file_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
