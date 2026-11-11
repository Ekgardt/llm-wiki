"""Publishing, adopting and reading the results of leased queue tasks."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.queue_artifacts import (
    discard_temporary,
    harden_owner_only,
    is_owner_only,
    link_result,
    read_stable_owner_file,
    write_result_file,
)
from llm_wiki.queue_contracts import (
    MAX_RESULT_BYTES,
    QueueLease,
    QueueOperationError,
    QueueTask,
    ResultConflictError,
)
from llm_wiki.queue_timing import as_utc, timestamp
from llm_wiki.queue_validation import (
    check_result_operation,
    check_result_request,
    require_positive_task_lease,
)
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_transactions import begin_immediate
from reliable_memory import fsync_directory, fsync_file


def _descriptor_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_stable_result(descriptor: int) -> bytes | None:
    """The file's bytes, when it neither grew nor moved while we read it."""
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_RESULT_BYTES:
        return None
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        data = handle.read(MAX_RESULT_BYTES + 1)
    if len(data) > MAX_RESULT_BYTES or _descriptor_identity(
        opened
    ) != _descriptor_identity(os.fstat(descriptor)):
        return None
    return data


def _stable_result_digest(path: Path) -> str | None:
    """The digest of a result file read without following a link or a swap."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        data = _read_stable_result(descriptor)
    finally:
        os.close(descriptor)
    return None if data is None else sha256_bytes(data)


class MemoryQueueResults:
    def _stored_result_is_valid(self, row: sqlite3.Row) -> bool:
        reference = row["result_reference"]
        digest = row["result_sha256"]
        if not isinstance(reference, str) or not isinstance(digest, str):
            return False
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False
        return self._validated_result_digest(reference) == digest

    def _result_path_usable(self, path: Path) -> bool:
        """The reference names a real, owner-only file directly in results."""
        resolved = path.resolve(strict=True)
        resolved.relative_to(self.results_dir.resolve())
        if path.parent.resolve() != self.results_dir.resolve() or path.is_symlink():
            return False
        metadata = path.lstat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_size <= MAX_RESULT_BYTES
            and is_owner_only(path)
        )

    def _validated_result_digest(self, reference: str) -> str | None:
        try:
            path = self.state_root / reference
            if not self._result_path_usable(path):
                return None
            return _stable_result_digest(path)
        except (OSError, ValueError):
            return None

    def _read_result_for_export(self, reference: str, digest: str) -> bytes:
        try:
            path = self.state_root / reference
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.results_dir.resolve())
            if path.parent.resolve() != self.results_dir.resolve():
                raise QueueOperationError("result_verification_failed")
            data = read_stable_owner_file(path, MAX_RESULT_BYTES)
            if sha256_bytes(data) != digest:
                raise QueueOperationError("result_verification_failed")
            return data
        except QueueOperationError:
            raise
        except (OSError, ValueError):
            raise QueueOperationError("result_verification_failed") from None

    def adopt_published_result(
        self, lease: QueueLease, *, operation_id: str
    ) -> str | None:
        """Adopt an owner-only stable result left before its DB reference committed."""
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        if not target.exists() and not target.is_symlink():
            return None
        now = as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            digest = self._validated_result_digest(relative)
            if digest is None:
                return self._bury_corrupt_publication(connection, row, lease, now)
            connection.execute(
                """UPDATE tasks SET result_reference=?, result_sha256=?,
                       result_operation_id=?, updated_at=? WHERE id=?""",
                (relative, digest, operation_id, timestamp(now), lease.id),
            )
            return "adopted"

    def _bury_corrupt_publication(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        lease: QueueLease,
        now: datetime,
    ) -> str:
        """Kill the leased task whose published result no longer verifies."""
        self._record_attempt(connection, row, now, "failed", "result_corrupt")
        self._finish_lease(
            connection, lease.id, now, "dead", "result_corrupt", None, last_attempt_at=now
        )
        return "corrupt"

    def heartbeat(
        self,
        lease: QueueLease,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> QueueLease:
        require_positive_task_lease(lease_seconds)
        now = as_utc(self._clock())
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._require_lease(connection, lease, now)
            connection.execute(
                "UPDATE tasks SET lease_expires_at=?, lease_heartbeat_at=?, updated_at=? WHERE id=?",
                (timestamp(expires), timestamp(now), timestamp(now), lease.id),
            )
        return replace(lease, expires_at=expires)

    def _fail_corrupt_result(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, object],
        lease: QueueLease,
        now: datetime,
    ) -> None:
        """A result we cannot read again kills the lease that published it."""
        self._record_attempt(connection, row, now, "failed", "result_corrupt")
        self._finish_lease(
            connection,
            lease.id,
            now,
            "dead",
            "result_corrupt",
            None,
            last_attempt_at=now,
        )

    def _existing_result_conflict(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, object],
        lease: QueueLease,
        now: datetime,
        relative: str,
        digest: str,
    ) -> ResultConflictError | None:
        """Why the result already in place cannot stand for this publication."""
        existing_digest = self._validated_result_digest(relative)
        if existing_digest is None:
            self._fail_corrupt_result(connection, row, lease, now)
            return ResultConflictError("result_corrupt")
        if existing_digest != digest:
            return ResultConflictError(
                "operation ID already has different result bytes"
            )
        return None

    def _unlinked_result_conflict(
        self,
        linked: bool,
        connection: sqlite3.Connection,
        row: Mapping[str, object],
        lease: QueueLease,
        now: datetime,
        relative: str,
        digest: str,
    ) -> ResultConflictError | None:
        if linked:
            return None
        return self._existing_result_conflict(connection, row, lease, now, relative, digest)

    def _record_published_result(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        now: datetime,
        relative: str,
        digest: str,
        operation_id: str,
        target: Path,
        linked: bool,
    ) -> None:
        """Make the published bytes, and the row that names them, durable."""
        if linked:
            harden_owner_only(target, 0o600)
            fsync_file(target)
        fsync_directory(self.results_dir)
        connection.execute(
            """UPDATE tasks SET result_reference=?, result_sha256=?,
                   result_operation_id=?, updated_at=?
               WHERE id=?""",
            (relative, digest, operation_id, timestamp(now), lease.id),
        )

    def _commit_result(
        self,
        lease: QueueLease,
        operation_id: str,
        digest: str,
        relative: str,
        target: Path,
        temporary: Path,
    ) -> ResultConflictError | None:
        """Publish the staged bytes under the lease, or say why it cannot be."""
        now = as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            check_result_operation(row, operation_id)
            linked = link_result(temporary, target)
            conflict = self._unlinked_result_conflict(
                linked, connection, row, lease, now, relative, digest
            )
            if conflict is not None:
                return conflict
            self._record_published_result(
                connection, lease, now, relative, digest, operation_id, target, linked
            )
        return None

    def publish_result(
        self, lease: QueueLease, *, operation_id: str, result: bytes
    ) -> str:
        check_result_request(operation_id, result)
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        temporary = self.results_dir / f".{result_name}.{uuid.uuid4().hex}.tmp"
        try:
            write_result_file(temporary, result)
            conflict = self._commit_result(
                lease,
                operation_id,
                sha256_bytes(result),
                relative,
                target,
                temporary,
            )
        finally:
            discard_temporary(temporary)
        if conflict is not None:
            raise conflict
        return relative

    def acknowledge(self, lease: QueueLease) -> QueueTask:
        now = as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if not self._stored_result_is_valid(row):
                self._record_attempt(
                    connection, row, now, "failed", "result_corrupt"
                )
                self._finish_lease(
                    connection,
                    lease.id,
                    now,
                    "dead",
                    "result_corrupt",
                    None,
                    last_attempt_at=now,
                )
            else:
                self._record_attempt(connection, row, now, "succeeded", None)
                self._finish_lease(
                    connection, lease.id, now, "succeeded", None, None
                )
        return self.get(lease.id)
