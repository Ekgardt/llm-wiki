"""Fenced SQLite queue for deferred memory-pipeline work."""

from __future__ import annotations

import email.utils
import json
import os
import random
import sqlite3
import sys
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
    fsync_file,
    open_operational_db,
    sha256_bytes,
)
from secret_redact import redact_secrets

_STATES = ("ready", "leased", "blocked", "succeeded", "dead", "cancelled")
_TERMINAL_STATES = ("succeeded", "dead", "cancelled")
_PERMANENT_CODES = {"invalid_input", "unsupported_version"}


class LeaseFenceError(RuntimeError):
    """Raised when a lease token no longer owns an unexpired task."""


class ResultConflictError(RuntimeError):
    """Raised when an operation ID already names different result bytes."""


@dataclass(frozen=True)
class QueueFailure:
    error_code: str
    permanent: bool = False
    blocked_capability: str | None = None
    retry_after: float | datetime | str | None = None


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
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_heartbeat_at: datetime | None
    error_code: str | None
    blocked_capability: str | None
    result_reference: str | None
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


def _redact_payload(value: object) -> object:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    return value


class MemoryQueue:
    """Durable priority queue with lease-token fencing and at-least-once delivery."""

    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.run_dir = self.state_root / "run"
        self.db_path = self.run_dir / "queue.sqlite3"
        self.results_dir = self.run_dir / "queue-results"
        self._clock = clock or _utc_now
        self._rng = rng or random.SystemRandom()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _set_owner_only(self.results_dir, 0o700)
        with self._connect() as connection:
            self._create_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        return open_operational_db(self.db_path, busy_ms=DEFAULTS.queue_busy_ms)

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
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 8),
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                lease_heartbeat_at TEXT,
                attempt_started_at TEXT,
                error_code TEXT,
                blocked_capability TEXT,
                result_reference TEXT,
                result_operation_id TEXT
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
        lease: int = DEFAULTS.queue_lease_seconds,
        lease_seconds: int | None = None,
    ) -> QueueLease | None:
        if not owner:
            raise ValueError("owner must be non-empty")
        duration = lease_seconds if lease_seconds is not None else lease
        if duration <= 0:
            raise ValueError("lease must be positive")
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            self._expire_leases(connection, now)
            row = connection.execute(
                """SELECT * FROM tasks
                   WHERE state = 'ready' AND available_at <= ?
                   ORDER BY priority DESC, available_at, created_at, rowid LIMIT 1""",
                (_timestamp(now),),
            ).fetchone()
            if row is None:
                return None
            token = f"{self._rng.getrandbits(256):064x}"
            expires = now + timedelta(seconds=duration)
            changed = connection.execute(
                """UPDATE tasks SET state='leased', attempts=attempts+1,
                       lease_owner=?, lease_token=?, lease_expires_at=?, lease_heartbeat_at=?,
                       attempt_started_at=?, updated_at=?, error_code=NULL, blocked_capability=NULL
                   WHERE id=? AND state='ready'""",
                (
                    owner,
                    token,
                    _timestamp(expires),
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    row["id"],
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
        )

    def _expire_leases(self, connection: sqlite3.Connection, now: datetime) -> int:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='leased' AND lease_expires_at <= ?",
            (_timestamp(now),),
        ).fetchall()
        for row in rows:
            self._record_attempt(connection, row, now, "lease_expired", "lease_expired")
            connection.execute(
                """UPDATE tasks SET state='ready', available_at=?, updated_at=?,
                       lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                       lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code='lease_expired'
                   WHERE id=? AND state='leased' AND lease_token=?""",
                (_timestamp(now), _timestamp(now), row["id"], row["lease_token"]),
            )
        return len(rows)

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
        digest = sha256_bytes(result)
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        temporary = self.results_dir / f".{result_name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(result)
                handle.flush()
                os.fsync(handle.fileno())
            _set_owner_only(temporary, 0o600)
            now = _as_utc(self._clock())
            with self._connect() as connection, begin_immediate(connection):
                row = self._require_lease(connection, lease, now)
                existing_operation = row["result_operation_id"]
                existing_reference = row["result_reference"]
                if existing_operation is not None and existing_operation != operation_id:
                    raise ResultConflictError("lease already published a different operation")
                if target.exists():
                    if sha256_bytes(target.read_bytes()) != digest:
                        raise ResultConflictError("operation ID already has different result bytes")
                else:
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        if sha256_bytes(target.read_bytes()) != digest:
                            raise ResultConflictError(
                                "operation ID already has different result bytes"
                            ) from None
                    _set_owner_only(target, 0o600)
                    fsync_file(target)
                connection.execute(
                    """UPDATE tasks SET result_reference=?, result_operation_id=?, updated_at=?
                       WHERE id=?""",
                    (relative, operation_id, _timestamp(now), lease.id),
                )
                return str(existing_reference or relative)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def acknowledge(self, lease: QueueLease) -> None:
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if row["result_reference"] is None:
                raise ValueError("a result must be published before acknowledgement")
            self._record_attempt(connection, row, now, "succeeded", None)
            self._finish_lease(connection, lease.id, now, "succeeded", None, None)

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
                    connection, lease.id, now, "dead", failure.error_code, None
                )
                return
            jitter_cap = min(
                DEFAULTS.retry_cap_seconds,
                DEFAULTS.retry_base_seconds * (2 ** (int(row["attempts"]) - 1)),
            )
            delay = self._rng.uniform(0, jitter_cap)
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
            )

    @staticmethod
    def _retry_after_seconds(
        value: float | datetime | str | None, now: datetime
    ) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) if value >= 0 else None
        if isinstance(value, datetime):
            return max(0.0, (_as_utc(value) - now).total_seconds())
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isdigit():
                return float(stripped)
            try:
                parsed = email.utils.parsedate_to_datetime(stripped)
            except (TypeError, ValueError, OverflowError):
                return None
            return max(0.0, (_as_utc(parsed) - now).total_seconds())
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
    ) -> None:
        connection.execute(
            """UPDATE tasks SET state=?, updated_at=?, available_at=COALESCE(?, available_at),
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?,
                   blocked_capability=NULL WHERE id=?""",
            (
                state,
                _timestamp(now),
                _timestamp(available_at) if available_at else None,
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
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=_parse_timestamp(row["lease_expires_at"]),
            lease_heartbeat_at=_parse_timestamp(row["lease_heartbeat_at"]),
            error_code=row["error_code"],
            blocked_capability=row["blocked_capability"],
            result_reference=row["result_reference"],
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


def _queue_dir() -> Path:
    """Compatibility path: SQLite and results now live directly under run/."""
    path = _state_root() / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _queue() -> MemoryQueue:
    return MemoryQueue(_state_root())


def enqueue(task_type: str, payload: dict[str, Any]) -> str:
    """Compatibility facade that enqueues handler version 1."""
    return _queue().enqueue(task_type, 1, payload)


def _compat_task(task: QueueTask | QueueLease) -> dict[str, Any]:
    created = task.created_at if isinstance(task, QueueTask) else None
    attempts = task.attempts if isinstance(task, QueueTask) else task.attempt
    return {
        "id": task.id,
        "type": task.kind,
        "handler_version": task.handler_version,
        "enqueued_at": created.isoformat(timespec="seconds") if created else None,
        "attempts": attempts,
        "last_attempt_at": None,
        "payload": task.payload,
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


def drain_with(processor: Callable[[dict], bool], max_tasks: int = 10) -> dict[str, int]:
    """Run handlers outside transactions and fence completion with a result marker."""
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    queue = _queue()
    owner = f"compat-{os.getpid()}-{uuid.uuid4().hex}"
    for _ in range(max_tasks):
        lease = queue.claim(owner)
        if lease is None:
            break
        try:
            succeeded = bool(processor(_compat_task(lease)))
        except Exception as exc:  # noqa: BLE001
            print(
                f"memory_queue: processor raised {type(exc).__name__}: {exc}", file=sys.stderr
            )
            succeeded = False
        if succeeded:
            queue.publish_result(lease, operation_id=lease.id, result=b"")
            queue.acknowledge(lease)
            counts["ok"] += 1
        else:
            queue.fail(lease, QueueFailure("processor_failed", retry_after=60))
            counts["failed"] += 1
    return counts


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


def _manual_processor(task: dict[str, Any]) -> bool:
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
        output_path = payload.get("output_path")
        results_dir = _queue().results_dir
        if not output_path:
            output_path = results_dir / f"{task['id']}.txt"
        else:
            output_path = Path(output_path).resolve()
            try:
                output_path.relative_to(results_dir.resolve())
            except ValueError:
                return False
        Path(output_path).write_text(result, encoding="utf-8")
        return True
    if task_type == "flush":
        prompt = payload.get("prompt", "")
        if not prompt:
            return False
        result = call_llm(
            prompt,
            payload.get("system_prompt", ""),
            max_tokens=int(payload.get("max_tokens") or 1500),
        )
        if not result:
            return False
        from daily_log_append import locked_append
        from flush_memory import _classify_response

        tier, body = _classify_response(result)
        if tier != "ok" and body:
            now = _utc_now()
            day = payload.get("day") or now.strftime("%Y-%m-%d")
            daily_path = (
                Path(os.environ.get("LLM_WIKI_ROOT", "."))
                / "knowledge"
                / "daily"
                / f"{day}.md"
            )
            session_id = payload.get("session_id", "deferred")
            event = payload.get("event", "session-end")
            block = (
                f"\n## [{now.strftime('%H:%M:%S')}] deferred-{event} | {session_id}\n"
                f"- Tier: `{tier}`\n\n{redact_secrets(body)}\n"
            )
            locked_append(daily_path, block)
        return True
    if task_type == "compile":
        from maybe_compile import spawn_compile_if_idle

        spawned, reason = spawn_compile_if_idle(force=bool(payload.get("force")))
        return spawned or "no pending" in reason
    return False


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["list", "status", "drain", "clear-failed"])
    args = parser.parse_args()
    if args.command == "list":
        for task in list_pending():
            print(
                f"  {task['id']}  type={task['type']}  attempts={task['attempts']} "
                f"enqueued={task['enqueued_at']}"
            )
        return 0
    if args.command == "status":
        print(json.dumps(status(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "drain":
        counts = drain_with(_manual_processor, max_tasks=20)
        print(f"drain complete: {counts}")
        return 0
    print("dead tasks are retained; export-first purge is introduced in Task 7")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
