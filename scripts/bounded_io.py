"""Bounded, no-follow, stable reads for compile-time authoritative files."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def read_stable_bytes(path: Path, max_bytes: int, *, label: str = "file") -> bytes:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    path = Path(path)
    for parent in path.parents:
        if parent == Path(parent.anchor):
            break
        parent_info = parent.lstat()
        if (
            parent.is_symlink()
            or getattr(parent_info, "st_file_attributes", 0) & 0x400
            or not stat.S_ISDIR(parent_info.st_mode)
        ):
            raise PermissionError(f"{label} parent must be a regular directory")
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
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if not stat.S_ISREG(opened.st_mode) or identity != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise PermissionError(f"{label} changed before open")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError(f"{label} exceeds {max_bytes} bytes")
        after = os.fstat(descriptor)
        current = path.lstat()
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise PermissionError(f"{label} changed during read")
        if identity[:2] != (current.st_dev, current.st_ino) or path.is_symlink():
            raise PermissionError(f"{label} was replaced during read")
        return content
    finally:
        os.close(descriptor)


def read_stable_utf8(path: Path, max_bytes: int, *, label: str = "file") -> str:
    return read_stable_bytes(path, max_bytes, label=label).decode("utf-8", errors="strict")
