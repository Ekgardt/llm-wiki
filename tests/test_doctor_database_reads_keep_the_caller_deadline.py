"""Doctor's SQLite opening and SQL execution share its caller's clock."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import doctor
import pytest


def _database_file(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE observations(value TEXT)")
        writer.execute("INSERT INTO observations VALUES ('verified observation')")
    return path


def test_expired_doctor_refuses_before_opening_sqlite(tmp_path):
    path = _database_file(tmp_path)
    with pytest.raises(TimeoutError, match="deadline"):
        with closing(doctor._readonly_database(path, tmp_path, deadline=time.monotonic() - 1)):
            pytest.fail("Expired doctor admitted a database reader")


def test_doctor_interrupts_sql_work_under_its_existing_deadline(tmp_path):
    path = _database_file(tmp_path)
    deadline = time.monotonic() + 0.02
    with closing(doctor._readonly_database(path, tmp_path, deadline=deadline)) as database:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            database.execute(
                "WITH RECURSIVE values_to_read(value) AS (SELECT 1 UNION ALL "
                "SELECT value + 1 FROM values_to_read WHERE value < 1000000) "
                "SELECT SUM(value) FROM values_to_read"
            ).fetchone()


def test_doctor_reads_valid_data_without_mutation_before_deadline(tmp_path):
    path = _database_file(tmp_path)
    with closing(doctor._readonly_database(path, tmp_path, deadline=time.monotonic() + 5)) as database:
        assert database.execute("SELECT value FROM observations").fetchone()[0] == "verified observation"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            database.execute("DELETE FROM observations")
