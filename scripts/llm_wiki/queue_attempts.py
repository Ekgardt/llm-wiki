"""Queue attempts; callers own time, admission and transactions."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.queue_contracts import (
    MAX_RUNTIME_ATTEMPTS,
    LeaseFenceError,
    QueueFailure,
    QueueLease,
    QueueOperationError,
)
from llm_wiki.queue_timing import retry_available_at, timestamp
from llm_wiki.reliability_defaults import DEFAULTS

_PERMANENT_CODES = {"invalid_input", "unsupported_version"}


def attempt_histories(
    connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]
) -> dict[str, list[sqlite3.Row]]:
    """Every listed task's attempt history, read in one query."""
    histories: dict[str, list[sqlite3.Row]] = {}
    if not rows:
        return histories
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    for history in connection.execute(
        f"""SELECT * FROM attempt_history WHERE task_id IN ({placeholders})
            ORDER BY sequence""",  # noqa: S608 - generated placeholders
        ids,
    ):
        histories.setdefault(str(history["task_id"]), []).append(history)
    return histories

def record_failed_attempt(
    database: sqlite3.Connection,
    lease: QueueLease,
    row: sqlite3.Row,
    failure: QueueFailure,
    now: datetime,
) -> None:
    """Append the attempt this failure ends."""
    outcome = "blocked" if failure.blocked_capability else "failed"
    database.execute(
        """INSERT INTO attempt_history(
               task_id,attempt,started_at,finished_at,outcome,error_code
           ) VALUES (?,?,?,?,?,?)""",
        (
            lease.id,
            row["attempts"],
            row["attempt_started_at"] or timestamp(now),
            timestamp(now),
            outcome,
            failure.error_code,
        ),
    )

def failure_state(
    row: sqlite3.Row, failure: QueueFailure, attempt_limit: int
) -> tuple[str, int]:
    """Where a failed task goes, and what its attempt count becomes.

    A task that has used every attempt it is allowed is dead, not ready. This
    path used to check only whether the failure was permanent, so an ordinary
    failure on the last attempt left the task `ready` with `attempts` at the
    limit — which the worker will not claim, because the budget is spent, and
    `redrive` will not take, because it requires a dead task. Twenty-three
    session classifications sat in that gap on this vault, each one eight
    failed attempts old and reachable by nothing. The lease-expiry path had
    the rule from the start; this one did not.
    """
    attempts = int(row["attempts"])
    if failure.blocked_capability:
        return "blocked", attempts - 1
    if failure_is_terminal(row, failure, attempt_limit):
        return "dead", attempts
    return "ready", attempts

def apply_failure_state(
    database: sqlite3.Connection,
    lease: QueueLease,
    row: sqlite3.Row,
    failure: QueueFailure,
    now: datetime,
    attempt_limit: int = DEFAULTS.queue_max_attempts,
) -> None:
    """Move the task out of its lease, under the lease's own fence."""
    state, attempts = failure_state(row, failure, attempt_limit)
    available_at = retry_available_at(state, attempts, now, failure.retry_after)
    changed = database.execute(
        """UPDATE tasks SET state=?,attempts=?,error_code=?,
               blocked_capability=?,updated_at=?,available_at=?,
               lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
               lease_heartbeat_at=NULL,attempt_started_at=NULL
           WHERE id=? AND lease_token=? AND state='leased'""",
        (
            state,
            attempts,
            failure.error_code,
            failure.blocked_capability,
            timestamp(now),
            timestamp(available_at),
            lease.id,
            lease.token,
        ),
    ).rowcount
    if changed != 1:
        raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

def failure_is_terminal(
    row: sqlite3.Row, failure: QueueFailure, attempt_limit: int
) -> bool:
    """This failure ends the task: it is permanent, or attempts ran out."""
    if failure.permanent or failure.error_code in _PERMANENT_CODES:
        return True
    return int(row["attempts"]) >= attempt_limit

def retire_exhausted_history(
    database: sqlite3.Connection, row: sqlite3.Row, now: datetime
) -> bool:
    """Retire a task whose attempt history is full; True when it was retired."""
    history_count = database.execute(
        "SELECT COUNT(*) FROM attempt_history WHERE task_id=?",
        (row["id"],),
    ).fetchone()[0]
    if int(history_count) < MAX_RUNTIME_ATTEMPTS:
        return False
    changed = database.execute(
        """UPDATE tasks SET state='dead',
               error_code='attempt_history_exhausted',updated_at=?
           WHERE id=? AND state='ready'""",
        (timestamp(now), row["id"]),
    ).rowcount
    if changed != 1:
        raise QueueOperationError("attempt_history_demotion_failed")
    return True

def retire_exhausted_ready(database: sqlite3.Connection, now: datetime) -> None:
    """A ready task no claim can take any more is dead, so a redrive can reach it."""
    database.execute(
        """UPDATE tasks SET state='dead',error_code='attempts_exhausted',updated_at=?
           WHERE state='ready' AND attempts>=?""",
        (timestamp(now), DEFAULTS.queue_max_attempts),
    )

def take_task_lease(
    database: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner: str,
    token: str,
    now: datetime,
    expires_at: datetime,
    max_attempts: int,
) -> bool:
    """Move the task into a lease; False when someone else got there first."""
    changed = database.execute(
        """UPDATE tasks SET state='leased', attempts=attempts+1,
               lease_owner=?, lease_token=?, lease_expires_at=?,
               lease_heartbeat_at=?, attempt_started_at=?, updated_at=?
           WHERE id=? AND state='ready' AND attempts < ?""",
        (
            owner,
            token,
            timestamp(expires_at),
            timestamp(now),
            timestamp(now),
            timestamp(now),
            row["id"],
            max_attempts,
        ),
    ).rowcount
    return changed == 1

def take_v3_task_lease(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner: str,
    token: str,
    now: datetime,
    expires: datetime,
    attempt_limit: int,
) -> bool:
    """Move the task into a lease; False when someone else got there first."""
    changed = connection.execute(
        """UPDATE tasks SET state='leased', attempts=attempts+1,
               lease_owner=?, lease_token=?, lease_expires_at=?, lease_heartbeat_at=?,
               attempt_started_at=?, updated_at=?, error_code=NULL,
               blocked_capability=NULL
           WHERE id=? AND state='ready' AND attempts < ?""",
        (
            owner,
            token,
            timestamp(expires),
            timestamp(now),
            timestamp(now),
            timestamp(now),
            row["id"],
            attempt_limit,
        ),
    ).rowcount
    return changed == 1

def record_cancelled_attempt(
    database: sqlite3.Connection, row: sqlite3.Row, now: datetime
) -> None:
    """The attempt_history row for a lease that an operator cancelled."""
    database.execute(
        """INSERT INTO attempt_history(
               task_id,attempt,started_at,finished_at,outcome,error_code
           ) VALUES (?,?,?,?,?,?)""",
        (
            row["id"],
            row["attempts"],
            row["attempt_started_at"] or timestamp(now),
            timestamp(now),
            "cancelled",
            "cancelled",
        ),
    )

def _lease_row_holds(
    row: sqlite3.Row | None, lease: QueueLease, now: datetime
) -> bool:
    """Whether this row is still the leased task this lease owns, unexpired."""
    if row is None or row["state"] != "leased":
        return False
    if (row["lease_owner"], row["lease_token"]) != (lease.owner, lease.token):
        return False
    return row["lease_expires_at"] > timestamp(now)

def require_leased_row(
    connection: sqlite3.Connection, lease: QueueLease, now: datetime
) -> sqlite3.Row:
    """The leased task row, or a fence refusal naming the lease."""
    row = connection.execute("SELECT * FROM tasks WHERE id=?", (lease.id,)).fetchone()
    if not _lease_row_holds(row, lease, now):
        raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
    return row

def require_lease_input(row: sqlite3.Row, lease: QueueLease) -> None:
    """Bind caller input to the stored intent, without redacting it a second time."""
    actual = [lease.kind, lease.handler_version, lease.input_hash]
    expected = [row["kind"], row["handler_version"], row["input_hash"]]
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise LeaseFenceError("lease input identity differs from stored task")
    if sha256_bytes(canonical_json_bytes(dict(lease.payload))) != row["input_hash"]:
        raise LeaseFenceError("lease input payload differs from stored task")

def record_acknowledged_attempt(
    database: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
    *,
    lease: QueueLease,
    outcome: str,
    error_code: str | None,
) -> None:
    """Close the attempt history row this acknowledgement ends."""
    database.execute(
        """INSERT INTO attempt_history(
               task_id,attempt,started_at,finished_at,outcome,error_code
           ) VALUES (?,?,?,?,?,?)""",
        (
            lease.id,
            row["attempts"],
            row["attempt_started_at"] or timestamp(now),
            timestamp(now),
            outcome,
            error_code,
        ),
    )
