"""Source failures and the fences that keep a source from being worked twice."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

from llm_wiki import queue_store
from llm_wiki.queue_contracts import QueueOperationError, SourceFence
from llm_wiki.queue_store import (
    SOURCE_FAILURE_KEYS,
    valid_source_failure_fields,
)
from llm_wiki.queue_timing import as_utc, parse_timestamp, timestamp
from llm_wiki.queue_validation import require_known_states, require_positive_lease
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_transactions import begin_immediate


def _source_fence_row_matches(row: sqlite3.Row, fence: SourceFence) -> bool:
    """The stored fence names the same source, token and owning process."""
    stored = (
        str(row["daily_id"]),
        str(row["source_digest"]),
        str(row["token"]),
        int(row["owner_pid"]),
        str(row["acquired_at"]),
    )
    expected = (
        fence.daily_id,
        fence.source_digest,
        fence.token,
        os.getpid(),
        timestamp(fence.acquired_at),
    )
    return stored == expected and fence.owner_pid == os.getpid()


def insert_source_fence(
    connection: sqlite3.Connection, values: tuple[object, ...]
) -> None:
    """Record the fence, refusing when another one already holds the source."""
    try:
        connection.execute(
            "INSERT INTO source_fences "
            "(daily_id, source_digest, token, owner_pid, acquired_at, "
            "heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    except sqlite3.IntegrityError as exc:
        raise QueueOperationError("source_fenced") from exc


class MemoryQueueSourceFences:
    def record_source_failure(
        self,
        logical_path: str,
        source_digest: str,
        *,
        error_code: str,
        producer: str,
        deadline: float = float("inf"),
    ) -> None:
        self._validate_failure_identity(logical_path, source_digest)
        if not valid_source_failure_fields(error_code, producer):
            raise ValueError("source failure fields are invalid")
        with queue_store.source_failure_connection(self, deadline) as connection, begin_immediate(connection):
            queue_store.record_source_failure_row(
                connection,
                logical_path,
                source_digest,
                error_code,
                producer,
                as_utc(self._clock()),
            )
            queue_store.source_failure_commit_active(connection, deadline)

    def clear_source_failure(self, logical_path: str, source_digest: str, *, deadline: float = float("inf")) -> None:
        self._validate_failure_identity(logical_path, source_digest)
        with queue_store.source_failure_connection(self, deadline) as connection, begin_immediate(connection):
            connection.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            )
            queue_store.source_failure_commit_active(connection, deadline)

    def source_failure_keys(self, *, deadline: float = float("inf")) -> list[tuple[str, str]]:
        """Every recorded `(logical_path, source_digest)`, so a caller can retire stale ones.

        A row keyed by a digest the file no longer has is about content that is
        gone (audit B-15, docs/research/2026-09-25-a-failure-of-content-that-is-gone-is-retired.md).
        """
        with queue_store.source_failure_connection(self, deadline) as connection:
            rows = connection.execute(SOURCE_FAILURE_KEYS).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

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

    def _delete_stale_source_fences(self, connection: sqlite3.Connection) -> None:
        import process_liveness

        now = as_utc(self._clock())
        rows = connection.execute(
            "SELECT token, owner_pid, expires_at FROM source_fences"
        ).fetchall()
        for row in rows:
            expires_at = parse_timestamp(str(row["expires_at"]))
            expired = expires_at is None or expires_at <= now
            if expired or not process_liveness.owner_alive(int(row["owner_pid"])):
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
        require_known_states(states)
        placeholders = ",".join("?" for _ in states)
        with self._connect() as connection:
            cursor = connection.execute(
                f"SELECT id, payload_json FROM tasks WHERE state IN ({placeholders}) "
                "ORDER BY created_at, rowid",  # noqa: S608 - generated placeholders
                states,
            )
            return tuple(
                str(row["id"])
                for row in cursor
                if self._payload_references_source(
                    str(row["payload_json"]), daily_id, source_digest
                )
            )

    def _require_source_unreferenced(
        self, connection: sqlite3.Connection, daily_id: str, source_digest: str
    ) -> None:
        """Refuse a fence over a source that some live task still points at."""
        rows = connection.execute(
            "SELECT id, payload_json FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        )
        if any(
            self._payload_references_source(
                str(row["payload_json"]), daily_id, source_digest
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
        self._validate_source_identity(daily_id, source_digest)
        require_positive_lease(lease_seconds)
        token = f"{self._rng.getrandbits(256):064x}"
        acquired = as_utc(self._clock())
        expires = acquired + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            self._require_source_unreferenced(connection, daily_id, source_digest)
            insert_source_fence(
                connection,
                (
                    daily_id,
                    source_digest,
                    token,
                    os.getpid(),
                    timestamp(acquired),
                    timestamp(acquired),
                    timestamp(expires),
                ),
            )
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
        require_positive_lease(lease_seconds)
        now = as_utc(self._clock())
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            changed = connection.execute(
                """UPDATE source_fences SET heartbeat_at=?, expires_at=?
                   WHERE daily_id=? AND source_digest=? AND token=? AND owner_pid=?
                     AND acquired_at=? AND heartbeat_at=? AND expires_at=?""",
                (
                    timestamp(now),
                    timestamp(expires),
                    fence.daily_id,
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

    @contextmanager
    def source_finalization(self, fence: SourceFence):
        if not isinstance(fence, SourceFence):
            raise TypeError("source finalization requires a SourceFence")
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            self._require_live_source_fence(connection, fence)
            self._require_no_source_failure(connection, fence)
            self._require_no_source_reference(connection, fence)
            yield

    def _require_live_source_fence(
        self, connection: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """The fence row still matches this holder, and has not expired."""
        row = connection.execute(
            "SELECT daily_id, source_digest, token, owner_pid, acquired_at, expires_at "
            "FROM source_fences WHERE token=?",
            (fence.token,),
        ).fetchone()
        if row is None or not _source_fence_row_matches(row, fence):
            raise QueueOperationError("source_fence_lost")
        expires_at = parse_timestamp(str(row["expires_at"]))
        if expires_at is None or as_utc(self._clock()) >= expires_at:
            raise QueueOperationError("source_fence_lost")

    def _require_no_source_reference(
        self, connection: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """No retained task may still point at the source being finalized."""
        for task in connection.execute(
            "SELECT payload_json FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        ):
            if self._payload_references_source(
                str(task["payload_json"]), fence.daily_id, fence.source_digest
            ):
                raise QueueOperationError("source_referenced")

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
