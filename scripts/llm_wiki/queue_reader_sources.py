"""Adopted-queue source fences and source failures."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

from llm_wiki.queue_task_lineage import (
    decision_seal_of,
    redrive_ancestors,
)
from llm_wiki.queue_validation import (
    require_known_states,
    require_positive_lease,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from llm_wiki import queue_store, sqlite_transactions
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.queue_contracts import (
    QueueOperationError,
    SourceFence,
)
from llm_wiki.queue_sources import (
    validate_failure_identity,
    validate_source_identity,
)
from llm_wiki.queue_store import (
    SOURCE_FAILURE_KEYS,
    TASK_FENCE_SECONDS,
    TaskFence,
    daily_logical_path,
    payload_references_source,
    record_source_failure_row,
    renewed_source_fence,
    require_no_source_failure,
    task_payload_text,
    valid_source_failure_fields,
)
from llm_wiki.queue_timing import parse_timestamp, timestamp
from llm_wiki.reliability_defaults import DEFAULTS


def _sealed_binding(seal: sqlite3.Row, intent_id: str) -> dict[str, str]:
    """The capture binding a Markdown transaction records for this seal."""
    return {
        "intent_id": intent_id,
        "task_id": str(seal["task_id"]),
        "active_link_digest": str(seal["active_digest"]),
        "seal_digest": str(seal["seal_digest"]),
    }


def _adopted_fence_row_matches(row: sqlite3.Row, fence: SourceFence) -> bool:
    """The stored adopted fence names the same source, token and owning process."""
    stored = (
        str(row["logical_path"]),
        str(row["source_digest"]),
        str(row["token"]),
        int(row["owner_pid"]),
        str(row["acquired_at"]),
    )
    expected = (
        daily_logical_path(fence.daily_id),
        fence.source_digest,
        fence.token,
        os.getpid(),
        timestamp(fence.acquired_at),
    )
    return stored == expected and fence.owner_pid == os.getpid()


def insert_adopted_source_fence(
    database: sqlite3.Connection, values: tuple[object, ...]
) -> None:
    """Record the fence, refusing when another one already holds the source."""
    try:
        database.execute(
            "INSERT INTO source_fences "
            "(logical_path, source_digest, token, owner_pid, owner_start_identity, "
            "acquired_at, heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    except sqlite3.IntegrityError as exc:
        raise QueueOperationError("source_fenced") from exc


class QueueSourceFenceOperations:
    def record_source_failure(
        self,
        logical_path: str,
        source_digest: str,
        *,
        error_code: str,
        producer: str,
        deadline: float = float("inf"),
    ) -> None:
        validate_failure_identity(logical_path, source_digest)
        if not valid_source_failure_fields(error_code, producer):
            raise ValueError("source failure fields are invalid")
        with queue_store.source_failure_connection(self, deadline) as database, sqlite_transactions.begin_immediate(database):
            record_source_failure_row(
                database,
                logical_path,
                source_digest,
                error_code,
                producer,
                queue_store.utc_now(),
            )
            queue_store.source_failure_commit_active(database, deadline)

    def clear_source_failure(self, logical_path: str, source_digest: str, *, deadline: float = float("inf")) -> None:
        validate_failure_identity(logical_path, source_digest)
        with queue_store.source_failure_connection(self, deadline) as database, sqlite_transactions.begin_immediate(database):
            database.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            )
            queue_store.source_failure_commit_active(database, deadline)

    def source_failure_keys(self, *, deadline: float = float("inf")) -> list[tuple[str, str]]:
        """Every recorded `(logical_path, source_digest)`; see `MemoryQueue.source_failure_keys`."""
        with queue_store.source_failure_connection(self, deadline) as database:
            rows = database.execute(SOURCE_FAILURE_KEYS).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    def source_failure(
        self, logical_path: str, source_digest: str
    ) -> dict[str, str] | None:
        validate_failure_identity(logical_path, source_digest)
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT logical_path, source_digest, error_code, producer "
                "FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            ).fetchone()
        return None if row is None else {key: str(row[key]) for key in row.keys()}

    def referencing_source_tasks(
        self,
        daily_id: str,
        source_digest: str,
        *,
        states: tuple[str, ...] = ("ready", "leased", "blocked", "dead"),
    ) -> tuple[str, ...]:
        validate_source_identity(daily_id, source_digest)
        require_known_states(states)
        placeholders = ",".join("?" for _ in states)
        with closing(self._connect()) as database:
            cursor = database.execute(
                f"SELECT id, payload_blob FROM tasks WHERE state IN ({placeholders}) "
                "ORDER BY created_at, rowid",  # noqa: S608 - generated placeholders
                states,
            )
            return tuple(
                str(row["id"])
                for row in cursor
                if payload_references_source(
                    task_payload_text(row), daily_id, source_digest
                )
            )

    def _require_source_unreferenced(
        self, database: sqlite3.Connection, daily_id: str, source_digest: str
    ) -> None:
        """Refuse a fence over a source that some live task still points at."""
        rows = database.execute(
            "SELECT payload_blob FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        )
        if any(
            payload_references_source(
                task_payload_text(row), daily_id, source_digest
            )
            for row in rows
        ):
            raise QueueOperationError("source_referenced")

    def acquire_source_fence(
        self,
        daily_id: str,
        source_digest: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        from operational_ownership import current_process_identity

        validate_source_identity(daily_id, source_digest)
        require_positive_lease(lease_seconds)
        process = current_process_identity()
        token = sha256_bytes(os.urandom(32))
        acquired = queue_store.utc_now()
        expires = acquired + timedelta(seconds=lease_seconds)
        with closing(self._connect()) as database, sqlite_transactions.begin_immediate(database):
            self._delete_stale_source_fences(database)
            self._require_source_unreferenced(database, daily_id, source_digest)
            insert_adopted_source_fence(
                database,
                (
                    daily_logical_path(daily_id),
                    source_digest,
                    token,
                    process.pid,
                    process.start_identity,
                    timestamp(acquired),
                    timestamp(acquired),
                    timestamp(expires),
                ),
            )
        return SourceFence(
            daily_id,
            source_digest,
            token,
            process.pid,
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
        require_positive_lease(lease_seconds)
        now = queue_store.utc_now()
        expires = now + timedelta(seconds=lease_seconds)
        with closing(self._connect()) as database, sqlite_transactions.begin_immediate(database):
            self._delete_stale_source_fences(database)
            changed = database.execute(
                """UPDATE source_fences SET heartbeat_at=?, expires_at=?
                   WHERE logical_path=? AND source_digest=? AND token=?
                     AND owner_pid=? AND acquired_at=? AND heartbeat_at=?
                     AND expires_at=?""",
                (
                    timestamp(now),
                    timestamp(expires),
                    daily_logical_path(fence.daily_id),
                    fence.source_digest,
                    fence.token,
                    fence.owner_pid,
                    timestamp(fence.acquired_at),
                    timestamp(fence.heartbeat_at),
                    timestamp(fence.expires_at),
                ),
            ).rowcount
            if changed != 1 or now >= fence.expires_at:
                raise QueueOperationError("source_fence_lost")
        return replace(fence, heartbeat_at=now, expires_at=expires)

    def source_fence_heartbeat(
        self,
        fence: SourceFence,
        *,
        heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ):
        """A held fence kept renewed by the same heartbeat the legacy queue runs."""
        return renewed_source_fence(
            self,
            fence,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
        )

    @contextmanager
    def source_finalization(self, fence: SourceFence):
        if not isinstance(fence, SourceFence):
            raise TypeError("source finalization requires a SourceFence")
        with closing(self._connect()) as database, sqlite_transactions.begin_immediate(database):
            self._delete_stale_source_fences(database)
            self._require_live_source_fence(database, fence)
            require_no_source_failure(database, fence)
            self._require_no_source_reference(database, fence)
            yield

    def _require_live_source_fence(
        self, database: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """The fence row still matches this holder, and has not expired."""
        row = database.execute(
            "SELECT logical_path, source_digest, token, owner_pid, "
            "acquired_at, expires_at FROM source_fences WHERE token=?",
            (fence.token,),
        ).fetchone()
        if row is None or not _adopted_fence_row_matches(row, fence):
            raise QueueOperationError("source_fence_lost")
        expires_at = parse_timestamp(str(row["expires_at"]))
        if expires_at is None or queue_store.utc_now() >= expires_at:
            raise QueueOperationError("source_fence_lost")

    def _require_no_source_reference(
        self, database: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """No retained task may still point at the source being finalized."""
        for task in database.execute(
            "SELECT payload_blob FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        ):
            if payload_references_source(
                task_payload_text(task), fence.daily_id, fence.source_digest
            ):
                raise QueueOperationError("source_referenced")

    def release_source_fence(self, token: str) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("source fence token is invalid")
        with closing(self._connect()) as database, sqlite_transactions.begin_immediate(database):
            changed = database.execute(
                "DELETE FROM source_fences WHERE token=? AND owner_pid=?",
                (token, os.getpid()),
            ).rowcount
            if changed != 1:
                raise QueueOperationError("source_fence_lost")

    def heartbeat_task_fence(self, fence: TaskFence, owner: OwnerLease) -> None:
        """Push a live task fence out by its own length, capped by its owner's lease.

        `owner` is the fence's owner as last renewed. See
        `docs/research/2026-09-14-a-capture-keeps-its-claim-while-it-asks.md`.
        """
        if not isinstance(fence, TaskFence):
            raise TypeError("fence must be a TaskFence")
        with closing(self._connect()) as database, sqlite_transactions.begin_immediate(database):
            # Judge authority after SQLite has admitted this write transaction.
            now = queue_store.utc_now()
            expires_at = min(owner.expires_at, now + timedelta(seconds=TASK_FENCE_SECONDS))
            self._require_owner_projection(database, owner, now)
            renewed = database.execute(
                """UPDATE task_fences SET heartbeat_at=?, expires_at=?
                   WHERE task_id=? AND mode=? AND token=? AND fencing_epoch=?
                     AND canonical_owner_token=? AND canonical_fencing_epoch=?
                     AND process_id=? AND process_start_identity=? AND expires_at>?""",
                (
                    timestamp(now),
                    timestamp(expires_at),
                    fence.task_id,
                    fence.mode,
                    fence.token,
                    fence.epoch,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    timestamp(now),
                ),
            ).rowcount
            if renewed != 1:
                raise QueueOperationError("task_fence_lost")

    def sealed_ancestor_bindings(
        self, task_id: str, intent_id: str, stage: str
    ) -> tuple[dict[str, str], ...]:
        """The capture bindings this task's redrive ancestors sealed to the same decision.

        A block an ancestor committed before it died carries that ancestor's binding;
        its redrive replays the same operation and must recognise the block as its
        own family's, not as a conflict (audit 2026-09-27 B-14).
        """
        with closing(self._connect()) as database:
            seals = [
                decision_seal_of(database, ancestor, intent_id, stage)
                for ancestor in sorted(redrive_ancestors(database, task_id))
            ]
        return tuple(_sealed_binding(seal, intent_id) for seal in seals if seal is not None)
