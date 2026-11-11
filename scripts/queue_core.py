"""Fenced SQLite queue for deferred memory-pipeline work."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from llm_wiki.queue_artifacts import harden_owner_only as harden_owner_only
from llm_wiki.queue_artifacts import stream_digest as stream_digest
from llm_wiki.queue_artifacts import write_durable_file as write_durable_file
from llm_wiki.queue_artifacts import write_durable_producer as write_durable_producer
from llm_wiki.queue_attempts import attempt_histories as attempt_histories
from llm_wiki.queue_attempts import failure_state as failure_state
from llm_wiki.queue_attempts import take_v3_task_lease as take_v3_task_lease
from llm_wiki.queue_child_runtime import (
    ChildRun as ChildRun,
)
from llm_wiki.queue_child_runtime import (
    child_ready_handshake as child_ready_handshake,
)
from llm_wiki.queue_child_runtime import (
    decode_processor_frame as decode_processor_frame,
)
from llm_wiki.queue_child_runtime import (
    settle_child_exit as settle_child_exit,
)
from llm_wiki.queue_contracts import MAX_RESULT_BYTES as MAX_RESULT_BYTES
from llm_wiki.queue_migration_plan import parents_before_children as parents_before_children
from llm_wiki.queue_migration_plan import queue_v2_columns as queue_v2_columns
from llm_wiki.queue_process_cleanup import (
    pid_is_alive as pid_is_alive,
)
from llm_wiki.queue_process_cleanup import (
    taskkill as taskkill,
)
from llm_wiki.queue_task_lineage import (
    redrive_ancestors as redrive_ancestors,
)
from llm_wiki.queue_task_lineage import (
    redrive_lineage as redrive_lineage,
)
from llm_wiki.queue_task_lineage import (
    require_inherited_capture_decision as require_inherited_capture_decision,
)
from llm_wiki.queue_task_records import ExportedRows as ExportedRows
from llm_wiki.queue_task_records import task_record as task_record
from llm_wiki.queue_v3_candidate import (
    queue_v3_invariants_hold as queue_v3_invariants_hold,
)
from llm_wiki.queue_validation import (
    require_claim_owner_and_lease as require_claim_owner_and_lease,
)
from llm_wiki.queue_validation import (
    require_legacy_enqueue_arguments as require_legacy_enqueue_arguments,
)
from llm_wiki.queue_validation import validate_retry_policy as validate_retry_policy
from llm_wiki.queue_validation import validate_worker_policy as validate_worker_policy
from llm_wiki.queue_worker_state import PROCESSOR_ERROR_CODE_BYTES as PROCESSOR_ERROR_CODE_BYTES
from llm_wiki.queue_worker_state import ProcessorOutcome as ProcessorOutcome
from llm_wiki.queue_worker_state import QueueRetryPolicy as QueueRetryPolicy
from llm_wiki.queue_worker_state import WorkerProgress as WorkerProgress
from llm_wiki.queue_worker_state import processor_error_code as processor_error_code

if TYPE_CHECKING:
    from operational_ownership import OwnerLease

import compile_progress
from llm_wiki import queue_store, runtime_roots
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import (
    PayloadValidation as PayloadValidation,
)
from llm_wiki.formats.queue_payload import (
    validate_payload_blob as validate_payload_blob,
)
from llm_wiki.memory_queue_failures import MemoryQueueFailures as MemoryQueueFailures
from llm_wiki.memory_queue_purge import MemoryQueuePurge as MemoryQueuePurge
from llm_wiki.memory_queue_results import MemoryQueueResults as MemoryQueueResults
from llm_wiki.memory_queue_sources import MemoryQueueSourceFences as MemoryQueueSourceFences
from llm_wiki.memory_queue_sources import insert_source_fence as insert_source_fence
from llm_wiki.queue_contracts import CaptureTaskBinding as CaptureTaskBinding
from llm_wiki.queue_contracts import CompileCompletionError as CompileCompletionError
from llm_wiki.queue_contracts import CorruptExportProgress as CorruptExportProgress
from llm_wiki.queue_contracts import CorruptPurgeProgress as CorruptPurgeProgress
from llm_wiki.queue_contracts import DeferredResult as DeferredResult
from llm_wiki.queue_contracts import LeaseFenceError as LeaseFenceError
from llm_wiki.queue_contracts import LeaseProcessor as LeaseProcessor
from llm_wiki.queue_contracts import PurgeReceipt as PurgeReceipt
from llm_wiki.queue_contracts import QueueFailure as QueueFailure
from llm_wiki.queue_contracts import QueueLease as QueueLease
from llm_wiki.queue_contracts import QueueOperationError as QueueOperationError
from llm_wiki.queue_contracts import QueueTask as QueueTask
from llm_wiki.queue_contracts import RestoreReceipt as RestoreReceipt
from llm_wiki.queue_contracts import ResultConflictError as ResultConflictError
from llm_wiki.queue_contracts import WorkerSummary as WorkerSummary
from llm_wiki.queue_database_contract import QUEUE_COMPILE_CONTRACT as QUEUE_COMPILE_CONTRACT
from llm_wiki.queue_database_contract import (
    QUEUE_COMPILE_SCHEMA_SHA256 as QUEUE_COMPILE_SCHEMA_SHA256,
)
from llm_wiki.queue_database_contract import QUEUE_V3_CONTRACT as QUEUE_V3_CONTRACT
from llm_wiki.queue_database_contract import QUEUE_V3_SCHEMA_SHA256 as QUEUE_V3_SCHEMA_SHA256
from llm_wiki.queue_database_contract import (
    queue_contract_for_version as queue_contract_for_version,
)
from llm_wiki.queue_heartbeat import (
    LeaseHeartbeat as LeaseHeartbeat,
)
from llm_wiki.queue_heartbeat import (
    SourceFenceHeartbeat as SourceFenceHeartbeat,
)
from llm_wiki.queue_reader_base import QueueReaderFoundation
from llm_wiki.queue_reader_capture import QueueCaptureOperations
from llm_wiki.queue_reader_compile import QueueCompileOperations
from llm_wiki.queue_reader_purge import QueuePurgeOperations
from llm_wiki.queue_reader_quarantine import QueueQuarantineOperations
from llm_wiki.queue_reader_semantic import (
    QueueSemanticDecisionOperations,
)
from llm_wiki.queue_reader_sources import QueueSourceFenceOperations
from llm_wiki.queue_reader_tasks import QueueTaskOperations
from llm_wiki.queue_schema_operations import (
    check_no_populated_rebuild as check_no_populated_rebuild,
)
from llm_wiki.queue_schema_operations import drop_schema_object as drop_schema_object
from llm_wiki.queue_schema_operations import queue_v3_schema_complete as queue_v3_schema_complete
from llm_wiki.queue_schema_operations import table_is_populated as table_is_populated
from llm_wiki.queue_sources import queue_v3_source_links as queue_v3_source_links
from llm_wiki.queue_sources import source_identity_from_payload as source_identity_from_payload
from llm_wiki.queue_sources import validate_failure_identity as validate_failure_identity
from llm_wiki.queue_sources import validate_source_identity as validate_source_identity
from llm_wiki.queue_store import (
    EXPIRED_LEASE_OUTCOMES,
    TaskFence,
    age_filter_clause,
    payload_references_source,
    register_source_reference,
    renewed_source_fence,
    require_adopted_attempt_limit,
    require_adopted_retry_policy,
    require_no_source_failure,
    same_enqueued_task,
    validated_listed_states,
)
from llm_wiki.queue_timing import as_utc as as_utc
from llm_wiki.queue_timing import parse_timestamp as parse_timestamp
from llm_wiki.queue_timing import retry_available_at as retry_available_at
from llm_wiki.queue_timing import timestamp as timestamp
from llm_wiki.queue_v2_migration import (
    initialize_queue_v3_candidate as initialize_queue_v3_candidate,
)
from llm_wiki.queue_v2_migration import queue_v3_report as queue_v3_report
from llm_wiki.queue_v2_migration import require_empty_queue_v3 as require_empty_queue_v3
from llm_wiki.queue_v2_migration import require_queue_v3_openable as require_queue_v3_openable
from llm_wiki.queue_v2_migration import validate_queue_v3_database as validate_queue_v3_database
from llm_wiki.queue_worker import WORKER_MIN_CLAIM_SECONDS as WORKER_MIN_CLAIM_SECONDS
from llm_wiki.queue_worker import bind_lease_processor as bind_lease_processor
from llm_wiki.queue_worker import claim_when_reachable as claim_when_reachable
from llm_wiki.queue_worker import compat_task as compat_task
from llm_wiki.queue_worker import drive_worker as drive_worker
from llm_wiki.queue_worker import interruptible as interruptible
from llm_wiki.queue_worker import is_busy_database as is_busy_database
from llm_wiki.queue_worker import processor_child_entry as processor_child_entry
from llm_wiki.queue_worker import processor_result_frame as processor_result_frame
from llm_wiki.queue_worker import run_and_settle as run_and_settle
from llm_wiki.queue_worker import run_processor as run_processor
from llm_wiki.queue_worker import run_processor_child as run_processor_child
from llm_wiki.queue_worker import run_processor_inline as run_processor_inline
from llm_wiki.queue_worker import unsuccessful_worker_failure as unsuccessful_worker_failure
from llm_wiki.queue_worker import work_claimed as work_claimed
from llm_wiki.queue_worker import work_one as work_one
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_contracts import (
    OperationalDatabaseContract,
    quote_sqlite_identifier,
)
from llm_wiki.sqlite_contracts import (
    OperationalDatabaseContractError as OperationalDatabaseContractError,
)
from llm_wiki.sqlite_contracts import migration_error as migration_error
from llm_wiki.sqlite_deadlines import (
    operational_deadline_active,
    refresh_operational_deadline,
)
from llm_wiki.sqlite_transactions import begin_immediate
from llm_wiki.windows_acl import acl_is_owner_only as acl_is_owner_only
from llm_wiki.windows_acl import acl_owner_lines as acl_owner_lines
from llm_wiki.windows_acl import is_owner_only_windows as is_owner_only_windows
from llm_wiki.windows_acl import windows_acl_lines as windows_acl_lines
from reliable_memory import (
    open_operational_db,
)
from secret_redact import redact_structure

# A queue failure's redacted detail as the CLI prints it: one readable line, not a dump.
MAX_CLI_DETAIL_CHARS = 240


# Which bucket each payload key contributes a source-identity string to.


# The tables a queue v2 migration fills, and the ones it must leave empty.


class InvalidArguments(Exception):
    pass


class RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise InvalidArguments

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            del message
            raise InvalidArguments
        super().exit(status, message)


def _legacy_dedupe_match(
    existing: sqlite3.Row,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    input_hash: str,
) -> str:
    """The existing id when it is the same task; a dedupe conflict otherwise."""
    existing_bytes = _legacy_payload_bytes(existing)
    validation = validate_payload_blob(
        existing_bytes, existing["input_hash"], parse=True
    )
    if validation.code is None and same_enqueued_task(
        existing, existing_bytes, kind, handler_version, payload_bytes, input_hash
    ):
        return str(existing["id"])
    raise QueueOperationError("dedupe_conflict")


def _legacy_payload_bytes(row: sqlite3.Row) -> bytes:
    try:
        return str(row["payload_json"]).encode("utf-8", errors="strict")
    except UnicodeError:
        return b""


# What the legacy queue's claim may take, and therefore what its count names:
# one predicate, as `V3_WORK_CLAIMABLE` is for the adopted queue. Parameters:
# max_attempts, now.
_V2_WORK_CLAIMABLE = """state = 'ready' AND attempts < ? AND available_at <= ?
    AND NOT EXISTS (
        SELECT 1 FROM source_fences f
        WHERE payload_references_source(tasks.payload_json, f.daily_id, f.source_digest)
    )"""


def _next_unfenced_ready_task(
    connection: sqlite3.Connection, now: datetime, attempt_limit: int
) -> sqlite3.Row | None:
    """The next ready task whose source nobody is finalizing."""
    register_source_reference(connection)
    return connection.execute(
        f"""SELECT * FROM tasks WHERE {_V2_WORK_CLAIMABLE}
           ORDER BY priority DESC, available_at, created_at, rowid LIMIT 1""",
        (attempt_limit, timestamp(now)),
    ).fetchone()


# The attempt outcome, task state, and error code for a lease that expired after
# its result was published, keyed by whether that result was still intact.
_EXPIRED_PUBLISHED_OUTCOMES = {
    True: ("succeeded", "succeeded", None),
    False: ("failed", "dead", "result_corrupt"),
}


_SQLITE_FILE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite_file(path: Path) -> bool:
    """Whether the file begins with the SQLite header, without opening a database."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_FILE_MAGIC)) == _SQLITE_FILE_MAGIC
    except OSError:
        return False


def _state_relative_name(path: Path, state_root: Path) -> str:
    """The path as the operator knows it: relative to the state root, POSIX form.

    The refusal detail below travels through the CLI's 240-character bound.
    Composed with absolute paths it failed its own purpose exactly where paths
    are long -- macOS `/private/var/folders/...` and Windows `D:\\a\\...` temp
    roots pushed the adopted path past the cut. The relative form is short and
    stable everywhere, and the operator already knows the root.
    """
    try:
        return path.relative_to(state_root).as_posix()
    except ValueError:
        return str(path)


def _require_no_adoption_tombstone(db_path: Path, state_root: Path) -> None:
    """Refuse a legacy queue path by name once adoption has replaced it.

    Adoption leaves a JSON tombstone where the pre-adoption database was, so a
    reader that opens it anyway gets `file is not a database` -- a refusal that
    names neither the boundary that broke nor the path that broke it. The
    tombstone already names its replacement, and one reader follows it, so ask
    that reader and say what it found.
    """
    if _is_sqlite_file(db_path):
        return
    from llm_wiki.adoption_admission import (
        adopted_database_path,
    )

    active = adopted_database_path(database_name="queue", state_root=state_root)
    if active == db_path:
        return
    raise QueueOperationError(
        "queue_tombstoned_by_adoption",
        f"{_state_relative_name(db_path, state_root)} is a Reliability V3 "
        f"adoption tombstone naming {_state_relative_name(active, state_root)}; "
        "open the adopted queue through active_or_legacy_memory_queue()",
    )


def upgrade_legacy_attempt_constraint(connection: sqlite3.Connection) -> None:
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


class MemoryQueue(MemoryQueueResults, MemoryQueueFailures, MemoryQueueSourceFences, MemoryQueuePurge):
    """Durable priority queue with lease-token fencing and at-least-once delivery."""

    @classmethod
    def from_v3_candidate(
        cls, path: Path, *, state_root: Path, vault: Path | None = None
    ) -> QueueV3CandidateReader:
        report = validate_queue_v3_database(path, state_root=state_root)
        return QueueV3CandidateReader(
            Path(path), vault=state_root if vault is None else vault,
            expected_contract=queue_contract_for_version(report["user_version"]),
        )

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
        deadline: float | None = None,
    ) -> None:
        operational_deadline_active(deadline)
        validate_retry_policy(max_attempts, retry_base_seconds, retry_cap_seconds)
        self.state_root = Path(state_root).resolve()
        self.run_dir = self.state_root / "run"
        self.db_path = self.run_dir / "queue.sqlite3"
        _require_no_adoption_tombstone(self.db_path, self.state_root)
        self.results_dir = self.run_dir / "queue-results"
        self._clock = clock or queue_store.utc_now
        self._rng = rng or random.SystemRandom()
        self._heartbeat_wait = heartbeat_wait or (
            lambda stop, interval: stop.wait(interval)
        )
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._db_hardened = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        harden_owner_only(self.run_dir, 0o700)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        harden_owner_only(self.results_dir, 0o700)
        with self._connect(deadline=deadline) as connection:
            self._create_schema(connection)
            self._retire_exhausted_on_open(connection, deadline=deadline)

    def _retire_exhausted_on_open(self, connection: sqlite3.Connection, *, deadline: float | None = None) -> None:
        """Take the write lock only when a ready task is out of attempts.

        Every construction opened `BEGIN IMMEDIATE`, so opening a queue while a
        writer held it failed with `database is locked` although there was
        nothing to retire (docs/research/2026-09-25-opening-the-legacy-queue-takes-no-write-lock.md).
        """
        # fetchall steps the statement to its end, so no read lock outlives it.
        exhausted = connection.execute(
            "SELECT 1 FROM tasks WHERE state='ready' AND attempts >= ? LIMIT 1",
            (self._max_attempts,),
        ).fetchall()
        if not exhausted:
            return
        with begin_immediate(connection):
            self._retire_exhausted_ready(connection, as_utc(self._clock()), self._max_attempts)
            refresh_operational_deadline(connection, DEFAULTS.queue_busy_ms, deadline)

    @contextmanager
    def _connect(self, *, busy_ms: int | None = None, deadline: float | None = None) -> Iterator[sqlite3.Connection]:
        connection = open_operational_db(self.db_path, busy_ms=DEFAULTS.queue_busy_ms if busy_ms is None else busy_ms, deadline=deadline)
        try:
            # Preserve this legacy context manager's implicit commit/rollback API.
            connection.isolation_level = "DEFERRED"
            if not self._db_hardened:
                harden_owner_only(self.db_path, 0o600)
                self._db_hardened = True
            with connection:
                yield connection
                refresh_operational_deadline(connection, DEFAULTS.queue_busy_ms, deadline)
        finally:
            connection.close()
            operational_deadline_active(deadline)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """One write connection; the seam both queue backends offer alike."""
        with self._connect() as database:
            yield database

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        upgrade_legacy_attempt_constraint(connection)
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
        _add_missing_queue_columns(connection)
    _upgrade_legacy_attempt_constraint = staticmethod(upgrade_legacy_attempt_constraint)

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
                    timestamp(now),
                    timestamp(now),
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

    def _legacy_deduplicated_task_id(
        self,
        connection: sqlite3.Connection,
        dedupe_key: str | None,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
    ) -> str | None:
        """The id of an identical task already enqueued under this dedupe key."""
        if dedupe_key is None:
            return None
        existing = connection.execute(
            """SELECT id, kind, handler_version, payload_json, input_hash
               FROM tasks WHERE dedupe_key=?""",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            return None
        return _legacy_dedupe_match(
            existing, kind, handler_version, payload_bytes, input_hash
        )

    def _insert_legacy_task_row(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_json: str,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
    ) -> None:
        inserted = connection.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_json, input_hash, dedupe_key,
                   state, priority, created_at, updated_at, available_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)""",
            (
                task_id,
                kind,
                handler_version,
                payload_json,
                input_hash,
                dedupe_key,
                priority,
                timestamp(now),
                timestamp(now),
                timestamp(ready_at),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("enqueue_failed")

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
        require_legacy_enqueue_arguments(kind, handler_version, priority, dedupe_key)
        payload_bytes = canonical_json_bytes(redact_structure(dict(payload)))
        payload_json = payload_bytes.decode("utf-8")
        input_hash = sha256_bytes(payload_bytes)
        now = as_utc(self._clock())
        ready_at = as_utc(available_at or now)
        task_id = uuid.UUID(int=self._rng.getrandbits(128)).hex
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            self._assert_payload_not_fenced(connection, payload_json)
            deduplicated = self._legacy_deduplicated_task_id(
                connection, dedupe_key, kind, handler_version, payload_bytes, input_hash
            )
            if deduplicated is not None:
                return deduplicated
            self._insert_legacy_task_row(
                connection,
                task_id,
                kind,
                handler_version,
                payload_json,
                input_hash,
                dedupe_key,
                priority,
                now,
                ready_at,
            )
        return task_id

    def claim(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int | None = None,
    ) -> QueueLease | None:
        require_claim_owner_and_lease(owner, lease_seconds)
        attempt_limit, _base, _cap = self._retry_policy(max_attempts, None, None)
        now = as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            self._expire_leases(connection, now, attempt_limit)
            self._delete_stale_source_fences(connection)
            row = _next_unfenced_ready_task(connection, now, attempt_limit)
            if row is None:
                return None
            token = f"{self._rng.getrandbits(256):064x}"
            expires = now + timedelta(seconds=lease_seconds)
            if not take_v3_task_lease(
                connection,
                row,
                owner=owner,
                token=token,
                now=now,
                expires=expires,
                attempt_limit=attempt_limit,
            ):
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
            created_at=parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    def _settle_published_expiry(
        self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        """Close a lease that expired after its result had already been published."""
        outcome, state, error_code = _EXPIRED_PUBLISHED_OUTCOMES[
            self._stored_result_is_valid(row)
        ]
        self._record_attempt(connection, row, now, outcome, error_code)
        self._finish_lease(
            connection, row["id"], now, state, error_code, None, last_attempt_at=now
        )

    def _retire_expired_lease(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        max_attempts: int,
    ) -> None:
        """Return an expired lease to the queue, or bury it if it is out of tries."""
        state, error_code = EXPIRED_LEASE_OUTCOMES[
            int(row["attempts"]) >= max_attempts
        ]
        self._record_attempt(connection, row, now, "lease_expired", error_code)
        connection.execute(
            """UPDATE tasks SET state=?, available_at=?, updated_at=?, last_attempt_at=?,
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?
               WHERE id=? AND state='leased' AND lease_token=?""",
            (
                state,
                timestamp(now),
                timestamp(now),
                timestamp(now),
                error_code,
                row["id"],
                row["lease_token"],
            ),
        )

    def _expire_one_lease(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        max_attempts: int,
    ) -> None:
        """Settle one lease whose expiry has passed."""
        if row["result_reference"] is not None or row["result_sha256"] is not None:
            self._settle_published_expiry(connection, row, now)
            return
        self._retire_expired_lease(connection, row, now, max_attempts)

    def _expire_leases(
        self, connection: sqlite3.Connection, now: datetime, max_attempts: int
    ) -> int:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='leased' AND lease_expires_at <= ?",
            (timestamp(now),),
        ).fetchall()
        for row in rows:
            self._expire_one_lease(connection, row, now, max_attempts)
        return len(rows)


    _validate_source_identity = staticmethod(validate_source_identity)

    _validate_failure_identity = staticmethod(validate_failure_identity)

    _source_identity_from_payload = staticmethod(source_identity_from_payload)



    _payload_references_source = staticmethod(payload_references_source)


    source_fence_heartbeat = renewed_source_fence


    _require_no_source_failure = staticmethod(require_no_source_failure)


    def recover_expired_leases(self) -> int:
        now = as_utc(self._clock())
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
        states = validated_listed_states(states)
        placeholders = ",".join("?" for _ in states)
        parameters: list[object] = list(states)
        age_clause = self._age_clause(max_age_days, parameters)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM tasks WHERE state IN ({placeholders}){age_clause}
                    ORDER BY created_at, rowid""",  # noqa: S608 - generated placeholders
                parameters,
            ).fetchall()
            histories = attempt_histories(connection, rows)
        return [
            self._task_from_row(row, histories.get(str(row["id"]), []))
            for row in rows
        ]

    def _age_clause(
        self, max_age_days: int | None, parameters: list[object]
    ) -> str:
        """The age filter, appending its parameter when there is one."""
        return age_filter_clause(max_age_days, parameters, as_utc(self._clock()))

    def count_eligible(self, *, max_attempts: int = DEFAULTS.queue_max_attempts) -> int:
        validate_retry_policy(
            max_attempts, self._retry_base_seconds, self._retry_cap_seconds
        )
        now = as_utc(self._clock())
        with self._connect() as connection:
            register_source_reference(connection)
            row = connection.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {_V2_WORK_CLAIMABLE}",
                (max_attempts, timestamp(now)),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _task_from_row(row: sqlite3.Row, history: list[sqlite3.Row]) -> QueueTask:
        return task_record(row, history, json.loads(row["payload_json"]))


class QueueV3CandidateReader(QueueReaderFoundation, QueueTaskOperations, QueueCompileOperations, QueueSourceFenceOperations, QueueSemanticDecisionOperations, QueueCaptureOperations, QueueQuarantineOperations, QueuePurgeOperations):
    """The queue-v3 backend: an unpublished candidate, or the adopted database.

    It began as a claim seam for tests against a candidate. Adoption made it the
    only queue an adopted vault has, so it is the backend
    `active_or_legacy_memory_queue` hands every caller once adoption is in force.
    """


@contextmanager
def capture_task_fences(
    queue: QueueV3CandidateReader,
    coordinator: object,
    task_id: str,
    *,
    intent_id: str | None,
    mode: Literal["worker", "queue-operator"],
    owner: OwnerLease,
) -> Iterator[tuple[TaskFence, object | None]]:
    task_fence = queue.acquire_task_fence(task_id, mode=mode, owner=owner)
    intent_fence = None
    try:
        if intent_id is not None:
            intent_mode = "worker" if mode == "worker" else "operator"
            intent_fence = coordinator.acquire_intent_fence(
                intent_id, mode=intent_mode, owner=owner
            )
        yield task_fence, intent_fence
    finally:
        if intent_fence is not None:
            coordinator.release_intent_fence(intent_fence)
        queue.release_task_fence(task_fence)


# Columns added to the queue v2 tables after the original schema: the table,
# the column, the statement that adds it, and the backfill it needs, if any.
_QUEUE_V2_ADDED_COLUMNS = (
    ("tasks", "last_attempt_at", "ALTER TABLE tasks ADD COLUMN last_attempt_at TEXT", None),
    ("tasks", "result_sha256", "ALTER TABLE tasks ADD COLUMN result_sha256 TEXT", None),
    (
        "tasks",
        "redrive_of",
        "ALTER TABLE tasks ADD COLUMN redrive_of TEXT REFERENCES tasks(id)",
        None,
    ),
    (
        "source_fences",
        "heartbeat_at",
        "ALTER TABLE source_fences ADD COLUMN heartbeat_at TEXT",
        "UPDATE source_fences SET heartbeat_at=acquired_at WHERE heartbeat_at IS NULL",
    ),
    (
        "source_fences",
        "expires_at",
        "ALTER TABLE source_fences ADD COLUMN expires_at TEXT",
        "UPDATE source_fences SET expires_at=acquired_at WHERE expires_at IS NULL",
    ),
)


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """The column names a table currently has."""
    rows = connection.execute(f"PRAGMA table_info({quote_sqlite_identifier(table)})").fetchall()
    return {str(row["name"]) for row in rows}


def _run_optional_statement(connection: sqlite3.Connection, sql: str | None) -> None:
    """Run the statement if this column needs one."""
    if sql is None:
        return
    connection.execute(sql)


def _add_missing_queue_columns(connection: sqlite3.Connection) -> None:
    """Bring an older queue v2 schema up to the current column set."""
    present = {
        table: table_columns(connection, table)
        for table in ("tasks", "source_fences")
    }
    for table, column, add, backfill in _QUEUE_V2_ADDED_COLUMNS:
        if column in present[table]:
            continue
        connection.execute(add)
        _run_optional_statement(connection, backfill)


def default_queue(
    *,
    max_attempts: int = DEFAULTS.queue_max_attempts,
    retry_base_seconds: int = DEFAULTS.retry_base_seconds,
    retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
) -> MemoryQueue | QueueV3CandidateReader:
    return active_or_legacy_memory_queue(
        runtime_roots.vault_root(),
        runtime_roots.state_root(),
        policy=QueueRetryPolicy(
            max_attempts, retry_base_seconds, retry_cap_seconds
        ),
    )


def v3_queue_for_cli() -> QueueV3CandidateReader:
    """The v3 queue the repair commands work on: adopted, else the candidate."""
    from markdown_transaction import reliability_v3_records_present

    state_root = runtime_roots.state_root()
    if reliability_v3_records_present(state_root):
        return active_memory_queue(runtime_roots.vault_root(), state_root)
    return MemoryQueue.from_v3_candidate(
        state_root / "run" / "queue-v3.candidate.sqlite3",
        state_root=state_root, vault=runtime_roots.vault_root(),
    )


def active_memory_queue(vault: Path, state_root: Path, *, deadline: float | None = None) -> QueueV3CandidateReader:
    """Open the queue side of an adopted pair with verified files and contracts.

    Normal admission checks adoption evidence, identities and schema; complete
    retained-history certification belongs to adoption, backup and doctor.
    """
    from llm_wiki.adoption_admission import (
        read_adoption_record,
    )
    from markdown_transaction import require_adopted_once

    resolved_vault = Path(vault).resolve(strict=True)
    state = Path(state_root).absolute()
    require_adopted_once(resolved_vault, state, deadline=deadline)
    queue_path = state / "run" / "queue-v3.sqlite3"
    coordinator_path = state / "run" / "markdown-transactions-v3.sqlite3"
    adoption = read_adoption_record(state / "run/reliability-v3-adopted.json", state)
    contract = _adopted_queue_contract(adoption)
    require_queue_v3_openable(
        queue_path, state_root=state, deadline=deadline, expected_contract=contract,
    )
    return QueueV3CandidateReader(
        queue_path, coordinator_path=coordinator_path, vault=resolved_vault,
        expected_contract=contract,
    )


def _adopted_queue_contract(adoption: dict) -> OperationalDatabaseContract:
    if adoption["schema_version"] in {"reliability-v3-adoption/v2", "reliability-v3-adoption/v3"}:
        return QUEUE_COMPILE_CONTRACT
    return QUEUE_V3_CONTRACT


def _json_queue_entries(state_root: Path, *, deadline: float | None = None) -> int:
    """Entries under `run/queue/`, the file-per-task queue of v3.3.0–v3.4.0.

    An unreadable directory or a symlink counts as one: unknown is not empty.
    """
    directory = Path(state_root) / "run" / "queue"
    if directory.is_symlink():
        return 1
    if not directory.is_dir():
        return 0
    try:
        with os.scandir(directory) as entries:
            return sum(_deadline_queue_entry(deadline) for _ in entries)
    except OSError:
        return 1


def _deadline_queue_entry(deadline: float | None) -> int:
    operational_deadline_active(deadline)
    return 1


def _require_no_json_queue(state_root: Path, *, deadline: float | None = None) -> None:
    """Records of the JSON queue are refused, never imported or dropped.

    No release since v4.0.0 wrote that queue and, since 2026-09-23, none reads
    it; the installer's adoption refuses on the same directory. See
    `docs/research/2026-09-23-the-json-queue-import-goes.md`.
    """
    operational_deadline_active(deadline)
    if _json_queue_entries(state_root, deadline=deadline):
        raise QueueOperationError("legacy_json_queue_unsupported")


def _legacy_memory_queue(
    state_root: Path, policy: QueueRetryPolicy | None, *, deadline: float | None = None
) -> MemoryQueue:
    """The pre-adoption queue, given the caller's bounds only if it named any."""
    _require_no_json_queue(state_root, deadline=deadline)
    if policy is None:
        return MemoryQueue(state_root, deadline=deadline)
    return MemoryQueue(
        state_root,
        max_attempts=policy.max_attempts,
        retry_base_seconds=policy.retry_base_seconds,
        retry_cap_seconds=policy.retry_cap_seconds,
        deadline=deadline,
    )


def active_or_legacy_memory_queue(
    vault: Path,
    state_root: Path,
    *,
    policy: QueueRetryPolicy | None = None,
    deadline: float | None = None,
) -> MemoryQueue | QueueV3CandidateReader:
    """The adopted V3 queue where adoption is in force, else the legacy one.

    Adoption replaces the pre-adoption `run/queue.sqlite3` with a JSON tombstone,
    so a reader that opens that path directly fails with `file is not a database`
    on every call. This is the queue's instance of the one rule
    `markdown_transaction.active_or_legacy_coordinator` states for the
    coordinator, and it reads the same evidence; a caller that constructs
    `MemoryQueue` itself bypasses adoption and dies on the tombstone.

    `policy` belongs to the legacy queue alone: the adopted backend settles
    retries by its published contract and takes `max_attempts` per call. Callers
    with no policy of their own pass none, and construct exactly as they did.
    """
    from markdown_transaction import reliability_v3_records_present

    operational_deadline_active(deadline)
    if reliability_v3_records_present(state_root):
        return active_memory_queue(vault, state_root, deadline=deadline)
    return _legacy_memory_queue(state_root, policy, deadline=deadline)


@contextmanager
def repair_owner_for_cli(queue: QueueV3CandidateReader) -> Iterator[OwnerLease]:
    """A repair owner in the registry that pairs with the queue being repaired."""
    registry = queue.ownership_registry()
    owner = registry.acquire("repair", scope=f"repair:cli:{os.getpid()}")
    try:
        yield owner
    finally:
        registry.release(owner)


def enqueue(task_type: str, payload: dict[str, Any]) -> str:
    """Compatibility facade that enqueues handler version 1."""
    return default_queue().enqueue(task_type, 1, payload)


def cancel(
    task_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    return default_queue().cancel(task_id, deadline=deadline, cancelled=cancelled)


def redrive(
    task_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> str:
    return default_queue().redrive(task_id, deadline=deadline, cancelled=cancelled)


def purge(
    *,
    terminal_before: datetime,
    export_path: Path,
    include_dead: bool = False,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> PurgeReceipt:
    return default_queue().purge(
        terminal_before=terminal_before,
        export_path=export_path,
        include_dead=include_dead,
        deadline=deadline,
        cancelled=cancelled,
    )


def restore(
    *,
    export_path: Path,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> RestoreReceipt:
    return default_queue().restore(
        export_path=export_path,
        deadline=deadline,
        cancelled=cancelled,
    )


def _require_policy_this_queue_takes(
    queue: MemoryQueue | QueueV3CandidateReader, policy: Mapping[str, int]
) -> None:
    """Refuse a retry policy the queue will not take, before any task is claimed.

    The adopted queue settles its own attempts, so a worker told otherwise would
    claim a task and only then find it cannot record the failure.
    """
    if isinstance(queue, MemoryQueue):
        return
    require_adopted_attempt_limit(policy["max_attempts"])
    require_adopted_retry_policy(
        policy["max_attempts"],
        policy["retry_base_seconds"],
        policy["retry_cap_seconds"],
    )


def run_worker(
    processor: Callable[[dict], bool | DeferredResult] | LeaseProcessor,
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
    ] = run_processor_child,
    cancelled: Callable[[], bool] | None = None,
    min_claim_seconds: float = 0,
) -> WorkerSummary:
    """Run a short-lived worker bounded by tasks, wall time, and idle time.

    No task is claimed with less than `min_claim_seconds` of the wall time left.
    LeaseProcessor opts in to canonical ownership arguments bound after claim.
    """
    if max_tasks < 0 or max_seconds < 0 or idle_seconds < 0:
        raise ValueError("worker limits must be non-negative")
    policy = {
        "lease_seconds": lease_seconds,
        "heartbeat_seconds": heartbeat_seconds,
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
        "retry_cap_seconds": retry_cap_seconds,
    }
    validate_worker_policy(
        lease_seconds,
        heartbeat_seconds,
        max_attempts,
        retry_base_seconds,
        retry_cap_seconds,
    )
    progress = WorkerProgress({"ok": 0, "failed": 0, "dead": 0, "skipped": 0})
    queue = default_queue(
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )
    _require_policy_this_queue_takes(queue, policy)
    drive_worker(
        queue,
        processor,
        processor_runner,
        progress,
        owner=f"worker-{os.getpid()}-{uuid.uuid4().hex}",
        deadline=monotonic() + max_seconds,
        max_tasks=max_tasks,
        idle_seconds=idle_seconds,
        monotonic=monotonic,
        sleep=sleep,
        cancelled=cancelled,
        policy=policy,
        min_claim_seconds=min_claim_seconds,
    )
    return WorkerSummary(
        progress.processed,
        progress.totals["ok"],
        progress.totals["failed"],
        progress.totals["dead"],
        progress.totals["skipped"],
        queue.count_eligible(max_attempts=max_attempts),
    )


def uses_compile_journal(queue):
    contract = getattr(queue, 'expected_contract', None)
    return getattr(contract, 'user_version', None) == compile_progress.QUEUE_SCHEMA_VERSION
