"""Connections, row reads and validation every adopted-queue operation shares."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from llm_wiki.queue_artifacts import (
    check_owner_file_metadata,
    read_stable_owner_file,
    require_stream_identity,
    stream_digest,
)
from llm_wiki.queue_attempts import (
    require_lease_input,
    require_leased_row,
    retire_exhausted_history,
    retire_exhausted_ready,
    take_task_lease,
)
from llm_wiki.queue_contracts import MAX_RESULT_BYTES
from llm_wiki.queue_task_lineage import (
    capture_binding_of,
    capture_semantic_seal_digest,
    decision_sealer,
    redrive_ancestors,
    require_compile_root_work,
    require_compile_work_family,
    require_indexed_capture_decision,
    require_inherited_capture_decision,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
import compile_progress
from llm_wiki import queue_store, sqlite_transactions
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.formats.queue_payload import (
    PayloadValidation,
    validate_payload_blob,
)
from llm_wiki.queue_contracts import (
    CaptureTaskBinding,
    QueueLease,
    QueueOperationError,
    SemanticDecision,
)
from llm_wiki.queue_database_contract import (
    checked_queue_connection,
)
from llm_wiki.queue_store import (
    EXPIRED_LEASE_OUTCOMES,
    TASK_FENCE_SECONDS,
    V3_WORK_CLAIMABLE,
    TaskFence,
    is_compile_recovery,
    payload_references_source,
    register_source_reference,
    settle_compile_recovery,
)
from llm_wiki.queue_timing import (
    parse_timestamp,
    timestamp,
)
from llm_wiki.queue_v3_readers import (
    verify_retained_artifact,
)
from llm_wiki.reliability_defaults import DEFAULTS
from reliable_memory import (
    open_operational_db,
)


def _open_queue_operational(path: Path, *, busy_ms: int, deadline=None, expected_contract=None) -> sqlite3.Connection:
    return checked_queue_connection(open_operational_db(
        path, busy_ms=busy_ms, deadline=deadline,
    ), expected_contract)


# Which owner roles may project each queue role.
_QUEUE_OWNER_PARENT_ROLES = {
    "queue-worker": frozenset(
        {"queue-worker", "compile", "doctor", "nightly", "weekly"}
    ),
    "queue-operator": frozenset({"queue-operator", "repair"}),
}


def _reclaim_dead_queue_projections(
    database: sqlite3.Connection, registry: Any, lease: OwnerLease
) -> None:
    """Take over the projections a dead owner left in this database.

    The projection lives here, not in the coordinator, so no reclaim on that
    side can ever reach it: it needs the same consultation, on the same proof
    — expired lease plus a process the OS says is gone — which is why the
    answer comes from the registry rather than from a second rule written
    here. A live or unprovable owner still refuses by name. The registry is
    resolved by the caller, before this database's write transaction opens, so
    the two databases are never locked in the reverse of the usual order.
    """
    rows = database.execute(
        """SELECT * FROM queue_ownership
           WHERE actor_id=? OR owner_token=?
              OR (canonical_role=? AND canonical_scope=?)""",
        (lease.actor_id, lease.token, lease.role, lease.scope),
    ).fetchall()
    for row in rows:
        _require_dead_queue_owner(registry, row)
        _delete_queue_projection_row(database, row)


def _require_dead_queue_owner(registry: Any, row: sqlite3.Row) -> None:
    """A projection is reclaimable exactly when its canonical owner is."""
    if registry.reclaimable_dead_owner(row):
        return
    raise QueueOperationError(
        "queue_owner_busy",
        f"{row['canonical_role']} {row['canonical_scope']} is already projected",
    )


# A task fence exists only while its canonical owner does, and it names that
# owner by the same tuple, so it is reclaimed by the same proof, in the same
# transaction. Without this the ownership reclaim succeeds and the very next
# step still refuses `task_fenced` for ever.
_DEAD_OWNER_TASK_FENCES = """DELETE FROM task_fences
   WHERE canonical_role=? AND canonical_scope=? AND canonical_actor_id=?
     AND canonical_owner_token=? AND canonical_fencing_epoch=?"""


def _delete_queue_projection_row(
    database: sqlite3.Connection, row: sqlite3.Row
) -> None:
    database.execute(
        _DEAD_OWNER_TASK_FENCES,
        (
            row["canonical_role"],
            row["canonical_scope"],
            row["actor_id"],
            row["owner_token"],
            row["fencing_epoch"],
        ),
    )
    deleted = database.execute(
        """DELETE FROM queue_ownership
           WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
             AND owner_token=? AND fencing_epoch=? AND process_id=?
             AND process_start_identity=?""",
        (
            row["actor_id"],
            row["canonical_role"],
            row["canonical_scope"],
            row["owner_token"],
            row["fencing_epoch"],
            row["process_id"],
            row["process_start_identity"],
        ),
    ).rowcount
    if deleted != 1:
        raise QueueOperationError("queue_owner_fence_lost")


def _require_projectable_parent(registry: Any, parent: object, role: str) -> None:
    """Refuse a parent lease that cannot project the requested queue role."""
    from operational_ownership import OwnerLease

    if not isinstance(parent, OwnerLease):
        raise TypeError("parent must be an OwnerLease")
    if parent.role not in _QUEUE_OWNER_PARENT_ROLES[role]:
        raise ValueError("parent role cannot project the requested queue role")
    with closing(registry.connect()) as database:
        registry.require(database, parent)


_LeaseAction = Callable[[sqlite3.Connection, sqlite3.Row, datetime], object]


def _require_dispatch_execution(row, event):
    if is_compile_recovery(row) and 'model_call' in event.details:
        compile_progress.require_model_phase(event)
        if event.details['model_call']['phase'] == 'dispatch':
            raise QueueOperationError('compile_recovery_is_not_execution')


def _fence_owner_alive(row: sqlite3.Row) -> bool:
    """A v3 fence names its owner's process, so a reused PID is not its owner (C-12)."""
    import process_liveness

    return process_liveness.owner_alive(int(row["owner_pid"]), str(row["owner_start_identity"]))


def _verify_open_artifact(handle, path, before, expected, deadline):
    require_stream_identity(path, handle.fileno(), before)
    actual, _ = stream_digest(handle, before.st_size, deadline=deadline)
    if actual != expected:
        raise compile_progress.ProgressCorrupt('compile artifact digest mismatch')
    require_stream_identity(path, handle.fileno(), before)
    handle.seek(0)


class QueueReaderFoundation:
    def ownership_registry(self):
        from operational_ownership import OwnershipRegistry

        if self.coordinator_path is None:
            return OwnershipRegistry(self.state_root)
        return OwnershipRegistry.from_adopted_database(
            self.state_root, self.coordinator_path
        )

    def _connect(self, *, busy_ms: int | None = None, deadline: float | None = None) -> sqlite3.Connection:
        return _open_queue_operational(
            self.db_path,
            busy_ms=DEFAULTS.queue_busy_ms if busy_ms is None else busy_ms,
            deadline=deadline,
            expected_contract=self.expected_contract,
        )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """One write connection; the seam both queue backends offer alike."""
        with closing(self._connect()) as database, database:
            yield database

    # The source-fence family, adopted (2026-08-27). The rows live in the V3
    # `source_fences` table -- keyed by `logical_path` instead of the legacy
    # `daily_id`, and carrying `owner_start_identity` -- while every
    # caller-visible contract (arguments, refusal codes, ordering) stays
    # exactly the legacy one, so `archive_daily` runs unchanged.
    def _delete_stale_source_fences(self, database: sqlite3.Connection) -> None:
        """Drop fences whose lease expired or whose owning process died."""
        now = queue_store.utc_now()
        rows = database.execute(
            "SELECT token, owner_pid, owner_start_identity, expires_at FROM source_fences"
        ).fetchall()
        for row in rows:
            expires_at = parse_timestamp(str(row["expires_at"]))
            expired = expires_at is None or expires_at <= now
            if expired or not _fence_owner_alive(row):
                database.execute(
                    "DELETE FROM source_fences WHERE token=?", (row["token"],)
                )

    @staticmethod
    def _fenced_sources(database: sqlite3.Connection) -> list[tuple[str, str]]:
        """Every held fence as the (daily_id, source_digest) it protects."""
        return [
            (Path(str(row["logical_path"])).stem, str(row["source_digest"]))
            for row in database.execute(
                "SELECT logical_path, source_digest FROM source_fences"
            )
        ]

    def _assert_payload_not_fenced(
        self, database: sqlite3.Connection, payload_json: str
    ) -> None:
        for daily_id, source_digest in self._fenced_sources(database):
            if payload_references_source(
                payload_json, daily_id, source_digest
            ):
                raise QueueOperationError("source_fenced")

    @staticmethod
    def _demote_payload_mismatch(
        database: sqlite3.Connection, row: sqlite3.Row, *, now: datetime
    ) -> None:
        changed = database.execute(
            """UPDATE tasks SET state='dead', error_code='payload_hash_mismatch',
                   updated_at=?, lease_owner=NULL, lease_token=NULL,
                   lease_expires_at=NULL, lease_heartbeat_at=NULL,
                   attempt_started_at=NULL, blocked_capability=NULL
               WHERE id=? AND state=?""",
            (timestamp(now), row["id"], row["state"]),
        ).rowcount
        if changed != 1:
            raise QueueOperationError("payload_demotion_failed")

    @staticmethod
    def _require_valid_task_payload(
        database: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
        parse: bool,
    ) -> PayloadValidation | None:
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=parse
        )
        if validation.code is not None:
            QueueReaderFoundation._demote_payload_mismatch(database, row, now=now)
            return None
        return validation

    @staticmethod
    def _require_lease_row(
        database: sqlite3.Connection, lease: QueueLease, now: datetime
    ) -> sqlite3.Row:
        return require_leased_row(database, lease, now)

    @staticmethod
    def _raise_payload_mismatch() -> None:
        raise QueueOperationError("payload_hash_mismatch")

    def _insert_task_row(
        self,
        database: sqlite3.Connection,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
    ) -> None:
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

    @staticmethod
    def _validate_task_fence_owner(
        owner: OwnerLease, mode: Literal["worker", "queue-operator"]
    ) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(owner, OwnerLease):
            raise TypeError("owner must be an OwnerLease")
        allowed = {
            "worker": {"queue-worker", "compile", "doctor", "nightly", "weekly"},
            "queue-operator": {"queue-operator", "repair"},
        }
        if mode not in allowed or owner.role not in allowed[mode]:
            raise ValueError("owner cannot acquire the requested task fence")

    @staticmethod
    def _require_owner_projection(
        database: sqlite3.Connection, owner: OwnerLease, now: datetime
    ) -> None:
        """Refuse a fence whose owner is no longer projected into the queue."""
        projection = database.execute(
            """SELECT 1 FROM queue_ownership
               WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                 AND owner_token=? AND fencing_epoch=? AND process_id=?
                 AND process_start_identity=? AND expires_at>?""",
            (
                owner.actor_id,
                owner.role,
                owner.scope,
                owner.token,
                owner.epoch,
                owner.process.pid,
                owner.process.start_identity,
                timestamp(now),
            ),
        ).fetchone()
        if projection is None:
            raise QueueOperationError("queue_owner_fence_lost")

    @staticmethod
    def _require_fenceable_task(
        database: sqlite3.Connection, task_id: str
    ) -> None:
        """Refuse a fence over a task that is missing or already fenced."""
        if database.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone() is None:
            raise KeyError(task_id)
        if database.execute(
            "SELECT 1 FROM task_fences WHERE task_id=?", (task_id,)
        ).fetchone() is not None:
            raise QueueOperationError("task_fenced")

    @staticmethod
    def _next_task_fence_epoch(database: sqlite3.Connection, task_id: str) -> int:
        """The next fencing epoch for this task, taken atomically."""
        return int(
            database.execute(
                """INSERT INTO task_fence_epochs(task_id,last_epoch) VALUES (?,1)
                   ON CONFLICT(task_id) DO UPDATE
                   SET last_epoch=task_fence_epochs.last_epoch+1
                   RETURNING last_epoch""",
                (task_id,),
            ).fetchone()[0]
        )

    @staticmethod
    def _insert_task_fence(
        database: sqlite3.Connection, values: tuple[object, ...]
    ) -> None:
        """Record the fence row, refusing when it did not land."""
        inserted = database.execute(
            """INSERT INTO task_fences(
                   task_id,mode,token,fencing_epoch,canonical_role,
                   canonical_scope,canonical_actor_id,canonical_owner_token,
                   canonical_fencing_epoch,process_id,process_start_identity,
                   heartbeat_at,expires_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("task_fence_failed")

    def acquire_task_fence(
        self,
        task_id: str,
        *,
        mode: Literal["worker", "queue-operator"],
        owner: OwnerLease,
    ) -> TaskFence:
        self._validate_task_fence_owner(owner, mode)
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be non-empty")
        registry = self.ownership_registry()
        with closing(registry.connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        token = uuid.uuid4().hex
        with closing(self._connect()) as database, sqlite_transactions.begin_immediate(database):
            # Judge authority after SQLite has admitted this write transaction.
            now = queue_store.utc_now()
            expires_at = min(owner.expires_at, now + timedelta(seconds=TASK_FENCE_SECONDS))
            self._require_owner_projection(database, owner, now)
            self._require_fenceable_task(database, task_id)
            epoch = self._next_task_fence_epoch(database, task_id)
            self._insert_task_fence(
                database,
                (
                    task_id,
                    mode,
                    token,
                    epoch,
                    owner.role,
                    owner.scope,
                    owner.actor_id,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    timestamp(now),
                    timestamp(expires_at),
                ),
            )
        return TaskFence(task_id, mode, token, epoch, owner, expires_at)

    def release_task_fence(self, fence: TaskFence) -> None:
        if not isinstance(fence, TaskFence):
            raise TypeError("fence must be a TaskFence")
        with closing(self._connect()) as database, sqlite_transactions.begin_immediate(database):
            deleted = database.execute(
                """DELETE FROM task_fences
                   WHERE task_id=? AND mode=? AND token=? AND fencing_epoch=?
                     AND canonical_role=? AND canonical_scope=?
                     AND canonical_actor_id=? AND canonical_owner_token=?
                     AND canonical_fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    fence.task_id,
                    fence.mode,
                    fence.token,
                    fence.epoch,
                    fence.owner.role,
                    fence.owner.scope,
                    fence.owner.actor_id,
                    fence.owner.token,
                    fence.owner.epoch,
                    fence.owner.process.pid,
                    fence.owner.process.start_identity,
                ),
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("task_fence_lost")

    @contextmanager
    def task_fence(
        self,
        task_id: str,
        *,
        mode: Literal["worker", "queue-operator"],
        owner: OwnerLease,
    ) -> Iterator[TaskFence]:
        fence = self.acquire_task_fence(task_id, mode=mode, owner=owner)
        try:
            yield fence
        finally:
            self.release_task_fence(fence)

    def active_capture_binding(
        self, connection: sqlite3.Connection | None, task_id: str
    ) -> CaptureTaskBinding:
        owned = connection is None
        database = self._connect() if owned else connection
        try:
            return capture_binding_of(database, task_id)
        finally:
            if owned:
                database.close()

    def _require_decision_for_task(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        task_id: str,
        *,
        intent_id: str,
        stage: str,
        active_link_digest: str,
    ) -> sqlite3.Row:
        """The seal behind this decision, which this task made or inherited by redrive."""
        active = self.active_capture_binding(database, task_id)
        sealer = decision_sealer(database, row, intent_id, stage)
        if sealer["task_id"] == task_id:
            require_indexed_capture_decision(
                row, active, intent_id=intent_id, stage=stage, active_link_digest=active_link_digest
            )
            return sealer
        require_inherited_capture_decision(
            database, sealer, active, intent_id=intent_id, stage=stage,
            active_link_digest=active_link_digest,
        )
        return sealer

    def _require_inherited_decision_row(
        self,
        database: sqlite3.Connection,
        decision: SemanticDecision,
        task_id: str,
        active_link_digest: str,
    ) -> None:
        row = database.execute(
            "SELECT * FROM semantic_decisions WHERE intent_id=? AND stage=?",
            (decision.intent_id, decision.stage),
        ).fetchone()
        if row is None or row["decision_sha256"] != decision.decision_sha256:
            raise QueueOperationError("semantic_decision_conflict")
        self._require_decision_for_task(
            database, row, task_id, intent_id=decision.intent_id, stage=decision.stage,
            active_link_digest=active_link_digest,
        )

    @staticmethod
    def _insert_inherited_seal(
        database: sqlite3.Connection,
        decision: SemanticDecision,
        task_id: str,
        active_link_digest: str,
        now: datetime,
    ) -> None:
        """Insert this task's seal once; a retry of the same task finds it already there."""
        seal_digest = capture_semantic_seal_digest(
            task_id, decision.intent_id, decision.stage, active_link_digest
        )
        database.execute(
            """INSERT OR IGNORE INTO capture_task_link_seals(
                   task_id,active_digest,consumer_kind,consumer_id,seal_digest,sealed_at
               ) VALUES (?,?,'semantic-decision',?,?,?)""",
            (
                task_id,
                active_link_digest,
                f"{decision.intent_id}:{decision.stage}",
                seal_digest,
                timestamp(now),
            ),
        )

    @staticmethod
    def _terminal_processing_of(binding: CaptureTaskBinding) -> dict[str, str]:
        return {"kind": "task", "task_id": binding.task_id, "active_link_digest": binding.active_digest}

    def _terminal_proof_binding(self, record: Mapping[str, object], binding: CaptureTaskBinding) -> CaptureTaskBinding:
        processing = record.get("processing_binding")
        if processing == self._terminal_processing_of(binding):
            return binding
        if not isinstance(processing, dict):
            raise QueueOperationError("capture_terminal_invalid")
        return self._inherited_terminal_binding(processing, binding)

    def _inherited_terminal_binding(self, processing: dict, binding: CaptureTaskBinding) -> CaptureTaskBinding:
        task_id = processing.get("task_id")
        with closing(self._connect()) as database:
            ancestors = redrive_ancestors(database, binding.task_id)
            if not isinstance(task_id, str) or task_id not in ancestors:
                raise QueueOperationError("capture_terminal_invalid")
            parent = self.active_capture_binding(database, task_id)
        self._require_terminal_ancestor(parent, binding, processing)
        return parent

    def _require_terminal_ancestor(self, parent: CaptureTaskBinding, child: CaptureTaskBinding, processing: dict) -> None:
        actual = (parent.intent_id, parent.intent_sha256, parent.handler_version, parent.seal_digest is not None, processing)
        expected = (child.intent_id, child.intent_sha256, child.handler_version, True, self._terminal_processing_of(parent))
        if actual != expected:
            raise QueueOperationError("capture_terminal_invalid")

    def _require_versioned_capture_proof(self, binding: CaptureTaskBinding, record: Mapping[str, object]) -> None:
        if binding.handler_version == 1:
            return
        if binding.handler_version != 2:
            raise QueueOperationError("capture_terminal_handler_unsupported")
        from breadcrumb_terminal import require_terminal_proof

        try:
            require_terminal_proof(self, binding, record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise QueueOperationError("breadcrumb_terminal_unverified") from exc

    @staticmethod
    def _release_unless_nested(
        registry: Any, lease: OwnerLease, *, nested: bool
    ) -> None:
        """Release the lease unless it belongs to a caller further out."""
        if nested:
            return
        registry.release(lease)

    def _queue_owner_lease(
        self, registry: Any, role: str, scope: str, parent: OwnerLease | None
    ) -> OwnerLease:
        """The lease this queue owner runs under, projected or freshly acquired."""
        if parent is None:
            return registry.acquire(role, scope=scope)
        _require_projectable_parent(registry, parent, role)
        return parent

    @contextmanager
    def queue_owner(
        self,
        *,
        role: Literal["queue-worker", "queue-operator"],
        scope: str,
        parent: OwnerLease | None = None,
    ) -> Iterator[OwnerLease]:
        if role not in _QUEUE_OWNER_PARENT_ROLES:
            raise ValueError("queue owner role must be queue-worker or queue-operator")
        registry = self.ownership_registry()
        nested = parent is not None
        lease = self._queue_owner_lease(registry, role, scope, parent)
        try:
            self._insert_queue_projection(
                lease, role=role, scope=scope, registry=registry
            )
        except BaseException:
            self._release_unless_nested(registry, lease, nested=nested)
            raise
        try:
            yield lease
        finally:
            self._remove_queue_projection(lease)
            self._release_unless_nested(registry, lease, nested=nested)

    def _insert_queue_projection(
        self,
        lease: OwnerLease,
        *,
        role: Literal["queue-worker", "queue-operator"],
        scope: str,
        registry: Any | None = None,
    ) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(lease, OwnerLease):
            raise TypeError("lease must be an OwnerLease")
        domain_role = "worker" if role == "queue-worker" else "operator"
        owners = self.ownership_registry() if registry is None else registry
        with closing(
            self._connect()
        ) as database, sqlite_transactions.begin_immediate(database):
            _reclaim_dead_queue_projections(database, owners, lease)
            database.execute(
                """INSERT INTO queue_ownership(
                       actor_id,domain_role,canonical_role,canonical_scope,
                       owner_token,fencing_epoch,process_id,process_start_identity,
                       acquired_at,heartbeat_at,expires_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lease.actor_id,
                    domain_role,
                    lease.role,
                    lease.scope,
                    lease.token,
                    lease.epoch,
                    lease.process.pid,
                    lease.process.start_identity,
                    timestamp(lease.acquired_at),
                    timestamp(lease.heartbeat_at),
                    timestamp(lease.expires_at),
                ),
            )

    def _remove_queue_projection(self, lease: OwnerLease) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(lease, OwnerLease):
            raise TypeError("lease must be an OwnerLease")
        with closing(
            self._connect()
        ) as database, sqlite_transactions.begin_immediate(database):
            deleted = database.execute(
                """DELETE FROM queue_ownership
                   WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                     AND owner_token=? AND fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    lease.actor_id,
                    lease.role,
                    lease.scope,
                    lease.token,
                    lease.epoch,
                    lease.process.pid,
                    lease.process.start_identity,
                ),
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("queue_owner_fence_lost")

    def _claim_in(self, database, *, owner, lease_seconds, max_attempts, task_filter):
        now = queue_store.utc_now()
        self._delete_stale_source_fences(database)
        self._recover_expired_in(database, now)
        claimable = self._next_claimable_task(database, now, max_attempts, task_filter)
        if claimable is None:
            return None
        row, validation = claimable
        token = sha256_bytes(os.urandom(32))
        expires_at = now + timedelta(seconds=lease_seconds)
        if not take_task_lease(database, row, owner=owner, token=token, now=now,
                               expires_at=expires_at, max_attempts=max_attempts):
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

    def _next_claimable_task(
        self, database: sqlite3.Connection, now: datetime, max_attempts: int, task_filter
    ) -> tuple[sqlite3.Row, PayloadValidation] | None:
        """The next ready task with a readable payload and attempts to spare."""
        register_source_reference(database)
        predicate, target_parameters = task_filter
        while True:
            row = database.execute(
                f"""SELECT * FROM tasks WHERE {V3_WORK_CLAIMABLE}{predicate}
                   ORDER BY priority DESC, available_at, created_at, id LIMIT 1""",
                (max_attempts, timestamp(now), *target_parameters),
            ).fetchone()
            if row is None:
                return None
            claimable = self._claimable_row(database, row, now)
            if claimable is not None:
                return claimable

    def _claimable_row(
        self, database: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> tuple[sqlite3.Row, PayloadValidation] | None:
        """This row when it can be claimed; None means skip it and look again."""
        validation = self._require_valid_task_payload(
            database, row, now=now, parse=True
        )
        if validation is None:
            return None
        if retire_exhausted_history(database, row, now):
            return None
        return row, validation

    def _with_verified_lease(self, lease: QueueLease, action: _LeaseAction) -> object:
        """Run `action` on the leased row, once the lease and payload hold.

        A payload mismatch demotes the task inside the transaction and must be
        reported to the caller afterwards, so the action is simply skipped and
        the refusal raised once the transaction has closed. Whatever the action
        answers is passed back.
        """
        now = queue_store.utc_now()
        result: object = None
        with closing(self._connect()) as database, sqlite_transactions.begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            verified = (
                self._require_valid_task_payload(database, row, now=now, parse=True)
                is not None
            )
            if verified:
                require_lease_input(row, lease)
                result = action(database, row, now)
        if not verified:
            self._raise_payload_mismatch()
        return result

    def _append_compile_event(self, database, row, now, *, event, lease) -> None:
        from compile_progress import ProgressConflict, append_event

        if row["kind"] != "compile":
            raise ProgressConflict("only compile tasks may append compile progress")
        _require_dispatch_execution(row, event)
        require_compile_work_family(database, row, event)
        require_compile_root_work(database, row, event)
        if "artifact" in event.details:
            self.verify_compile_artifact(event)
        compile_progress.require_disjoint_dispatch(
            database, event, partial(self._verify_retained_compile_answer, database))
        self._require_lease_row(database, lease, queue_store.utc_now())
        append_event(database, event)

    def _verify_retained_compile_answer(self, database, answer):
        from llm_wiki.compile_answer_records import decoded_answer, require_same_call

        dispatch = compile_progress.answer_dispatch(database, answer)
        require_same_call(answer, dispatch)
        decoded_answer(dispatch, self.read_compile_artifact(answer))

    def verify_compile_artifact(self, event, *, deadline=None) -> int:
        return verify_retained_artifact(self.state_root, event, deadline=deadline)

    @contextmanager
    def _open_compile_artifact_bytes(self, relative, expected, deadline):
        path = self.state_root / relative
        before = path.lstat()
        check_owner_file_metadata(path, before, before.st_size)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        with os.fdopen(os.open(path, flags), 'rb') as handle:
            _verify_open_artifact(handle, path, before, expected, deadline)
            try:
                yield handle
            finally:
                require_stream_identity(path, handle.fileno(), before)

    def read_compile_artifact(self, event) -> bytes:
        from compile_progress import ProgressCorrupt, artifact_identity

        relative, digest = artifact_identity(event)
        try:
            data = read_stable_owner_file(self.state_root / relative, MAX_RESULT_BYTES)
        except (OSError, ValueError, QueueOperationError) as error:
            raise ProgressCorrupt("compile artifact is unavailable") from error
        if sha256_bytes(data) != digest:
            raise ProgressCorrupt("compile artifact digest mismatch")
        return data

    def _recover_expired_in(self, database: sqlite3.Connection, now: datetime) -> int:
        """Settle every expired lease; a task out of attempts is dead, never stuck ready.

        See `docs/research/2026-09-14-one-second-chance-and-no-stuck-task.md`.
        """
        rows = database.execute(
            "SELECT * FROM tasks WHERE state='leased' AND lease_expires_at<=?",
            (timestamp(now),),
        ).fetchall()
        for row in rows:
            self._settle_expired_lease(database, row, now)
        retire_exhausted_ready(database, now)
        return len(rows)

    def _settle_expired_lease(
        self, database: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        if self._require_valid_task_payload(database, row, now=now, parse=True) is None:
            return
        if is_compile_recovery(row):
            settle_compile_recovery(database, row, now, state='blocked',
                                     outcome='lease_expired', code='compile_recovery_expired')
            return
        self._settle_execution_expired_lease(database, row, now)

    @staticmethod
    def _settle_execution_expired_lease(database, row, now):
        state, error_code = EXPIRED_LEASE_OUTCOMES[
            int(row["attempts"]) >= DEFAULTS.queue_max_attempts
        ]
        database.execute(
            """INSERT INTO attempt_history(
                   task_id,attempt,started_at,finished_at,outcome,error_code
               ) VALUES (?,?,?,?,?,?)""",
            (
                row["id"],
                row["attempts"],
                row["attempt_started_at"] or timestamp(now),
                timestamp(now),
                "lease_expired",
                error_code,
            ),
        )
        changed = database.execute(
            """UPDATE tasks SET state=?,error_code=?,
                   updated_at=?,available_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL,lease_heartbeat_at=NULL,
                   attempt_started_at=NULL WHERE id=? AND state='leased'""",
            (state, error_code, timestamp(now), timestamp(now), row["id"]),
        ).rowcount
        if changed != 1:
            raise QueueOperationError("lease_expiry_failed")
