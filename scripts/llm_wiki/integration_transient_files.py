"""Private transient transcript files written without following links."""

from __future__ import annotations

import os
import secrets
import stat
import subprocess
from pathlib import Path

from event_envelope import EventEnvelope
from llm_wiki import runtime_roots
from llm_wiki.integration_settings import TRANSIENT_CREATE_ATTEMPTS


def _is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _validate_transient_dir(
    state_root: Path,
    path: Path,
    expected: os.stat_result | None = None,
) -> os.stat_result:
    info = path.lstat()
    _validate_transient_kind(path, info)
    _validate_transient_containment(state_root, path)
    if expected is None:
        return info
    if not _same_identity(info, expected):
        raise PermissionError("transient directory identity changed")
    return info


def _validate_transient_kind(path: Path, info: os.stat_result) -> None:
    if _is_reparse_point(path):
        raise PermissionError("transient directory is not secure")
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError("transient directory is not secure")


def _validate_transient_containment(state_root: Path, path: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(state_root)
    except (OSError, ValueError) as exc:
        raise PermissionError("transient directory escaped state root") from exc


def _create_transient_parent(state_root: Path) -> tuple[Path, os.stat_result]:
    current = state_root
    for name in ("cache", "transient-transcripts"):
        current = current / name
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = _validate_transient_dir(state_root, current)
    return current, info


def _revalidated_private_dir(state_root: Path, current: Path) -> os.stat_result:
    """Re-stat after the chmod: the mode must hold, not merely have been set."""
    secured = _validate_transient_dir(state_root, current)
    if stat.S_IMODE(secured.st_mode) != 0o700:
        raise PermissionError("transient directory is not private")
    return secured


def _secure_posix_parent(state_root: Path, current: Path, info: os.stat_result) -> os.stat_result:
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise PermissionError("transient directory is not private")
    if mode != 0o700:
        current.chmod(0o700)
    return _revalidated_private_dir(state_root, current)


def _secure_windows_parent(state_root: Path, current: Path, info: os.stat_result) -> os.stat_result:
    restrict_file_permissions(current)
    return _validate_transient_dir(state_root, current, info)


def _secure_transient_dir() -> tuple[Path, Path, os.stat_result]:
    state_root = Path(runtime_roots.state_root()).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    current, info = _create_transient_parent(state_root)
    if os.name == "posix":
        info = _secure_posix_parent(state_root, current, info)
    else:
        info = _secure_windows_parent(state_root, current, info)
    return state_root, current, info


def _same_file(path: Path, opened: os.stat_result) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(current.st_mode)
        and current.st_dev == opened.st_dev
        and current.st_ino == opened.st_ino
    )


def _cleanup_created_transient(path: Path, opened: os.stat_result) -> None:
    if _same_file(path, opened):
        try:
            path.unlink()
        except OSError:
            pass


def _write_all(descriptor: int, text: str) -> None:
    remaining = memoryview(text.encode("utf-8"))
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("transient write failed")
        remaining = remaining[written:]


def _same_file_at(directory_fd: int, name: str, opened: os.stat_result) -> bool:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(current.st_mode) and _same_identity(current, opened)


def _validate_posix_parent_fd(directory_fd: int, parent_info: os.stat_result) -> os.stat_result:
    directory_info = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_info.st_mode):
        raise PermissionError("transient directory identity changed")
    if not _same_identity(directory_info, parent_info):
        raise PermissionError("transient directory identity changed")
    return directory_info


def _open_posix_transient(directory_fd: int, event_id: str) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(TRANSIENT_CREATE_ATTEMPTS):
        name = f"{event_id}-{secrets.token_hex(16)}.txt"
        try:
            return os.open(name, flags, 0o600, dir_fd=directory_fd), name
        except FileExistsError:
            continue
    raise FileExistsError("could not create unique transient transcript")


def _write_open_posix_transient(
    descriptor: int,
    directory_fd: int,
    name: str,
    text: str,
    state_root: Path,
    parent: Path,
    directory_info: os.stat_result,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise PermissionError("transient file is not regular")
    os.fchmod(descriptor, 0o600)
    _write_all(descriptor, text)
    os.fsync(descriptor)
    _validate_transient_dir(state_root, parent, directory_info)
    if not _same_file_at(directory_fd, name, opened):
        raise PermissionError("transient file identity changed")
    return opened


def _unlink_posix_transient(
    directory_fd: int, name: str | None, opened: os.stat_result | None
) -> None:
    if name is None or opened is None:
        return
    if not _same_file_at(directory_fd, name, opened):
        return
    try:
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        pass


def _close_descriptor(descriptor: int) -> None:
    if descriptor >= 0:
        os.close(descriptor)


def _write_posix_transient(
    envelope: EventEnvelope,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = -1
    name: str | None = None
    opened: os.stat_result | None = None
    succeeded = False
    try:
        directory_info = _validate_posix_parent_fd(directory_fd, parent_info)
        descriptor, name = _open_posix_transient(directory_fd, envelope.event_id)
        # Capture the identity at creation time. Taking it from the write result
        # instead would leave `opened` unset whenever the write or the
        # post-write parent validation fails, and the cleanup below would then
        # skip an already-created transcript containing session content.
        opened = os.fstat(descriptor)
        _write_open_posix_transient(
            descriptor, directory_fd, name, text, state_root, parent, directory_info
        )
        succeeded = True
        return parent / name
    finally:
        if not succeeded:
            _unlink_posix_transient(directory_fd, name, opened)
        _close_descriptor(descriptor)
        os.close(directory_fd)


def _raw_windows_path_from_fd(descriptor: int) -> str | None:
    import ctypes
    import msvcrt

    handle = msvcrt.get_osfhandle(descriptor)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    get_final_path.restype = ctypes.c_uint32
    length = get_final_path(handle, None, 0, 0)
    if not length:
        return None
    buffer = ctypes.create_unicode_buffer(length + 1)
    if not get_final_path(handle, buffer, len(buffer), 0):
        return None
    return buffer.value


def _normalize_windows_path(value: str) -> Path:
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return Path(value)


def _windows_path_from_fd(descriptor: int) -> Path | None:
    if os.name != "nt":
        return None
    try:
        value = _raw_windows_path_from_fd(descriptor)
    except (ImportError, OSError, ValueError):
        return None
    if value is None:
        return None
    return _normalize_windows_path(value)


def _open_windows_transient(parent: Path, event_id: str) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _ in range(TRANSIENT_CREATE_ATTEMPTS):
        path = parent / f"{event_id}-{secrets.token_hex(16)}.txt"
        try:
            return os.open(path, flags, 0o600), path
        except FileExistsError:
            continue
    raise FileExistsError("could not create unique transient transcript")


def _validate_windows_opened(
    path: Path, opened: os.stat_result, state_root: Path, parent: Path
) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise PermissionError("transient file is not regular")
    resolved = path.resolve(strict=True)
    resolved.relative_to(state_root)
    if resolved.parent != parent.resolve(strict=True) or not _same_file(path, opened):
        raise PermissionError("transient file containment changed")


def _write_open_windows_transient(
    descriptor: int,
    path: Path,
    opened: os.stat_result,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    _validate_windows_opened(path, opened, state_root, parent)
    restrict_file_permissions(path)
    if not _same_file(path, opened):
        raise PermissionError("transient file identity changed")
    _write_all(descriptor, text)
    os.fsync(descriptor)
    cleanup_path = _windows_path_from_fd(descriptor)
    _validate_transient_dir(state_root, parent, parent_info)
    if not _same_file(path, opened):
        raise PermissionError("transient file identity changed")
    return cleanup_path or path


def _write_windows_transient(
    envelope: EventEnvelope,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    descriptor, path = _open_windows_transient(parent, envelope.event_id)
    opened: os.stat_result | None = None
    cleanup_path = path
    try:
        opened = os.fstat(descriptor)
        cleanup_path = _write_open_windows_transient(
            descriptor, path, opened, text, state_root, parent, parent_info
        )
    except (OSError, PermissionError, subprocess.SubprocessError, ValueError):
        os.close(descriptor)
        if opened is not None:
            _cleanup_created_transient(cleanup_path, opened)
        raise
    os.close(descriptor)
    return path


def write_transient_transcript(envelope: EventEnvelope, text: str) -> Path:
    state_root, parent, parent_info = _secure_transient_dir()
    if os.name == "posix":
        return _write_posix_transient(envelope, text, state_root, parent, parent_info)
    return _write_windows_transient(envelope, text, state_root, parent, parent_info)


def restrict_file_permissions(path: Path) -> None:
    if os.name == "nt":
        _restrict_windows_permissions(path)
        return
    path.chmod(0o600)


def _restrict_windows_permissions(path: Path) -> None:
    username = os.environ.get("USERNAME")
    if not username:
        raise PermissionError("transient permissions unavailable")
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            # D: read and write do not include delete, and the transient file
            # is deleted once it has been read. See
            # `docs/research/2026-09-17-a-transient-transcript-can-be-deleted-on-windows.md`.
            f"{username}:(R,W,D)",
        ],
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        raise PermissionError("transient permissions unavailable")

