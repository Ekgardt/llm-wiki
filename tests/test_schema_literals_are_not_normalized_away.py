"""Managed-schema comparison must reject changes to SQL values and identities."""
import sqlite3
from contextlib import closing

import markdown_transaction as coordinator
import pytest
from llm_wiki import queue_schema, queue_schema_operations


@pytest.mark.parametrize(('before', 'after'), [
    ("'preparing'", "'PREPARING'"),
    ('state', 'ſtate'),
    ('operation_id TEXT', 'operation_id\u00a0TEXT'),
])
def test_coordinator_rejects_semantically_changed_schema(before, after):
    with closing(sqlite3.connect(':memory:')) as database:
        for name, sql in coordinator._COORDINATOR_V3_TABLE_SQL:
            if name == 'transaction':
                changed = sql.replace(before, after)
                assert changed != sql
                sql = changed
            database.execute(sql)
        assert not coordinator._coordinator_v3_schema_complete(database)


def test_queue_rejects_changed_ready_constraint():
    with closing(sqlite3.connect(':memory:')) as database:
        for name, sql in queue_schema._QUEUE_V3_TABLE_SQL:
            if name == 'tasks':
                changed = sql.replace("'ready'", "'READY'")
                assert changed != sql
                sql = changed
            database.execute(sql)
        for _, sql in queue_schema._QUEUE_V3_INDEX_SQL + queue_schema._QUEUE_V3_TRIGGER_SQL:
            database.execute(sql)
        assert not queue_schema_operations._queue_v3_schema_complete(database)
