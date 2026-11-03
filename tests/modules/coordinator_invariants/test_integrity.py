"""Original corruption regressions; local legacy alias preserves their ASTs."""
import contextlib
import sqlite3

import pytest
from llm_wiki import coordinator_invariants as markdown_transaction


def test_coordinator_integrity_reports_a_damaged_index_separately():
    """An absent index entry must not be reported as an ownership violation."""
    with contextlib.closing(sqlite3.connect(":memory:")) as database:
        database.executescript(
            "CREATE TABLE sample(value INTEGER);"
            "CREATE INDEX sample_idx ON sample(value);"
            "INSERT INTO sample VALUES(1);"
        )
        index = database.execute(
            "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master WHERE name='sample_idx'"
        ).fetchone()
        database.executescript(
            "PRAGMA writable_schema=ON;"
            "DELETE FROM sqlite_master WHERE name='sample_idx';"
            "PRAGMA schema_version=3;"
            "INSERT INTO sample VALUES(2);"
        )
        database.execute("INSERT INTO sqlite_master VALUES(?,?,?,?,?)", index)
        database.executescript("PRAGMA writable_schema=OFF; PRAGMA schema_version=4;")
        with pytest.raises(sqlite3.DatabaseError, match="SQLite integrity_check"):
            markdown_transaction._require_v3_integrity(database)


def test_coordinator_integrity_reports_foreign_key_violations_separately():
    with contextlib.closing(sqlite3.connect(":memory:")) as database:
        database.executescript(
            "PRAGMA foreign_keys=OFF;"
            "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
            "CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));"
            "INSERT INTO child VALUES(1);"
        )
        with pytest.raises(sqlite3.DatabaseError, match="foreign-key violations: 1"):
            markdown_transaction._require_v3_integrity(database)
