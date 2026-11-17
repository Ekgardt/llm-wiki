"""Adopted-queue semantic decisions: publication and adoption."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from llm_wiki.queue_task_lineage import (
    capture_semantic_seal_digest,
)
from llm_wiki.queue_validation import (
    require_capture_decision_path,
    require_matching_decision_owner,
    require_semantic_decision_shape,
    require_worker_intent_fence,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from llm_wiki import queue_store
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.queue_contracts import (
    QueueOperationError,
    SemanticDecision,
)
from llm_wiki.queue_store import TaskFence
from llm_wiki.queue_timing import parse_timestamp, timestamp
from llm_wiki.sqlite_transactions import begin_immediate
from reliable_memory import (
    MAX_CAPTURE_DECISION_BYTES,
    read_runtime_bytes,
)


def _require_semantic_decision_fences(
    task_id: str,
    intent_id: str,
    stage: str,
    decision_sha256: str,
    task_fence: object,
    intent_fence: object,
    owner: object,
    fence_type: type,
    owner_type: type,
) -> None:
    require_semantic_decision_shape(stage, decision_sha256)
    if not isinstance(task_fence, TaskFence) or task_fence.task_id != task_id:
        raise ValueError("task fence does not match")
    require_worker_intent_fence(intent_fence, intent_id, fence_type)
    require_matching_decision_owner(owner, task_fence, intent_fence, owner_type)


# The stored columns a semantic decision has to agree with, in argument order.
_SEMANTIC_DECISION_FIELDS = (
    "seal_digest",
    "decision_path",
    "decision_sha256",
    "active_link_digest",
)


def _semantic_decision_matches(
    existing: sqlite3.Row,
    intent_id: str,
    stage: str,
    seal_digest: str,
    decision_path: str,
    decision_sha256: str,
    active_link_digest: str,
) -> bool:
    if existing["consumer_kind"] != "semantic-decision":
        return False
    if existing["consumer_id"] != f"{intent_id}:{stage}":
        return False
    expected = (seal_digest, decision_path, decision_sha256, active_link_digest)
    return tuple(existing[field] for field in _SEMANTIC_DECISION_FIELDS) == expected


def _require_semantic_seal_matches(existing: sqlite3.Row, *expected: str) -> None:
    """An existing seal is idempotent only for the same decision."""
    if not _semantic_decision_matches(existing, *expected):
        raise QueueOperationError("semantic_decision_conflict")


class QueueSemanticDecisionOperations:
    @staticmethod
    def _insert_semantic_decision(
        database: sqlite3.Connection,
        *,
        intent_id: str,
        stage: str,
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        published_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO semantic_decisions(
                   intent_id,stage,decision_path,decision_sha256,
                   active_link_digest,publication_state,published_at
               ) VALUES (?,?,?,?,?,'published',?)""",
            (
                intent_id,
                stage,
                decision_path,
                decision_sha256,
                active_link_digest,
                published_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("semantic_decision_insert_failed")

    def _read_semantic_decision_bytes(
        self, decision_path: str, decision_sha256: str, intent_id: str, stage: str
    ) -> None:
        require_capture_decision_path(decision_path, intent_id, stage)
        data = read_runtime_bytes(
            self.state_root / decision_path,
            self.state_root,
            max_bytes=MAX_CAPTURE_DECISION_BYTES,
            owner_only=True,
        )
        if sha256_bytes(data) != decision_sha256:
            raise QueueOperationError("semantic_decision_conflict")

    def _require_live_worker_intent(
        self, intent_id: str, intent_fence: object, owner: OwnerLease, now: datetime
    ) -> None:
        registry = self.ownership_registry()
        with closing(registry.connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
            intent_row = coordinator_database.execute(
                """SELECT 1 FROM intent_fences WHERE intent_id=? AND mode='worker'
                   AND token=? AND fencing_epoch=? AND canonical_owner_token=?
                   AND canonical_fencing_epoch=? AND expires_at>?""",
                (
                    intent_id,
                    intent_fence.token,
                    intent_fence.epoch,
                    owner.token,
                    owner.epoch,
                    timestamp(now),
                ),
            ).fetchone()
        if intent_row is None:
            raise QueueOperationError("intent_fence_lost")

    def _require_live_task_fence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task_fence: TaskFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        projection = database.execute(
            """SELECT 1 FROM queue_ownership WHERE owner_token=?
               AND fencing_epoch=? AND expires_at>?""",
            (owner.token, owner.epoch, timestamp(now)),
        ).fetchone()
        fence_row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_id,
                task_fence.token,
                task_fence.epoch,
                owner.token,
                owner.epoch,
                timestamp(now),
            ),
        ).fetchone()
        if projection is None or fence_row is None:
            raise QueueOperationError("task_fence_lost")

    def _require_unsealed_capture_link(
        self,
        database: sqlite3.Connection,
        task_id: str,
        intent_id: str,
        active_link_digest: str,
    ) -> None:
        active = self.active_capture_binding(database, task_id)
        if (
            active.intent_id != intent_id
            or active.active_digest != active_link_digest
            or active.seal_digest is not None
        ):
            raise QueueOperationError("capture_link_conflicted")

    def _seal_semantic_decision(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        intent_id: str,
        stage: str,
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        seal_digest: str,
        now: datetime,
    ) -> None:
        existing = database.execute(
            """SELECT seal.*, decision.decision_path,
                      decision.decision_sha256,decision.active_link_digest
               FROM capture_task_link_seals AS seal
               LEFT JOIN semantic_decisions AS decision
                 ON decision.intent_id=? AND decision.stage=?
               WHERE seal.task_id=?""",
            (intent_id, stage, task_id),
        ).fetchone()
        if existing is not None:
            _require_semantic_seal_matches(
                existing,
                intent_id,
                stage,
                seal_digest,
                decision_path,
                decision_sha256,
                active_link_digest,
            )
            return
        inserted = database.execute(
            """INSERT INTO capture_task_link_seals(
                   task_id,active_digest,consumer_kind,consumer_id,
                   seal_digest,sealed_at
               ) VALUES (?,?,'semantic-decision',?,?,?)""",
            (
                task_id,
                active_link_digest,
                f"{intent_id}:{stage}",
                seal_digest,
                timestamp(now),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("capture_link_seal_failed")
        self._insert_semantic_decision(
            database,
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=active_link_digest,
            published_at=timestamp(now),
        )

    def _published_decision_time(
        self, database: sqlite3.Connection, intent_id: str, stage: str
    ) -> datetime:
        published = database.execute(
            """SELECT published_at FROM semantic_decisions
               WHERE intent_id=? AND stage=?""",
            (intent_id, stage),
        ).fetchone()
        if published is None:
            raise QueueOperationError("semantic_decision_conflict")
        published_at = parse_timestamp(str(published["published_at"]))
        if published_at is None:
            raise QueueOperationError("semantic_decision_conflict")
        return published_at

    def publish_semantic_decision(
        self,
        coordinator: object,
        *,
        task_id: str,
        intent_id: str,
        stage: Literal["flush", "feedback", "feedback-verify"],
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> SemanticDecision:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        _require_semantic_decision_fences(
            task_id,
            intent_id,
            stage,
            decision_sha256,
            task_fence,
            intent_fence,
            owner,
            IntentFence,
            OwnerLease,
        )
        self._read_semantic_decision_bytes(
            decision_path, decision_sha256, intent_id, stage
        )
        now = queue_store.utc_now()
        self._require_live_worker_intent(intent_id, intent_fence, owner, now)
        seal_digest = capture_semantic_seal_digest(
            task_id, intent_id, stage, active_link_digest
        )
        with closing(self._connect()) as database, begin_immediate(database):
            self._require_live_task_fence(database, task_id, task_fence, owner, now)
            self._require_unsealed_capture_link(
                database, task_id, intent_id, active_link_digest
            )
            self._seal_semantic_decision(
                database,
                task_id=task_id,
                intent_id=intent_id,
                stage=stage,
                decision_path=decision_path,
                decision_sha256=decision_sha256,
                active_link_digest=active_link_digest,
                seal_digest=seal_digest,
                now=now,
            )
            published_at = self._published_decision_time(database, intent_id, stage)
        return SemanticDecision(
            task_id=task_id,
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=active_link_digest,
            seal_digest=seal_digest,
            published_at=published_at,
        )

    def adopt_semantic_decision(
        self,
        decision: SemanticDecision,
        *,
        task_id: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> None:
        """Seal this redrive's link to the decision an ancestor already published.

        Writing Markdown needs the working task's own sealed link, and a redrive has
        none: its link was re-signed for it. The seal names the same decision and
        adds no second one (audit 2026-09-27 B-14).
        """
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        _require_semantic_decision_fences(
            task_id, decision.intent_id, decision.stage, decision.decision_sha256,
            task_fence, intent_fence, owner, IntentFence, OwnerLease,
        )
        now = queue_store.utc_now()
        self._require_live_worker_intent(decision.intent_id, intent_fence, owner, now)
        with closing(self._connect()) as database, begin_immediate(database):
            self._require_live_task_fence(database, task_id, task_fence, owner, now)
            self._require_inherited_decision_row(database, decision, task_id, active_link_digest)
            self._insert_inherited_seal(database, decision, task_id, active_link_digest, now)
