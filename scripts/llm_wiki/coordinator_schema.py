"""Coordinator v3 schema declarations and supplied-connection inspection."""
from __future__ import annotations

import sqlite3

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.sqlite_contracts import MigrationStatement, OperationalDatabaseContract
from llm_wiki.sqlite_schema_text import normalized_schema_sql

_COORDINATOR_V3_CONTRACT = OperationalDatabaseContract(application_id=0x4C575433)


_COORDINATOR_V3_TABLE_SQL = (
    (
        "blackboard_claim_epochs",
        """CREATE TABLE blackboard_claim_epochs (
            project TEXT NOT NULL CHECK (
                length(CAST(project AS BLOB)) BETWEEN 1 AND 128
            ),
            resource TEXT NOT NULL CHECK (
                length(CAST(resource AS BLOB)) BETWEEN 1 AND 512
            ),
            last_epoch INTEGER NOT NULL CHECK (last_epoch >= 0),
            PRIMARY KEY(project, resource),
            UNIQUE(project, resource, last_epoch)
        )""",
    ),
    (
        "blackboard_claims",
        """CREATE TABLE blackboard_claims (
            project TEXT NOT NULL CHECK (
                length(CAST(project AS BLOB)) BETWEEN 1 AND 128
            ),
            resource TEXT NOT NULL CHECK (
                length(CAST(resource AS BLOB)) BETWEEN 1 AND 512
            ),
            claim_id TEXT NOT NULL CHECK (
                length(claim_id) = 64 AND claim_id NOT GLOB '*[^0-9a-f]*'
            ),
            agent TEXT NOT NULL CHECK (
                length(CAST(agent AS BLOB)) BETWEEN 1 AND 128
            ),
            lease_token TEXT NOT NULL CHECK (
                length(lease_token) = 64 AND lease_token NOT GLOB '*[^0-9a-f]*'
            ),
            fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY(project, resource),
            UNIQUE(project, claim_id, resource),
            FOREIGN KEY(project, resource, fencing_epoch)
                REFERENCES blackboard_claim_epochs(project, resource, last_epoch)
        )""",
    ),
    (
        "transaction",
        """CREATE TABLE "transaction" (
            id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'preparing','prepared','applying','committed','discarded',
                'conflicted','quarantined','aborting','aborted'
            )),
            preconditions_json TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            parent_transaction_id TEXT,
            error_code TEXT,
            artifacts_pruned_at TEXT,
            owner_pid INTEGER,
            intent_id TEXT,
            intent_fence_token TEXT,
            intent_fence_epoch INTEGER,
            capture_link_digest TEXT,
            capture_seal_digest TEXT,
            abort_operation_id TEXT UNIQUE,
            abort_manifest_sha256 TEXT,
            abort_receipt_sha256 TEXT,
            abort_chosen_at TEXT,
            aborted_at TEXT
        )""",
    ),
    (
        "operation",
        """CREATE TABLE "operation" (
            transaction_id TEXT NOT NULL REFERENCES "transaction"(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('create','replace','delete')),
            path TEXT NOT NULL,
            before_hash TEXT NOT NULL,
            after_hash TEXT NOT NULL,
            parent_device INTEGER NOT NULL,
            parent_inode INTEGER NOT NULL,
            applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0,1)),
            PRIMARY KEY(transaction_id, position),
            UNIQUE(transaction_id, path)
        )""",
    ),
    (
        "maintenance_owner_epochs",
        """CREATE TABLE maintenance_owner_epochs (
            role TEXT NOT NULL CHECK (role IN (
                'capture','project','markdown-writer','queue-worker','compile','doctor',
                'nightly','weekly','lsp','queue-operator','repair','runtime-deletion-check'
            )),
            scope TEXT NOT NULL CHECK (length(CAST(scope AS BLOB)) BETWEEN 1 AND 512),
            last_epoch INTEGER NOT NULL CHECK (last_epoch >= 0),
            PRIMARY KEY(role, scope)
        )""",
    ),
    (
        "maintenance_owners",
        """CREATE TABLE maintenance_owners (
            role TEXT NOT NULL CHECK (role IN (
                'capture','project','markdown-writer','queue-worker','compile','doctor',
                'nightly','weekly','lsp','queue-operator','repair','runtime-deletion-check'
            )),
            scope TEXT NOT NULL CHECK (length(CAST(scope AS BLOB)) BETWEEN 1 AND 512),
            actor_id TEXT NOT NULL UNIQUE CHECK (
                length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 256
            ),
            owner_token TEXT NOT NULL UNIQUE CHECK (
                length(CAST(owner_token AS BLOB)) BETWEEN 1 AND 256
            ),
            process_id INTEGER NOT NULL CHECK (process_id > 0),
            process_start_identity TEXT NOT NULL CHECK (
                length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
            ),
            fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            marker_path TEXT CHECK (
                marker_path IS NULL OR length(CAST(marker_path AS BLOB)) BETWEEN 1 AND 4096
            ),
            marker_sha256 TEXT CHECK (
                marker_sha256 IS NULL OR (
                    length(marker_sha256) = 64
                    AND marker_sha256 NOT GLOB '*[^0-9a-f]*'
                )
            ),
            marker_identity_json BLOB CHECK (
                marker_identity_json IS NULL OR length(marker_identity_json) <= 4096
            ),
            PRIMARY KEY(role, scope),
            UNIQUE(role, scope, fencing_epoch),
            UNIQUE(role, scope, actor_id, owner_token, fencing_epoch),
            CHECK (
                (
                    role NOT IN ('compile','nightly','weekly')
                    AND marker_path IS NULL
                    AND marker_sha256 IS NULL
                    AND marker_identity_json IS NULL
                )
                OR
                (
                    role IN ('compile','nightly','weekly')
                    AND marker_path IS NOT NULL
                    AND marker_sha256 IS NOT NULL
                    AND marker_identity_json IS NOT NULL
                )
            )
        )""",
    ),
    (
        "intent_fence_epochs",
        """CREATE TABLE intent_fence_epochs (
            intent_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(intent_id) = 64 AND intent_id NOT GLOB '*[^0-9a-f]*'
            ),
            last_epoch INTEGER NOT NULL CHECK (last_epoch >= 0)
        )""",
    ),
    (
        "intent_fences",
        """CREATE TABLE intent_fences (
            intent_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(intent_id) = 64 AND intent_id NOT GLOB '*[^0-9a-f]*'
            ),
            mode TEXT NOT NULL CHECK (mode IN ('capture','worker','operator')),
            token TEXT NOT NULL UNIQUE CHECK (
                length(CAST(token AS BLOB)) BETWEEN 1 AND 256
            ),
            fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
            canonical_role TEXT NOT NULL CHECK (canonical_role IN (
                'capture','queue-worker','compile','doctor','nightly','weekly',
                'queue-operator','repair'
            )),
            canonical_scope TEXT NOT NULL CHECK (
                length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512
            ),
            canonical_actor_id TEXT NOT NULL CHECK (
                length(CAST(canonical_actor_id AS BLOB)) BETWEEN 1 AND 256
            ),
            canonical_owner_token TEXT NOT NULL CHECK (
                length(CAST(canonical_owner_token AS BLOB)) BETWEEN 1 AND 256
            ),
            canonical_fencing_epoch INTEGER NOT NULL CHECK (
                canonical_fencing_epoch >= 1
            ),
            process_id INTEGER NOT NULL CHECK (process_id > 0),
            process_start_identity TEXT NOT NULL CHECK (
                length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
            ),
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(
                canonical_role,
                canonical_scope,
                canonical_actor_id,
                canonical_owner_token,
                canonical_fencing_epoch
            ) REFERENCES maintenance_owners(
                role,
                scope,
                actor_id,
                owner_token,
                fencing_epoch
            ),
            CHECK (
                (mode = 'capture' AND canonical_role = 'capture')
                OR
                (mode = 'worker' AND canonical_role IN (
                    'queue-worker','compile','doctor','nightly','weekly'
                ))
                OR
                (mode = 'operator' AND canonical_role IN ('queue-operator','repair'))
            )
        )""",
    ),
    (
        "capture_binding_projections",
        """CREATE TABLE capture_binding_projections (
            intent_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(intent_id) = 64 AND intent_id NOT GLOB '*[^0-9a-f]*'
            ),
            task_id TEXT NOT NULL UNIQUE CHECK (
                length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256
            ),
            active_link_digest TEXT NOT NULL CHECK (
                length(active_link_digest) = 64
                AND active_link_digest NOT GLOB '*[^0-9a-f]*'
            ),
            seal_digest TEXT NOT NULL UNIQUE CHECK (
                length(seal_digest) = 64 AND seal_digest NOT GLOB '*[^0-9a-f]*'
            ),
            projected_at TEXT NOT NULL,
            intent_fence_token TEXT NOT NULL CHECK (
                length(CAST(intent_fence_token AS BLOB)) BETWEEN 1 AND 256
            ),
            intent_fence_epoch INTEGER NOT NULL CHECK (intent_fence_epoch >= 1)
        )""",
    ),
    (
        "project_leases",
        """CREATE TABLE project_leases (
            project TEXT PRIMARY KEY,
            lease_token TEXT NOT NULL CHECK (
                length(CAST(lease_token AS BLOB)) BETWEEN 1 AND 256
            ),
            fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
            owner TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            canonical_role TEXT NOT NULL CHECK (
                length(CAST(canonical_role AS BLOB)) BETWEEN 1 AND 64
            ),
            canonical_scope TEXT NOT NULL CHECK (
                length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512
            ),
            actor_id TEXT NOT NULL CHECK (
                length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 256
            ),
            process_id INTEGER NOT NULL CHECK (process_id > 0),
            process_start_identity TEXT NOT NULL CHECK (
                length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
            ),
            FOREIGN KEY(
                canonical_role,
                canonical_scope,
                actor_id,
                lease_token,
                fencing_epoch
            ) REFERENCES maintenance_owners(
                role,
                scope,
                actor_id,
                owner_token,
                fencing_epoch
            )
        )""",
    ),
    (
        "writer_fences",
        """CREATE TABLE writer_fences (
            gate_name TEXT PRIMARY KEY,
            last_epoch INTEGER NOT NULL CHECK (last_epoch >= 0)
        )""",
    ),
    (
        "writer_owners",
        """CREATE TABLE writer_owners (
            gate_name TEXT PRIMARY KEY,
            owner_token TEXT NOT NULL CHECK (
                length(CAST(owner_token AS BLOB)) BETWEEN 1 AND 256
            ),
            process_id INTEGER NOT NULL CHECK (process_id > 0),
            thread_id INTEGER NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
            canonical_role TEXT NOT NULL CHECK (
                length(CAST(canonical_role AS BLOB)) BETWEEN 1 AND 64
            ),
            canonical_scope TEXT NOT NULL CHECK (
                length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512
            ),
            actor_id TEXT NOT NULL CHECK (
                length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 256
            ),
            process_start_identity TEXT NOT NULL CHECK (
                length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
            ),
            FOREIGN KEY(
                canonical_role,
                canonical_scope,
                actor_id,
                owner_token,
                fencing_epoch
            ) REFERENCES maintenance_owners(
                role,
                scope,
                actor_id,
                owner_token,
                fencing_epoch
            )
        )""",
    ),
    (
        "project_checkpoints",
        """CREATE TABLE project_checkpoints (
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
            transaction_id TEXT REFERENCES "transaction"(id),
            state TEXT NOT NULL CHECK (state IN (
                'reserved','prepared','committed','quarantined'
            )),
            PRIMARY KEY(project, sequence),
            UNIQUE(project, occurrence_id),
            UNIQUE(project, idempotency_key)
        )""",
    ),
    (
        "project_checkpoint_attempts",
        """CREATE TABLE project_checkpoint_attempts (
            project TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            attempt_number INTEGER NOT NULL,
            operation_id TEXT NOT NULL UNIQUE,
            parent_operation_id TEXT,
            lease_token TEXT NOT NULL,
            fencing_epoch INTEGER NOT NULL,
            transaction_id TEXT REFERENCES "transaction"(id),
            state TEXT NOT NULL CHECK (state IN (
                'reserved','prepared','committed','quarantined'
            )),
            created_at TEXT NOT NULL,
            PRIMARY KEY(project, sequence, attempt_number),
            FOREIGN KEY(project, sequence)
                REFERENCES project_checkpoints(project, sequence) ON DELETE CASCADE
        )""",
    ),
)


COORDINATOR_V3_SCHEMA_SHA256 = sha256_bytes(
    canonical_json_bytes(
        {
            "tables": [list(item) for item in _COORDINATOR_V3_TABLE_SQL],
            "application_id": _COORDINATOR_V3_CONTRACT.application_id,
            "user_version": _COORDINATOR_V3_CONTRACT.user_version,
        }
    )
)


def _normalized_coordinator_sql(value: str | None) -> str:
    return normalized_schema_sql(value)


def _coordinator_v3_object_matches(
    database: sqlite3.Connection, name: str, sql: str
) -> bool:
    row = database.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None and _normalized_coordinator_sql(
        row[0]
    ) == _normalized_coordinator_sql(sql)


# Indexes on the child keys the history prune deletes against: without them every
# deleted transaction row scans both checkpoint tables (audit 2026-09-27 B-1, owner
# approved; docs/research/2026-09-27-the-checkpoint-keys-are-indexed.md). Optional in
# a v3 database — one built before them is complete without them, and the prune
# creates them before it deletes. They are not part of COORDINATOR_V3_SCHEMA_SHA256,
# which names the table contract recorded adoptions were written against.
_COORDINATOR_V3_INDEX_SQL = (
    (
        "project_checkpoints_transaction",
        "CREATE INDEX project_checkpoints_transaction ON project_checkpoints(transaction_id)",
    ),
    (
        "project_checkpoint_attempts_transaction",
        "CREATE INDEX project_checkpoint_attempts_transaction ON project_checkpoint_attempts(transaction_id)",
    ),
)


_COORDINATOR_V3_INDEXES = dict(_COORDINATOR_V3_INDEX_SQL)


def _known_coordinator_index(database: sqlite3.Connection, kind: object, name: object) -> bool:
    if kind != "index" or name not in _COORDINATOR_V3_INDEXES:
        return False
    row = database.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (name,)).fetchone()
    return row is not None and _normalized_coordinator_sql(row[0]) == _normalized_coordinator_sql(
        _COORDINATOR_V3_INDEXES[str(name)]
    )


def _ensure_coordinator_v3_indexes(database: sqlite3.Connection) -> None:
    for _name, sql in _COORDINATOR_V3_INDEX_SQL:
        database.execute(sql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1))


def _schema_objects(database: sqlite3.Connection) -> set[tuple[str, str]]:
    rows = database.execute("SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
    return {(str(row[0]), str(row[1])) for row in rows}


def _coordinator_v3_tables_match(database: sqlite3.Connection) -> bool:
    return all(_coordinator_v3_object_matches(database, name, sql) for name, sql in _COORDINATOR_V3_TABLE_SQL)


_COORDINATOR_V3_TABLES = frozenset(("table", name) for name, _sql in _COORDINATOR_V3_TABLE_SQL)


def _tables_of(objects: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return {item for item in objects if item[0] == "table"}


def _only_known_indexes(database: sqlite3.Connection, others: set[tuple[str, str]]) -> bool:
    return all(_known_coordinator_index(database, kind, name) for kind, name in others)


def _coordinator_v3_schema_complete(database: sqlite3.Connection) -> bool:
    """Exactly the v3 tables, each as declared, plus only the known optional indexes."""
    objects = _schema_objects(database)
    tables = _tables_of(objects)
    if tables != _COORDINATOR_V3_TABLES:
        return False
    if not _only_known_indexes(database, objects - tables):
        return False
    return _coordinator_v3_tables_match(database)


def _coordinator_v3_statements() -> tuple[MigrationStatement, ...]:
    return tuple(
        MigrationStatement(
            name=f"create_{name}",
            sql=sql,
            completed=lambda database, expected_name=name, expected_sql=sql: (
                _coordinator_v3_object_matches(
                    database, expected_name, expected_sql
                )
            ),
        )
        for name, sql in _COORDINATOR_V3_TABLE_SQL
    )
