"""Failing, retrying, cancelling and redriving queue tasks."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from llm_wiki import queue_store
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.queue_attempts import failure_is_terminal, require_leased_row
from llm_wiki.queue_contracts import QueueFailure, QueueLease, QueueOperationError
from llm_wiki.queue_store import TERMINAL_STATES, require_active, require_lineage_budget, unblock_in
from llm_wiki.queue_timing import as_utc, retry_after_seconds, timestamp
from llm_wiki.queue_validation import require_error_code, validate_retry_policy
from llm_wiki.sqlite_transactions import begin_immediate


class MemoryQueueFailures:
    def fail(
        self,
        lease: QueueLease,
        failure: QueueFailure,
        *,
        max_attempts: int | None = None,
        retry_base_seconds: int | None = None,
        retry_cap_seconds: int | None = None,
    ) -> None:
        require_error_code(failure)
        attempt_limit, retry_base, retry_cap = self._retry_policy(
            max_attempts, retry_base_seconds, retry_cap_seconds
        )
        now = as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if failure.blocked_capability:
                self._block_leased_task(connection, lease, row, failure, now)
                return
            self._record_attempt(connection, row, now, "failed", failure.error_code)
            if failure_is_terminal(row, failure, attempt_limit):
                self._retire_failed_task(connection, lease, failure, now)
                return
            self._reschedule_failed_task(
                connection,
                lease,
                row,
                failure,
                now,
                retry_base=retry_base,
                retry_cap=retry_cap,
            )

    def _retry_policy(
        self,
        max_attempts: int | None,
        retry_base_seconds: int | None,
        retry_cap_seconds: int | None,
    ) -> tuple[int, int, int]:
        """This call's retry policy: the caller's numbers, or the queue's."""
        attempt_limit = self._max_attempts if max_attempts is None else max_attempts
        retry_base = (
            self._retry_base_seconds
            if retry_base_seconds is None
            else retry_base_seconds
        )
        retry_cap = (
            self._retry_cap_seconds
            if retry_cap_seconds is None
            else retry_cap_seconds
        )
        validate_retry_policy(attempt_limit, retry_base, retry_cap)
        return attempt_limit, retry_base, retry_cap

    def _block_leased_task(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        row: sqlite3.Row,
        failure: QueueFailure,
        now: datetime,
    ) -> None:
        """A missing capability blocks the task without spending an attempt."""
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
                timestamp(now),
                failure.error_code,
                failure.blocked_capability,
                lease.id,
            ),
        )

    def _retire_failed_task(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        failure: QueueFailure,
        now: datetime,
    ) -> None:
        self._finish_lease(
            connection,
            lease.id,
            now,
            "dead",
            failure.error_code,
            None,
            last_attempt_at=now,
        )

    def _reschedule_failed_task(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        row: sqlite3.Row,
        failure: QueueFailure,
        now: datetime,
        *,
        retry_base: int,
        retry_cap: int,
    ) -> None:
        """Put the task back, no sooner than the backoff and the Retry-After."""
        delay = self._retry_delay(
            int(row["attempts"]),
            retry_base_seconds=retry_base,
            retry_cap_seconds=retry_cap,
        )
        retry_after = retry_after_seconds(failure.retry_after, now)
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
                row["attempt_started_at"] or timestamp(now),
                timestamp(now),
                outcome,
                error_code,
            ),
        )

    def _settle_payload_failure(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        now: datetime,
        state: str,
        error_code: str | None,
    ) -> None:
        """Record or clear the source failure this lease outcome implies."""
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            return
        self._apply_payload_failure(
            connection, str(row["payload_json"]), now, state, error_code
        )

    def _apply_payload_failure(
        self,
        connection: sqlite3.Connection,
        payload_json: str,
        now: datetime,
        state: str,
        error_code: str | None,
    ) -> None:
        """Record or clear the source failure this settled state implies."""
        if error_code is not None and state in {"ready", "dead"}:
            self._record_payload_failure(
                connection, payload_json, error_code, "queue", now
            )
            return
        if state in {"succeeded", "cancelled"}:
            self._clear_payload_failure(connection, payload_json)

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
        self._settle_payload_failure(connection, task_id, now, state, error_code)
        connection.execute(
            """UPDATE tasks SET state=?, updated_at=?, available_at=COALESCE(?, available_at),
                   last_attempt_at=COALESCE(?, last_attempt_at),
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?,
                   blocked_capability=NULL WHERE id=?""",
            (
                state,
                timestamp(now),
                timestamp(available_at) if available_at else None,
                timestamp(last_attempt_at) if last_attempt_at else None,
                error_code,
                task_id,
            ),
        )

    @staticmethod
    def _require_lease(
        connection: sqlite3.Connection, lease: QueueLease, now: datetime
    ) -> sqlite3.Row:
        return require_leased_row(connection, lease, now)

    def cancel(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        require_active(deadline, cancelled)
        now = as_utc(self._clock())
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: require_active(deadline, cancelled),
        ):
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in TERMINAL_STATES:
                return False
            if row["state"] == "leased":
                self._record_attempt(connection, row, now, "cancelled", "cancelled")
            self._clear_payload_failure(connection, str(row["payload_json"]))
            require_active(deadline, cancelled)
            connection.execute(
                """UPDATE tasks SET state='cancelled', updated_at=?, lease_owner=NULL,
                       lease_token=NULL, lease_expires_at=NULL, lease_heartbeat_at=NULL,
                       attempt_started_at=NULL, error_code='cancelled' WHERE id=?""",
                (timestamp(now), task_id),
            )
            return True

    def unblock(self, task_id: str) -> None:
        """A blocked task goes back to ready; any other state is refused."""
        now = as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            unblock_in(connection, task_id, now)

    def redrive(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        require_active(deadline, cancelled)
        now = as_utc(self._clock())
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: require_active(deadline, cancelled),
        ):
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] != "dead":
                raise QueueOperationError("redrive_requires_dead")
            require_lineage_budget(connection, row)
            self._delete_stale_source_fences(connection)
            self._assert_payload_not_fenced(connection, str(row["payload_json"]))
            payload_bytes = str(row["payload_json"]).encode("utf-8")
            replacement = uuid.UUID(int=self._rng.getrandbits(128)).hex
            require_active(deadline, cancelled)
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
                    timestamp(now),
                    timestamp(now),
                    timestamp(now),
                    task_id,
                ),
            )
        return replacement

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
            queue_store.record_source_failure_row(
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
