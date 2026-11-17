"""Runtime file metadata validation without opening a second descriptor."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from llm_wiki.runtime_environment import require_owner_only_file, windows_reparse_point


def contained_runtime_metadata(path: Path, state_root: Path) -> os.stat_result:
    root = Path(state_root).resolve(strict=True)
    try:
        parent = path.parent.resolve(strict=True)
        require_runtime_parent_containment(parent, root)
    except (OSError, ValueError) as exc:
        raise PermissionError("runtime file is outside the configured state root") from exc
    return path.lstat()


def require_runtime_parent_containment(parent: Path, root: Path) -> None:
    normalized_root = os.path.normcase(root)
    normalized_parent = os.path.normcase(parent)
    if os.path.commonpath((normalized_parent, normalized_root)) != normalized_root:
        raise ValueError("runtime parent is outside the configured state root")


def require_bounded_regular_file(
    path: Path, metadata: os.stat_result, max_bytes: int | None
) -> None:
    if (
        stat.S_ISLNK(metadata.st_mode)
        or windows_reparse_point(path)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise PermissionError("runtime file must be a bounded regular file")
    require_file_size_budget(metadata.st_size, max_bytes)


def require_file_size_budget(size: int, max_bytes: int | None) -> None:
    """SQLite metadata opens need no whole-file byte budget."""
    if max_bytes is None:
        return
    if size > max_bytes:
        raise PermissionError("runtime file must be a bounded regular file")


def require_nonnegative_file_budget(max_bytes: int | None) -> None:
    if max_bytes is None:
        return
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")


def validated_runtime_metadata(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int | None,
    owner_only: bool,
) -> os.stat_result:
    """Validate a bounded regular runtime file from metadata alone."""
    require_nonnegative_file_budget(max_bytes)
    metadata = contained_runtime_metadata(path, state_root)
    require_bounded_regular_file(path, metadata, max_bytes)
    if owner_only:
        require_owner_only_file(path, metadata)
    return metadata


def validate_operational_db_file(
    path: Path,
    state_root: Path,
    *,
    max_bytes: int | None,
    owner_only: bool = False,
) -> os.stat_result:
    """Validate an operational database without opening a second descriptor.

    `validate_runtime_file` proves identity by opening and closing its own
    descriptor. On POSIX that close drops every advisory lock this process holds
    on the inode, including the locks an already-open SQLite connection relies
    on, so concurrent writers stop excluding each other and one of them deletes
    the rollback journal underneath a live transaction. The database openers
    bracket `sqlite3.connect` with `lstat` and `samestat` instead, which proves
    the same identity across the connect without touching a descriptor.
    """
    return validated_runtime_metadata(
        Path(path), state_root, max_bytes=max_bytes, owner_only=owner_only
    )

# Preserve historical callable identities through retained aliases.
contained_runtime_metadata.__module__ = "llm_wiki.runtime_records"
require_runtime_parent_containment.__module__ = "llm_wiki.runtime_records"
require_bounded_regular_file.__module__ = "llm_wiki.runtime_records"
require_file_size_budget.__module__ = "llm_wiki.runtime_records"
require_nonnegative_file_budget.__module__ = "llm_wiki.runtime_records"
validated_runtime_metadata.__module__ = "llm_wiki.runtime_records"
validate_operational_db_file.__module__ = "reliable_memory"
