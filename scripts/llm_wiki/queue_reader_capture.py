"""Adopted-queue capture tasks, their seals and their publication."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from llm_wiki.queue_artifacts import (
    digest_stable_owner_file,
    harden_owner_only,
    write_durable_producer,
)
from llm_wiki.queue_contracts import MAX_RUNTIME_ATTEMPTS
from llm_wiki.queue_evidence_records import (
    capture_resolution_record,
)
from llm_wiki.queue_task_lineage import (
    compile_lineage_origin,
)
from llm_wiki.queue_validation import (
    capture_decision_relative_path,
    capture_scan_cursor,
    capture_terminal_disposition_fields,
    check_capture_intent_descriptor,
    check_ready_intent_identity,
    check_resolution_reason,
    require_bounded_int,
    require_capture_decision_path,
    require_capture_fence,
    require_capture_identity,
    require_capture_intent_descriptor,
    require_capture_intent_path,
    require_capture_terminal_path,
    require_enqueue_arguments,
    require_lower_sha256,
)

if TYPE_CHECKING:
    from markdown_transaction import IntentFence
    from operational_ownership import OwnerLease
import compile_progress
from llm_wiki import queue_store
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import (
    PayloadValidation,
    validate_payload_blob,
)
from llm_wiki.queue_contracts import (
    CaptureTaskBinding,
    DeadCaptureRedrive,
    LeaseFenceError,
    QueueLease,
    QueueOperationError,
    SemanticDecision,
)
from llm_wiki.queue_store import (
    MAX_REDRIVE_GENERATIONS,
    TaskFence,
    check_claim_arguments,
    require_active,
    require_lineage_budget,
    task_payload_text,
)
from llm_wiki.queue_timing import (
    as_utc,
    parse_timestamp,
    timestamp,
)
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_deadlines import (
    operational_deadline_active,
)
from llm_wiki.sqlite_transactions import begin_immediate
from reliable_memory import (
    MAX_CAPTURE_DECISION_BYTES,
    read_runtime_bytes,
)
from secret_redact import redact_structure


def _indexed_capture_decision_published_at(row: sqlite3.Row) -> datetime:
    published_at = parse_timestamp(str(row["published_at"]))
    if published_at is None:
        raise QueueOperationError("semantic_decision_conflict")
    return published_at


def _read_indexed_capture_decision(
    state_root: Path, row: sqlite3.Row
) -> tuple[str, str]:
    decision_path = str(row["decision_path"])
    decision_sha256 = str(row["decision_sha256"])
    require_lower_sha256(decision_sha256, "decision_sha256")
    data = read_runtime_bytes(
        state_root / decision_path,
        state_root,
        max_bytes=MAX_CAPTURE_DECISION_BYTES,
        owner_only=True,
    )
    if sha256_bytes(data) != decision_sha256:
        raise QueueOperationError("semantic_decision_conflict")
    return decision_path, decision_sha256


def _existing_capture_publication_state(
    row: sqlite3.Row | None,
    *,
    pending_path: str,
    ready_path: str,
    intent_sha256: str,
    byte_size: int,
) -> str:
    if row is None:
        raise QueueOperationError("capture_intent_conflict")
    state = str(row["publication_state"])
    expected_paths = {"pending": pending_path, "ready": ready_path}
    matches = (
        expected_paths.get(state) == row["relative_path"]
        and row["intent_sha256"] == intent_sha256
        and row["byte_size"] == byte_size
    )
    if not matches:
        raise QueueOperationError("capture_intent_conflict")
    return state


def _require_capture_replay_task(
    row: sqlite3.Row,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    dedupe_key: str,
) -> None:
    stored = bytes(row["payload_blob"])
    validation = validate_payload_blob(stored, str(row["input_hash"]), parse=True)
    actual = (
        validation.code,
        row["kind"],
        row["handler_version"],
        row["input_hash"],
        row["dedupe_key"],
        stored,
    )
    expected = (None, kind, handler_version, sha256_bytes(payload_bytes), dedupe_key, payload_bytes)
    if actual != expected:
        raise QueueOperationError("capture_replay_conflict")


# How many dead captures one nightly pass redrives: each becomes a model call.
DEAD_CAPTURE_REDRIVES_PER_PASS = 50


def capture_link_record(
    task_id: str, intent_id: str, intent_sha256: str, handler_version: int
) -> dict[str, object]:
    return {
        "schema_version": "capture-task-link/v1",
        "task_id": task_id,
        "intent_id": intent_id,
        "intent_sha256": intent_sha256,
        "handler_version": handler_version,
    }


def _matching_capture_intent(
    intent: sqlite3.Row | None, intent_path: str, intent_sha256: str
) -> bool:
    return (
        intent is not None
        and intent["relative_path"] == intent_path
        and intent["intent_sha256"] == intent_sha256
        and intent["publication_state"] == "ready"
    )


def _validated_capture_payload(payload: Mapping[str, object]) -> tuple[bytes, str]:
    payload_bytes = canonical_json_bytes(redact_structure(dict(payload)))
    input_hash = sha256_bytes(payload_bytes)
    if validate_payload_blob(payload_bytes, input_hash, parse=True).code is not None:
        raise ValueError("payload_hash_mismatch")
    return payload_bytes, input_hash


def _insert_link_resolution(
    database: sqlite3.Connection,
    *,
    resolution_digest: str,
    task_id: str,
    supersedes_digest: str | None,
    observed_json: bytes,
    selected_id: str | None,
    actor: str,
    reason: str,
    now: datetime,
) -> None:
    inserted = database.execute(
        """INSERT INTO capture_task_link_resolutions(
               resolution_digest,task_id,supersedes_digest,observed_json,
               selected_intent_id,actor_identity,reason,created_at
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (
            resolution_digest,
            task_id,
            supersedes_digest,
            observed_json,
            selected_id,
            actor,
            reason,
            timestamp(now),
        ),
    ).rowcount
    if inserted != 1:
        raise QueueOperationError("capture_link_resolution_failed")


def _require_repair_projection(
    database: sqlite3.Connection, owner: OwnerLease, now: datetime
) -> None:
    """The queue still projects this repair owner at this exact epoch."""
    projection = database.execute(
        """SELECT 1 FROM queue_ownership WHERE owner_token=?
           AND fencing_epoch=? AND canonical_role='repair' AND expires_at>?""",
        (owner.token, owner.epoch, timestamp(now)),
    ).fetchone()
    if projection is None:
        raise QueueOperationError("queue_owner_fence_lost")


def _require_unfenced_unsealed_task(
    database: sqlite3.Connection, task_id: str
) -> None:
    """A fenced or sealed task's links may not be resolved."""
    if database.execute(
        "SELECT 1 FROM task_fences WHERE task_id=?", (task_id,)
    ).fetchone() is not None:
        raise QueueOperationError("task_fenced")
    if database.execute(
        "SELECT 1 FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone() is not None:
        raise QueueOperationError("capture_link_sealed")


def _selected_intent_id(
    database: sqlite3.Connection, selected_intent: Mapping[str, object] | None
) -> str | None:
    """The intent this resolution selects, when it names one that matches."""
    if selected_intent is None:
        return None
    selected_id = selected_intent["intent_id"]
    intent = database.execute(
        "SELECT * FROM capture_intents WHERE intent_id=?", (selected_id,)
    ).fetchone()
    if intent is None or intent["intent_sha256"] != selected_intent["intent_sha256"]:
        raise QueueOperationError("capture_link_conflicted")
    return selected_id


def require_dead_task(database: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    """The dead task row this redrive is allowed to copy."""
    row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    _require_redrivable(row)
    require_lineage_budget(database, row)
    _require_unspent_redrive(database, row)
    return row


def _require_redrivable(row: sqlite3.Row) -> None:
    if row["state"] != "dead":
        raise QueueOperationError("redrive_requires_dead")


# A redrive that could reach the worker: its child carries the parent's capture
# link, or the parent was not a capture. `lineage_generation` counts every change
# to a lineage and is the version its compare-and-set reads, not a count of
# chances: a child created on 2026-09-06, before a redrive carried the link, never
# reached the capture worker and still raised it.
_SPENT_REDRIVES = (
    "SELECT COUNT(*) FROM tasks AS child WHERE child.redrive_of=? AND ("
    "NOT EXISTS (SELECT 1 FROM capture_task_links WHERE task_id=?) "
    "OR EXISTS (SELECT 1 FROM capture_task_links WHERE task_id=child.id))"
)


def _require_unspent_redrive(database: sqlite3.Connection, row: sqlite3.Row) -> None:
    """The dead row gets one redrive, and only one the worker could reach counts.

    See `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.
    """
    task_id = str(row["id"])
    spent = database.execute(_SPENT_REDRIVES, (task_id, task_id)).fetchone()[0]
    if int(spent) >= MAX_REDRIVE_GENERATIONS:
        raise QueueOperationError("redrive_generations_exhausted")


def _require_initial_capture_root(root, lease, origin):
    expected = (lease.id, 0, 'pending', 'ready')
    if (root.task_id, root.sequence, root.stage, root.outcome) != expected:
        raise compile_progress.ProgressConflict('initial capture root identity changed')
    if set(root.details) != {'compile_work'}:
        raise compile_progress.ProgressConflict('initial capture requires a whole-work root')
    compile_progress.validate_root_event(root, origin)


class QueueCaptureOperations:
    def ready_capture_intents_without_task(
        self, limit: int, *, after: tuple[str, str] | None = None,
    ) -> list[dict[str, object]]:
        """Ready intents that no task was ever created for, oldest first.

        This is the outbox query: a row that committed but was never
        dispatched. `recover_expired_leases` cannot see these because it
        recovers a task whose lease expired, and these have no task at all.
        Read-only by design — adoption decides what to do with the answer.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        cursor = capture_scan_cursor(after)
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT intent.intent_id,intent.relative_path,
                          intent.intent_sha256,intent.byte_size,intent.updated_at
                   FROM capture_intents AS intent
                   LEFT JOIN capture_task_links AS link
                     ON link.intent_id=intent.intent_id
                   WHERE intent.publication_state='ready'
                     AND link.intent_id IS NULL
                     AND (intent.updated_at, intent.intent_id) > (?, ?)
                   ORDER BY intent.updated_at ASC, intent.intent_id ASC
                   LIMIT ?""",
                (*cursor, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_capture_intents(
        self, limit: int, older_than: str | None = None, *, after: tuple[str, str] | None = None,
    ) -> list[dict[str, object]]:
        """Intents still half-published, oldest first; read-only like the query above.

        A publisher killed between the pending write and `mark_capture_intent_ready`
        leaves one of these, and the ready-state query cannot see it. `older_than` is
        an ISO stamp: rows touched more recently belong to a publisher that may still
        be running. See
        `docs/research/2026-09-17-a-publication-that-stopped-half-way-is-finished.md`.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        cursor = capture_scan_cursor(after)
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT intent_id,relative_path,intent_sha256,byte_size,updated_at
                   FROM capture_intents
                   WHERE publication_state='pending'
                     AND (? IS NULL OR updated_at < ?)
                     AND (updated_at, intent_id) > (?, ?)
                   ORDER BY updated_at ASC, intent_id ASC
                   LIMIT ?""",
                (older_than, older_than, *cursor, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def publish_capture_intent(
        self,
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> None:
        check_capture_intent_descriptor(
            intent_id, intent_path, intent_sha256, byte_size
        )
        now = timestamp(queue_store.utc_now())
        with closing(self._connect()) as database, begin_immediate(database):
            existing = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                check_ready_intent_identity(
                    existing, intent_path, intent_sha256, byte_size
                )
                return
            inserted = database.execute(
                """INSERT INTO capture_intents(
                       intent_id,relative_path,intent_sha256,byte_size,
                       publication_state,updated_at
                   ) VALUES (?,?,?,?,'ready',?)""",
                (intent_id, intent_path, intent_sha256, byte_size, now),
            ).rowcount
            if inserted != 1:
                raise QueueOperationError("capture_intent_insert_failed")

    def index_capture_intent_pending(
        self,
        *,
        intent_id: str,
        pending_path: str,
        ready_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> str:
        require_capture_intent_descriptor(
            intent_id, pending_path, intent_sha256, byte_size
        )
        require_capture_intent_path(ready_path, "ready")
        now = timestamp(queue_store.utc_now())
        with closing(self._connect()) as database, begin_immediate(database):
            existing = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                return _existing_capture_publication_state(
                    existing,
                    pending_path=pending_path,
                    ready_path=ready_path,
                    intent_sha256=intent_sha256,
                    byte_size=byte_size,
                )
            inserted = database.execute(
                """INSERT INTO capture_intents(
                       intent_id,relative_path,intent_sha256,byte_size,
                       publication_state,updated_at
                   ) VALUES (?,?,?,?,'pending',?)""",
                (intent_id, pending_path, intent_sha256, byte_size, now),
            ).rowcount
            if inserted != 1:
                raise QueueOperationError("capture_intent_insert_failed")
        return "pending"

    def mark_capture_intent_ready(
        self,
        *,
        intent_id: str,
        pending_path: str,
        ready_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> None:
        require_capture_intent_descriptor(
            intent_id, pending_path, intent_sha256, byte_size
        )
        require_capture_intent_path(ready_path, "ready")
        with closing(self._connect()) as database, begin_immediate(database):
            row = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            state = _existing_capture_publication_state(
                row,
                pending_path=pending_path,
                ready_path=ready_path,
                intent_sha256=intent_sha256,
                byte_size=byte_size,
            )
            if state == "ready":
                return
            updated = database.execute(
                """UPDATE capture_intents
                   SET relative_path=?,publication_state='ready',updated_at=?
                   WHERE intent_id=? AND relative_path=?
                     AND publication_state='pending'""",
                (ready_path, timestamp(queue_store.utc_now()), intent_id, pending_path),
            ).rowcount
            if updated != 1:
                raise QueueOperationError("capture_intent_conflict")

    def enqueue_capture_task_replay_safe(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        capture_fence: object,
        owner: OwnerLease,
    ) -> CaptureTaskBinding:
        payload_bytes = canonical_json_bytes(redact_structure(dict(payload)))
        dedupe_key = f"capture:{intent_id}:{handler_version}"
        existing = self._capture_replay_binding(
            intent_id=intent_id,
            kind=kind,
            handler_version=handler_version,
            payload_bytes=payload_bytes,
            dedupe_key=dedupe_key,
        )
        if existing is not None:
            return existing
        return self.enqueue_capture_task(
            kind,
            handler_version,
            payload,
            intent_id=intent_id,
            intent_path=intent_path,
            intent_sha256=intent_sha256,
            capture_fence=capture_fence,
            owner=owner,
            dedupe_key=dedupe_key,
        )

    def _capture_replay_binding(
        self,
        *,
        intent_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        dedupe_key: str,
    ) -> CaptureTaskBinding | None:
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT task.*,link.intent_sha256
                   FROM capture_task_links AS link
                   JOIN tasks AS task ON task.id=link.task_id
                   WHERE link.intent_id=?""",
                (intent_id,),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise QueueOperationError("capture_link_conflicted")
            _require_capture_replay_task(
                rows[0], kind, handler_version, payload_bytes, dedupe_key
            )
            return self.active_capture_binding(database, str(rows[0]["id"]))

    @staticmethod
    def _insert_capture_link(
        database: sqlite3.Connection,
        *,
        task_id: str,
        intent_id: str,
        intent_sha256: str,
        handler_version: int,
        link_digest: str,
        created_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO capture_task_links(
                   task_id,intent_id,intent_sha256,handler_version,
                   link_digest,created_at
               ) VALUES (?,?,?,?,?,?)""",
            (
                task_id,
                intent_id,
                intent_sha256,
                handler_version,
                link_digest,
                created_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("capture_link_insert_failed")

    def _require_live_capture_fence(
        self, intent_id: str, capture_fence: object, owner: OwnerLease
    ) -> None:
        """The capture fence must still be the one this owner holds."""
        registry = self.ownership_registry()
        with closing(registry.connect()) as coordinator_database:
            try:
                registry.require(coordinator_database, owner)
            except Exception as exc:
                raise QueueOperationError("intent_fence_lost") from exc
            fence_row = coordinator_database.execute(
                """SELECT 1 FROM intent_fences
                   WHERE intent_id=? AND mode='capture' AND token=?
                     AND fencing_epoch=? AND canonical_role=?
                     AND canonical_scope=? AND canonical_actor_id=?
                     AND canonical_owner_token=? AND canonical_fencing_epoch=?
                     AND process_id=? AND process_start_identity=? AND expires_at>?""",
                (
                    intent_id,
                    capture_fence.token,
                    capture_fence.epoch,
                    owner.role,
                    owner.scope,
                    owner.actor_id,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    timestamp(queue_store.utc_now()),
                ),
            ).fetchone()
        if fence_row is None:
            raise QueueOperationError("intent_fence_lost")

    def _insert_capture_task_row(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
        intent_path: str,
        intent_id: str,
        intent_sha256: str,
        link_digest: str,
    ) -> None:
        self._require_capture_insert_allowed(
            database,
            intent_id=intent_id,
            intent_path=intent_path,
            intent_sha256=intent_sha256,
            dedupe_key=dedupe_key,
        )
        inserted = database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,dedupe_key,
                   state,priority,created_at,updated_at,available_at
               ) VALUES (?,?,?,?,?,?,'ready',?,?,?,?)""",
            (
                task_id,
                kind,
                handler_version,
                payload_bytes,
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
        self._insert_capture_link(
            database,
            task_id=task_id,
            intent_id=intent_id,
            intent_sha256=intent_sha256,
            handler_version=handler_version,
            link_digest=link_digest,
            created_at=timestamp(now),
        )

    def enqueue_capture_task(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        capture_fence: object,
        owner: OwnerLease,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> CaptureTaskBinding:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        require_capture_identity(intent_id, intent_sha256, intent_path)
        if not isinstance(owner, OwnerLease) or owner.role != "capture":
            raise ValueError("capture enqueue requires a capture owner")
        require_capture_fence(capture_fence, intent_id, owner, IntentFence)
        self._require_live_capture_fence(intent_id, capture_fence, owner)
        require_enqueue_arguments(kind, handler_version, priority, None)
        payload_bytes, input_hash = _validated_capture_payload(payload)
        now = queue_store.utc_now()
        ready_at = as_utc(available_at or now)
        task_id = uuid.uuid4().hex
        link_digest = sha256_bytes(
            canonical_json_bytes(
                capture_link_record(
                    task_id, intent_id, intent_sha256, handler_version
                )
            )
        )
        with closing(self._connect()) as database, begin_immediate(database):
            self._insert_capture_task_row(
                database,
                task_id=task_id,
                kind=kind,
                handler_version=handler_version,
                payload_bytes=payload_bytes,
                input_hash=input_hash,
                dedupe_key=dedupe_key,
                priority=priority,
                now=now,
                ready_at=ready_at,
                intent_path=intent_path,
                intent_id=intent_id,
                intent_sha256=intent_sha256,
                link_digest=link_digest,
            )
        return CaptureTaskBinding(
            task_id=task_id,
            intent_id=intent_id,
            intent_sha256=intent_sha256,
            handler_version=handler_version,
            active_digest=link_digest,
            seal_digest=None,
        )

    def append_capture_link_resolution(
        self,
        task_id: str,
        *,
        supersedes_digest: str | None,
        observed: Mapping[str, object],
        selected_intent: Mapping[str, object] | None,
        owner: OwnerLease,
        reason: str,
    ) -> CaptureTaskBinding:
        from operational_ownership import OwnerLease, current_actor_identity

        if not isinstance(owner, OwnerLease) or owner.role != "repair":
            raise ValueError("capture link resolution requires a repair owner")
        check_resolution_reason(reason)
        actor = current_actor_identity()
        now = queue_store.utc_now()
        record = capture_resolution_record(
            task_id=task_id,
            supersedes_digest=supersedes_digest,
            observed=observed,
            selected_intent=selected_intent,
            actor=actor,
            reason=reason,
            now=now,
        )
        resolution_digest = sha256_bytes(canonical_json_bytes(record))
        observed_json = canonical_json_bytes(record["observed"])
        registry = self.ownership_registry()
        with closing(registry.connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        with closing(self._connect()) as database, begin_immediate(database):
            _require_repair_projection(database, owner, now)
            _require_unfenced_unsealed_task(database, task_id)
            active = self.active_capture_binding(database, task_id)
            if supersedes_digest != active.active_digest:
                raise QueueOperationError("capture_link_conflicted")
            selected_id = _selected_intent_id(database, selected_intent)
            _insert_link_resolution(
                database,
                resolution_digest=resolution_digest,
                task_id=task_id,
                supersedes_digest=supersedes_digest,
                observed_json=observed_json,
                selected_id=selected_id,
                actor=actor,
                reason=reason,
                now=now,
            )
            return self.active_capture_binding(database, task_id)

    def indexed_capture_decision(
        self,
        *,
        task_id: str,
        intent_id: str,
        stage: Literal["flush", "feedback", "feedback-verify"],
        active_link_digest: str,
    ) -> SemanticDecision | None:
        require_capture_decision_path(
            capture_decision_relative_path(intent_id, stage), intent_id, stage
        )
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT * FROM semantic_decisions WHERE intent_id=? AND stage=?",
                (intent_id, stage),
            ).fetchone()
            if row is None:
                return None
            sealer = self._require_decision_for_task(
                database, row, task_id, intent_id=intent_id, stage=stage,
                active_link_digest=active_link_digest,
            )
            published_at = _indexed_capture_decision_published_at(row)
        decision_path, decision_sha256 = _read_indexed_capture_decision(
            self.state_root, row
        )
        return SemanticDecision(
            task_id=str(sealer["task_id"]),
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=str(row["active_link_digest"]),
            seal_digest=str(sealer["seal_digest"]),
            published_at=published_at,
        )

    def _require_capture_insert_allowed(
        self,
        database: sqlite3.Connection,
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        dedupe_key: str | None,
    ) -> None:
        """The intent this task claims is ready, and the dedupe key is free."""
        intent = database.execute(
            "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if not _matching_capture_intent(intent, intent_path, intent_sha256):
            raise QueueOperationError("capture_intent_conflict")
        if dedupe_key is not None and database.execute(
            "SELECT 1 FROM tasks WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone() is not None:
            raise QueueOperationError("dedupe_conflict")

    def _read_capture_terminal(
        self, terminal_path: str, terminal_sha256: str
    ) -> tuple[dict[str, object], bytes]:
        data = read_runtime_bytes(
            self.state_root / terminal_path,
            self.state_root,
            max_bytes=64 * 1024,
            owner_only=True,
        )
        if sha256_bytes(data) != terminal_sha256:
            raise QueueOperationError("capture_terminal_conflict")
        value = json.loads(data.decode("utf-8", errors="strict"))
        if not isinstance(value, dict):
            raise QueueOperationError("capture_terminal_invalid")
        return value, data

    def _capture_terminal_decisions(
        self, database: sqlite3.Connection, intent_id: str
    ) -> list[dict[str, str]]:
        rows = database.execute(
            """SELECT stage,decision_path,decision_sha256 FROM semantic_decisions
               WHERE intent_id=? ORDER BY stage""",
            (intent_id,),
        ).fetchall()
        result = []
        for row in rows:
            data = read_runtime_bytes(
                self.state_root / str(row["decision_path"]),
                self.state_root,
                max_bytes=MAX_CAPTURE_DECISION_BYTES,
                owner_only=True,
            )
            if sha256_bytes(data) != row["decision_sha256"]:
                raise QueueOperationError("semantic_decision_conflict")
            result.append(
                {
                    "stage": str(row["stage"]),
                    "decision_path": str(row["decision_path"]),
                    "decision_sha256": str(row["decision_sha256"]),
                }
            )
        return result

    @staticmethod
    def _require_capture_terminal_disposition(
        record: Mapping[str, object], decisions: list[dict[str, str]]
    ) -> None:
        disposition = record.get("disposition")
        if not isinstance(disposition, dict):
            raise QueueOperationError("capture_terminal_invalid")
        required = capture_terminal_disposition_fields(disposition.get("kind"))
        decision_digests = {item["decision_sha256"] for item in decisions}
        actual = (set(disposition), disposition.get("decision_sha256") in decision_digests)
        expected = (required, True)
        if actual != expected:
            raise QueueOperationError("capture_terminal_invalid")

    def _require_capture_terminal_record(
        self,
        record: Mapping[str, object],
        *,
        binding: CaptureTaskBinding,
        decisions: list[dict[str, str]],
    ) -> CaptureTaskBinding:
        expected_keys = {
            "schema_version",
            "intent_id",
            "intent_sha256",
            "semantic_decisions",
            "processing_binding",
            "disposition",
        }
        processing = record.get("processing_binding")
        identity = (
            set(record),
            record.get("schema_version"),
            record.get("intent_id"),
            record.get("intent_sha256"),
            record.get("semantic_decisions"),
        )
        expected_identity = (
            expected_keys,
            "capture-terminal/v1",
            binding.intent_id,
            binding.intent_sha256,
            decisions,
        )
        if identity != expected_identity or not isinstance(processing, dict):
            raise QueueOperationError("capture_terminal_invalid")
        proven = self._terminal_proof_binding(record, binding)
        self._require_capture_terminal_disposition(record, decisions)
        self._require_versioned_capture_proof(proven, record)
        return proven

    def _adopt_terminal_seal(
        self, database: sqlite3.Connection, binding: CaptureTaskBinding, proven: CaptureTaskBinding, now: datetime,
    ) -> None:
        if binding.task_id == proven.task_id:
            return
        decision = self.indexed_capture_decision(
            task_id=proven.task_id, intent_id=proven.intent_id, stage="flush",
            active_link_digest=proven.active_digest,
        )
        if decision is None:
            raise QueueOperationError("capture_terminal_invalid")
        self._require_inherited_decision_row(database, decision, binding.task_id, binding.active_digest)
        self._insert_inherited_seal(database, decision, binding.task_id, binding.active_digest, now)

    def _require_capture_terminal_owner(
        self,
        *,
        lease: QueueLease,
        intent_id: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> None:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        actual = (
            isinstance(owner, OwnerLease),
            task_fence.task_id,
            task_fence.mode,
            task_fence.owner,
            isinstance(intent_fence, IntentFence),
            getattr(intent_fence, "intent_id", None),
            getattr(intent_fence, "mode", None),
            getattr(intent_fence, "owner", None),
        )
        expected = (True, lease.id, "worker", owner, True, intent_id, "worker", owner)
        if actual != expected:
            raise ValueError("capture terminal fences do not match")

    @staticmethod
    def _require_capture_terminal_task_fence(
        database: sqlite3.Connection,
        task_fence: TaskFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND mode='worker'
               AND token=? AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_fence.task_id,
                task_fence.token,
                task_fence.epoch,
                owner.token,
                owner.epoch,
                timestamp(now),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("task_fence_lost")

    @staticmethod
    def _require_capture_terminal_intent_fence(
        database: sqlite3.Connection,
        intent_fence: IntentFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM intent_fences WHERE intent_id=? AND mode='worker'
               AND token=? AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                intent_fence.intent_id,
                intent_fence.token,
                intent_fence.epoch,
                owner.token,
                owner.epoch,
                timestamp(now),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("intent_fence_lost")

    @staticmethod
    def _commit_capture_terminal_row(
        database: sqlite3.Connection,
        row: sqlite3.Row,
        lease: QueueLease,
        *,
        terminal_path: str,
        terminal_sha256: str,
        intent_id: str,
        now: datetime,
    ) -> None:
        database.execute(
            """INSERT INTO attempt_history(
                   task_id,attempt,started_at,finished_at,outcome,error_code
               ) VALUES (?,?,?,?, 'succeeded',NULL)""",
            (
                lease.id,
                row["attempts"],
                row["attempt_started_at"] or timestamp(now),
                timestamp(now),
            ),
        )
        changed = database.execute(
            """UPDATE tasks SET state='succeeded',result_reference=?,result_sha256=?,
                   result_operation_id=?,error_code=NULL,updated_at=?,last_attempt_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   lease_heartbeat_at=NULL,attempt_started_at=NULL
               WHERE id=? AND lease_token=? AND state='leased'""",
            (
                terminal_path,
                terminal_sha256,
                f"capture-terminal:{intent_id}",
                timestamp(now),
                timestamp(now),
                lease.id,
                lease.token,
            ),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def complete_existing_capture_terminal(
        self,
        lease: QueueLease,
        *,
        intent_id: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> str | None:
        relative = f"run/queue-results/capture-{intent_id}.json"
        candidate = self.state_root / relative
        try:
            candidate.lstat()
        except FileNotFoundError:
            return None
        data = read_runtime_bytes(
            candidate,
            self.state_root,
            max_bytes=64 * 1024,
            owner_only=True,
        )
        return self.complete_capture_terminal(
            lease,
            intent_id=intent_id,
            terminal_path=relative,
            terminal_sha256=sha256_bytes(data),
            active_link_digest=active_link_digest,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )

    def complete_capture_terminal(
        self,
        lease: QueueLease,
        *,
        intent_id: str,
        terminal_path: str,
        terminal_sha256: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> str:
        require_lower_sha256(intent_id, "intent_id")
        require_lower_sha256(terminal_sha256, "terminal_sha256")
        require_capture_terminal_path(terminal_path, intent_id)
        self._require_capture_terminal_owner(
            lease=lease,
            intent_id=intent_id,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )
        record, _data = self._read_capture_terminal(terminal_path, terminal_sha256)
        registry = self.ownership_registry()
        now = queue_store.utc_now()
        with closing(registry.connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
            self._require_capture_terminal_intent_fence(
                coordinator_database, intent_fence, owner, now
            )
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            self._require_capture_terminal_task_fence(database, task_fence, owner, now)
            binding = self.active_capture_binding(database, lease.id)
            if binding.active_digest != active_link_digest or binding.intent_id != intent_id:
                raise QueueOperationError("capture_link_conflicted")
            decisions = self._capture_terminal_decisions(database, intent_id)
            proven = self._require_capture_terminal_record(record, binding=binding, decisions=decisions)
            self._adopt_terminal_seal(database, binding, proven, now)
            self._commit_capture_terminal_row(
                database,
                row,
                lease,
                terminal_path=terminal_path,
                terminal_sha256=terminal_sha256,
                intent_id=intent_id,
                now=now,
            )
        return terminal_path

    @staticmethod
    def _validate_capture_claim(
        owner: str, lease_seconds: int, max_attempts: int,
        handler_versions: tuple[int, ...],
    ) -> None:
        check_claim_arguments(owner, lease_seconds, max_attempts)
        if not handler_versions:
            raise ValueError("capture claim requires supported handler versions")
        for version in handler_versions:
            require_bounded_int(version, 1, 2_147_483_647, "invalid capture handler version")

    @staticmethod
    def _capture_claim_row(
        database: sqlite3.Connection, max_attempts: int, now: datetime,
        handler_versions: tuple[int, ...],
    ) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _version in handler_versions)
        return database.execute(
            f"""SELECT task.* FROM tasks AS task
               JOIN capture_task_links AS link ON link.task_id=task.id
               WHERE task.state='ready' AND task.kind='flush'
                 AND task.handler_version IN ({placeholders}) AND task.attempts<?
                 AND task.available_at<=?
                 AND (SELECT COUNT(*) FROM attempt_history AS history
                      WHERE history.task_id=task.id)<?
               ORDER BY task.priority DESC,task.available_at,task.created_at,task.id
               LIMIT 1""",
            (*handler_versions, max_attempts, timestamp(now), MAX_RUNTIME_ATTEMPTS),
        ).fetchone()

    def _lease_capture_row(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        owner: str,
        lease_seconds: int,
        max_attempts: int,
        now: datetime,
    ) -> QueueLease | None:
        validation = self._require_valid_task_payload(database, row, now=now, parse=True)
        if validation is None:
            return None
        token = sha256_bytes(os.urandom(32))
        expires_at = now + timedelta(seconds=lease_seconds)
        changed = database.execute(
            """UPDATE tasks SET state='leased',attempts=attempts+1,
                   lease_owner=?,lease_token=?,lease_expires_at=?,
                   lease_heartbeat_at=?,attempt_started_at=?,updated_at=?
               WHERE id=? AND state='ready' AND attempts<?""",
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
        if changed != 1:
            return None
        return QueueLease(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=validation.payload or {},
            input_hash=str(row["input_hash"]),
            owner=owner,
            token=token,
            expires_at=expires_at,
            attempt=int(row["attempts"]) + 1,
            created_at=parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    def claim_capture(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        handler_versions: tuple[int, ...] = (1,),
    ) -> QueueLease | None:
        self._validate_capture_claim(owner, lease_seconds, max_attempts, handler_versions)
        now = queue_store.utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._capture_claim_row(database, max_attempts, now, handler_versions)
            if row is None:
                return None
            return self._lease_capture_row(
                database,
                row,
                owner=owner,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
                now=now,
            )

    def compile_origin(self, lease: QueueLease) -> str:
        """Resolve original work under the current lease and payload fence."""
        return self._with_verified_lease(lease, self._compile_origin)

    @staticmethod
    def _compile_origin(database, row, now):
        return compile_lineage_origin(database, row)

    def publish_initial_compile_capture(self, lease, *, producer, decode_root, deadline=None):
        """Publish root and snapshot together after durable, validated capture bytes.

        A complete orphan is decoded before considering a fresh producer. The
        decoder must verify the source format and bind the original payload.
        """
        origin = self.compile_origin(lease)
        work_id = compile_progress.root_work_id(origin)
        relative = 'run/queue-results/' + sha256_bytes(f'compile:{work_id}:1'.encode()) + '.result'
        digest = self._initial_compile_capture_file(relative, producer, deadline)
        with self._open_compile_artifact_bytes(relative, digest, deadline) as handle:
            root = compile_progress.snapshot_event(decode_root(handle))
        _require_initial_capture_root(root, lease, origin)
        snapshot = replace(root, sequence=1, stage='snapshot_ready', details={
            'artifact': {'reference': relative, 'sha256': digest}})
        self._with_verified_lease(lease, partial(self._append_initial_compile_capture,
            root=root, snapshot=snapshot, lease=lease, deadline=deadline))
        return snapshot

    def _initial_compile_capture_file(self, relative, producer, deadline):
        path = self.state_root / relative
        try:
            digest, _size = digest_stable_owner_file(path, deadline=deadline)
            return digest
        except FileNotFoundError:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            harden_owner_only(self.results_dir, 0o700)
            return write_durable_producer(path, producer, deadline=deadline)

    def _append_initial_compile_capture(self, database, row, now, *, root, snapshot, lease, deadline):
        operational_deadline_active(deadline)
        _require_initial_capture_root(root, lease, compile_lineage_origin(database, row))
        if compile_progress.event_at(database, root.work_id, 0) is not None:
            raise compile_progress.ProgressConflict('initial capture work is already recorded')
        self._append_compile_event(database, row, now, event=root, lease=lease)
        self._append_compile_event(database, row, now, event=snapshot, lease=lease)
        operational_deadline_active(deadline)

    def _insert_redriven_task(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        replacement: str,
        task_id: str,
        validation: PayloadValidation,
    ) -> None:
        """Copy the dead task into a fresh ready one that points back at it."""
        inserted = database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,state,
                   priority,created_at,updated_at,available_at,redrive_of
               ) VALUES (?,?,?,?,?,'ready',?,?,?,?,?)""",
            (
                replacement,
                row["kind"],
                row["handler_version"],
                validation.raw,
                validation.input_hash,
                row["priority"],
                timestamp(now),
                timestamp(now),
                timestamp(now),
                task_id,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("redrive_failed")
        self._carry_capture_link(database, task_id, replacement, now)

    @staticmethod
    def _parent_capture_link(
        database: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row | None:
        return database.execute(
            "SELECT intent_id, intent_sha256, handler_version "
            "FROM capture_task_links WHERE task_id=?",
            (task_id,),
        ).fetchone()

    def _carry_capture_link(
        self,
        database: sqlite3.Connection,
        task_id: str,
        replacement: str,
        now: datetime,
    ) -> None:
        """A redriven capture task keeps its intent, or it reaches no worker.

        `claim_capture` selects only tasks that have a row in
        `capture_task_links`, and a redrive used to copy the task without it,
        so a redriven capture task was invisible to the capture worker for
        good — and the sweeper that hands a task to an intent looks for
        intents with *no* task, so an intent whose only task was dead could be
        neither adopted nor redriven. Twenty-three session classifications sat
        in that gap on this vault.

        This is not a way around the admission fence. The fence exists so a
        task cannot appear for an intent nobody holds; this intent passed it
        once, and its signed link already exists. What travels is the same
        record — intent, its digest, the handler version — re-signed for the
        child's id, which is what current practice calls carrying the
        idempotency key across a redrive. See
        `docs/research/2026-09-07-how-many-second-chances-an-intent-gets.md`.
        """
        parent = self._parent_capture_link(database, task_id)
        if parent is None:
            return
        record = capture_link_record(
            replacement,
            str(parent["intent_id"]),
            str(parent["intent_sha256"]),
            int(parent["handler_version"]),
        )
        self._insert_capture_link(
            database,
            task_id=replacement,
            intent_id=str(parent["intent_id"]),
            intent_sha256=str(parent["intent_sha256"]),
            handler_version=int(parent["handler_version"]),
            link_digest=sha256_bytes(canonical_json_bytes(record)),
            created_at=timestamp(now),
        )

    def redrive(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        require_active(deadline, cancelled)
        now = queue_store.utc_now()
        replacement = uuid.uuid4().hex
        with closing(self._connect()) as database, begin_immediate(database):
            row = require_dead_task(database, task_id)
            self._delete_stale_source_fences(database)
            self._assert_payload_not_fenced(database, task_payload_text(row))
            validation = self._require_valid_task_payload(
                database, row, now=now, parse=True
            )
            if validation is not None:
                self._insert_redriven_task(
                    database,
                    row,
                    now,
                    replacement=replacement,
                    task_id=task_id,
                    validation=validation,
                )
        if validation is None:
            self._raise_payload_mismatch()
        return replacement

    def redrive_dead_captures(
        self,
        *,
        changed_after: datetime,
        limit: int = DEAD_CAPTURE_REDRIVES_PER_PASS,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> DeadCaptureRedrive:
        """Give every dead capture that died before the code changed its one redrive.

        A fix arriving is the event a dead-letter redrive waits for, and nobody
        else here would ever ask. A redriven capture that dies again is final. See
        `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.
        """
        before = timestamp(as_utc(changed_after))
        redriven: list[tuple[str, str]] = []
        refused: list[tuple[str, str]] = []
        for task_id in self._dead_captures_before(before, limit):
            require_active(deadline, cancelled)
            outcome = self._redrive_or_refusal(task_id, deadline, cancelled)
            target = redriven if outcome[0] == "redriven" else refused
            target.append((task_id, outcome[1]))
        return DeadCaptureRedrive(tuple(redriven), tuple(refused))

    def _redrive_or_refusal(
        self,
        task_id: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[str, str]:
        try:
            return "redriven", self.redrive(task_id, deadline=deadline, cancelled=cancelled)
        except QueueOperationError as exc:
            return "refused", exc.code

    def _dead_captures_before(self, before: str, limit: int) -> list[str]:
        """Dead capture tasks at redrive depth 0 that died before `before`."""
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT t.id FROM tasks AS t
                   JOIN capture_task_links AS l ON l.task_id=t.id
                   WHERE t.state='dead' AND t.error_code IS NOT 'payload_hash_mismatch'
                     AND t.redrive_of IS NULL AND t.updated_at<?
                     AND NOT EXISTS (SELECT 1 FROM tasks AS c
                                     JOIN capture_task_links AS cl ON cl.task_id=c.id
                                     WHERE c.redrive_of=t.id)
                   ORDER BY t.updated_at, t.id LIMIT ?""",
                (before, limit),
            ).fetchall()
        return [str(row["id"]) for row in rows]
