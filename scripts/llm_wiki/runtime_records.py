"""Extracted runtime filesystem owner; public compatibility remains in reliable_memory."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from llm_wiki.runtime_contracts import RuntimeFileIdentity
from llm_wiki.runtime_environment import _require_owner_only_file as _require_owner_only_file
from llm_wiki.runtime_environment import _windows_reparse_point as _windows_reparse_point
from llm_wiki.runtime_metadata import _contained_runtime_metadata as _contained_runtime_metadata
from llm_wiki.runtime_metadata import _require_bounded_regular_file as _require_bounded_regular_file
from llm_wiki.runtime_metadata import _require_file_size_budget as _require_file_size_budget
from llm_wiki.runtime_metadata import (
    _require_nonnegative_file_budget as _require_nonnegative_file_budget,
)
from llm_wiki.runtime_metadata import (
    _require_runtime_parent_containment as _require_runtime_parent_containment,
)
from llm_wiki.runtime_metadata import _validated_runtime_metadata as _validated_runtime_metadata
from llm_wiki.runtime_metadata import validate_operational_db_file as validate_operational_db_file


def _confirm_runtime_identity(path: Path, metadata: os.stat_result) -> None:
    """Prove the validated metadata still describes the file behind the path.

    Never call this for a file that may already have an open SQLite connection
    in this process. Closing any descriptor releases every POSIX advisory lock
    the process holds on that inode, whichever descriptor took them, so this
    probe would silently strip the locks a live connection depends on.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(metadata, opened):
            raise PermissionError("runtime file identity changed during validation")
    finally:
        os.close(descriptor)


def validate_runtime_file(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> os.stat_result:
    """Validate one bounded regular runtime file without following links."""
    path = Path(path)
    metadata = _validated_runtime_metadata(
        path, state_root, max_bytes=max_bytes, owner_only=owner_only
    )
    _confirm_runtime_identity(path, metadata)
    return metadata


def _require_stable_read(opened, after, length: int, max_bytes: int) -> None:
    """The file did not change under the descriptor, and the read stayed bounded."""
    moved = not os.path.samestat(opened, after) or (
        opened.st_size,
        opened.st_mtime_ns,
    ) != (after.st_size, after.st_mtime_ns)
    if moved or length > max_bytes:
        raise PermissionError("runtime file changed during bounded read")


def read_runtime_bytes(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> bytes:
    """Read stable bounded runtime bytes through a no-follow descriptor."""
    expected = validate_runtime_file(path, state_root, max_bytes=max_bytes, owner_only=owner_only)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(expected, opened):
            raise PermissionError("runtime file identity changed before read")
        data = os.read(descriptor, max_bytes + 1)
        _require_stable_read(opened, os.fstat(descriptor), len(data), max_bytes)
        return data
    finally:
        os.close(descriptor)


def _windows_runtime_identity_names(target: Path) -> tuple[str, str]:
    """Volume and file id for one open runtime file, writers still allowed."""
    import windows_workspace

    handle = windows_workspace.open_shared_readonly_runtime_file(target)
    try:
        volume, file_id, is_directory = windows_workspace.identity(
            handle, directory=False
        )
    finally:
        windows_workspace.close_handle(handle)
    if is_directory:
        raise PermissionError("runtime artifact must be a regular file")
    return f"volume:{volume}", f"file-id:{file_id.hex()}"


def _posix_runtime_identity_names(current: os.stat_result) -> tuple[str, str]:
    return f"dev:{current.st_dev}", f"ino:{current.st_ino}"


def capture_runtime_file_identity(
    path: Path, *, state_root: Path
) -> RuntimeFileIdentity:
    """Capture stable platform file identity for one runtime artifact."""
    target = Path(path)
    root = Path(state_root).resolve(strict=True)
    metadata = validate_runtime_file(target, root, max_bytes=1 << 50)
    return _runtime_identity_from_metadata(target, metadata)


def capture_operational_database_identity(
    path: Path, *, state_root: Path
) -> RuntimeFileIdentity:
    """Identify a live database without closing a non-SQLite descriptor."""
    target = Path(path)
    root = Path(state_root).resolve(strict=True)
    metadata = validate_operational_db_file(target, root, max_bytes=None)
    return _runtime_identity_from_metadata(target, metadata)


def _runtime_identity_from_metadata(
    target: Path, metadata: os.stat_result
) -> RuntimeFileIdentity:
    on_windows = os.name == "nt"
    names = _windows_runtime_identity_names(target) if on_windows else None
    current = target.stat(follow_symlinks=False)
    if not os.path.samestat(metadata, current):
        raise PermissionError("runtime file identity changed during capture")
    if names is None:
        names = _posix_runtime_identity_names(current)
    volume_name, file_name = names
    return RuntimeFileIdentity(
        platform="windows" if on_windows else "posix",
        volume=volume_name,
        file_id=file_name,
        size=current.st_size,
        mtime_ns=current.st_mtime_ns,
    )


# Keep existing serialized public callable/class paths.
validate_runtime_file.__module__ = "reliable_memory"
validate_operational_db_file.__module__ = "reliable_memory"
read_runtime_bytes.__module__ = "reliable_memory"
capture_runtime_file_identity.__module__ = "reliable_memory"
capture_operational_database_identity.__module__ = "reliable_memory"
