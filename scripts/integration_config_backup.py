"""Publish integration configuration with bounded byte-exact preimages."""
from __future__ import annotations

import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

MAX_BACKUPS = 10
MAX_BACKUP_AGE_SECONDS = 90 * 24 * 60 * 60
MAX_BACKUP_BYTES = 100 * 1024 * 1024


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
    with backup.open("xb") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    if backup.read_bytes() != original:
        backup.unlink(missing_ok=True)
        raise OSError("integration config backup verification failed")
    return backup


def _atomic_write_verified(destination: Path, replacement: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    if destination.read_bytes() != replacement:
        raise OSError("integration config write verification failed")


def _owned_backups(destination: Path) -> list[tuple[Path, os.stat_result]]:
    prefix = re.escape(f"{destination.name}.bak-llm-wiki-")
    owned_name = re.compile(rf"{prefix}\d{{8}}-\d{{6}}(?:-\d{{6}})?")
    backups = []
    for candidate in destination.parent.iterdir():
        if not owned_name.fullmatch(candidate.name) or candidate.is_symlink():
            continue
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if candidate.is_file():
            backups.append((candidate, stat))
    return sorted(backups, key=lambda item: (item[1].st_mtime_ns, item[0].name))


def _prune_backups(destination: Path, protected: Path | None) -> None:
    backups = _owned_backups(destination)
    if not backups:
        return
    if protected is None:
        protected = backups[-1][0]
    cutoff = time.time() - MAX_BACKUP_AGE_SECONDS

    for item in list(backups):
        path, stat = item
        if path != protected and stat.st_mtime < cutoff and len(backups) > 1:
            path.unlink()
            backups.remove(item)

    total_bytes = sum(stat.st_size for _, stat in backups)
    while len(backups) > MAX_BACKUPS or total_bytes > MAX_BACKUP_BYTES:
        removable = next((item for item in backups if item[0] != protected), None)
        if removable is None:
            break
        path, stat = removable
        path.unlink()
        backups.remove(removable)
        total_bytes -= stat.st_size


def publish_configuration(
    destination: Path, replacement: bytes
) -> tuple[bool, Path | None]:
    """Replace changed config and retain a bounded verified sibling preimage."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("integration config must not be a symlink")
    original = destination.read_bytes() if destination.exists() else None
    if original == replacement:
        _prune_backups(destination, None)
        return False, None

    backup = _create_verified_backup(destination, original) if original is not None else None
    _atomic_write_verified(destination, replacement)
    _prune_backups(destination, backup)
    return True, backup
