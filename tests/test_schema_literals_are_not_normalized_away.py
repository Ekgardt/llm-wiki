"""Managed-schema comparison must reject changes to SQL values and identities."""
import sqlite3
from contextlib import closing

import markdown_transaction as coordinator
import pytest
from llm_wiki import queue_schema, queue_schema_operations


def _changed_table_sql(name, sql, changed_table, before, after):
    if name != changed_table:
        return sql
    changed = sql.replace(before, after)
    assert changed != sql
    return changed


@pytest.mark.parametrize(('before', 'after'), [
    ("'preparing'", "'PREPARING'"),
    ('state', 'ſtate'),
    ('operation_id TEXT', 'operation_id\u00a0TEXT'),
])
def test_coordinator_rejects_semantically_changed_schema(before, after):
    with closing(sqlite3.connect(':memory:')) as database:
        for name, sql in coordinator.COORDINATOR_V3_TABLE_SQL:
            database.execute(_changed_table_sql(name, sql, 'transaction', before, after))
        assert not coordinator.coordinator_v3_schema_complete(database)


def test_queue_rejects_changed_ready_constraint():
    with closing(sqlite3.connect(':memory:')) as database:
        for name, sql in queue_schema.QUEUE_V3_TABLE_SQL:
            database.execute(_changed_table_sql(name, sql, 'tasks', "'ready'", "'READY'"))
        for _, sql in queue_schema.QUEUE_V3_INDEX_SQL + queue_schema.QUEUE_V3_TRIGGER_SQL:
            database.execute(sql)
        assert not queue_schema_operations.queue_v3_schema_complete(database)


def test_coordinator_schema_facade_preserves_object_identity():
    import markdown_transaction as coordinator
    from llm_wiki import coordinator_schema as schema

    names = (
        'COORDINATOR_V3_CONTRACT', 'COORDINATOR_V3_TABLE_SQL',
        'COORDINATOR_V3_SCHEMA_SHA256', 'COORDINATOR_V3_INDEX_SQL',
        'COORDINATOR_V3_INDEXES', 'COORDINATOR_V3_TABLES',
        'normalized_coordinator_sql', 'coordinator_v3_object_matches',
        'known_coordinator_index', 'ensure_coordinator_v3_indexes',
        'schema_objects', 'coordinator_v3_tables_match', 'tables_of',
        'only_known_indexes', 'coordinator_v3_schema_complete', 'coordinator_v3_statements',
    )
    for name in names:
        assert getattr(coordinator, name) is getattr(schema, name)
