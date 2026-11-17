"""Sqlite migrations on caller-owned connections."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

from llm_wiki.sqlite_contracts import MigrationStatement, OperationalDatabaseContractError
from llm_wiki.sqlite_transactions import begin_immediate


def migration_incomplete(message: str) -> OperationalDatabaseContractError:
    error = OperationalDatabaseContractError(message)
    error.code = "operational_migration_incomplete"
    return error

def require_unique_statement_names(statements: Sequence[MigrationStatement]) -> None:
    names = [statement.name for statement in statements]
    if any(not name for name in names):
        raise ValueError("migration statement name must be non-empty")
    if len(names) != len(set(names)):
        raise ValueError("migration statement names must be unique")

def apply_one_migration_statement(
    connection: sqlite3.Connection, statement: MigrationStatement, emit
) -> None:
    """One statement in one transaction, skipped when its invariant already holds."""
    if statement.completed(connection):
        return
    emit(f"before:{statement.name}")
    with begin_immediate(connection):
        connection.execute(statement.sql, tuple(statement.parameters))
        if not statement.completed(connection):
            raise migration_incomplete(
                f"migration statement invariant is incomplete: {statement.name}"
            )
        emit(f"after_execute:{statement.name}")
    emit(f"after_commit:{statement.name}")

def require_final_invariant(connection: sqlite3.Connection, final_invariant) -> None:
    if final_invariant(connection):
        return
    raise migration_incomplete("operational migration final invariant is incomplete")

def run_resumable_migration(
    connection: sqlite3.Connection,
    statements: Sequence[MigrationStatement],
    *,
    final_invariant: Callable[[sqlite3.Connection], bool],
    killpoint: Callable[[str], None] | None = None,
) -> None:
    """Apply incomplete migration statements one transaction at a time."""
    require_unique_statement_names(statements)
    emit = killpoint or (lambda _event: None)
    for statement in statements:
        apply_one_migration_statement(connection, statement, emit)
    require_final_invariant(connection, final_invariant)

# Preserve the historical public pickle/import identity without importing IO.
run_resumable_migration.__module__ = "reliable_memory"
