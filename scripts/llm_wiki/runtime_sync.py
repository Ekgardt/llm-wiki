"""Extracted runtime filesystem owner; public compatibility remains in reliable_memory."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

from llm_wiki.runtime_contracts import MetadataDurabilityUnavailable


def fsync_file(path: Path) -> None:
    with Path(path).open(file_sync_mode()) as handle:
        os.fsync(handle.fileno())


def file_sync_mode() -> str:
    # Windows FlushFileBuffers requires write access; POSIX fsync does not.
    if os.name == "nt":
        return "rb+"
    return "rb"


def fsync_directory_windows(path: Path) -> None:
    import windows_workspace

    handle: int | None = None
    try:
        handle = windows_workspace.open_writable_directory_path(Path(path))
        if not windows_workspace.flush_directory(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    except (OSError, RuntimeError) as exc:
        raise metadata_durability_error("Windows directory flush failed", exc) from exc
    finally:
        close_windows_handle(handle)


def close_windows_handle(handle: int | None) -> None:
    if handle is None:
        return
    import windows_workspace

    windows_workspace.close_handle(handle)


def opened_directory_descriptor(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        return os.open(Path(path), flags)
    except OSError as exc:
        raise metadata_durability_error("directory open for fsync failed", exc) from exc


def fsync_directory_posix(path: Path) -> None:
    descriptor = opened_directory_descriptor(path)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise metadata_durability_error("directory fsync failed", exc) from exc
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    """Sync directory metadata or fail when the platform cannot prove it."""
    if os.name == "nt":
        fsync_directory_windows(path)
        return
    fsync_directory_posix(path)


def metadata_durability_error(
    message: str,
    error: BaseException,
) -> MetadataDurabilityUnavailable:
    error_number = getattr(error, "errno", None)
    if error_number is None:
        result = MetadataDurabilityUnavailable(f"{message}: {error}")
    else:
        result = MetadataDurabilityUnavailable(error_number, f"{message}: {error}")
    if getattr(error, "winerror", None) is not None:
        result.winerror = error.winerror
    return result


def sync_runtime_directory(path: Path) -> None:
    """Expose the shared checked metadata sync boundary to runtime publishers."""
    fsync_directory(Path(path))


# Keep existing serialized public callable/class paths.
fsync_file.__module__ = "reliable_memory"
fsync_directory.__module__ = "reliable_memory"
sync_runtime_directory.__module__ = "reliable_memory"
