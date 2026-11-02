"""Queue v2 migration statements and reconciliation on caller-owned connections."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from functools import partial

from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import _MAX_QUEUE_PAYLOAD_BYTES, validate_payload_blob
from llm_wiki.queue_sources import _queue_v3_source_links
from llm_wiki.sqlite_contracts import MigrationStatement, _migration_error, quote_sqlite_identifier


def _queue_v2_tables(database: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in database.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()
    }

_QUEUE_V2_SCHEMA_OBJECTS = {
    ("index", "queue_claim_order"),
    ("table", "attempt_history"),
    ("table", "queue_ownership"),
    ("table", "source_failures"),
    ("table", "source_fences"),
    ("table", "tasks"),
    ("trigger", "attempt_history_immutable_delete"),
    ("trigger", "attempt_history_immutable_update"),
}

def _validate_queue_v2_schema_objects(database: sqlite3.Connection) -> None:
    objects = {
        (str(row[0]), str(row[1]))
        for row in database.execute(
            """SELECT type, name FROM sqlite_schema
               WHERE name NOT LIKE 'sqlite_%'"""
        )
    }
    if not objects <= _QUEUE_V2_SCHEMA_OBJECTS:
        raise _migration_error(
            "queue_v2_schema_unknown",
            "queue v2 source has unknown schema objects",
        )

def _queue_v2_columns(database: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in database.execute(
        f'PRAGMA table_info({quote_sqlite_identifier(table)})'
    )}

def _queue_v3_row_matches(
    database: sqlite3.Connection,
    table: str,
    key_column: str,
    key: object,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> bool:
    selected = ", ".join(f'"{column}"' for column in columns)
    row = database.execute(
        f'SELECT {selected} FROM "{table}" WHERE "{key_column}"=?', (key,)
    ).fetchone()
    return row is not None and tuple(row) == values

def _queue_v3_insert_statement(
    *,
    name: str,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
    key_column: str,
    key: object,
) -> MigrationStatement:
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    return MigrationStatement(
        name=name,
        sql=f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({placeholders})',
        parameters=values,
        completed=lambda database: _queue_v3_row_matches(
            database, table, key_column, key, columns, values
        ),
    )

_QUEUE_V2_REQUIRED_TASK_COLUMNS = {
    "id",
    "kind",
    "handler_version",
    "payload_json",
    "input_hash",
    "dedupe_key",
    "state",
    "priority",
    "created_at",
    "updated_at",
    "available_at",
    "attempts",
    "last_attempt_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "lease_heartbeat_at",
    "attempt_started_at",
    "error_code",
    "blocked_capability",
    "result_reference",
    "result_sha256",
    "result_operation_id",
    "redrive_of",
}

_QUEUE_V3_TASK_COLUMNS = (
    "id",
    "kind",
    "handler_version",
    "payload_blob",
    "input_hash",
    "dedupe_key",
    "state",
    "priority",
    "created_at",
    "updated_at",
    "available_at",
    "attempts",
    "last_attempt_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "lease_heartbeat_at",
    "attempt_started_at",
    "error_code",
    "blocked_capability",
    "result_reference",
    "result_sha256",
    "result_operation_id",
    "redrive_of",
    "lineage_generation",
)

_QUEUE_V2_HISTORY_COLUMNS = (
    "sequence",
    "task_id",
    "attempt",
    "started_at",
    "finished_at",
    "outcome",
    "error_code",
)

_QUEUE_V2_FAILURE_COLUMNS = (
    "logical_path",
    "source_digest",
    "error_code",
    "producer",
    "updated_at",
)

def _require_queue_v2_tasks_schema(
    source: sqlite3.Connection, tables: set[str]
) -> None:
    if "tasks" not in tables:
        raise _migration_error(
            "queue_v2_schema_incomplete", "queue v2 source has no tasks table"
        )
    if not _QUEUE_V2_REQUIRED_TASK_COLUMNS <= _queue_v2_columns(source, "tasks"):
        raise _migration_error(
            "queue_v2_schema_incomplete", "queue v2 tasks schema is incomplete"
        )

def _queue_v2_child_counts(
    task_rows: list, rows_by_id: dict[str, sqlite3.Row]
) -> dict[str, int]:
    child_counts = {task_id: 0 for task_id in rows_by_id}
    for row in task_rows:
        parent = row["redrive_of"]
        if parent is None:
            continue
        if str(parent) not in rows_by_id:
            raise _migration_error(
                "queue_v2_lineage_ambiguous", "queue v2 redrive parent is missing"
            )
        child_counts[str(parent)] += 1
    return child_counts

def _already_ordered(task_id: str, visited: set[str], visiting: set[str]) -> bool:
    """Whether this task is placed; meeting it twice on one path is a cycle."""
    if task_id in visited:
        return True
    if task_id in visiting:
        raise _migration_error(
            "queue_v2_lineage_ambiguous", "queue v2 redrive lineage is cyclic"
        )
    return False

def _parents_before_children(rows_by_id: dict[str, sqlite3.Row]) -> list:
    """Redrive parents must be inserted before the tasks that name them.

    The walk carries its own stack: a redrive chain is as long as whatever the
    pre-adoption queue accumulated, and Python's recursion limit is not a bound
    this migration chose. See
    `docs/research/2026-09-18-a-refusal-is-cheaper-than-a-crash.md`.
    """
    ordered_rows: list[sqlite3.Row] = []
    visited: set[str] = set()
    visiting: set[str] = set()
    for task_id in sorted(rows_by_id):
        _place_with_parents(task_id, rows_by_id, ordered_rows, visited, visiting)
    return ordered_rows

def _place_with_parents(
    task_id: str,
    rows_by_id: dict[str, sqlite3.Row],
    ordered_rows: list,
    visited: set[str],
    visiting: set[str],
) -> None:
    """Place this task after every parent it names, deepest parent first."""
    chain = _redrive_chain(task_id, rows_by_id, visited, visiting)
    for placed in reversed(chain):
        visiting.discard(placed)
        visited.add(placed)
        ordered_rows.append(rows_by_id[placed])

def _redrive_chain(
    task_id: str,
    rows_by_id: dict[str, sqlite3.Row],
    visited: set[str],
    visiting: set[str],
) -> list[str]:
    """This task and every unplaced parent above it, child first."""
    chain: list[str] = []
    current: str | None = task_id
    while current is not None and not _already_ordered(current, visited, visiting):
        visiting.add(current)
        chain.append(current)
        parent = rows_by_id[current]["redrive_of"]
        current = None if parent is None else str(parent)
    return chain

def _ordered_queue_v2_tasks(source: sqlite3.Connection) -> tuple[list, dict[str, int]]:
    task_rows = list(source.execute("SELECT * FROM tasks ORDER BY id"))
    rows_by_id = {str(row["id"]): row for row in task_rows}
    if len(rows_by_id) != len(task_rows):
        raise _migration_error(
            "queue_v2_task_identity_conflict", "queue v2 task IDs are not unique"
        )
    child_counts = _queue_v2_child_counts(task_rows, rows_by_id)
    return _parents_before_children(rows_by_id), child_counts

def _migrated_payload_bytes(row: sqlite3.Row) -> bytes:
    payload_text = row["payload_json"]
    if not isinstance(payload_text, str):
        raise _migration_error(
            "queue_v2_payload_not_text", "queue v2 payload is not TEXT"
        )
    try:
        payload_bytes = payload_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _migration_error(
            "queue_v2_payload_not_utf8", "queue v2 payload is not UTF-8"
        ) from exc
    if len(payload_bytes) > _MAX_QUEUE_PAYLOAD_BYTES:
        raise _migration_error(
            "queue_v2_payload_too_large", "queue v2 payload exceeds 1 MiB"
        )
    return payload_bytes

def _migrated_attempts(row: sqlite3.Row) -> int:
    attempts = row["attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise _migration_error(
            "queue_v2_attempts_out_of_range", "queue v2 attempts exceed v3 bounds"
        )
    if not 0 <= attempts <= 100:
        raise _migration_error(
            "queue_v2_attempts_out_of_range", "queue v2 attempts exceed v3 bounds"
        )
    return attempts

def _migrated_task_values(
    row: sqlite3.Row,
    payload_bytes: bytes,
    attempts: int,
    child_counts: dict[str, int],
    mismatch: bool,
) -> tuple:
    state = row["state"]
    error_code = row["error_code"]
    if mismatch:
        state = "dead"
        error_code = "payload_hash_mismatch"
    return (
        row["id"],
        row["kind"],
        row["handler_version"],
        payload_bytes,
        row["input_hash"],
        row["dedupe_key"],
        state,
        row["priority"],
        row["created_at"],
        row["updated_at"],
        row["available_at"],
        attempts,
        row["last_attempt_at"],
        row["lease_owner"],
        row["lease_token"],
        row["lease_expires_at"],
        row["lease_heartbeat_at"],
        row["attempt_started_at"],
        error_code,
        row["blocked_capability"],
        row["result_reference"],
        row["result_sha256"],
        row["result_operation_id"],
        row["redrive_of"],
        child_counts[str(row["id"])],
    )

def _task_migration_statements(
    ordered_rows: list,
    child_counts: dict[str, int],
    statements: list[MigrationStatement],
    summary: dict[str, int],
) -> list[tuple[str, str, str]]:
    """Task inserts in lineage order; returns the source links they imply."""
    task_links: list[tuple[str, str, str]] = []
    for row in ordered_rows:
        payload_bytes = _migrated_payload_bytes(row)
        attempts = _migrated_attempts(row)
        mismatch = (
            validate_payload_blob(
                payload_bytes, row["input_hash"], parse=True
            ).code
            is not None
        )
        task_id = str(row["id"])
        statements.append(
            _queue_v3_insert_statement(
                name=f"migrate_task_{sha256_bytes(task_id.encode('utf-8'))[:16]}",
                table="tasks",
                columns=_QUEUE_V3_TASK_COLUMNS,
                values=_migrated_task_values(
                    row, payload_bytes, attempts, child_counts, mismatch
                ),
                key_column="id",
                key=row["id"],
            )
        )
        summary["tasks"] += 1
        if mismatch:
            summary["payload_hash_mismatches"] += 1
            continue
        task_links.extend(
            (task_id, logical_path, source_digest)
            for logical_path, source_digest in _queue_v3_source_links(payload_bytes)
        )
    return task_links

def _attempt_history_statements(
    source: sqlite3.Connection,
    tables: set[str],
    statements: list[MigrationStatement],
    summary: dict[str, int],
) -> None:
    if "attempt_history" not in tables:
        return
    if set(_QUEUE_V2_HISTORY_COLUMNS) != _queue_v2_columns(source, "attempt_history"):
        raise _migration_error(
            "queue_v2_schema_incomplete",
            "queue v2 attempt history schema is incomplete",
        )
    for row in source.execute("SELECT * FROM attempt_history ORDER BY sequence"):
        statements.append(
            _queue_v3_insert_statement(
                name=f"migrate_attempt_{int(row['sequence'])}",
                table="attempt_history",
                columns=_QUEUE_V2_HISTORY_COLUMNS,
                values=tuple(row[column] for column in _QUEUE_V2_HISTORY_COLUMNS),
                key_column="sequence",
                key=row["sequence"],
            )
        )
        summary["attempt_history"] += 1

def _task_source_link_statements(
    task_links: list[tuple[str, str, str]],
    statements: list[MigrationStatement],
    summary: dict[str, int],
) -> None:
    for task_id, logical_path, source_digest in sorted(task_links):
        values = (task_id, logical_path, source_digest)
        identity = sha256_bytes(canonical_json_bytes(list(values)))[:16]
        statements.append(
            MigrationStatement(
                name=f"migrate_task_source_link_{identity}",
                sql="""INSERT OR IGNORE INTO task_source_links(
                    task_id, logical_path, source_digest
                ) VALUES (?, ?, ?)""",
                parameters=values,
                completed=lambda database, expected=values: database.execute(
                    """SELECT 1 FROM task_source_links
                       WHERE task_id=? AND logical_path=? AND source_digest=?""",
                    expected,
                ).fetchone()
                is not None,
            )
        )
        summary["task_source_links"] += 1

def _source_failure_migrated(
    database: sqlite3.Connection, expected: tuple[object, ...]
) -> bool:
    """Whether the migrated row carries exactly the values it was given."""
    row = database.execute(
        """SELECT error_code, producer, updated_at FROM source_failures
           WHERE logical_path=? AND source_digest=?""",
        expected[:2],
    ).fetchone()
    return tuple(row or ()) == tuple(expected[2:])

def _source_failure_statement(values: tuple[object, ...]) -> MigrationStatement:
    """The migration statement that carries one source failure row across."""
    identity = sha256_bytes(canonical_json_bytes(list(values[:2])))[:16]
    return MigrationStatement(
        name=f"migrate_source_failure_{identity}",
        sql="""INSERT OR IGNORE INTO source_failures(
            logical_path, source_digest, error_code, producer, updated_at
        ) VALUES (?, ?, ?, ?, ?)""",
        parameters=values,
        completed=partial(_source_failure_migrated, expected=values),
    )

def _source_failure_statements(
    source: sqlite3.Connection,
    tables: set[str],
    statements: list[MigrationStatement],
    summary: dict[str, int],
) -> None:
    if "source_failures" not in tables:
        return
    if set(_QUEUE_V2_FAILURE_COLUMNS) != _queue_v2_columns(source, "source_failures"):
        raise _migration_error(
            "queue_v2_schema_incomplete",
            "queue v2 source failure schema is incomplete",
        )
    for row in source.execute(
        "SELECT * FROM source_failures ORDER BY logical_path, source_digest"
    ):
        values = tuple(row[column] for column in _QUEUE_V2_FAILURE_COLUMNS)
        statements.append(_source_failure_statement(values))
        summary["source_failures"] += 1

def _queue_v2_migration_statements(
    source: sqlite3.Connection,
    *,
    check_owners: Callable[[sqlite3.Connection, set[str]], None],
) -> tuple[tuple[MigrationStatement, ...], dict[str, int]]:
    _validate_queue_v2_schema_objects(source)
    tables = _queue_v2_tables(source)
    _require_queue_v2_tasks_schema(source, tables)
    check_owners(source, tables)
    statements: list[MigrationStatement] = []
    summary = {
        "attempt_history": 0,
        "payload_hash_mismatches": 0,
        "source_failures": 0,
        "source_fences": 0,
        "task_source_links": 0,
        "tasks": 0,
    }
    ordered_rows, child_counts = _ordered_queue_v2_tasks(source)
    task_links = _task_migration_statements(
        ordered_rows, child_counts, statements, summary
    )
    _attempt_history_statements(source, tables, statements, summary)
    _task_source_link_statements(task_links, statements, summary)
    _source_failure_statements(source, tables, statements, summary)
    return tuple(statements), summary

_QUEUE_V2_MIGRATED_TABLES = (
    "attempt_history",
    "source_failures",
    "source_fences",
    "task_source_links",
    "tasks",
)

_QUEUE_V3_ONLY_TABLES = (
    "capture_intents",
    "capture_task_link_resolutions",
    "capture_task_link_seals",
    "capture_task_links",
    "corrupt_dispositions",
    "corrupt_export_operations",
    "corrupt_export_pages",
    "corrupt_package_supersession_operations",
    "corrupt_package_supersession_pages",
    "corrupt_package_supersessions",
    "corrupt_purge_operations",
    "corrupt_purge_pages",
    "queue_ownership",
    "semantic_decisions",
    "task_fence_epochs",
    "task_fences",
    "task_purge_authorizations",
)

def _tables_have_counts(
    database: sqlite3.Connection, expected: Mapping[str, int]
) -> bool:
    """Whether every named table holds exactly the number of rows expected."""
    return all(
        database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == count
        for table, count in expected.items()
    )

def _queue_v2_reconciliation_complete(
    database: sqlite3.Connection,
    statements: tuple[MigrationStatement, ...],
    summary: Mapping[str, int],
) -> bool:
    if not all(statement.completed(database) for statement in statements):
        return False
    migrated = {table: summary[table] for table in _QUEUE_V2_MIGRATED_TABLES}
    if not _tables_have_counts(database, migrated):
        return False
    return _tables_have_counts(database, dict.fromkeys(_QUEUE_V3_ONLY_TABLES, 0))
