"""Schema inspection and repair never reinterpret a stored name as SQL."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import markdown_transaction as transactions
import memory_queue as queue
import pytest
from reliable_memory import OperationalDatabaseContractError


def _quoted(database, name):
    return database.execute("SELECT printf('\"%w\"', ?)", (name,)).fetchone()[0]


def _tables(database, name):
    database.execute("CREATE TABLE safe (value TEXT)")
    identifier = _quoted(database, name)
    database.execute(f"CREATE TABLE {identifier} (value TEXT)")
    database.execute(f"INSERT INTO {identifier} VALUES (?)", ("retained evidence",))


@pytest.mark.parametrize("probe", [transactions._table_has_rows, queue._table_is_populated])
def test_populated_schema_name_cannot_redirect_probe(probe):
    name = 'safe" --'
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as database:
        _tables(database, name)
        assert probe(database, name) is True
        assert probe(database, "safe") is False


@pytest.mark.parametrize("owner", ["coordinator", "queue"])
def test_drop_removes_only_the_exact_schema_name(owner):
    name = 'safe" --'
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as database:
        _tables(database, name)
        _drop(database, owner, name)
        names = {row[0] for row in database.execute("SELECT name FROM sqlite_schema")}
        assert "safe" in names
        assert name not in names


def _drop(database, owner, name):
    if owner == "coordinator":
        transactions._drop_objects_of_kind(database, [("table", name)], "table")
        return
    queue._drop_schema_object(database, "table", name)


@pytest.mark.parametrize("owner", ["coordinator", "queue"])
def test_populated_candidate_still_blocks_rebuild(owner):
    name = 'safe" --'
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as database:
        _tables(database, name)
        with pytest.raises(OperationalDatabaseContractError, match="populated|existing rows"):
            _require_empty(database, owner, name)
        assert database.execute(f"SELECT value FROM {_quoted(database, name)}").fetchone()[0] == (
            "retained evidence"
        )


def _require_empty(database, owner, name):
    objects = [("table", "safe"), ("table", name)]
    if owner == "coordinator":
        transactions._require_rebuildable_schema(database, objects, False)
        return
    queue._check_no_populated_rebuild(database, objects, allow_populated_rebuild=False)


@pytest.mark.parametrize("reader", [
    transactions._coordinator_table_columns, transactions._table_column_names,
    queue._queue_v2_columns, queue._table_columns,
])
@pytest.mark.parametrize("name", ['quoted"table', "with space", "select"])
def test_schema_columns_accept_exact_sqlite_identifiers(reader, name):
    with closing(sqlite3.connect(":memory:")) as database:
        database.row_factory = sqlite3.Row
        database.execute(f"CREATE TABLE {_quoted(database, name)} (value TEXT)")
        assert set(reader(database, name)) == {"value"}
