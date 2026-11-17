"""Markdown coordinator foundation: database connection, ownership registry and intent fences."""

from __future__ import annotations

import contextlib
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from iso_time import utc_text
from llm_wiki import markdown_store, sqlite_transactions
from llm_wiki.coordinator_schema import (
    COORDINATOR_V3_CONTRACT,
)
from llm_wiki.coordinator_validation import (
    require_coordinator_v3_openable,
    validate_coordinator_v3_database,
)
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.markdown_records import (
    IntentFence,
)
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_contracts import (
    quote_sqlite_identifier,
)
from llm_wiki.sqlite_deadlines import (
    operational_deadline_active,
    refresh_operational_deadline,
)
from reliable_memory import (
    open_operational_db,
    set_owner_only,
    validate_state_root,
)

# An intent fence's own length; a holder doing slow work renews it
# (`heartbeat_intent_fence`).
INTENT_FENCE_SECONDS = 30


_COORDINATOR_V2_SCHEMA_SQL = """
                    CREATE TABLE IF NOT EXISTS "transaction" (
                        id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN (
                            'preparing', 'prepared', 'applying', 'committed',
                            'discarded', 'conflicted', 'quarantined'
                        )),
                        preconditions_json TEXT NOT NULL,
                        plan_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        parent_transaction_id TEXT,
                        error_code TEXT,
                        artifacts_pruned_at TEXT,
                        owner_pid INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS "operation" (
                        transaction_id TEXT NOT NULL REFERENCES "transaction"(id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('create', 'replace', 'delete')),
                        path TEXT NOT NULL,
                        before_hash TEXT NOT NULL,
                        after_hash TEXT NOT NULL,
                        parent_device INTEGER NOT NULL,
                        parent_inode INTEGER NOT NULL,
                        applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
                        PRIMARY KEY (transaction_id, position),
                        UNIQUE (transaction_id, path)
                    );
                    CREATE TABLE IF NOT EXISTS project_leases (
                        project TEXT PRIMARY KEY,
                        lease_token TEXT NOT NULL,
                        fencing_epoch INTEGER NOT NULL,
                        owner TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS project_checkpoints (
                        project TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        occurrence_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        lease_token TEXT NOT NULL,
                        fencing_epoch INTEGER NOT NULL,
                        operation_id TEXT NOT NULL UNIQUE,
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        parent_operation_id TEXT,
                        transaction_id TEXT,
                        state TEXT NOT NULL CHECK (state IN (
                            'reserved', 'prepared', 'committed', 'quarantined'
                        )),
                        PRIMARY KEY (project, sequence),
                        UNIQUE (project, occurrence_id),
                        UNIQUE (project, idempotency_key)
                    );
                    CREATE TABLE IF NOT EXISTS project_checkpoint_attempts (
                        project TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        attempt_number INTEGER NOT NULL,
                        operation_id TEXT NOT NULL UNIQUE,
                        parent_operation_id TEXT,
                        lease_token TEXT NOT NULL,
                        fencing_epoch INTEGER NOT NULL,
                        transaction_id TEXT,
                        state TEXT NOT NULL CHECK (state IN (
                            'reserved', 'prepared', 'committed', 'quarantined'
                        )),
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (project, sequence, attempt_number)
                    );
                    CREATE TABLE IF NOT EXISTS writer_owners (
                        gate_name TEXT PRIMARY KEY,
                        owner_token TEXT NOT NULL,
                        process_id INTEGER NOT NULL,
                        thread_id INTEGER NOT NULL,
                        acquired_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        fencing_epoch INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS writer_fences (
                        gate_name TEXT PRIMARY KEY,
                        last_epoch INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS maintenance_owners (
                        owner_name TEXT PRIMARY KEY,
                        owner_token TEXT NOT NULL,
                        process_id INTEGER NOT NULL,
                        acquired_at TEXT NOT NULL
                    );
                    """


def table_column_names(database: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in database.execute(
        f"PRAGMA table_info({quote_sqlite_identifier(table)})"
    )}


def _add_column_if_missing(
    database: sqlite3.Connection, table: str, name: str, declaration: str
) -> None:
    """Databases from earlier stages are widened in place, never rebuilt."""
    if name in table_column_names(database, table):
        return
    database.execute(
        f"ALTER TABLE {quote_sqlite_identifier(table)} "
        f"ADD COLUMN {quote_sqlite_identifier(name)} {declaration}"
    )


def add_writer_owner_columns(database: sqlite3.Connection) -> None:
    for name, declaration in (
        ("heartbeat_at", "TEXT"),
        ("expires_at", "TEXT"),
        ("fencing_epoch", "INTEGER"),
        ("process_start_identity", "TEXT"),
    ):
        _add_column_if_missing(database, "writer_owners", name, declaration)


def _add_operation_columns(database: sqlite3.Connection) -> None:
    for name in ("parent_device", "parent_inode"):
        _add_column_if_missing(database, "operation", name, "INTEGER")


def _add_transaction_columns(database: sqlite3.Connection) -> None:
    for name in ("parent_transaction_id", "error_code", "artifacts_pruned_at"):
        _add_column_if_missing(database, "transaction", name, "TEXT")
    _add_column_if_missing(database, "transaction", "owner_pid", "INTEGER")


def _add_checkpoint_columns(database: sqlite3.Connection) -> None:
    _add_column_if_missing(
        database,
        "project_checkpoints",
        "attempt_number",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _add_column_if_missing(
        database, "project_checkpoints", "parent_operation_id", "TEXT"
    )


def _backfill_checkpoint_operations(database: sqlite3.Connection) -> None:
    """Rows written before operation identity existed get a derived one."""
    if "operation_id" in table_column_names(database, "project_checkpoints"):
        return
    database.execute("ALTER TABLE project_checkpoints ADD COLUMN operation_id TEXT")
    for row in database.execute(
        "SELECT project, sequence, event_json FROM project_checkpoints"
    ).fetchall():
        _set_migrated_operation_id(database, row)
    database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS project_checkpoint_operation "
        "ON project_checkpoints(operation_id)"
    )


def _set_migrated_operation_id(
    database: sqlite3.Connection, row: sqlite3.Row
) -> None:
    event_hash = sha256_bytes(row["event_json"].encode("utf-8"))
    database.execute(
        "UPDATE project_checkpoints SET operation_id = ? "
        "WHERE project = ? AND sequence = ?",
        (
            f"project:{row['project']}:{row['sequence']}:migrated:{event_hash}",
            row["project"],
            row["sequence"],
        ),
    )


def _require_matching_intent(binding: object, intent_fence: object) -> None:
    if (
        not isinstance(intent_fence, IntentFence)
        or binding.intent_id != intent_fence.intent_id
    ):
        raise ValueError("intent fence does not match capture binding")


def require_live_intent_fence(
    database: sqlite3.Connection, intent_fence: object, now: datetime
) -> None:
    row = database.execute(
        """SELECT 1 FROM intent_fences WHERE intent_id=? AND mode=?
           AND token=? AND fencing_epoch=? AND expires_at>?""",
        (
            intent_fence.intent_id,
            intent_fence.mode,
            intent_fence.token,
            intent_fence.epoch,
            utc_text(now),
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("intent_fence_lost")


def _project_binding_row(
    database: sqlite3.Connection,
    binding: object,
    intent_fence: object,
    now: datetime,
) -> None:
    """Projecting the same binding twice is a no-op; a different one conflicts."""
    expected = _binding_projection(binding, intent_fence)
    existing = database.execute(
        "SELECT * FROM capture_binding_projections WHERE intent_id=?",
        (binding.intent_id,),
    ).fetchone()
    if existing is not None:
        _require_same_binding(existing, expected)
        return
    _insert_binding_projection(database, binding, intent_fence, now)


def _binding_projection(binding: object, intent_fence: object) -> tuple[object, ...]:
    return (
        binding.task_id,
        binding.active_digest,
        binding.seal_digest,
        intent_fence.token,
        intent_fence.epoch,
    )


def _require_same_binding(
    existing: sqlite3.Row, expected: tuple[object, ...]
) -> None:
    current = (
        existing["task_id"],
        existing["active_link_digest"],
        existing["seal_digest"],
        existing["intent_fence_token"],
        existing["intent_fence_epoch"],
    )
    if current != expected:
        raise RuntimeError("capture_binding_conflict")


def _insert_binding_projection(
    database: sqlite3.Connection,
    binding: object,
    intent_fence: object,
    now: datetime,
) -> None:
    inserted = database.execute(
        """INSERT INTO capture_binding_projections(
               intent_id,task_id,active_link_digest,seal_digest,
               projected_at,intent_fence_token,intent_fence_epoch
           ) VALUES (?,?,?,?,?,?,?)""",
        (
            binding.intent_id,
            binding.task_id,
            binding.active_digest,
            binding.seal_digest,
            utc_text(now),
            intent_fence.token,
            intent_fence.epoch,
        ),
    ).rowcount
    if inserted != 1:
        raise RuntimeError("capture_binding_projection_failed")


def _relax_legacy_state_constraint(database: sqlite3.Connection) -> None:
    """Stage 2 Task 2 shipped a narrower state constraint.

    Preserve those databases while allowing their rows to enter recovery-only
    states.
    """
    schema = database.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transaction'"
    ).fetchone()
    if schema is not None and "conflicted" not in (schema["sql"] or ""):
        database.execute("PRAGMA ignore_check_constraints = ON")


class MarkdownCoordinatorFoundation:
    @classmethod
    def from_v3_candidate(
        cls, path: Path, *, state_root: Path
    ) -> Self:
        validate_coordinator_v3_database(path, state_root=state_root)
        return cls._from_validated_v3_database(path, state_root=state_root)

    @classmethod
    def from_v3_active(cls, path: Path, *, state_root: Path, deadline: float | None = None) -> Self:
        require_coordinator_v3_openable(path, state_root=state_root, deadline=deadline)
        return cls._from_validated_v3_database(path, state_root=state_root)

    @classmethod
    def _from_validated_v3_database(cls, path: Path, *, state_root: Path) -> Self:
        coordinator = cls.__new__(cls)
        coordinator.vault = Path(state_root).resolve()
        coordinator.state_root = Path(state_root)
        coordinator.run_root = Path(path).parent
        coordinator.transaction_root = coordinator.run_root / "transactions"
        coordinator.database_path = Path(path)
        coordinator._local = threading.local()
        coordinator._database_contract = COORDINATOR_V3_CONTRACT
        return coordinator

    def __init__(self, vault: Path, state_root: Path, *, deadline: float | None = None):
        operational_deadline_active(deadline)
        self.vault = Path(vault).resolve(strict=True)
        if not self.vault.is_dir():
            raise ValueError(f"vault is not a directory: {self.vault}")
        self.state_root = Path(state_root)
        validate_state_root(self.state_root)
        self.run_root = self.state_root / "run"
        self.transaction_root = self.run_root / "transactions"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.transaction_root.mkdir(parents=True, exist_ok=True)
        set_owner_only(self.run_root, 0o700)
        set_owner_only(self.transaction_root, 0o700)
        self.database_path = self.run_root / "markdown-transactions.sqlite3"
        self._local = threading.local()
        self._initialize_database(deadline=deadline)

    @contextlib.contextmanager
    def _connect(
        self, *, busy_ms: int | None = None, deadline: float | None = None
    ) -> Iterator[sqlite3.Connection]:
        database = open_operational_db(
            self.database_path,
            busy_ms=DEFAULTS.markdown_busy_ms if busy_ms is None else busy_ms,
            contract=getattr(self, "_database_contract", None),
            deadline=deadline,
        )
        try:
            # Preserve this legacy context manager's implicit commit/rollback API.
            database.isolation_level = "DEFERRED"
            _relax_legacy_state_constraint(database)
            with database:
                yield database
                refresh_operational_deadline(database, DEFAULTS.markdown_busy_ms, deadline)
        finally:
            database.close()
            operational_deadline_active(deadline)

    def _ownership_registry(self) -> object:
        from operational_ownership import OwnershipRegistry

        if getattr(self, "_database_contract", None) != COORDINATOR_V3_CONTRACT:
            return OwnershipRegistry(self.state_root)
        return OwnershipRegistry.from_adopted_database(
            self.state_root, self.database_path
        )

    @staticmethod
    def _validate_intent_fence_owner(
        owner: OwnerLease, mode: Literal["capture", "worker", "operator"]
    ) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(owner, OwnerLease):
            raise TypeError("owner must be an OwnerLease")
        allowed = {
            "capture": {"capture"},
            "worker": {"queue-worker", "compile", "doctor", "nightly", "weekly"},
            "operator": {"queue-operator", "repair"},
        }
        if mode not in allowed or owner.role not in allowed[mode]:
            raise ValueError("owner cannot acquire the requested intent fence")

    def acquire_intent_fence(
        self,
        intent_id: str,
        *,
        mode: Literal["capture", "worker", "operator"],
        owner: OwnerLease,
    ) -> IntentFence:
        self._validate_intent_fence_owner(owner, mode)
        if re.fullmatch(r"[0-9a-f]{64}", intent_id) is None:
            raise ValueError("intent_id must be lowercase 64-hex")
        registry = self._ownership_registry()
        token = uuid.uuid4().hex
        with self._connect() as database, sqlite_transactions.begin_immediate(database):
            # Judge authority after SQLite has admitted this write transaction.
            now = markdown_store.utc_now()
            expires_at = min(owner.expires_at, now + timedelta(seconds=INTENT_FENCE_SECONDS))
            registry.require(database, owner)
            self._reclaim_dead_intent_fence(database, registry, intent_id)
            epoch = int(
                database.execute(
                    """INSERT INTO intent_fence_epochs(intent_id,last_epoch)
                       VALUES (?,1)
                       ON CONFLICT(intent_id) DO UPDATE
                       SET last_epoch=intent_fence_epochs.last_epoch+1
                       RETURNING last_epoch""",
                    (intent_id,),
                ).fetchone()[0]
            )
            inserted = database.execute(
                """INSERT INTO intent_fences(
                       intent_id,mode,token,fencing_epoch,canonical_role,
                       canonical_scope,canonical_actor_id,canonical_owner_token,
                       canonical_fencing_epoch,process_id,process_start_identity,
                       heartbeat_at,expires_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    intent_id,
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
                    utc_text(now),
                    utc_text(expires_at),
                ),
            ).rowcount
            if inserted != 1:
                raise RuntimeError("intent_fence_failed")
        return IntentFence(intent_id, mode, token, epoch, owner, expires_at)

    @staticmethod
    def _reclaim_dead_intent_fence(
        database: sqlite3.Connection, registry: object, intent_id: str
    ) -> None:
        """Take over one intent's fence row when its holder is provably dead.

        `intent_fences.intent_id` is the primary key, so exactly one row can
        exist per intent, and this refused on the mere presence of that row —
        it had no expiry check at all. The row is keyed by `intent_id`, by
        neither `role` nor `scope`, so the registry, which reclaims a dead
        owner by `(role, scope)`, cannot reach it from a caller holding a
        different owner. `capture_task_fences` hands the *worker's* own owner
        to a fence keyed by the intent, so a worker killed mid-capture-task
        left a row that adoption — which takes `(capture, intent:<id>)` — could
        never reach, and the intent was wedged for good.

        Measured on unmodified code 2026-08-29: a dead worker holding a
        worker-mode fence answered `RuntimeError: intent_fenced` and the row
        was still there after the pass. A dead *capture publisher* holding its
        own fence was already adopted 1 of 1, because publisher and adopter
        take the same owner and the registry deletes the fence as one of that
        owner's projections — so the wedge is the cross-owner case, not the
        publisher case named when `NEW-136` closed.

        The proof is the registry's own `reclaimable_dead_owner`, not a second
        answer to the same question. Doubt refuses closed under the name
        callers already report: a row that cannot be proved dead — a live
        holder, or a probe that cannot settle — leaves `intent_fenced`
        standing. The binding projection goes with the fence and only with the
        fence, exactly the pairing `release_intent_fence` performs, because a
        binding without its fence is a shape violation the coordinator counts.
        """
        row = database.execute(
            "SELECT * FROM intent_fences WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            return
        if not registry.reclaimable_dead_owner(row):
            raise RuntimeError("intent_fenced")
        database.execute(
            """DELETE FROM capture_binding_projections
               WHERE intent_id=? AND intent_fence_token=? AND intent_fence_epoch=?""",
            (intent_id, row["token"], row["fencing_epoch"]),
        )
        database.execute(
            "DELETE FROM intent_fences WHERE intent_id=? AND token=?",
            (intent_id, row["token"]),
        )

    def release_intent_fence(self, fence: IntentFence) -> None:
        if not isinstance(fence, IntentFence):
            raise TypeError("fence must be an IntentFence")
        with self._connect() as database, sqlite_transactions.begin_immediate(database):
            database.execute(
                """DELETE FROM capture_binding_projections
                   WHERE intent_id=? AND intent_fence_token=?
                     AND intent_fence_epoch=?""",
                (fence.intent_id, fence.token, fence.epoch),
            )
            deleted = database.execute(
                """DELETE FROM intent_fences
                   WHERE intent_id=? AND mode=? AND token=? AND fencing_epoch=?
                     AND canonical_role=? AND canonical_scope=?
                     AND canonical_actor_id=? AND canonical_owner_token=?
                     AND canonical_fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    fence.intent_id,
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
                raise RuntimeError("intent_fence_lost")

    def heartbeat_intent_fence(self, fence: IntentFence, owner: OwnerLease) -> None:
        """Push a live intent fence out by its own length, capped by its owner's lease.

        A capture holds this fence while its classifier runs, and nothing renewed
        it: no capture that took longer than 30 seconds ever succeeded. `owner` is
        the fence's owner as last renewed. See
        `docs/research/2026-09-14-a-capture-keeps-its-claim-while-it-asks.md`.
        """
        if not isinstance(fence, IntentFence):
            raise TypeError("fence must be an IntentFence")
        registry = self._ownership_registry()
        with self._connect() as database, sqlite_transactions.begin_immediate(database):
            # Judge authority after SQLite has admitted this write transaction.
            now = markdown_store.utc_now()
            expires_at = min(owner.expires_at, now + timedelta(seconds=INTENT_FENCE_SECONDS))
            registry.require(database, owner)
            renewed = database.execute(
                """UPDATE intent_fences SET heartbeat_at=?, expires_at=?
                   WHERE intent_id=? AND mode=? AND token=? AND fencing_epoch=?
                     AND canonical_owner_token=? AND canonical_fencing_epoch=?
                     AND process_id=? AND process_start_identity=? AND expires_at>?""",
                (
                    utc_text(now),
                    utc_text(expires_at),
                    fence.intent_id,
                    fence.mode,
                    fence.token,
                    fence.epoch,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    utc_text(now),
                ),
            ).rowcount
            if renewed != 1:
                raise RuntimeError("intent_fence_lost")

    def project_capture_binding(
        self, binding: object, *, intent_fence: IntentFence
    ) -> None:
        from llm_wiki.queue_contracts import CaptureTaskBinding

        if not isinstance(binding, CaptureTaskBinding) or binding.seal_digest is None:
            raise ValueError("capture binding must be sealed")
        _require_matching_intent(binding, intent_fence)
        with self._connect() as database, sqlite_transactions.begin_immediate(database):
            # Judge authority after SQLite has admitted this write transaction.
            now = markdown_store.utc_now()
            require_live_intent_fence(database, intent_fence, now)
            _project_binding_row(database, binding, intent_fence, now)

    def _initialize_database(self, *, deadline: float | None = None) -> None:
        with self._connect(deadline=deadline) as database:
            # executescript commits any pending transaction before its script.
            database.executescript("BEGIN IMMEDIATE;\n" + _COORDINATOR_V2_SCHEMA_SQL)
            add_writer_owner_columns(database)
            _add_operation_columns(database)
            _add_transaction_columns(database)
            _backfill_checkpoint_operations(database)
            _add_checkpoint_columns(database)
            database.execute(
                "INSERT OR IGNORE INTO project_checkpoint_attempts "
                "(project, sequence, attempt_number, operation_id, "
                "parent_operation_id, lease_token, fencing_epoch, transaction_id, "
                "state, created_at) "
                "SELECT project, sequence, attempt_number, operation_id, "
                "parent_operation_id, lease_token, fencing_epoch, transaction_id, "
                "state, ? FROM project_checkpoints WHERE operation_id IS NOT NULL",
                (markdown_store.now(),),
            )
            self._backfill_parent_identities(database)

    def _set_transaction_state(
        self, transaction_id: str, state: str, *, error_code: str | None = None
    ) -> None:
        with self._connect() as database, sqlite_transactions.begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            database.execute(
                'UPDATE "transaction" SET state = ?, error_code = ?, updated_at = ? '
                "WHERE id = ?",
                (state, error_code, markdown_store.now(), transaction_id),
            )
