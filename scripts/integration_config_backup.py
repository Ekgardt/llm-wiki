"""Publish integration configuration with bounded byte-exact preimages."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from reliable_memory import fsync_directory

MAX_BACKUPS = 10
MAX_BACKUP_AGE_SECONDS = 90 * 24 * 60 * 60
MAX_BACKUP_BYTES = 100 * 1024 * 1024
_UNSET = object()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _missing_parent_chain(path: Path) -> tuple[Path, list[Path]]:
    missing: list[Path] = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError("integration config parent must not be a symlink")
        missing.append(current)
        current = current.parent
    return current, missing


def _require_no_symlink_ancestors(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("integration config parent must not be a symlink")
        current = current.parent


def _require_safe_parent(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("integration config parent is unsafe")


def _create_private_parents(missing: list[Path]) -> None:
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        fsync_directory(directory.parent)


def _ensure_private_parent(path: Path) -> None:
    _require_no_symlink_ancestors(path)
    existing, missing = _missing_parent_chain(path)
    _require_safe_parent(existing)
    _create_private_parents(missing)


def _destination_exists(destination: Path) -> bool:
    if destination.is_symlink():
        raise ValueError("integration config must not be a symlink")
    if not destination.exists():
        return False
    if not destination.is_file():
        raise ValueError("integration config must be a regular file")
    return True


def _require_bounded_size(size: int, max_bytes: int | None) -> None:
    if max_bytes is None:
        return
    if size > max_bytes:
        raise ValueError("integration config exceeds the size limit")


def _read_destination(destination: Path, max_bytes: int | None) -> bytes | None:
    if not _destination_exists(destination):
        return None
    _require_bounded_size(destination.stat().st_size, max_bytes)
    value = destination.read_bytes()
    _require_bounded_size(len(value), max_bytes)
    return value


def _validate_expected_digest(expected: bytes | None, digest: str | None) -> None:
    if digest is None:
        return
    if expected is None or _sha256(expected) != digest:
        raise ValueError("integration config expected digest is invalid")


def _require_unset_digest(digest: str | None) -> None:
    if digest is not None:
        raise ValueError("integration config expected bytes are required")


def _require_expected_type(expected: object) -> None:
    if not isinstance(expected, bytes) and expected is not None:
        raise TypeError("expected integration config must be bytes or None")


def _require_expected(
    current: bytes | None,
    expected: bytes | None | object,
    digest: str | None,
) -> None:
    if expected is _UNSET:
        _require_unset_digest(digest)
        return
    _require_expected_type(expected)
    _validate_expected_digest(expected, digest)
    if current != expected:
        raise RuntimeError("integration config changed concurrently")


def _backup_path(destination: Path) -> Path:
    started_at = datetime.now()
    for offset in range(1000):
        stamp = (started_at + timedelta(microseconds=offset)).strftime(
            "%Y%m%d-%H%M%S-%f"
        )
        candidate = destination.with_name(f"{destination.name}.bak-llm-wiki-{stamp}")
        if not candidate.exists():
            return candidate
    raise FileExistsError("could not allocate integration config backup path")


def _create_verified_backup(destination: Path, original: bytes) -> Path:
    backup = _backup_path(destination)
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(backup, 0o600)
    if backup.read_bytes() != original:
        backup.unlink(missing_ok=True)
        raise OSError("integration config backup verification failed")
    fsync_directory(backup.parent)
    return backup


def _destination_metadata(destination: Path) -> tuple[int, tuple[int, int] | None]:
    if not destination.exists():
        return 0o600, None
    metadata = destination.stat()
    owner = (metadata.st_uid, metadata.st_gid)
    return stat.S_IMODE(metadata.st_mode), owner


def _restore_owner(path: Path, owner: tuple[int, int] | None) -> None:
    if owner is None or not hasattr(os, "chown"):
        return
    os.chown(path, *owner)


def _atomic_write_verified(
    destination: Path,
    replacement: bytes,
    expected_original: bytes | None | object = _UNSET,
    expected_original_sha256: str | None = None,
    max_original_bytes: int | None = None,
) -> None:
    mode, owner = _destination_metadata(destination)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        _restore_owner(temporary, owner)
        current = _read_destination(destination, max_original_bytes)
        _require_expected(current, expected_original, expected_original_sha256)
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    if destination.read_bytes() != replacement:
        raise OSError("integration config write verification failed")


def _owned_backup(
    candidate: Path, owned_name: re.Pattern[str]
) -> tuple[Path, os.stat_result] | None:
    if not owned_name.fullmatch(candidate.name):
        return None
    if candidate.is_symlink():
        return None
    try:
        metadata = candidate.stat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return candidate, metadata


def _owned_backups(destination: Path) -> list[tuple[Path, os.stat_result]]:
    prefix = re.escape(f"{destination.name}.bak-llm-wiki-")
    owned_name = re.compile(rf"{prefix}\d{{8}}-\d{{6}}(?:-\d{{6}})?")
    backups: list[tuple[Path, os.stat_result]] = []
    for candidate in destination.parent.iterdir():
        owned = _owned_backup(candidate, owned_name)
        if owned is not None:
            backups.append(owned)
    return sorted(backups, key=lambda item: (item[1].st_mtime_ns, item[0].name))


def _protected_backup(
    backups: list[tuple[Path, os.stat_result]], protected: Path | None
) -> Path:
    if protected is None:
        return backups[-1][0]
    return protected


def _expired_backup(
    item: tuple[Path, os.stat_result], protected: Path, cutoff: float
) -> bool:
    path, metadata = item
    return path != protected and metadata.st_mtime < cutoff


def _prune_expired(
    backups: list[tuple[Path, os.stat_result]], protected: Path
) -> None:
    cutoff = time.time() - MAX_BACKUP_AGE_SECONDS
    for item in list(backups):
        if _expired_backup(item, protected, cutoff) and len(backups) > 1:
            item[0].unlink()
            backups.remove(item)


def _over_backup_limits(backups: list[tuple[Path, os.stat_result]]) -> bool:
    total_bytes = sum(metadata.st_size for _, metadata in backups)
    return len(backups) > MAX_BACKUPS or total_bytes > MAX_BACKUP_BYTES


def _oldest_unprotected(
    backups: list[tuple[Path, os.stat_result]], protected: Path
) -> tuple[Path, os.stat_result] | None:
    return next((item for item in backups if item[0] != protected), None)


def _prune_to_limits(
    backups: list[tuple[Path, os.stat_result]], protected: Path
) -> None:
    while _over_backup_limits(backups):
        removable = _oldest_unprotected(backups, protected)
        if removable is None:
            return
        removable[0].unlink()
        backups.remove(removable)


def _prune_backups(destination: Path, protected: Path | None) -> None:
    backups = _owned_backups(destination)
    if not backups:
        return
    retained = _protected_backup(backups, protected)
    _prune_expired(backups, retained)
    _prune_to_limits(backups, retained)


def _optional_backup(destination: Path, original: bytes | None) -> Path | None:
    if original is None:
        return None
    return _create_verified_backup(destination, original)


def _cleanup_failed_backup(backup: Path | None) -> None:
    if backup is None:
        return
    backup.unlink(missing_ok=True)
    fsync_directory(backup.parent)


def _require_replacement_size(replacement: bytes, max_bytes: int | None) -> None:
    if max_bytes is None:
        return
    if len(replacement) > max_bytes:
        raise ValueError("integration config replacement exceeds the size limit")


def _publish_changed_configuration(
    destination: Path,
    replacement: bytes,
    original: bytes | None,
    expected_original: bytes | None | object,
    expected_original_sha256: str | None,
    max_original_bytes: int | None,
) -> Path | None:
    backup = _optional_backup(destination, original)
    try:
        _atomic_write_verified(
            destination,
            replacement,
            expected_original,
            expected_original_sha256,
            max_original_bytes,
        )
    except BaseException:
        _cleanup_failed_backup(backup)
        raise
    return backup


def publish_configuration(
    destination: Path,
    replacement: bytes,
    *,
    expected_original: bytes | None | object = _UNSET,
    expected_original_sha256: str | None = None,
    max_original_bytes: int | None = None,
) -> tuple[bool, Path | None]:
    """Replace changed config and retain a bounded verified sibling preimage."""
    destination = Path(destination)
    _ensure_private_parent(destination.parent)
    original = _read_destination(destination, max_original_bytes)
    _require_expected(original, expected_original, expected_original_sha256)
    _require_replacement_size(replacement, max_original_bytes)
    if original == replacement:
        _prune_backups(destination, None)
        return False, None
    backup = _publish_changed_configuration(
        destination,
        replacement,
        original,
        expected_original,
        expected_original_sha256,
        max_original_bytes,
    )
    _prune_backups(destination, backup)
    return True, backup
