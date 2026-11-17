"""Cooperative query interruption and caller budget semantics on real SQLite."""
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from llm_wiki import sqlite_deadlines as control


def _clock(monkeypatch, value):
    monkeypatch.setattr(control, 'time', SimpleNamespace(monotonic=lambda: value[0]))


@pytest.mark.parametrize('deadline', [None, float('inf')])
def test_unbounded_deadline_preserves_existing_connection_controls(monkeypatch, deadline):
    _clock(monkeypatch, [10.0])
    with closing(sqlite3.connect(':memory:')) as database:
        database.execute('PRAGMA busy_timeout=4321')
        database.set_progress_handler(lambda: 1, 1)
        control.configure_operational_deadline(database, deadline)
        control.refresh_operational_deadline(database, 99, deadline)
        with pytest.raises(sqlite3.OperationalError, match='interrupted'):
            database.execute('SELECT 1')
        database.set_progress_handler(None, 0)
        assert database.execute('PRAGMA busy_timeout').fetchone()[0] == 4321
        assert control.operational_busy_ms(99, deadline) == 99


def test_expired_budget_refuses_before_touching_connection(monkeypatch):
    _clock(monkeypatch, [10.0])
    with pytest.raises(TimeoutError, match='deadline expired'):
        control.configure_operational_deadline(object(), 10.0)
    with pytest.raises(TimeoutError, match='deadline expired'):
        control.refresh_operational_deadline(object(), 3000, 9.0)


def test_lock_wait_is_recomputed_with_remaining_budget(monkeypatch):
    now = [10.0]
    _clock(monkeypatch, now)
    with closing(sqlite3.connect(':memory:')) as database:
        control.refresh_operational_deadline(database, 5000, 12.0)
        assert database.execute('PRAGMA busy_timeout').fetchone()[0] == 2000
        now[0] = 11.5
        control.refresh_operational_deadline(database, 5000, 12.0)
        assert database.execute('PRAGMA busy_timeout').fetchone()[0] == 500
        assert control.operational_busy_ms(100, 12.0) == 100


def test_real_query_stops_after_deadline_and_connection_remains_owned_by_caller(monkeypatch):
    now = [10.0]
    _clock(monkeypatch, now)
    with closing(sqlite3.connect(':memory:')) as database:
        control.configure_operational_deadline(database, 11.0)
        now[0] = 11.0
        with pytest.raises(sqlite3.OperationalError, match='interrupted'):
            database.execute('''WITH RECURSIVE n(x) AS
                (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000)
                SELECT sum(x) FROM n''').fetchone()
        database.set_progress_handler(None, 0)
        assert database.execute('SELECT 42').fetchone()[0] == 42
