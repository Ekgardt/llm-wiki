"""Bounded, no-follow, stable reads for compile-time authoritative files."""
from __future__ import annotations

import math
import os
import stat
import time
from pathlib import Path

_READ_CHUNK_BYTES = 64 * 1024


def _validated_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp or None")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)


def _check_deadline(deadline: float | None, label: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f"{label} read deadline expired")


def read_stable_bytes(
    path: Path,
    max_bytes: int,
    *,
    label: str = "file",
    deadline: float | None = None,
) -> bytes:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    deadline = _validated_deadline(deadline)
    path = Path(path)
    for parent in path.parents:
        if parent == Path(parent.anchor):
            break
        _check_deadline(deadline, label)
        parent_info = parent.lstat()
        if (
            parent.is_symlink()
            or getattr(parent_info, "st_file_attributes", 0) & 0x400
            or not stat.S_ISDIR(parent_info.st_mode)
        ):
            raise PermissionError(f"{label} parent must be a regular directory")
    _check_deadline(deadline, label)
    before = path.lstat()
    if (
        path.is_symlink()
        or getattr(before, "st_file_attributes", 0) & 0x400
        or not stat.S_ISREG(before.st_mode)
    ):
        raise PermissionError(f"{label} must be a regular non-symlink file")
    if before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    _check_deadline(deadline, label)
    descriptor = os.open(path, flags)
    try:
        _check_deadline(deadline, label)
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if not stat.S_ISREG(opened.st_mode) or identity != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise PermissionError(f"{label} changed before open")
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            _check_deadline(deadline, label)
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_BYTES, max_bytes + 1 - total),
            )
            _check_deadline(deadline, label)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        content = b"".join(chunks)
        if len(content) > max_bytes:
            raise ValueError(f"{label} exceeds {max_bytes} bytes")
        _check_deadline(deadline, label)
        after = os.fstat(descriptor)
        current = path.lstat()
        _check_deadline(deadline, label)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise PermissionError(f"{label} changed during read")
        if identity[:2] != (current.st_dev, current.st_ino) or path.is_symlink():
            raise PermissionError(f"{label} was replaced during read")
        return content
    finally:
        os.close(descriptor)


def read_stable_utf8(
    path: Path,
    max_bytes: int,
    *,
    label: str = "file",
    deadline: float | None = None,
) -> str:
    return read_stable_bytes(
        path,
        max_bytes,
        label=label,
        deadline=deadline,
    ).decode("utf-8", errors="strict")
