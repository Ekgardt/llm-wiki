"""Sqlite pragmas with no connection or filesystem ownership."""
from __future__ import annotations

import sqlite3

from llm_wiki.sqlite_contracts import OperationalDatabaseContract, OperationalDatabaseContractError


def _apply_operational_pragmas(connection: sqlite3.Connection, busy_ms: int) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={busy_ms:d}")
    mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    if str(mode).casefold() != "delete":
        raise sqlite3.OperationalError(f"SQLite refused journal_mode=DELETE: {mode}")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    _require_pragma(connection, "synchronous", 2)
    _require_pragma(connection, "foreign_keys", 1)
    _require_pragma(connection, "trusted_schema", 0)
    _require_pragma(connection, "busy_timeout", busy_ms)

def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise OperationalDatabaseContractError(
            f"operational database PRAGMA {name} returned an invalid value"
        )
    return row[0]

def _require_pragma(
    connection: sqlite3.Connection,
    name: str,
    expected: int,
) -> None:
    actual = _pragma_integer(connection, name)
    if actual != expected:
        raise OperationalDatabaseContractError(
            f"operational database PRAGMA {name} mismatch: expected {expected}, got {actual}"
        )

def _validate_or_initialize_operational_contract(
    connection: sqlite3.Connection,
    contract: OperationalDatabaseContract,
    *,
    initialize: bool,
) -> None:
    identity = (
        _pragma_integer(connection, "application_id"),
        _pragma_integer(connection, "user_version"),
    )
    if initialize:
        identity = _initialized_contract_identity(connection, contract, identity)
    if identity != (contract.application_id, contract.user_version):
        raise OperationalDatabaseContractError(
            "operational database application_id or user_version mismatch"
        )

def _initialized_contract_identity(
    connection: sqlite3.Connection,
    contract: OperationalDatabaseContract,
    identity: tuple[int, int],
) -> tuple[int, int]:
    """Stamp an empty database with the contract; refuse one stamped otherwise."""
    if identity == (0, 0):
        connection.execute(f"PRAGMA application_id={contract.application_id:d}")
        connection.execute(f"PRAGMA user_version={contract.user_version:d}")
        return (
            _pragma_integer(connection, "application_id"),
            _pragma_integer(connection, "user_version"),
        )
    if identity != (contract.application_id, contract.user_version):
        raise OperationalDatabaseContractError(
            "cannot initialize a conflicting operational database contract"
        )
    return identity

def _apply_readonly_operational_pragmas(
    database: sqlite3.Connection, busy_ms: int
) -> None:
    database.row_factory = sqlite3.Row
    database.execute(f"PRAGMA busy_timeout={busy_ms:d}")
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("PRAGMA trusted_schema=OFF")
    database.execute("PRAGMA query_only=ON")
    mode = database.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).casefold() != "delete":
        raise OperationalDatabaseContractError(
            f"operational database journal_mode mismatch: expected delete, got {mode}"
        )
    _require_pragma(database, "synchronous", 2)
    _require_pragma(database, "foreign_keys", 1)
    _require_pragma(database, "trusted_schema", 0)
    _require_pragma(database, "busy_timeout", busy_ms)
