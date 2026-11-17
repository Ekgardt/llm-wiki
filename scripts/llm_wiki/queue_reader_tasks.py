"""Adopted-queue task lifecycle: enqueue, claim, heartbeat, settle and redrive."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from llm_wiki.queue_artifacts import (
    harden_owner_only,
    read_stable_owner_file,
    write_durable_file,
)
from llm_wiki.queue_attempts import (
    apply_failure_state,
    attempt_histories,
    record_acknowledged_attempt,
    record_cancelled_attempt,
    record_failed_attempt,
)
from llm_wiki.queue_contracts import MAX_RESULT_BYTES
from llm_wiki.queue_task_lineage import (
    require_complete_compile_attestation,
)
from llm_wiki.queue_task_records import (
    task_record,
)
from llm_wiki.queue_validation import (
    check_result_operation,
    check_result_request,
    require_enqueue_arguments,
    require_positive_task_lease,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
import compile_progress
from llm_wiki import queue_store
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.queue_contracts import (
    LeaseFenceError,
    QueueFailure,
    QueueLease,
    QueueOperationError,
    QueueTask,
    RestoreReceipt,
    ResultConflictError,
)
from llm_wiki.queue_database_contract import (
    QUEUE_V3_CONTRACT,
)
from llm_wiki.queue_sources import (
    normalized_source_links,
)
from llm_wiki.queue_store import (
    RETAINING_QUEUE_QUERIES,
    TERMINAL_STATES,
    V3_WORK_CLAIMABLE,
    age_filter_clause,
    check_claim_arguments,
    claim_task_filter,
    directory_holds_anything,
    register_source_reference,
    require_active,
    require_adopted_retry_policy,
    restore_from_export,
    unblock_in,
    v3_dedupe_match,
    validated_listed_states,
    validated_payload_bytes,
)
from llm_wiki.queue_timing import (
    as_utc,
    timestamp,
)
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_contracts import (
    OperationalDatabaseContract,
)
from llm_wiki.sqlite_transactions import begin_immediate


def _v3_task_record(
    row: sqlite3.Row, history: Sequence[sqlite3.Row]
) -> QueueTask:
    """One adopted-queue task record; v3 keeps the payload in a blob column."""
    return task_record(row, history, json.loads(bytes(row["payload_blob"])))


# state, error_code, and attempt outcome for an acknowledgement, by whether the
# published result was still intact when the lease was acknowledged.
_ACKNOWLEDGED_OUTCOMES = {
    True: ("succeeded", None, "succeeded"),
    False: ("dead", "result_corrupt", "failed"),
}


class QueueTaskOperations:
    def __init__(self, path: Path, *, coordinator_path: Path | None = None, vault: Path | None = None,
                 expected_contract: OperationalDatabaseContract = QUEUE_V3_CONTRACT) -> None:
        self.expected_contract = expected_contract
        self.db_path = Path(path)
        self.state_root = self.db_path.parent.parent
        self.results_dir = self.state_root / "run" / "queue-results"
        self.coordinator_path = coordinator_path
        self.vault = None if vault is None else Path(vault).resolve(strict=True)

    def _heartbeat_wait(self, stop: threading.Event, interval: float) -> bool:
        """The heartbeat thread's wait; the adopted backend injects no seam."""
        return stop.wait(interval)

    @property
    def run_dir(self) -> Path:
        """Where this queue's runtime files live, named as the legacy queue names it."""
        return self.state_root / "run"

    def get(self, task_id: str) -> QueueTask:
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            history = database.execute(
                "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return _v3_task_record(row, history)

    def list_tasks(
        self, *, states: tuple[str, ...] | None = None, max_age_days: int | None = None
    ) -> list[QueueTask]:
        states = validated_listed_states(states)
        placeholders = ",".join("?" for _ in states)
        parameters: list[object] = list(states)
        age_clause = age_filter_clause(max_age_days, parameters, queue_store.utc_now())
        with closing(self._connect()) as database:
            rows = database.execute(
                f"""SELECT * FROM tasks WHERE state IN ({placeholders}){age_clause}
                    ORDER BY created_at, rowid""",  # noqa: S608 - generated placeholders
                parameters,
            ).fetchall()
            histories = attempt_histories(database, rows)
        return [
            _v3_task_record(row, histories.get(str(row["id"]), [])) for row in rows
        ]

    def count_eligible(self, *, max_attempts: int = DEFAULTS.queue_max_attempts) -> int:
        """Ready tasks `work` could claim, by the claim's own predicate.

        A capture task is the capture worker's (`claim_capture`) and a fenced source
        waits for its fence; `work` claims neither, so neither is counted here.
        """
        with closing(self._connect()) as database:
            register_source_reference(database)
            row = database.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {V3_WORK_CLAIMABLE}",
                (max_attempts, timestamp(queue_store.utc_now())),
            ).fetchone()
        return int(row[0])

    def retains_run_directory(self) -> bool:
        """Whether queue rows, quarantine or results still hold `run/` open."""
        with closing(self._connect()) as database:
            held = any(
                database.execute(query).fetchone() is not None
                for query in RETAINING_QUEUE_QUERIES
            )
        if held or directory_holds_anything(self.run_dir / "queue-quarantine"):
            return True
        return directory_holds_anything(self.results_dir)

    def restore(
        self,
        *,
        export_path: Path,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> RestoreReceipt:
        """Re-enqueue the work in one verified purge export."""
        return restore_from_export(
            self, export_path=export_path, deadline=deadline, cancelled=cancelled
        )

    def _deduplicated_task_id(
        self,
        database: sqlite3.Connection,
        dedupe_key: str | None,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
    ) -> str | None:
        """The id of an identical task already enqueued under this dedupe key."""
        if dedupe_key is None:
            return None
        existing = database.execute(
            """SELECT id, kind, handler_version, payload_blob, input_hash
               FROM tasks WHERE dedupe_key=?""",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            return None
        return v3_dedupe_match(
            existing, kind, handler_version, payload_bytes, input_hash
        )

    def _insert_source_links(
        self,
        database: sqlite3.Connection,
        task_id: str,
        normalized_links: list[tuple[str, str]],
    ) -> None:
        for logical_path, source_digest in sorted(normalized_links):
            linked = database.execute(
                """INSERT INTO task_source_links(
                       task_id,logical_path,source_digest
                   ) VALUES (?,?,?)""",
                (task_id, logical_path, source_digest),
            ).rowcount
            if linked != 1:
                raise QueueOperationError("source_link_insert_failed")

    def enqueue(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
        source_links: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    ) -> str:
        require_enqueue_arguments(kind, handler_version, priority, dedupe_key)
        normalized_links = normalized_source_links(source_links)
        payload_bytes, input_hash = validated_payload_bytes(payload)
        now = queue_store.utc_now()
        ready_at = as_utc(available_at or now)
        task_id = uuid.uuid4().hex
        with closing(self._connect()) as database, begin_immediate(database):
            self._delete_stale_source_fences(database)
            self._assert_payload_not_fenced(database, payload_bytes.decode("utf-8"))
            deduplicated = self._deduplicated_task_id(
                database, dedupe_key, kind, handler_version, payload_bytes, input_hash
            )
            if deduplicated is not None:
                return deduplicated
            self._insert_task_row(
                database,
                task_id,
                kind,
                handler_version,
                payload_bytes,
                input_hash,
                dedupe_key,
                priority,
                now,
                ready_at,
            )
            self._insert_source_links(database, task_id, normalized_links)
        return task_id

    def heartbeat_queue_owner(self, lease: OwnerLease) -> OwnerLease:
        registry = self.ownership_registry()
        current = registry.heartbeat(lease)
        with closing(
            self._connect()
        ) as database, begin_immediate(database):
            updated = database.execute(
                """UPDATE queue_ownership SET heartbeat_at=?,expires_at=?
                   WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                     AND owner_token=? AND fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    timestamp(current.heartbeat_at),
                    timestamp(current.expires_at),
                    current.actor_id,
                    current.role,
                    current.scope,
                    current.token,
                    current.epoch,
                    current.process.pid,
                    current.process.start_identity,
                ),
            ).rowcount
            if updated != 1:
                raise QueueOperationError("queue_owner_fence_lost")
        return current

    def claim(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        task_id: str | None = None,
    ) -> QueueLease | None:
        check_claim_arguments(owner, lease_seconds, max_attempts)
        task_filter = claim_task_filter(task_id)
        with closing(self._connect()) as database, begin_immediate(database):
            return self._claim_in(database, owner=owner, lease_seconds=lease_seconds,
                                  max_attempts=max_attempts, task_filter=task_filter)

    def heartbeat(
        self,
        lease: QueueLease,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> QueueLease:
        require_positive_task_lease(lease_seconds)
        expires_at = self._with_verified_lease(
            lease, partial(self._extend_leased_task, lease=lease, lease_seconds=lease_seconds)
        )
        return replace(lease, expires_at=expires_at)

    @staticmethod
    def _extend_leased_task(
        database: sqlite3.Connection,
        _row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
        lease_seconds: int,
    ) -> datetime:
        """Move the lease's expiry on; a lease that is no longer ours raises."""
        expires_at = now + timedelta(seconds=lease_seconds)
        changed = database.execute(
            """UPDATE tasks SET lease_expires_at=?, lease_heartbeat_at=?,
                   updated_at=? WHERE id=? AND lease_token=? AND state='leased'""",
            (
                timestamp(expires_at),
                timestamp(now),
                timestamp(now),
                lease.id,
                lease.token,
            ),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        return expires_at

    def _validated_result_digest(self, relative: str) -> str | None:
        try:
            path = self.state_root / relative
            if path.parent.resolve(strict=True) != self.results_dir.resolve(strict=True):
                return None
            data = read_stable_owner_file(path, MAX_RESULT_BYTES)
            return sha256_bytes(data)
        except (OSError, PermissionError, ValueError):
            return None

    def _bury_corrupt_result(
        self, database: sqlite3.Connection, now: datetime, *, lease: QueueLease
    ) -> None:
        """Kill the task whose published result no longer verifies."""
        database.execute(
            """UPDATE tasks SET state='dead',error_code='result_corrupt',
                   updated_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL,lease_heartbeat_at=NULL,
                   attempt_started_at=NULL WHERE id=?""",
            (timestamp(now), lease.id),
        )

    def _rebind_adopted_result(
        self,
        database: sqlite3.Connection,
        now: datetime,
        *,
        lease: QueueLease,
        relative: str,
        digest: str,
        operation_id: str,
    ) -> None:
        """Point the leased task at the result it is adopting."""
        changed = database.execute(
            """UPDATE tasks SET result_reference=?,result_sha256=?,
                   result_operation_id=?,updated_at=?
               WHERE id=? AND lease_token=? AND state='leased'""",
            (relative, digest, operation_id, timestamp(now), lease.id, lease.token),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def _adopt_result_row(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
        relative: str,
        operation_id: str,
    ) -> str:
        """Adopt the published result, or bury the task if it no longer verifies."""
        digest = self._validated_result_digest(relative)
        if digest is None:
            self._bury_corrupt_result(database, now, lease=lease)
            return "corrupt"
        self._rebind_adopted_result(
            database,
            now,
            lease=lease,
            relative=relative,
            digest=digest,
            operation_id=operation_id,
        )
        return "adopted"

    def adopt_published_result(
        self, lease: QueueLease, *, operation_id: str
    ) -> str | None:
        if not operation_id:
            raise ValueError("operation_id must be non-empty")
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        if not (self.state_root / relative).exists():
            return None
        outcome = self._with_verified_lease(
            lease,
            partial(
                self._adopt_result_row,
                lease=lease,
                relative=relative,
                operation_id=operation_id,
            ),
        )
        return str(outcome) if outcome is not None else None

    def _store_result_bytes(self, operation_id: str, result: bytes) -> tuple[str, str]:
        """Write the result file; answer its vault-relative path and digest."""
        self.results_dir.mkdir(parents=True, exist_ok=True)
        harden_owner_only(self.results_dir, 0o700)
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        write_durable_file(self.results_dir / result_name, result)
        return f"run/queue-results/{result_name}", sha256_bytes(result)

    def _require_publication_is_new(
        self, row: sqlite3.Row, operation_id: str, relative: str, digest: str
    ) -> None:
        """Refuse a publication that contradicts one this lease already made."""
        check_result_operation(row, operation_id)
        if self._validated_result_digest(relative) != digest:
            raise ResultConflictError("operation ID already has different result bytes")

    def _bind_published_result(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
        operation_id: str,
        relative: str,
        digest: str,
    ) -> None:
        """Point the leased task at the result bytes already written."""
        self._require_compile_completion(database, row, now)
        self._require_publication_is_new(row, operation_id, relative, digest)
        changed = database.execute(
            """UPDATE tasks SET result_reference=?,result_sha256=?,
                   result_operation_id=?,updated_at=?
               WHERE id=? AND lease_token=? AND state='leased'""",
            (relative, digest, operation_id, timestamp(now), lease.id, lease.token),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def publish_result(
        self, lease: QueueLease, *, operation_id: str, result: bytes
    ) -> str:
        check_result_request(operation_id, result)
        self._with_verified_lease(lease, self._require_compile_completion)
        relative, digest = self._store_result_bytes(operation_id, result)
        self._with_verified_lease(
            lease,
            partial(
                self._bind_published_result,
                lease=lease,
                operation_id=operation_id,
                relative=relative,
                digest=digest,
            ),
        )
        return relative

    def _require_compile_completion(self, database, row, now):
        if row['kind'] != 'compile' or self.expected_contract.user_version != compile_progress.QUEUE_SCHEMA_VERSION:
            return
        require_complete_compile_attestation(database, row)

    def _result_is_intact(self, row: sqlite3.Row) -> bool:
        """Whether the task's recorded result still hashes to what it claims."""
        digest = row["result_sha256"]
        reference = row["result_reference"]
        if not isinstance(digest, str) or not isinstance(reference, str):
            return False
        return self._validated_result_digest(reference) == digest

    def _settle_acknowledged(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
    ) -> None:
        """Close the leased attempt, recording whether its result survived."""
        self._require_compile_completion(database, row, now)
        state, error_code, outcome = _ACKNOWLEDGED_OUTCOMES[self._result_is_intact(row)]
        record_acknowledged_attempt(
            database, row, now, lease=lease, outcome=outcome, error_code=error_code
        )
        changed = database.execute(
            """UPDATE tasks SET state=?,error_code=?,updated_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   lease_heartbeat_at=NULL,attempt_started_at=NULL
               WHERE id=? AND lease_token=? AND state='leased'""",
            (state, error_code, timestamp(now), lease.id, lease.token),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def acknowledge(self, lease: QueueLease) -> QueueTask:
        """Settle the lease and return the task, as the legacy queue does.

        The worker counts a terminal task by reading the record back, so a
        backend that returns nothing here leaves it counting `None`.
        """
        self._with_verified_lease(lease, partial(self._settle_acknowledged, lease=lease))
        return self.get(lease.id)

    def fail(
        self,
        lease: QueueLease,
        failure: QueueFailure,
        *,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        retry_base_seconds: int = DEFAULTS.retry_base_seconds,
        retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
    ) -> None:
        require_adopted_retry_policy(
            max_attempts, retry_base_seconds, retry_cap_seconds
        )
        if not isinstance(failure, QueueFailure) or not failure.error_code:
            raise ValueError("failure must have a non-empty error code")
        self._with_verified_lease(
            lease,
            partial(
                self._fail_leased_task,
                lease=lease,
                failure=failure,
                max_attempts=max_attempts,
            ),
        )

    @staticmethod
    def _fail_leased_task(
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
        failure: QueueFailure,
        max_attempts: int,
    ) -> None:
        record_failed_attempt(database, lease, row, failure, now)
        apply_failure_state(database, lease, row, failure, now, max_attempts)

    def recover_expired_leases(self) -> int:
        now = queue_store.utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            return self._recover_expired_in(database, now)

    def cancel(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        require_active(deadline, cancelled)
        now = queue_store.utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in TERMINAL_STATES:
                return False
            changed = self._cancel_valid_row(database, row, now)
        if changed is None:
            self._raise_payload_mismatch()
        return bool(changed)

    def _cancel_valid_row(
        self, database: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> bool | None:
        """Cancel a row whose payload holds; None when the payload was demoted instead."""
        if self._require_valid_task_payload(database, row, now=now, parse=True) is None:
            return None
        return self._cancel_row(database, row, now)

    @staticmethod
    def _cancel_row(
        database: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> bool:
        """An attempt cancelled in flight is recorded, as the legacy queue records it."""
        if row["state"] == "leased":
            record_cancelled_attempt(database, row, now)
        return database.execute(
            """UPDATE tasks SET state='cancelled',error_code='cancelled',
                   updated_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL,lease_heartbeat_at=NULL,
                   attempt_started_at=NULL WHERE id=? AND state=?""",
            (timestamp(now), row["id"], row["state"]),
        ).rowcount == 1

    def unblock(self, task_id: str) -> None:
        """A blocked task goes back to ready; any other state is refused."""
        with closing(self._connect()) as database, begin_immediate(database):
            unblock_in(database, task_id, queue_store.utc_now())
