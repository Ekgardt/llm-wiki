"""Offline two-pass file fingerprints; callers own writer exclusion."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from bounded_io import IO_CHUNK_BYTES, path_descriptor_change_time_matches
from llm_wiki.runtime_contracts import RuntimeFileIdentity
from llm_wiki.runtime_records import runtime_identity_from_metadata, validated_runtime_metadata
from llm_wiki.sqlite_deadlines import operational_deadline_active


def fingerprint_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def require_fingerprint_metadata(expected, actual) -> None:
    if fingerprint_metadata(expected) != fingerprint_metadata(actual):
        raise PermissionError("runtime file changed during fingerprint")


def _path_descriptor_fingerprint_metadata(metadata):
    mode = metadata.st_mode
    if os.name == "nt":
        # CPython adds name-derived execute bits to path stat, but not fstat.
        # Same-API comparisons keep the original complete mode unchanged.
        mode &= ~0o111
    return (metadata.st_dev, metadata.st_ino, mode, metadata.st_size, metadata.st_mtime_ns)


def require_opened_fingerprint_metadata(expected, opened):
    same_fields = _path_descriptor_fingerprint_metadata(expected) == _path_descriptor_fingerprint_metadata(opened)
    if not same_fields or not path_descriptor_change_time_matches(expected, opened):
        raise PermissionError("runtime file changed during fingerprint")


def digest_runtime_descriptor(descriptor: int, size: int, deadline) -> str:
    digest, length = hashlib.sha256(), 0
    while length <= size:
        operational_deadline_active(deadline)
        chunk = os.read(descriptor, min(IO_CHUNK_BYTES, size + 1 - length))
        if not chunk:
            break
        digest.update(chunk)
        length += len(chunk)
    operational_deadline_active(deadline)
    if length != size:
        raise PermissionError("runtime file size changed during fingerprint")
    return digest.hexdigest()


def verified_runtime_digest(descriptor, expected, deadline) -> str:
    digest = digest_runtime_descriptor(descriptor, expected.st_size, deadline)
    require_fingerprint_metadata(expected, os.fstat(descriptor))
    os.lseek(descriptor, 0, os.SEEK_SET)
    if digest_runtime_descriptor(descriptor, expected.st_size, deadline) != digest:
        raise PermissionError("runtime file content changed during fingerprint")
    return digest


def fingerprint_runtime_file(
    path: Path, state_root: Path, *, max_bytes: int | None = None, deadline=None,
) -> tuple[str, RuntimeFileIdentity]:
    """Stream a stable owner-only artifact, binding its digest to its identity.

    Only use for offline artifacts: closing a descriptor for a live SQLite file
    can release this process's advisory locks. The maintenance fence and closed
    SQLite connections remain the caller's responsibility. Chunk size bounds
    memory, not accepted file size; a growing file is refused at its initial size.
    A second hash detects rewrites hidden by coarse/restored timestamps. This
    consistency witness cannot replace exclusion of noncooperating writers.
    """
    return fingerprint_regular_file(
        path, state_root, owner_only=True, max_bytes=max_bytes, deadline=deadline,
    )


def fingerprint_regular_file(
    path: Path, root: Path, *, owner_only: bool = False,
    max_bytes: int | None = None, deadline=None,
) -> tuple[str, RuntimeFileIdentity]:
    """Fingerprint stable regular bytes without following a leaf symlink.

    Code and installed dependencies may be publicly readable. Runtime callers
    retain their owner-only requirement through fingerprint_runtime_file.
    This is the same two-pass consistency witness, not a writer exclusion lock;
    the offline-file restriction of fingerprint_runtime_file also applies here.
    """
    operational_deadline_active(deadline)
    path = Path(path)
    expected = validated_runtime_metadata(
        path, root, max_bytes=max_bytes, owner_only=owner_only,
    )
    identity = runtime_identity_from_metadata(path, expected)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        require_opened_fingerprint_metadata(expected, opened)
        digest = verified_runtime_digest(descriptor, opened, deadline)
        require_fingerprint_metadata(opened, os.fstat(descriptor))
        require_fingerprint_metadata(expected, path.stat(follow_symlinks=False))
        return digest, identity
    finally:
        os.close(descriptor)


fingerprint_runtime_file.__module__ = "reliable_memory"
fingerprint_regular_file.__module__ = "reliable_memory"
