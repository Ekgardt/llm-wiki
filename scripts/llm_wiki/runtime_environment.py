"""Extracted runtime filesystem owner; public compatibility remains in reliable_memory."""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import re
import stat
import subprocess
import warnings
from pathlib import Path

from llm_wiki.runtime_contracts import UnsafeStateRoot


def _windows_network_anchor(path: Path) -> bool:
    anchor = path.resolve(strict=False).anchor
    if not anchor:
        return False
    drive_type_remote = 4
    return ctypes.windll.kernel32.GetDriveTypeW(anchor) == drive_type_remote


def _posix_mount_table() -> list[tuple[str, str]]:
    mount_data, is_mountinfo = _read_posix_mount_data()
    mounts = _parse_posix_mounts(mount_data, is_mountinfo=is_mountinfo)
    if mounts or _platform_system() != "Darwin":
        return mounts
    return _parse_darwin_mounts(_query_darwin_mounts())


def _posix_network_path(path: Path) -> bool:
    target = str(path.resolve(strict=False)).replace("\\", "/")
    matching = [entry for entry in _posix_mount_table() if _path_is_under(target, entry[0])]
    if not matching:
        return False
    _mount_point, filesystem = max(matching, key=lambda entry: len(entry[0]))
    return _is_network_filesystem(filesystem)


def _known_network_path(path: Path) -> bool:
    if str(path).startswith(("\\\\", "//")):
        return True
    if _platform_system() == "Windows":
        return _windows_network_anchor(path)
    return _posix_network_path(path)


def _platform_system() -> str:
    return platform.system()


def _read_posix_mount_data() -> tuple[str, bool]:
    for path, is_mountinfo in (
        (Path("/proc/self/mountinfo"), True),
        (Path("/proc/mounts"), False),
    ):
        try:
            return path.read_text(encoding="utf-8"), is_mountinfo
        except OSError:
            continue
    return "", True


def _query_darwin_mounts() -> str:
    try:
        result = subprocess.run(
            ["/sbin/mount"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout[:1_048_576]


def _parse_posix_mounts(data: str, *, is_mountinfo: bool) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    for line in data.splitlines():
        try:
            if is_mountinfo:
                left, right = line.split(" - ", 1)
                mount_point = left.split()[4]
                filesystem = right.split()[0]
            else:
                _source, mount_point, filesystem, *_rest = line.split()
        except (IndexError, ValueError):
            continue
        mounts.append((_decode_mount_path(mount_point), filesystem.casefold()))
    return mounts


def _parse_darwin_mounts(data: str) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    for line in data.splitlines():
        match = re.match(r"^.* on (.+) \(([^,()]+)(?:,|\))", line)
        if match is None:
            continue
        mount_point, filesystem = match.groups()
        mounts.append((_decode_mount_path(mount_point), filesystem.casefold()))
    return mounts


def _is_network_filesystem(filesystem: str) -> bool:
    return filesystem in {
        "9p",
        "afpfs",
        "ceph",
        "cifs",
        "nfs",
        "nfs4",
        "smbfs",
        "sshfs",
        "webdav",
    } or filesystem.endswith(".sshfs")


def _decode_mount_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _path_is_under(path: str, mount_point: str) -> bool:
    mount_point = mount_point.rstrip("/") or "/"
    return mount_point == "/" or path == mount_point or path.startswith(f"{mount_point}/")


def _has_reparse_attribute(candidate: Path) -> bool:
    if not candidate.exists():
        return False
    attributes = getattr(candidate.stat(follow_symlinks=False), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _windows_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    current = path.absolute()
    candidates = [current, *current.parents]
    return any(_has_reparse_attribute(candidate) for candidate in candidates)


_CLOUD_DIRECTORY_NAMES = frozenset(
    {"dropbox", "googledrive", "google drive", "iclouddrive", "onedrive"}
)


def _require_local_non_reparse_root(path: Path) -> None:
    if _windows_reparse_point(path):
        raise UnsafeStateRoot(f"state root must not traverse a Windows reparse point: {path}")
    if _known_network_path(path):
        raise UnsafeStateRoot(f"state root must use a local filesystem: {path}")


def _warn_if_cloud_synchronized(path: Path) -> None:
    """Cloud sync is detected by name only; locking there is probed, not proven."""
    if not any(part.casefold() in _CLOUD_DIRECTORY_NAMES for part in path.parts):
        return
    warnings.warn(
        f"state root appears to be cloud-synchronized; local locking is only probed: {path}",
        RuntimeWarning,
        stacklevel=3,
    )


def _owner_permissions_supported(path: Path) -> bool:
    return _platform_system() != "Windows" and os.name == "posix"


def _chmod_or_warn(path: Path, mode: int) -> bool:
    """Apply `mode`; False (with the warning) when the filesystem has no permission bits."""
    if stat.S_IMODE(path.stat().st_mode) == mode:
        return True
    try:
        path.chmod(mode)
    except OSError as exc:
        unsupported = {errno.ENOSYS, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
        if exc.errno not in unsupported:
            raise
        warnings.warn(
            f"owner-only permission bits are unsupported for {path}",
            RuntimeWarning,
            stacklevel=3,
        )
        return False
    return True


def _require_applied_mode(path: Path, mode: int) -> None:
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != mode:
        raise PermissionError(f"owner-only mode {mode:o} was not applied to {path}: got {actual:o}")


def _set_owner_only(path: Path, mode: int) -> bool:
    if not _owner_permissions_supported(path):
        return False
    if not _chmod_or_warn(path, mode):
        return False
    _require_applied_mode(path, mode)
    return True


def _harden_runtime_owner_only(path: Path, mode: int) -> None:
    if os.name == "nt":
        from llm_wiki.windows_acl import _harden_windows_acl

        _harden_windows_acl(Path(path))
        return
    _set_owner_only(Path(path), mode)


def _require_windows_owner_only(path: Path) -> None:
    from llm_wiki.windows_acl import _is_owner_only_windows

    if not _is_owner_only_windows(path):
        raise PermissionError("runtime file must be owner-only")


def _require_owner_only_file(path: Path, metadata: os.stat_result) -> None:
    if os.name == "nt":
        _require_windows_owner_only(path)
        return
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & 0o600 != 0o600:
        raise PermissionError("runtime file must be owner-only")
