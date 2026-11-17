"""Queue artifact byte IO; task decisions and SQL stay with the queue."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import uuid
from functools import partial
from pathlib import Path

from llm_wiki.queue_contracts import QueueOperationError
from llm_wiki.runtime_environment import set_owner_only
from llm_wiki.runtime_sync import fsync_directory, fsync_file
from llm_wiki.sqlite_deadlines import operational_deadline_active
from llm_wiki.windows_acl import is_owner_only_windows


def harden_owner_only(path: Path, mode: int) -> None:
    if os.name == "nt":
        from llm_wiki.windows_acl import harden_windows_acl  # noqa: E402

        harden_windows_acl(path)
        return
    if not set_owner_only(path, mode):
        raise PermissionError(f"could not apply owner-only permissions to {path}")


def _is_owner_only_posix(path: Path) -> bool:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode & 0o077 == 0 and mode & 0o600 == 0o600


def is_owner_only(path: Path) -> bool:
    if os.name == "nt":
        return is_owner_only_windows(path)
    return _is_owner_only_posix(path)


def write_result_file(path: Path, payload: bytes) -> None:
    """Write the result to a fresh owner-only temporary file, durably."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        harden_owner_only(path, 0o600)
        handle = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def link_result(temporary: Path, target: Path) -> bool:
    """Link the staged bytes into place; False when a result was already there."""
    if target.exists() or target.is_symlink():
        return False
    try:
        os.link(temporary, target)
    except FileExistsError:
        return False
    return True


def discard_temporary(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_durable_file(path: Path, data: bytes) -> None:
    """Small-result compatibility delegates to the same durable stream writer."""
    write_durable_stream(path, (data,))


def stream_digest(handle, expected_size, *, deadline=None):
    # The stdlib file buffer is an I/O working-set choice, never a file-size cap.
    operational_deadline_active(deadline)
    digest = hashlib.sha256()
    size = 0
    for chunk in artifact_chunks(handle, expected_size, deadline):
        digest.update(chunk)
        size += len(chunk)
    operational_deadline_active(deadline)
    return digest.hexdigest(), size


def artifact_chunks(handle, remaining, deadline):
    """Read the observed file extent; concurrent growth cannot extend this scan."""
    while remaining:
        operational_deadline_active(deadline)
        chunk = handle.read(min(io.DEFAULT_BUFFER_SIZE, remaining))
        if not chunk:
            raise QueueOperationError('artifact_truncated_during_read')
        remaining -= len(chunk)
        yield chunk


def require_stream_identity(path, descriptor, before):
    after = os.fstat(descriptor)
    named = path.lstat()
    if file_identity(before) != file_identity(after):
        raise QueueOperationError('artifact_changed_during_read')
    if file_identity(after) != file_identity(named):
        raise QueueOperationError('artifact_replaced_during_read')
    check_owner_file_metadata(path, named, named.st_size)


def digest_stable_owner_file(path, *, deadline=None):
    before = path.lstat()
    check_owner_file_metadata(path, before, before.st_size)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as handle:
        require_stream_identity(path, descriptor, before)
        result = stream_digest(handle, before.st_size, deadline=deadline)
        require_stream_identity(path, descriptor, before)
        return result


class ArtifactOutput:
    """Sequential output accepted by stdlib archive writers; no seek or overwrite."""

    def __init__(self, handle, deadline):
        self.handle = handle
        self.deadline = deadline
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data):
        operational_deadline_active(self.deadline)
        written = self.handle.write(data)
        if written != len(data):
            raise QueueOperationError('artifact_short_write')
        self.digest.update(data)
        self.size += written
        return written

    def tell(self):
        return self.size

    def flush(self):
        self.handle.flush()


def copy_artifact_chunks(output, *, chunks):
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError('artifact chunks must be bytes')
        output.write(chunk)


def _require_matching_stream_file(path, digest):
    try:
        existing, _ = digest_stable_owner_file(path)
    except (OSError, QueueOperationError) as error:
        raise QueueOperationError('durable_file_conflict') from error
    if existing != digest:
        raise QueueOperationError('durable_file_conflict')


def _publish_stream_file(temporary, path, digest):
    try:
        os.link(temporary, path)
    except FileExistsError:
        _require_matching_stream_file(path, digest)
    fsync_file(path)
    fsync_directory(path.parent)


def write_durable_stream(path, chunks, *, deadline=None):
    return write_durable_producer(
        path, partial(copy_artifact_chunks, chunks=chunks), deadline=deadline)


def write_durable_producer(path, producer, *, deadline=None):
    operational_deadline_active(deadline)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            output = ArtifactOutput(handle, deadline)
            producer(output)
            handle.flush()
            os.fsync(handle.fileno())
            digest = output.digest.hexdigest()
        operational_deadline_active(deadline)
        harden_owner_only(temporary, 0o600)
        _publish_stream_file(temporary, path, digest)
        return digest
    finally:
        temporary.unlink(missing_ok=True)


def check_owner_file_metadata(
    path: Path, metadata: os.stat_result, max_bytes: int
) -> None:
    """Refuse anything that is not a plain, small, owner-only file."""
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise QueueOperationError("export_verification_failed")
    if metadata.st_size > max_bytes or not is_owner_only(path):
        raise QueueOperationError("export_verification_failed")


def file_identity(status: os.stat_result) -> tuple[int, int, int, int]:
    """What must not change underneath a file while it is being read."""
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)


def read_stable_descriptor(
    descriptor: int, metadata: os.stat_result, max_bytes: int
) -> bytes:
    """The bytes of the checked file, proved unchanged across the read."""
    opened = os.fstat(descriptor)
    if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
        raise QueueOperationError("export_verification_failed")
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes or file_identity(opened) != file_identity(
        os.fstat(descriptor)
    ):
        raise QueueOperationError("export_verification_failed")
    return data


def read_stable_owner_file(path: Path, max_bytes: int) -> bytes:
    metadata = path.lstat()
    check_owner_file_metadata(path, metadata, max_bytes)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        return read_stable_descriptor(descriptor, metadata, max_bytes)
    finally:
        os.close(descriptor)


def remove_export_staging(staging: Path) -> None:
    if not staging.exists():
        return
    if staging.is_symlink() or not staging.is_dir() or not is_owner_only(staging):
        raise QueueOperationError("export_staging_invalid")
    shutil.rmtree(staging)
    fsync_directory(staging.parent)


def cleanup_export_staging(parent: Path, export_name: str) -> None:
    for staging in parent.glob(f".{export_name}.staging-*"):
        remove_export_staging(staging)
