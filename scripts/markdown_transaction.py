"""Recoverable, hash-checked transactions for authoritative Markdown files."""

from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import getpass
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from operational_ownership import OwnerLease

from bounded_io import read_stable_bytes
from claim_tree_manifest import (
    snapshot_claim_tree,
    snapshot_guardrail_sources,
    validate_claim_tree_manifest,
    validate_guardrail_source_manifest,
)
from model_dlp import DLPContentBlocked, DLPPolicyError, require_safe_publication
from reliable_memory import (
    DEFAULTS,
    MigrationStatement,
    OperationalDatabaseContract,
    OperationalDatabaseContractError,
    _set_owner_only,
    begin_immediate,
    canonical_json_bytes,
    durable_publish_file,
    fsync_directory,
    fsync_file,
    open_operational_db,
    open_readonly_operational_db,
    publish_runtime_file,
    read_runtime_bytes,
    restricted_relative_path,
    run_resumable_migration,
    sha256_bytes,
    validate_schema,
    validate_state_root,
)

ChangeKind = Literal["create", "replace", "delete"]
Validator = Callable[[Mapping[str, object]], object]
ABSENT = "absent"
_OVERSIZED_TARGET = "oversized"
_ALLOWED_DIRECTORIES = (
    "knowledge/daily",
    "knowledge/notes",
    "knowledge/projects",
    "knowledge/inbox",
    "knowledge/feedback",
)
_ALLOWED_FILES = {
    "knowledge/guardrails.md",
    "knowledge/index.md",
    "knowledge/log.md",
}
_SCHEMA = Path(__file__).with_name("schemas") / "markdown-transaction-v1.json"
_PROJECT_CHECKPOINT_SCHEMA = (
    Path(__file__).with_name("schemas") / "project-checkpoint-v1.json"
)
_WRITER_LEASE_SECONDS = 30.0
_WRITER_HEARTBEAT_SECONDS = 0.5
_WRITER_WAIT_SECONDS = DEFAULTS.markdown_busy_ms / 1_000
_WRITER_RETRY_BASE_SECONDS = 0.005
_WRITER_RETRY_CAP_SECONDS = 0.05
_ADOPTION_VALIDATION_SECONDS = 30.0
_ADOPTION_VALIDATION_CACHE: set[tuple[object, ...]] = set()
_ADOPTION_VALIDATION_LOCK = threading.Lock()
MAX_KNOWLEDGE_TARGET_BYTES = 64 * 1024 * 1024
MAX_KNOWLEDGE_PATH_BYTES = 512
MAX_KNOWLEDGE_COMPONENT_BYTES = 128
MAX_KNOWLEDGE_DEPTH = 12
_FEEDBACK_JSON_RE = re.compile(r"knowledge/feedback/[0-9a-f]{6,64}\.json")
_BLACKBOARD_JSONL_RE = re.compile(
    r"knowledge/projects/[A-Za-z0-9._-]+/\.blackboard/"
    r"(?:tasks|completed|signals|conflicts)\.jsonl"
)
_SQLITE_INT64_MIN = -(1 << 63)
_SQLITE_INT64_MAX = (1 << 63) - 1
_UINT64_MODULUS = 1 << 64
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

_COORDINATOR_V2_COLUMNS = {
    "transaction": (
        "id",
        "operation_id",
        "request_hash",
        "state",
        "preconditions_json",
        "plan_hash",
        "created_at",
        "updated_at",
        "parent_transaction_id",
        "error_code",
        "artifacts_pruned_at",
        "owner_pid",
    ),
    "operation": (
        "transaction_id",
        "position",
        "kind",
        "path",
        "before_hash",
        "after_hash",
        "parent_device",
        "parent_inode",
        "applied",
    ),
    "project_checkpoints": (
        "project",
        "sequence",
        "occurrence_id",
        "idempotency_key",
        "event_json",
        "lease_token",
        "fencing_epoch",
        "operation_id",
        "attempt_number",
        "parent_operation_id",
        "transaction_id",
        "state",
    ),
    "project_checkpoint_attempts": (
        "project",
        "sequence",
        "attempt_number",
        "operation_id",
        "parent_operation_id",
        "lease_token",
        "fencing_epoch",
        "transaction_id",
        "state",
        "created_at",
    ),
    "writer_fences": ("gate_name", "last_epoch"),
}

_COORDINATOR_V2_SCHEMA_OBJECTS = {
    ("index", "project_checkpoint_operation"),
    ("table", "maintenance_owners"),
    ("table", "operation"),
    ("table", "project_checkpoint_attempts"),
    ("table", "project_checkpoints"),
    ("table", "project_leases"),
    ("table", "transaction"),
    ("table", "writer_fences"),
    ("table", "writer_owners"),
}


def _normalized_coordinator_sql(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _coordinator_v3_object_matches(
    database: sqlite3.Connection, name: str, sql: str
) -> bool:
    row = database.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None and _normalized_coordinator_sql(
        row[0]
    ) == _normalized_coordinator_sql(sql)


def _coordinator_v3_schema_complete(database: sqlite3.Connection) -> bool:
    objects = database.execute(
        """SELECT type, name FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
    ).fetchall()
    expected = {("table", name) for name, _sql in _COORDINATOR_V3_TABLE_SQL}
    return {(str(row[0]), str(row[1])) for row in objects} == expected and all(
        _coordinator_v3_object_matches(database, name, sql)
        for name, sql in _COORDINATOR_V3_TABLE_SQL
    )


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


def _coordinator_migration_error(
    code: str, message: str
) -> OperationalDatabaseContractError:
    error = OperationalDatabaseContractError(message)
    error.code = code
    return error


def _coordinator_table_columns(
    database: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in database.execute(f'PRAGMA table_info("{table}")'))


def _coordinator_table_exists(database: sqlite3.Connection, table: str) -> bool:
    return database.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _validate_coordinator_v2_schema_objects(database: sqlite3.Connection) -> None:
    objects = {
        (str(row[0]), str(row[1]))
        for row in database.execute(
            """SELECT type, name FROM sqlite_schema
               WHERE name NOT LIKE 'sqlite_%'"""
        )
    }
    if not objects <= _COORDINATOR_V2_SCHEMA_OBJECTS:
        raise _coordinator_migration_error(
            "coordinator_v2_schema_unknown",
            "coordinator v2 source has unknown schema objects",
        )


def _repair_partial_coordinator_v3_schema(
    database: sqlite3.Connection, *, allow_populated_rebuild: bool
) -> None:
    if _coordinator_v3_schema_complete(database):
        return
    _require_unpublished_coordinator(database)
    objects = _coordinator_schema_objects(database)
    if not objects or _coordinator_schema_exact(database, objects):
        return
    _require_rebuildable_schema(database, objects, allow_populated_rebuild)
    _drop_coordinator_objects(database, objects)


def _require_unpublished_coordinator(database: sqlite3.Connection) -> None:
    """A published database is never repaired in place."""
    application_id = int(database.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(database.execute("PRAGMA user_version").fetchone()[0])
    if (application_id, user_version) != (0, 0):
        raise _coordinator_migration_error(
            "coordinator_v3_schema_conflict",
            "published coordinator v3 database has an incomplete schema",
        )


def _coordinator_schema_objects(
    database: sqlite3.Connection,
) -> list[tuple[str, str]]:
    return database.execute(
        """SELECT type, name FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
    ).fetchall()


def _coordinator_schema_exact(
    database: sqlite3.Connection, objects: list[tuple[str, str]]
) -> bool:
    expected = {name: sql for name, sql in _COORDINATOR_V3_TABLE_SQL}
    return all(
        kind == "table"
        and name in expected
        and _coordinator_v3_object_matches(database, str(name), expected[str(name)])
        for kind, name in objects
    )


def _require_rebuildable_schema(
    database: sqlite3.Connection,
    objects: list[tuple[str, str]],
    allow_populated_rebuild: bool,
) -> None:
    if allow_populated_rebuild:
        return
    if _coordinator_schema_populated(database, objects):
        raise _coordinator_migration_error(
            "coordinator_v3_source_conflict",
            "fresh coordinator v3 initialization found existing rows",
        )


def _coordinator_schema_populated(
    database: sqlite3.Connection, objects: list[tuple[str, str]]
) -> bool:
    """A table that cannot even be read counts as populated: it is not ours."""
    return any(
        _table_has_rows(database, str(name)) for kind, name in objects if kind == "table"
    )


def _table_has_rows(database: sqlite3.Connection, name: str) -> bool:
    try:
        return (
            database.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone() is not None
        )
    except sqlite3.DatabaseError:
        return True


def _drop_coordinator_objects(
    database: sqlite3.Connection, objects: list[tuple[str, str]]
) -> None:
    database.execute("PRAGMA foreign_keys=OFF")
    try:
        for kind in ("trigger", "index", "view", "table"):
            _drop_objects_of_kind(database, objects, kind)
    finally:
        _restore_foreign_keys(database)


def _drop_objects_of_kind(
    database: sqlite3.Connection, objects: list[tuple[str, str]], kind: str
) -> None:
    keyword = kind.upper()
    for name in _object_names_of_kind(objects, kind):
        with begin_immediate(database):
            database.execute(f'DROP {keyword} "{name}"')


def _object_names_of_kind(objects: list[tuple[str, str]], kind: str) -> list[str]:
    return [str(name) for object_kind, name in reversed(objects) if object_kind == kind]


def _restore_foreign_keys(database: sqlite3.Connection) -> None:
    database.execute("PRAGMA foreign_keys=ON")
    if database.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise _coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 candidate did not restore foreign keys",
        )


def _coordinator_v2_project_history(
    source: sqlite3.Connection,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    checkpoints = _coordinator_v2_checkpoint_rows(source)
    attempts = _coordinator_v2_attempt_rows(source)
    if bool(checkpoints) != bool(attempts):
        raise _coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint and attempt history are incomplete",
        )
    _require_complete_checkpoint_history(checkpoints, attempts)
    return checkpoints, attempts


def _coordinator_v2_checkpoint_rows(
    source: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    if not _coordinator_table_exists(source, "project_checkpoints"):
        return []
    expected = _require_v2_history_columns(
        source, "project_checkpoints", "coordinator v2 checkpoint schema is incomplete"
    )
    return [
        tuple(row[column] for column in expected)
        for row in source.execute(
            "SELECT * FROM project_checkpoints ORDER BY project, sequence"
        )
    ]


def _coordinator_v2_attempt_rows(
    source: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    if not _coordinator_table_exists(source, "project_checkpoint_attempts"):
        return []
    expected = _require_v2_history_columns(
        source,
        "project_checkpoint_attempts",
        "coordinator v2 checkpoint attempt schema is incomplete",
    )
    return [
        tuple(row[column] for column in expected)
        for row in source.execute(
            """SELECT * FROM project_checkpoint_attempts
               ORDER BY project, sequence, attempt_number"""
        )
    ]


def _require_v2_history_columns(
    source: sqlite3.Connection, table: str, message: str
) -> tuple[str, ...]:
    expected = _COORDINATOR_V2_COLUMNS[table]
    if not set(expected).issubset(_coordinator_table_columns(source, table)):
        raise _coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete", message
        )
    return expected


def _require_complete_checkpoint_history(
    checkpoints: list[tuple[object, ...]], attempts: list[tuple[object, ...]]
) -> None:
    """Every checkpoint owns its whole attempt lineage, and nothing is orphaned."""
    by_checkpoint: dict[tuple[object, object], list[tuple[object, ...]]] = {}
    for attempt in attempts:
        by_checkpoint.setdefault((attempt[0], attempt[1]), []).append(attempt)
    for checkpoint in checkpoints:
        history = by_checkpoint.pop((checkpoint[0], checkpoint[1]), [])
        _require_checkpoint_attempts(checkpoint, history)
    if by_checkpoint:
        raise _coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 attempt history has no checkpoint",
        )


def _require_checkpoint_attempts(
    checkpoint: tuple[object, ...], history: list[tuple[object, ...]]
) -> None:
    _require_attempt_numbering(checkpoint, history)
    for position, attempt in enumerate(history):
        _require_attempt_lineage(position, attempt, history)
    _require_checkpoint_matches_latest(checkpoint, history[-1])


def _require_attempt_numbering(
    checkpoint: tuple[object, ...], history: list[tuple[object, ...]]
) -> None:
    current_attempt = checkpoint[8]
    if (
        type(current_attempt) is not int
        or current_attempt < 1
        or [attempt[2] for attempt in history] != list(range(1, current_attempt + 1))
    ):
        raise _coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint attempts are not complete",
        )


def _require_attempt_lineage(
    position: int, attempt: tuple[object, ...], history: list[tuple[object, ...]]
) -> None:
    _require_attempt_timestamp(attempt)
    expected_parent = None if position == 0 else history[position - 1][3]
    if attempt[4] != expected_parent or _unquarantined_middle(position, attempt, history):
        raise _coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint attempt lineage is inconsistent",
        )


def _require_attempt_timestamp(attempt: tuple[object, ...]) -> None:
    if not isinstance(attempt[9], str) or not attempt[9]:
        raise _coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint attempt lacks its original timestamp",
        )


def _unquarantined_middle(
    position: int, attempt: tuple[object, ...], history: list[tuple[object, ...]]
) -> bool:
    """Every attempt but the last must have been quarantined."""
    return position < len(history) - 1 and attempt[8] != "quarantined"


def _require_checkpoint_matches_latest(
    checkpoint: tuple[object, ...], latest: tuple[object, ...]
) -> None:
    if (
        checkpoint[7],
        checkpoint[9],
        checkpoint[5],
        checkpoint[6],
        checkpoint[10],
        checkpoint[11],
    ) != (latest[3], latest[4], latest[5], latest[6], latest[7], latest[8]):
        raise _coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint does not match its latest attempt",
        )


def _coordinator_v2_rows(
    source: sqlite3.Connection,
) -> tuple[dict[str, list[tuple[object, ...]]], dict[str, int]]:
    _require_no_v2_ownership(source)
    transaction_rows = _coordinator_v2_transaction_rows(source)
    operation_rows = _coordinator_v2_operation_rows(source)
    transaction_ids = {row[0] for row in transaction_rows}
    _require_v2_operations_bound(operation_rows, transaction_ids)
    checkpoints, attempts = _coordinator_v2_project_history(source)
    _require_v2_checkpoint_transactions(checkpoints, transaction_ids)
    writer_fences = _coordinator_v2_fence_rows(source)
    rows = {
        "transaction": transaction_rows,
        "operation": operation_rows,
        "project_checkpoints": checkpoints,
        "project_checkpoint_attempts": attempts,
        "writer_fences": writer_fences,
    }
    summary = {
        "transactions": len(transaction_rows),
        "operations": len(operation_rows),
        "project_checkpoints": len(checkpoints),
        "project_checkpoint_attempts": len(attempts),
        "writer_fences": len(writer_fences),
    }
    return rows, summary


_V2_OWNERSHIP_TABLES = ("project_leases", "writer_owners", "maintenance_owners")

_V2_TRANSACTION_STATES = frozenset(
    {
        "preparing",
        "prepared",
        "applying",
        "committed",
        "discarded",
        "conflicted",
        "quarantined",
    }
)


def _require_no_v2_ownership(source: sqlite3.Connection) -> None:
    """Live ownership cannot be canonicalized, so it must not be carried over."""
    if any(_v2_table_populated(source, table) for table in _V2_OWNERSHIP_TABLES):
        raise _coordinator_migration_error(
            "coordinator_v2_ambiguous_ownership",
            "coordinator v2 contains ownership that cannot be canonicalized",
        )


def _v2_table_populated(source: sqlite3.Connection, table: str) -> bool:
    if not _coordinator_table_exists(source, table):
        return False
    return source.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None


def _coordinator_v2_transaction_rows(
    source: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    columns = _coordinator_table_columns(source, "transaction")
    if not set(_COORDINATOR_V2_COLUMNS["transaction"][:8]).issubset(columns):
        raise _coordinator_migration_error(
            "coordinator_v2_schema_incomplete",
            "coordinator v2 transaction schema is incomplete",
        )
    select = ", ".join(_v2_transaction_select(columns))
    rows = [
        tuple(row)
        for row in source.execute(f'SELECT {select} FROM "transaction" ORDER BY id')
    ]
    _require_v2_transaction_states(rows)
    return rows


def _v2_transaction_select(columns: object) -> list[str]:
    """A column the source never had is selected as NULL, not skipped."""
    return [
        column if column in columns else f"NULL AS {column}"
        for column in _COORDINATOR_V2_COLUMNS["transaction"]
    ]


def _require_v2_transaction_states(rows: list[tuple[object, ...]]) -> None:
    if any(row[3] not in _V2_TRANSACTION_STATES for row in rows):
        raise _coordinator_migration_error(
            "coordinator_v2_transaction_invalid",
            "coordinator v2 transaction state is invalid",
        )


def _coordinator_v2_operation_rows(
    source: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    columns = _COORDINATOR_V2_COLUMNS["operation"]
    if not set(columns).issubset(_coordinator_table_columns(source, "operation")):
        raise _coordinator_migration_error(
            "coordinator_v2_schema_incomplete",
            "coordinator v2 operation schema is incomplete",
        )
    return [
        tuple(row[column] for column in columns)
        for row in source.execute(
            'SELECT * FROM "operation" ORDER BY transaction_id, position'
        )
    ]


def _require_v2_operations_bound(
    rows: list[tuple[object, ...]], transaction_ids: set[object]
) -> None:
    if any(_v2_operation_invalid(row, transaction_ids) for row in rows):
        raise _coordinator_migration_error(
            "coordinator_v2_operation_invalid",
            "coordinator v2 operation history is incomplete",
        )


def _v2_operation_invalid(
    row: tuple[object, ...], transaction_ids: set[object]
) -> bool:
    if row[0] not in transaction_ids:
        return True
    if row[2] not in {"create", "replace", "delete"}:
        return True
    return row[6] is None or row[7] is None or row[8] not in {0, 1}


def _require_v2_checkpoint_transactions(
    checkpoints: list[tuple[object, ...]], transaction_ids: set[object]
) -> None:
    if any(
        row[10] is not None and row[10] not in transaction_ids for row in checkpoints
    ):
        raise _coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint transaction is missing",
        )


def _coordinator_v2_fence_rows(
    source: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    if not _coordinator_table_exists(source, "writer_fences"):
        return []
    columns = _COORDINATOR_V2_COLUMNS["writer_fences"]
    if not set(columns).issubset(_coordinator_table_columns(source, "writer_fences")):
        raise _coordinator_migration_error(
            "coordinator_v2_schema_incomplete",
            "coordinator v2 writer fence schema is incomplete",
        )
    return [
        tuple(row[column] for column in columns)
        for row in source.execute("SELECT * FROM writer_fences ORDER BY gate_name")
    ]


_V3_KEY_COLUMNS = {
    "transaction": ("id",),
    "operation": ("transaction_id", "position"),
    "project_checkpoints": ("project", "sequence"),
    "project_checkpoint_attempts": ("project", "sequence", "attempt_number"),
}


def _coordinator_v3_insert_statement(
    *, table: str, columns: tuple[str, ...], values: tuple[object, ...], identity: object
) -> MigrationStatement:
    placeholders = ", ".join("?" for _column in columns)
    column_sql = ", ".join(f'"{column}"' for column in columns)
    key_columns = _V3_KEY_COLUMNS.get(table, ("gate_name",))
    key_values = values[: len(key_columns)]
    where = " AND ".join(f'"{column}"=?' for column in key_columns)
    digest = sha256_bytes(repr(identity).encode("utf-8"))[:16]

    def completed(database: sqlite3.Connection) -> bool:
        row = database.execute(
            f'SELECT {column_sql} FROM "{table}" WHERE {where}', key_values
        ).fetchone()
        return row is not None and tuple(row) == values

    return MigrationStatement(
        name=f"migrate_{table}_{digest}",
        sql=f'INSERT OR IGNORE INTO "{table}" ({column_sql}) VALUES ({placeholders})',
        parameters=values,
        completed=completed,
    )


def _coordinator_v2_migration_statements(
    source: sqlite3.Connection,
) -> tuple[tuple[MigrationStatement, ...], dict[str, int]]:
    _validate_coordinator_v2_schema_objects(source)
    rows, summary = _coordinator_v2_rows(source)
    statements: list[MigrationStatement] = []
    for table in (
        "transaction",
        "operation",
        "project_checkpoints",
        "project_checkpoint_attempts",
        "writer_fences",
    ):
        columns = _COORDINATOR_V2_COLUMNS[table]
        for values in rows[table]:
            statements.append(
                _coordinator_v3_insert_statement(
                    table=table,
                    columns=columns,
                    values=values,
                    identity=values[:3],
                )
            )
    return tuple(statements), summary


def _coordinator_v3_row_counts(database: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(database.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        for name, _sql in _COORDINATOR_V3_TABLE_SQL
    }


def _coordinator_v2_reconciliation_complete(
    database: sqlite3.Connection,
    statements: tuple[MigrationStatement, ...],
    summary: Mapping[str, int],
) -> bool:
    expected = {
        "transaction": summary["transactions"],
        "operation": summary["operations"],
        "project_checkpoints": summary["project_checkpoints"],
        "project_checkpoint_attempts": summary["project_checkpoint_attempts"],
        "writer_fences": summary["writer_fences"],
    }
    empty = {
        "blackboard_claim_epochs",
        "blackboard_claims",
        "capture_binding_projections",
        "intent_fence_epochs",
        "intent_fences",
        "maintenance_owner_epochs",
        "maintenance_owners",
        "project_leases",
        "writer_owners",
    }
    new_transaction_fields = (
        "intent_id",
        "intent_fence_token",
        "intent_fence_epoch",
        "capture_link_digest",
        "capture_seal_digest",
        "abort_operation_id",
        "abort_manifest_sha256",
        "abort_receipt_sha256",
        "abort_chosen_at",
        "aborted_at",
    )
    populated_new_fields = " OR ".join(
        f'"{field}" IS NOT NULL' for field in new_transaction_fields
    )
    checks = (
        _migration_statements_complete(database, statements),
        _coordinator_counts_match(database, expected),
        _coordinator_tables_empty(database, empty),
        _coordinator_transaction_fields_empty(database, populated_new_fields),
    )
    return all(checks)


def _migration_statements_complete(
    database: sqlite3.Connection, statements: tuple[MigrationStatement, ...]
) -> bool:
    return all(statement.completed(database) for statement in statements)


def _coordinator_counts_match(
    database: sqlite3.Connection, expected: Mapping[str, int]
) -> bool:
    return all(
        database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == count
        for table, count in expected.items()
    )


def _coordinator_tables_empty(
    database: sqlite3.Connection, tables: set[str]
) -> bool:
    return all(
        database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
        for table in tables
    )


def _coordinator_transaction_fields_empty(
    database: sqlite3.Connection, populated_fields: str
) -> bool:
    return database.execute(
        f'SELECT COUNT(*) FROM "transaction" WHERE {populated_fields}'
    ).fetchone()[0] == 0


def _coordinator_v3_base_cross_table_invariant(database: sqlite3.Connection) -> bool:
    owner_projection_violations = database.execute(
        """SELECT COUNT(*) FROM (
               SELECT process_id, process_start_identity, canonical_role,
                      canonical_scope, actor_id, owner_token, fencing_epoch
               FROM writer_owners
               UNION ALL
               SELECT process_id, process_start_identity, canonical_role,
                      canonical_scope, actor_id, lease_token, fencing_epoch
               FROM project_leases
               UNION ALL
               SELECT process_id, process_start_identity, canonical_role,
                      canonical_scope, canonical_actor_id, canonical_owner_token,
                      canonical_fencing_epoch
               FROM intent_fences
           ) AS projection
           LEFT JOIN maintenance_owners AS owner
             ON owner.role=projection.canonical_role
            AND owner.scope=projection.canonical_scope
            AND owner.actor_id=projection.actor_id
            AND owner.owner_token=projection.owner_token
            AND owner.fencing_epoch=projection.fencing_epoch
           WHERE owner.role IS NULL
              OR owner.process_id != projection.process_id
              OR owner.process_start_identity != projection.process_start_identity"""
    ).fetchone()[0]
    capture_projection_violations = database.execute(
        """SELECT COUNT(*) FROM capture_binding_projections AS binding
           LEFT JOIN intent_fences AS fence
             ON fence.intent_id=binding.intent_id
            AND fence.token=binding.intent_fence_token
            AND fence.fencing_epoch=binding.intent_fence_epoch
           WHERE fence.intent_id IS NULL"""
    ).fetchone()[0]
    owner_epoch_violations = database.execute(
        """SELECT COUNT(*) FROM maintenance_owners AS owner
           LEFT JOIN maintenance_owner_epochs AS epoch
             ON epoch.role=owner.role AND epoch.scope=owner.scope
           WHERE epoch.role IS NULL OR epoch.last_epoch < owner.fencing_epoch"""
    ).fetchone()[0]
    intent_epoch_violations = database.execute(
        """SELECT COUNT(*) FROM intent_fences AS fence
           LEFT JOIN intent_fence_epochs AS epoch ON epoch.intent_id=fence.intent_id
           WHERE epoch.intent_id IS NULL OR epoch.last_epoch < fence.fencing_epoch"""
    ).fetchone()[0]
    return not any(
        (
            owner_projection_violations,
            capture_projection_violations,
            owner_epoch_violations,
            intent_epoch_violations,
        )
    )


def _coordinator_v3_blackboard_invariant(database: sqlite3.Connection) -> bool:
    blackboard_epoch_violations = database.execute(
        """SELECT COUNT(*) FROM blackboard_claims AS claim
           LEFT JOIN blackboard_claim_epochs AS epoch
             ON epoch.project=claim.project AND epoch.resource=claim.resource
            AND epoch.last_epoch=claim.fencing_epoch
           WHERE epoch.project IS NULL"""
    ).fetchone()[0]
    blackboard_set_violations = database.execute(
        """SELECT COUNT(*) FROM blackboard_claims AS claim
           WHERE EXISTS (
               SELECT 1 FROM blackboard_claims AS sibling
                WHERE sibling.project=claim.project
                  AND sibling.claim_id=claim.claim_id
                  AND (
                      sibling.agent != claim.agent
                      OR sibling.lease_token != claim.lease_token
                      OR sibling.heartbeat_at != claim.heartbeat_at
                      OR sibling.expires_at != claim.expires_at
                  )
           )"""
    ).fetchone()[0]
    return not any((blackboard_epoch_violations, blackboard_set_violations))


def _coordinator_v3_cross_table_invariant(database: sqlite3.Connection) -> bool:
    return _coordinator_v3_base_cross_table_invariant(
        database
    ) and _coordinator_v3_blackboard_invariant(database)


_BLACKBOARD_V3_TABLES = frozenset({"blackboard_claim_epochs", "blackboard_claims"})
_COORDINATOR_V3_BASE_TABLE_SQL = tuple(
    item for item in _COORDINATOR_V3_TABLE_SQL if item[0] not in _BLACKBOARD_V3_TABLES
)


def _coordinator_v3_base_schema_complete(database: sqlite3.Connection) -> bool:
    objects = database.execute(
        """SELECT type, name FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
    ).fetchall()
    expected = {("table", name) for name, _sql in _COORDINATOR_V3_BASE_TABLE_SQL}
    return {(str(row[0]), str(row[1])) for row in objects} == expected and all(
        _coordinator_v3_object_matches(database, name, sql)
        for name, sql in _COORDINATOR_V3_BASE_TABLE_SQL
    )


def _upgrade_object_names_valid(
    observed: set[tuple[str, str]], expected: set[str]
) -> bool:
    for kind, name in observed:
        if kind != "table":
            return False
        if name not in expected:
            return False
    return True


def _coordinator_schema_upgrade_objects_valid(database: sqlite3.Connection) -> bool:
    objects = database.execute(
        """SELECT type, name FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
    ).fetchall()
    expected = {name: sql for name, sql in _COORDINATOR_V3_TABLE_SQL}
    observed = {(str(kind), str(name)) for kind, name in objects}
    if not _upgrade_object_names_valid(observed, set(expected)):
        return False
    return all(
        _coordinator_v3_object_matches(database, name, expected[name])
        for _kind, name in observed
    )


def _bounded_table_rows(
    database: sqlite3.Connection, table: str
) -> list[tuple[object, ...]]:
    rows = database.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchmany(100_001)
    if len(rows) > 100_000:
        raise _coordinator_migration_error(
            "coordinator_v3_source_conflict",
            "coordinator v3 schema upgrade exceeded the row bound",
        )
    return [tuple(row) for row in rows]


def _coordinator_base_rows_match(
    source: sqlite3.Connection, candidate: sqlite3.Connection
) -> bool:
    for table, _sql in _COORDINATOR_V3_BASE_TABLE_SQL:
        if _bounded_table_rows(source, table) != _bounded_table_rows(candidate, table):
            return False
    return True


def _coordinator_v3_upgrade_source_healthy(database: sqlite3.Connection) -> bool:
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    integrity_ok = len(integrity) == 1 and integrity[0][0] == "ok"
    checks = (
        integrity_ok,
        not foreign_keys,
        _coordinator_v3_base_cross_table_invariant(database),
    )
    return all(checks)


def _validate_coordinator_v3_upgrade_source(database: sqlite3.Connection) -> None:
    if not _coordinator_v3_base_schema_complete(database):
        raise _coordinator_migration_error(
            "coordinator_v3_source_conflict",
            "coordinator v3 schema upgrade source is not the previous exact schema",
        )
    if database.execute("SELECT COUNT(*) FROM maintenance_owners").fetchone()[0]:
        raise _coordinator_migration_error(
            "coordinator_v3_source_live",
            "coordinator v3 schema upgrade source has active owners",
        )
    if not _coordinator_v3_upgrade_source_healthy(database):
        raise _coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 schema upgrade source validation failed",
        )


def _copy_coordinator_v3_source(
    source: sqlite3.Connection, candidate: Path
) -> None:
    if candidate.exists():
        return
    with contextlib.closing(
        open_operational_db(candidate, busy_ms=DEFAULTS.markdown_busy_ms)
    ) as destination:
        source.backup(destination)


def _require_upgrade_candidate_matches(
    source: sqlite3.Connection, database: sqlite3.Connection
) -> None:
    if not _coordinator_schema_upgrade_objects_valid(database):
        raise _coordinator_migration_error(
            "coordinator_v3_source_conflict",
            "coordinator v3 schema upgrade candidate has unknown objects",
        )
    if not _coordinator_base_rows_match(source, database):
        raise _coordinator_migration_error(
            "coordinator_v3_source_conflict",
            "coordinator v3 schema upgrade candidate differs from source",
        )


def _require_empty_blackboard_tables(database: sqlite3.Connection) -> None:
    for table in _BLACKBOARD_V3_TABLES:
        if not _coordinator_table_exists(database, table):
            continue
        if database.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
            raise _coordinator_migration_error(
                "coordinator_v3_source_conflict",
                "coordinator v3 schema upgrade candidate has claim rows",
            )


def _blackboard_schema_statements() -> tuple[MigrationStatement, ...]:
    return tuple(
        statement
        for statement in _coordinator_v3_statements()
        if statement.name.removeprefix("create_") in _BLACKBOARD_V3_TABLES
    )


def _upgrade_coordinator_candidate_database(
    source: sqlite3.Connection,
    candidate: Path,
    killpoint: Callable[[str], None] | None,
) -> None:
    with contextlib.closing(
        open_operational_db(
            candidate,
            busy_ms=DEFAULTS.markdown_busy_ms,
            contract=_COORDINATOR_V3_CONTRACT,
        )
    ) as database:
        database.row_factory = sqlite3.Row
        _require_upgrade_candidate_matches(source, database)
        _require_empty_blackboard_tables(database)
        run_resumable_migration(
            database,
            _blackboard_schema_statements(),
            final_invariant=_coordinator_v3_schema_complete,
            killpoint=killpoint,
        )


def upgrade_coordinator_v3_candidate(
    path: Path,
    *,
    source_v3: Path,
    killpoint: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Copy and resumably extend the previous exact coordinator-v3 schema."""
    candidate = Path(path)
    source_path = Path(source_v3)
    _reject_coordinator_source_alias(candidate, source_path)
    with contextlib.closing(
        open_readonly_operational_db(
            source_path,
            source_path.parent.parent,
            max_bytes=1 << 50,
            owner_only=True,
            contract=_COORDINATOR_V3_CONTRACT,
        )
    ) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        _validate_coordinator_v3_upgrade_source(source)
        _copy_coordinator_v3_source(source, candidate)
        _upgrade_coordinator_candidate_database(source, candidate, killpoint)
    _harden_owner_only(candidate, 0o600)
    return validate_coordinator_v3_database(
        candidate, state_root=candidate.parent.parent
    )["row_counts"]


def _reject_coordinator_source_alias(candidate: Path, source: Path) -> None:
    if candidate.absolute() == source.absolute():
        raise ValueError("coordinator v3 candidate and v2 source are the same file")
    if _same_existing_file(candidate, source):
        raise ValueError("coordinator v3 candidate and v2 source are the same file")


def _same_existing_file(candidate: Path, source: Path) -> bool:
    """An unverifiable identity is refused rather than assumed distinct."""
    try:
        if not (candidate.exists() or candidate.is_symlink()):
            return False
        return os.path.samefile(candidate, source)
    except OSError as exc:
        raise PermissionError(
            "coordinator v3 candidate identity could not be verified"
        ) from exc


def initialize_coordinator_v3_candidate(
    path: Path,
    *,
    source_v2: Path | None,
    killpoint: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Create or resume an unpublished coordinator v3 candidate database."""
    candidate = Path(path)
    source_path = Path(source_v2) if source_v2 is not None else None
    summary = _build_coordinator_v3_candidate(candidate, source_path, killpoint)
    _publish_coordinator_v3_candidate(candidate)
    return summary


_EMPTY_V3_SUMMARY = {
    "operations": 0,
    "project_checkpoint_attempts": 0,
    "project_checkpoints": 0,
    "transactions": 0,
    "writer_fences": 0,
}


def _build_coordinator_v3_candidate(
    candidate: Path,
    source_path: Path | None,
    killpoint: Callable[[str], None] | None,
) -> dict[str, int]:
    with contextlib.ExitStack() as stack:
        migration, summary = _coordinator_v2_source_migration(
            stack, candidate, source_path
        )
        database = stack.enter_context(
            contextlib.closing(
                open_operational_db(candidate, busy_ms=DEFAULTS.markdown_busy_ms)
            )
        )
        if source_path is not None:
            _reject_coordinator_source_alias(candidate, source_path)
        _repair_partial_coordinator_v3_schema(
            database, allow_populated_rebuild=source_path is not None
        )
        run_resumable_migration(
            database,
            _coordinator_v3_statements(),
            final_invariant=_coordinator_v3_schema_complete,
            killpoint=killpoint,
        )
        _apply_coordinator_v2_rows(
            database, source_path, migration, summary, killpoint
        )
        _require_candidate_valid(database)
    return summary


def _coordinator_v2_source_migration(
    stack: contextlib.ExitStack, candidate: Path, source_path: Path | None
) -> tuple[tuple[MigrationStatement, ...], dict[str, int]]:
    """The source is checked for aliasing before and after it is read."""
    if source_path is None:
        return (), dict(_EMPTY_V3_SUMMARY)
    _reject_coordinator_source_alias(candidate, source_path)
    source = stack.enter_context(
        contextlib.closing(
            open_readonly_operational_db(
                source_path, source_path.parent.parent, max_bytes=1 << 50
            )
        )
    )
    source.row_factory = sqlite3.Row
    source.execute("BEGIN")
    migration, summary = _coordinator_v2_migration_statements(source)
    _reject_coordinator_source_alias(candidate, source_path)
    return migration, summary


def _apply_coordinator_v2_rows(
    database: sqlite3.Connection,
    source_path: Path | None,
    migration: tuple[MigrationStatement, ...],
    summary: dict[str, int],
    killpoint: Callable[[str], None] | None,
) -> None:
    if source_path is None:
        _require_fresh_candidate(database)
        return
    run_resumable_migration(
        database,
        migration,
        final_invariant=lambda current: _coordinator_v2_reconciliation_complete(
            current, migration, summary
        ),
        killpoint=killpoint,
    )


def _require_fresh_candidate(database: sqlite3.Connection) -> None:
    if any(_coordinator_v3_row_counts(database).values()):
        raise _coordinator_migration_error(
            "coordinator_v3_source_conflict",
            "fresh coordinator v3 initialization found existing rows",
        )


def _require_candidate_valid(database: sqlite3.Connection) -> None:
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    if (
        len(integrity) != 1
        or integrity[0][0] != "ok"
        or foreign_keys
        or not _coordinator_v3_cross_table_invariant(database)
    ):
        raise _coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 candidate validation failed",
        )


def _publish_coordinator_v3_candidate(candidate: Path) -> None:
    with contextlib.closing(
        open_operational_db(
            candidate,
            busy_ms=DEFAULTS.markdown_busy_ms,
            contract=_COORDINATOR_V3_CONTRACT,
            initialize_contract=True,
        )
    ):
        pass
    _harden_owner_only(candidate, 0o600)
    validate_coordinator_v3_database(candidate, state_root=candidate.parent.parent)


def _require_inside_state_root(path: Path, state_root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PermissionError(
            "coordinator v3 database is outside the state root"
        ) from exc


def _require_v3_invariants(database: sqlite3.Connection) -> None:
    _require_v3_integrity(database)
    if _v3_operation_violations(database) or not _coordinator_v3_cross_table_invariant(
        database
    ):
        raise _coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 database invariant failed",
        )


def _require_v3_integrity(database: sqlite3.Connection) -> None:
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok" or foreign_keys:
        raise _coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 database invariant failed",
        )


def _v3_operation_violations(database: sqlite3.Connection) -> int:
    return database.execute(
        """SELECT COUNT(*) FROM "operation"
           WHERE position < 0
              OR parent_device IS NULL
              OR parent_inode IS NULL
              OR applied NOT IN (0,1)"""
    ).fetchone()[0]


def validate_coordinator_v3_database(
    path: Path, *, state_root: Path
) -> dict[str, object]:
    """Validate one unpublished or active coordinator v3 database fail-closed."""
    _require_inside_state_root(Path(path), Path(state_root))
    with contextlib.closing(
        open_readonly_operational_db(
            Path(path),
            Path(state_root),
            max_bytes=1 << 50,
            owner_only=True,
            busy_ms=DEFAULTS.markdown_busy_ms,
            contract=_COORDINATOR_V3_CONTRACT,
        )
    ) as database:
        if not _coordinator_v3_schema_complete(database):
            raise _coordinator_migration_error(
                "coordinator_v3_schema_incomplete",
                "coordinator v3 schema is incomplete",
            )
        _require_v3_invariants(database)
        return {
            "application_id": database.execute("PRAGMA application_id").fetchone()[0],
            "foreign_key_check": [],
            "integrity_check": "ok",
            "journal_mode": database.execute("PRAGMA journal_mode").fetchone()[0],
            "row_counts": _coordinator_v3_row_counts(database),
            "synchronous": database.execute("PRAGMA synchronous").fetchone()[0],
            "trusted_schema": database.execute("PRAGMA trusted_schema").fetchone()[0],
            "user_version": database.execute("PRAGMA user_version").fetchone()[0],
        }


def _unsigned_filesystem_id(value: object) -> int:
    """Normalize signed or unsigned OS stat representations to unsigned 64-bit."""
    if type(value) is not int or not _SQLITE_INT64_MIN <= value < _UINT64_MODULUS:
        raise ValueError("filesystem identity is not a 64-bit integer")
    return value if value >= 0 else value + _UINT64_MODULUS


def _stat_identity(metadata: object) -> tuple[int, int]:
    return (
        _unsigned_filesystem_id(getattr(metadata, "st_dev")),
        _unsigned_filesystem_id(getattr(metadata, "st_ino")),
    )


def _encode_filesystem_id(value: object) -> int:
    """Map an unsigned filesystem ID bijectively onto SQLite's signed int64."""
    if type(value) is not int or not 0 <= value < _UINT64_MODULUS:
        raise ValueError("filesystem identity must be an unsigned 64-bit integer")
    return value if value <= _SQLITE_INT64_MAX else value - _UINT64_MODULUS


def _decode_filesystem_id(value: object) -> int:
    """Restore an unsigned filesystem ID from its SQLite int64 encoding."""
    if type(value) is not int or not _SQLITE_INT64_MIN <= value <= _SQLITE_INT64_MAX:
        raise ValueError("persisted filesystem identity is not a signed 64-bit integer")
    return value if value >= 0 else value + _UINT64_MODULUS


def _encode_parent_identity(identity: tuple[object, object]) -> tuple[int, int]:
    return (_encode_filesystem_id(identity[0]), _encode_filesystem_id(identity[1]))


def _decode_parent_identity(identity: tuple[object, object]) -> tuple[int, int]:
    return (
        _decode_filesystem_id(identity[0]),
        _decode_filesystem_id(identity[1]),
    )


@dataclass(frozen=True)
class MarkdownChange:
    kind: ChangeKind
    path: str
    content: bytes | None
    max_before_bytes: int | None = None

    @classmethod
    def create(
        cls, path: str, content: bytes, *, max_before_bytes: int | None = None
    ) -> MarkdownChange:
        return cls("create", path, _require_bytes(content), max_before_bytes)

    @classmethod
    def replace(
        cls, path: str, content: bytes, *, max_before_bytes: int | None = None
    ) -> MarkdownChange:
        return cls("replace", path, _require_bytes(content), max_before_bytes)

    @classmethod
    def delete(cls, path: str, *, max_before_bytes: int | None = None) -> MarkdownChange:
        return cls("delete", path, None, max_before_bytes)


@dataclass(frozen=True)
class MarkdownOperation:
    kind: ChangeKind
    path: str
    before_hash: str
    after_hash: str


@dataclass(frozen=True)
class TransactionRecord:
    id: str
    operation_id: str
    state: str
    operations: tuple[MarkdownOperation, ...]
    preconditions: Mapping[str, object]
    created_at: str
    updated_at: str
    parent_transaction_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class IntentFence:
    intent_id: str
    mode: Literal["capture", "worker", "operator"]
    token: str
    epoch: int
    owner: OwnerLease
    expires_at: datetime


@dataclass(frozen=True)
class TransactionAbortReceipt:
    transaction_id: str
    intent_id: str
    abort_operation_id: str
    receipt_path: str
    receipt_sha256: str
    aborted_at: str


@dataclass(frozen=True)
class ProjectCheckpointReservation:
    project: str
    sequence: int
    occurrence_id: str
    idempotency_key: str
    event_json: str
    operation_id: str
    attempt_number: int
    state: str
    transaction_id: str | None = None
    parent_operation_id: str | None = None
    duplicate: bool = False


class TransactionFailure(RuntimeError):
    """An apply failure with a stable machine-readable disposition."""

    def __init__(self, message: str, code: str, state: str):
        super().__init__(message)
        self.code = code
        self.state = state

    def __reduce__(self):
        """Carry the disposition across a process boundary, not just the text."""
        return (self.__class__, (str(self), self.code, self.state))


class TransactionDriftError(RuntimeError):
    """A committed operation no longer matches its persisted after-state."""

    def __init__(self, transaction_id: str, paths: Sequence[str]):
        self.transaction_id = transaction_id
        self.paths = tuple(paths)
        super().__init__(
            "committed transaction target drift detected; use a new operation_id: "
            + ", ".join(paths)
        )

    def __reduce__(self):
        return (self.__class__, (self.transaction_id, self.paths))


class ProjectPendingPriorError(TransactionFailure):
    """A project checkpoint is blocked by an unapplied lower sequence."""

    status = "pending_prior"

    def __init__(self, project: str, sequence: int, prior_sequence: int):
        super().__init__(
            f"project {project!r} sequence {sequence} waits for sequence {prior_sequence}",
            "pending_prior",
            "prepared",
        )
        self.project = project
        self.sequence = sequence
        self.prior_sequence = prior_sequence

    def __reduce__(self):
        """This subclass takes its own arguments, not the base three."""
        return (self.__class__, (self.project, self.sequence, self.prior_sequence))


class TargetBoundaryFailure(RuntimeError):
    """A persisted target no longer has its prepared containment identity."""


class TargetTooLargeError(ValueError):
    """A transaction target exceeds the authoritative target-size contract."""


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_CLAIM_TARGET_FIELDS = {
    "page",
    "claim_id",
    "fingerprint",
    "record_hash",
    "evidence_hash",
}


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and _SHA256_HEX.fullmatch(value) is not None


def _is_filled_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _valid_positive_epoch(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= 1


def _require_bounded_sequence(expected: object, name: str) -> None:
    if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
        raise TypeError(f"{name} precondition must be an array")
    if not 1 <= len(expected) <= 1000:
        raise ValueError(f"{name} precondition must be bounded")


def _require_mapping_fields(expected: object, required: set[str], name: str) -> None:
    if not isinstance(expected, Mapping):
        raise TypeError(f"{name} precondition must be a mapping")
    if set(expected) != required:
        raise ValueError(f"{name} precondition has invalid fields")


def _require_claim_target_hashes(item: Mapping[str, object]) -> None:
    for field in ("fingerprint", "record_hash", "evidence_hash"):
        if not _is_sha256_hex(item[field]):
            raise ValueError("claim_targets precondition has invalid hash")


def _valid_project_lease_values(expected: Mapping[str, object]) -> bool:
    if not _is_filled_string(expected["project"]):
        return False
    if not _is_filled_string(expected["lease_token"]):
        return False
    if not isinstance(expected["fencing_epoch"], int):
        return False
    if expected["fencing_epoch"] < 1:
        return False
    return isinstance(expected["expires_at"], str)


def _validated_project_lease(expected: object) -> dict:
    _require_mapping_fields(
        expected,
        {"project", "lease_token", "fencing_epoch", "expires_at"},
        "project_lease",
    )
    if not _valid_project_lease_values(expected):
        raise ValueError("project_lease precondition has invalid values")
    _parse_timestamp(expected["expires_at"])
    return dict(expected)


def _valid_intent_fence_values(expected: Mapping[str, object]) -> bool:
    if not _is_sha256_hex(str(expected["intent_id"])):
        return False
    if expected["mode"] not in {"capture", "worker", "operator"}:
        return False
    if not _is_filled_string(expected["token"]):
        return False
    if not _valid_positive_epoch(expected["fencing_epoch"]):
        return False
    return isinstance(expected["expires_at"], str)


def _validated_intent_fence(expected: object) -> dict:
    _require_mapping_fields(
        expected,
        {"intent_id", "mode", "token", "fencing_epoch", "expires_at"},
        "intent_fence",
    )
    if not _valid_intent_fence_values(expected):
        raise ValueError("intent_fence precondition has invalid values")
    _parse_timestamp(str(expected["expires_at"]))
    return dict(expected)


def _valid_capture_binding_values(expected: Mapping[str, object]) -> bool:
    if not _is_sha256_hex(str(expected["intent_id"])):
        return False
    if not _is_filled_string(expected["task_id"]):
        return False
    if not _is_sha256_hex(str(expected["active_link_digest"])):
        return False
    return _is_sha256_hex(str(expected["seal_digest"]))


def _validated_capture_binding(expected: object) -> dict:
    _require_mapping_fields(
        expected,
        {"intent_id", "task_id", "active_link_digest", "seal_digest"},
        "capture_binding",
    )
    if not _valid_capture_binding_values(expected):
        raise ValueError("capture_binding precondition has invalid values")
    return dict(expected)


def _require_hash_or_absent(expected: object) -> None:
    if expected == ABSENT:
        return
    if not _is_sha256_hex(expected):
        raise ValueError("precondition values must be 'absent' or SHA-256 hashes")


_RECOVERY_MANIFEST_FIELDS = {
    "schema_version",
    "transaction_id",
    "request_hash",
    "plan_hash",
    "operations",
}
_PERSISTED_OPERATION_FIELDS = {
    "position",
    "before_hash",
    "after_hash",
    "parent_device",
    "parent_inode",
}
# Which side of a recorded operation must be absent, per kind.
_OPERATION_ABSENCE = {
    "create": (True, False),
    "replace": (False, False),
    "delete": (False, True),
}


class _BuiltOperation(NamedTuple):
    row: tuple
    change: dict
    parent_mismatch: bool


class _PromotionPlan(NamedTuple):
    manifest: dict
    operations: list
    parent_mismatch: bool


def _manifest_identity_matches(
    manifest: Mapping[str, object], transaction_id: str, plan_bytes: bytes
) -> bool:
    if manifest["schema_version"] != "markdown-transaction-recovery/v1":
        return False
    if manifest["transaction_id"] != transaction_id:
        return False
    return manifest["plan_hash"] == sha256_bytes(plan_bytes)


def _comparable_operation_lists(plan_operations: object, manifest_operations: object) -> bool:
    if not isinstance(plan_operations, list) or not isinstance(manifest_operations, list):
        return False
    return len(plan_operations) == len(manifest_operations)


def _persisted_integers(persisted: Mapping[str, object]) -> bool:
    if type(persisted["position"]) is not int:
        return False
    if type(persisted["parent_device"]) is not int:
        return False
    return type(persisted["parent_inode"]) is int


def _persisted_shape_matches(persisted: object, position: int) -> bool:
    if not isinstance(persisted, dict):
        return False
    if set(persisted) != _PERSISTED_OPERATION_FIELDS:
        return False
    if persisted["position"] != position:
        return False
    return _persisted_integers(persisted)


def _validated_persisted_operation(
    persisted: object, position: int, operation: object
) -> tuple | None:
    """The encoded parent identity, or None when the pair does not line up."""
    if not isinstance(operation, dict):
        return None
    if not _persisted_shape_matches(persisted, position):
        return None
    try:
        return _encode_parent_identity(
            (persisted["parent_device"], persisted["parent_inode"])
        )
    except ValueError:
        return None


def _kind_transition_valid(kind: object, before_hash: str, after_hash: str) -> bool:
    expected = _OPERATION_ABSENCE.get(kind)
    if expected is None:
        return True
    return (before_hash == ABSENT, after_hash == ABSENT) == expected


def _operation_hashes_consistent(
    persisted: Mapping[str, object], kind: object, before_hash: str, after_hash: str
) -> bool:
    if persisted["before_hash"] != before_hash:
        return False
    if persisted["after_hash"] != after_hash:
        return False
    return _kind_transition_valid(kind, before_hash, after_hash)


_RESERVED_DEVICE_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def _require_normalized_path(value: str) -> None:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("path must use NFC Unicode normalization")


def _require_bounded_path(value: str, relative: Path) -> None:
    if len(value.encode("utf-8")) > MAX_KNOWLEDGE_PATH_BYTES:
        raise ValueError("target path exceeds length limit")
    if len(relative.parts) > MAX_KNOWLEDGE_DEPTH:
        raise ValueError("target path depth exceeds limit")
    if any(
        len(part.encode("utf-8")) > MAX_KNOWLEDGE_COMPONENT_BYTES
        for part in relative.parts
    ):
        raise ValueError("target path component exceeds length limit")


def _non_portable_component(part: str) -> bool:
    if part.endswith((" ", ".")):
        return True
    if any(character in '<>:"|?*' or ord(character) < 32 for character in part):
        return True
    return part.rstrip(" .").split(".", 1)[0].casefold() in _RESERVED_DEVICE_NAMES


def _require_portable_components(relative: Path) -> None:
    for part in relative.parts:
        if _non_portable_component(part):
            raise ValueError("path contains a non-portable or reserved component")


def _require_allowed_root(normalized: str) -> None:
    if normalized in _ALLOWED_FILES:
        return
    if any(normalized.startswith(f"{root}/") for root in _ALLOWED_DIRECTORIES):
        return
    raise ValueError("path is outside every allowed Markdown root")


def _require_markdown_target(normalized: str) -> None:
    if normalized.startswith("knowledge/feedback/"):
        raise ValueError("feedback candidates must use JSON")


def _require_feedback_target(normalized: str) -> None:
    if _FEEDBACK_JSON_RE.fullmatch(normalized) is None:
        raise ValueError("JSON targets must be feedback candidates")


def _require_blackboard_target(normalized: str) -> None:
    if _BLACKBOARD_JSONL_RE.fullmatch(normalized) is None:
        raise ValueError("JSONL targets must be project blackboard streams")


def _require_approved_suffix(relative: Path, normalized: str) -> None:
    """Each approved file type belongs in exactly one place."""
    suffix = relative.suffix.casefold()
    if suffix == ".md":
        _require_markdown_target(normalized)
        return
    if suffix == ".json":
        _require_feedback_target(normalized)
        return
    if suffix == ".jsonl":
        _require_blackboard_target(normalized)
        return
    raise ValueError("transaction targets must use an approved file type")


def _require_recovery_bound(max_transactions: object) -> None:
    if max_transactions is None:
        return
    if isinstance(max_transactions, bool) or not isinstance(max_transactions, int):
        raise ValueError("max_transactions must be a non-negative integer or None")
    if max_transactions < 0:
        raise ValueError("max_transactions must be a non-negative integer or None")


def _preparing_owner_alive(selected_state: str, owner_pid: object) -> bool:
    if selected_state != "preparing" or owner_pid is None:
        return False
    return _pid_alive(owner_pid)


class _ArtifactRoots(NamedTuple):
    artifact: Path
    before: Path
    after: Path


class _StagedOperation(NamedTuple):
    operation: MarkdownOperation
    parent_identity: tuple[int, int]
    plan_entry: dict[str, object]


class _StagedPlan(NamedTuple):
    operations: list[MarkdownOperation]
    parent_identities: list[tuple[int, int]]
    plan_operations: list[dict[str, object]]


def _require_prepare_arguments(
    operation_id: object, changes: Sequence[MarkdownChange], content_guard: object
) -> None:
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")
    if not changes:
        raise ValueError("a transaction requires at least one change")
    if content_guard not in {None, "model_output"}:
        raise ValueError("content_guard must be 'model_output' or None")


def _require_matching_reservation(
    project_reservation: object,
    operation_id: str,
    persisted_preconditions: Mapping[str, object],
) -> None:
    if project_reservation is None:
        return
    if not isinstance(project_reservation, ProjectCheckpointReservation):
        raise TypeError("project_reservation must be a ProjectCheckpointReservation")
    if project_reservation.operation_id != operation_id:
        raise ValueError("project reservation operation_id does not match")
    if "project_lease" not in persisted_preconditions:
        raise ValueError("project reservation requires a project lease precondition")


def _require_capture_matches_kind(change: MarkdownChange, before: bytes | None) -> None:
    if change.kind == "create" and before is not None:
        raise FileExistsError(change.path)
    if change.kind in {"replace", "delete"} and before is None:
        raise FileNotFoundError(change.path)


def _require_change_precondition(
    change: MarkdownChange,
    before_hash: str,
    persisted_preconditions: Mapping[str, object],
) -> None:
    expected_before = persisted_preconditions.get(change.path)
    if expected_before is not None and expected_before != before_hash:
        raise ValueError(
            f"target precondition changed before prepare: {change.path}"
        )


def _content_hash(content: bytes | None) -> str:
    if content is None:
        return ABSENT
    return sha256_bytes(content)


def _run_plan_validators(plan: dict, validators: Sequence[Validator]) -> None:
    for validator in validators:
        if not callable(validator):
            raise TypeError("validators must be callable")
        if validator(copy.deepcopy(plan)) is False:
            raise ValueError("transaction validator rejected the plan")


def _validated_plan(
    transaction_id: str,
    plan_operations: list[dict[str, object]],
    content_guard: object,
    validators: Sequence[Validator],
) -> dict[str, object]:
    plan: dict[str, object] = {
        "schema_version": "markdown-transaction/v1",
        "transaction_id": transaction_id,
        "operations": plan_operations,
    }
    if content_guard is not None:
        plan["content_guard"] = content_guard
    validate_schema(plan, _SCHEMA)
    _run_plan_validators(plan, validators)
    validate_schema(plan, _SCHEMA)
    return plan


def _recovery_manifest(
    transaction_id: str, request_hash: str, plan_bytes: bytes, staged: _StagedPlan
) -> dict[str, object]:
    return {
        "schema_version": "markdown-transaction-recovery/v1",
        "transaction_id": transaction_id,
        "request_hash": request_hash,
        "plan_hash": sha256_bytes(plan_bytes),
        "operations": [
            {
                "position": position,
                "before_hash": operation.before_hash,
                "after_hash": operation.after_hash,
                "parent_device": staged.parent_identities[position][0],
                "parent_inode": staged.parent_identities[position][1],
            }
            for position, operation in enumerate(staged.operations)
        ],
    }


_ABORT_FENCE_QUERY = """SELECT 1 FROM intent_fences AS fence
                       JOIN capture_binding_projections AS binding
                         ON binding.intent_id=fence.intent_id
                        AND binding.intent_fence_token=fence.token
                        AND binding.intent_fence_epoch=fence.fencing_epoch
                       WHERE fence.intent_id=? AND fence.token=?
                         AND fence.fencing_epoch=? AND fence.expires_at>?
                         AND binding.active_link_digest=?"""


def _require_operator_fence(intent_fence: object, active_link_digest: object) -> None:
    if not isinstance(intent_fence, IntentFence) or intent_fence.mode != "operator":
        raise ValueError("abort requires an operator intent fence")
    if re.fullmatch(r"[0-9a-f]{64}", str(active_link_digest)) is None:
        raise ValueError("active link digest must be lowercase 64-hex")


def _require_actor_identity(actor_identity: object) -> None:
    if not isinstance(actor_identity, str):
        raise ValueError("actor identity is invalid")
    if not 1 <= len(actor_identity.encode("utf-8")) <= 512:
        raise ValueError("actor identity is invalid")


def _require_abortable_state(row: sqlite3.Row, transaction_id: str) -> None:
    if row["state"] == "committed":
        raise TransactionFailure(
            "committed transaction cannot be aborted",
            "abort_committed",
            "committed",
        )
    if row["state"] in {"conflicted", "quarantined"}:
        raise TransactionFailure(
            "conflicted transaction cannot be aborted",
            "abort_state_refused",
            str(row["state"]),
        )


def _abort_identity(
    transaction_id: str,
    intent_fence: IntentFence,
    active_link_digest: str,
    manifest_sha256: str,
) -> str:
    identity = {
        "active_link_digest": active_link_digest,
        "before_manifest_sha256": manifest_sha256,
        "intent_fence_epoch": intent_fence.epoch,
        "intent_id": intent_fence.intent_id,
        "transaction_id": transaction_id,
    }
    return f"transaction-abort:{sha256_bytes(canonical_json_bytes(identity))}"


class _AbortDirection(NamedTuple):
    operation_id: str
    manifest_sha256: str
    chosen_at: str


def _require_gate_wait(wait_seconds: object) -> None:
    if wait_seconds is None:
        return
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
        raise ValueError("writer gate wait_seconds must be non-negative or None")
    if wait_seconds < 0:
        raise ValueError("writer gate wait_seconds must be non-negative or None")


def _gate_wait_seconds(wait_seconds: float | None) -> float:
    if wait_seconds is None:
        return _WRITER_WAIT_SECONDS
    return wait_seconds


def _retry_gate_or_fail(
    exc: BaseException, attempt: int, deadline: float
) -> int:
    """The next attempt number, or the timeout that ends the wait."""
    if not _is_transient_writer_contention(exc):
        raise exc
    delay = _writer_retry_delay(attempt, deadline)
    if delay <= 0:
        raise TimeoutError(
            "timed out waiting for the global Markdown writer gate"
        ) from exc
    time.sleep(delay)
    return attempt + 1


def _is_target_boundary_error(error: BaseException) -> bool:
    if isinstance(error, (TargetBoundaryFailure, FileNotFoundError)):
        return True
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "parent identity",
            "parent does not exist",
            "reparse point",
            "traverses a symlink",
            "non-canonical parent",
            "outside the vault",
            "escapes the vault",
            "stable directory",
        )
    )


def _require_bytes(content: bytes) -> bytes:
    if not isinstance(content, bytes):
        raise TypeError("Markdown content must be bytes")
    return content


class _TargetBoundaryChanged(Exception):
    """A target's parent identity changed; recovery stops and quarantines."""


_UNAVAILABLE = object()


def _inverse_kind(before_hash: str, after_hash: str) -> str:
    """Undoing a create is a delete, and undoing a delete is a create."""
    if before_hash == ABSENT:
        return "delete"
    if after_hash == ABSENT:
        return "create"
    return "replace"


def _before_artifact(root: Path, transaction_id: str, position: object) -> Path:
    return root / transaction_id / "before" / f"{int(position):06d}.bin"


def _before_state(root: Path, transaction_id: str, row: sqlite3.Row) -> object:
    """The undo image for one operation; ABSENT when there was nothing there."""
    if row["before_hash"] == ABSENT:
        return ABSENT
    content = _before_artifact(root, transaction_id, row["position"]).read_bytes()
    if sha256_bytes(content) != row["before_hash"]:
        raise RuntimeError(
            f"transaction before-image is corrupt for {row['path']}"
        )
    return {
        "sha256": row["before_hash"],
        "artifact": f"before/{int(row['position']):06d}.bin",
    }


def _optional_before_state(
    root: Path, transaction_id: str, row: sqlite3.Row
) -> object:
    """_UNAVAILABLE means the undo image is missing or corrupt, so skip it."""
    try:
        return _before_state(root, transaction_id, row)
    except (OSError, RuntimeError):
        return _UNAVAILABLE


def _inverse_operation(transaction_id: str, row: sqlite3.Row) -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "position": row["position"],
        "kind": _inverse_kind(row["before_hash"], row["after_hash"]),
        "path": row["path"],
        "before_hash": row["after_hash"],
        "after_hash": row["before_hash"],
        "parent_device": row["parent_device"],
        "parent_inode": row["parent_inode"],
    }


def _undoable(
    row: sqlite3.Row,
    before_states: dict[int, object],
    current_hashes: dict[int, str],
) -> bool:
    position = row["position"]
    if not row["applied"] or position not in before_states:
        return False
    return current_hashes[position] == row["after_hash"]


_CAPTURE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)

_PRECONDITION_METHODS = {
    "claim_targets": "_check_claim_targets_precondition",
    "claim_tree_manifest": "_check_claim_tree_precondition",
    "guardrails_source_manifest": "_check_guardrails_precondition",
    "project_lease": "_check_lease_precondition",
    "intent_fence": "_check_capture_precondition",
    "capture_binding": "_check_capture_precondition",
}


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


def _table_column_names(database: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in database.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(
    database: sqlite3.Connection, table: str, name: str, declaration: str
) -> None:
    """Databases from earlier stages are widened in place, never rebuilt."""
    if name in _table_column_names(database, table):
        return
    database.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _add_writer_owner_columns(database: sqlite3.Connection) -> None:
    for name, declaration in (
        ("heartbeat_at", "TEXT"),
        ("expires_at", "TEXT"),
        ("fencing_epoch", "INTEGER"),
    ):
        _add_column_if_missing(database, "writer_owners", name, declaration)


def _add_operation_columns(database: sqlite3.Connection) -> None:
    for name in ("parent_device", "parent_inode"):
        _add_column_if_missing(database, '"operation"', name, "INTEGER")


def _add_transaction_columns(database: sqlite3.Connection) -> None:
    for name in ("parent_transaction_id", "error_code", "artifacts_pruned_at"):
        _add_column_if_missing(database, '"transaction"', name, "TEXT")
    _add_column_if_missing(database, '"transaction"', "owner_pid", "INTEGER")


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
    if "operation_id" in _table_column_names(database, "project_checkpoints"):
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


def _require_live_intent_fence(
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
            now.isoformat().replace("+00:00", "Z"),
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
            now.isoformat().replace("+00:00", "Z"),
            intent_fence.token,
            intent_fence.epoch,
        ),
    ).rowcount
    if inserted != 1:
        raise RuntimeError("capture_binding_projection_failed")


def _require_checkpoint_key(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"checkpoint {label} must be a non-empty string")
    return value


def _checkpoint_event_json(
    base: Mapping[str, object], project: str, sequence: int, row: sqlite3.Row
) -> str:
    full_event = dict(base)
    full_event.update(
        project=project,
        sequence=sequence,
        last_applied_sequence=int(row["last_applied_sequence"]),
    )
    return canonical_json_bytes(full_event).decode("utf-8")


def _insert_checkpoint(
    database: sqlite3.Connection,
    project: str,
    sequence: int,
    occurrence_id: str,
    idempotency_key: str,
    event_json: str,
    precondition: Mapping[str, object],
    operation_id: str,
) -> None:
    database.execute(
        "INSERT INTO project_checkpoints "
        "(project, sequence, occurrence_id, idempotency_key, event_json, "
        "lease_token, fencing_epoch, operation_id, attempt_number, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'reserved')",
        (
            project,
            sequence,
            occurrence_id,
            idempotency_key,
            event_json,
            precondition["lease_token"],
            precondition["fencing_epoch"],
            operation_id,
        ),
    )
    database.execute(
        "INSERT INTO project_checkpoint_attempts "
        "(project, sequence, attempt_number, operation_id, lease_token, "
        "fencing_epoch, state, created_at) "
        "VALUES (?, ?, 1, ?, ?, ?, 'reserved', ?)",
        (
            project,
            sequence,
            operation_id,
            precondition["lease_token"],
            precondition["fencing_epoch"],
            _now(),
        ),
    )


def _operation_states(
    rows: Sequence[sqlite3.Row],
) -> dict[str, tuple[str, str]]:
    return {row["path"]: (row["before_hash"], row["after_hash"]) for row in rows}


def _dlp_code(exc: Exception) -> str:
    if isinstance(exc, DLPPolicyError):
        return "dlp_policy_error"
    return "dlp_content_blocked"


def _conflicted_failure(message: str) -> TransactionFailure | None:
    """The two mismatches a caller can act on; anything else is not ours to name."""
    if "after state mismatch" in message:
        return TransactionFailure(message, "unknown_target_bytes", "conflicted")
    if "before state mismatch" in message:
        return TransactionFailure(message, "before_hash_mismatch", "conflicted")
    return None


def _abort_receipt_matches(
    receipt: Mapping[str, object],
    receipt_bytes: bytes,
    transaction_id: str,
    row: sqlite3.Row,
) -> bool:
    if canonical_json_bytes(receipt) != receipt_bytes:
        return False
    if receipt["transaction_id"] != transaction_id:
        return False
    if receipt["abort_operation_id"] != row["abort_operation_id"]:
        return False
    if receipt["before_manifest_sha256"] != row["abort_manifest_sha256"]:
        return False
    return sha256_bytes(receipt_bytes) == row["abort_receipt_sha256"]


def _required_parent_identity(row: sqlite3.Row) -> object:
    persisted = (row["parent_device"], row["parent_inode"])
    if None in persisted:
        raise RuntimeError(
            f"transaction lacks parent identity for {row['path']}"
        )
    return _decode_parent_identity(persisted)


def _close_windows_handles(handles: list[int]) -> None:
    """Every handle is closed; the first failure is raised after the last close."""
    close_error: OSError | None = None
    for handle in reversed(handles):
        close_error = close_error or _closed_windows_handle(handle)
    if close_error is not None:
        raise close_error


def _closed_windows_handle(handle: int) -> OSError | None:
    try:
        _close_windows_handle(handle)
    except OSError as exc:
        return exc
    return None


def _link_or_replace(
    kind: str,
    temporary_name: str,
    name: str,
    parent_descriptor: int,
    path: str,
) -> None:
    """A create must not clobber; a replace must be atomic."""
    if kind != "create":
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        return
    try:
        os.link(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise RuntimeError(f"before state mismatch for {path}") from exc
    os.unlink(temporary_name, dir_fd=parent_descriptor)


def _require_wait_seconds(wait_seconds: object) -> None:
    if wait_seconds is None:
        return
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
        raise ValueError("writer gate wait_seconds must be non-negative or None")
    if wait_seconds < 0:
        raise ValueError("writer gate wait_seconds must be non-negative or None")


def _writer_wait_window(wait_seconds: float | None) -> float:
    if wait_seconds is None:
        return _WRITER_WAIT_SECONDS
    return wait_seconds


def _owns_writer_gate(
    row: object, owner_token: str, fencing_epoch: int
) -> bool:
    return bool(
        row is not None
        and row["owner_token"] == owner_token
        and row["fencing_epoch"] == fencing_epoch
    )


def _retry_or_raise(exc: BaseException, attempt: int, deadline: float) -> int:
    """Wait out transient contention; anything else belongs to the caller."""
    if not _is_transient_writer_contention(exc):
        raise exc
    delay = _writer_retry_delay(attempt, deadline)
    if delay <= 0:
        raise exc
    time.sleep(delay)
    return attempt + 1


def _heartbeat_retry(
    exc: BaseException, attempt: int, lease_deadline: float, stop: threading.Event
) -> int | None:
    """None means the gate is lost: the wait ran out or the stop event fired."""
    if not _is_transient_writer_contention(exc):
        return None
    delay = _writer_retry_delay(attempt, lease_deadline)
    if delay <= 0 or stop.wait(delay):
        return None
    return attempt + 1


def _ensure_safe_directory(current: Path, value: str) -> None:
    if current.exists():
        _require_safe_parent(
            current,
            f"target traverses an unsafe parent: {value}",
            f"target parent is not a directory: {value}",
        )
        return
    _make_directory(current)
    _require_safe_parent(
        current,
        f"created target parent is unsafe: {value}",
        f"created target parent is not a directory: {value}",
    )
    fsync_directory(current.parent)


def _make_directory(current: Path) -> None:
    try:
        current.mkdir()
    except FileExistsError:
        pass


def _require_safe_parent(
    current: Path, unsafe_message: str, not_directory_message: str
) -> None:
    if current.is_symlink() or _is_reparse_point(current):
        raise ValueError(unsafe_message)
    if not current.is_dir():
        raise ValueError(not_directory_message)


def _require_change_content(change: MarkdownChange) -> None:
    if change.kind == "delete":
        if change.content is not None:
            raise ValueError("delete content must be absent")
        return
    if not isinstance(change.content, bytes):
        raise TypeError("create and replace content must be bytes")
    if len(change.content) > MAX_KNOWLEDGE_TARGET_BYTES:
        raise ValueError("transaction target size exceeds limit")


def _require_before_bound(max_before_bytes: object) -> None:
    if max_before_bytes is None:
        return
    if isinstance(max_before_bytes, bool) or not isinstance(max_before_bytes, int):
        raise ValueError("max_before_bytes must be a non-negative integer or None")
    if max_before_bytes < 0:
        raise ValueError("max_before_bytes must be a non-negative integer or None")


def _deletion_blocker_code(row: sqlite3.Row, now: datetime) -> str | None:
    """None means this transaction does not stand in the way of deletion."""
    if row["state"] in {"preparing", "prepared", "applying"}:
        return "nonterminal_transaction"
    if row["state"] in {"conflicted", "quarantined"}:
        return row["error_code"] or "transaction_requires_attention"
    if _within_undo_retention(row, now):
        return "undo_retention"
    return None


def _within_undo_retention(row: sqlite3.Row, now: datetime) -> bool:
    if row["state"] != "committed" or row["artifacts_pruned_at"] is not None:
        return False
    return _parse_timestamp(row["updated_at"]) >= now - timedelta(days=30)


def _require_same_lease_fence(
    previous: object, refreshed: Mapping[str, object]
) -> None:
    if not isinstance(previous, dict) or any(
        previous.get(field) != refreshed[field]
        for field in ("project", "lease_token", "fencing_epoch")
    ):
        raise TransactionFailure(
            "project lease changed before transaction refresh",
            "precondition_failed",
            "quarantined",
        )


def _promotion_plan(
    manifest: Mapping[str, object], built: Sequence[object]
) -> _PromotionPlan:
    return _PromotionPlan(
        dict(manifest),
        [item.row for item in built],
        any(item.parent_mismatch for item in built),
    )


def _prune_cutoff(retention_days: int, now: datetime | None) -> datetime:
    if retention_days < 30:
        raise ValueError("retention_days must be at least 30")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current - timedelta(days=retention_days)


def _lstat_or_none(target: Path) -> os.stat_result | None:
    try:
        return os.lstat(target)
    except FileNotFoundError:
        return None


def _stat_at_or_none(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_capture_descriptor(
    target: object, label: object, dir_fd: int | None = None
) -> int:
    try:
        if dir_fd is None:
            return os.open(target, _CAPTURE_OPEN_FLAGS)
        return os.open(target, _CAPTURE_OPEN_FLAGS, dir_fd=dir_fd)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"transaction target cannot be opened safely: {label}"
        ) from exc


def _is_reparse_metadata(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _require_regular_file(metadata: os.stat_result, target: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_metadata(metadata):
        raise ValueError(f"transaction target is a link: {target}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"transaction target is not a regular file: {target}")


def _bounded_chunks(descriptor: int, remaining: int) -> Iterator[bytes]:
    """One byte past the bound is read, so the caller can see the overflow."""
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            return
        yield chunk
        remaining -= len(chunk)


def _descriptor_bytes(descriptor: int, max_bytes: int | None) -> bytes:
    if max_bytes is None:
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            return handle.read()
    return b"".join(_bounded_chunks(descriptor, max_bytes + 1))


def _require_read_size(
    content: bytes, after: os.stat_result, target: Path, max_bytes: int | None
) -> None:
    if len(content) != after.st_size:
        raise ValueError(f"transaction target changed while reading: {target}")
    if max_bytes is not None and len(content) > max_bytes:
        raise ValueError(f"transaction target exceeds {max_bytes} bytes: {target}")


def _bounded_digest(descriptor: int, target: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for chunk in _bounded_chunks(descriptor, MAX_KNOWLEDGE_TARGET_BYTES + 1):
        total += len(chunk)
        _require_hash_size(total, target)
        digest.update(chunk)
    return digest.hexdigest(), total


def _require_hash_size(total: int, target: Path) -> None:
    if total > MAX_KNOWLEDGE_TARGET_BYTES:
        raise TargetTooLargeError(
            f"transaction target exceeds {MAX_KNOWLEDGE_TARGET_BYTES} bytes: {target}"
        )


def _reconciled_target(
    operation_state: tuple[str, str] | None, expected: object, current: str
) -> bool:
    """The target may already hold the after state this transaction will write."""
    if operation_state is None:
        return False
    return expected == operation_state[0] and current == operation_state[1]


def _guardrail_sources_or_fail(vault: Path) -> object:
    try:
        return snapshot_guardrail_sources(vault)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TransactionFailure(
            "persisted guardrails source manifest precondition failed",
            "precondition_failed",
            "quarantined",
        ) from exc


def _sole_claim_record(
    ledger: Mapping[str, object] | None, claim_id: object
) -> Mapping[str, object]:
    records = (
        []
        if ledger is None
        else [
            record for record in ledger["claims"] if str(record["id"]) == claim_id
        ]
    )
    if len(records) != 1:
        raise ValueError("claim target is missing or ambiguous")
    return records[0]


def _claim_record_matches(
    record: Mapping[str, object], item: Mapping[str, object]
) -> bool:
    if record["fingerprint"] != item["fingerprint"]:
        return False
    if sha256_bytes(canonical_json_bytes(record)) != item["record_hash"]:
        return False
    return record["evidence"]["sha256"] == item["evidence_hash"]


def _manifest_entries(manifest: Mapping[str, object]) -> dict[str, str]:
    return {str(item["path"]): str(item["sha256"]) for item in manifest["entries"]}


def _entry_matches(
    before: str, now: str, operation: tuple[str, str] | None
) -> bool:
    if operation is None:
        return now == before
    return before == operation[0] and now in operation


def _lease_row_matches(row: object, expected: Mapping[str, object]) -> bool:
    if row is None:
        return False
    if row["lease_token"] != expected["lease_token"]:
        return False
    if row["fencing_epoch"] != expected["fencing_epoch"]:
        return False
    now = datetime.now(timezone.utc)
    return (
        _parse_timestamp(row["expires_at"]) > now
        and _parse_timestamp(str(expected["expires_at"])) > now
    )


def _is_transient_writer_contention(error: BaseException) -> bool:
    if isinstance(error, sqlite3.OperationalError):
        return _sqlite_contention(error)
    if isinstance(error, OSError):
        return _windows_sharing_violation(error)
    return False


def _sqlite_contention(error: sqlite3.OperationalError) -> bool:
    """The driver's code decides when it has one; otherwise the message does."""
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    message = str(error).casefold()
    return "busy" in message or "locked" in message


def _windows_sharing_violation(error: OSError) -> bool:
    return getattr(error, "winerror", None) in {32, 33} or error.errno in {32, 33}


def _writer_gate_loss_message(heartbeat_lost: bool) -> str:
    """Say what actually went wrong, since only a reclaim is a real loss."""
    if heartbeat_lost:
        return (
            "Markdown writer gate ownership was lost: the lease heartbeat stopped "
            "and another owner reclaimed the gate"
        )
    return "Markdown writer gate ownership was lost: another owner holds the gate"


def _writer_retry_delay(attempt: int, deadline: float) -> float:
    delay = min(
        _WRITER_RETRY_BASE_SECONDS * (2 ** min(attempt, 8)),
        _WRITER_RETRY_CAP_SECONDS,
    )
    return max(0.0, min(delay, deadline - time.monotonic()))


def _recover_initial_contention(
    coordinator: MarkdownCoordinator,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> None:
    deadline = min(deadline, time.monotonic() + _WRITER_WAIT_SECONDS)
    attempt = 0
    while True:
        try:
            coordinator.recover(
                writer_wait_seconds=max(0.0, deadline - time.monotonic()),
                deadline=deadline,
                cancelled=cancelled,
            )
            return
        except (OSError, sqlite3.Error) as exc:
            if not _is_transient_writer_contention(exc):
                raise
            delay = _writer_retry_delay(attempt, deadline)
            if delay <= 0:
                raise
            time.sleep(delay)
            attempt += 1


def stable_operation_id(kind: str, event_id: str, content: bytes) -> str:
    """Return a deterministic operation ID bound to one redacted event payload."""
    if not kind or not event_id:
        raise ValueError("operation kind and event_id must be non-empty")
    content = _require_bytes(content)
    return f"{kind}:{event_id}:{sha256_bytes(content)}"


def _reliability_v3_records_present(state_root: Path) -> bool:
    run_root = Path(state_root) / "run"
    records = (
        run_root / "reliability-v3-migration.json",
        run_root / "reliability-v3-adopted.json",
    )
    return any(path.exists() or path.is_symlink() for path in records)


def _adoption_validation_key(vault: Path, state_root: Path) -> tuple[object, ...]:
    run_root = Path(state_root) / "run"
    records = tuple(
        sha256_bytes(
            read_runtime_bytes(path, state_root, max_bytes=1024 * 1024, owner_only=True)
        )
        for path in (
            run_root / "reliability-v3-migration.json",
            run_root / "reliability-v3-adopted.json",
        )
    )
    integration = read_stable_bytes(
        Path(vault) / "scripts" / "integration_adapter.py",
        16 * 1024 * 1024,
        label="installed integration adapter",
    )
    active = os.lstat(run_root / "markdown-transactions-v3.sqlite3")
    identity = (active.st_mode, active.st_dev, active.st_ino)
    return (str(vault), str(state_root), *records, sha256_bytes(integration), *identity)


def _transient_adoption_contention(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if _is_transient_writer_contention(current):
            return True
        current = current.__cause__
    return False


def _validate_adoption_with_retry(vault: Path, state_root: Path) -> None:
    from installed_memory_repair import require_reliability_v3_adopted

    deadline = time.monotonic() + _ADOPTION_VALIDATION_SECONDS
    while True:
        try:
            require_reliability_v3_adopted(root=vault, state_root=state_root)
            break
        except Exception as exc:
            if not _transient_adoption_contention(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(_WRITER_RETRY_CAP_SECONDS)


def _require_adopted_once(vault: Path, state_root: Path) -> None:
    key = _adoption_validation_key(vault, state_root)
    with _ADOPTION_VALIDATION_LOCK:
        if key in _ADOPTION_VALIDATION_CACHE:
            return
    _validate_adoption_with_retry(vault, state_root)
    with _ADOPTION_VALIDATION_LOCK:
        _ADOPTION_VALIDATION_CACHE.add(key)


def active_markdown_coordinator(vault: Path, state_root: Path) -> MarkdownCoordinator:
    """Open the validated adopted coordinator-v3 database for normal writes."""
    resolved_vault = Path(vault).resolve(strict=True)
    state = Path(state_root).absolute()
    _require_adopted_once(resolved_vault, state)
    path = state / "run" / "markdown-transactions-v3.sqlite3"
    coordinator = MarkdownCoordinator._from_v3_candidate(path, state_root=state)
    coordinator.vault = resolved_vault
    return coordinator


def _default_coordinator() -> MarkdownCoordinator:
    vault = Path(
        os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))
    ).resolve(strict=True)
    state_root = Path(os.environ.get("LLM_WIKI_STATE_ROOT", str(vault))).resolve()
    if _reliability_v3_records_present(state_root):
        return active_markdown_coordinator(vault, state_root)
    return MarkdownCoordinator(vault, state_root)


def _relative_target(coordinator: MarkdownCoordinator, path: Path) -> str:
    absolute = Path(path).absolute()
    try:
        relative = absolute.relative_to(coordinator.vault).as_posix()
    except ValueError as exc:
        raise ValueError(f"knowledge target is outside the vault: {path}") from exc
    coordinator.ensure_target_parent(relative)
    return relative


def _relative_changes(
    coordinator: MarkdownCoordinator, changes: Mapping[Path, bytes | None]
) -> dict[str, bytes | None]:
    relative_changes = {
        _relative_target(coordinator, Path(path)): content
        for path, content in changes.items()
    }
    for content in relative_changes.values():
        if content is None:
            continue
        if len(_require_bytes(content)) > MAX_KNOWLEDGE_TARGET_BYTES:
            raise ValueError("knowledge target size exceeds limit")
    return relative_changes


def _desired_hashes(relative_changes: Mapping[str, bytes | None]) -> dict[str, str]:
    return {
        path: ABSENT if content is None else sha256_bytes(content)
        for path, content in relative_changes.items()
    }


def _persisted_hashes(record: TransactionRecord) -> dict[str, str]:
    return {operation.path: operation.after_hash for operation in record.operations}


def _existing_committed_record(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    relative_changes: Mapping[str, bytes | None],
) -> TransactionRecord | None:
    """The committed record already bound to this operation id, if there is one."""
    if coordinator._record_for_operation_id(operation_id) is None:
        return None
    existing = _settle_operation(coordinator, operation_id)
    if existing is None:
        return None
    if _desired_hashes(relative_changes) != _persisted_hashes(existing):
        raise ValueError("operation_id is already bound to a different request")
    if existing.state != "committed":
        raise RuntimeError(
            f"duplicate mutation ended in noncommitted state {existing.state}"
        )
    _verify_committed_targets(coordinator, existing)
    return existing


def _captured_targets(
    coordinator: MarkdownCoordinator, relative_changes: Mapping[str, bytes | None]
) -> dict[str, bytes | None]:
    with coordinator.writer_gate():
        return {
            relative: coordinator._read_bounded_target(
                coordinator._target(relative), MAX_KNOWLEDGE_TARGET_BYTES
            )
            for relative in relative_changes
        }


def _prepared_change(
    relative: str, content: bytes | None, before: bytes | None
) -> MarkdownChange:
    if content is None:
        if before is None:
            raise FileNotFoundError(relative)
        return MarkdownChange.delete(relative)
    if before is None:
        return MarkdownChange.create(relative, _require_bytes(content))
    return MarkdownChange.replace(relative, _require_bytes(content))


def _prepared_changes(
    relative_changes: Mapping[str, bytes | None],
    captured: Mapping[str, bytes | None],
    preconditions: Mapping[str, object] | None,
) -> tuple[list[MarkdownChange], dict[str, object]]:
    """The changes to prepare, plus the preconditions they were captured under."""
    expected = dict(preconditions or {})
    prepared: list[MarkdownChange] = []
    for relative, content in relative_changes.items():
        before = captured[relative]
        captured_hash = ABSENT if before is None else sha256_bytes(before)
        _require_caller_precondition(expected, relative, captured_hash)
        expected[relative] = captured_hash
        prepared.append(_prepared_change(relative, content, before))
    return prepared, expected


def _require_caller_precondition(
    expected: Mapping[str, object], relative: str, captured_hash: str
) -> None:
    caller_expected = expected.get(relative)
    if caller_expected is not None and caller_expected != captured_hash:
        raise ValueError(f"caller precondition does not match target: {relative}")


def _recovered_duplicate(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    relative_changes: Mapping[str, bytes | None],
    exc: ValueError,
) -> TransactionRecord:
    record = _settle_operation(coordinator, operation_id)
    if record is None:
        raise RuntimeError(
            "winning transaction disappeared during duplicate recovery"
        ) from exc
    if _desired_hashes(relative_changes) != _persisted_hashes(record):
        raise exc
    if record.state != "committed":
        raise RuntimeError(
            f"duplicate mutation ended in noncommitted state {record.state}"
        )
    _verify_committed_targets(coordinator, record)
    return record


def _prepare_or_recover(
    coordinator: MarkdownCoordinator,
    prepared: list[MarkdownChange],
    operation_id: str,
    validators: Sequence[Validator],
    expected: Mapping[str, object],
    relative_changes: Mapping[str, bytes | None],
) -> TransactionRecord | None:
    """None when preparation won the race, else the record that already won."""
    try:
        coordinator.prepare(
            prepared,
            operation_id=operation_id,
            validators=validators,
            preconditions=expected,
        )
    except ValueError as exc:
        if "operation_id is already bound" not in str(exc):
            raise
        return _recovered_duplicate(coordinator, operation_id, relative_changes, exc)
    return None


def mutate_knowledge(
    operation_id: str,
    changes: Mapping[Path, bytes | None],
    *,
    validators: Sequence[Validator] = (),
    preconditions: Mapping[str, object] | None = None,
) -> TransactionRecord:
    """Apply one recoverable mutation with caller-independent before hashes."""
    if not changes:
        raise ValueError("a knowledge mutation requires at least one change")
    coordinator = _default_coordinator()
    relative_changes = _relative_changes(coordinator, changes)
    _recover_initial_contention(coordinator)
    existing = _existing_committed_record(coordinator, operation_id, relative_changes)
    if existing is not None:
        return existing
    captured = _captured_targets(coordinator, relative_changes)
    prepared, expected = _prepared_changes(relative_changes, captured, preconditions)
    recovered = _prepare_or_recover(
        coordinator, prepared, operation_id, validators, expected, relative_changes
    )
    if recovered is not None:
        return recovered
    settled = _settle_operation(coordinator, operation_id)
    if settled is None:
        raise RuntimeError("prepared transaction disappeared before apply")
    return settled


def _settle_operation(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> TransactionRecord | None:
    deadline = min(deadline, time.monotonic() + _WRITER_WAIT_SECONDS)
    while True:
        coordinator._require_operation_active(deadline, cancelled)
        record = coordinator._record_for_operation_id(operation_id)
        if record is None:
            return None
        settled = _settle_nonpreparing_record(
            coordinator, record, deadline=deadline, cancelled=cancelled
        )
        if settled is not None:
            return settled
        _recover_abandoned_preparation(
            coordinator, operation_id, deadline=deadline, cancelled=cancelled
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for duplicate transaction preparation")
        time.sleep(0.01)


def _settle_nonpreparing_record(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> TransactionRecord | None:
    if record.state == "preparing":
        return None
    if record.state in {"prepared", "applying"}:
        return _apply_or_reread_terminal(
            coordinator, record, deadline=deadline, cancelled=cancelled
        )
    return record


def _apply_or_reread_terminal(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> TransactionRecord:
    try:
        return coordinator.apply(record.id, deadline=deadline, cancelled=cancelled)
    except RuntimeError as exc:
        current = coordinator._record(record.id)
        message = f"transaction cannot be applied from state {current.state}"
        if str(exc) != message or current.state in {"prepared", "applying"}:
            raise
        return current


def _preparer_is_alive(coordinator: MarkdownCoordinator, operation_id: str) -> bool:
    with coordinator._connect() as database:
        owner = database.execute(
            'SELECT owner_pid FROM "transaction" WHERE operation_id = ?',
            (operation_id,),
        ).fetchone()
    if owner is None:
        return False
    if owner["owner_pid"] is None:
        return False
    return _pid_alive(owner["owner_pid"])


def _recover_abandoned_preparation(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> None:
    if _preparer_is_alive(coordinator, operation_id):
        return
    coordinator.recover(deadline=deadline, cancelled=cancelled)


def _verify_committed_targets(
    coordinator: MarkdownCoordinator, record: TransactionRecord
) -> None:
    with coordinator.writer_gate():
        drifted = [
            operation.path
            for operation in record.operations
            if coordinator._current_hash(operation.path) != operation.after_hash
        ]
    if drifted:
        raise TransactionDriftError(record.id, drifted)


def _append_request_matches(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord,
    relative: str,
    block: bytes,
) -> bool:
    if not _single_operation_on(record, relative):
        return False
    content = _append_after_bytes(coordinator, record)
    if content is None or not content.endswith(block):
        return False
    prefix = content[: len(content) - len(block)] if block else content
    return _append_before_hash(record, prefix) == record.operations[0].before_hash


def _single_operation_on(record: TransactionRecord, relative: str) -> bool:
    return len(record.operations) == 1 and record.operations[0].path == relative


def _append_after_bytes(
    coordinator: MarkdownCoordinator, record: TransactionRecord
) -> bytes | None:
    plan = coordinator._load_verified_plan(record)
    after = plan["operations"][0]["after"]
    if not isinstance(after, dict):
        return None
    return (coordinator.transaction_root / record.id / after["artifact"]).read_bytes()


def _append_before_hash(record: TransactionRecord, prefix: bytes) -> str:
    if record.operations[0].kind == "create":
        return ABSENT
    return sha256_bytes(prefix)


_AppendAttemptResult = TransactionRecord | Literal["advance", "retry"]


def _coerce_legacy_append_arguments(
    operation_id: str | Path | None,
    path: Path | bytes | None,
    block: bytes | None,
) -> tuple[str | Path | None, Path | bytes | None, bytes | None]:
    if block is not None:
        return operation_id, path, block
    if not isinstance(operation_id, Path):
        return operation_id, path, block
    if not isinstance(path, bytes):
        return operation_id, path, block
    return None, operation_id, path


def _append_operation_id(value: str | Path | None) -> str:
    if value is None:
        return f"append:{uuid.uuid4().hex}"
    if not value:
        raise ValueError("operation_id must be non-empty")
    return value  # type: ignore[return-value]


def _normalize_append_request(
    operation_id: str | Path | None,
    path: Path | bytes | None,
    block: bytes | None,
) -> tuple[str, Path, bytes]:
    operation_id, path, block = _coerce_legacy_append_arguments(
        operation_id, path, block
    )
    if not isinstance(path, Path):
        raise TypeError("knowledge append path must be a Path")
    content = _require_bytes(block)
    if len(content) > MAX_KNOWLEDGE_TARGET_BYTES:
        raise ValueError("knowledge append block size exceeds limit")
    return _append_operation_id(operation_id), path, content


def _append_candidate_id(operation_id: str, attempt: int) -> str:
    if attempt == 0:
        return operation_id
    return f"{operation_id}:cas:{attempt}"


def _capture_append_before(
    coordinator: MarkdownCoordinator,
    relative: str,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> bytes | None:
    for _attempt in range(64):
        try:
            coordinator._require_operation_active(deadline, cancelled)
            with coordinator.writer_gate(
                wait_seconds=min(
                    _WRITER_WAIT_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
            ):
                coordinator._require_operation_active(deadline, cancelled)
                return coordinator._read_bounded_target(
                    coordinator._target(relative), MAX_KNOWLEDGE_TARGET_BYTES
                )
        except TimeoutError:
            continue
    raise TimeoutError("timed out capturing the knowledge append target")


def _append_change(relative: str, before: bytes | None, block: bytes) -> MarkdownChange:
    content = (before or b"") + block
    if len(content) > MAX_KNOWLEDGE_TARGET_BYTES:
        raise ValueError("prospective knowledge target size exceeds limit")
    if before is None:
        return MarkdownChange.create(
            relative, content, max_before_bytes=MAX_KNOWLEDGE_TARGET_BYTES
        )
    return MarkdownChange.replace(
        relative, content, max_before_bytes=MAX_KNOWLEDGE_TARGET_BYTES
    )


def _append_expected_hash(before: bytes | None) -> str:
    if before is None:
        return ABSENT
    return sha256_bytes(before)


def _append_preconditions(
    relative: str,
    before: bytes | None,
    extra: Mapping[str, object] | None,
) -> dict[str, object]:
    result = dict(extra or {})
    expected = _append_expected_hash(before)
    if relative in result and result[relative] != expected:
        raise ValueError("append target precondition conflicts with captured bytes")
    result[relative] = expected
    return result


def _classify_settled_append(
    coordinator: MarkdownCoordinator,
    record: TransactionRecord | None,
    relative: str,
    block: bytes,
) -> _AppendAttemptResult:
    if record is None:
        return "retry"
    if not _append_request_matches(coordinator, record, relative, block):
        raise ValueError("operation_id is already bound to a different request")
    if record.state == "committed":
        return record
    return "advance"


def _append_transaction_failure(error: TransactionFailure) -> Literal["advance"]:
    retryable = {"before_hash_mismatch", "unknown_target_bytes", "precondition_failed"}
    if error.code not in retryable:
        raise error
    return "advance"


def _append_runtime_failure(error: RuntimeError) -> Literal["advance"]:
    if "transaction cannot be applied from state" not in str(error):
        raise error
    return "advance"


def _settle_append_candidate(
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    block: bytes,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    try:
        record = _settle_operation(
            coordinator, candidate_id, deadline=deadline, cancelled=cancelled
        )
    except TransactionFailure as exc:
        return _append_transaction_failure(exc)
    except RuntimeError as exc:
        return _append_runtime_failure(exc)
    except TimeoutError:
        return "retry"
    return _classify_settled_append(coordinator, record, relative, block)


def _append_value_failure(
    error: ValueError,
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    block: bytes,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    if "operation_id is already bound" in str(error):
        return _settle_append_candidate(
            coordinator,
            candidate_id,
            relative,
            block,
            deadline=deadline,
            cancelled=cancelled,
        )
    if "precondition changed" in str(error):
        return "retry"
    raise error


def _append_prepare_failure(
    error: Exception,
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    block: bytes,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    if isinstance(error, TransactionFailure):
        return _append_transaction_failure(error)
    if isinstance(error, ValueError):
        return _append_value_failure(
            error,
            coordinator,
            candidate_id,
            relative,
            block,
            deadline=deadline,
            cancelled=cancelled,
        )
    if isinstance(error, RuntimeError):
        return _append_runtime_failure(error)
    return "retry"


def _prepare_append_candidate(
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    before: bytes | None,
    block: bytes,
    *,
    extra_preconditions: Mapping[str, object] | None,
    content_guard: Literal["model_output"] | None,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    change = _append_change(relative, before, block)
    try:
        coordinator.prepare(
            [change],
            operation_id=candidate_id,
            preconditions=_append_preconditions(relative, before, extra_preconditions),
            content_guard=content_guard,
            deadline=deadline,
            cancelled=cancelled,
        )
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        TransactionFailure,
        RuntimeError,
        TimeoutError,
    ) as exc:
        return _append_prepare_failure(
            exc,
            coordinator,
            candidate_id,
            relative,
            block,
            deadline=deadline,
            cancelled=cancelled,
        )
    return _settle_append_candidate(
        coordinator,
        candidate_id,
        relative,
        block,
        deadline=deadline,
        cancelled=cancelled,
    )


def _run_append_candidate(
    coordinator: MarkdownCoordinator,
    candidate_id: str,
    relative: str,
    block: bytes,
    *,
    extra_preconditions: Mapping[str, object] | None,
    content_guard: Literal["model_output"] | None,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> _AppendAttemptResult:
    if coordinator._record_for_operation_id(candidate_id) is not None:
        return _settle_append_candidate(
            coordinator,
            candidate_id,
            relative,
            block,
            deadline=deadline,
            cancelled=cancelled,
        )
    before = _capture_append_before(
        coordinator, relative, deadline=deadline, cancelled=cancelled
    )
    return _prepare_append_candidate(
        coordinator,
        candidate_id,
        relative,
        before,
        block,
        extra_preconditions=extra_preconditions,
        content_guard=content_guard,
        deadline=deadline,
        cancelled=cancelled,
    )


def _append_until_committed(
    coordinator: MarkdownCoordinator,
    operation_id: str,
    relative: str,
    block: bytes,
    *,
    extra_preconditions: Mapping[str, object] | None = None,
    content_guard: Literal["model_output"] | None = None,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> TransactionRecord:
    attempt = 0
    while attempt < 64:
        candidate_id = _append_candidate_id(operation_id, attempt)
        outcome = _run_append_candidate(
            coordinator,
            candidate_id,
            relative,
            block,
            extra_preconditions=extra_preconditions,
            content_guard=content_guard,
            deadline=deadline,
            cancelled=cancelled,
        )
        if isinstance(outcome, TransactionRecord):
            return outcome
        if outcome == "advance":
            attempt += 1
    raise TimeoutError("knowledge append did not converge after 64 CAS attempts")


def append_knowledge(
    operation_id: str | Path | None = None,
    path: Path | bytes | None = None,
    block: bytes | None = None,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> TransactionRecord:
    """CAS-append Markdown or project JSONL bytes, retrying concurrent winners."""
    operation_id, append_path, block = _normalize_append_request(
        operation_id, path, block
    )
    coordinator = _default_coordinator()
    relative = _relative_target(coordinator, append_path)
    _recover_initial_contention(
        coordinator, deadline=deadline, cancelled=cancelled
    )
    return _append_until_committed(
        coordinator,
        operation_id,
        relative,
        block,
        deadline=deadline,
        cancelled=cancelled,
    )


def _capture_append_context_matches(
    stored: Mapping[str, object], current: Mapping[str, object]
) -> bool:
    stored_fence = stored.get("intent_fence")
    current_fence = current.get("intent_fence")
    if not isinstance(stored_fence, Mapping) or not isinstance(current_fence, Mapping):
        return False
    return stored.get("capture_binding") == current.get("capture_binding") and (
        stored_fence.get("intent_id"),
        stored_fence.get("mode"),
    ) == (
        current_fence.get("intent_id"),
        current_fence.get("mode"),
    )


def append_captured_knowledge(
    coordinator: MarkdownCoordinator,
    owner: object,
    operation_id: str,
    path: Path,
    block: bytes,
    *,
    preconditions: Mapping[str, object],
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> TransactionRecord:
    """CAS-append one provider decision under live capture preconditions."""
    operation_id, append_path, content = _normalize_append_request(
        operation_id, path, block
    )
    relative = _relative_target(coordinator, append_path)
    expected = coordinator._validate_preconditions(preconditions)
    with coordinator.writer_gate(owner=owner):
        with coordinator._connect() as database:
            coordinator._check_capture_preconditions(database, expected)
        _recover_initial_contention(
            coordinator, deadline=deadline, cancelled=cancelled
        )
        record = _append_until_committed(
            coordinator,
            operation_id,
            relative,
            content,
            extra_preconditions=preconditions,
            content_guard="model_output",
            deadline=deadline,
            cancelled=cancelled,
        )
    if not _capture_append_context_matches(record.preconditions, expected):
        raise ValueError("capture append transaction preconditions conflict")
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _future_timestamp(seconds: float) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat().replace("+00:00", "Z")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, pid
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
            handle, ctypes.byref(exit_code)
        ):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _use_posix_dir_fd() -> bool:
    return os.name == "posix"


def _windows_acl_identity() -> str:
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{username}" if domain else username


def _run_acl_command(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=5,
    )


def _acl_permission(path: Path, identity: str) -> str:
    if path.is_dir():
        return f"{identity}:(OI)(CI)(F)"
    return f"{identity}:(F)"


def _acl_failure(path: Path, changed: subprocess.CompletedProcess[bytes]) -> None:
    detail = _acl_output_text(changed.stderr).strip()
    if not detail:
        detail = "icacls failed"
    raise PermissionError(f"could not apply owner-only ACL to {path}: {detail}")


def _apply_windows_acl(
    path: Path, permission: str
) -> subprocess.CompletedProcess[bytes]:
    try:
        changed = _run_acl_command(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/remove:g",
                "*S-1-5-18",
                "*S-1-5-32-544",
                "*S-1-5-32-545",
                "*S-1-5-11",
                "*S-1-3-0",
                "*S-1-3-4",
                "/grant:r",
                permission,
            ]
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermissionError(f"could not apply owner-only ACL to {path}: {exc}") from exc
    if changed.returncode != 0:
        _acl_failure(path, changed)
    try:
        verified = _run_acl_command(["icacls", str(path)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermissionError(f"could not verify owner-only ACL on {path}: {exc}") from exc
    if verified.returncode != 0:
        _acl_failure(path, verified)
    return verified


def _verified_owner_acl_line(
    path: Path, owner_lines: list[str]
) -> str:
    if len(owner_lines) != 1:
        raise PermissionError(f"owner-only ACL verification failed for {path}")
    owner = owner_lines[0]
    if "(F)" not in owner:
        raise PermissionError(f"owner-only ACL verification failed for {path}")
    if "(I)" in owner:
        raise PermissionError(f"owner-only ACL verification failed for {path}")
    return owner


def _verify_no_other_acl(path: Path, acl_lines: list[str], identity: str) -> None:
    for line in acl_lines:
        if identity.casefold() not in line.casefold():
            raise PermissionError(f"owner-only ACL verification failed for {path}")


def _harden_windows_acl(path: Path) -> None:
    identity = _windows_acl_identity()
    permission = _acl_permission(path, identity)
    verified = _apply_windows_acl(path, permission)
    output = _acl_output_text(verified.stdout)
    acl_lines = [line.strip() for line in output.splitlines() if ":(" in line]
    owner_lines = [line for line in acl_lines if identity.casefold() in line.casefold()]
    _verified_owner_acl_line(path, owner_lines)
    _verify_no_other_acl(path, acl_lines, identity)


def _acl_output_text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="ignore")
    return value or ""


def _harden_owner_only(path: Path, mode: int) -> None:
    if os.name == "nt":
        _harden_windows_acl(path)
    else:
        _set_owner_only(path, mode)


class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _WindowsDispositionInformation(ctypes.Structure):
    _fields_ = [("delete_file", wintypes.BOOL)]


def _open_windows_directory(path: Path) -> int:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    file_read_attributes = 0x80
    file_share_read = 0x1
    file_share_write = 0x2
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), f"cannot lock Windows directory: {path}")
    information = _WindowsFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, f"cannot identify Windows directory: {path}")
    if information.file_attributes & 0x400:
        kernel32.CloseHandle(handle)
        raise RuntimeError(f"Windows directory handle resolves to a reparse point: {path}")
    return handle


def _close_windows_handle(handle: int) -> None:
    if not ctypes.windll.kernel32.CloseHandle(handle):
        raise OSError(ctypes.get_last_error(), "cannot close Windows directory handle")


def _open_windows_file_for_mutation(path: Path) -> int:
    kernel32 = ctypes.windll.kernel32
    generic_read = 0x80000000
    delete_access = 0x00010000
    share_read = 0x1
    share_delete = 0x4
    open_existing = 3
    open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        generic_read | delete_access,
        share_read | share_delete,
        None,
        open_existing,
        open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f"cannot open Windows mutation handle: {path}")
    return handle


def _delete_windows_handle(file_handle: int) -> None:
    information = _WindowsDispositionInformation(True)
    file_disposition_info = 4
    kernel32 = ctypes.windll.kernel32
    if not kernel32.SetFileInformationByHandle(
        file_handle,
        file_disposition_info,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise OSError(kernel32.GetLastError(), "cannot delete Windows target")


class MarkdownCoordinator:
    """Prepare and apply durable multi-file Markdown transactions."""

    @classmethod
    def _from_v3_candidate(
        cls, path: Path, *, state_root: Path
    ) -> MarkdownCoordinator:
        validate_coordinator_v3_database(path, state_root=state_root)
        coordinator = cls.__new__(cls)
        coordinator.vault = Path(state_root).resolve()
        coordinator.state_root = Path(state_root)
        coordinator.run_root = Path(path).parent
        coordinator.transaction_root = coordinator.run_root / "transactions"
        coordinator.database_path = Path(path)
        coordinator._local = threading.local()
        coordinator._database_contract = _COORDINATOR_V3_CONTRACT
        return coordinator

    def __init__(self, vault: Path, state_root: Path):
        self.vault = Path(vault).resolve(strict=True)
        if not self.vault.is_dir():
            raise ValueError(f"vault is not a directory: {self.vault}")
        self.state_root = Path(state_root)
        validate_state_root(self.state_root)
        self.run_root = self.state_root / "run"
        self.transaction_root = self.run_root / "transactions"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.transaction_root.mkdir(parents=True, exist_ok=True)
        _set_owner_only(self.run_root, 0o700)
        _set_owner_only(self.transaction_root, 0o700)
        self.database_path = self.run_root / "markdown-transactions.sqlite3"
        self._local = threading.local()
        self._initialize_database()

    @contextlib.contextmanager
    def _connect(
        self, *, busy_ms: int | None = None
    ) -> Iterator[sqlite3.Connection]:
        database = open_operational_db(
            self.database_path,
            busy_ms=DEFAULTS.markdown_busy_ms if busy_ms is None else busy_ms,
            contract=getattr(self, "_database_contract", None),
        )
        try:
            # Preserve this legacy context manager's implicit commit/rollback API.
            database.isolation_level = "DEFERRED"
            _relax_legacy_state_constraint(database)
            with database:
                yield database
        finally:
            database.close()

    def _ownership_registry(self) -> object:
        from operational_ownership import OwnershipRegistry

        if getattr(self, "_database_contract", None) != _COORDINATOR_V3_CONTRACT:
            return OwnershipRegistry(self.state_root)
        return OwnershipRegistry._from_adopted_database(
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
        now = datetime.now(timezone.utc)
        expires_at = min(owner.expires_at, now + timedelta(seconds=30))
        token = uuid.uuid4().hex
        with self._connect() as database, begin_immediate(database):
            registry.require(database, owner)
            if database.execute(
                "SELECT 1 FROM intent_fences WHERE intent_id=?", (intent_id,)
            ).fetchone() is not None:
                raise RuntimeError("intent_fenced")
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
                    now.isoformat().replace("+00:00", "Z"),
                    expires_at.isoformat().replace("+00:00", "Z"),
                ),
            ).rowcount
            if inserted != 1:
                raise RuntimeError("intent_fence_failed")
        return IntentFence(intent_id, mode, token, epoch, owner, expires_at)

    def release_intent_fence(self, fence: IntentFence) -> None:
        if not isinstance(fence, IntentFence):
            raise TypeError("fence must be an IntentFence")
        with self._connect() as database, begin_immediate(database):
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

    def project_capture_binding(
        self, binding: object, *, intent_fence: IntentFence
    ) -> None:
        from memory_queue import CaptureTaskBinding

        if not isinstance(binding, CaptureTaskBinding) or binding.seal_digest is None:
            raise ValueError("capture binding must be sealed")
        _require_matching_intent(binding, intent_fence)
        now = datetime.now(timezone.utc)
        with self._connect() as database, begin_immediate(database):
            _require_live_intent_fence(database, intent_fence, now)
            _project_binding_row(database, binding, intent_fence, now)

    def _initialize_database(self) -> None:
        with self._connect() as database:
            with begin_immediate(database):
                database.executescript(_COORDINATOR_V2_SCHEMA_SQL)
                _add_writer_owner_columns(database)
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
                    (_now(),),
                )
                self._backfill_parent_identities(database)

    def reserve_project_checkpoint(
        self,
        project: str,
        event: Mapping[str, object],
        lease: Mapping[str, object],
    ) -> ProjectCheckpointReservation:
        """Atomically fence and reserve one idempotent project sequence."""
        base = self.normalize_project_checkpoint(project, event)
        occurrence_id = _require_checkpoint_key(
            base.get("occurrence_id"), "occurrence_id"
        )
        idempotency_key = _require_checkpoint_key(
            base.get("idempotency_key"), "idempotency_key"
        )
        precondition = self._checkpoint_lease(project, lease)
        with self._connect() as database, begin_immediate(database):
            self._check_project_lease(database, precondition)
            existing = self._existing_checkpoint(
                database, project, base, occurrence_id, idempotency_key
            )
            if existing is not None:
                return self._reserved_or_retried(
                    database, project, existing, precondition
                )
            return self._reserve_new_checkpoint(
                database, project, base, occurrence_id, idempotency_key, precondition
            )

    def _checkpoint_lease(
        self, project: str, lease: Mapping[str, object]
    ) -> Mapping[str, object]:
        precondition = self._validate_preconditions({"project_lease": lease})[
            "project_lease"
        ]
        assert isinstance(precondition, Mapping)
        if precondition["project"] != project:
            raise ValueError("project lease does not match checkpoint project")
        return precondition

    def _existing_checkpoint(
        self,
        database: sqlite3.Connection,
        project: str,
        base: Mapping[str, object],
        occurrence_id: str,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        """The row this reservation has already been made under, if any."""
        occurrence = database.execute(
            "SELECT * FROM project_checkpoints "
            "WHERE project = ? AND occurrence_id = ?",
            (project, occurrence_id),
        ).fetchone()
        deduplicated = database.execute(
            "SELECT * FROM project_checkpoints "
            "WHERE project = ? AND idempotency_key = ?",
            (project, idempotency_key),
        ).fetchone()
        self._require_consistent_checkpoint(
            occurrence, deduplicated, base, idempotency_key
        )
        return occurrence or deduplicated

    def _require_consistent_checkpoint(
        self,
        occurrence: sqlite3.Row | None,
        deduplicated: sqlite3.Row | None,
        base: Mapping[str, object],
        idempotency_key: str,
    ) -> None:
        allocated = {"project", "sequence", "last_applied_sequence"}
        if occurrence is not None:
            if occurrence["idempotency_key"] != idempotency_key:
                raise ValueError(
                    "occurrence_id is already bound to another idempotency key"
                )
            self._require_same_checkpoint_event(occurrence, base, allocated)
        if deduplicated is not None:
            self._require_same_checkpoint_event(
                deduplicated, base, allocated | {"occurrence_id"}
            )

    def _reserved_or_retried(
        self,
        database: sqlite3.Connection,
        project: str,
        existing: sqlite3.Row,
        precondition: Mapping[str, object],
    ) -> ProjectCheckpointReservation:
        if existing["state"] != "quarantined":
            return self._project_reservation(existing, duplicate=True)
        return self._retry_quarantined_checkpoint(
            database, project, existing, precondition
        )

    def _retry_quarantined_checkpoint(
        self,
        database: sqlite3.Connection,
        project: str,
        existing: sqlite3.Row,
        precondition: Mapping[str, object],
    ) -> ProjectCheckpointReservation:
        """A quarantined sequence is retried as a new attempt, not a new row."""
        attempt_number = int(existing["attempt_number"]) + 1
        parent_operation_id = str(existing["operation_id"])
        operation_id = self._project_attempt_operation_id(
            project,
            int(existing["sequence"]),
            attempt_number,
            int(precondition["fencing_epoch"]),
            str(existing["event_json"]),
        )
        database.execute(
            "UPDATE project_checkpoints SET lease_token = ?, "
            "fencing_epoch = ?, operation_id = ?, attempt_number = ?, "
            "parent_operation_id = ?, transaction_id = NULL, state = 'reserved' "
            "WHERE project = ? AND sequence = ? AND state = 'quarantined'",
            (
                precondition["lease_token"],
                precondition["fencing_epoch"],
                operation_id,
                attempt_number,
                parent_operation_id,
                project,
                existing["sequence"],
            ),
        )
        database.execute(
            "INSERT INTO project_checkpoint_attempts "
            "(project, sequence, attempt_number, operation_id, "
            "parent_operation_id, lease_token, fencing_epoch, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?)",
            (
                project,
                existing["sequence"],
                attempt_number,
                operation_id,
                parent_operation_id,
                precondition["lease_token"],
                precondition["fencing_epoch"],
                _now(),
            ),
        )
        return self._project_reservation(
            self._checkpoint_row(database, project, int(existing["sequence"]))
        )

    @staticmethod
    def _checkpoint_row(
        database: sqlite3.Connection, project: str, sequence: int
    ) -> sqlite3.Row | None:
        return database.execute(
            "SELECT * FROM project_checkpoints WHERE project = ? AND sequence = ?",
            (project, sequence),
        ).fetchone()

    def _reserve_new_checkpoint(
        self,
        database: sqlite3.Connection,
        project: str,
        base: Mapping[str, object],
        occurrence_id: str,
        idempotency_key: str,
        precondition: Mapping[str, object],
    ) -> ProjectCheckpointReservation:
        row = database.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last_sequence, "
            "COALESCE(MAX(CASE WHEN state = 'committed' THEN sequence END), 0) "
            "AS last_applied_sequence FROM project_checkpoints WHERE project = ?",
            (project,),
        ).fetchone()
        sequence = int(row["last_sequence"]) + 1
        event_json = _checkpoint_event_json(base, project, sequence, row)
        operation_id = self._project_attempt_operation_id(
            project, sequence, 1, int(precondition["fencing_epoch"]), event_json
        )
        _insert_checkpoint(
            database,
            project,
            sequence,
            occurrence_id,
            idempotency_key,
            event_json,
            precondition,
            operation_id,
        )
        return self._project_reservation(
            self._checkpoint_row(database, project, sequence)
        )

    @staticmethod
    def _project_attempt_operation_id(
        project: str,
        sequence: int,
        attempt_number: int,
        fencing_epoch: int,
        event_json: str,
    ) -> str:
        return (
            f"project:{project}:{sequence}:attempt:{attempt_number}:"
            f"epoch:{fencing_epoch}:{sha256_bytes(event_json.encode('utf-8'))}"
        )

    @staticmethod
    def normalize_project_checkpoint(
        project: str, event: Mapping[str, object]
    ) -> dict[str, object]:
        """Canonicalize and fully validate an event before reserving any state."""
        if not isinstance(project, str) or not project:
            raise ValueError("project must be a non-empty string")
        if not isinstance(event, Mapping):
            raise TypeError("checkpoint event must be a mapping")
        allocated = {"project", "sequence", "last_applied_sequence"}
        if allocated.intersection(event):
            raise ValueError("checkpoint event contains coordinator-allocated fields")
        normalized = json.loads(canonical_json_bytes(dict(event)))
        candidate = copy.deepcopy(normalized)
        candidate.update(project=project, sequence=1, last_applied_sequence=0)
        validate_schema(candidate, _PROJECT_CHECKPOINT_SCHEMA)
        return normalized

    @staticmethod
    def _require_same_checkpoint_event(
        row: sqlite3.Row,
        requested: Mapping[str, object],
        ignored: set[str],
    ) -> None:
        stored = json.loads(row["event_json"])
        candidate = dict(requested)
        for field in ignored:
            stored.pop(field, None)
            candidate.pop(field, None)
        if canonical_json_bytes(stored) != canonical_json_bytes(candidate):
            label = "idempotency_key" if "occurrence_id" in ignored else "occurrence_id"
            raise ValueError(f"{label} is already bound to another event")

    @staticmethod
    def _project_reservation(
        row: Mapping[str, object], *, duplicate: bool = False
    ) -> ProjectCheckpointReservation:
        return ProjectCheckpointReservation(
            project=str(row["project"]),
            sequence=int(row["sequence"]),
            occurrence_id=str(row["occurrence_id"]),
            idempotency_key=str(row["idempotency_key"]),
            event_json=str(row["event_json"]),
            operation_id=str(row["operation_id"]),
            attempt_number=int(row["attempt_number"]),
            state=str(row["state"]),
            transaction_id=str(row["transaction_id"])
            if row["transaction_id"]
            else None,
            parent_operation_id=str(row["parent_operation_id"])
            if row["parent_operation_id"]
            else None,
            duplicate=duplicate,
        )

    def _backfill_parent_identities(self, database: sqlite3.Connection) -> None:
        rows = database.execute(
            'SELECT operation.*, "transaction".state AS transaction_state '
            'FROM "operation" JOIN "transaction" '
            'ON "transaction".id = operation.transaction_id '
            "WHERE (operation.parent_device IS NULL OR operation.parent_inode IS NULL) "
            "AND \"transaction\".state IN ('prepared', 'applying')"
        ).fetchall()
        for row in rows:
            target = self._target(row["path"])
            content, identity = self._capture_target(target)
            current_hash = ABSENT if content is None else sha256_bytes(content)
            expected_hashes = {row["after_hash"]} if row["applied"] else {
                row["before_hash"],
                row["after_hash"],
            }
            if current_hash not in expected_hashes:
                raise RuntimeError(
                    f"cannot migrate parent identity with unknown target bytes: {row['path']}"
                )
            database.execute(
                'UPDATE "operation" SET parent_device = ?, parent_inode = ? '
                "WHERE transaction_id = ? AND position = ?",
                (*_encode_parent_identity(identity), row["transaction_id"], row["position"]),
            )

    def _normalized_changes(
        self, changes: Sequence[MarkdownChange]
    ) -> tuple[MarkdownChange, ...]:
        normalized = tuple(self._validate_change(change) for change in changes)
        paths = [
            unicodedata.normalize("NFC", change.path).casefold()
            for change in normalized
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate transaction target")
        return normalized

    def _existing_bound_record(
        self, operation_id: str, request_hash: str
    ) -> TransactionRecord | None:
        existing = self._record_for_operation_id(operation_id)
        if existing is None:
            return None
        if self._request_hash_for_operation_id(operation_id) != request_hash:
            raise ValueError("operation_id is already bound to a different request")
        return existing

    def _create_artifact_roots(
        self, transaction_id: str, deadline: float, cancelled: Callable[[], bool] | None
    ) -> _ArtifactRoots:
        artifact_root = self.transaction_root / transaction_id
        before_root = artifact_root / "before"
        after_root = artifact_root / "after"
        try:
            self._require_operation_active(deadline, cancelled)
            artifact_root.mkdir(parents=True)
            _harden_owner_only(artifact_root, 0o700)
            before_root.mkdir()
            after_root.mkdir()
            if os.name != "nt":
                for directory in (before_root, after_root):
                    _harden_owner_only(directory, 0o700)
        except BaseException:
            self._remove_artifacts(artifact_root)
            raise
        return _ArtifactRoots(artifact_root, before_root, after_root)

    def _insert_preparing_row(
        self,
        transaction_id: str,
        operation_id: str,
        request_hash: str,
        persisted_preconditions: Mapping[str, object],
        timestamp: str,
        parent_transaction_id: str | None,
        artifact_root: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> TransactionRecord | None:
        """None once the row is inserted, or the record that already owns the id."""
        try:
            self._require_operation_active(deadline, cancelled)
            with self._connect() as database, begin_immediate(
                database,
                before_commit=lambda: self._require_operation_active(
                    deadline, cancelled
                ),
            ):
                database.execute(
                    'INSERT INTO "transaction" '
                    "(id, operation_id, request_hash, state, preconditions_json, plan_hash, "
                    "created_at, updated_at, parent_transaction_id, owner_pid) "
                    "VALUES (?, ?, ?, 'preparing', ?, '', ?, ?, ?, ?)",
                    (
                        transaction_id,
                        operation_id,
                        request_hash,
                        canonical_json_bytes(persisted_preconditions).decode("utf-8"),
                        timestamp,
                        timestamp,
                        parent_transaction_id,
                        os.getpid(),
                    ),
                )
        except sqlite3.IntegrityError:
            self._remove_artifacts(artifact_root)
            existing = self._record_for_operation_id(operation_id)
            if existing is None:
                raise ValueError(
                    "operation_id is already bound to a different request"
                ) from None
            if self._request_hash_for_operation_id(operation_id) != request_hash:
                raise ValueError(
                    "operation_id is already bound to a different request"
                ) from None
            return existing
        return None

    def _staged_change(
        self,
        position: int,
        change: MarkdownChange,
        roots: _ArtifactRoots,
        persisted_preconditions: Mapping[str, object],
    ) -> _StagedOperation:
        target = self._target(change.path)
        before, parent_identity = self._capture_target(
            target, max_before_bytes=change.max_before_bytes
        )
        _require_capture_matches_kind(change, before)
        before_hash = _content_hash(before)
        _require_change_precondition(change, before_hash, persisted_preconditions)
        before_description = self._stage_state(roots.before, position, before)
        after_description = self._stage_state(roots.after, position, change.content)
        return _StagedOperation(
            MarkdownOperation(
                change.kind, change.path, before_hash, _content_hash(change.content)
            ),
            parent_identity,
            {
                "kind": change.kind,
                "path": change.path,
                "before": before_description,
                "after": after_description,
            },
        )

    def _staged_operations(
        self,
        normalized: tuple[MarkdownChange, ...],
        roots: _ArtifactRoots,
        persisted_preconditions: Mapping[str, object],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _StagedPlan:
        operations: list[MarkdownOperation] = []
        parent_identities: list[tuple[int, int]] = []
        plan_operations: list[dict[str, object]] = []
        for position, change in enumerate(normalized):
            self._require_operation_active(deadline, cancelled)
            staged = self._staged_change(
                position, change, roots, persisted_preconditions
            )
            operations.append(staged.operation)
            parent_identities.append(staged.parent_identity)
            plan_operations.append(staged.plan_entry)
        return _StagedPlan(operations, parent_identities, plan_operations)

    def _write_plan_artifacts(
        self,
        plan: dict[str, object],
        roots: _ArtifactRoots,
        transaction_id: str,
        request_hash: str,
        staged: _StagedPlan,
    ) -> bytes:
        self._verify_plan_artifacts(plan, roots.artifact)
        plan_bytes = canonical_json_bytes(plan)
        self._write_new_file(roots.artifact / "plan.json", plan_bytes)
        self._write_new_file(
            roots.artifact / "manifest.json",
            canonical_json_bytes(
                _recovery_manifest(transaction_id, request_hash, plan_bytes, staged)
            ),
        )
        for directory in (
            roots.before,
            roots.after,
            roots.artifact,
            self.transaction_root,
        ):
            fsync_directory(directory)
        return plan_bytes

    def _operation_rows_for_insert(
        self, transaction_id: str, staged: _StagedPlan
    ) -> list[tuple]:
        return [
            (
                transaction_id,
                position,
                operation.kind,
                operation.path,
                operation.before_hash,
                operation.after_hash,
                *_encode_parent_identity(staged.parent_identities[position]),
            )
            for position, operation in enumerate(staged.operations)
        ]

    def _commit_prepared_row(
        self,
        transaction_id: str,
        plan_bytes: bytes,
        timestamp: str,
        staged: _StagedPlan,
        project_reservation: ProjectCheckpointReservation | None,
        persisted_preconditions: Mapping[str, object],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        try:
            self._require_operation_active(deadline, cancelled)
            with self._connect() as database, begin_immediate(
                database,
                before_commit=lambda: self._require_operation_active(
                    deadline, cancelled
                ),
            ):
                if project_reservation is not None:
                    self._bind_project_reservation(
                        database,
                        project_reservation,
                        transaction_id,
                        persisted_preconditions,
                    )
                database.execute(
                    'UPDATE "transaction" SET state = \'prepared\', plan_hash = ?, '
                    "updated_at = ?, owner_pid = NULL "
                    "WHERE id = ? AND state = 'preparing'",
                    (sha256_bytes(plan_bytes), timestamp, transaction_id),
                )
                database.executemany(
                    'INSERT INTO "operation" '
                    "(transaction_id, position, kind, path, before_hash, after_hash, "
                    "parent_device, parent_inode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    self._operation_rows_for_insert(transaction_id, staged),
                )
        except sqlite3.IntegrityError:
            raise RuntimeError(
                "transaction operations could not be persisted"
            ) from None

    def _discard_failed_preparation(
        self,
        transaction_id: str,
        artifact_root: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        record = self._record_if_present(transaction_id)
        if record is not None and record.state != "preparing":
            return
        with self._connect() as database, begin_immediate(
            database,
            before_commit=lambda: self._require_operation_active(deadline, cancelled),
        ):
            database.execute(
                'DELETE FROM "transaction" WHERE id = ? AND state = \'preparing\'',
                (transaction_id,),
            )
        self._remove_artifacts(artifact_root)

    def _stage_and_commit(
        self,
        normalized: tuple[MarkdownChange, ...],
        roots: _ArtifactRoots,
        persisted_preconditions: Mapping[str, object],
        request_hash: str,
        transaction_id: str,
        timestamp: str,
        *,
        validators: Sequence[Validator],
        content_guard: object,
        project_reservation: ProjectCheckpointReservation | None,
        parent_transaction_id: str | None,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> TransactionRecord:
        staged = self._staged_operations(
            normalized, roots, persisted_preconditions, deadline, cancelled
        )
        self._killpoint("after_images_fsynced", parent_transaction_id)
        plan = _validated_plan(
            transaction_id, staged.plan_operations, content_guard, validators
        )
        plan_bytes = self._write_plan_artifacts(
            plan, roots, transaction_id, request_hash, staged
        )
        self._killpoint("after_plan_fsynced", parent_transaction_id)
        self._commit_prepared_row(
            transaction_id,
            plan_bytes,
            timestamp,
            staged,
            project_reservation,
            persisted_preconditions,
            deadline,
            cancelled,
        )
        self._killpoint("after_prepared", parent_transaction_id)
        return self._record(transaction_id)

    def _prepare_new_transaction(
        self,
        normalized: tuple[MarkdownChange, ...],
        persisted_preconditions: Mapping[str, object],
        request_hash: str,
        *,
        operation_id: str,
        validators: Sequence[Validator],
        content_guard: object,
        project_reservation: ProjectCheckpointReservation | None,
        parent_transaction_id: str | None,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> TransactionRecord:
        transaction_id = uuid.uuid4().hex
        roots = self._create_artifact_roots(transaction_id, deadline, cancelled)
        timestamp = _now()
        existing = self._insert_preparing_row(
            transaction_id,
            operation_id,
            request_hash,
            persisted_preconditions,
            timestamp,
            parent_transaction_id,
            roots.artifact,
            deadline,
            cancelled,
        )
        if existing is not None:
            return existing
        self._killpoint("after_preparing", parent_transaction_id)
        try:
            return self._stage_and_commit(
                normalized,
                roots,
                persisted_preconditions,
                request_hash,
                transaction_id,
                timestamp,
                validators=validators,
                content_guard=content_guard,
                project_reservation=project_reservation,
                parent_transaction_id=parent_transaction_id,
                deadline=deadline,
                cancelled=cancelled,
            )
        except BaseException:
            self._discard_failed_preparation(
                transaction_id, roots.artifact, deadline, cancelled
            )
            raise

    def prepare(
        self,
        changes: Sequence[MarkdownChange],
        *,
        operation_id: str,
        preconditions: Mapping[str, object] | None = None,
        validators: Sequence[Validator] = (),
        content_guard: Literal["model_output"] | None = None,
        project_reservation: ProjectCheckpointReservation | None = None,
        _parent_transaction_id: str | None = None,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> TransactionRecord:
        self._require_operation_active(deadline, cancelled)
        _require_prepare_arguments(operation_id, changes, content_guard)
        normalized = self._normalized_changes(changes)
        persisted_preconditions = self._validate_preconditions(preconditions or {})
        _require_matching_reservation(
            project_reservation, operation_id, persisted_preconditions
        )
        request_hash = self._request_hash(normalized, persisted_preconditions)
        self.recover(deadline=deadline, cancelled=cancelled)
        self._require_operation_active(deadline, cancelled)
        existing = self._existing_bound_record(operation_id, request_hash)
        if existing is not None:
            return existing
        return self._prepare_new_transaction(
            normalized,
            persisted_preconditions,
            request_hash,
            operation_id=operation_id,
            validators=validators,
            content_guard=content_guard,
            project_reservation=project_reservation,
            parent_transaction_id=_parent_transaction_id,
            deadline=deadline,
            cancelled=cancelled,
        )

    def _bind_project_reservation(
        self,
        database: sqlite3.Connection,
        reservation: ProjectCheckpointReservation,
        transaction_id: str,
        preconditions: Mapping[str, object],
    ) -> None:
        lease = preconditions["project_lease"]
        assert isinstance(lease, Mapping)
        self._check_project_lease(database, lease)
        self._check_project_head(database, reservation.project, reservation.sequence)
        cursor = database.execute(
            "UPDATE project_checkpoints SET transaction_id = ?, state = 'prepared', "
            "lease_token = ?, fencing_epoch = ? "
            "WHERE project = ? AND sequence = ? AND operation_id = ? "
            "AND state IN ('reserved', 'prepared')",
            (
                transaction_id,
                lease["lease_token"],
                lease["fencing_epoch"],
                reservation.project,
                reservation.sequence,
                reservation.operation_id,
            ),
        )
        if cursor.rowcount != 1:
            raise TransactionFailure(
                "project checkpoint reservation is no longer available",
                "precondition_failed",
                "quarantined",
            )
        attempt = database.execute(
            "UPDATE project_checkpoint_attempts SET transaction_id = ?, "
            "state = 'prepared' WHERE operation_id = ? AND state = 'reserved'",
            (transaction_id, reservation.operation_id),
        )
        if attempt.rowcount != 1:
            raise TransactionFailure(
                "project checkpoint attempt is no longer available",
                "precondition_failed",
                "quarantined",
            )

    def refresh_project_lease_precondition(
        self,
        transaction_id: str,
        lease: Mapping[str, object],
    ) -> TransactionRecord:
        """Refresh a prepared transaction's lease fence without changing its plan."""
        refreshed = self._validate_preconditions({"project_lease": lease})[
            "project_lease"
        ]
        assert isinstance(refreshed, Mapping)
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            preconditions = self._prepared_preconditions(
                database, transaction_id, refreshed
            )
            self._check_project_lease(database, refreshed)
            preconditions["project_lease"] = dict(refreshed)
            self._store_refreshed_preconditions(
                database, transaction_id, preconditions
            )
        return self._record(transaction_id)

    def _prepared_preconditions(
        self,
        database: sqlite3.Connection,
        transaction_id: str,
        refreshed: Mapping[str, object],
    ) -> dict[str, object]:
        row = database.execute(
            'SELECT state, preconditions_json FROM "transaction" WHERE id = ?',
            (transaction_id,),
        ).fetchone()
        if row is None or row["state"] != "prepared":
            raise TransactionFailure(
                "project lease refresh requires a prepared transaction",
                "precondition_failed",
                "quarantined",
            )
        preconditions = json.loads(row["preconditions_json"])
        _require_same_lease_fence(preconditions.get("project_lease"), refreshed)
        return preconditions

    def _store_refreshed_preconditions(
        self,
        database: sqlite3.Connection,
        transaction_id: str,
        preconditions: Mapping[str, object],
    ) -> None:
        updated = database.execute(
            'UPDATE "transaction" SET preconditions_json = ?, updated_at = ? '
            "WHERE id = ? AND state = 'prepared'",
            (
                canonical_json_bytes(preconditions).decode("utf-8"),
                _now(),
                transaction_id,
            ),
        )
        if updated.rowcount != 1:
            raise TransactionFailure(
                "project transaction changed during lease refresh",
                "precondition_failed",
                "quarantined",
            )

    @staticmethod
    def _check_project_head(
        database: sqlite3.Connection, project: str, sequence: int
    ) -> None:
        prior = database.execute(
            "SELECT sequence FROM project_checkpoints WHERE project = ? "
            "AND sequence < ? AND state != 'committed' ORDER BY sequence LIMIT 1",
            (project, sequence),
        ).fetchone()
        if prior is not None:
            raise ProjectPendingPriorError(project, sequence, int(prior["sequence"]))

    def _check_project_transaction_head(
        self, database: sqlite3.Connection, transaction_id: str
    ) -> None:
        reservation = database.execute(
            "SELECT project, sequence FROM project_checkpoints WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if reservation is not None:
            self._check_project_head(
                database, str(reservation["project"]), int(reservation["sequence"])
            )

    def apply(
        self,
        transaction_id: str,
        *,
        writer_wait_seconds: float | None = None,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> TransactionRecord:
        self._require_operation_active(deadline, cancelled)
        with self.writer_gate(wait_seconds=writer_wait_seconds):
            self._require_operation_active(deadline, cancelled)
            previous = self._enter_recovery_scope(deadline, cancelled)
            try:
                return self._apply_locked(transaction_id)
            except (DLPContentBlocked, DLPPolicyError) as exc:
                self._quarantine_blocked_publication(transaction_id, exc)
            except TransactionFailure as exc:
                self._record_transaction_failure(transaction_id, exc)
                raise
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                self._translate_apply_error(transaction_id, exc)
            finally:
                self._leave_recovery_scope(previous)

    def _enter_recovery_scope(
        self, deadline: float, cancelled: Callable[[], bool] | None
    ) -> tuple[object, object]:
        """Install this call's deadline, returning the one it displaced."""
        previous = (
            getattr(self._local, "recovery_deadline", None),
            getattr(self._local, "recovery_cancelled", None),
        )
        self._local.recovery_deadline = deadline
        self._local.recovery_cancelled = cancelled
        return previous

    def _leave_recovery_scope(self, previous: tuple[object, object]) -> None:
        self._local.recovery_deadline, self._local.recovery_cancelled = previous

    def _quarantine_blocked_publication(
        self, transaction_id: str, exc: Exception
    ) -> None:
        code = _dlp_code(exc)
        self._rollback_for_quarantine(transaction_id, code)
        raise TransactionFailure(
            "model output publication was blocked", code, "quarantined"
        ) from exc

    def _record_transaction_failure(
        self, transaction_id: str, exc: TransactionFailure
    ) -> None:
        if exc.code == "precondition_failed":
            self._rollback_for_quarantine(transaction_id, exc.code)
            return
        self._set_transaction_state(transaction_id, exc.state, error_code=exc.code)

    def _translate_apply_error(self, transaction_id: str, exc: Exception) -> None:
        """Name what went wrong, or re-raise what this layer cannot name."""
        if _is_target_boundary_error(exc):
            self._set_transaction_state(
                transaction_id, "quarantined", error_code="parent_identity_changed"
            )
            raise TransactionFailure(
                "target parent identity changed",
                "parent_identity_changed",
                "quarantined",
            ) from exc
        message = str(exc)
        if "after-image is corrupt" in message or "plan hash mismatch" in message:
            recovered = self._recover_corrupt_after_image(transaction_id)
            raise TransactionFailure(
                message, "after_image_corrupt", recovered.state
            ) from exc
        failure = _conflicted_failure(message)
        if failure is None:
            raise
        self._set_transaction_state(
            transaction_id, failure.state, error_code=failure.code
        )
        raise failure from exc

    def _apply_locked(self, transaction_id: str) -> TransactionRecord:
        record = self._record(transaction_id)
        if record.state == "committed":
            return record
        if record.state not in {"prepared", "applying"}:
            raise RuntimeError(f"transaction cannot be applied from state {record.state}")
        plan = self._load_verified_plan(record)
        content_guard = self._content_guard(record, plan)
        rows = self._operation_rows(transaction_id)
        operation_states = _operation_states(rows)
        if "project_lease" in record.preconditions:
            return self._apply_project_locked(record, plan, rows, operation_states)
        self._check_preconditions(record.preconditions, operation_states)
        self._reconcile_operation_states(transaction_id, rows)
        self._mark_transaction_state(transaction_id, "applying")
        self._killpoint("after_applying", record.parent_transaction_id)
        self._mutate_all(record, plan, operation_states, content_guard)
        self._check_preconditions(record.preconditions, operation_states)
        self._require_all_after_state(transaction_id)
        self._killpoint("before_commit", record.parent_transaction_id)
        self._mark_transaction_state(transaction_id, "committed")
        result = self._record(transaction_id)
        self._killpoint("after_commit", record.parent_transaction_id)
        return result

    def _mutate_all(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        content_guard: object,
    ) -> None:
        rows = self._operation_rows(record.id)
        for row, operation_plan in zip(rows, plan["operations"], strict=True):
            self._require_current_operation_active()
            self._check_preconditions(record.preconditions, operation_states)
            self._mutate_one(record, row, operation_plan, content_guard)

    def _mutate_one(
        self,
        record: TransactionRecord,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        content_guard: object,
    ) -> None:
        if row["applied"]:
            self._require_operation_state(row, row["after_hash"], "after state")
            return
        self._mutate_and_mark(
            record.id, row, operation_plan, content_guard=content_guard
        )
        self._killpoint("after_each_target", record.parent_transaction_id)

    def _require_all_after_state(self, transaction_id: str) -> None:
        for row in self._operation_rows(transaction_id):
            self._require_operation_state(row, row["after_hash"], "after state")

    def _mark_transaction_state(self, transaction_id: str, state: str) -> None:
        self._require_current_operation_active()
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            database.execute(
                'UPDATE "transaction" SET state = ?, updated_at = ? WHERE id = ?',
                (state, _now(), transaction_id),
            )

    def _apply_project_locked(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> TransactionRecord:
        content_guard = self._content_guard(record, plan)
        self._check_preconditions(record.preconditions, operation_states)
        self._reconcile_operation_states(record.id, rows)
        rows = self._operation_rows(record.id)
        self._mark_project_applying(record)
        self._killpoint("after_applying", record.parent_transaction_id)
        self._commit_project_operations(
            record, plan, rows, operation_states, content_guard
        )
        result = self._record(record.id)
        self._killpoint("after_commit", record.parent_transaction_id)
        return result

    def _mark_project_applying(self, record: TransactionRecord) -> None:
        self._require_current_operation_active()
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            database.execute(
                'UPDATE "transaction" SET state = \'applying\', updated_at = ? '
                "WHERE id = ?",
                (_now(), record.id),
            )

    def _commit_project_operations(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
        content_guard: object,
    ) -> None:
        """Every project target moves inside the database transaction that
        commits its checkpoint, so a failure rewinds both together."""
        changed: list[sqlite3.Row] = []
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            self._check_preconditions(
                record.preconditions, operation_states, database=database
            )
            self._check_project_transaction_head(database, record.id)
            self._local.mutation_database = database
            try:
                self._mutate_project_operations(
                    database, record, plan, rows, operation_states,
                    content_guard, changed,
                )
                self._commit_project_checkpoint(
                    database, record, rows, operation_states
                )
            except BaseException:
                self._restore_inflight_operations(record.id, changed)
                raise
            finally:
                self._local.mutation_database = None

    def _mutate_project_operations(
        self,
        database: sqlite3.Connection,
        record: TransactionRecord,
        plan: Mapping[str, object],
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
        content_guard: object,
        changed: list[sqlite3.Row],
    ) -> None:
        operations = plan["operations"]
        assert isinstance(operations, list)
        for row, operation_plan in zip(rows, operations, strict=True):
            self._require_current_operation_active()
            self._check_preconditions(
                record.preconditions, operation_states, database=database
            )
            if not self._mutate_project_one(
                record, row, operation_plan, content_guard, changed
            ):
                continue
            self._check_preconditions(
                record.preconditions, operation_states, database=database
            )

    def _mutate_project_one(
        self,
        record: TransactionRecord,
        row: sqlite3.Row,
        operation_plan: object,
        content_guard: object,
        changed: list[sqlite3.Row],
    ) -> bool:
        """True when this call moved the target; False when it was already applied."""
        if row["applied"]:
            self._require_operation_state(row, row["after_hash"], "after state")
            return False
        assert isinstance(operation_plan, Mapping)
        self._mutate_and_mark(
            record.id, row, operation_plan, content_guard=content_guard
        )
        changed.append(row)
        self._killpoint("after_each_target", record.parent_transaction_id)
        return True

    def _commit_project_checkpoint(
        self,
        database: sqlite3.Connection,
        record: TransactionRecord,
        rows: Sequence[sqlite3.Row],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> None:
        self._check_preconditions(
            record.preconditions, operation_states, database=database
        )
        for row in rows:
            self._require_operation_state(row, row["after_hash"], "after state")
        self._killpoint("before_commit", record.parent_transaction_id)
        self._require_current_operation_active()
        self._check_preconditions(
            record.preconditions, operation_states, database=database
        )
        database.execute(
            'UPDATE "transaction" SET state = \'committed\', updated_at = ? '
            "WHERE id = ?",
            (_now(), record.id),
        )
        database.execute(
            "UPDATE project_checkpoints SET state = 'committed' "
            "WHERE transaction_id = ? AND state = 'prepared'",
            (record.id,),
        )
        database.execute(
            "UPDATE project_checkpoint_attempts SET state = 'committed' "
            "WHERE transaction_id = ? AND state = 'prepared'",
            (record.id,),
        )

    def _restore_inflight_operations(
        self, transaction_id: str, changed: Sequence[sqlite3.Row]
    ) -> None:
        for row in reversed(changed):
            if self._operation_hash(row) != row["after_hash"]:
                continue
            before_state = _before_state(self.transaction_root, transaction_id, row)
            inverse = dict(row)
            inverse.update(
                kind=_inverse_kind(row["before_hash"], row["after_hash"]),
                before_hash=row["after_hash"],
                after_hash=row["before_hash"],
            )
            self._apply_operation(inverse, {"after": before_state})
            self._require_operation_state(inverse, row["before_hash"], "restored state")

    def _abort_before_manifest(self, transaction_id: str) -> list[dict[str, object]]:
        manifest = [
            {
                "position": int(row["position"]),
                "kind": str(row["kind"]),
                "path": str(row["path"]),
                "before_hash": str(row["before_hash"]),
                "after_hash": str(row["after_hash"]),
            }
            for row in self._operation_rows(transaction_id)
        ]
        if not manifest:
            raise KeyError(transaction_id)
        return manifest

    def _require_live_abort_fence(
        self,
        database: sqlite3.Connection,
        intent_fence: IntentFence,
        active_link_digest: str,
        at: str,
        state: str,
        code: str,
        message: str,
    ) -> None:
        fence = database.execute(
            _ABORT_FENCE_QUERY,
            (
                intent_fence.intent_id,
                intent_fence.token,
                intent_fence.epoch,
                at,
                active_link_digest,
            ),
        ).fetchone()
        if fence is None:
            raise TransactionFailure(message, code, state)

    def _direct_abort(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        transaction_id: str,
        direction: _AbortDirection,
    ) -> _AbortDirection:
        """Move the transaction into aborting, or adopt the direction already there."""
        if row["state"] == "aborting":
            return _AbortDirection(
                str(row["abort_operation_id"]),
                str(row["abort_manifest_sha256"]),
                str(row["abort_chosen_at"]),
            )
        changed = database.execute(
            """UPDATE "transaction" SET state='aborting',error_code=NULL,
                   abort_operation_id=?,abort_manifest_sha256=?,
                   abort_chosen_at=?,updated_at=?
               WHERE id=? AND state IN ('preparing','prepared','applying')""",
            (
                direction.operation_id,
                direction.manifest_sha256,
                direction.chosen_at,
                direction.chosen_at,
                transaction_id,
            ),
        ).rowcount
        if changed != 1:
            raise TransactionFailure(
                "transaction cannot enter aborting",
                "abort_state_refused",
                str(row["state"]),
            )
        return direction

    def _enter_aborting(
        self,
        transaction_id: str,
        intent_fence: IntentFence,
        active_link_digest: str,
        direction: _AbortDirection,
    ) -> _AbortDirection:
        with self._connect() as database, begin_immediate(database):
            row = database.execute(
                'SELECT * FROM "transaction" WHERE id=?', (transaction_id,)
            ).fetchone()
            if row is None:
                raise KeyError(transaction_id)
            _require_abortable_state(row, transaction_id)
            self._require_live_abort_fence(
                database,
                intent_fence,
                active_link_digest,
                direction.chosen_at,
                str(row["state"]),
                "intent_fence_lost",
                "abort intent fence is stale",
            )
            return self._direct_abort(database, row, transaction_id, direction)

    def _restored_before_state(
        self, operation: sqlite3.Row, transaction_id: str
    ) -> object:
        if operation["before_hash"] == ABSENT:
            return ABSENT
        position = int(operation["position"])
        artifact = (
            self.transaction_root / transaction_id / "before" / f"{position:06d}.bin"
        )
        content = artifact.read_bytes()
        if sha256_bytes(content) != operation["before_hash"]:
            raise TransactionFailure(
                "abort before-image is corrupt",
                "abort_before_image_corrupt",
                "aborting",
            )
        return {
            "sha256": operation["before_hash"],
            "artifact": f"before/{position:06d}.bin",
        }

    def _restore_one_target(self, operation: sqlite3.Row, transaction_id: str) -> None:
        current = self._operation_hash(operation)
        if current == operation["before_hash"]:
            return
        if current != operation["after_hash"]:
            raise TransactionFailure(
                "abort target has third-party bytes",
                "abort_target_conflict",
                "aborting",
            )
        before_state = self._restored_before_state(operation, transaction_id)
        inverse = dict(operation)
        inverse.update(
            kind=_inverse_kind(operation["before_hash"], operation["after_hash"]),
            before_hash=operation["after_hash"],
            after_hash=operation["before_hash"],
        )
        self._apply_operation(inverse, {"after": before_state})
        self._require_operation_state(
            inverse, operation["before_hash"], "restored state"
        )
        self._killpoint("after_each_abort_target")

    def _restore_abort_targets(
        self,
        transaction_id: str,
        rows: list[sqlite3.Row],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        try:
            for operation in reversed(rows):
                self._require_operation_active(deadline, cancelled)
                self._restore_one_target(operation, transaction_id)
        except TransactionFailure as exc:
            self._set_transaction_state(
                transaction_id, "aborting", error_code=exc.code
            )
            raise

    def _abort_receipt_record(
        self,
        transaction_id: str,
        rows: list[sqlite3.Row],
        intent_fence: IntentFence,
        active_link_digest: str,
        actor_identity: str,
        direction: _AbortDirection,
    ) -> dict[str, object]:
        restored = [
            {"path": str(row["path"]), "sha256": str(row["before_hash"])}
            for row in rows
        ]
        return {
            "schema_version": "transaction-abort/v1",
            "transaction_id": transaction_id,
            "intent_id": intent_fence.intent_id,
            "active_link_digest": active_link_digest,
            "intent_fence_token_sha256": sha256_bytes(
                intent_fence.token.encode("utf-8")
            ),
            "intent_fence_epoch": intent_fence.epoch,
            "abort_operation_id": direction.operation_id,
            "before_manifest_sha256": direction.manifest_sha256,
            "restored_target_count": len(rows),
            "restored_tree_sha256": sha256_bytes(canonical_json_bytes(restored)),
            "actor_identity": actor_identity,
            "aborted_at": direction.chosen_at,
        }

    def _publish_abort_receipt(
        self, transaction_id: str, receipt_record: dict[str, object]
    ) -> tuple[str, str]:
        validate_schema(
            receipt_record,
            Path(__file__).with_name("schemas") / "transaction-abort-v1.json",
        )
        receipt_bytes = canonical_json_bytes(receipt_record)
        if len(receipt_bytes) > 64 * 1024:
            raise TransactionFailure(
                "abort receipt exceeds its bound",
                "abort_receipt_oversized",
                "aborting",
            )
        relative_path = f"run/transactions/{transaction_id}/abort-receipt.json"
        receipt_path = self.state_root / relative_path
        self._killpoint("before_abort_receipt")
        publish_runtime_file(
            receipt_path,
            receipt_bytes,
            state_root=self.state_root,
            create_only=True,
        )
        read_back = read_runtime_bytes(
            receipt_path, self.state_root, max_bytes=64 * 1024, owner_only=True
        )
        if read_back != receipt_bytes:
            raise TransactionFailure(
                "abort receipt read-back failed",
                "abort_receipt_conflict",
                "aborting",
            )
        self._killpoint("after_abort_receipt")
        return relative_path, sha256_bytes(read_back)

    def _finalize_aborted(
        self,
        transaction_id: str,
        rows: list[sqlite3.Row],
        intent_fence: IntentFence,
        active_link_digest: str,
        direction: _AbortDirection,
        receipt_sha256: str,
    ) -> None:
        with self._connect() as database, begin_immediate(database):
            self._require_live_abort_fence(
                database,
                intent_fence,
                active_link_digest,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "aborting",
                "abort_final_verification_failed",
                "abort final verification failed",
            )
            if any(self._operation_hash(row) != row["before_hash"] for row in rows):
                raise TransactionFailure(
                    "abort final verification failed",
                    "abort_final_verification_failed",
                    "aborting",
                )
            self._killpoint("before_aborted")
            changed = database.execute(
                """UPDATE "transaction" SET state='aborted',error_code=NULL,
                       abort_receipt_sha256=?,aborted_at=?,updated_at=?
                   WHERE id=? AND state='aborting' AND abort_operation_id=?
                     AND abort_manifest_sha256=?""",
                (
                    receipt_sha256,
                    direction.chosen_at,
                    direction.chosen_at,
                    transaction_id,
                    direction.operation_id,
                    direction.manifest_sha256,
                ),
            ).rowcount
            if changed != 1:
                raise TransactionFailure(
                    "abort fence was lost", "abort_fence_lost", "aborting"
                )

    def abort_for_discard(
        self,
        transaction_id: str,
        *,
        intent_fence: IntentFence,
        active_link_digest: str,
        actor_identity: str,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> TransactionAbortReceipt:
        self._require_operation_active(deadline, cancelled)
        _require_operator_fence(intent_fence, active_link_digest)
        _require_actor_identity(actor_identity)
        before_manifest = self._abort_before_manifest(transaction_id)
        manifest_sha256 = sha256_bytes(canonical_json_bytes(before_manifest))
        chosen_at = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        direction = _AbortDirection(
            _abort_identity(
                transaction_id, intent_fence, active_link_digest, manifest_sha256
            ),
            manifest_sha256,
            chosen_at,
        )
        with self.writer_gate():
            direction = self._enter_aborting(
                transaction_id, intent_fence, active_link_digest, direction
            )
            self._killpoint("after_aborting")
            rows = self._operation_rows(transaction_id)
            self._restore_abort_targets(transaction_id, rows, deadline, cancelled)
            relative_path, receipt_sha256 = self._publish_abort_receipt(
                transaction_id,
                self._abort_receipt_record(
                    transaction_id,
                    rows,
                    intent_fence,
                    active_link_digest,
                    actor_identity,
                    direction,
                ),
            )
            self._finalize_aborted(
                transaction_id,
                rows,
                intent_fence,
                active_link_digest,
                direction,
                receipt_sha256,
            )
        return TransactionAbortReceipt(
            transaction_id=transaction_id,
            intent_id=intent_fence.intent_id,
            abort_operation_id=direction.operation_id,
            receipt_path=relative_path,
            receipt_sha256=receipt_sha256,
            aborted_at=direction.chosen_at,
        )

    def _incomplete_transaction_rows(self, max_transactions: int | None) -> list[tuple]:
        query = (
            'SELECT id, state, owner_pid FROM "transaction" '
            "WHERE state IN ('aborting','aborted','preparing','prepared','applying') "
            "ORDER BY CASE state WHEN 'aborting' THEN 0 WHEN 'aborted' THEN 1 ELSE 2 END, "
            "created_at, id"
        )
        parameters: tuple[object, ...] = ()
        if max_transactions is not None:
            query += " LIMIT ?"
            parameters = (max_transactions,)
        with self._connect() as database:
            return [
                (row["id"], row["state"], row["owner_pid"])
                for row in database.execute(query, parameters)
            ]

    def _quarantine_dlp(self, transaction_id: str, exc: BaseException) -> None:
        code = "dlp_content_blocked"
        if isinstance(exc, DLPPolicyError):
            code = "dlp_policy_error"
        self._rollback_for_quarantine(transaction_id, code)

    def _settle_transaction_failure(
        self, transaction_id: str, exc: TransactionFailure
    ) -> None:
        if exc.code == "precondition_failed":
            self._rollback_for_quarantine(transaction_id, exc.code)
            return
        self._set_transaction_state(transaction_id, exc.state, error_code=exc.code)

    def _conflicted(self, transaction_id: str, code: str) -> TransactionRecord:
        self._set_transaction_state(transaction_id, "conflicted", error_code=code)
        return self._record(transaction_id)

    def _before_mismatch_code(self, transaction_id: str) -> str:
        create_conflict = any(
            row["kind"] == "create" and self._operation_hash(row) != row["before_hash"]
            for row in self._operation_rows(transaction_id)
        )
        if create_conflict:
            return "before_hash_mismatch"
        return "unknown_target_bytes"

    def _conflicted_from_message(
        self, transaction_id: str, exc: BaseException, message: str
    ) -> TransactionRecord:
        if "before state mismatch" in message:
            return self._conflicted(
                transaction_id, self._before_mismatch_code(transaction_id)
            )
        if "after state mismatch" in message:
            return self._conflicted(transaction_id, "unknown_target_bytes")
        raise exc

    def _recovered_from_message(
        self, transaction_id: str, exc: BaseException
    ) -> TransactionRecord:
        """Turn a known apply failure into the state it should settle in."""
        message = str(exc)
        if _is_target_boundary_error(exc):
            self._set_transaction_state(
                transaction_id, "quarantined", error_code="parent_identity_changed"
            )
            return self._record(transaction_id)
        if "after-image is corrupt" in message or "plan hash mismatch" in message:
            return self._recover_corrupt_after_image(transaction_id)
        return self._conflicted_from_message(transaction_id, exc, message)

    def _apply_recovered(
        self, transaction_id: str, recovered: list[TransactionRecord]
    ) -> None:
        try:
            recovered.append(self._apply_locked(transaction_id))
        except (DLPContentBlocked, DLPPolicyError) as exc:
            self._quarantine_dlp(transaction_id, exc)
            recovered.append(self._record(transaction_id))
        except TransactionFailure as exc:
            self._settle_transaction_failure(transaction_id, exc)
            recovered.append(self._record(transaction_id))
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            recovered.append(self._recovered_from_message(transaction_id, exc))

    def _promoted_for_recovery(
        self, transaction_id: str, recovered: list[TransactionRecord]
    ) -> bool:
        """False when the record settled here and needs no apply."""
        record = self._record(transaction_id)
        if record.state != "preparing":
            return True
        promotion = self._promote_preparing(record)
        if promotion == "invalid":
            self._set_transaction_state(transaction_id, "discarded")
            self._remove_artifacts(self.transaction_root / transaction_id)
            recovered.append(self._record(transaction_id))
            return False
        if promotion == "quarantined":
            recovered.append(self._record(transaction_id))
            return False
        return True

    def _recover_one(
        self,
        transaction_id: str,
        selected_state: str,
        owner_pid: object,
        recovered: list[TransactionRecord],
    ) -> None:
        if selected_state == "aborting":
            self._recover_aborting(transaction_id)
            recovered.append(self._record(transaction_id))
            return
        if selected_state == "aborted":
            self._validate_aborted(transaction_id)
            recovered.append(self._record(transaction_id))
            return
        if _preparing_owner_alive(selected_state, owner_pid):
            return
        if not self._promoted_for_recovery(transaction_id, recovered):
            return
        self._apply_recovered(transaction_id, recovered)

    def _recover_selected(
        self,
        max_transactions: int | None,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[TransactionRecord]:
        rows = self._incomplete_transaction_rows(max_transactions)
        self._local.recovery_deadline = deadline
        self._local.recovery_cancelled = cancelled
        recovered: list[TransactionRecord] = []
        try:
            for transaction_id, selected_state, owner_pid in rows:
                if self._recovery_stopped(deadline, cancelled):
                    break
                self._recover_one(
                    transaction_id, selected_state, owner_pid, recovered
                )
        finally:
            self._local.recovery_deadline = None
            self._local.recovery_cancelled = None
        return recovered

    def recover(
        self,
        *,
        owner: OwnerLease | None = None,
        writer_wait_seconds: float | None = None,
        max_transactions: int | None = None,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> list[TransactionRecord]:
        """Converge every incomplete transaction without overwriting unknown bytes."""
        _require_recovery_bound(max_transactions)
        if max_transactions == 0 or self._recovery_stopped(deadline, cancelled):
            return []
        with self.writer_gate(owner=owner, wait_seconds=writer_wait_seconds):
            if self._recovery_stopped(deadline, cancelled):
                return []
            return self._recover_selected(max_transactions, deadline, cancelled)

    def _recover_aborting(self, transaction_id: str) -> None:
        error_code = "abort_receipt_pending"
        for operation in reversed(self._operation_rows(transaction_id)):
            outcome = self._restore_aborted_operation(transaction_id, operation)
            if outcome is not None:
                error_code = outcome
                break
        self._set_transaction_state(transaction_id, "aborting", error_code=error_code)

    def _restore_aborted_operation(
        self, transaction_id: str, operation: sqlite3.Row
    ) -> str | None:
        """None means this operation is settled; a code means recovery stopped."""
        current = self._operation_hash(operation)
        if current == operation["before_hash"]:
            return None
        if current != operation["after_hash"]:
            return "abort_target_conflict"
        try:
            before_state = _before_state(
                self.transaction_root, transaction_id, operation
            )
        except (OSError, RuntimeError):
            return "abort_before_image_corrupt"
        inverse = dict(operation)
        inverse.update(
            kind=_inverse_kind(operation["before_hash"], operation["after_hash"]),
            before_hash=operation["after_hash"],
            after_hash=operation["before_hash"],
        )
        self._apply_operation(inverse, {"after": before_state})
        self._require_operation_state(
            inverse, operation["before_hash"], "restored state"
        )
        return None

    def _validate_aborted(self, transaction_id: str) -> None:
        with self._connect() as database:
            row = database.execute(
                'SELECT * FROM "transaction" WHERE id=?', (transaction_id,)
            ).fetchone()
        if row is None or not self._abort_receipt_valid(transaction_id, row):
            self._set_transaction_state(
                transaction_id, "aborted", error_code="abort_receipt_invalid"
            )

    def _abort_receipt_valid(self, transaction_id: str, row: sqlite3.Row) -> bool:
        """An unreadable, unparsable or unmatched receipt is simply not valid."""
        try:
            receipt_bytes = read_runtime_bytes(
                self.transaction_root / transaction_id / "abort-receipt.json",
                self.state_root,
                max_bytes=64 * 1024,
                owner_only=True,
            )
            receipt = json.loads(receipt_bytes)
            validate_schema(
                receipt,
                Path(__file__).with_name("schemas") / "transaction-abort-v1.json",
            )
        except (OSError, TypeError, ValueError):
            return False
        return _abort_receipt_matches(
            receipt, receipt_bytes, transaction_id, row
        ) and self._targets_hold_before_state(transaction_id)

    def _targets_hold_before_state(self, transaction_id: str) -> bool:
        return all(
            self._operation_hash(operation) == operation["before_hash"]
            for operation in self._operation_rows(transaction_id)
        )

    @staticmethod
    def _recovery_stopped(
        deadline: float, cancelled: Callable[[], bool] | None
    ) -> bool:
        return time.monotonic() >= deadline or bool(cancelled and cancelled())

    def _quarantine_parent_identity(self, record: TransactionRecord) -> str:
        self._set_transaction_state(
            record.id, "quarantined", error_code="parent_identity_changed"
        )
        return "quarantined"

    def _require_target_within_boundary(self, path: str) -> None:
        try:
            self._target(path)
        except (FileNotFoundError, ValueError) as exc:
            raise TargetBoundaryFailure from exc

    def _current_parent_identity(self, path: str) -> tuple[int, int]:
        try:
            return self._parent_identity(self._target(path).parent)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise TargetBoundaryFailure from exc

    def _validated_plan_document(
        self, record: TransactionRecord, artifact_root: Path
    ) -> tuple[dict | None, bytes]:
        plan_bytes = (artifact_root / "plan.json").read_bytes()
        plan = json.loads(plan_bytes)
        validate_schema(plan, _SCHEMA)
        if plan_bytes != canonical_json_bytes(plan):
            return None, plan_bytes
        if plan["transaction_id"] != record.id:
            return None, plan_bytes
        return plan, plan_bytes

    def _validated_manifest_document(
        self, record: TransactionRecord, artifact_root: Path, plan_bytes: bytes
    ) -> dict | None:
        manifest_bytes = (artifact_root / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest_bytes != canonical_json_bytes(manifest):
            return None
        if set(manifest) != _RECOVERY_MANIFEST_FIELDS:
            return None
        if not _manifest_identity_matches(manifest, record.id, plan_bytes):
            return None
        return manifest

    def _manifest_matches_request(
        self, record: TransactionRecord, manifest: Mapping[str, object]
    ) -> bool:
        with self._connect() as database:
            row = database.execute(
                'SELECT request_hash FROM "transaction" WHERE id = ?',
                (record.id,),
            ).fetchone()
        if row is None:
            return False
        return manifest["request_hash"] == row["request_hash"]

    def _built_operation(
        self,
        record: TransactionRecord,
        position: int,
        operation: object,
        persisted: object,
        seen_paths: set[str],
    ) -> _BuiltOperation | None:
        encoded_parent_identity = _validated_persisted_operation(
            persisted, position, operation
        )
        if encoded_parent_identity is None:
            return None
        path = str(operation["path"])
        self._require_target_within_boundary(path)
        normalized = unicodedata.normalize("NFC", path).casefold()
        if normalized in seen_paths:
            return None
        seen_paths.add(normalized)
        before_hash = self._state_description_hash(operation["before"])
        after_hash = self._state_description_hash(operation["after"])
        kind = operation["kind"]
        if not _operation_hashes_consistent(persisted, kind, before_hash, after_hash):
            return None
        current_parent = self._current_parent_identity(path)
        parent_identity = (persisted["parent_device"], persisted["parent_inode"])
        return _BuiltOperation(
            (
                record.id,
                position,
                kind,
                path,
                before_hash,
                after_hash,
                *encoded_parent_identity,
                0,
            ),
            {"kind": kind, "path": path, "content_hash": after_hash},
            current_parent != parent_identity,
        )

    def _request_hash_matches(
        self,
        record: TransactionRecord,
        request_changes: list[dict[str, object]],
        manifest: Mapping[str, object],
    ) -> bool:
        request = {
            "changes": request_changes,
            "preconditions": dict(record.preconditions),
        }
        return sha256_bytes(canonical_json_bytes(request)) == manifest["request_hash"]

    def _built_operations(
        self,
        record: TransactionRecord,
        plan: Mapping[str, object],
        manifest: Mapping[str, object],
    ) -> _PromotionPlan | None:
        plan_operations = plan["operations"]
        manifest_operations = manifest["operations"]
        if not _comparable_operation_lists(plan_operations, manifest_operations):
            return None
        built = self._all_built_operations(record, plan_operations, manifest_operations)
        if built is None:
            return None
        if not self._request_hash_matches(
            record, [item.change for item in built], manifest
        ):
            return None
        return _promotion_plan(manifest, built)

    def _all_built_operations(
        self,
        record: TransactionRecord,
        plan_operations: list,
        manifest_operations: list,
    ) -> list[_BuiltOperation] | None:
        seen_paths: set[str] = set()
        built: list[_BuiltOperation] = []
        for position, (operation, persisted) in enumerate(
            zip(plan_operations, manifest_operations, strict=True)
        ):
            item = self._built_operation(
                record, position, operation, persisted, seen_paths
            )
            if item is None:
                return None
            built.append(item)
        return built

    def _validated_promotion_plan(
        self, record: TransactionRecord
    ) -> _PromotionPlan | None:
        """Everything the artifacts must agree on before a transaction promotes."""
        artifact_root = self.transaction_root / record.id
        plan, plan_bytes = self._validated_plan_document(record, artifact_root)
        if plan is None:
            return None
        self._verify_plan_artifacts(plan, artifact_root)
        manifest = self._validated_manifest_document(record, artifact_root, plan_bytes)
        if manifest is None:
            return None
        if not self._manifest_matches_request(record, manifest):
            return None
        return self._built_operations(record, plan, manifest)

    def _promotion_plan_or_verdict(self, record: TransactionRecord):
        try:
            plan = self._validated_promotion_plan(record)
        except TargetBoundaryFailure:
            return self._quarantine_parent_identity(record)
        except (AssertionError, KeyError, OSError, TypeError, ValueError):
            return "invalid"
        if plan is None:
            return "invalid"
        return plan

    def _bind_existing_reservation(
        self, database: sqlite3.Connection, record: TransactionRecord
    ) -> None:
        reservation_row = database.execute(
            "SELECT * FROM project_checkpoints WHERE operation_id = ?",
            (record.operation_id,),
        ).fetchone()
        if reservation_row is None:
            return
        self._bind_project_reservation(
            database,
            self._project_reservation(reservation_row),
            record.id,
            record.preconditions,
        )

    def _quarantine_failed_promotion(
        self, record: TransactionRecord, exc: TransactionFailure
    ) -> str:
        self._set_transaction_state(record.id, "quarantined", error_code=exc.code)
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            database.execute(
                "UPDATE project_checkpoints SET state = 'quarantined' "
                "WHERE operation_id = ?",
                (record.operation_id,),
            )
            database.execute(
                "UPDATE project_checkpoint_attempts SET state = 'quarantined' "
                "WHERE operation_id = ?",
                (record.operation_id,),
            )
        return "quarantined"

    def _commit_promotion(
        self, record: TransactionRecord, promotion: _PromotionPlan
    ) -> str | None:
        """The verdict when the promotion could not commit, otherwise None."""
        try:
            with self._connect() as database, begin_immediate(
                database, before_commit=self._require_current_operation_active
            ):
                self._bind_existing_reservation(database, record)
                cursor = database.execute(
                    'UPDATE "transaction" SET state = \'prepared\', plan_hash = ?, '
                    "updated_at = ?, owner_pid = NULL WHERE id = ? AND state = 'preparing'",
                    (promotion.manifest["plan_hash"], _now(), record.id),
                )
                if cursor.rowcount != 1:
                    return "invalid"
                database.executemany(
                    'INSERT INTO "operation" '
                    "(transaction_id, position, kind, path, before_hash, after_hash, "
                    "parent_device, parent_inode, applied) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    promotion.operations,
                )
        except ProjectPendingPriorError:
            return "invalid"
        except TransactionFailure as exc:
            return self._quarantine_failed_promotion(record, exc)
        return None

    def _promote_preparing(self, record: TransactionRecord) -> str:
        promotion = self._promotion_plan_or_verdict(record)
        if isinstance(promotion, str):
            return promotion
        verdict = self._commit_promotion(record, promotion)
        if verdict is not None:
            return verdict
        if promotion.parent_mismatch:
            return self._quarantine_parent_identity(record)
        return "promoted"

    def _state_description_hash(self, state: object) -> str:
        if state == ABSENT:
            return ABSENT
        if not isinstance(state, dict) or set(state) != {"sha256", "artifact"}:
            raise ValueError("invalid transaction state description")
        value = state["sha256"]
        if not isinstance(value, str):
            raise ValueError("invalid transaction state hash")
        return value

    def _rollback_for_quarantine(self, transaction_id: str, error_code: str) -> None:
        try:
            for row in self._operation_rows(transaction_id):
                if row["applied"]:
                    self._rollback_one_operation(transaction_id, row)
        except _TargetBoundaryChanged:
            self._set_transaction_state(
                transaction_id, "quarantined", error_code="parent_identity_changed"
            )
            return
        self._set_transaction_state(
            transaction_id, "quarantined", error_code=error_code
        )

    def _rollback_one_operation(self, transaction_id: str, row: sqlite3.Row) -> None:
        """An operation that cannot be rolled back is left as it stands."""
        current = self._boundary_checked_hash(row)
        if current != row["after_hash"]:
            return
        before_state = _optional_before_state(
            self.transaction_root, transaction_id, row
        )
        if before_state is _UNAVAILABLE:
            return
        if not self._try_apply_inverse(transaction_id, row, before_state):
            return
        self._mark_operation_unapplied(transaction_id, row["position"])

    def _boundary_checked_hash(self, row: sqlite3.Row) -> str:
        try:
            return self._operation_hash(row)
        except (OSError, RuntimeError, ValueError) as exc:
            if not _is_target_boundary_error(exc):
                raise
            raise _TargetBoundaryChanged from exc

    def _try_apply_inverse(
        self, transaction_id: str, row: sqlite3.Row, before_state: object
    ) -> bool:
        """False means this one could not be undone; the parent changing raises."""
        try:
            self._apply_inverse_under_fence(
                _inverse_operation(transaction_id, row), before_state
            )
        except (OSError, RuntimeError, ValueError) as exc:
            if _is_target_boundary_error(exc):
                raise _TargetBoundaryChanged from exc
            return False
        return True

    def _mark_operation_unapplied(self, transaction_id: str, position: int) -> None:
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            database.execute(
                'UPDATE "operation" SET applied = 0 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )

    def _apply_inverse_under_fence(
        self, inverse: Mapping[str, object], before_state: object
    ) -> None:
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            self._apply_operation(inverse, {"after": before_state})

    def _recover_corrupt_after_image(self, transaction_id: str) -> TransactionRecord:
        """Undo what can be explained; anything else leaves the transaction dirty."""
        rows = self._operation_rows(transaction_id)
        try:
            before_states, current_hashes, ambiguous = self._undo_images(
                transaction_id, rows
            )
            ambiguous = self._undo_applied_operations(
                transaction_id, rows, before_states, current_hashes, ambiguous
            )
        except _TargetBoundaryChanged:
            self._set_transaction_state(
                transaction_id, "quarantined", error_code="parent_identity_changed"
            )
            return self._record(transaction_id)
        self._set_transaction_state(
            transaction_id,
            "quarantined" if ambiguous else "discarded",
            error_code="after_image_corrupt",
        )
        return self._record(transaction_id)

    def _undo_images(
        self, transaction_id: str, rows: Sequence[sqlite3.Row]
    ) -> tuple[dict[int, object], dict[int, str], bool]:
        before_states: dict[int, object] = {}
        current_hashes: dict[int, str] = {}
        ambiguous = False
        for row in rows:
            current = self._boundary_checked_hash(row)
            current_hashes[row["position"]] = current
            if not self._collect_undo_image(
                transaction_id, row, current, before_states
            ):
                ambiguous = True
        return before_states, current_hashes, ambiguous

    def _collect_undo_image(
        self,
        transaction_id: str,
        row: sqlite3.Row,
        current: str,
        before_states: dict[int, object],
    ) -> bool:
        """False marks an operation whose current state cannot be explained."""
        if current not in {row["before_hash"], row["after_hash"]}:
            return False
        if not row["applied"]:
            return current != row["after_hash"]
        if current == row["before_hash"]:
            return True
        state = _optional_before_state(self.transaction_root, transaction_id, row)
        if state is _UNAVAILABLE:
            return False
        before_states[row["position"]] = state
        return True

    def _undo_applied_operations(
        self,
        transaction_id: str,
        rows: Sequence[sqlite3.Row],
        before_states: dict[int, object],
        current_hashes: dict[int, str],
        ambiguous: bool,
    ) -> bool:
        for row in rows:
            if not _undoable(row, before_states, current_hashes):
                continue
            if not self._try_apply_inverse(
                transaction_id, row, before_states[row["position"]]
            ):
                ambiguous = True
        return ambiguous

    def undo(
        self,
        transaction_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> TransactionRecord:
        self._require_operation_active(deadline, cancelled)
        with self.writer_gate():
            self._require_operation_active(deadline, cancelled)
            return self._prepare_undo(
                transaction_id, deadline=deadline, cancelled=cancelled
            )

    def _prepare_undo(
        self,
        transaction_id: str,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> TransactionRecord:
        self._require_undoable(transaction_id)
        rows = self._operation_rows(transaction_id)
        self._require_targets_unchanged(rows, deadline, cancelled)
        changes, preconditions = self._undo_changes(
            transaction_id, rows, deadline, cancelled
        )
        return self.prepare(
            changes,
            operation_id=f"undo:{transaction_id}:{uuid.uuid4().hex}",
            preconditions=preconditions,
            _parent_transaction_id=transaction_id,
            deadline=deadline,
            cancelled=cancelled,
        )

    def prune(
        self,
        *,
        retention_days: int = 30,
        now: datetime | None = None,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> int:
        self._require_operation_active(deadline, cancelled)
        cutoff = _prune_cutoff(retention_days, now)
        pruned = 0
        with self.writer_gate():
            for row in self._prunable_rows():
                self._require_operation_active(deadline, cancelled)
                if _parse_timestamp(row["updated_at"]) < cutoff:
                    pruned += self._prune_one(row, deadline, cancelled)
        return pruned

    def _prunable_rows(self) -> list[sqlite3.Row]:
        with self._connect() as database:
            return list(
                database.execute(
                    'SELECT id, updated_at FROM "transaction" '
                    "WHERE state IN ('committed', 'discarded') "
                    "AND artifacts_pruned_at IS NULL"
                )
            )

    def _prune_one(
        self,
        row: sqlite3.Row,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> int:
        """The images are staged aside first, so a failure can put them back."""
        artifact_root = self.transaction_root / row["id"]
        if not artifact_root.exists():
            return 0
        staged_root = self.transaction_root / (
            f".{row['id']}.pruning-{uuid.uuid4().hex}"
        )
        artifact_root.replace(staged_root)
        try:
            self._require_operation_active(deadline, cancelled)
            self._mark_artifacts_pruned(row["id"], deadline, cancelled)
        except BaseException:
            staged_root.replace(artifact_root)
            raise
        self._remove_artifacts(staged_root)
        return 1

    def _mark_artifacts_pruned(
        self,
        transaction_id: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        with self._connect() as database, begin_immediate(
            database,
            before_commit=lambda: self._require_operation_active(deadline, cancelled),
        ):
            database.execute(
                'UPDATE "transaction" SET artifacts_pruned_at = ? WHERE id = ?',
                (_now(), transaction_id),
            )

    def deletion_blockers(self) -> list[dict[str, str]]:
        now = datetime.now(timezone.utc)
        with self._connect() as database:
            rows = list(
                database.execute(
                    'SELECT id, state, error_code, updated_at, artifacts_pruned_at '
                    'FROM "transaction" ORDER BY created_at, id'
                )
            )
        coded = ((row, _deletion_blocker_code(row, now)) for row in rows)
        return [
            {"transaction_id": row["id"], "state": row["state"], "code": code}
            for row, code in coded
            if code is not None
        ]

    def _set_transaction_state(
        self, transaction_id: str, state: str, *, error_code: str | None = None
    ) -> None:
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            database.execute(
                'UPDATE "transaction" SET state = ?, error_code = ?, updated_at = ? '
                "WHERE id = ?",
                (state, error_code, _now(), transaction_id),
            )

    def _reentrant_writer_gate(self, depth: int) -> Iterator[OwnerLease]:
        self._local.gate_depth = depth + 1
        try:
            yield getattr(self._local, "gate_owner", None)
        finally:
            self._local.gate_depth -= 1

    def _install_legacy_owner(
        self, database: sqlite3.Connection, owner_token: str
    ) -> int:
        fence = database.execute(
            "SELECT last_epoch FROM writer_fences WHERE gate_name = 'global'"
        ).fetchone()
        fencing_epoch = 1 if fence is None else fence["last_epoch"] + 1
        database.execute(
            "INSERT INTO writer_fences (gate_name, last_epoch) VALUES ('global', ?) "
            "ON CONFLICT(gate_name) DO UPDATE SET last_epoch = excluded.last_epoch",
            (fencing_epoch,),
        )
        heartbeat = _now()
        expires = _future_timestamp(_WRITER_LEASE_SECONDS)
        database.execute("DELETE FROM writer_owners WHERE gate_name = 'global'")
        database.execute(
            "INSERT INTO writer_owners "
            "(gate_name, owner_token, process_id, thread_id, acquired_at, "
            "heartbeat_at, expires_at, fencing_epoch) "
            "VALUES ('global', ?, ?, ?, ?, ?, ?, ?)",
            (
                owner_token,
                os.getpid(),
                threading.get_ident(),
                heartbeat,
                heartbeat,
                expires,
                fencing_epoch,
            ),
        )
        return fencing_epoch

    def _try_claim_legacy_gate(
        self, owner_token: str, deadline: float
    ) -> int | None:
        """The fencing epoch once claimed, or None while another owner holds it."""
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
        with self._connect(busy_ms=remaining_ms) as database, begin_immediate(database):
            row = database.execute(
                "SELECT * FROM writer_owners WHERE gate_name = 'global'"
            ).fetchone()
            if row is not None and not self._writer_owner_reclaimable(row):
                return None
            return self._install_legacy_owner(database, owner_token)

    def _acquire_legacy_gate(self, owner_token: str, deadline: float) -> int:
        acquisition_attempt = 0
        while True:
            try:
                fencing_epoch = self._try_claim_legacy_gate(owner_token, deadline)
            except (OSError, sqlite3.Error) as exc:
                acquisition_attempt = _retry_gate_or_fail(
                    exc, acquisition_attempt, deadline
                )
                continue
            if fencing_epoch is not None:
                return fencing_epoch
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for the global Markdown writer gate"
                )
            time.sleep(0.01)

    def _stop_legacy_gate(
        self,
        owner_token: str,
        fencing_epoch: int,
        heartbeat_stop: threading.Event,
        heartbeat_thread: threading.Thread,
        heartbeat_lost: threading.Event,
    ) -> None:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=_WRITER_HEARTBEAT_SECONDS * 2)
        try:
            self._release_writer_gate(
                owner_token, fencing_epoch, heartbeat_lost.is_set()
            )
        finally:
            self._local.gate_depth = 0
            self._local.gate_token = None
            self._local.gate_fence = None

    def _legacy_writer_gate(
        self, wait_seconds: float | None
    ) -> Iterator[OwnerLease]:
        _require_gate_wait(wait_seconds)
        owner_token = uuid.uuid4().hex
        deadline = time.monotonic() + _gate_wait_seconds(wait_seconds)
        fencing_epoch = self._acquire_legacy_gate(owner_token, deadline)
        self._local.gate_depth = 1
        self._local.gate_token = owner_token
        self._local.gate_fence = fencing_epoch
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_writer_gate,
            args=(owner_token, fencing_epoch, heartbeat_stop, heartbeat_lost),
            name="markdown-writer-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            yield None
        finally:
            self._stop_legacy_gate(
                owner_token,
                fencing_epoch,
                heartbeat_stop,
                heartbeat_thread,
                heartbeat_lost,
            )

    @contextlib.contextmanager
    def writer_gate(
        self,
        *,
        owner: OwnerLease | None = None,
        wait_seconds: float | None = None,
    ) -> Iterator[OwnerLease]:
        if owner is not None:
            yield from self._nested_writer_gate(owner)
            return
        depth = getattr(self._local, "gate_depth", 0)
        if depth:
            yield from self._reentrant_writer_gate(depth)
            return
        if getattr(self, "_database_contract", None) == _COORDINATOR_V3_CONTRACT:
            yield from self._canonical_writer_gate(wait_seconds)
            return
        yield from self._legacy_writer_gate(wait_seconds)

    def _canonical_writer_gate(
        self, wait_seconds: float | None
    ) -> Iterator[OwnerLease]:
        _require_wait_seconds(wait_seconds)
        registry = self._ownership_registry()
        lease = self._acquire_canonical_lease(registry, wait_seconds)
        self._enter_gate(lease)
        stop = threading.Event()
        lost = threading.Event()
        heartbeat = self._start_canonical_heartbeat(registry, lease, stop, lost)
        try:
            yield lease
        finally:
            self._leave_canonical_gate(registry, lease, stop, heartbeat)

    def _acquire_canonical_lease(
        self, registry: object, wait_seconds: float | None
    ) -> OwnerLease:
        deadline = time.monotonic() + _writer_wait_window(wait_seconds)
        attempt = 0
        while True:
            try:
                return self._claim_canonical_lease(registry)
            except Exception as exc:
                if getattr(exc, "code", None) != "owner_busy":
                    raise
                delay = _writer_retry_delay(attempt, deadline)
                if delay <= 0:
                    raise TimeoutError(
                        "timed out waiting for the global Markdown writer gate"
                    ) from exc
                time.sleep(delay)
                attempt += 1

    def _claim_canonical_lease(self, registry: object) -> OwnerLease:
        with self._connect() as database, begin_immediate(database):
            lease = registry._acquire_in_transaction(
                database, "markdown-writer", scope="global"
            )
            self._insert_writer_projection(database, lease)
        return lease

    def _enter_gate(self, lease: OwnerLease) -> None:
        self._local.gate_depth = 1
        self._local.gate_token = lease.token
        self._local.gate_fence = lease.epoch
        self._local.gate_owner = lease

    def _clear_gate(self) -> None:
        self._local.gate_depth = 0
        self._local.gate_token = None
        self._local.gate_fence = None
        self._local.gate_owner = None

    def _start_canonical_heartbeat(
        self,
        registry: object,
        lease: OwnerLease,
        stop: threading.Event,
        lost: threading.Event,
    ) -> threading.Thread:
        heartbeat = threading.Thread(
            target=self._heartbeat_canonical_writer_gate,
            args=(registry, lease, stop, lost),
            name="markdown-writer-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        return heartbeat

    def _leave_canonical_gate(
        self,
        registry: object,
        lease: OwnerLease,
        stop: threading.Event,
        heartbeat: threading.Thread,
    ) -> None:
        stop.set()
        heartbeat.join(timeout=lease.heartbeat_seconds * 2)
        try:
            # A heartbeat that failed transiently is not a lost gate. What
            # proves the loss is the projection row: reclaiming it deletes
            # this owner's row and bumps the fence, so a delete that removes
            # our row means nobody else ever took the gate.
            with self._connect() as database, begin_immediate(database):
                self._delete_writer_projection(database, lease)
                registry._release_in_transaction(database, lease)
        finally:
            self._clear_gate()

    @staticmethod
    def _insert_writer_projection(database: sqlite3.Connection, owner: OwnerLease) -> None:
        now = _timestamp(owner.heartbeat_at)
        database.execute(
            """INSERT INTO writer_owners(
                   gate_name,owner_token,process_id,thread_id,acquired_at,
                   heartbeat_at,expires_at,fencing_epoch,canonical_role,
                   canonical_scope,actor_id,process_start_identity
               ) VALUES ('global',?,?,?,?,?,?,?,?,?,?,?)""",
            (
                owner.token,
                owner.process.pid,
                threading.get_ident(),
                _timestamp(owner.acquired_at),
                now,
                _timestamp(owner.expires_at),
                owner.epoch,
                owner.role,
                owner.scope,
                owner.actor_id,
                owner.process.start_identity,
            ),
        )

    @staticmethod
    def _delete_writer_projection(
        database: sqlite3.Connection, owner: OwnerLease
    ) -> None:
        deleted = database.execute(
            """DELETE FROM writer_owners
               WHERE gate_name='global' AND owner_token=? AND fencing_epoch=?
                 AND canonical_role=? AND canonical_scope=? AND actor_id=?
                 AND process_id=? AND process_start_identity=?""",
            (
                owner.token,
                owner.epoch,
                owner.role,
                owner.scope,
                owner.actor_id,
                owner.process.pid,
                owner.process.start_identity,
            ),
        ).rowcount
        if deleted != 1:
            raise RuntimeError(_writer_gate_loss_message(heartbeat_lost=False))

    def _heartbeat_canonical_writer_gate(
        self,
        registry: object,
        owner: OwnerLease,
        stop: threading.Event,
        lost: threading.Event,
    ) -> None:
        while not stop.wait(owner.heartbeat_seconds):
            try:
                with self._connect() as database, begin_immediate(database):
                    renewed = registry._heartbeat_in_transaction(database, owner)
                    updated = database.execute(
                        """UPDATE writer_owners SET heartbeat_at=?,expires_at=?
                           WHERE gate_name='global' AND owner_token=?
                             AND fencing_epoch=?""",
                        (
                            _timestamp(renewed.heartbeat_at),
                            _timestamp(renewed.expires_at),
                            owner.token,
                            owner.epoch,
                        ),
                    ).rowcount
                    if updated != 1:
                        raise RuntimeError("writer projection was lost")
                owner = renewed
            except BaseException:
                lost.set()
                return

    def _nested_writer_gate(self, owner: OwnerLease) -> Iterator[OwnerLease]:
        from operational_ownership import OwnerLease

        if not isinstance(owner, OwnerLease):
            raise TypeError("owner must be an OwnerLease")
        if getattr(self, "_database_contract", None) != _COORDINATOR_V3_CONTRACT:
            raise RuntimeError("canonical writer projection requires a v3 coordinator")
        if getattr(self._local, "gate_depth", 0):
            yield from self._reentered_writer_gate(owner)
            return

        registry = self._ownership_registry()
        with self._connect() as database, begin_immediate(database):
            registry.require(database, owner)
            self._insert_writer_projection(database, owner)
        self._enter_gate(owner)
        try:
            yield owner
        finally:
            with self._connect() as database, begin_immediate(database):
                self._delete_writer_projection(database, owner)
            self._clear_gate()

    def _reentered_writer_gate(self, owner: OwnerLease) -> Iterator[OwnerLease]:
        """A nested gate belongs to the same owner or it is not the same gate."""
        if getattr(self._local, "gate_owner", None) != owner:
            raise RuntimeError("nested writer owner changed")
        self._local.gate_depth = getattr(self._local, "gate_depth", 0) + 1
        try:
            yield owner
        finally:
            self._local.gate_depth -= 1

    def _release_writer_gate(
        self, owner_token: str, fencing_epoch: int, heartbeat_lost: bool
    ) -> None:
        deadline = time.monotonic() + _WRITER_WAIT_SECONDS
        attempt = 0
        while True:
            try:
                if not self._delete_writer_owner(owner_token, fencing_epoch, deadline):
                    raise RuntimeError(_writer_gate_loss_message(heartbeat_lost))
                return
            except (OSError, sqlite3.Error) as exc:
                attempt = _retry_or_raise(exc, attempt, deadline)

    def _delete_writer_owner(
        self, owner_token: str, fencing_epoch: int, deadline: float
    ) -> bool:
        """False means somebody else holds the gate, so nothing was deleted."""
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
        with self._connect(busy_ms=remaining_ms) as database, begin_immediate(
            database
        ):
            row = database.execute(
                "SELECT owner_token, fencing_epoch FROM writer_owners "
                "WHERE gate_name = 'global'"
            ).fetchone()
            if not _owns_writer_gate(row, owner_token, fencing_epoch):
                return False
            database.execute(
                "DELETE FROM writer_owners WHERE gate_name = 'global' "
                "AND owner_token = ? AND fencing_epoch = ?",
                (owner_token, fencing_epoch),
            )
        return True

    def _writer_owner_reclaimable(self, row: sqlite3.Row) -> bool:
        expires_at = row["expires_at"]
        expired = not expires_at or _parse_timestamp(expires_at) <= datetime.now(timezone.utc)
        return expired or not _pid_alive(row["process_id"])

    def _heartbeat_writer_gate(
        self,
        owner_token: str,
        fencing_epoch: int,
        stop: threading.Event,
        lost: threading.Event,
    ) -> None:
        lease_deadline = time.monotonic() + _WRITER_LEASE_SECONDS
        attempt = 0
        while not stop.wait(_WRITER_HEARTBEAT_SECONDS):
            try:
                if not self._refresh_writer_owner(
                    owner_token, fencing_epoch, lease_deadline
                ):
                    lost.set()
                    return
            except (OSError, sqlite3.Error) as exc:
                retried = _heartbeat_retry(exc, attempt, lease_deadline, stop)
                if retried is None:
                    lost.set()
                    return
                attempt = retried
                continue
            lease_deadline = time.monotonic() + _WRITER_LEASE_SECONDS
            attempt = 0

    def _refresh_writer_owner(
        self, owner_token: str, fencing_epoch: int, lease_deadline: float
    ) -> bool:
        """False means the row is gone: this owner no longer holds the gate."""
        remaining = min(
            _WRITER_HEARTBEAT_SECONDS, lease_deadline - time.monotonic()
        )
        remaining_ms = max(1, int(remaining * 1_000))
        with self._connect(busy_ms=remaining_ms) as database, begin_immediate(
            database
        ):
            cursor = database.execute(
                "UPDATE writer_owners SET heartbeat_at = ?, expires_at = ? "
                "WHERE gate_name = 'global' AND owner_token = ? AND fencing_epoch = ?",
                (
                    _now(),
                    _future_timestamp(_WRITER_LEASE_SECONDS),
                    owner_token,
                    fencing_epoch,
                ),
            )
            return cursor.rowcount == 1

    def writer_gate_held(self) -> bool:
        return bool(getattr(self._local, "gate_depth", 0))

    def assert_external_work_allowed(self) -> None:
        if self.writer_gate_held():
            raise RuntimeError("external LLM or Git work is forbidden under the writer gate")

    def coherent_read(self, paths: Sequence[Path]) -> dict[Path, bytes | None]:
        with self.writer_gate():
            return {Path(path): self._read_target(self._target(Path(path).as_posix())) for path in paths}

    def ensure_target_parent(self, value: str) -> Path:
        """Create a covered target's parent without traversing unsafe directories."""
        relative = restricted_relative_path(
            value, (*_ALLOWED_DIRECTORIES, *_ALLOWED_FILES)
        )
        if len(relative.parts) > MAX_KNOWLEDGE_DEPTH:
            raise ValueError("target path depth exceeds limit")
        current = self.vault
        for part in relative.parts[:-1]:
            current = current / part
            _ensure_safe_directory(current, value)
        return self._target(value)

    def _validate_change(self, change: MarkdownChange) -> MarkdownChange:
        if not isinstance(change, MarkdownChange):
            raise TypeError("changes must contain MarkdownChange values")
        if change.kind not in {"create", "replace", "delete"}:
            raise ValueError(f"unsupported change kind: {change.kind}")
        _require_change_content(change)
        _require_before_bound(change.max_before_bytes)
        self._target(change.path)
        if change.max_before_bytes is None:
            return MarkdownChange(
                change.kind,
                change.path,
                change.content,
                MAX_KNOWLEDGE_TARGET_BYTES,
            )
        return change

    def _require_no_reparse_traversal(self, relative: Path, value: str) -> None:
        current = self.vault
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"target traverses a symlink: {value}")
            if _is_reparse_point(current):
                raise ValueError(f"target traverses a Windows reparse point: {value}")
            if not current.exists():
                return

    def _require_canonical_parent(self, target: Path, value: str) -> None:
        if target.parent.resolve(strict=False) != target.parent:
            raise ValueError(f"target has a non-canonical parent: {value}")
        try:
            target.relative_to(self.vault)
        except ValueError as exc:
            raise ValueError("target escapes the vault") from exc
        if not target.parent.is_dir():
            raise ValueError(f"target parent does not exist: {value}")

    def _target(self, value: str) -> Path:
        relative = restricted_relative_path(
            value, (*_ALLOWED_DIRECTORIES, *_ALLOWED_FILES)
        )
        _require_normalized_path(value)
        _require_bounded_path(value, relative)
        _require_portable_components(relative)
        normalized = relative.as_posix()
        _require_allowed_root(normalized)
        _require_approved_suffix(relative, normalized)
        target = self.vault.joinpath(*relative.parts)
        self._require_no_reparse_traversal(relative, value)
        self._require_canonical_parent(target, value)
        return target

    def _require_claim_target_identity(self, item: object) -> None:
        if not isinstance(item, Mapping) or set(item) != _CLAIM_TARGET_FIELDS:
            raise ValueError("claim_targets precondition has invalid fields")
        self._target(str(item["page"]))
        if not _is_filled_string(item["claim_id"]):
            raise ValueError("claim_targets precondition has invalid claim id")

    def _require_claim_target(self, item: object, identities: set) -> None:
        self._require_claim_target_identity(item)
        _require_claim_target_hashes(item)
        identity = (str(item["page"]), item["claim_id"])
        if identity in identities:
            raise ValueError("claim_targets precondition contains duplicates")
        identities.add(identity)

    def _validated_claim_targets(self, expected: object) -> list[dict]:
        _require_bounded_sequence(expected, "claim_targets")
        targets: list[dict] = []
        identities: set = set()
        for item in expected:
            self._require_claim_target(item, identities)
            targets.append(dict(item))
        return sorted(targets, key=lambda item: (item["page"], item["claim_id"]))

    def _validated_fence_precondition(self, path: str, expected: object) -> object:
        if path == "project_lease":
            return _validated_project_lease(expected)
        if path == "intent_fence":
            return _validated_intent_fence(expected)
        if path == "capture_binding":
            return _validated_capture_binding(expected)
        self._target(path)
        _require_hash_or_absent(expected)
        return expected

    def _validated_precondition(self, path: str, expected: object) -> object:
        """One precondition value, validated by the kind its key names."""
        if path == "claim_targets":
            return self._validated_claim_targets(expected)
        if path == "claim_tree_manifest":
            return validate_claim_tree_manifest(expected)
        if path == "guardrails_source_manifest":
            return validate_guardrail_source_manifest(expected)
        return self._validated_fence_precondition(path, expected)

    def _validate_preconditions(self, preconditions: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(preconditions, Mapping):
            raise TypeError("preconditions must be a mapping")
        result: dict[str, object] = {}
        for path, expected in preconditions.items():
            result[path] = self._validated_precondition(path, expected)
        canonical_json_bytes(result)
        return result

    def _request_hash(
        self, changes: Sequence[MarkdownChange], preconditions: Mapping[str, object]
    ) -> str:
        request = {
            "changes": [
                {
                    "kind": change.kind,
                    "path": change.path,
                    "content_hash": ABSENT
                    if change.content is None
                    else sha256_bytes(change.content),
                }
                for change in changes
            ],
            "preconditions": dict(preconditions),
        }
        return sha256_bytes(canonical_json_bytes(request))

    def _stage_state(self, root: Path, position: int, content: bytes | None) -> object:
        if content is None:
            return ABSENT
        name = f"{position:06d}.bin"
        self._write_new_file(root / name, content)
        return {"sha256": sha256_bytes(content), "artifact": f"{root.name}/{name}"}

    def _verify_plan_artifacts(self, plan: Mapping[str, object], artifact_root: Path) -> None:
        operations = plan["operations"]
        assert isinstance(operations, list)
        for operation in operations:
            assert isinstance(operation, dict)
            self._verify_operation_artifacts(operation, artifact_root)

    def _verify_operation_artifacts(
        self, operation: Mapping[str, object], artifact_root: Path
    ) -> None:
        for state_name in ("before", "after"):
            self._verify_artifact_state(
                operation[state_name], state_name, artifact_root
            )

    @staticmethod
    def _verify_artifact_state(
        state: object, state_name: str, artifact_root: Path
    ) -> None:
        if state == ABSENT:
            return
        assert isinstance(state, dict)
        relative = restricted_relative_path(str(state["artifact"]), (state_name,))
        artifact = artifact_root.joinpath(*relative.parts)
        if sha256_bytes(artifact.read_bytes()) != state["sha256"]:
            raise RuntimeError(f"transaction artifact hash mismatch: {relative}")

    def _write_new_file(self, path: Path, content: bytes, *, owner_only: bool = True) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
        if owner_only:
            if os.name != "nt":
                _harden_owner_only(path, 0o600)
        fsync_file(path)

    def _read_target(self, target: Path) -> bytes | None:
        return self._read_bounded_target(target, MAX_KNOWLEDGE_TARGET_BYTES)

    def _parent_identity(self, parent: Path) -> tuple[int, int]:
        metadata = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(parent):
            raise RuntimeError(f"parent identity is not a stable directory: {parent}")
        return _stat_identity(metadata)

    def _capture_target(
        self, target: Path, *, max_before_bytes: int | None = None
    ) -> tuple[bytes | None, tuple[int, int]]:
        if not _use_posix_dir_fd():
            with self._hold_windows_parent(target.parent):
                before = self._parent_identity(target.parent)
                content = self._read_bounded_target(target, max_before_bytes)
                if self._parent_identity(target.parent) != before:
                    raise RuntimeError(f"parent identity changed while reading {target}")
                return content, before
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target.parent, flags)
        try:
            metadata = os.fstat(descriptor)
            identity = _stat_identity(metadata)
            content = self._read_bounded_from_parent(
                descriptor, target.name, max_before_bytes
            )
            if self._parent_identity(target.parent) != identity:
                raise RuntimeError(f"parent identity changed while reading {target}")
            return content, identity
        finally:
            os.close(descriptor)

    def _read_bounded_target(self, target: Path, max_bytes: int | None) -> bytes | None:
        before = _lstat_or_none(target)
        if before is None:
            return None
        self._validate_capture_metadata(before, target, max_bytes)
        descriptor = _open_capture_descriptor(target, target)
        try:
            content, after = self._read_stable_descriptor(
                descriptor, before, target, max_bytes
            )
            self._require_unchanged_read(after, _lstat_or_none(target), target)
            return content
        finally:
            os.close(descriptor)

    def _read_bounded_from_parent(
        self, parent_descriptor: int, name: str, max_bytes: int | None
    ) -> bytes | None:
        before = _stat_at_or_none(parent_descriptor, name)
        if before is None:
            return None
        self._validate_capture_metadata(before, Path(name), max_bytes)
        descriptor = _open_capture_descriptor(name, name, dir_fd=parent_descriptor)
        try:
            content, after = self._read_stable_descriptor(
                descriptor, before, Path(name), max_bytes
            )
            self._require_unchanged_read(
                after, _stat_at_or_none(parent_descriptor, name), name
            )
            return content
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_capture_metadata(
        metadata: os.stat_result, target: Path, max_bytes: int | None
    ) -> None:
        _require_regular_file(metadata, target)
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise TargetTooLargeError(
                f"transaction target exceeds {max_bytes} bytes: {target}"
            )

    def _read_stable_descriptor(
        self,
        descriptor: int,
        before: os.stat_result,
        target: Path,
        max_bytes: int | None,
    ) -> tuple[bytes, os.stat_result]:
        opened = os.fstat(descriptor)
        if not self._same_capture_identity(before, opened):
            raise ValueError(f"transaction target changed before open: {target}")
        self._validate_capture_metadata(opened, target, max_bytes)
        content = _descriptor_bytes(descriptor, max_bytes)
        after = os.fstat(descriptor)
        if not self._same_capture_snapshot(opened, after):
            raise ValueError(f"transaction target changed while reading: {target}")
        _require_read_size(content, after, target, max_bytes)
        return content, after

    @staticmethod
    def _same_capture_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return _stat_identity(left) == _stat_identity(right)

    @classmethod
    def _same_capture_snapshot(cls, left: os.stat_result, right: os.stat_result) -> bool:
        metadata_matches = (
            left.st_mode,
            left.st_size,
            left.st_mtime_ns,
        ) == (
            right.st_mode,
            right.st_size,
            right.st_mtime_ns,
        )
        # Python exposes creation time as st_ctime on Windows, where descriptor and
        # path views may differ. It is not a content-change signal on that platform.
        change_time_matches = os.name == "nt" or left.st_ctime_ns == right.st_ctime_ns
        return (
            cls._same_capture_identity(left, right)
            and metadata_matches
            and change_time_matches
        )

    @contextlib.contextmanager
    def _stable_parent(self, row: sqlite3.Row) -> Iterator[tuple[Path, int | None]]:
        target = self._target(row["path"])
        expected = _required_parent_identity(row)
        if not _use_posix_dir_fd():
            yield from self._windows_stable_parent(target, expected, row["path"])
            return
        yield from self._posix_stable_parent(target, expected, row["path"])

    def _windows_stable_parent(
        self, target: Path, expected: object, path: str
    ) -> Iterator[tuple[Path, int | None]]:
        with self._hold_windows_parent(target.parent):
            self._require_parent_identity(target.parent, expected, path)
            try:
                yield target, None
            finally:
                self._require_parent_identity(target.parent, expected, path)

    def _posix_stable_parent(
        self, target: Path, expected: object, path: str
    ) -> Iterator[tuple[Path, int | None]]:
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(target.parent, flags)
        try:
            if _stat_identity(os.fstat(descriptor)) != expected:
                raise RuntimeError(f"parent identity mismatch for {path}")
            yield target, descriptor
            self._require_parent_identity(target.parent, expected, path)
        finally:
            os.close(descriptor)

    def _require_parent_identity(
        self, parent: Path, expected: object, path: str
    ) -> None:
        if self._parent_identity(parent) != expected:
            raise RuntimeError(f"parent identity mismatch for {path}")

    @contextlib.contextmanager
    def _hold_windows_parent(self, parent: Path) -> Iterator[None]:
        if os.name != "nt":
            raise RuntimeError(
                "safe non-POSIX mutation requires Windows directory handles"
            )
        paths = self._vault_chain(parent)
        handles: list[int] = []
        previous_parent_handle = getattr(self._local, "windows_parent_handle", None)
        try:
            for path in paths:
                handles.append(_open_windows_directory(path))
            self._local.windows_parent_handle = handles[-1]
            yield
        finally:
            self._local.windows_parent_handle = previous_parent_handle
            _close_windows_handles(handles)

    def _vault_chain(self, parent: Path) -> list[Path]:
        """Every directory from the vault down to the parent, vault first."""
        try:
            relative = parent.relative_to(self.vault)
        except ValueError as exc:
            raise RuntimeError("target parent is outside the vault") from exc
        paths = [self.vault]
        current = self.vault
        for part in relative.parts:
            current = current / part
            paths.append(current)
        return paths

    def _hash_bounded_target(self, target: Path) -> str:
        before = _lstat_or_none(target)
        if before is None:
            return ABSENT
        if not self._within_capture_bound(before, target):
            return _OVERSIZED_TARGET
        descriptor = os.open(target, _CAPTURE_OPEN_FLAGS)
        try:
            hashed = self._hashed_or_none(descriptor, before, target)
            if hashed is None:
                return _OVERSIZED_TARGET
            self._require_unchanged_hash(hashed[1], _lstat_or_none(target), target)
            return hashed[0]
        finally:
            os.close(descriptor)

    def _hash_bounded_from_parent(self, parent_descriptor: int, name: str) -> str:
        before = _stat_at_or_none(parent_descriptor, name)
        if before is None:
            return ABSENT
        if not self._within_capture_bound(before, Path(name)):
            return _OVERSIZED_TARGET
        descriptor = os.open(name, _CAPTURE_OPEN_FLAGS, dir_fd=parent_descriptor)
        try:
            hashed = self._hashed_or_none(descriptor, before, Path(name))
            if hashed is None:
                return _OVERSIZED_TARGET
            self._require_unchanged_hash(
                hashed[1], _stat_at_or_none(parent_descriptor, name), name
            )
            return hashed[0]
        finally:
            os.close(descriptor)

    def _within_capture_bound(self, metadata: os.stat_result, target: Path) -> bool:
        """False means the target is too large to be captured at all."""
        try:
            self._validate_capture_metadata(
                metadata, target, MAX_KNOWLEDGE_TARGET_BYTES
            )
        except TargetTooLargeError:
            return False
        return True

    def _hashed_or_none(
        self, descriptor: int, before: os.stat_result, target: Path
    ) -> tuple[str, os.stat_result] | None:
        """None means the target grew past the bound while it was open."""
        try:
            return self._hash_stable_descriptor(descriptor, before, target)
        except TargetTooLargeError:
            return None

    def _require_unchanged_read(
        self, after: os.stat_result, current: os.stat_result | None, label: object
    ) -> None:
        if current is None or not self._same_capture_snapshot(after, current):
            raise ValueError(f"transaction target changed while reading: {label}")

    def _require_unchanged_hash(
        self, after: os.stat_result, current: os.stat_result | None, label: object
    ) -> None:
        if current is None or not self._same_capture_snapshot(after, current):
            raise ValueError(f"transaction target changed while hashing: {label}")

    def _hash_stable_descriptor(
        self, descriptor: int, before: os.stat_result, target: Path
    ) -> tuple[str, os.stat_result]:
        opened = os.fstat(descriptor)
        if not self._same_capture_identity(before, opened):
            raise ValueError(f"transaction target changed before hash: {target}")
        self._validate_capture_metadata(
            opened, target, MAX_KNOWLEDGE_TARGET_BYTES
        )
        digest, total = _bounded_digest(descriptor, target)
        after = os.fstat(descriptor)
        if not self._same_capture_snapshot(opened, after) or total != after.st_size:
            raise ValueError(f"transaction target changed while hashing: {target}")
        return digest, after

    def _hash_operation_target(
        self, target: Path, parent_descriptor: int | None
    ) -> str:
        if parent_descriptor is None:
            return self._hash_bounded_target(target)
        return self._hash_bounded_from_parent(parent_descriptor, target.name)

    def _require_undoable(self, transaction_id: str) -> None:
        original = self._record(transaction_id)
        if original.state != "committed":
            raise RuntimeError("only a committed transaction can be undone")
        if _parse_timestamp(original.updated_at) < datetime.now(
            timezone.utc
        ) - timedelta(days=30):
            raise RuntimeError("transaction is outside the 30-day undo window")
        if not (self.transaction_root / transaction_id).is_dir():
            raise RuntimeError("transaction undo images are no longer retained")

    def _require_targets_unchanged(
        self,
        rows: Sequence[sqlite3.Row],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        for row in rows:
            self._require_operation_active(deadline, cancelled)
            if self._operation_hash(row) != row["after_hash"]:
                raise RuntimeError(
                    "undo precondition failed: current target changed"
                )

    def _undo_changes(
        self,
        transaction_id: str,
        rows: Sequence[sqlite3.Row],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[list[MarkdownChange], dict[str, object]]:
        changes: list[MarkdownChange] = []
        preconditions: dict[str, object] = {}
        for row in rows:
            self._require_operation_active(deadline, cancelled)
            preconditions[row["path"]] = row["after_hash"]
            changes.append(self._undo_change(transaction_id, row))
        return changes, preconditions

    def _undo_change(self, transaction_id: str, row: sqlite3.Row) -> MarkdownChange:
        """The inverse of one committed operation, read back from its before-image."""
        if row["before_hash"] == ABSENT:
            return MarkdownChange.delete(row["path"])
        before = (
            self.transaction_root
            / transaction_id
            / "before"
            / f"{row['position']:06d}.bin"
        ).read_bytes()
        if sha256_bytes(before) != row["before_hash"]:
            raise RuntimeError("transaction before-image is corrupt")
        if row["after_hash"] == ABSENT:
            return MarkdownChange.create(row["path"], before)
        return MarkdownChange.replace(row["path"], before)

    def _operation_hash(self, row: sqlite3.Row) -> str:
        with self._stable_parent(row) as (target, parent_descriptor):
            return self._hash_operation_target(target, parent_descriptor)

    def _current_hash(self, path: str) -> str:
        return self._hash_bounded_target(self._target(path))

    def _check_preconditions(
        self,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        *,
        database: sqlite3.Connection | None = None,
    ) -> None:
        for path, expected in preconditions.items():
            self._check_one_precondition(
                path, expected, preconditions, operation_states, database
            )

    def _check_one_precondition(
        self,
        path: str,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        """A named precondition has its own checker; anything else is a target."""
        checker = getattr(self, _PRECONDITION_METHODS.get(path, ""), None)
        if checker is not None:
            checker(expected, preconditions, operation_states, database)
            return
        self._check_target_precondition(path, expected, operation_states)

    def _check_claim_targets_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        assert isinstance(expected, Sequence)
        self._check_claim_targets(expected, operation_states)

    def _check_claim_tree_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        assert isinstance(expected, Mapping)
        if not self._claim_tree_matches(
            expected, snapshot_claim_tree(self.vault), operation_states
        ):
            raise TransactionFailure(
                "persisted claim tree manifest precondition failed",
                "precondition_failed",
                "quarantined",
            )

    def _check_guardrails_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        assert isinstance(expected, Mapping)
        if expected != _guardrail_sources_or_fail(self.vault):
            raise TransactionFailure(
                "persisted guardrails source manifest precondition failed",
                "precondition_failed",
                "quarantined",
            )

    def _check_lease_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        assert isinstance(expected, Mapping)
        if database is not None:
            self._check_project_lease(database, expected)
            return
        with self._connect() as lease_database:
            self._check_project_lease(lease_database, expected)

    def _check_capture_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        if database is not None:
            self._check_capture_preconditions(database, preconditions)
            return
        with self._connect() as precondition_database:
            self._check_capture_preconditions(precondition_database, preconditions)

    def _check_target_precondition(
        self,
        path: str,
        expected: object,
        operation_states: Mapping[str, tuple[str, str]],
    ) -> None:
        current = self._current_hash(path)
        if current == expected:
            return
        if _reconciled_target(operation_states.get(path), expected, current):
            return
        raise TransactionFailure(
            f"persisted precondition failed for {path}",
            "precondition_failed",
            "quarantined",
        )

    @staticmethod
    def _check_capture_preconditions(
        database: sqlite3.Connection, preconditions: Mapping[str, object]
    ) -> None:
        fence = preconditions.get("intent_fence")
        binding = preconditions.get("capture_binding")
        if not isinstance(fence, Mapping) or not isinstance(binding, Mapping):
            raise TransactionFailure(
                "capture preconditions are incomplete",
                "precondition_failed",
                "quarantined",
            )
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        row = database.execute(
            """SELECT 1 FROM intent_fences AS fence
               JOIN capture_binding_projections AS binding
                 ON binding.intent_id=fence.intent_id
                AND binding.intent_fence_token=fence.token
                AND binding.intent_fence_epoch=fence.fencing_epoch
               WHERE fence.intent_id=? AND fence.mode=? AND fence.token=?
                 AND fence.fencing_epoch=? AND fence.expires_at>?
                 AND binding.task_id=? AND binding.active_link_digest=?
                 AND binding.seal_digest=?""",
            (
                fence["intent_id"],
                fence["mode"],
                fence["token"],
                fence["fencing_epoch"],
                now,
                binding["task_id"],
                binding["active_link_digest"],
                binding["seal_digest"],
            ),
        ).fetchone()
        if row is None:
            raise TransactionFailure(
                "persisted capture precondition failed",
                "precondition_failed",
                "quarantined",
            )

    def _check_claim_targets(
        self,
        expected: Sequence[object],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> None:
        for item in expected:
            assert isinstance(item, Mapping)
            self._check_one_claim_target(item, operation_states)

    def _check_one_claim_target(
        self,
        item: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> None:
        path = str(item["page"])
        operation = operation_states.get(path)
        if operation is not None and self._current_hash(path) == operation[1]:
            return
        if not self._claim_target_matches(path, item):
            raise TransactionFailure(
                f"persisted claim target precondition failed for {path}#{item['claim_id']}",
                "precondition_failed",
                "quarantined",
            )

    def _claim_target_matches(self, path: str, item: Mapping[str, object]) -> bool:
        """Anything that cannot be read or parsed is a failed precondition."""
        from claims import MAX_CLAIM_PAGE_BYTES, parse_claim_ledger

        try:
            content = read_stable_bytes(
                self._target(path),
                MAX_CLAIM_PAGE_BYTES,
                label="claim target precondition page",
            )
            record = _sole_claim_record(parse_claim_ledger(content), item["claim_id"])
            return _claim_record_matches(record, item)
        except (OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _claim_tree_matches(
        expected: Mapping[str, object],
        current: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> bool:
        expected_entries = _manifest_entries(expected)
        current_entries = _manifest_entries(current)
        return all(
            _entry_matches(
                expected_entries.get(path, ABSENT),
                current_entries.get(path, ABSENT),
                operation_states.get(path),
            )
            for path in set(expected_entries) | set(current_entries)
        )

    @staticmethod
    def _check_project_lease(
        database: sqlite3.Connection, expected: Mapping[str, object]
    ) -> None:
        row = database.execute(
            "SELECT * FROM project_leases WHERE project = ?",
            (expected["project"],),
        ).fetchone()
        if not _lease_row_matches(row, expected):
            raise TransactionFailure(
                "persisted project lease precondition failed",
                "precondition_failed",
                "quarantined",
            )

    def _killpoint(self, name: str, parent_transaction_id: str | None = None) -> None:
        configured = os.environ.get("LLM_WIKI_TRANSACTION_KILLPOINT")
        if configured in _killpoint_aliases(name, parent_transaction_id):
            os._exit(86)

    def _reconcile_operation_states(
        self, transaction_id: str, rows: Sequence[sqlite3.Row]
    ) -> set[str]:
        reconciled_after: set[str] = set()
        for row in rows:
            if self._reconcile_one_operation(transaction_id, row):
                reconciled_after.add(row["path"])
        return reconciled_after

    def _reconcile_one_operation(self, transaction_id: str, row: sqlite3.Row) -> bool:
        """True when the target already holds this operation's after state."""
        current = self._operation_hash(row)
        if row["applied"]:
            if current != row["after_hash"]:
                raise RuntimeError(f"after state mismatch for {row['path']}")
            return True
        if current == row["after_hash"]:
            self._mark_operation_applied(transaction_id, row["position"])
            return True
        if current != row["before_hash"]:
            raise RuntimeError(f"before state mismatch for {row['path']}")
        return False

    def _mark_operation_applied(self, transaction_id: str, position: int) -> None:
        active_database = getattr(self._local, "mutation_database", None)
        if active_database is not None:
            active_database.execute(
                'UPDATE "operation" SET applied = 1 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )
            return
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            database.execute(
                'UPDATE "operation" SET applied = 1 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )

    def _mutate_and_mark(
        self,
        transaction_id: str,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        *,
        content_guard: str | None = None,
    ) -> None:
        active_database = getattr(self._local, "mutation_database", None)
        if active_database is not None:
            self._assert_writer_ownership(active_database)
            self._apply_forward_operation(row, operation_plan, content_guard)
            self._require_operation_state(row, row["after_hash"], "after state")
            self._mark_operation_applied(transaction_id, row["position"])
            return
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            self._local.mutation_database = database
            try:
                self._apply_forward_operation(row, operation_plan, content_guard)
                self._require_operation_state(row, row["after_hash"], "after state")
                self._mark_operation_applied(transaction_id, row["position"])
            finally:
                self._local.mutation_database = None

    @staticmethod
    def _content_guard(
        record: TransactionRecord, plan: Mapping[str, object]
    ) -> str | None:
        configured = plan.get("content_guard")
        if configured == "model_output":
            return configured
        if record.operation_id.startswith(
            ("compile:", "compile-quarantine:", "contradiction:")
        ):
            return "model_output"
        return None

    def _apply_forward_operation(
        self,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        content_guard: str | None,
    ) -> None:
        previous = getattr(self._local, "content_guard", None)
        self._local.content_guard = content_guard
        try:
            self._apply_operation(row, operation_plan)
        finally:
            self._local.content_guard = previous

    def _assert_writer_ownership(self, database: sqlite3.Connection) -> None:
        owner_token = getattr(self._local, "gate_token", None)
        fencing_epoch = getattr(self._local, "gate_fence", None)
        owner = getattr(self._local, "gate_owner", None)
        if (
            getattr(self, "_database_contract", None) == _COORDINATOR_V3_CONTRACT
            and owner is not None
        ):
            registry = self._ownership_registry()
            registry.require(database, owner)
            cursor = database.execute(
                """UPDATE writer_owners
                   SET heartbeat_at=(
                           SELECT heartbeat_at FROM maintenance_owners
                           WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                             AND fencing_epoch=?
                       ),
                       expires_at=(
                           SELECT expires_at FROM maintenance_owners
                           WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                             AND fencing_epoch=?
                       )
                   WHERE gate_name='global' AND owner_token=? AND fencing_epoch=?""",
                (
                    owner.role,
                    owner.scope,
                    owner.actor_id,
                    owner.token,
                    owner.epoch,
                    owner.role,
                    owner.scope,
                    owner.actor_id,
                    owner.token,
                    owner.epoch,
                    owner_token,
                    fencing_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Markdown writer gate ownership was lost before mutation"
                )
            return
        heartbeat = _now()
        cursor = database.execute(
            "UPDATE writer_owners SET heartbeat_at = ?, expires_at = ? "
            "WHERE gate_name = 'global' AND owner_token = ? AND fencing_epoch = ? "
            "AND expires_at > ?",
            (
                heartbeat,
                _future_timestamp(_WRITER_LEASE_SECONDS),
                owner_token,
                fencing_epoch,
                heartbeat,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Markdown writer gate ownership was lost before mutation")

    def _require_operation_state(self, row: sqlite3.Row, expected: str, label: str) -> None:
        if self._operation_hash(row) != expected:
            raise RuntimeError(f"{label} mismatch for {row['path']}")

    def _apply_operation(self, row: sqlite3.Row, operation_plan: Mapping[str, object]) -> None:
        with self._stable_parent(row) as (target, parent_descriptor):
            self._require_operation_target_state(
                target, parent_descriptor, row, "before"
            )
            if parent_descriptor is None:
                self._apply_windows_operation(row, operation_plan, target)
                return
            self._before_target_mutation(target)
            if row["kind"] == "delete":
                os.unlink(target.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
                return
            content = self._verified_after_content(row, operation_plan)
            self._publish_at_parent(row, target, parent_descriptor, content)

    def _require_operation_target_state(
        self,
        target: Path,
        parent_descriptor: int | None,
        row: sqlite3.Row,
        state_name: str,
    ) -> None:
        current = self._hash_operation_target(target, parent_descriptor)
        if current != row[f"{state_name}_hash"]:
            raise RuntimeError(f"{state_name} state mismatch for {row['path']}")

    def _verified_after_content(
        self, row: sqlite3.Row, operation_plan: Mapping[str, object]
    ) -> bytes:
        """The staged after-image, refused unless it hashes and publishes clean."""
        after = operation_plan["after"]
        if not isinstance(after, dict):
            raise RuntimeError("transaction after-image is absent")
        artifact = (
            self.transaction_root / row["transaction_id"] / str(after["artifact"])
        )
        content = artifact.read_bytes()
        if sha256_bytes(content) != row["after_hash"]:
            raise RuntimeError(
                f"transaction after-image is corrupt for {row['path']}"
            )
        if getattr(self._local, "content_guard", None) == "model_output":
            require_safe_publication(content)
        return content

    def _publish_at_parent(
        self,
        row: sqlite3.Row,
        target: Path,
        parent_descriptor: int,
        content: bytes,
    ) -> None:
        temporary_name = f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            self._write_new_file_at(parent_descriptor, temporary_name, content)
            self._require_operation_target_state(
                target, parent_descriptor, row, "before"
            )
            _link_or_replace(
                row["kind"],
                temporary_name,
                target.name,
                parent_descriptor,
                row["path"],
            )
            os.fsync(parent_descriptor)
            self._require_operation_target_state(
                target, parent_descriptor, row, "after"
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_descriptor)

    def _apply_windows_operation(
        self,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        target: Path,
    ) -> None:
        if getattr(self._local, "windows_parent_handle", None) is None:
            raise RuntimeError("Windows parent handle is not held at mutation")
        if row["kind"] == "delete":
            self._delete_windows_target(target)
            return
        content = self._verified_after_content(row, operation_plan)
        self._publish_windows_target(row, target, content)

    def _delete_windows_target(self, target: Path) -> None:
        target_handle = _open_windows_file_for_mutation(target)
        try:
            self._before_target_mutation(target)
            _delete_windows_handle(target_handle)
        finally:
            _close_windows_handle(target_handle)
        fsync_directory(target.parent)

    def _publish_windows_target(
        self, row: sqlite3.Row, target: Path, content: bytes
    ) -> None:
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        self._write_new_file(temporary, content, owner_only=False)
        self._before_target_mutation(target)
        outcome = durable_publish_file(
            temporary,
            target,
            replace=row["kind"] == "replace",
            expected_sha256=row["after_hash"],
            max_bytes=MAX_KNOWLEDGE_TARGET_BYTES,
        )
        if outcome == "duplicate":
            temporary.unlink()
            fsync_directory(target.parent)

    def _before_target_mutation(self, target: Path) -> None:
        """Failure-injection boundary after parent binding and before mutation."""
        del target
        self._require_current_operation_active()

    def _require_current_operation_active(self) -> None:
        deadline = getattr(self._local, "recovery_deadline", None)
        cancelled = getattr(self._local, "recovery_cancelled", None)
        if deadline is not None and self._recovery_stopped(deadline, cancelled):
            raise TimeoutError("transaction mutation deadline or cancellation reached")

    def _require_operation_active(
        self, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        if self._recovery_stopped(deadline, cancelled):
            raise TimeoutError("transaction mutation deadline or cancellation reached")

    def _write_new_file_at(self, parent_descriptor: int, name: str, content: bytes) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    def _load_verified_plan(self, record: TransactionRecord) -> dict[str, object]:
        plan_path = self.transaction_root / record.id / "plan.json"
        try:
            plan_bytes = plan_path.read_bytes()
            with self._connect() as database:
                expected = database.execute(
                    'SELECT plan_hash FROM "transaction" WHERE id = ?', (record.id,)
                ).fetchone()["plan_hash"]
            if sha256_bytes(plan_bytes) != expected:
                raise RuntimeError("transaction plan hash mismatch")
            plan = json.loads(plan_bytes)
            validate_schema(plan, _SCHEMA)
            self._verify_plan_artifacts(plan, self.transaction_root / record.id)
        except (AssertionError, KeyError, OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("transaction after-image is corrupt") from exc
        return plan

    def _operation_rows(self, transaction_id: str) -> list[sqlite3.Row]:
        with self._connect() as database:
            return list(
                database.execute(
                    'SELECT * FROM "operation" WHERE transaction_id = ? ORDER BY position',
                    (transaction_id,),
                )
            )

    def _record(self, transaction_id: str) -> TransactionRecord:
        record = self._record_if_present(transaction_id)
        if record is None:
            raise KeyError(f"unknown transaction: {transaction_id}")
        return record

    def _record_if_present(self, transaction_id: str) -> TransactionRecord | None:
        with self._connect() as database:
            row = database.execute(
                'SELECT * FROM "transaction" WHERE id = ?', (transaction_id,)
            ).fetchone()
            if row is None:
                return None
            operation_rows = list(
                database.execute(
                    'SELECT * FROM "operation" WHERE transaction_id = ? ORDER BY position',
                    (transaction_id,),
                )
            )
        return TransactionRecord(
            id=row["id"],
            operation_id=row["operation_id"],
            state=row["state"],
            operations=tuple(
                MarkdownOperation(
                    operation["kind"],
                    operation["path"],
                    operation["before_hash"],
                    operation["after_hash"],
                )
                for operation in operation_rows
            ),
            preconditions=json.loads(row["preconditions_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            parent_transaction_id=row["parent_transaction_id"],
            error_code=row["error_code"],
        )

    def _record_for_operation_id(self, operation_id: str) -> TransactionRecord | None:
        with self._connect() as database:
            row = database.execute(
                'SELECT id, state FROM "transaction" WHERE operation_id = ?',
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._record(row["id"])
        except KeyError:
            if row["state"] == "preparing":
                return None
            raise

    def _request_hash_for_operation_id(self, operation_id: str) -> str | None:
        with self._connect() as database:
            row = database.execute(
                'SELECT request_hash FROM "transaction" WHERE operation_id = ?', (operation_id,)
            ).fetchone()
        return None if row is None else row["request_hash"]

    def _remove_artifacts(self, root: Path) -> None:
        if not root.exists():
            return
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        root.rmdir()


def _redacted_record(record: TransactionRecord) -> dict[str, str | None]:
    return {
        "transaction_id": record.id,
        "state": record.state,
        "code": record.error_code,
    }


def _print_canonical_json(payload: object) -> None:
    sys.stdout.write(canonical_json_bytes(payload).decode("utf-8") + "\n")


def _bounded_transaction_id(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if any(character not in "0123456789abcdefghijklmnopqrstuvwxyz-_" for character in value):
        return None
    return value


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


_CLI_MESSAGE_CODES = (
    ("at least 30", "retention_too_short"),
    ("undo precondition", "undo_precondition_failed"),
    ("undo window", "undo_window_expired"),
    ("only a committed transaction", "transaction_not_committed"),
    ("before-image is corrupt", "before_image_corrupt"),
)


_UNDO_KILLPOINT_ALIASES = {
    "after_each_target": "after_each_undo_target",
    "before_commit": "before_undo_commit",
}


def _killpoint_aliases(name: str, parent_transaction_id: str | None) -> set[str]:
    """An undo transaction answers to its own killpoint names as well."""
    aliases = {name, _undo_killpoint_alias(name, parent_transaction_id)}
    if parent_transaction_id is None:
        return aliases
    extra = _UNDO_KILLPOINT_ALIASES.get(name)
    if extra is not None:
        aliases.add(extra)
    return aliases


def _undo_killpoint_alias(name: str, parent_transaction_id: str | None) -> str:
    if not name.startswith("after_"):
        return name
    prefix = "undo_" if parent_transaction_id is not None else ""
    return f"{name[:6]}{prefix}{name[6:]}"


def _cli_error_code(error: Exception) -> str:
    if isinstance(error, TransactionFailure):
        return error.code
    if isinstance(error, KeyError):
        return "unknown_transaction"
    return _cli_message_code(str(error)) or _cli_type_code(error)


def _cli_message_code(message: str) -> str | None:
    for fragment, code in _CLI_MESSAGE_CODES:
        if fragment in message:
            return code
    return None


def _cli_type_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "writer_busy"
    if isinstance(error, ValueError):
        return "invalid_argument"
    return "operation_failed"


def _main() -> int:
    args = _parse_cli_args()
    coordinator: MarkdownCoordinator | None = None
    try:
        coordinator = _cli_coordinator()
        payload, exit_code = _run_cli_command(coordinator, args)
    except Exception as error:  # noqa: BLE001 - the CLI answers with one envelope
        _print_canonical_json(_cli_failure(coordinator, args, error))
        return 2
    _print_canonical_json(payload)
    return exit_code


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("recover", help="recover incomplete transactions")
    undo_parser = subparsers.add_parser("undo", help="undo a committed transaction")
    undo_parser.add_argument("transaction_id")
    prune_parser = subparsers.add_parser(
        "prune", help="prune expired transaction images"
    )
    prune_parser.add_argument("--retention-days", type=int, default=30)
    return parser.parse_args()


def _cli_coordinator() -> MarkdownCoordinator:
    vault = Path(
        os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent)
    ).resolve()
    state_root = Path(os.environ.get("LLM_WIKI_STATE_ROOT", vault)).resolve()
    return MarkdownCoordinator(vault, state_root)


def _run_cli_command(
    coordinator: MarkdownCoordinator, args: argparse.Namespace
) -> tuple[object, int]:
    if args.command == "recover":
        return _cli_recover(coordinator)
    if args.command == "undo":
        return _cli_undo(coordinator, args.transaction_id), 0
    return {"pruned": coordinator.prune(retention_days=args.retention_days)}, 0


def _cli_recover(coordinator: MarkdownCoordinator) -> tuple[object, int]:
    """A transaction that needs attention is reported by the exit code."""
    records = coordinator.recover()
    payload = [_redacted_record(record) for record in records]
    needs_attention = any(
        record.state in {"conflicted", "quarantined"} for record in records
    )
    return payload, 2 if needs_attention else 0


def _cli_undo(
    coordinator: MarkdownCoordinator, transaction_id: str
) -> dict[str, object]:
    undo = coordinator.undo(transaction_id)
    committed = coordinator.apply(undo.id)
    return {
        **_redacted_record(committed),
        "parent_transaction_id": committed.parent_transaction_id,
    }


def _cli_failure(
    coordinator: MarkdownCoordinator | None,
    args: argparse.Namespace,
    error: Exception,
) -> dict[str, object]:
    transaction_id = _bounded_transaction_id(getattr(args, "transaction_id", None))
    return {
        "code": _cli_error_code(error),
        "state": _cli_transaction_state(coordinator, transaction_id),
        "transaction_id": transaction_id,
    }


def _cli_transaction_state(
    coordinator: MarkdownCoordinator | None, transaction_id: str | None
) -> str | None:
    """A lookup that fails must not replace the error being reported."""
    if coordinator is None or transaction_id is None:
        return None
    try:
        record = coordinator._record_if_present(transaction_id)
    except Exception:  # noqa: BLE001 - diagnosis is best effort here
        return None
    if record is None:
        return None
    return record.state


if __name__ == "__main__":
    raise SystemExit(_main())
