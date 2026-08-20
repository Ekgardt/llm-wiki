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


def _root_controlled(info: os.stat_result) -> bool:
    """Only an administrator can write here."""
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _system_symlink(link: Path) -> bool:
    """A symlink an ordinary user could not have created or replaced.

    Traversal-resistant APIs draw the line here rather than at "no symlink
    anywhere": `openat2(RESOLVE_BENEATH)`, FreeBSD's `O_RESOLVE_BENEATH` and
    Go's `os.Root` all defend against constructs an unprivileged user can make
    and state plainly that ones requiring root are outside the threat model.
    macOS ships `/var` as a root-owned symlink to `/private/var`, so every
    temporary file on that platform sits behind one; refusing them all made
    this reader unusable there. A symlink qualifies only when both it and the
    directory holding it belong to root and are not group- or other-writable,
    which is exactly the "an administrator put it there" case. Symlink mode
    bits are meaningless on Linux, so the containing directory carries the
    decision.
    """
    if os.name == "nt":
        return False
    try:
        link_info = link.lstat()
        parent_info = link.parent.stat()
    except OSError:
        return False
    if link_info.st_uid != 0:
        return False
    return _root_controlled(parent_info)


def _acceptable_ancestor(parent: Path) -> bool:
    """Every ancestor is a real directory, or a system-owned symlink to one."""
    info = parent.lstat()
    if getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    if parent.is_symlink():
        return _system_symlink(parent) and parent.is_dir()
    return stat.S_ISDIR(info.st_mode)


def _require_byte_limit(max_bytes: object) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")


def _require_safe_ancestors(path: Path, label: str, deadline: float | None) -> None:
    for parent in path.parents:
        if parent == Path(parent.anchor):
            break
        _check_deadline(deadline, label)
        if not _acceptable_ancestor(parent):
            raise PermissionError(f"{label} parent must be a regular directory")


def _require_regular_file(path: Path, before: os.stat_result, label: str) -> None:
    if (
        path.is_symlink()
        or getattr(before, "st_file_attributes", 0) & 0x400
        or not stat.S_ISREG(before.st_mode)
    ):
        raise PermissionError(f"{label} must be a regular non-symlink file")


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _require_opened_identity(
    descriptor: int, identity: tuple[int, int, int, int], label: str
) -> None:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or identity != _file_identity(opened):
        raise PermissionError(f"{label} changed before open")


def _read_bounded_chunks(
    descriptor: int, max_bytes: int, label: str, deadline: float | None
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        _check_deadline(deadline, label)
        chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, max_bytes + 1 - total))
        _check_deadline(deadline, label)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    return content


def _require_unchanged_after_read(
    descriptor: int,
    path: Path,
    identity: tuple[int, int, int, int],
    label: str,
) -> None:
    after = os.fstat(descriptor)
    current = path.lstat()
    if identity != _file_identity(after):
        raise PermissionError(f"{label} changed during read")
    if identity[:2] != (current.st_dev, current.st_ino) or path.is_symlink():
        raise PermissionError(f"{label} was replaced during read")


def _read_open_descriptor(
    descriptor: int,
    path: Path,
    identity: tuple[int, int, int, int],
    max_bytes: int,
    label: str,
    deadline: float | None,
) -> bytes:
    _check_deadline(deadline, label)
    _require_opened_identity(descriptor, identity, label)
    content = _read_bounded_chunks(descriptor, max_bytes, label, deadline)
    _check_deadline(deadline, label)
    _require_unchanged_after_read(descriptor, path, identity, label)
    _check_deadline(deadline, label)
    return content


def read_stable_bytes(
    path: Path,
    max_bytes: int,
    *,
    label: str = "file",
    deadline: float | None = None,
) -> bytes:
    _require_byte_limit(max_bytes)
    deadline = _validated_deadline(deadline)
    path = Path(path)
    _require_safe_ancestors(path, label, deadline)
    _check_deadline(deadline, label)
    before = path.lstat()
    _require_regular_file(path, before, label)
    if before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    _check_deadline(deadline, label)
    descriptor = os.open(path, flags)
    try:
        return _read_open_descriptor(
            descriptor, path, _file_identity(before), max_bytes, label, deadline
        )
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
