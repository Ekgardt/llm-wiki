"""Fenced SQLite queue for deferred memory-pipeline work."""

from __future__ import annotations

import argparse
import email.utils
import json
import math
import multiprocessing
import os
import random
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
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
_MAX_EXPORT_METADATA_BYTES = 64 * 1024 * 1024
_MAX_LEGACY_RECORD_BYTES = 16 * 1024 * 1024
_MAX_RUNTIME_ATTEMPTS = 100
_MAX_MARKER_BYTES = 64
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


def _require_active(
    deadline: float, cancelled: Callable[[], bool] | None = None
) -> None:
    if time.monotonic() >= deadline or bool(cancelled and cancelled()):
        raise TimeoutError("queue mutation deadline or cancellation reached")


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


class _InvalidArguments(Exception):
    pass


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _InvalidArguments

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            del message
            raise _InvalidArguments
        super().exit(status, message)


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
class QueueOwnerLease:
    state_root: Path
    role: str
    token: str
    pid: int
    epoch: int
    expires_at: datetime
    ttl_seconds: int


@dataclass(frozen=True)
class SourceFence:
    daily_id: str
    source_digest: str
    token: str
    owner_pid: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


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


def _validate_retry_policy(
    max_attempts: int, retry_base_seconds: int, retry_cap_seconds: int
) -> None:
    for name, value in (
        ("max_attempts", max_attempts),
        ("retry_base_seconds", retry_base_seconds),
        ("retry_cap_seconds", retry_cap_seconds),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
    if not 1 <= max_attempts <= _MAX_RUNTIME_ATTEMPTS:
        raise ValueError(
            f"max_attempts must be between 1 and {_MAX_RUNTIME_ATTEMPTS}"
        )
    if retry_base_seconds <= 0:
        raise ValueError("retry_base_seconds must be positive")
    if retry_cap_seconds <= 0:
        raise ValueError("retry_cap_seconds must be positive")
    if retry_base_seconds > retry_cap_seconds:
        raise ValueError("retry_base_seconds must not exceed retry_cap_seconds")


def _validate_worker_policy(
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    _validate_retry_policy(max_attempts, retry_base_seconds, retry_cap_seconds)
    for name, value in (
        ("lease_seconds", lease_seconds),
        ("heartbeat_seconds", heartbeat_seconds),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if heartbeat_seconds >= lease_seconds:
        raise ValueError("heartbeat_seconds must be less than lease_seconds")


class MemoryQueue:
    """Durable priority queue with lease-token fencing and at-least-once delivery."""

    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | random.SystemRandom | None = None,
        heartbeat_wait: Callable[[threading.Event, float], bool] | None = None,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        retry_base_seconds: int = DEFAULTS.retry_base_seconds,
        retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
    ) -> None:
        _validate_retry_policy(max_attempts, retry_base_seconds, retry_cap_seconds)
        self.state_root = Path(state_root).resolve()
        self.run_dir = self.state_root / "run"
        self.db_path = self.run_dir / "queue.sqlite3"
        self.results_dir = self.run_dir / "queue-results"
        self._clock = clock or _utc_now
        self._rng = rng or random.SystemRandom()
        self._heartbeat_wait = heartbeat_wait or (
            lambda stop, interval: stop.wait(interval)
        )
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._db_hardened = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.run_dir, 0o700)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.results_dir, 0o700)
        with self._connect() as connection:
            self._create_schema(connection)
            with begin_immediate(connection):
                self._retire_exhausted_ready(
                    connection, _as_utc(self._clock()), self._max_attempts
                )

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
        MemoryQueue._upgrade_legacy_attempt_constraint(connection)
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
            CREATE TABLE IF NOT EXISTS source_fences (
                daily_id TEXT PRIMARY KEY,
                source_digest TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL UNIQUE,
                owner_pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_failures (
                logical_path TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                error_code TEXT NOT NULL,
                producer TEXT NOT NULL CHECK (producer IN ('compile','queue')),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (logical_path, source_digest)
            );
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
        source_fence_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(source_fences)").fetchall()
        }
        if "heartbeat_at" not in source_fence_columns:
            connection.execute("ALTER TABLE source_fences ADD COLUMN heartbeat_at TEXT")
            connection.execute(
                "UPDATE source_fences SET heartbeat_at=acquired_at WHERE heartbeat_at IS NULL"
            )
        if "expires_at" not in source_fence_columns:
            connection.execute("ALTER TABLE source_fences ADD COLUMN expires_at TEXT")
            connection.execute(
                "UPDATE source_fences SET expires_at=acquired_at WHERE expires_at IS NULL"
            )

    @staticmethod
    def _upgrade_legacy_attempt_constraint(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if row is None or "attempts BETWEEN 0 AND 8" not in str(row["sql"]):
            return
        old_sql = str(row["sql"])
        new_sql = re.sub(
            r"^CREATE TABLE\s+tasks\b",
            "CREATE TABLE tasks_unbounded",
            old_sql,
            count=1,
            flags=re.IGNORECASE,
        ).replace("attempts BETWEEN 0 AND 8", "attempts >= 0")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with begin_immediate(connection):
                connection.execute(new_sql)
                connection.execute("INSERT INTO tasks_unbounded SELECT * FROM tasks")
                connection.execute("DROP TABLE tasks")
                connection.execute("ALTER TABLE tasks_unbounded RENAME TO tasks")
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    def _retire_exhausted_ready(
        self,
        connection: sqlite3.Connection, now: datetime, max_attempts: int
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='ready' AND attempts >= ?",
            (max_attempts,),
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
                    max_attempts,
                ),
            )
            self._record_payload_failure(
                connection,
                str(row["payload_json"]),
                "attempts_exhausted",
                "queue",
                now,
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
                self._delete_stale_source_fences(connection)
                self._assert_payload_not_fenced(connection, payload_json)
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
        max_attempts: int | None = None,
    ) -> QueueLease | None:
        if not owner:
            raise ValueError("owner must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        attempt_limit = self._max_attempts if max_attempts is None else max_attempts
        _validate_retry_policy(
            attempt_limit, self._retry_base_seconds, self._retry_cap_seconds
        )
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            self._expire_leases(connection, now, attempt_limit)
            self._delete_stale_source_fences(connection)
            row = connection.execute(
                """SELECT * FROM tasks
                   WHERE state = 'ready' AND attempts < ? AND available_at <= ?
                     AND NOT EXISTS (
                         SELECT 1 FROM source_fences f
                         WHERE instr(tasks.payload_json, f.daily_id) > 0
                            OR instr(tasks.payload_json, f.source_digest) > 0
                     )
                   ORDER BY priority DESC, available_at, created_at, rowid LIMIT 1""",
                (attempt_limit, _timestamp(now)),
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
                    attempt_limit,
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
            self._delete_stale_source_fences(connection)
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=? AND state='ready'", (task_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                self._assert_payload_not_fenced(connection, str(row["payload_json"]))
            except QueueOperationError:
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

    def _expire_leases(
        self, connection: sqlite3.Connection, now: datetime, max_attempts: int
    ) -> int:
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
            exhausted = int(row["attempts"]) >= max_attempts
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

    def _read_result_for_export(self, reference: str, digest: str) -> bytes:
        try:
            path = self.state_root / reference
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.results_dir.resolve())
            if path.parent.resolve() != self.results_dir.resolve():
                raise QueueOperationError("result_verification_failed")
            data = _read_stable_owner_file(path, _MAX_RESULT_BYTES)
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

    def fail(
        self,
        lease: QueueLease,
        failure: QueueFailure,
        *,
        max_attempts: int | None = None,
        retry_base_seconds: int | None = None,
        retry_cap_seconds: int | None = None,
    ) -> None:
        if not failure.error_code:
            raise ValueError("error_code must be non-empty")
        attempt_limit = self._max_attempts if max_attempts is None else max_attempts
        retry_base = (
            self._retry_base_seconds
            if retry_base_seconds is None
            else retry_base_seconds
        )
        retry_cap = (
            self._retry_cap_seconds if retry_cap_seconds is None else retry_cap_seconds
        )
        _validate_retry_policy(attempt_limit, retry_base, retry_cap)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if failure.blocked_capability:
                self._record_attempt(connection, row, now, "blocked", failure.error_code)
                self._record_payload_failure(
                    connection,
                    str(row["payload_json"]),
                    failure.error_code,
                    "queue",
                    now,
                )
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
                or int(row["attempts"]) >= attempt_limit
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
            delay = self._retry_delay(
                int(row["attempts"]),
                retry_base_seconds=retry_base,
                retry_cap_seconds=retry_cap,
            )
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

    def _retry_delay(
        self,
        attempts: int,
        *,
        retry_base_seconds: int | None = None,
        retry_cap_seconds: int | None = None,
    ) -> float:
        retry_base = (
            self._retry_base_seconds
            if retry_base_seconds is None
            else retry_base_seconds
        )
        retry_cap = (
            self._retry_cap_seconds if retry_cap_seconds is None else retry_cap_seconds
        )
        jitter_cap = min(
            retry_cap,
            retry_base * (2 ** (attempts - 1)),
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

    def _finish_lease(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        now: datetime,
        state: str,
        error_code: str | None,
        available_at: datetime | None,
        *,
        last_attempt_at: datetime | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is not None:
            payload_json = str(row["payload_json"])
            if error_code is not None and state in {"ready", "dead"}:
                self._record_payload_failure(
                    connection, payload_json, error_code, "queue", now
                )
            elif state in {"succeeded", "cancelled"}:
                self._clear_payload_failure(connection, payload_json)
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

    def cancel(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        _require_active(deadline, cancelled)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in _TERMINAL_STATES:
                return False
            if row["state"] == "leased":
                self._record_attempt(connection, row, now, "cancelled", "cancelled")
            self._clear_payload_failure(connection, str(row["payload_json"]))
            _require_active(deadline, cancelled)
            connection.execute(
                """UPDATE tasks SET state='cancelled', updated_at=?, lease_owner=NULL,
                       lease_token=NULL, lease_expires_at=NULL, lease_heartbeat_at=NULL,
                       attempt_started_at=NULL, error_code='cancelled' WHERE id=?""",
                (_timestamp(now), task_id),
            )
            return True

    def redrive(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        _require_active(deadline, cancelled)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] != "dead":
                raise QueueOperationError("redrive_requires_dead")
            self._delete_stale_source_fences(connection)
            self._assert_payload_not_fenced(connection, str(row["payload_json"]))
            payload_bytes = str(row["payload_json"]).encode("utf-8")
            replacement = uuid.UUID(int=self._rng.getrandbits(128)).hex
            _require_active(deadline, cancelled)
            connection.execute(
                """INSERT INTO tasks(
                       id, kind, handler_version, payload_json, input_hash, state,
                       priority, created_at, updated_at, available_at, redrive_of
                   ) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
                (
                    replacement,
                    row["kind"],
                    row["handler_version"],
                    payload_bytes.decode("utf-8"),
                    sha256_bytes(payload_bytes),
                    row["priority"],
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    task_id,
                ),
            )
        return replacement

    @staticmethod
    def _validate_source_identity(daily_id: str, source_digest: str) -> None:
        if not isinstance(daily_id, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", daily_id) is None:
            raise ValueError("daily_id must be a canonical date")
        try:
            if date.fromisoformat(daily_id).isoformat() != daily_id:
                raise ValueError
        except ValueError as exc:
            raise ValueError("daily_id must be a canonical date") from exc
        if not isinstance(source_digest, str) or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
            raise ValueError("source_digest must be a lowercase SHA-256")

    @staticmethod
    def _validate_failure_identity(logical_path: str, source_digest: str) -> None:
        if (
            not isinstance(logical_path, str)
            or re.fullmatch(r"knowledge/daily/\d{4}-\d{2}-\d{2}\.md", logical_path)
            is None
        ):
            raise ValueError("logical_path must name a canonical daily source")
        MemoryQueue._validate_source_identity(
            Path(logical_path).stem, source_digest
        )

    @staticmethod
    def _source_identity_from_payload(payload_json: str) -> tuple[str, str] | None:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return None
        paths: list[str] = []
        digests: list[str] = []
        daily_ids: list[str] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = str(key).casefold()
                    if isinstance(item, str):
                        if normalized in {"source_path", "logical_path"}:
                            paths.append(item)
                        elif normalized in {"source_digest", "digest", "hash"}:
                            digests.append(item)
                        elif normalized == "daily_id":
                            daily_ids.append(item)
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(payload)
        if not paths and daily_ids:
            paths.append(f"knowledge/daily/{daily_ids[0]}.md")
        if not paths or not digests:
            return None
        try:
            MemoryQueue._validate_failure_identity(paths[0], digests[0])
        except ValueError:
            return None
        return paths[0], digests[0]

    @staticmethod
    def _record_source_failure_row(
        connection: sqlite3.Connection,
        logical_path: str,
        source_digest: str,
        error_code: str,
        producer: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO source_failures(
                   logical_path, source_digest, error_code, producer, updated_at
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(logical_path, source_digest) DO UPDATE SET
                   error_code=excluded.error_code,
                   producer=excluded.producer,
                   updated_at=excluded.updated_at""",
            (logical_path, source_digest, error_code, producer, _timestamp(now)),
        )

    def _record_payload_failure(
        self,
        connection: sqlite3.Connection,
        payload_json: str,
        error_code: str,
        producer: str,
        now: datetime,
    ) -> None:
        identity = self._source_identity_from_payload(payload_json)
        if identity is not None:
            self._record_source_failure_row(
                connection, *identity, error_code, producer, now
            )

    def _clear_payload_failure(
        self, connection: sqlite3.Connection, payload_json: str
    ) -> None:
        identity = self._source_identity_from_payload(payload_json)
        if identity is not None:
            connection.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                identity,
            )

    def record_source_failure(
        self,
        logical_path: str,
        source_digest: str,
        *,
        error_code: str,
        producer: str,
    ) -> None:
        self._validate_failure_identity(logical_path, source_digest)
        if (
            not isinstance(error_code, str)
            or not 1 <= len(error_code) <= 200
            or any(char in error_code for char in "\r\n")
            or producer not in {"compile", "queue"}
        ):
            raise ValueError("source failure fields are invalid")
        with self._connect() as connection, begin_immediate(connection):
            self._record_source_failure_row(
                connection,
                logical_path,
                source_digest,
                error_code,
                producer,
                _as_utc(self._clock()),
            )

    def clear_source_failure(self, logical_path: str, source_digest: str) -> None:
        self._validate_failure_identity(logical_path, source_digest)
        with self._connect() as connection, begin_immediate(connection):
            connection.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            )

    def source_failure(
        self, logical_path: str, source_digest: str
    ) -> dict[str, str] | None:
        self._validate_failure_identity(logical_path, source_digest)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT logical_path, source_digest, error_code, producer "
                "FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            ).fetchone()
        return None if row is None else {key: str(row[key]) for key in row.keys()}

    @staticmethod
    def _payload_references_source(
        payload_json: str, daily_id: str, source_digest: str
    ) -> bool:
        return daily_id in payload_json or source_digest in payload_json

    def _delete_stale_source_fences(self, connection: sqlite3.Connection) -> None:
        now = _as_utc(self._clock())
        rows = connection.execute(
            "SELECT token, owner_pid, expires_at FROM source_fences"
        ).fetchall()
        for row in rows:
            expires_at = _parse_timestamp(str(row["expires_at"]))
            expired = expires_at is None or expires_at <= now
            if expired or not _pid_is_alive(int(row["owner_pid"])):
                connection.execute(
                    "DELETE FROM source_fences WHERE token=?", (row["token"],)
                )

    def _assert_payload_not_fenced(
        self, connection: sqlite3.Connection, payload_json: str
    ) -> None:
        for row in connection.execute(
            "SELECT daily_id, source_digest FROM source_fences"
        ):
            if self._payload_references_source(
                payload_json, str(row["daily_id"]), str(row["source_digest"])
            ):
                raise QueueOperationError("source_fenced")

    def referencing_source_tasks(
        self,
        daily_id: str,
        source_digest: str,
        *,
        states: tuple[str, ...] = ("ready", "leased", "blocked", "dead"),
    ) -> tuple[str, ...]:
        self._validate_source_identity(daily_id, source_digest)
        if not states or any(state not in _STATES for state in states):
            raise ValueError("invalid queue state")
        placeholders = ",".join("?" for _ in states)
        with self._connect() as connection:
            cursor = connection.execute(
                f"SELECT id, payload_json FROM tasks WHERE state IN ({placeholders}) "
                "ORDER BY created_at, rowid",  # noqa: S608 - generated placeholders
                states,
            )
            matches = []
            for row in cursor:
                if self._payload_references_source(
                    str(row["payload_json"]), daily_id, source_digest
                ):
                    matches.append(str(row["id"]))
            return tuple(matches)

    def acquire_source_fence(
        self,
        daily_id: str,
        source_digest: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        self._validate_source_identity(daily_id, source_digest)
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("source fence lease must be a positive integer")
        token = f"{self._rng.getrandbits(256):064x}"
        acquired = _as_utc(self._clock())
        expires = acquired + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            for row in connection.execute(
                "SELECT id, payload_json FROM tasks "
                "WHERE state IN ('ready','leased','blocked','dead')"
            ):
                if self._payload_references_source(
                    str(row["payload_json"]), daily_id, source_digest
                ):
                    raise QueueOperationError("source_referenced")
            try:
                connection.execute(
                    "INSERT INTO source_fences "
                    "(daily_id, source_digest, token, owner_pid, acquired_at, "
                    "heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        daily_id,
                        source_digest,
                        token,
                        os.getpid(),
                        _timestamp(acquired),
                        _timestamp(acquired),
                        _timestamp(expires),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise QueueOperationError("source_fenced") from exc
        return SourceFence(
            daily_id,
            source_digest,
            token,
            os.getpid(),
            acquired,
            acquired,
            expires,
        )

    def heartbeat_source_fence(
        self,
        fence: SourceFence,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        if not isinstance(fence, SourceFence):
            raise TypeError("source heartbeat requires a SourceFence")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("source fence lease must be a positive integer")
        now = _as_utc(self._clock())
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            changed = connection.execute(
                """UPDATE source_fences SET heartbeat_at=?, expires_at=?
                   WHERE daily_id=? AND source_digest=? AND token=? AND owner_pid=?
                     AND acquired_at=? AND heartbeat_at=? AND expires_at=?""",
                (
                    _timestamp(now),
                    _timestamp(expires),
                    fence.daily_id,
                    fence.source_digest,
                    fence.token,
                    fence.owner_pid,
                    _timestamp(fence.acquired_at),
                    _timestamp(fence.heartbeat_at),
                    _timestamp(fence.expires_at),
                ),
            ).rowcount
            if changed != 1 or now >= fence.expires_at:
                raise QueueOperationError("source_fence_lost")
        return replace(fence, heartbeat_at=now, expires_at=expires)

    @contextmanager
    def source_fence_heartbeat(
        self,
        fence: SourceFence,
        *,
        heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ):
        if (
            not isinstance(heartbeat_seconds, int)
            or isinstance(heartbeat_seconds, bool)
            or heartbeat_seconds <= 0
            or not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= heartbeat_seconds
        ):
            raise ValueError("source heartbeat interval must be shorter than its lease")
        heartbeat = _SourceFenceHeartbeat(
            self,
            fence,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
        )
        heartbeat.start()
        try:
            yield heartbeat
        finally:
            heartbeat.stop()

    @contextmanager
    def source_finalization(self, fence: SourceFence):
        if not isinstance(fence, SourceFence):
            raise TypeError("source finalization requires a SourceFence")
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            row = connection.execute(
                "SELECT daily_id, source_digest, token, owner_pid, acquired_at, expires_at "
                "FROM source_fences WHERE token=?",
                (fence.token,),
            ).fetchone()
            now = _as_utc(self._clock())
            row_expires_at = (
                None if row is None else _parse_timestamp(str(row["expires_at"]))
            )
            if (
                row is None
                or str(row["daily_id"]) != fence.daily_id
                or str(row["source_digest"]) != fence.source_digest
                or str(row["token"]) != fence.token
                or int(row["owner_pid"]) != os.getpid()
                or fence.owner_pid != os.getpid()
                or str(row["acquired_at"]) != _timestamp(fence.acquired_at)
                or row_expires_at is None
                or now >= row_expires_at
            ):
                raise QueueOperationError("source_fence_lost")
            logical_path = f"knowledge/daily/{fence.daily_id}.md"
            if connection.execute(
                "SELECT 1 FROM source_failures "
                "WHERE logical_path=? AND source_digest=?",
                (logical_path, fence.source_digest),
            ).fetchone() is not None:
                raise QueueOperationError("source_failure")
            for task in connection.execute(
                "SELECT payload_json FROM tasks "
                "WHERE state IN ('ready','leased','blocked','dead')"
            ):
                if self._payload_references_source(
                    str(task["payload_json"]), fence.daily_id, fence.source_digest
                ):
                    raise QueueOperationError("source_referenced")
            yield

    def release_source_fence(self, token: str) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("source fence token is invalid")
        with self._connect() as connection, begin_immediate(connection):
            changed = connection.execute(
                "DELETE FROM source_fences WHERE token=? AND owner_pid=?",
                (token, os.getpid()),
            ).rowcount
            if changed != 1:
                raise QueueOperationError("source_fence_lost")

    def retains_run_directory(self) -> bool:
        with self._connect() as connection:
            task = connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
            source_fence = connection.execute(
                "SELECT 1 FROM source_fences LIMIT 1"
            ).fetchone()
            source_failure = connection.execute(
                "SELECT 1 FROM source_failures LIMIT 1"
            ).fetchone()
        if task is not None or source_fence is not None or source_failure is not None:
            return True
        quarantine = self.run_dir / "queue-quarantine"
        try:
            if quarantine.exists() and any(quarantine.iterdir()):
                return True
        except OSError:
            return True
        try:
            return any(self.results_dir.iterdir())
        except OSError:
            return True

    def purge(
        self,
        *,
        terminal_before: datetime,
        export_path: Path,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> PurgeReceipt:
        _require_active(deadline, cancelled)
        requested_cutoff = _as_utc(terminal_before)
        retention_cutoff = _as_utc(self._clock()) - timedelta(
            days=DEFAULTS.queue_result_retention_days
        )
        cutoff = min(requested_cutoff, retention_cutoff)
        export = Path(export_path).absolute()
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
        parent = export.parent
        if not parent.exists():
            parent.mkdir(parents=True)
            _harden_owner_only(parent, 0o700)
        if (
            parent.is_symlink()
            or not parent.is_dir()
            or not _is_owner_only(parent)
        ):
            raise QueueOperationError("export_parent_permissions_invalid")
        _cleanup_export_staging(parent, export.name)
        staging = parent / f".{export.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        _harden_owner_only(staging, 0o700)
        try:
            results_export = staging / "results"
            results_export.mkdir()
            _harden_owner_only(results_export, 0o700)
            result_manifest: list[dict[str, str]] = []
            for row in rows:
                _require_active(deadline, cancelled)
                reference = row["result_reference"]
                digest = row["result_sha256"]
                if reference is None:
                    continue
                if not isinstance(digest, str):
                    raise QueueOperationError("result_verification_failed")
                result = self._read_result_for_export(str(reference), digest)
                target = results_export / f"{row['id']}.result"
                _write_durable_file(target, result)
                result_manifest.append({"id": str(row["id"]), "sha256": digest})
            records_path = staging / "records.json"
            _write_durable_file(records_path, records_bytes)
            manifest = {
                "records_sha256": sha256_bytes(records_bytes),
                "results": result_manifest,
                "task_ids": list(task_ids),
            }
            manifest_path = staging / "manifest.json"
            manifest_bytes = canonical_json_bytes(manifest)
            _write_durable_file(manifest_path, manifest_bytes)
            fsync_directory(results_export)
            fsync_directory(staging)
            if (
                _read_stable_owner_file(records_path, _MAX_EXPORT_METADATA_BYTES)
                != records_bytes
            ):
                raise QueueOperationError("export_verification_failed")
            for item in result_manifest:
                exported = results_export / f"{item['id']}.result"
                data = _read_stable_owner_file(exported, _MAX_RESULT_BYTES)
                if sha256_bytes(data) != item["sha256"]:
                    raise QueueOperationError("export_verification_failed")
            if (
                _read_stable_owner_file(manifest_path, _MAX_EXPORT_METADATA_BYTES)
                != manifest_bytes
            ):
                raise QueueOperationError("export_verification_failed")
            _require_active(deadline, cancelled)
            staging.replace(export)
            fsync_directory(parent)
        except Exception:
            _remove_export_staging(staging)
            raise
        if task_ids:
            try:
                _require_active(deadline, cancelled)
                placeholders = ",".join("?" for _ in task_ids)
                with self._connect() as connection, begin_immediate(
                    connection,
                    before_commit=lambda: _require_active(deadline, cancelled),
                ):
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
                    _require_active(deadline, cancelled)
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
            except BaseException:
                _remove_export_staging(export)
                raise
            for row in rows:
                _require_active(deadline, cancelled)
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
            return self._expire_leases(connection, now, self._max_attempts)

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
        run_dir / "queue.sqlite3",
        run_dir / "queue-migrated-v2",
    )


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _legacy_write_allowed(state_root: Path) -> bool:
    _run_dir, legacy_dir, _db_path, marker = _migration_paths(state_root)
    if _path_present(marker):
        _validate_migration_marker(marker)
        if legacy_dir.exists():
            _post_marker_legacy_conflict(state_root)
        raise LegacyBackendDisabled("legacy_backend_disabled")
    if _queue_owner_is_active(state_root, "migration"):
        raise LegacyBackendDisabled("legacy_migration_quiesced")
    return True


def _open_queue_ownership_db(state_root: Path) -> sqlite3.Connection:
    run_dir, _legacy_dir, db_path, _marker = _migration_paths(state_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    _harden_owner_only(run_dir, 0o700)
    connection = open_operational_db(db_path, busy_ms=DEFAULTS.queue_busy_ms)
    try:
        _harden_owner_only(db_path, 0o600)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS queue_ownership (
                   role TEXT PRIMARY KEY,
                   token TEXT,
                   pid INTEGER,
                   heartbeat_at TEXT,
                   expires_at TEXT,
                   epoch INTEGER NOT NULL CHECK (epoch >= 0)
               )"""
        )
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection


def _acquire_queue_owner(
    state_root: Path,
    role: str,
    busy_code: str,
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULTS.queue_lease_seconds,
) -> QueueOwnerLease:
    if not role or ttl_seconds <= 0:
        raise ValueError("owner role and ttl must be valid")
    acquired_at = _as_utc(now or _utc_now())
    token = uuid.uuid4().hex
    pid = os.getpid()
    expires_at = acquired_at + timedelta(seconds=ttl_seconds)
    with _open_queue_ownership_db(state_root) as connection, begin_immediate(connection):
        row = connection.execute(
            "SELECT * FROM queue_ownership WHERE role=?", (role,)
        ).fetchone()
        epoch = 1
        if row is not None:
            epoch = int(row["epoch"]) + 1
            existing_token = row["token"]
            if existing_token is not None:
                existing_pid = row["pid"]
                dead = not isinstance(existing_pid, int) or not _pid_is_alive(existing_pid)
                if not dead:
                    raise MigrationBusy(busy_code)
            connection.execute(
                """UPDATE queue_ownership
                   SET token=?, pid=?, heartbeat_at=?, expires_at=?, epoch=?
                   WHERE role=?""",
                (
                    token,
                    pid,
                    _timestamp(acquired_at),
                    _timestamp(expires_at),
                    epoch,
                    role,
                ),
            )
        else:
            connection.execute(
                """INSERT INTO queue_ownership(
                       role, token, pid, heartbeat_at, expires_at, epoch
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    role,
                    token,
                    pid,
                    _timestamp(acquired_at),
                    _timestamp(expires_at),
                    epoch,
                ),
            )
    return QueueOwnerLease(
        Path(state_root).resolve(), role, token, pid, epoch, expires_at, ttl_seconds
    )


def _require_queue_owner(
    connection: sqlite3.Connection,
    lease: QueueOwnerLease,
    now: datetime,
    *,
    heartbeat: bool,
) -> datetime:
    row = connection.execute(
        "SELECT * FROM queue_ownership WHERE role=?", (lease.role,)
    ).fetchone()
    expiry = _parse_timestamp(row["expires_at"]) if row is not None else None
    if (
        row is None
        or row["token"] != lease.token
        or int(row["epoch"]) != lease.epoch
        or expiry is None
        or row["pid"] != lease.pid
        or not _pid_is_alive(lease.pid)
    ):
        code = "migration_fence_lost" if lease.role == "migration" else "legacy_owner_fence_lost"
        raise QueueOperationError(code)
    if heartbeat:
        expiry = now + timedelta(seconds=lease.ttl_seconds)
        connection.execute(
            """UPDATE queue_ownership SET heartbeat_at=?, expires_at=?
               WHERE role=? AND token=? AND epoch=?""",
            (
                _timestamp(now),
                _timestamp(expiry),
                lease.role,
                lease.token,
                lease.epoch,
            ),
        )
    return expiry


def _heartbeat_queue_owner(
    lease: QueueOwnerLease, *, now: datetime | None = None
) -> QueueOwnerLease:
    heartbeat_at = _as_utc(now or _utc_now())
    with _open_queue_ownership_db(lease.state_root) as connection, begin_immediate(connection):
        expires_at = _require_queue_owner(
            connection, lease, heartbeat_at, heartbeat=True
        )
    return replace(lease, expires_at=expires_at)


def _release_queue_owner(lease: QueueOwnerLease) -> bool:
    with _open_queue_ownership_db(lease.state_root) as connection, begin_immediate(connection):
        changed = connection.execute(
            """UPDATE queue_ownership
               SET token=NULL, pid=NULL, heartbeat_at=NULL, expires_at=NULL
               WHERE role=? AND token=? AND epoch=?""",
            (lease.role, lease.token, lease.epoch),
        ).rowcount
    return changed == 1


def _queue_owner_is_active(state_root: Path, role: str) -> bool:
    with _open_queue_ownership_db(state_root) as connection:
        row = connection.execute(
            "SELECT token, pid, expires_at FROM queue_ownership WHERE role=?", (role,)
        ).fetchone()
    if row is None or row["token"] is None:
        return False
    return (
        isinstance(row["pid"], int)
        and _pid_is_alive(row["pid"])
    )


def _validate_migration_marker(marker: Path) -> None:
    expected = canonical_json_bytes({"version": 2})
    try:
        metadata = marker.lstat()
        if (
            marker.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_MARKER_BYTES
            or not _is_owner_only(marker)
        ):
            raise QueueOperationError("migration_marker_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(marker, flags)
        try:
            opened = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
                raise QueueOperationError("migration_marker_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(_MAX_MARKER_BYTES + 1)
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
                raise QueueOperationError("migration_marker_invalid")
        finally:
            os.close(descriptor)
        if raw != expected:
            raise QueueOperationError("migration_marker_invalid")
    except QueueOperationError:
        raise
    except (OSError, ValueError):
        raise QueueOperationError("migration_marker_invalid") from None


def _read_bounded_regular_nofollow(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise QueueOperationError("legacy_source_unsafe")
    if metadata.st_size > _MAX_LEGACY_RECORD_BYTES:
        raise QueueOperationError("legacy_record_too_large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_LEGACY_RECORD_BYTES:
            raise QueueOperationError("legacy_source_unsafe")
        if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            raise QueueOperationError("legacy_source_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_LEGACY_RECORD_BYTES + 1)
        if len(raw) > _MAX_LEGACY_RECORD_BYTES:
            raise QueueOperationError("legacy_record_too_large")
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
            raise QueueOperationError("legacy_source_changed")
        return raw
    finally:
        os.close(descriptor)


def _prove_no_live_processing(legacy_dir: Path) -> None:
    if not legacy_dir.exists():
        return
    for path in legacy_dir.glob("*.processing"):
        raw = _read_bounded_regular_nofollow(path)
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MigrationBusy("legacy_owner_unverifiable") from None
        pid = record.get("lease_pid") if isinstance(record, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            raise MigrationBusy("legacy_owner_unverifiable")
        if _pid_is_alive(pid):
            raise MigrationBusy("legacy_owner_live")


def _scan_legacy_records(
    legacy_dir: Path,
) -> tuple[list[tuple[Path, dict[str, object]]], list[tuple[Path, bytes]]]:
    valid: list[tuple[Path, dict[str, object]]] = []
    malformed: list[tuple[Path, bytes]] = []
    if not legacy_dir.exists():
        return valid, malformed
    paths = sorted((*legacy_dir.glob("*.json"), *legacy_dir.glob("*.processing")))
    for path in paths:
        try:
            raw = _read_bounded_regular_nofollow(path)
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
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


def _quarantine_legacy_record(
    run_dir: Path, source: Path, raw: bytes, *, code: str = "legacy_invalid"
) -> None:
    quarantine = run_dir / "queue-quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    _harden_owner_only(quarantine, 0o700)
    digest = sha256_bytes(raw)
    raw_name = f"{digest}.raw"
    _write_durable_file(quarantine / raw_name, raw)
    record = {
        "code": code,
        "raw_name": raw_name,
        "source_name": source.name,
        "source_sha256": digest,
    }
    source_digest = sha256_bytes(source.name.encode("utf-8"))[:16]
    _write_durable_file(
        quarantine / f"{digest}-{source_digest}.json", canonical_json_bytes(record)
    )
    fsync_directory(quarantine)


def _quarantine_legacy_directory(run_dir: Path, legacy_dir: Path, *, code: str) -> int:
    quarantined = 0
    for source in sorted(legacy_dir.iterdir()):
        raw = _read_bounded_regular_nofollow(source)
        _quarantine_legacy_record(run_dir, source, raw, code=code)
        source.unlink()
        quarantined += 1
    legacy_dir.rmdir()
    fsync_directory(run_dir)
    return quarantined


def _post_marker_legacy_conflict(state_root: Path) -> None:
    run_dir, legacy_dir, _db_path, marker = _migration_paths(state_root)
    if not _path_present(marker) or not legacy_dir.exists():
        return
    owner = _acquire_queue_owner(state_root, "legacy", "legacy_owner_busy")
    try:
        if legacy_dir.exists():
            _quarantine_legacy_directory(
                run_dir, legacy_dir, code="legacy_backend_conflict"
            )
            raise LegacyBackendDisabled("legacy_backend_conflict")
    finally:
        _release_queue_owner(owner)


def _legacy_enqueue_file(
    task_type: str, payload: dict[str, Any], state_root: Path | None = None
) -> str:
    root = Path(state_root or _state_root()).resolve()
    _legacy_write_allowed(root)
    owner = _acquire_queue_owner(root, "legacy", "legacy_owner_busy")
    target: Path | None = None
    try:
        _legacy_write_allowed(root)
        task_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        record = {
            "attempts": 0,
            "enqueued_at": _timestamp(_utc_now()),
            "id": task_id,
            "last_attempt_at": None,
            "payload": payload,
            "type": task_type,
        }
        queue_dir = root / "run" / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(queue_dir, 0o700)
        target = queue_dir / f"{task_id}.json"
        owner = _heartbeat_queue_owner(owner)
        marker = _migration_paths(root)[3]
        if _path_present(marker):
            _validate_migration_marker(marker)
            raise LegacyBackendDisabled("legacy_marker_race")
        _write_durable_file(target, canonical_json_bytes(record))
        try:
            owner = _heartbeat_queue_owner(owner)
            marker = _migration_paths(root)[3]
            if _path_present(marker):
                _validate_migration_marker(marker)
                raise LegacyBackendDisabled("legacy_marker_race")
        except Exception:
            target.unlink(missing_ok=True)
            fsync_directory(queue_dir)
            raise
        return task_id
    finally:
        _release_queue_owner(owner)


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


def _read_stable_owner_file(path: Path, max_bytes: int) -> bytes:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > max_bytes
        or not _is_owner_only(path)
    ):
        raise QueueOperationError("export_verification_failed")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            raise QueueOperationError("export_verification_failed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
        after = os.fstat(descriptor)
        if len(data) > max_bytes or (
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
            raise QueueOperationError("export_verification_failed")
        return data
    finally:
        os.close(descriptor)


def _remove_export_staging(staging: Path) -> None:
    if not staging.exists():
        return
    if staging.is_symlink() or not staging.is_dir() or not _is_owner_only(staging):
        raise QueueOperationError("export_staging_invalid")
    shutil.rmtree(staging)
    fsync_directory(staging.parent)


def _cleanup_export_staging(parent: Path, export_name: str) -> None:
    for staging in parent.glob(f".{export_name}.staging-*"):
        _remove_export_staging(staging)


def _commit_migration_marker(
    lease: QueueOwnerLease, marker: Path, run_dir: Path
) -> QueueOwnerLease:
    now = _utc_now()
    with _open_queue_ownership_db(lease.state_root) as connection, begin_immediate(connection):
        expires_at = _require_queue_owner(connection, lease, now, heartbeat=True)
        try:
            _write_durable_file(marker, canonical_json_bytes({"version": 2}))
        except QueueOperationError:
            if _path_present(marker):
                _validate_migration_marker(marker)
            raise
        _validate_migration_marker(marker)
        fsync_directory(run_dir)
    return replace(lease, expires_at=expires_at)


def migrate_legacy_queue(
    state_root: Path,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> MigrationReceipt:
    def require_active() -> None:
        if time.monotonic() >= deadline or bool(cancelled and cancelled()):
            raise TimeoutError("queue migration cancelled or deadline reached")

    require_active()
    run_dir, legacy_dir, _db_path, marker = _migration_paths(state_root)
    if _path_present(marker):
        _validate_migration_marker(marker)
        _post_marker_legacy_conflict(state_root)
        return MigrationReceipt(0, 0, (), ())
    owner = _acquire_queue_owner(state_root, "migration", "migration_busy")
    legacy_owner: QueueOwnerLease | None = None
    try:
        require_active()
        if _path_present(marker):
            _validate_migration_marker(marker)
            _post_marker_legacy_conflict(state_root)
            return MigrationReceipt(0, 0, (), ())
        legacy_owner = _acquire_queue_owner(state_root, "legacy", "legacy_owner_busy")
        owner = _heartbeat_queue_owner(owner)
        require_active()
        _prove_no_live_processing(legacy_dir)
        owner = _heartbeat_queue_owner(owner)
        require_active()
        migration_inputs = sorted(run_dir.glob("queue-migration-*"))
        if migration_inputs and legacy_dir.exists():
            raise MigrationBusy("legacy_migration_conflict")
        if len(migration_inputs) > 1:
            raise MigrationBusy("legacy_migration_conflict")
        if migration_inputs:
            migration_input = migration_inputs[0]
        elif legacy_dir.exists():
            migration_input = run_dir / f"queue-migration-{uuid.uuid4().hex}"
            legacy_dir.replace(migration_input)
            fsync_directory(run_dir)
        else:
            migration_input = None
        owner = _heartbeat_queue_owner(owner)
        require_active()
        valid, malformed = (
            _scan_legacy_records(migration_input)
            if migration_input is not None
            else ([], [])
        )
        owner = _heartbeat_queue_owner(owner)
        queue = MemoryQueue(Path(state_root)) if valid else None
        imported: list[str] = []
        for source, record in valid:
            require_active()
            if queue is None:  # pragma: no cover - guarded by valid
                raise AssertionError
            imported.append(_import_legacy_record(queue, record, source))
            source.unlink()
            owner = _heartbeat_queue_owner(owner)
        for source, raw in malformed:
            require_active()
            _quarantine_legacy_record(run_dir, source, raw)
            source.unlink(missing_ok=True)
            owner = _heartbeat_queue_owner(owner)
        if migration_input is not None:
            fsync_directory(migration_input)
            migration_input.rmdir()
            fsync_directory(run_dir)
        owner = _heartbeat_queue_owner(owner)
        if legacy_dir.exists():
            _quarantine_legacy_directory(
                run_dir, legacy_dir, code="legacy_backend_conflict"
            )
            raise LegacyBackendDisabled("legacy_backend_conflict")
        owner = _commit_migration_marker(owner, marker, run_dir)
        codes = ("legacy_invalid",) if malformed else ()
        return MigrationReceipt(len(imported), len(malformed), tuple(imported), codes)
    finally:
        if legacy_owner is not None:
            _release_queue_owner(legacy_owner)
        _release_queue_owner(owner)


def _ensure_sqlite_enabled() -> None:
    state_root = _state_root()
    marker = _migration_paths(state_root)[3]
    if not _path_present(marker):
        migrate_legacy_queue(state_root)
    else:
        _validate_migration_marker(marker)
        _post_marker_legacy_conflict(state_root)


def _queue_dir() -> Path:
    """Compatibility path: SQLite and results now live directly under run/."""
    path = _state_root() / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _queue(
    *,
    max_attempts: int = DEFAULTS.queue_max_attempts,
    retry_base_seconds: int = DEFAULTS.retry_base_seconds,
    retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
) -> MemoryQueue:
    _ensure_sqlite_enabled()
    return MemoryQueue(
        _state_root(),
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )


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


def cancel(
    task_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    return _queue().cancel(task_id, deadline=deadline, cancelled=cancelled)


def redrive(
    task_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> str:
    return _queue().redrive(task_id, deadline=deadline, cancelled=cancelled)


def purge(
    *,
    terminal_before: datetime,
    export_path: Path,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> PurgeReceipt:
    return _queue().purge(
        terminal_before=terminal_before,
        export_path=export_path,
        deadline=deadline,
        cancelled=cancelled,
    )


def retained_queue_state() -> bool:
    """Return whether queue records or results block deletion of run/."""
    return _queue().retains_run_directory()


class _SourceFenceHeartbeat:
    def __init__(
        self,
        queue: MemoryQueue,
        fence: SourceFence,
        *,
        heartbeat_seconds: int,
        lease_seconds: int,
    ) -> None:
        self._queue = queue
        self._fence = fence
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"memory-source-fence-heartbeat-{fence.daily_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def current(self) -> SourceFence:
        with self._lock:
            if self.error is not None:
                raise QueueOperationError("source_fence_lost") from self.error
            return self._fence

    def refresh(self) -> SourceFence:
        with self._lock:
            if self.error is not None:
                raise QueueOperationError("source_fence_lost") from self.error
            try:
                self._fence = self._queue.heartbeat_source_fence(
                    self._fence, lease_seconds=self._lease_seconds
                )
            except Exception as exc:
                self.error = exc
                self._stop.set()
                raise QueueOperationError("source_fence_lost") from exc
            return self._fence

    def _run(self) -> None:
        while not self._queue._heartbeat_wait(  # noqa: SLF001 - injected queue seam
            self._stop, self._heartbeat_seconds
        ):
            try:
                self.refresh()
            except QueueOperationError:
                return


class _LeaseHeartbeat:
    def __init__(
        self,
        queue: MemoryQueue,
        lease: QueueLease,
        *,
        heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> None:
        self._queue = queue
        self._lease = lease
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_seconds = lease_seconds
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
            self._stop, self._heartbeat_seconds
        ):
            try:
                self._lease = self._queue.heartbeat(
                    self._lease, lease_seconds=self._lease_seconds
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


def _run_processor_inline(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
) -> bool | DeferredResult:
    del timeout
    return processor(task)


def _processor_child_entry(
    sender: Any,
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
) -> None:
    try:
        if os.name != "nt":
            os.setsid()
        try:
            outcome = processor(task)
            if isinstance(outcome, DeferredResult):
                frame = b"D" + outcome.data
            elif isinstance(outcome, bool):
                frame = b"T" if outcome else b"F"
            else:
                frame = b"?"
        except Exception:  # noqa: BLE001 - parent receives a stable code only
            frame = b"E"
        sender.send_bytes(b"R")
        if not sender.poll(5) or sender.recv_bytes(1) != b"A":
            return
        sender.send_bytes(frame)
    except Exception:  # noqa: BLE001 - parent receives a stable code only
        pass
    finally:
        sender.close()


def _kill_process_group(pid: int, sig: int) -> None:
    os.killpg(pid, sig)


def _process_snapshot_posix() -> list[tuple[int, int, int, str]] | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    rows: list[tuple[int, int, int, str]] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2 :].split()
            rows.append((int(entry.name), int(fields[1]), int(fields[2]), fields[0]))
        except (OSError, ValueError, IndexError):
            continue
    return rows


def _tracked_descendant_pids(root_pid: int, platform_name: str) -> set[int] | None:
    if platform_name == "nt":
        try:
            result = subprocess.run(
                ["wmic", "process", "get", "ParentProcessId,ProcessId", "/format:csv"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                raise OSError
            pairs = []
            for line in result.stdout.splitlines():
                fields = [field.strip() for field in line.split(",")]
                if len(fields) >= 3 and fields[-1].isdigit() and fields[-2].isdigit():
                    pairs.append((int(fields[-1]), int(fields[-2])))
        except (OSError, ValueError, subprocess.SubprocessError):
            command = (
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"
            )
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", command],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    return None
                decoded = json.loads(result.stdout or "[]")
                items = decoded if isinstance(decoded, list) else [decoded]
                pairs = [
                    (int(item["ProcessId"]), int(item["ParentProcessId"]))
                    for item in items
                    if isinstance(item, dict)
                ]
            except (
                OSError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
                subprocess.SubprocessError,
            ):
                return None
    else:
        snapshot = _process_snapshot_posix()
        if snapshot is None:
            return set()
        pairs = [(pid, ppid) for pid, ppid, _pgrp, _state in snapshot]
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {pid for pid, ppid in pairs if ppid in frontier and pid not in descendants}
        descendants.update(children)
        frontier = children
    return descendants


def _process_group_alive(group_id: int) -> bool:
    snapshot = _process_snapshot_posix()
    if snapshot is not None:
        return any(pgrp == group_id and state != "Z" for _pid, _ppid, pgrp, state in snapshot)
    try:
        _kill_process_group(group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cleanup_confirmed(
    process: multiprocessing.Process,
    descendants: set[int],
    *,
    platform_name: str,
) -> bool:
    snapshot = _process_snapshot_posix() if platform_name != "nt" else None
    if snapshot is None:
        descendant_alive = any(_pid_is_alive(pid) for pid in descendants)
    else:
        states = {pid: state for pid, _ppid, _pgrp, state in snapshot}
        descendant_alive = any(
            pid in states and states[pid] != "Z" for pid in descendants
        )
    if process.is_alive() or descendant_alive:
        return False
    return platform_name == "nt" or not _process_group_alive(process.pid)


_DISCOVER_DESCENDANTS = object()


def _terminate_processor_child(
    process: multiprocessing.Process,
    *,
    platform_name: str | None = None,
    cleanup_timeout: float = 1.0,
    tracked_descendants: set[int] | None | object = _DISCOVER_DESCENDANTS,
) -> set[int]:
    platform_name = platform_name or os.name
    direct_was_alive = process.is_alive()
    discover_descendants = tracked_descendants is _DISCOVER_DESCENDANTS
    if not direct_was_alive and platform_name == "nt" and discover_descendants:
        raise QueueOperationError("process_cleanup_failed")
    descendants = (
        _tracked_descendant_pids(process.pid, platform_name)
        if discover_descendants
        else tracked_descendants
    )
    if descendants is not None and _cleanup_confirmed(
        process, descendants, platform_name=platform_name
    ):
        return descendants
    tree_verified = False
    if platform_name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            tree_verified = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            tree_verified = False
        if descendants is not None:
            for pid in descendants:
                if not _pid_is_alive(pid):
                    continue
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
    else:
        try:
            _kill_process_group(process.pid, signal.SIGTERM)
            tree_verified = True
        except OSError:
            tree_verified = False
    process.join(0.2)
    if process.is_alive():
        if platform_name != "nt" and tree_verified:
            try:
                _kill_process_group(process.pid, signal.SIGKILL)
            except OSError:
                tree_verified = False
            process.join(0.2)
    if process.is_alive():
        try:
            process.terminate()
        except (OSError, ValueError):
            pass
        process.join(0.2)
    if process.is_alive():
        try:
            process.kill()
        except (OSError, ValueError):
            pass
        process.join(0.2)
    deadline = time.monotonic() + max(0.0, cleanup_timeout)
    cleanup_verified = descendants is not None and _cleanup_confirmed(
        process, descendants, platform_name=platform_name
    )
    while not cleanup_verified and descendants is not None and time.monotonic() < deadline:
        time.sleep(0.02)
        cleanup_verified = _cleanup_confirmed(
            process, descendants, platform_name=platform_name
        )
    if not cleanup_verified or (platform_name != "nt" and not tree_verified):
        raise QueueOperationError("process_cleanup_failed")
    return descendants


def _decode_processor_frame(frame: bytes) -> bool | DeferredResult:
    if not frame:
        raise QueueOperationError("processor_result_malformed")
    tag = frame[:1]
    if tag == b"D":
        data = frame[1:]
        if len(data) > _MAX_RESULT_BYTES:
            raise QueueOperationError("processor_result_oversize")
        return DeferredResult(data)
    if frame == b"T":
        return True
    if frame == b"F":
        return False
    if frame == b"E":
        raise QueueOperationError("processor_exception")
    raise QueueOperationError("processor_result_malformed")


def _run_processor_child(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
) -> bool | DeferredResult:
    if timeout <= 0:
        raise TimeoutError
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=True)
    process = context.Process(
        target=_processor_child_entry,
        args=(sender, processor, task),
        daemon=False,
    )
    deadline = time.monotonic() + timeout
    tracked_descendants: set[int] | None = None
    started = False
    try:
        process.start()
        started = True
        sender.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not receiver.poll(remaining):
            tracked_descendants = _terminate_processor_child(process)
            raise TimeoutError
        try:
            ready = receiver.recv_bytes(1)
        except OSError:
            raise QueueOperationError("processor_result_malformed") from None
        except EOFError:
            raise QueueOperationError("processor_result_malformed") from None
        if ready != b"R":
            raise QueueOperationError("processor_result_malformed")
        tracked_descendants = _tracked_descendant_pids(process.pid, os.name)
        receiver.send_bytes(b"A")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not receiver.poll(remaining):
            tracked_descendants = _terminate_processor_child(
                process, tracked_descendants=tracked_descendants
            )
            raise TimeoutError
        try:
            frame = receiver.recv_bytes(_MAX_RESULT_BYTES + 1)
        except OSError:
            receiver.close()
            process.join(0.5)
            if process.is_alive():
                tracked_descendants = _terminate_processor_child(
                    process, tracked_descendants=tracked_descendants
                )
            raise QueueOperationError("processor_result_oversize") from None
        except EOFError:
            raise QueueOperationError("processor_result_malformed") from None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tracked_descendants = _terminate_processor_child(
                process, tracked_descendants=tracked_descendants
            )
            raise TimeoutError
        process.join(remaining)
        if process.is_alive():
            tracked_descendants = _terminate_processor_child(
                process, tracked_descendants=tracked_descendants
            )
            raise TimeoutError
        if process.exitcode != 0:
            raise QueueOperationError("processor_child_failed")
        return _decode_processor_frame(frame)
    finally:
        receiver.close()
        if started:
            _terminate_processor_child(
                process, tracked_descendants=tracked_descendants
            )


def _work_one(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    *,
    owner: str,
    deadline: float,
    monotonic: Callable[[], float],
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> dict[str, int]:
    counts = {"ok": 0, "failed": 0, "dead": 0, "skipped": 0, "halted": 0}
    lease = queue.claim(
        owner, lease_seconds=lease_seconds, max_attempts=max_attempts
    )
    if lease is None:
        return counts
    adopted = queue.adopt_published_result(lease, operation_id=lease.id)
    if adopted == "adopted":
        _count_terminal(counts, queue.acknowledge(lease))
        return counts
    if adopted == "corrupt":
        _count_terminal(counts, queue.get(lease.id))
        return counts
    heartbeat = _LeaseHeartbeat(
        queue,
        lease,
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
    )
    heartbeat.start()
    timed_out = False
    cleanup_failed = False
    outcome: bool | DeferredResult = False
    try:
        remaining = deadline - monotonic()
        if remaining <= 0:
            timed_out = True
        else:
            try:
                outcome = processor_runner(processor, _compat_task(lease), remaining)
            except TimeoutError:
                timed_out = True
            except QueueOperationError as exc:
                if exc.code == "process_cleanup_failed":
                    cleanup_failed = True
                else:
                    outcome = False
            except Exception:  # noqa: BLE001 - queue exposes stable codes only
                outcome = False
        if monotonic() >= deadline:
            timed_out = True
    finally:
        heartbeat.stop()
    if heartbeat.error is not None and not cleanup_failed:
        counts["failed"] += 1
        return counts
    try:
        if cleanup_failed:
            counts["halted"] = 1
            queue.fail(
                lease,
                QueueFailure(
                    "process_cleanup_failed",
                    blocked_capability="process_cleanup",
                ),
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                retry_cap_seconds=retry_cap_seconds,
            )
            _count_terminal(counts, queue.get(lease.id))
        elif timed_out:
            queue.fail(
                lease,
                QueueFailure("worker_timeout"),
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                retry_cap_seconds=retry_cap_seconds,
            )
            _count_terminal(counts, queue.get(lease.id))
        elif bool(outcome):
            result = outcome.data if isinstance(outcome, DeferredResult) else b""
            queue.publish_result(lease, operation_id=lease.id, result=result)
            _count_terminal(counts, queue.acknowledge(lease))
        else:
            queue.fail(
                lease,
                QueueFailure("processor_failed"),
                max_attempts=max_attempts,
                retry_base_seconds=retry_base_seconds,
                retry_cap_seconds=retry_cap_seconds,
            )
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
    lease_seconds: int = DEFAULTS.queue_lease_seconds,
    heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
    max_attempts: int = DEFAULTS.queue_max_attempts,
    retry_base_seconds: int = DEFAULTS.retry_base_seconds,
    retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ] = _run_processor_child,
    cancelled: Callable[[], bool] | None = None,
) -> WorkerSummary:
    """Run a short-lived worker bounded by tasks, wall time, and idle time."""
    if max_tasks < 0 or max_seconds < 0 or idle_seconds < 0:
        raise ValueError("worker limits must be non-negative")
    _validate_worker_policy(
        lease_seconds,
        heartbeat_seconds,
        max_attempts,
        retry_base_seconds,
        retry_cap_seconds,
    )
    started = monotonic()
    idle_started: float | None = None
    totals = {"ok": 0, "failed": 0, "dead": 0, "skipped": 0}
    processed = 0
    queue = _queue(
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )
    owner = f"worker-{os.getpid()}-{uuid.uuid4().hex}"
    deadline = started + max_seconds
    while processed < max_tasks:
        if cancelled and cancelled():
            break
        now = monotonic()
        if now >= deadline:
            break
        if idle_started is not None and now - idle_started >= idle_seconds:
            break
        counts = _work_one(
            queue,
            processor,
            processor_runner,
            owner=owner,
            deadline=deadline,
            monotonic=monotonic,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds,
        )
        handled = counts["ok"] + counts["failed"]
        for key in totals:
            totals[key] += counts[key]
        if handled:
            processed += handled
            idle_started = None
            if counts.get("halted"):
                break
            continue
        if idle_seconds == 0:
            break
        if idle_started is None:
            idle_started = now
        idle_remaining = idle_seconds - (now - idle_started)
        budget_remaining = deadline - monotonic()
        if budget_remaining <= 0:
            break
        sleep(min(1.0, idle_remaining, budget_remaining))
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
        root = Path(os.environ.get("LLM_WIKI_ROOT", ".")).resolve()
        command = [
            sys.executable,
            str(root / "scripts" / "compile_memory.py"),
            "--trigger",
            "auto",
        ]
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return completed.returncode == 0
    return False


def _cli_error_code(error: BaseException) -> str:
    if isinstance(error, QueueOperationError):
        code = error.code
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            return code
        return "internal_error"
    if isinstance(error, KeyError):
        return "task_not_found"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, sqlite3.Error):
        return "sqlite_error"
    if isinstance(error, (UnicodeError, json.JSONDecodeError)):
        return "decode_error"
    if isinstance(
        error,
        (
            subprocess.SubprocessError,
            multiprocessing.ProcessError,
            BrokenPipeError,
            EOFError,
            ChildProcessError,
        ),
    ):
        return "process_error"
    if isinstance(error, TimeoutError):
        return "worker_timeout"
    if isinstance(error, OSError):
        return "os_error"
    if isinstance(error, (TypeError, ValueError)):
        return "invalid_input"
    return "internal_error"


def _emit_cli_error(error: BaseException) -> int:
    payload = canonical_json_bytes({"codes": [_cli_error_code(error)]})
    print(payload.decode("utf-8"))
    return 2


def _emit_invalid_arguments() -> int:
    payload = canonical_json_bytes({"ok": False, "code": "invalid_arguments"})
    print(payload.decode("utf-8"))
    return 2


def _cli() -> int:
    parser = _RedactedArgumentParser()
    parser.add_argument(
        "command",
        choices=["list", "status", "work", "cancel", "redrive", "migrate", "purge"],
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
    try:
        args = parser.parse_args()
        if args.command == "list":
            records = [
                {"id": task.id, "state": task.state} for task in _queue().list_tasks()
            ]
            print(json.dumps(records, sort_keys=True))
            return 0
        if args.command == "status":
            print(json.dumps(_operator_status(), sort_keys=True))
            return 0
        if args.command == "work":
            try:
                _validate_worker_policy(
                    args.lease_seconds,
                    args.heartbeat_seconds,
                    args.max_attempts,
                    args.retry_base_seconds,
                    args.retry_cap_seconds,
                )
            except ValueError as exc:
                parser.error(str(exc))
            summary = run_worker(
                _manual_processor,
                max_tasks=args.max_tasks,
                max_seconds=args.max_seconds,
                idle_seconds=args.idle_seconds,
                lease_seconds=args.lease_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                max_attempts=args.max_attempts,
                retry_base_seconds=args.retry_base_seconds,
                retry_cap_seconds=args.retry_cap_seconds,
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
    except _InvalidArguments:
        return _emit_invalid_arguments()
    except Exception as exc:  # noqa: BLE001 - CLI is a redacted trust boundary
        return _emit_cli_error(exc)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
