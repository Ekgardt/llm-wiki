"""Local path checks and the owner-only `run/lsp/` parent a language-server session launches under."""

import os
import stat
import time
from pathlib import Path

from compile_cache import restrict_owner_only, verify_owner_only
from reliable_memory import known_network_path

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def require_startup_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Pyright startup deadline expired")


def path_identity(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or int(getattr(info, "st_file_attributes", 0))
        & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise PermissionError(f"path must not traverse a link or reparse point: {path}")
    return info


def _check_parent_directories(path: Path, message: str, *, deadline: float) -> None:
    """Every ancestor below the anchor has to be a real directory."""
    for parent in path.parents:
        if parent == Path(parent.anchor):
            return
        require_startup_deadline(deadline)
        info = path_identity(parent)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(message)


def _check_local_path_shape(value: Path, label: str) -> None:
    """An absolute local path: no UNC prefix, no NUL, no parent traversal."""
    raw = os.fspath(value)
    if not value.is_absolute() or "\0" in raw:
        raise ValueError(f"{label} must be an absolute local path")
    if raw.startswith(("\\\\", "//")) or ".." in value.parts:
        raise ValueError(f"{label} must be an absolute local path")


def validated_local_file(value: object, label: str, *, deadline: float) -> Path:
    require_startup_deadline(deadline)
    if not isinstance(value, Path):
        raise TypeError(f"{label} must be a Path")
    _check_local_path_shape(value, label)
    _check_parent_directories(
        value, f"{label} parent must be a directory", deadline=deadline
    )
    require_startup_deadline(deadline)
    info = path_identity(value)
    if not stat.S_ISREG(info.st_mode) or known_network_path(value):
        raise ValueError(f"{label} must be a local regular file")
    require_startup_deadline(deadline)
    return value


def _ensure_owned_directory(
    parent: Path, name: str, message: str, *, deadline: float
) -> Path:
    """The named child directory, created owner-only when it is missing."""
    child = parent / name
    require_startup_deadline(deadline)
    try:
        child.mkdir(mode=0o700)
    except FileExistsError:
        pass
    require_startup_deadline(deadline)
    info = path_identity(child)
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(message)
    return child


def ensure_lsp_parent(state_root: Path, *, deadline: float) -> Path:
    require_startup_deadline(deadline)
    if not state_root.is_absolute() or known_network_path(state_root):
        raise ValueError("state_root must be an absolute local path")
    _check_parent_directories(
        state_root, "state_root parent must be a directory", deadline=deadline
    )
    require_startup_deadline(deadline)
    root_info = path_identity(state_root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise NotADirectoryError(state_root)
    run_root = _ensure_owned_directory(
        state_root, "run", "LSP run parent must be a directory", deadline=deadline
    )
    parent = _ensure_owned_directory(
        run_root, "lsp", "LSP owner parent must be a directory", deadline=deadline
    )
    require_startup_deadline(deadline)
    restrict_owner_only(parent, 0o700)
    require_startup_deadline(deadline)
    verify_owner_only(parent, 0o700)
    require_startup_deadline(deadline)
    return parent
