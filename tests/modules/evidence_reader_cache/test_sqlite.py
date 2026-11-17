"""Real SQLite readers verify lease lifetime without importing graph builders."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import evidence_reader_cache as cache
import pytest

from tests.slow_machine import LONG_TIMEOUT


class Reader:
    def __init__(self, path):
        self.database_path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)

    def close(self):
        self.connection.close()


def _borrow(tmp_path, opened):
    def open_reader(_scope):
        reader = Reader(tmp_path / 'evidence.sqlite3')
        opened.append(reader)
        return reader

    scope = SimpleNamespace(checkout_root=str(tmp_path), git_common_dir=None)
    return cache.leased_graph(
        (str(tmp_path), True), catalog_path=tmp_path / 'catalog.sqlite3',
        resolve_scope=lambda: scope, open_graph=open_reader,
    )


def _query(tmp_path, opened):
    with _borrow(tmp_path, opened) as lease:
        return lease.connection.execute('SELECT 42').fetchone()


def test_clear_defers_sqlite_close_until_held_lease_returns(tmp_path):
    opened = []
    lease = _borrow(tmp_path, opened)
    connection = lease.connection
    cache.clear()
    assert cache.cached_entries() == 0
    assert connection.execute('SELECT 42').fetchone() == (42,)
    lease.close()
    lease.close()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 42')


def test_a_released_reader_is_reused_on_another_thread_then_closed_on_forget(tmp_path):
    opened = []
    assert _query(tmp_path, opened) == (42,)
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(_query, tmp_path, opened).result(timeout=LONG_TIMEOUT) == (42,)
    assert len(opened) == 1
    cache.forget((str(tmp_path), True))
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].connection.execute('SELECT 42')


def test_generation_memo_is_shared_until_retirement(tmp_path):
    opened = []
    calls = []
    with _borrow(tmp_path, opened) as lease:
        assert lease.memoized('value', lambda: calls.append('first') or 42) == 42
    with _borrow(tmp_path, opened) as lease:
        assert lease.memoized('value', lambda: calls.append('second')) == 42
    assert calls == ['first']
    cache.clear()
    with _borrow(tmp_path, opened) as lease:
        assert lease.memoized('value', lambda: 'fresh') == 'fresh'


def test_opener_failure_propagates_without_publishing_an_entry(tmp_path):
    def fail(_scope):
        raise OSError('reader unavailable')

    scope = SimpleNamespace(checkout_root=str(tmp_path), git_common_dir=None)
    with pytest.raises(OSError, match='reader unavailable'):
        cache.leased_graph(
            ('failed', True), catalog_path=tmp_path / 'catalog.sqlite3',
            resolve_scope=lambda: scope, open_graph=fail,
        )
    assert cache.cached_entries() == 0
