"""Queue schema inspection and explicit repair on caller-owned connections."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from llm_wiki.queue_schema import _QUEUE_V3_INDEX_SQL, _QUEUE_V3_TABLE_SQL, _QUEUE_V3_TRIGGER_SQL
from llm_wiki.sqlite_contracts import MigrationStatement, _migration_error, quote_sqlite_identifier
from llm_wiki.sqlite_schema_text import normalized_schema_sql
from llm_wiki.sqlite_transactions import begin_immediate


def _normalized_sql(value: str | None) -> str:
    return normalized_schema_sql(value)


def _queue_v3_object_matches(
    database: sqlite3.Connection, kind: str, name: str, sql: str
) -> bool:
    row = database.execute(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return row is not None and _normalized_sql(row[0]) == _normalized_sql(sql)


def _queue_v3_statements(
    *, include_triggers: bool = True
) -> tuple[MigrationStatement, ...]:
    statements: list[MigrationStatement] = []
    definitions_by_kind = [
        ("table", _QUEUE_V3_TABLE_SQL),
        ("index", _QUEUE_V3_INDEX_SQL),
    ]
    if include_triggers:
        definitions_by_kind.append(("trigger", _QUEUE_V3_TRIGGER_SQL))
    for kind, definitions in definitions_by_kind:
        for name, sql in definitions:
            statements.append(
                MigrationStatement(
                    name=f"create_{name}",
                    sql=sql,
                    completed=lambda database, expected_kind=kind, expected_name=name, expected_sql=sql: (
                        _queue_v3_object_matches(
                            database, expected_kind, expected_name, expected_sql
                        )
                    ),
                )
            )
    return tuple(statements)


def _queue_v3_schema_complete(
    database: sqlite3.Connection, *, include_triggers: bool = True
) -> bool:
    definitions_by_kind = [
        ("table", _QUEUE_V3_TABLE_SQL),
        ("index", _QUEUE_V3_INDEX_SQL),
    ]
    if include_triggers:
        definitions_by_kind.append(("trigger", _QUEUE_V3_TRIGGER_SQL))
    return all(
        _queue_v3_object_matches(database, kind, name, sql)
        for kind, definitions in definitions_by_kind
        for name, sql in definitions
    )


_QUEUE_V3_DROP_ORDER = {"trigger": 0, "index": 1, "table": 2}


_QUEUE_V3_DROP_KEYWORD = {"index": "INDEX", "table": "TABLE", "trigger": "TRIGGER"}


def _expected_queue_v3_schema() -> dict[tuple[str, str], str]:
    """Every schema object a complete queue v3 candidate is made of."""
    return {
        (kind, name): sql
        for kind, definitions in (
            ("table", _QUEUE_V3_TABLE_SQL),
            ("index", _QUEUE_V3_INDEX_SQL),
            ("trigger", _QUEUE_V3_TRIGGER_SQL),
        )
        for name, sql in definitions
    }


def _queue_schema_rows(database: sqlite3.Connection) -> list[sqlite3.Row]:
    return database.execute(
        """SELECT type, name, sql FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
    ).fetchall()


def _drop_order_key(item: tuple[str, str]) -> tuple[int, str]:
    """Triggers before indexes before tables, so a drop never dangles."""
    return _QUEUE_V3_DROP_ORDER.get(item[0], 3), item[1]


def _drop_schema_object(
    database: sqlite3.Connection, kind: str, name: str
) -> None:
    """Drop one schema object, refusing a kind this migration never creates."""
    keyword = _QUEUE_V3_DROP_KEYWORD.get(kind)
    if keyword is None:
        raise _migration_error(
            "queue_v3_schema_conflict",
            "queue v3 candidate has an unsupported schema object",
        )
    with begin_immediate(database):
        database.execute(f'DROP {keyword} {quote_sqlite_identifier(name)}')


def _drop_unexpected_objects(
    database: sqlite3.Connection,
    unexpected: list[tuple[str, str]],
    *,
    allow_populated_rebuild: bool,
) -> None:
    """Objects we never create must go before the candidate can be trusted."""
    if not allow_populated_rebuild:
        raise _migration_error(
            "queue_v3_schema_conflict",
            "queue v3 candidate has unexpected schema objects",
        )
    for kind, name in sorted(unexpected, key=_drop_order_key):
        _drop_schema_object(database, kind, name)


def _table_is_populated(database: sqlite3.Connection, name: str) -> bool:
    """An unreadable table counts as populated; we must not rebuild it blind."""
    try:
        row = database.execute(f'SELECT 1 FROM {quote_sqlite_identifier(name)} LIMIT 1').fetchone()
    except sqlite3.DatabaseError:
        return True
    return row is not None


def _check_no_populated_rebuild(
    database: sqlite3.Connection,
    malformed: list[tuple[str, str]],
    *,
    allow_populated_rebuild: bool,
) -> None:
    """Refuse to rebuild a partial table that still holds rows."""
    if allow_populated_rebuild:
        return
    for kind, name in malformed:
        if kind == "table" and _table_is_populated(database, name):
            raise _migration_error(
                "queue_v3_schema_conflict",
                "queue v3 candidate has a populated partial table",
            )


def _malformed_queue_v3_objects(
    database: sqlite3.Connection,
    existing: list[sqlite3.Row],
    expected: Mapping[tuple[str, str], str],
) -> list[tuple[str, str]]:
    """Objects whose definition differs from the one this version creates."""
    return [
        (str(row["type"]), str(row["name"]))
        for row in existing
        if not _queue_v3_object_matches(
            database,
            str(row["type"]),
            str(row["name"]),
            expected[(str(row["type"]), str(row["name"]))],
        )
    ]


def _repair_partial_queue_v3_schema(
    database: sqlite3.Connection, *, allow_populated_rebuild: bool
) -> None:
    expected = _expected_queue_v3_schema()
    existing = _queue_schema_rows(database)
    unexpected = [
        (str(row["type"]), str(row["name"]))
        for row in existing
        if (str(row["type"]), str(row["name"])) not in expected
    ]
    if unexpected:
        _drop_unexpected_objects(
            database, unexpected, allow_populated_rebuild=allow_populated_rebuild
        )
        existing = _queue_schema_rows(database)
    malformed = _malformed_queue_v3_objects(database, existing, expected)
    _check_no_populated_rebuild(
        database, malformed, allow_populated_rebuild=allow_populated_rebuild
    )
    for kind, name in sorted(malformed, key=_drop_order_key):
        _drop_schema_object(database, kind, name)


def _queue_v3_trigger_statements() -> tuple[MigrationStatement, ...]:
    """Only the statements that create the v3 triggers."""
    trigger_names = {name for name, _sql in _QUEUE_V3_TRIGGER_SQL}
    return tuple(
        statement
        for statement in _queue_v3_statements()
        if statement.name.startswith("create_")
        and statement.name.removeprefix("create_") in trigger_names
    )

