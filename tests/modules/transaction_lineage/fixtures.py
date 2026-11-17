"""Original shared SQLite fixtures and checkpoint identities."""
from __future__ import annotations

import sqlite3

DIGEST = "399559fff7d1f24b8a7eb42319b9f52370048f12df81813c14caf9f57c548e41"
REFUSED = f"project:llm-wiki:716:attempt:1:epoch:41:{DIGEST}"
COMMITTED = f"project:llm-wiki:716:attempt:2:epoch:42:{DIGEST}"


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        'CREATE TABLE "transaction" ('
        "id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, "
        "state TEXT NOT NULL, parent_transaction_id TEXT)"
    )
    connection.execute(
        'CREATE TABLE "operation" ('
        "transaction_id TEXT NOT NULL, position INTEGER NOT NULL, "
        "kind TEXT NOT NULL, path TEXT NOT NULL)"
    )
    return connection


def _add(
    database: sqlite3.Connection,
    identifier: str,
    operation_id: str,
    state: str,
    operations: tuple[tuple[str, str], ...] = (),
    parent: str | None = None,
) -> None:
    database.execute(
        'INSERT INTO "transaction" (id, operation_id, state, parent_transaction_id) '
        "VALUES (?, ?, ?, ?)",
        (identifier, operation_id, state, parent),
    )
    for position, (kind, path) in enumerate(operations):
        database.execute(
            'INSERT INTO "operation" (transaction_id, position, kind, path) '
            "VALUES (?, ?, ?, ?)",
            (identifier, position, kind, path),
        )
