"""Operational SQLite value contracts, independent of runtime IO."""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


def quote_sqlite_identifier(name: str) -> str:
    """Encode one exact SQLite identifier, including its embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def _require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_migration_text(name: object, sql: object) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("migration statement name must be non-empty")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("migration statement SQL must be non-empty")


class OperationalDatabaseContractError(sqlite3.DatabaseError):
    """Raised when an operational database or migration violates its contract."""

    code = "operational_database_contract_mismatch"


@dataclass(frozen=True)
class OperationalDatabaseContract:
    application_id: int
    user_version: int = 3

    def __post_init__(self) -> None:
        _require_positive_int("application_id", self.application_id)
        _require_positive_int("user_version", self.user_version)


@dataclass(frozen=True)
class MigrationStatement:
    name: str
    sql: str
    completed: Callable[[sqlite3.Connection], bool] = field(compare=False, repr=False)
    parameters: Sequence[object] = ()

    def __post_init__(self) -> None:
        _require_migration_text(self.name, self.sql)
        if not callable(self.completed):
            raise TypeError("migration statement completed invariant must be callable")

# Preserve historical pickle lookup paths; reliable_memory re-exports these objects.
OperationalDatabaseContract.__module__ = "reliable_memory"
OperationalDatabaseContractError.__module__ = "reliable_memory"
MigrationStatement.__module__ = "reliable_memory"


def _migration_error(code: str, message: str) -> OperationalDatabaseContractError:
    error = OperationalDatabaseContractError(message)
    error.code = code
    return error
