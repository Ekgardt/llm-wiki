"""Existing transaction guarantees, isolated from filesystem integration."""
import sqlite3

import pytest
from llm_wiki.sqlite_transactions import begin_immediate


def test_begin_immediate_rolls_back_when_precommit_fence_expires():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE values_table(value INTEGER)")

    with pytest.raises(TimeoutError, match="deadline"):
        with begin_immediate(
            connection,
            before_commit=lambda: (_ for _ in ()).throw(
                TimeoutError("deadline expired before commit")
            ),
        ):
            connection.execute("INSERT INTO values_table VALUES (1)")

    assert connection.execute("SELECT value FROM values_table").fetchall() == []


def test_begin_immediate_commits_when_precommit_fence_succeeds():
    connection = sqlite3.connect(":memory:")
    checked = []
    connection.execute("CREATE TABLE values_table(value INTEGER)")

    with begin_immediate(connection, before_commit=lambda: checked.append(True)):
        connection.execute("INSERT INTO values_table VALUES (1)")

    assert checked == [True]
    assert connection.execute("SELECT value FROM values_table").fetchall() == [(1,)]


def test_begin_immediate_preserves_body_exception_when_rollback_fails():
    class BrokenRollbackConnection:
        def execute(self, statement):
            assert statement == "BEGIN IMMEDIATE"

        def commit(self):
            pytest.fail("commit must not run after a body failure")

        def rollback(self):
            raise OSError("rollback failed")

    original = RuntimeError("body failed")

    with pytest.raises(RuntimeError, match="body failed") as raised:
        with begin_immediate(BrokenRollbackConnection()):
            raise original

    assert raised.value is original
    assert isinstance(raised.value.__context__, OSError)



def test_keyboard_interrupt_rolls_back_without_closing_caller_connection():
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE values_table(value INTEGER)')
    with pytest.raises(KeyboardInterrupt):
        with begin_immediate(connection):
            connection.execute('INSERT INTO values_table VALUES (1)')
            raise KeyboardInterrupt
    assert connection.execute('SELECT value FROM values_table').fetchall() == []
    assert not connection.in_transaction
    connection.close()


class _CommitFails(sqlite3.Connection):
    def commit(self):
        raise sqlite3.OperationalError('commit failed')


def test_commit_failure_rolls_back():
    connection = sqlite3.connect(':memory:', factory=_CommitFails)
    connection.execute('CREATE TABLE values_table(value INTEGER)')
    with pytest.raises(sqlite3.OperationalError, match='commit failed'):
        with begin_immediate(connection):
            connection.execute('INSERT INTO values_table VALUES (1)')
    assert connection.execute('SELECT value FROM values_table').fetchall() == []
    connection.close()


def test_import_needs_no_filesystem_or_executor_module():
    import subprocess
    import sys
    from pathlib import Path
    code = '''
import importlib.abc, sqlite3, sys
sys.path.insert(0, sys.argv[1])
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert fullname not in {'reliable_memory','memory_queue','markdown_transaction'}
def refuse(*args, **kwargs):
    raise AssertionError('database opened during import')
sqlite3.connect = refuse
sys.meta_path.insert(0, Deny())
from llm_wiki.sqlite_transactions import begin_immediate
assert callable(begin_immediate)
'''
    result = subprocess.run([sys.executable, '-c', code, str(Path(__file__).resolve().parents[3] / 'scripts')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
