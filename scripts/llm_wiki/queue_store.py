"""Queue helpers shared by the legacy queue and the adopted-queue operations."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

from llm_wiki.queue_contracts import STATES
from llm_wiki.queue_export_proof import (
    verified_export_records,
)
from llm_wiki.queue_validation import (
    require_claim_owner_and_lease,
    require_task_identifier,
    valid_heartbeat_interval,
    validate_retry_policy,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import (
    validate_payload_blob,
)
from llm_wiki.queue_contracts import (
    LeaseFenceError,
    QueueOperationError,
    RestoreReceipt,
    SourceFence,
)
from llm_wiki.queue_heartbeat import (
    SourceFenceHeartbeat,
)
from llm_wiki.queue_sources import (
    payload_identity_fields,
)
from llm_wiki.queue_timing import timestamp
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_deadlines import (
    configure_operational_deadline,
    refresh_operational_deadline,
)
from secret_redact import redact_structure

# A task fence's own length; a holder doing slow work renews it
# (`heartbeat_task_fence`).
TASK_FENCE_SECONDS = 120


TERMINAL_STATES = ("succeeded", "dead", "cancelled")


def require_active(
    deadline: float, cancelled: Callable[[], bool] | None = None
) -> None:
    if time.monotonic() >= deadline or bool(cancelled and cancelled()):
        raise TimeoutError("queue mutation deadline or cancellation reached")


def source_failure_commit_active(database, deadline):
    require_active(deadline)
    refresh_operational_deadline(database, DEFAULTS.queue_busy_ms, deadline)


@contextmanager
def source_failure_connection(queue, deadline):
    require_active(deadline)
    connection = queue._connect(deadline=deadline)
    with _queue_connection_context(connection) as database:
        with _source_failure_deadline(database, deadline):
            yield database


def _queue_connection_context(connection):
    if isinstance(connection, sqlite3.Connection):
        return closing(connection)
    return connection


@contextmanager
def _source_failure_deadline(database, deadline):
    require_active(deadline)
    if deadline == float("inf"):
        yield database
        return
    configure_operational_deadline(database, deadline)
    try:
        yield database
        require_active(deadline)
    except sqlite3.OperationalError:
        require_active(deadline)
        raise
    finally:
        database.set_progress_handler(None, 0)


@dataclass(frozen=True)
class TaskFence:
    task_id: str
    mode: Literal["worker", "queue-operator"]
    token: str
    epoch: int
    owner: OwnerLease
    expires_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def validated_payload_bytes(payload: Mapping[str, object]) -> tuple[bytes, str]:
    payload_bytes = canonical_json_bytes(redact_structure(dict(payload)))
    input_hash = sha256_bytes(payload_bytes)
    validation = validate_payload_blob(payload_bytes, input_hash, parse=True)
    if validation.code is not None:
        raise ValueError(validation.code)
    return payload_bytes, input_hash


def same_enqueued_task(
    existing: sqlite3.Row,
    existing_bytes: bytes,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    input_hash: str,
) -> bool:
    if existing["kind"] != kind or existing["handler_version"] != handler_version:
        return False
    if existing["input_hash"] != input_hash:
        return False
    return existing_bytes == payload_bytes


def v3_dedupe_match(
    existing: sqlite3.Row,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    input_hash: str,
) -> str:
    """The existing id when it is the same task; a dedupe conflict otherwise."""
    existing_bytes = bytes(existing["payload_blob"])
    current = validate_payload_blob(
        existing_bytes, existing["input_hash"], parse=True
    )
    if current.code is None and same_enqueued_task(
        existing, existing_bytes, kind, handler_version, payload_bytes, input_hash
    ):
        return str(existing["id"])
    raise QueueOperationError("dedupe_conflict")


def validated_listed_states(states: tuple[str, ...] | None) -> tuple[str, ...]:
    """The states to list; every one of them has to be a real queue state."""
    listed = states or STATES
    if not listed or any(state not in STATES for state in listed):
        raise ValueError("invalid queue state")
    return listed


def age_filter_clause(
    max_age_days: int | None, parameters: list[object], now: datetime
) -> str:
    """The age filter, appending its parameter when there is one."""
    if max_age_days is None:
        return ""
    parameters.append(timestamp(now - timedelta(days=max_age_days)))
    return " AND created_at >= ?"


def _refuse_legacy_only_api(name: str) -> NoReturn:
    """Refuse by name a legacy behaviour the adopted queue does not implement."""
    raise QueueOperationError(
        "queue_api_not_adopted",
        f"{name} has no implementation on the adopted V3 queue; it exists only on "
        "the pre-adoption queue, so it is refused here rather than ignored",
    )


def require_adopted_retry_policy(
    max_attempts: int, retry_base_seconds: int, retry_cap_seconds: int
) -> None:
    """The adopted queue settles retries by its own contract, not by caller policy."""
    offered = (max_attempts, retry_base_seconds, retry_cap_seconds)
    settled = (
        DEFAULTS.queue_max_attempts,
        DEFAULTS.retry_base_seconds,
        DEFAULTS.retry_cap_seconds,
    )
    if offered != settled:
        _refuse_legacy_only_api("fail(retry_policy_override)")


def require_adopted_attempt_limit(max_attempts: int) -> None:
    """An attempt limit belongs to the queue, so a caller's own number is refused.

    `claim` used to take the caller's number while `fail` refused it and the
    terminal test settled by the contract's: one option with two answers, and a
    worker that could claim a task it could not then fail. See
    `docs/research/2026-09-17-the-adopted-queue-settles-its-own-attempts-and-can-let-the-dead-go.md`.
    """
    if max_attempts != DEFAULTS.queue_max_attempts:
        _refuse_legacy_only_api("claim(max_attempts_override)")


def check_claim_arguments(
    owner: str, lease_seconds: int, max_attempts: int
) -> None:
    require_claim_owner_and_lease(owner, lease_seconds)
    validate_retry_policy(
        max_attempts, DEFAULTS.retry_base_seconds, DEFAULTS.retry_cap_seconds
    )
    require_adopted_attempt_limit(max_attempts)


def register_source_reference(connection: sqlite3.Connection) -> None:
    """Give this connection the queue's one source-reference rule as SQL.

    The claim and the eligible count select with SQL, and they matched a fence by
    `instr` over the payload text: a task that merely carried a timestamp of the
    fenced day was skipped and not counted, while the enqueue and the fence check
    already read identity fields only. They now call the same rule the enqueue
    does. See `docs/research/2026-09-28-a-check-names-its-cause.md`.
    """
    connection.create_function(
        "payload_references_source", 3, _stored_payload_references_source, deterministic=True
    )


def _stored_payload_references_source(
    payload: object, daily_id: object, source_digest: object
) -> bool:
    """`payload_references_source` for a stored payload and one fence."""
    return payload_references_source(
        _stored_payload_text(payload), str(daily_id), str(source_digest)
    )


def _stored_payload_text(payload: object) -> str:
    """A payload column as text; undecodable blob bytes are replaced (`task_payload_text`)."""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload)


# The task state and error code for an expired lease, keyed by whether the task
# has now used up every attempt it is allowed.
EXPIRED_LEASE_OUTCOMES = {
    True: ("dead", "attempts_exhausted"),
    False: ("ready", "lease_expired"),
}


# The queue tables whose rows keep the run directory alive.
RETAINING_QUEUE_QUERIES = (
    "SELECT 1 FROM tasks LIMIT 1",
    "SELECT 1 FROM source_fences LIMIT 1",
    "SELECT 1 FROM source_failures LIMIT 1",
)


def directory_holds_anything(path: Path) -> bool:
    """Whether the directory has an entry; one that cannot be read counts as held."""
    try:
        return path.exists() and any(path.iterdir())
    except OSError:
        return True


def daily_logical_path(daily_id: str) -> str:
    """The vault-relative path a canonical daily id names."""
    return f"knowledge/daily/{daily_id}.md"


def task_payload_text(row: sqlite3.Row) -> str:
    """The task payload as text for the source-reference check.

    Undecodable bytes are replaced, never dropped: the replacement makes the
    payload unreadable as JSON, and an unreadable payload counts as referencing
    every fenced source, which is the conservative answer at both call sites.
    """
    return _stored_payload_text(bytes(row["payload_blob"]))


# What the v3 `source_failures.error_code` CHECK stores: bytes, not characters.
MAX_SOURCE_FAILURE_CODE_BYTES = 64


def _within_stored_code_bytes(error_code: str) -> bool:
    return 1 <= len(error_code.encode("utf-8")) <= MAX_SOURCE_FAILURE_CODE_BYTES


def valid_source_failure_fields(error_code: object, producer: object) -> bool:
    """Whether a source failure's own fields are inside contract.

    The bound is the one the schema enforces — 1 to 64 bytes of UTF-8 — and not
    200 characters, which let a code through validation and broke the insert. See
    `docs/research/2026-09-18-a-refusal-is-cheaper-than-a-crash.md`.
    """
    if not isinstance(error_code, str) or not _within_stored_code_bytes(error_code):
        return False
    if any(char in error_code for char in "\r\n"):
        return False
    return producer in {"compile", "queue"}


# How many times one task may be redriven. Current practice bounds nothing here
# because a redrive is everywhere an operator's decision, and an operator does
# not redrive the same message forever. Nobody here is that operator, so the
# bound has to exist, and it has to be small: a task that died with its eight
# attempts spent is already saying the trouble is not bad luck. One more round
# of eight is sixteen; there is no third.
# See `docs/research/2026-09-07-how-many-second-chances-an-intent-gets.md`.
MAX_REDRIVE_GENERATIONS = 1


def unblock_in(database: sqlite3.Connection, task_id: str, now: datetime) -> None:
    """Put one blocked task back to ready: the operator says its capability is back.

    A task blocked on `process_cleanup` had no way back but `cancel`, which loses
    the work. See `docs/research/2026-09-17-a-failed-start-is-not-a-failed-cleanup.md`.
    """
    row = database.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    if row["state"] != "blocked":
        raise QueueOperationError("unblock_requires_blocked")
    database.execute(
        """UPDATE tasks SET state='ready',blocked_capability=NULL,error_code=NULL,
               updated_at=?,available_at=? WHERE id=? AND state='blocked'""",
        (timestamp(now), timestamp(now), task_id),
    )


def _redrive_depth(database: sqlite3.Connection, task_id: str) -> int:
    """How many redrives deep this task is, counted to the bound and no further."""
    row = database.execute(
        """WITH RECURSIVE lineage(id, depth) AS (
               SELECT redrive_of, 1 FROM tasks WHERE id=? AND redrive_of IS NOT NULL
               UNION ALL
               SELECT tasks.redrive_of, lineage.depth + 1 FROM tasks
               JOIN lineage ON tasks.id=lineage.id
               WHERE tasks.redrive_of IS NOT NULL AND lineage.depth < ?
           )
           SELECT COALESCE(MAX(depth), 0) FROM lineage""",
        (task_id, MAX_REDRIVE_GENERATIONS),
    ).fetchone()
    return int(row[0])


def require_lineage_budget(database: sqlite3.Connection, row: sqlite3.Row) -> None:
    """A copy does not reset the bound: the lineage, not the dead row, gets one redrive.

    See `docs/research/2026-09-14-one-second-chance-and-no-stuck-task.md`.
    """
    if _redrive_depth(database, str(row["id"])) >= MAX_REDRIVE_GENERATIONS:
        raise QueueOperationError("redrive_generations_exhausted")


def record_source_failure_row(
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
        (logical_path, source_digest, error_code, producer, timestamp(now)),
    )


def payload_references_source(
    payload_json: str, daily_id: str, source_digest: str
) -> bool:
    """A reference is an identity field of the payload, not a date in its text.

    `daily_id` is a date, and every timestamp this product writes starts with
    one, so the substring test this replaces fenced the whole queue while a
    day was being archived. See `docs/research/
    2026-09-18-a-source-is-referenced-by-a-field-not-by-a-date-in-the-text.md`.
    """
    found = payload_identity_fields(payload_json)
    if found is None:
        return True
    return (
        daily_id in found["daily_ids"]
        or source_digest in found["digests"]
        or daily_logical_path(daily_id) in found["paths"]
    )


@contextmanager
def renewed_source_fence(
    queue,
    fence: SourceFence,
    *,
    heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
    lease_seconds: int = DEFAULTS.queue_lease_seconds,
):
    if not valid_heartbeat_interval(heartbeat_seconds, lease_seconds):
        raise ValueError("source heartbeat interval must be shorter than its lease")
    heartbeat = SourceFenceHeartbeat(
        queue,
        fence,
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
    )
    heartbeat.start()
    try:
        yield heartbeat
    finally:
        heartbeat.stop()


def require_no_source_failure(
    connection: sqlite3.Connection, fence: SourceFence
) -> None:
    """A source that already failed must not be finalized."""
    logical_path = f"knowledge/daily/{fence.daily_id}.md"
    row = connection.execute(
        "SELECT 1 FROM source_failures "
        "WHERE logical_path=? AND source_digest=?",
        (logical_path, fence.source_digest),
    ).fetchone()
    if row is not None:
        raise QueueOperationError("source_failure")


# What `work` may claim from the adopted queue, and therefore what it counts as
# remaining: one predicate, so the count can never name a task the claim skips.
# The two were written apart and drifted twice — a ready capture task (the capture
# worker's) and a task under a source fence were both counted and never claimed,
# and the nightly's work step failed on `remaining_eligible` with nothing to do.
# Parameters: max_attempts, now. See docs/research/2026-09-28-a-check-names-its-cause.md.
# substr(..., 17, 10) is the daily id inside the canonical
# 'knowledge/daily/YYYY-MM-DD.md'; acquire_source_fence validates that form
# before any fence row exists, so the slice is always the date.
V3_WORK_CLAIMABLE = """state='ready' AND attempts < ? AND available_at <= ?
    AND NOT EXISTS (
        SELECT 1 FROM capture_task_links link WHERE link.task_id=tasks.id
    )
    AND NOT EXISTS (
        SELECT 1 FROM source_fences fence
        WHERE payload_references_source(
            tasks.payload_blob, substr(fence.logical_path, 17, 10), fence.source_digest
        )
    )"""


def claim_task_filter(task_id):
    if task_id is None:
        return '', ()
    require_task_identifier(task_id)
    return ' AND tasks.id=?', (task_id,)


def is_compile_recovery(row):
    return (row['state'] == 'leased' and row['kind'] == 'compile'
            and row['blocked_capability'] == 'provider_outcome_unknown')


def _require_compile_recovery(row):
    if not is_compile_recovery(row) or row['attempts'] < 1:
        raise QueueOperationError('compile_recovery_lease_required')


def settle_compile_recovery(database, row, now, *, state, outcome, code):
    _require_compile_recovery(row)
    database.execute(
        """INSERT INTO attempt_history(task_id,attempt,started_at,finished_at,outcome,error_code)
           VALUES (?,?,?,?,?,?)""",
        (row['id'], row['attempts'], row['attempt_started_at'], timestamp(now), outcome, code),
    )
    error, capability = {
        'ready': (None, None),
        'blocked': ('provider_outcome_unknown', 'provider_outcome_unknown'),
    }[state]
    changed = database.execute(
        """UPDATE tasks SET state=?,attempts=attempts-1,error_code=?,blocked_capability=?,
           updated_at=?,available_at=?,lease_owner=NULL,lease_token=NULL,
           lease_expires_at=NULL,lease_heartbeat_at=NULL,attempt_started_at=NULL
           WHERE id=? AND state='leased' AND lease_token=?""",
        (state, error, capability, timestamp(now), timestamp(now), row['id'], row['lease_token']),
    ).rowcount
    if changed != 1:
        raise LeaseFenceError('compile recovery settlement lost its lease')


SOURCE_FAILURE_KEYS = "SELECT logical_path, source_digest FROM source_failures ORDER BY logical_path, source_digest"


def _as_positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise QueueOperationError(
            "restore_record_invalid", f"{field} is not a positive integer"
        )
    return value


def _as_priority(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return max(-100, min(100, value))


def _restored_task_id(queue: object, record: Mapping[str, object]) -> str:
    """Re-enqueue one exported record onto `queue` as new ready work."""
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise QueueOperationError("restore_record_invalid", "payload is not an object")
    return queue.enqueue(  # type: ignore[attr-defined]
        str(record.get("kind") or ""),
        _as_positive_int(record.get("handler_version"), "handler_version"),
        payload,
        priority=_as_priority(record.get("priority")),
    )


def restore_from_export(
    queue: object,
    *,
    export_path: Path,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> RestoreReceipt:
    """Re-enqueue the work in one verified purge export onto `queue`."""
    require_active(deadline, cancelled)
    records = verified_export_records(Path(export_path).absolute())
    restored: list[tuple[str, str]] = []
    for record in records:
        require_active(deadline, cancelled)
        restored.append((str(record["id"]), _restored_task_id(queue, record)))
    return RestoreReceipt(len(restored), tuple(restored))
