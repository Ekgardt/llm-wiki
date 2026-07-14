"""Fenced SQLite queue for deferred memory-pipeline work."""

from __future__ import annotations

import email.utils
import json
import math
import os
import random
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reliable_memory import (
    DEFAULTS,
    _set_owner_only,
    begin_immediate,
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    open_operational_db,
    sha256_bytes,
)
from secret_redact import redact_secrets

_STATES = ("ready", "leased", "blocked", "succeeded", "dead", "cancelled")
_TERMINAL_STATES = ("succeeded", "dead", "cancelled")
_PERMANENT_CODES = {"invalid_input", "unsupported_version"}
_MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "pass",
    "passwd",
    "passphrase",
    "password",
    "private_key",
    "secret",
    "set_cookie",
    "token",
}


class LeaseFenceError(RuntimeError):
    """Raised when a lease token no longer owns an unexpired task."""


class ResultConflictError(RuntimeError):
    """Raised when an operation ID already names different result bytes."""


class QueueOperationError(RuntimeError):
    """Operator-visible queue failure with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MigrationBusy(QueueOperationError):
    """Raised when migration cannot prove exclusive legacy ownership."""


class LegacyBackendDisabled(QueueOperationError):
    """Raised when a legacy writer is used after migration started."""


@dataclass(frozen=True)
class QueueFailure:
    error_code: str
    permanent: bool = False
    blocked_capability: str | None = None
    retry_after: float | datetime | str | None = None


@dataclass(frozen=True)
class DeferredResult:
    data: bytes


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    started_at: datetime
    finished_at: datetime
    outcome: str
    error_code: str | None


@dataclass(frozen=True)
class QueueTask:
    id: str
    kind: str
    handler_version: int
    payload: dict[str, object]
    input_hash: str
    dedupe_key: str | None
    state: str
    priority: int
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    attempts: int
    last_attempt_at: datetime | None
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_heartbeat_at: datetime | None
    error_code: str | None
    blocked_capability: str | None
    result_reference: str | None
    result_sha256: str | None
    redrive_of: str | None
    attempt_history: tuple[AttemptRecord, ...]


@dataclass(frozen=True)
class QueueLease:
    id: str
    kind: str
    handler_version: int
    payload: dict[str, object]
    input_hash: str
    owner: str
    token: str
    expires_at: datetime
    attempt: int
    created_at: datetime
    last_attempt_at: datetime | None
    prior_attempts: int


@dataclass(frozen=True)
class MigrationReceipt:
    imported: int
    quarantined: int
    task_ids: tuple[str, ...]
    codes: tuple[str, ...]


@dataclass(frozen=True)
class WorkerSummary:
    processed: int
    succeeded: int
    failed: int
    dead: int
    skipped: int


@dataclass(frozen=True)
class PurgeReceipt:
    purged: int
    task_ids: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: str | None) -> datetime | None:
    return _as_utc(datetime.fromisoformat(value)) if value is not None else None


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized in _SECRET_KEYS or normalized.endswith(
        ("_api_key", "_authorization", "_cookie", "_credential", "_password", "_secret", "_token")
    )


def _redact_payload(value: object) -> object:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, (list, tuple)):
        return [_redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(key) else _redact_payload(item)
            for key, item in value.items()
        }
    return value


def _harden_owner_only(path: Path, mode: int) -> None:
    if os.name == "nt":
        from markdown_transaction import _harden_windows_acl

        _harden_windows_acl(path)
        return
    if not _set_owner_only(path, mode):
        raise PermissionError(f"could not apply owner-only permissions to {path}")


def _is_owner_only(path: Path) -> bool:
    if os.name == "nt":
        from markdown_transaction import (
            _acl_output_text,
            _run_acl_command,
            _windows_acl_identity,
        )

        try:
            verified = _run_acl_command(["icacls", str(path)])
        except Exception:  # noqa: BLE001 - validation is fail-closed
            return False
        if verified.returncode != 0:
            return False
        identity = _windows_acl_identity()
        acl_lines = [
            line.strip()
            for line in _acl_output_text(verified.stdout).splitlines()
            if ":(" in line
        ]
        owner_lines = [
            line for line in acl_lines if identity.casefold() in line.casefold()
        ]
        return (
            len(owner_lines) == 1
            and "(F)" in owner_lines[0]
            and all(identity.casefold() in line.casefold() for line in acl_lines)
        )
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode & 0o077 == 0 and mode & 0o600 == 0o600


class MemoryQueue:
    """Durable priority queue with lease-token fencing and at-least-once delivery."""

    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | random.SystemRandom | None = None,
        heartbeat_wait: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.run_dir = self.state_root / "run"
        self.db_path = self.run_dir / "queue.sqlite3"
        self.results_dir = self.run_dir / "queue-results"
        self._clock = clock or _utc_now
        self._rng = rng or random.SystemRandom()
        self._heartbeat_wait = heartbeat_wait or (
            lambda stop, interval: stop.wait(interval)
        )
        self._db_hardened = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.run_dir, 0o700)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.results_dir, 0o700)
        with self._connect() as connection:
            self._create_schema(connection)
            with begin_immediate(connection):
                self._retire_exhausted_ready(connection, _as_utc(self._clock()))

    def _connect(self) -> sqlite3.Connection:
        connection = open_operational_db(self.db_path, busy_ms=DEFAULTS.queue_busy_ms)
        if not self._db_hardened:
            try:
                _harden_owner_only(self.db_path, 0o600)
            except Exception:
                connection.close()
                raise
            self._db_hardened = True
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                handler_version INTEGER NOT NULL CHECK (handler_version >= 1),
                payload_json TEXT NOT NULL,
                input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
                dedupe_key TEXT UNIQUE,
                state TEXT NOT NULL CHECK (state IN ('ready','leased','blocked','succeeded','dead','cancelled')),
                priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                last_attempt_at TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                lease_heartbeat_at TEXT,
                attempt_started_at TEXT,
                error_code TEXT,
                blocked_capability TEXT,
                result_reference TEXT,
                result_sha256 TEXT,
                result_operation_id TEXT,
                redrive_of TEXT REFERENCES tasks(id)
            );
            CREATE INDEX IF NOT EXISTS queue_claim_order
                ON tasks(state, priority DESC, available_at, created_at);
            CREATE TABLE IF NOT EXISTS attempt_history (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','blocked','cancelled','lease_expired')),
                error_code TEXT
            );
            CREATE TRIGGER IF NOT EXISTS attempt_history_immutable_update
                BEFORE UPDATE ON attempt_history BEGIN SELECT RAISE(ABORT, 'attempt history is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS attempt_history_immutable_delete
                BEFORE DELETE ON attempt_history BEGIN SELECT RAISE(ABORT, 'attempt history is immutable'); END;
            """
        )
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "last_attempt_at" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN last_attempt_at TEXT")
        if "result_sha256" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN result_sha256 TEXT")
        if "redrive_of" not in columns:
            connection.execute(
                "ALTER TABLE tasks ADD COLUMN redrive_of TEXT REFERENCES tasks(id)"
            )

    @staticmethod
    def _retire_exhausted_ready(
        connection: sqlite3.Connection, now: datetime
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='ready' AND attempts >= ?",
            (DEFAULTS.queue_max_attempts,),
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE tasks SET state='dead', error_code='attempts_exhausted',
                       updated_at=?, last_attempt_at=COALESCE(last_attempt_at, ?)
                   WHERE id=? AND state='ready' AND attempts >= ?""",
                (
                    _timestamp(now),
                    _timestamp(now),
                    row["id"],
                    DEFAULTS.queue_max_attempts,
                ),
            )

    def enqueue(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> str:
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if not isinstance(handler_version, int) or isinstance(handler_version, bool) or handler_version < 1:
            raise ValueError("handler_version must be a positive integer")
        if not isinstance(priority, int) or isinstance(priority, bool) or not -100 <= priority <= 100:
            raise ValueError("priority must be an integer from -100 to 100")
        if dedupe_key is not None and (not isinstance(dedupe_key, str) or not dedupe_key):
            raise ValueError("dedupe_key must be a non-empty string")
        redacted = _redact_payload(dict(payload))
        payload_bytes = canonical_json_bytes(redacted)
        payload_json = payload_bytes.decode("utf-8")
        now = _as_utc(self._clock())
        ready_at = _as_utc(available_at or now)
        task_id = uuid.UUID(int=self._rng.getrandbits(128)).hex
        try:
            with self._connect() as connection, begin_immediate(connection):
                connection.execute(
                    """INSERT INTO tasks(
                           id, kind, handler_version, payload_json, input_hash, dedupe_key,
                           state, priority, created_at, updated_at, available_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)""",
                    (
                        task_id,
                        kind,
                        handler_version,
                        payload_json,
                        sha256_bytes(payload_bytes),
                        dedupe_key,
                        priority,
                        _timestamp(now),
                        _timestamp(now),
                        _timestamp(ready_at),
                    ),
                )
        except sqlite3.IntegrityError:
            if dedupe_key is None:
                raise
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT id FROM tasks WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
            if row is None:
                raise
            return str(row["id"])
        return task_id

    def claim(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> QueueLease | None:
        if not owner:
            raise ValueError("owner must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            self._expire_leases(connection, now)
            row = connection.execute(
                """SELECT * FROM tasks
                   WHERE state = 'ready' AND attempts < ? AND available_at <= ?
                   ORDER BY priority DESC, available_at, created_at, rowid LIMIT 1""",
                (DEFAULTS.queue_max_attempts, _timestamp(now)),
            ).fetchone()
            if row is None:
                return None
            token = f"{self._rng.getrandbits(256):064x}"
            expires = now + timedelta(seconds=lease_seconds)
            changed = connection.execute(
                """UPDATE tasks SET state='leased', attempts=attempts+1,
                       lease_owner=?, lease_token=?, lease_expires_at=?, lease_heartbeat_at=?,
                       attempt_started_at=?, updated_at=?, error_code=NULL, blocked_capability=NULL
                   WHERE id=? AND state='ready' AND attempts < ?""",
                (
                    owner,
                    token,
                    _timestamp(expires),
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    row["id"],
                    DEFAULTS.queue_max_attempts,
                ),
            ).rowcount
            if changed != 1:
                return None
            attempt = int(row["attempts"]) + 1
        return QueueLease(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=json.loads(row["payload_json"]),
            input_hash=str(row["input_hash"]),
            owner=owner,
            token=token,
            expires_at=expires,
            attempt=attempt,
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    def _claim_task(self, task_id: str, owner: str) -> QueueLease | None:
        """Claim one known task for the legacy mark_attempt facade."""
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=? AND state='ready'", (task_id,)
            ).fetchone()
            if row is None:
                return None
            token = f"{self._rng.getrandbits(256):064x}"
            expires = now + timedelta(seconds=DEFAULTS.queue_lease_seconds)
            connection.execute(
                """UPDATE tasks SET state='leased', attempts=attempts+1, lease_owner=?,
                       lease_token=?, lease_expires_at=?, lease_heartbeat_at=?,
                       attempt_started_at=?, updated_at=? WHERE id=?""",
                (
                    owner,
                    token,
                    _timestamp(expires),
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    task_id,
                ),
            )
        return QueueLease(
            task_id,
            row["kind"],
            row["handler_version"],
            json.loads(row["payload_json"]),
            row["input_hash"],
            owner,
            token,
            expires,
            int(row["attempts"]) + 1,
            _parse_timestamp(row["created_at"]),
            _parse_timestamp(row["last_attempt_at"]),
            int(row["attempts"]),
        )

    def _expire_leases(self, connection: sqlite3.Connection, now: datetime) -> int:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='leased' AND lease_expires_at <= ?",
            (_timestamp(now),),
        ).fetchall()
        for row in rows:
            if row["result_reference"] is not None or row["result_sha256"] is not None:
                valid = self._stored_result_is_valid(row)
                error_code = None if valid else "result_corrupt"
                self._record_attempt(
                    connection,
                    row,
                    now,
                    "succeeded" if valid else "failed",
                    error_code,
                )
                self._finish_lease(
                    connection,
                    row["id"],
                    now,
                    "succeeded" if valid else "dead",
                    error_code,
                    None,
                    last_attempt_at=now,
                )
                continue
            exhausted = int(row["attempts"]) >= DEFAULTS.queue_max_attempts
            error_code = "attempts_exhausted" if exhausted else "lease_expired"
            state = "dead" if exhausted else "ready"
            self._record_attempt(connection, row, now, "lease_expired", error_code)
            connection.execute(
                """UPDATE tasks SET state=?, available_at=?, updated_at=?, last_attempt_at=?,
                       lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                       lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?
                   WHERE id=? AND state='leased' AND lease_token=?""",
                (
                    state,
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    error_code,
                    row["id"],
                    row["lease_token"],
                ),
            )
        return len(rows)

    def _stored_result_is_valid(self, row: sqlite3.Row) -> bool:
        reference = row["result_reference"]
        digest = row["result_sha256"]
        if not isinstance(reference, str) or not isinstance(digest, str):
            return False
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False
        return self._validated_result_digest(reference) == digest

    def _validated_result_digest(self, reference: str) -> str | None:
        try:
            path = self.state_root / reference
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.results_dir.resolve())
            if path.parent.resolve() != self.results_dir.resolve() or path.is_symlink():
                return None
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_RESULT_BYTES:
                return None
            if not _is_owner_only(path):
                return None
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_RESULT_BYTES:
                    return None
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    data = handle.read(_MAX_RESULT_BYTES + 1)
                if len(data) > _MAX_RESULT_BYTES:
                    return None
                after = os.fstat(descriptor)
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    return None
                return sha256_bytes(data)
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            return None

    def adopt_published_result(
        self, lease: QueueLease, *, operation_id: str
    ) -> str | None:
        """Adopt an owner-only stable result left before its DB reference committed."""
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        if not target.exists() and not target.is_symlink():
            return None
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            digest = self._validated_result_digest(relative)
            if digest is None:
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
                return "corrupt"
            connection.execute(
                """UPDATE tasks SET result_reference=?, result_sha256=?,
                       result_operation_id=?, updated_at=? WHERE id=?""",
                (relative, digest, operation_id, _timestamp(now), lease.id),
            )
            return "adopted"

    def heartbeat(
        self,
        lease: QueueLease,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> QueueLease:
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        now = _as_utc(self._clock())
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._require_lease(connection, lease, now)
            connection.execute(
                "UPDATE tasks SET lease_expires_at=?, lease_heartbeat_at=?, updated_at=? WHERE id=?",
                (_timestamp(expires), _timestamp(now), _timestamp(now), lease.id),
            )
        return replace(lease, expires_at=expires)

    def publish_result(
        self, lease: QueueLease, *, operation_id: str, result: bytes
    ) -> str:
        if not operation_id:
            raise ValueError("operation_id must be non-empty")
        if not isinstance(result, bytes):
            raise TypeError("result must be bytes")
        if len(result) > _MAX_RESULT_BYTES:
            raise ValueError("result exceeds maximum queue result size")
        digest = sha256_bytes(result)
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        temporary = self.results_dir / f".{result_name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        descriptor_open = True
        try:
            _harden_owner_only(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor_open = False
                handle.write(result)
                handle.flush()
                os.fsync(handle.fileno())
            now = _as_utc(self._clock())
            publication_error: ResultConflictError | None = None
            with self._connect() as connection, begin_immediate(connection):
                row = self._require_lease(connection, lease, now)
                existing_operation = row["result_operation_id"]
                if existing_operation is not None and existing_operation != operation_id:
                    raise ResultConflictError("lease already published a different operation")
                existing = target.exists() or target.is_symlink()
                linked = False
                if not existing:
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        existing = True
                    else:
                        linked = True
                if existing:
                    existing_digest = self._validated_result_digest(relative)
                    if existing_digest is None:
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
                        publication_error = ResultConflictError("result_corrupt")
                    elif existing_digest != digest:
                        publication_error = ResultConflictError(
                            "operation ID already has different result bytes"
                        )
                if publication_error is None:
                    if linked:
                        _harden_owner_only(target, 0o600)
                        fsync_file(target)
                    fsync_directory(self.results_dir)
                    connection.execute(
                        """UPDATE tasks SET result_reference=?, result_sha256=?,
                               result_operation_id=?, updated_at=?
                           WHERE id=?""",
                        (relative, digest, operation_id, _timestamp(now), lease.id),
                    )
            if publication_error is not None:
                raise publication_error
            return relative
        finally:
            if descriptor_open:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass

    def acknowledge(self, lease: QueueLease) -> QueueTask:
        now = _as_utc(self._clock())
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

    def fail(self, lease: QueueLease, failure: QueueFailure) -> None:
        if not failure.error_code:
            raise ValueError("error_code must be non-empty")
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if failure.blocked_capability:
                self._record_attempt(connection, row, now, "blocked", failure.error_code)
                connection.execute(
                    """UPDATE tasks SET state='blocked', attempts=attempts-1, updated_at=?,
                           lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                           lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?,
                           blocked_capability=? WHERE id=?""",
                    (
                        _timestamp(now),
                        failure.error_code,
                        failure.blocked_capability,
                        lease.id,
                    ),
                )
                return
            dead = (
                failure.permanent
                or failure.error_code in _PERMANENT_CODES
                or int(row["attempts"]) >= DEFAULTS.queue_max_attempts
            )
            self._record_attempt(connection, row, now, "failed", failure.error_code)
            if dead:
                self._finish_lease(
                    connection,
                    lease.id,
                    now,
                    "dead",
                    failure.error_code,
                    None,
                    last_attempt_at=now,
                )
                return
            delay = self._retry_delay(int(row["attempts"]))
            retry_after = self._retry_after_seconds(failure.retry_after, now)
            if retry_after is not None:
                delay = max(delay, retry_after)
            self._finish_lease(
                connection,
                lease.id,
                now,
                "ready",
                failure.error_code,
                now + timedelta(seconds=delay),
                last_attempt_at=now,
            )

    def _retry_delay(self, attempts: int) -> float:
        jitter_cap = min(
            DEFAULTS.retry_cap_seconds,
            DEFAULTS.retry_base_seconds * (2 ** (attempts - 1)),
        )
        return self._rng.uniform(0, jitter_cap)

    @staticmethod
    def _retry_after_seconds(
        value: float | datetime | str | None, now: datetime
    ) -> float | None:
        if isinstance(value, int) and not isinstance(value, bool):
            if value < 0:
                return None
            return float(min(value, _MAX_RETRY_AFTER_SECONDS))
        if isinstance(value, float):
            if not math.isfinite(value) or value < 0:
                return None
            return min(value, float(_MAX_RETRY_AFTER_SECONDS))
        if isinstance(value, datetime):
            seconds = max(0.0, (_as_utc(value) - now).total_seconds())
            return min(seconds, float(_MAX_RETRY_AFTER_SECONDS))
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isdigit():
                try:
                    seconds = int(stripped)
                except ValueError:
                    return None
                return float(min(seconds, _MAX_RETRY_AFTER_SECONDS))
            try:
                parsed = email.utils.parsedate_to_datetime(stripped)
            except (TypeError, ValueError, OverflowError):
                return None
            seconds = max(0.0, (_as_utc(parsed) - now).total_seconds())
            return min(seconds, float(_MAX_RETRY_AFTER_SECONDS))
        return None

    @staticmethod
    def _record_attempt(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        outcome: str,
        error_code: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO attempt_history(
                   task_id, attempt, started_at, finished_at, outcome, error_code
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["attempts"],
                row["attempt_started_at"] or _timestamp(now),
                _timestamp(now),
                outcome,
                error_code,
            ),
        )

    @staticmethod
    def _finish_lease(
        connection: sqlite3.Connection,
        task_id: str,
        now: datetime,
        state: str,
        error_code: str | None,
        available_at: datetime | None,
        *,
        last_attempt_at: datetime | None = None,
    ) -> None:
        connection.execute(
            """UPDATE tasks SET state=?, updated_at=?, available_at=COALESCE(?, available_at),
                   last_attempt_at=COALESCE(?, last_attempt_at),
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?,
                   blocked_capability=NULL WHERE id=?""",
            (
                state,
                _timestamp(now),
                _timestamp(available_at) if available_at else None,
                _timestamp(last_attempt_at) if last_attempt_at else None,
                error_code,
                task_id,
            ),
        )

    @staticmethod
    def _require_lease(
        connection: sqlite3.Connection, lease: QueueLease, now: datetime
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (lease.id,)).fetchone()
        if (
            row is None
            or row["state"] != "leased"
            or row["lease_owner"] != lease.owner
            or row["lease_token"] != lease.token
            or row["lease_expires_at"] <= _timestamp(now)
        ):
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        return row

    def cancel(self, task_id: str) -> bool:
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in _TERMINAL_STATES:
                return False
            if row["state"] == "leased":
                self._record_attempt(connection, row, now, "cancelled", "cancelled")
            connection.execute(
                """UPDATE tasks SET state='cancelled', updated_at=?, lease_owner=NULL,
                       lease_token=NULL, lease_expires_at=NULL, lease_heartbeat_at=NULL,
                       attempt_started_at=NULL, error_code='cancelled' WHERE id=?""",
                (_timestamp(now), task_id),
            )
            return True

    def redrive(self, task_id: str) -> str:
        original = self.get(task_id)
        if original.state != "dead":
            raise QueueOperationError("redrive_requires_dead")
        replacement = self.enqueue(
            original.kind,
            original.handler_version,
            original.payload,
            priority=original.priority,
        )
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            connection.execute(
                "UPDATE tasks SET redrive_of=?, updated_at=? WHERE id=?",
                (task_id, _timestamp(now), replacement),
            )
        return replacement

    def retains_run_directory(self) -> bool:
        with self._connect() as connection:
            task = connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
        if task is not None:
            return True
        try:
            return any(self.results_dir.iterdir())
        except OSError:
            return True

    def purge(
        self, *, terminal_before: datetime, export_path: Path
    ) -> PurgeReceipt:
        requested_cutoff = _as_utc(terminal_before)
        retention_cutoff = _as_utc(self._clock()) - timedelta(
            days=DEFAULTS.queue_result_retention_days
        )
        cutoff = min(requested_cutoff, retention_cutoff)
        export = Path(export_path).resolve()
        if export.exists():
            raise QueueOperationError("export_exists")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM tasks
                   WHERE state IN ('succeeded','cancelled') AND updated_at < ?
                   ORDER BY created_at, rowid""",
                (_timestamp(cutoff),),
            ).fetchall()
            histories = {
                str(row["id"]): connection.execute(
                    "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence",
                    (row["id"],),
                ).fetchall()
                for row in rows
            }
        task_ids = tuple(str(row["id"]) for row in rows)
        records = [
            _export_task_record(self._task_from_row(row, histories[str(row["id"])]))
            for row in rows
        ]
        records_bytes = canonical_json_bytes(records)
        export.parent.mkdir(parents=True, exist_ok=True)
        export.mkdir()
        _harden_owner_only(export, 0o700)
        results_export = export / "results"
        results_export.mkdir()
        _harden_owner_only(results_export, 0o700)
        result_manifest: list[dict[str, str]] = []
        for row in rows:
            reference = row["result_reference"]
            digest = row["result_sha256"]
            if reference is None:
                continue
            if not isinstance(digest, str) or self._validated_result_digest(reference) != digest:
                raise QueueOperationError("result_verification_failed")
            source = self.state_root / str(reference)
            target = results_export / f"{row['id']}.result"
            _write_durable_file(target, source.read_bytes())
            result_manifest.append({"id": str(row["id"]), "sha256": digest})
        records_path = export / "records.json"
        _write_durable_file(records_path, records_bytes)
        manifest = {
            "records_sha256": sha256_bytes(records_bytes),
            "results": result_manifest,
            "task_ids": list(task_ids),
        }
        manifest_path = export / "manifest.json"
        _write_durable_file(manifest_path, canonical_json_bytes(manifest))
        fsync_directory(results_export)
        fsync_directory(export)
        fsync_directory(export.parent)
        if sha256_bytes(records_path.read_bytes()) != manifest["records_sha256"]:
            raise QueueOperationError("export_verification_failed")
        for item in result_manifest:
            exported = results_export / f"{item['id']}.result"
            if sha256_bytes(exported.read_bytes()) != item["sha256"]:
                raise QueueOperationError("export_verification_failed")
        if canonical_json_bytes(json.loads(manifest_path.read_bytes())) != manifest_path.read_bytes():
            raise QueueOperationError("export_verification_failed")
        if not all(
            _is_owner_only(path)
            for path in (export, results_export, records_path, manifest_path)
        ):
            raise QueueOperationError("export_permissions_invalid")
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            with self._connect() as connection, begin_immediate(connection):
                current = connection.execute(
                    f"""SELECT id, state, updated_at FROM tasks
                        WHERE id IN ({placeholders})""",  # noqa: S608
                    task_ids,
                ).fetchall()
                if len(current) != len(task_ids) or any(
                    row["state"] not in ("succeeded", "cancelled")
                    or row["updated_at"] >= _timestamp(cutoff)
                    for row in current
                ):
                    raise QueueOperationError("purge_selection_changed")
                connection.execute("DROP TRIGGER attempt_history_immutable_delete")
                connection.execute(
                    f"DELETE FROM attempt_history WHERE task_id IN ({placeholders})",  # noqa: S608
                    task_ids,
                )
                connection.execute(
                    f"DELETE FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
                    task_ids,
                )
                connection.execute(
                    """CREATE TRIGGER attempt_history_immutable_delete
                       BEFORE DELETE ON attempt_history BEGIN
                       SELECT RAISE(ABORT, 'attempt history is immutable'); END"""
                )
            for row in rows:
                reference = row["result_reference"]
                if reference is None:
                    continue
                with self._connect() as connection:
                    retained = connection.execute(
                        "SELECT 1 FROM tasks WHERE result_reference=? LIMIT 1",
                        (reference,),
                    ).fetchone()
                if retained is None:
                    (self.state_root / str(reference)).unlink(missing_ok=True)
            fsync_directory(self.results_dir)
        return PurgeReceipt(len(task_ids), task_ids)

    def recover_expired_leases(self) -> int:
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            return self._expire_leases(connection, now)

    def get(self, task_id: str) -> QueueTask:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            history = connection.execute(
                "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence", (task_id,)
            ).fetchall()
        return self._task_from_row(row, history)

    def list_tasks(
        self, *, states: tuple[str, ...] | None = None, max_age_days: int | None = None
    ) -> list[QueueTask]:
        states = states or _STATES
        if not states or any(state not in _STATES for state in states):
            raise ValueError("invalid queue state")
        placeholders = ",".join("?" for _ in states)
        parameters: list[object] = list(states)
        age_clause = ""
        if max_age_days is not None:
            cutoff = _as_utc(self._clock()) - timedelta(days=max_age_days)
            age_clause = " AND created_at >= ?"
            parameters.append(_timestamp(cutoff))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM tasks WHERE state IN ({placeholders}){age_clause}
                    ORDER BY created_at, rowid""",  # noqa: S608 - generated placeholders
                parameters,
            ).fetchall()
            histories: dict[str, list[sqlite3.Row]] = {}
            if rows:
                ids = [row["id"] for row in rows]
                id_placeholders = ",".join("?" for _ in ids)
                for history in connection.execute(
                    f"""SELECT * FROM attempt_history WHERE task_id IN ({id_placeholders})
                        ORDER BY sequence""",  # noqa: S608
                    ids,
                ):
                    histories.setdefault(str(history["task_id"]), []).append(history)
        return [self._task_from_row(row, histories.get(str(row["id"]), [])) for row in rows]

    @staticmethod
    def _task_from_row(row: sqlite3.Row, history: list[sqlite3.Row]) -> QueueTask:
        return QueueTask(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=json.loads(row["payload_json"]),
            input_hash=str(row["input_hash"]),
            dedupe_key=row["dedupe_key"],
            state=str(row["state"]),
            priority=int(row["priority"]),
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            updated_at=_parse_timestamp(row["updated_at"]),  # type: ignore[arg-type]
            available_at=_parse_timestamp(row["available_at"]),  # type: ignore[arg-type]
            attempts=int(row["attempts"]),
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=_parse_timestamp(row["lease_expires_at"]),
            lease_heartbeat_at=_parse_timestamp(row["lease_heartbeat_at"]),
            error_code=row["error_code"],
            blocked_capability=row["blocked_capability"],
            result_reference=row["result_reference"],
            result_sha256=row["result_sha256"],
            redrive_of=row["redrive_of"],
            attempt_history=tuple(
                AttemptRecord(
                    attempt=int(item["attempt"]),
                    started_at=_parse_timestamp(item["started_at"]),  # type: ignore[arg-type]
                    finished_at=_parse_timestamp(item["finished_at"]),  # type: ignore[arg-type]
                    outcome=str(item["outcome"]),
                    error_code=item["error_code"],
                )
                for item in history
            ),
        )


def _state_root() -> Path:
    env = os.environ.get("LLM_WIKI_STATE_ROOT")
    if env:
        return Path(env)
    try:
        from memory_state import STATE_ROOT

        return Path(STATE_ROOT)
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent))


def _pid_is_alive(pid: int) -> bool:
    try:
        from memory_state import _is_pid_alive

        return _is_pid_alive(pid)
    except Exception:  # noqa: BLE001 - inability to prove death is treated as live
        return True


def _migration_paths(state_root: Path) -> tuple[Path, Path, Path, Path]:
    run_dir = Path(state_root).resolve() / "run"
    return (
        run_dir,
        run_dir / "queue",
        run_dir / "queue-migration.lock",
        run_dir / "queue-migrated-v2",
    )


def _legacy_write_allowed(state_root: Path) -> bool:
    _run_dir, _legacy_dir, lock, marker = _migration_paths(state_root)
    if marker.exists():
        raise LegacyBackendDisabled("legacy_backend_disabled")
    if lock.exists():
        raise LegacyBackendDisabled("legacy_migration_quiesced")
    return True


def _acquire_migration_lock(lock: Path) -> str:
    owner = f"{os.getpid()}:{uuid.uuid4().hex}"
    lock.parent.mkdir(parents=True, exist_ok=True)
    _harden_owner_only(lock.parent, 0o700)
    for _ in range(2):
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = lock.read_text(encoding="ascii").split(":", 1)[0]
                pid = int(existing)
            except (OSError, ValueError):
                raise MigrationBusy("migration_busy") from None
            if _pid_is_alive(pid):
                raise MigrationBusy("migration_busy")
            try:
                lock.unlink()
            except OSError:
                raise MigrationBusy("migration_busy") from None
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(owner)
            handle.flush()
            os.fsync(handle.fileno())
        _harden_owner_only(lock, 0o600)
        fsync_directory(lock.parent)
        return owner
    raise MigrationBusy("migration_busy")


def _release_migration_lock(lock: Path, owner: str) -> None:
    try:
        if lock.read_text(encoding="ascii") == owner:
            lock.unlink()
            fsync_directory(lock.parent)
    except OSError:
        pass


def _scan_legacy_records(
    legacy_dir: Path,
) -> tuple[list[tuple[Path, dict[str, object]]], list[tuple[Path, bytes]]]:
    valid: list[tuple[Path, dict[str, object]]] = []
    malformed: list[tuple[Path, bytes]] = []
    if not legacy_dir.exists():
        return valid, malformed
    paths = sorted((*legacy_dir.glob("*.json"), *legacy_dir.glob("*.processing")))
    for path in paths:
        raw = b""
        try:
            raw = path.read_bytes()
            record = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            malformed.append((path, raw))
            continue
        if path.suffix == ".processing":
            pid = record.get("lease_pid") if isinstance(record, dict) else None
            if isinstance(pid, int) and pid > 0 and _pid_is_alive(pid):
                raise MigrationBusy("legacy_owner_live")
        if not _valid_legacy_record(record):
            malformed.append((path, raw))
            continue
        valid.append((path, record))
    return valid, malformed


def _valid_legacy_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    return (
        isinstance(record.get("id"), str)
        and bool(record["id"])
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", record["id"])
        is not None
        and isinstance(record.get("type"), str)
        and bool(record["type"])
        and isinstance(record.get("payload"), dict)
        and isinstance(record.get("attempts", 0), int)
        and not isinstance(record.get("attempts", 0), bool)
        and int(record.get("attempts", 0)) >= 0
        and _safe_legacy_timestamp(record.get("enqueued_at")) is not None
    )


def _safe_legacy_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _import_legacy_record(queue: MemoryQueue, record: dict[str, object], source: Path) -> str:
    created = _safe_legacy_timestamp(record["enqueued_at"])
    if created is None:
        raise QueueOperationError("legacy_invalid")
    last_attempt = _safe_legacy_timestamp(record.get("last_attempt_at"))
    if source.suffix == ".processing":
        last_attempt = _safe_legacy_timestamp(record.get("lease_acquired_at")) or last_attempt
    attempts = int(record.get("attempts", 0))
    payload = _redact_payload(dict(record["payload"]))
    payload_bytes = canonical_json_bytes(payload)
    state = "dead" if attempts >= DEFAULTS.queue_max_attempts else "ready"
    updated = last_attempt or created
    expected = (
        str(record["type"]),
        payload_bytes.decode("utf-8"),
        sha256_bytes(payload_bytes),
        state,
        _timestamp(created),
        _timestamp(updated),
        _timestamp(updated),
        attempts,
        _timestamp(last_attempt) if last_attempt else None,
        "attempts_exhausted" if state == "dead" else None,
    )
    with queue._connect() as connection, begin_immediate(connection):
        connection.execute(
            """INSERT OR IGNORE INTO tasks(
                   id, kind, handler_version, payload_json, input_hash, state, priority,
                   created_at, updated_at, available_at, attempts, last_attempt_at,
                   error_code
               ) VALUES (?, ?, 1, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (
                record["id"],
                *expected,
            ),
        )
        stored = connection.execute(
            """SELECT kind, payload_json, input_hash, state, created_at, updated_at,
                      available_at, attempts, last_attempt_at, error_code
               FROM tasks WHERE id=?""",
            (record["id"],),
        ).fetchone()
        if stored is None or tuple(stored) != expected:
            raise QueueOperationError("legacy_import_conflict")
    return str(record["id"])


def _quarantine_legacy_record(run_dir: Path, source: Path, raw: bytes) -> None:
    quarantine = run_dir / "queue-quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    _harden_owner_only(quarantine, 0o700)
    digest = sha256_bytes(raw)
    record = {
        "code": "legacy_invalid",
        "source_name": source.name,
        "source_sha256": digest,
    }
    _write_durable_file(quarantine / f"{digest}.json", canonical_json_bytes(record))
    fsync_directory(quarantine)


def _write_durable_file(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _harden_owner_only(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data or not _is_owner_only(path):
                raise QueueOperationError("durable_file_conflict") from None
        else:
            fsync_file(path)
            fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_legacy_queue(state_root: Path) -> MigrationReceipt:
    run_dir, legacy_dir, lock, marker = _migration_paths(state_root)
    if marker.exists():
        return MigrationReceipt(0, 0, (), ())
    owner = _acquire_migration_lock(lock)
    try:
        if marker.exists():
            return MigrationReceipt(0, 0, (), ())
        valid, malformed = _scan_legacy_records(legacy_dir)
        queue = MemoryQueue(Path(state_root)) if valid else None
        imported: list[str] = []
        for source, record in valid:
            if queue is None:  # pragma: no cover - guarded by valid
                raise AssertionError
            imported.append(_import_legacy_record(queue, record, source))
            source.unlink()
        for source, raw in malformed:
            _quarantine_legacy_record(run_dir, source, raw)
            source.unlink(missing_ok=True)
        if legacy_dir.exists():
            fsync_directory(legacy_dir)
        marker_record = {"version": 2}
        _write_durable_file(marker, canonical_json_bytes(marker_record))
        fsync_directory(run_dir)
        codes = ("legacy_invalid",) if malformed else ()
        return MigrationReceipt(len(imported), len(malformed), tuple(imported), codes)
    finally:
        _release_migration_lock(lock, owner)


def _ensure_sqlite_enabled() -> None:
    state_root = _state_root()
    marker = _migration_paths(state_root)[3]
    if not marker.exists():
        migrate_legacy_queue(state_root)


def _queue_dir() -> Path:
    """Compatibility path: SQLite and results now live directly under run/."""
    path = _state_root() / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _queue() -> MemoryQueue:
    _ensure_sqlite_enabled()
    return MemoryQueue(_state_root())


def enqueue(task_type: str, payload: dict[str, Any]) -> str:
    """Compatibility facade that enqueues handler version 1."""
    return _queue().enqueue(task_type, 1, payload)


def _compat_task(task: QueueTask | QueueLease) -> dict[str, Any]:
    created = task.created_at
    last_attempt = task.last_attempt_at
    attempts = task.attempts if isinstance(task, QueueTask) else task.prior_attempts
    return {
        "id": task.id,
        "type": task.kind,
        "handler_version": task.handler_version,
        "enqueued_at": created.isoformat(timespec="seconds"),
        "attempts": attempts,
        "last_attempt_at": (
            last_attempt.isoformat(timespec="seconds") if last_attempt else None
        ),
        "payload": task.payload,
    }


def _export_task_record(task: QueueTask) -> dict[str, object]:
    return {
        "attempt_history": [
            {
                "attempt": item.attempt,
                "error_code": item.error_code,
                "finished_at": _timestamp(item.finished_at),
                "outcome": item.outcome,
                "started_at": _timestamp(item.started_at),
            }
            for item in task.attempt_history
        ],
        "attempts": task.attempts,
        "available_at": _timestamp(task.available_at),
        "blocked_capability": task.blocked_capability,
        "created_at": _timestamp(task.created_at),
        "dedupe_key": task.dedupe_key,
        "error_code": task.error_code,
        "handler_version": task.handler_version,
        "id": task.id,
        "input_hash": task.input_hash,
        "kind": task.kind,
        "last_attempt_at": (
            _timestamp(task.last_attempt_at) if task.last_attempt_at else None
        ),
        "payload": task.payload,
        "priority": task.priority,
        "redrive_of": task.redrive_of,
        "result_reference": task.result_reference,
        "result_sha256": task.result_sha256,
        "state": task.state,
        "updated_at": _timestamp(task.updated_at),
    }


def list_pending(max_age_days: int | None = None) -> list[dict[str, Any]]:
    tasks = _queue().list_tasks(
        states=("ready", "leased", "blocked", "dead"), max_age_days=max_age_days
    )
    return [_compat_task(task) for task in tasks]


def mark_attempt(task_id: str, success: bool) -> None:
    """Legacy attempt API retained for callers while storage is SQLite-backed."""
    queue = _queue()
    try:
        task = queue.get(task_id)
    except KeyError:
        return
    if task.state != "ready":
        return
    lease = queue._claim_task(task_id, f"legacy-{os.getpid()}")
    if lease is None:
        return
    if success:
        queue.publish_result(lease, operation_id=task_id, result=b"")
        queue.acknowledge(lease)
    else:
        queue.fail(lease, QueueFailure("processor_failed", retry_after=60))


def recover_stale_leases(max_age_seconds: int = 600) -> int:
    del max_age_seconds
    return _queue().recover_expired_leases()


def cancel(task_id: str) -> bool:
    return _queue().cancel(task_id)


def redrive(task_id: str) -> str:
    return _queue().redrive(task_id)


def purge(*, terminal_before: datetime, export_path: Path) -> PurgeReceipt:
    return _queue().purge(terminal_before=terminal_before, export_path=export_path)


def retained_queue_state() -> bool:
    """Return whether queue records or results block deletion of run/."""
    return _queue().retains_run_directory()


class _LeaseHeartbeat:
    def __init__(self, queue: MemoryQueue, lease: QueueLease) -> None:
        self._queue = queue
        self._lease = lease
        self._stop = threading.Event()
        self.error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"memory-queue-heartbeat-{lease.id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._queue._heartbeat_wait(  # noqa: SLF001 - injected queue seam
            self._stop, DEFAULTS.queue_heartbeat_seconds
        ):
            try:
                self._lease = self._queue.heartbeat(
                    self._lease, lease_seconds=DEFAULTS.queue_lease_seconds
                )
            except Exception as exc:  # noqa: BLE001 - completion must remain fenced
                self.error = exc
                self._stop.set()
                return


def drain_with(
    processor: Callable[[dict], bool | DeferredResult], max_tasks: int = 10
) -> dict[str, int]:
    """Run handlers outside transactions and fence completion with a result marker."""
    counts = {"ok": 0, "failed": 0, "dead": 0, "skipped": 0}
    queue = _queue()
    owner = f"compat-{os.getpid()}-{uuid.uuid4().hex}"
    for _ in range(max_tasks):
        lease = queue.claim(owner)
        if lease is None:
            break
        adopted = queue.adopt_published_result(lease, operation_id=lease.id)
        if adopted == "adopted":
            _count_terminal(counts, queue.acknowledge(lease))
            continue
        if adopted == "corrupt":
            _count_terminal(counts, queue.get(lease.id))
            continue
        heartbeat = _LeaseHeartbeat(queue, lease)
        heartbeat.start()
        try:
            try:
                outcome = processor(_compat_task(lease))
                succeeded = bool(outcome)
            except Exception:  # noqa: BLE001
                print("processor_exception", file=sys.stderr)
                succeeded = False
        finally:
            heartbeat.stop()
        if heartbeat.error is not None:
            counts["failed"] += 1
            continue
        try:
            if succeeded:
                result = outcome.data if isinstance(outcome, DeferredResult) else b""
                queue.publish_result(lease, operation_id=lease.id, result=result)
                _count_terminal(counts, queue.acknowledge(lease))
            else:
                queue.fail(lease, QueueFailure("processor_failed", retry_after=60))
                _count_terminal(counts, queue.get(lease.id))
        except (LeaseFenceError, ResultConflictError):
            counts["failed"] += 1
            task = queue.get(lease.id)
            if task.state == "dead":
                counts["dead"] += 1
    return counts


def run_worker(
    processor: Callable[[dict], bool | DeferredResult],
    *,
    max_tasks: int = DEFAULTS.worker_max_tasks,
    max_seconds: int = DEFAULTS.worker_max_seconds,
    idle_seconds: int = DEFAULTS.worker_idle_seconds,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WorkerSummary:
    """Run a short-lived worker bounded by tasks, wall time, and idle time."""
    if max_tasks < 0 or max_seconds < 0 or idle_seconds < 0:
        raise ValueError("worker limits must be non-negative")
    started = monotonic()
    idle_started: float | None = None
    totals = {"ok": 0, "failed": 0, "dead": 0, "skipped": 0}
    processed = 0
    while processed < max_tasks:
        now = monotonic()
        if now - started >= max_seconds:
            break
        if idle_started is not None and now - idle_started >= idle_seconds:
            break
        counts = drain_with(processor, max_tasks=1)
        handled = counts["ok"] + counts["failed"]
        for key in totals:
            totals[key] += counts[key]
        if handled:
            processed += handled
            idle_started = None
            continue
        if idle_seconds == 0:
            break
        if idle_started is None:
            idle_started = now
        remaining = idle_seconds - (now - idle_started)
        sleep(min(1.0, remaining))
    return WorkerSummary(
        processed,
        totals["ok"],
        totals["failed"],
        totals["dead"],
        totals["skipped"],
    )


def _count_terminal(counts: dict[str, int], task: QueueTask) -> None:
    if task.state == "succeeded":
        counts["ok"] += 1
        return
    counts["failed"] += 1
    if task.state == "dead":
        counts["dead"] += 1


def status() -> dict[str, Any]:
    queue = _queue()
    tasks = queue.list_tasks(states=("ready", "leased", "blocked", "dead"))
    by_type: dict[str, int] = {}
    for task in tasks:
        by_type[task.kind] = by_type.get(task.kind, 0) + 1
    return {
        "pending_total": len(tasks),
        "by_type": by_type,
        "permanently_failed": sum(task.state == "dead" for task in tasks),
        "queue_dir": str(queue.run_dir),
    }


def _operator_status() -> dict[str, object]:
    tasks = _queue().list_tasks()
    states = {state: 0 for state in _STATES}
    capabilities: set[str] = set()
    codes: set[str] = set()
    for task in tasks:
        states[task.state] += 1
        if task.blocked_capability:
            capabilities.add(task.blocked_capability)
        if task.error_code:
            codes.add(task.error_code)
    return {
        "counts": {"total": len(tasks)},
        "states": states,
        "capabilities": sorted(capabilities),
        "codes": sorted(codes),
    }


def _manual_processor(task: dict[str, Any]) -> bool | DeferredResult:
    from llm_client import call_llm

    task_type = task.get("type")
    payload = task.get("payload", {})
    if task_type == "query":
        prompt = payload.get("prompt", "")
        if not prompt:
            return False
        result = call_llm(
            prompt,
            payload.get("system_prompt", ""),
            max_tokens=int(payload.get("max_tokens") or 4000),
        )
        if not result:
            return False
        return DeferredResult(result.encode("utf-8"))
    if task_type == "flush":
        prompt = payload.get("prompt", "")
        if not prompt:
            return False
        now = _utc_now()
        raw_day = payload.get("day")
        day = now.strftime("%Y-%m-%d") if raw_day is None else raw_day
        if not isinstance(day, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
            return False
        try:
            if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                return False
        except ValueError:
            return False
        root = Path(os.environ.get("LLM_WIKI_ROOT", ".")).resolve()
        daily_dir = (root / "knowledge" / "daily").resolve()
        daily_path = (daily_dir / f"{day}.md").resolve()
        try:
            daily_path.relative_to(daily_dir)
        except ValueError:
            return False
        result = call_llm(
            prompt,
            payload.get("system_prompt", ""),
            max_tokens=int(payload.get("max_tokens") or 1500),
        )
        if not result:
            return False
        from daily_log_append import locked_append_once
        from flush_memory import _classify_response

        tier, body = _classify_response(result)
        if tier != "ok" and body:
            session_id = payload.get("session_id", "deferred")
            event = payload.get("event", "session-end")
            block = (
                f"\n## [{now.strftime('%H:%M:%S')}] deferred-{event} | {session_id}\n"
                f"- Tier: `{tier}`\n\n{redact_secrets(body)}\n"
            )
            locked_append_once(daily_path, block, str(task["id"]))
        return True
    if task_type == "compile":
        from maybe_compile import spawn_compile_if_idle

        spawned, reason = spawn_compile_if_idle(force=bool(payload.get("force")))
        return spawned or "no pending" in reason
    return False


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["list", "status", "work", "cancel", "redrive", "migrate", "purge", "drain"],
    )
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--max-tasks", type=int, default=DEFAULTS.worker_max_tasks)
    parser.add_argument("--max-seconds", type=int, default=DEFAULTS.worker_max_seconds)
    parser.add_argument("--idle-seconds", type=int, default=DEFAULTS.worker_idle_seconds)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULTS.queue_lease_seconds)
    parser.add_argument(
        "--heartbeat-seconds", type=int, default=DEFAULTS.queue_heartbeat_seconds
    )
    parser.add_argument("--max-attempts", type=int, default=DEFAULTS.queue_max_attempts)
    parser.add_argument(
        "--retry-base-seconds", type=int, default=DEFAULTS.retry_base_seconds
    )
    parser.add_argument(
        "--retry-cap-seconds", type=int, default=DEFAULTS.retry_cap_seconds
    )
    parser.add_argument("--terminal-before")
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "list":
            records = [
                {"id": task.id, "state": task.state} for task in _queue().list_tasks()
            ]
            print(json.dumps(records, sort_keys=True))
            return 0
        if args.command == "status":
            print(json.dumps(_operator_status(), sort_keys=True))
            return 0
        if args.command == "drain":
            counts = drain_with(_manual_processor, max_tasks=args.max_tasks)
            print(json.dumps({"counts": counts}, sort_keys=True))
            return 1 if counts["failed"] or counts["dead"] else 0
        if args.command == "work":
            summary = run_worker(
                _manual_processor,
                max_tasks=args.max_tasks,
                max_seconds=args.max_seconds,
                idle_seconds=args.idle_seconds,
            )
            counts = {
                "dead": summary.dead,
                "failed": summary.failed,
                "processed": summary.processed,
                "skipped": summary.skipped,
                "succeeded": summary.succeeded,
            }
            print(json.dumps({"counts": counts}, sort_keys=True))
            return 1 if summary.failed or summary.dead else 0
        if args.command == "migrate":
            receipt = migrate_legacy_queue(_state_root())
            print(
                json.dumps(
                    {
                        "codes": list(receipt.codes),
                        "counts": {
                            "imported": receipt.imported,
                            "quarantined": receipt.quarantined,
                        },
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command in ("cancel", "redrive") and not args.task_id:
            raise QueueOperationError("task_id_required")
        if args.command == "cancel":
            changed = cancel(args.task_id)
            state = _queue().get(args.task_id).state if changed else "unchanged"
            print(json.dumps({"id": args.task_id, "state": state}, sort_keys=True))
            return 0 if changed else 1
        if args.command == "redrive":
            task_id = redrive(args.task_id)
            print(json.dumps({"id": task_id, "state": "ready"}, sort_keys=True))
            return 0
        if args.command == "purge":
            if args.terminal_before is None:
                raise QueueOperationError("terminal_before_required")
            if args.export is None:
                raise QueueOperationError("export_required")
            try:
                terminal_before = datetime.fromisoformat(args.terminal_before)
            except ValueError:
                raise QueueOperationError("terminal_before_invalid") from None
            receipt = purge(
                terminal_before=terminal_before,
                export_path=args.export,
            )
            print(
                json.dumps(
                    {"counts": {"purged": receipt.purged}, "ids": list(receipt.task_ids)},
                    sort_keys=True,
                )
            )
            return 0
    except (QueueOperationError, KeyError) as exc:
        code = exc.code if isinstance(exc, QueueOperationError) else "task_not_found"
        print(json.dumps({"codes": [code]}, sort_keys=True))
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
