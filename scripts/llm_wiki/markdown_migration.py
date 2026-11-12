"""Coordinator v2 rows migrate into a fresh, validated coordinator-v3 candidate."""
from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from llm_wiki import sqlite_io
from llm_wiki.coordinator_invariants import (
    coordinator_migration_error,
    coordinator_v3_cross_table_invariant,
    coordinator_v3_row_counts,
)
from llm_wiki.coordinator_schema import (
    COORDINATOR_V3_CONTRACT,
    COORDINATOR_V3_TABLE_SQL,
    coordinator_v3_object_matches,
    coordinator_v3_schema_complete,
    coordinator_v3_statements,
    known_coordinator_index,
)
from llm_wiki.coordinator_validation import validate_coordinator_v3_database
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.markdown_store import harden_owner_only, parse_stored_timestamp
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_contracts import MigrationStatement, quote_sqlite_identifier
from llm_wiki.sqlite_migrations import run_resumable_migration
from llm_wiki.sqlite_transactions import begin_immediate
from reliable_memory import open_operational_db

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


def coordinator_table_columns(
    database: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in database.execute(
        f'PRAGMA table_info({quote_sqlite_identifier(table)})'
    ))


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
        raise coordinator_migration_error(
            "coordinator_v2_schema_unknown",
            "coordinator v2 source has unknown schema objects",
        )


def _repair_partial_coordinator_v3_schema(
    database: sqlite3.Connection, *, allow_populated_rebuild: bool
) -> None:
    if coordinator_v3_schema_complete(database):
        return
    _require_unpublished_coordinator(database)
    objects = _coordinator_schema_objects(database)
    if not objects or _coordinator_schema_exact(database, objects):
        return
    require_rebuildable_schema(database, objects, allow_populated_rebuild)
    _drop_coordinator_objects(database, objects)


def _require_unpublished_coordinator(database: sqlite3.Connection) -> None:
    """A published database is never repaired in place."""
    application_id = int(database.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(database.execute("PRAGMA user_version").fetchone()[0])
    if (application_id, user_version) != (0, 0):
        raise coordinator_migration_error(
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
    return all(_coordinator_v3_object_known(database, kind, name) for kind, name in objects)


def _coordinator_v3_object_known(database: sqlite3.Connection, kind: object, name: object) -> bool:
    expected = dict(COORDINATOR_V3_TABLE_SQL)
    if kind != "table":
        return known_coordinator_index(database, kind, name)
    return name in expected and coordinator_v3_object_matches(database, str(name), expected[str(name)])


def require_rebuildable_schema(
    database: sqlite3.Connection,
    objects: list[tuple[str, str]],
    allow_populated_rebuild: bool,
) -> None:
    if allow_populated_rebuild:
        return
    if _coordinator_schema_populated(database, objects):
        raise coordinator_migration_error(
            "coordinator_v3_source_conflict",
            "fresh coordinator v3 initialization found existing rows",
        )


def _coordinator_schema_populated(
    database: sqlite3.Connection, objects: list[tuple[str, str]]
) -> bool:
    """A table that cannot even be read counts as populated: it is not ours."""
    return any(
        table_has_rows(database, str(name)) for kind, name in objects if kind == "table"
    )


def table_has_rows(database: sqlite3.Connection, name: str) -> bool:
    try:
        return (
            database.execute(f'SELECT 1 FROM {quote_sqlite_identifier(name)} LIMIT 1').fetchone() is not None
        )
    except sqlite3.DatabaseError:
        return True


def _drop_coordinator_objects(
    database: sqlite3.Connection, objects: list[tuple[str, str]]
) -> None:
    database.execute("PRAGMA foreign_keys=OFF")
    try:
        for kind in ("trigger", "index", "view", "table"):
            drop_objects_of_kind(database, objects, kind)
    finally:
        _restore_foreign_keys(database)


def drop_objects_of_kind(
    database: sqlite3.Connection, objects: list[tuple[str, str]], kind: str
) -> None:
    keyword = kind.upper()
    for name in _object_names_of_kind(objects, kind):
        with begin_immediate(database):
            database.execute(f'DROP {keyword} {quote_sqlite_identifier(name)}')


def _object_names_of_kind(objects: list[tuple[str, str]], kind: str) -> list[str]:
    return [str(name) for object_kind, name in reversed(objects) if object_kind == kind]


def _restore_foreign_keys(database: sqlite3.Connection) -> None:
    database.execute("PRAGMA foreign_keys=ON")
    if database.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 candidate did not restore foreign keys",
        )


def _coordinator_v2_project_history(
    source: sqlite3.Connection,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    checkpoints = _coordinator_v2_checkpoint_rows(source)
    attempts = _coordinator_v2_attempt_rows(source)
    if bool(checkpoints) != bool(attempts):
        raise coordinator_migration_error(
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
    if not set(expected).issubset(coordinator_table_columns(source, table)):
        raise coordinator_migration_error(
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
        raise coordinator_migration_error(
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
        raise coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint attempts are not complete",
        )


def _require_attempt_lineage(
    position: int, attempt: tuple[object, ...], history: list[tuple[object, ...]]
) -> None:
    _require_attempt_timestamp(attempt)
    expected_parent = None if position == 0 else history[position - 1][3]
    if attempt[4] != expected_parent or _unquarantined_middle(position, attempt, history):
        raise coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint attempt lineage is inconsistent",
        )


def _require_attempt_timestamp(attempt: tuple[object, ...]) -> None:
    if not isinstance(attempt[9], str) or not attempt[9]:
        raise coordinator_migration_error(
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
        raise coordinator_migration_error(
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
    """Live ownership cannot be canonicalized, so it must not be carried over.

    Only *live* ownership refuses the migration. An expired row is not carried
    over either — none of these three tables is read by the v2 row collector —
    but it is no reason to stop, and treating it as one made adoption
    unreachable on any vault that had ever taken a project lease. A lease row is
    deleted only by the holder that releases it, so an agent that exits without
    releasing leaves the row behind for good: measured on the owner's vault on
    2026-08-26, 56 project leases, 55 of them expired on 2026-08-21, plus one
    released `doctor` maintenance owner. The existence rule refused every one of
    them, and `session_end` had never once completed on that machine. Liveness
    is also the rule the queue side of the same adoption already uses, where
    only a task still in `leased` state refuses.

    Fail closed on doubt: an expiry that is absent or unreadable counts as live,
    because it is not proof that the owner is gone.
    """
    now = datetime.now(UTC)
    if any(_v2_ownership_is_live(source, table, now) for table in _V2_OWNERSHIP_TABLES):
        raise coordinator_migration_error(
            "coordinator_v2_ambiguous_ownership",
            "coordinator v2 contains live ownership that cannot be canonicalized",
        )


def _v2_ownership_is_live(
    source: sqlite3.Connection, table: str, now: datetime
) -> bool:
    if not _coordinator_table_exists(source, table):
        return False
    if "expires_at" not in coordinator_table_columns(source, table):
        return _v2_table_populated(source, table)
    return _any_live_expiry(source, table, now)


def _any_live_expiry(source: sqlite3.Connection, table: str, now: datetime) -> bool:
    rows = source.execute(f'SELECT expires_at FROM "{table}"').fetchall()
    return any(expiry_is_live(row[0], now) for row in rows)


def _v2_table_populated(source: sqlite3.Connection, table: str) -> bool:
    """Without an expiry column there is nothing to date a row by, so any row counts."""
    return source.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None


def expiry_is_live(value: object, now: datetime) -> bool:
    if not isinstance(value, str) or not value:
        return True
    try:
        parsed = parse_stored_timestamp(value)
    except (ValueError, TypeError):
        return True
    return _as_utc(parsed) > now


def _as_utc(value: datetime) -> datetime:
    """A v2 row may carry a naive stamp; every writer of these tables meant UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _coordinator_v2_transaction_rows(
    source: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    columns = coordinator_table_columns(source, "transaction")
    if not set(_COORDINATOR_V2_COLUMNS["transaction"][:8]).issubset(columns):
        raise coordinator_migration_error(
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
        raise coordinator_migration_error(
            "coordinator_v2_transaction_invalid",
            "coordinator v2 transaction state is invalid",
        )


def _coordinator_v2_operation_rows(
    source: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    columns = _COORDINATOR_V2_COLUMNS["operation"]
    if not set(columns).issubset(coordinator_table_columns(source, "operation")):
        raise coordinator_migration_error(
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
        raise coordinator_migration_error(
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
        raise coordinator_migration_error(
            "coordinator_v2_checkpoint_history_incomplete",
            "coordinator v2 checkpoint transaction is missing",
        )


def _coordinator_v2_fence_rows(
    source: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    if not _coordinator_table_exists(source, "writer_fences"):
        return []
    columns = _COORDINATOR_V2_COLUMNS["writer_fences"]
    if not set(columns).issubset(coordinator_table_columns(source, "writer_fences")):
        raise coordinator_migration_error(
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
            coordinator_v3_statements(),
            final_invariant=coordinator_v3_schema_complete,
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
            sqlite_io.open_readonly_operational_db(
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
    if any(coordinator_v3_row_counts(database).values()):
        raise coordinator_migration_error(
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
        or not coordinator_v3_cross_table_invariant(database)
    ):
        raise coordinator_migration_error(
            "coordinator_v3_validation_failed",
            "coordinator v3 candidate validation failed",
        )


def _publish_coordinator_v3_candidate(candidate: Path) -> None:
    with contextlib.closing(
        open_operational_db(
            candidate,
            busy_ms=DEFAULTS.markdown_busy_ms,
            contract=COORDINATOR_V3_CONTRACT,
            initialize_contract=True,
        )
    ):
        pass
    harden_owner_only(candidate, 0o600)
    validate_coordinator_v3_database(candidate, state_root=candidate.parent.parent)
