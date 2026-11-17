"""Historical SQLite declarations and caller-owned transactions survive extraction."""
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest
from llm_wiki import coordinator_schema as schema

ORIGINAL = json.loads((Path(__file__).with_name('original-contract.json')).read_text(encoding='utf-8'))


def _database():
    database = sqlite3.connect(':memory:')
    for _name, sql in schema.COORDINATOR_V3_TABLE_SQL:
        database.execute(sql)
    return database


def test_historical_declarations_and_hash_are_exact():
    assert asdict(schema.COORDINATOR_V3_CONTRACT) == ORIGINAL['contract']
    assert [list(pair) for pair in schema.COORDINATOR_V3_TABLE_SQL] == ORIGINAL['tables']
    assert [list(pair) for pair in schema.COORDINATOR_V3_INDEX_SQL] == ORIGINAL['indexes']
    assert schema.COORDINATOR_V3_SCHEMA_SHA256 == ORIGINAL['schema_sha256']


def test_older_schema_without_optional_indexes_is_complete():
    with closing(_database()) as database:
        assert schema.coordinator_v3_schema_complete(database)
        assert schema.schema_objects(database) == schema.COORDINATOR_V3_TABLES


def test_optional_indexes_are_idempotent_and_rollback_with_caller():
    with closing(_database()) as database:
        database.execute('BEGIN')
        schema.ensure_coordinator_v3_indexes(database)
        schema.ensure_coordinator_v3_indexes(database)
        assert database.in_transaction
        assert schema.coordinator_v3_schema_complete(database)
        assert schema.schema_objects(database) - schema.COORDINATOR_V3_TABLES == {
            ('index', name) for name in schema.COORDINATOR_V3_INDEXES
        }
        database.rollback()
        assert schema.schema_objects(database) == schema.COORDINATOR_V3_TABLES
        assert schema.coordinator_v3_schema_complete(database)


@pytest.mark.parametrize('sql', [
    'CREATE TABLE stranger(value TEXT)',
    'CREATE INDEX stranger ON operation(path)',
    'CREATE VIEW stranger AS SELECT * FROM operation',
    'CREATE TRIGGER stranger AFTER INSERT ON operation BEGIN SELECT 1; END',
    'DROP TABLE writer_fences',
    'ALTER TABLE writer_fences ADD COLUMN stranger TEXT',
    'CREATE INDEX project_checkpoints_transaction ON project_checkpoints(project)',
])
def test_unknown_missing_or_changed_objects_are_refused(sql):
    with closing(_database()) as database:
        database.execute(sql)
        assert not schema.coordinator_v3_schema_complete(database)


def test_each_migration_callback_binds_its_own_table():
    with closing(sqlite3.connect(':memory:')) as database:
        statements = schema.coordinator_v3_statements()
        database.execute('BEGIN')
        for offset, statement in enumerate(statements):
            assert not statement.completed(database)
            database.execute(statement.sql)
            assert statement.completed(database)
            assert sum(item.completed(database) for item in statements) == offset + 1
        assert database.in_transaction
        assert schema.coordinator_v3_schema_complete(database)
        database.rollback()
        assert schema.schema_objects(database) == set()
