"""A held lease survives a busy database until it expires, and a lost fence ends it at once.

Seven renewers gave up at the first or second failure; a 30-second lease was
declared lost at t=30 s behind one slow, locked renewal. Then a retry that could not
finish before the expiry was still started, and the loss was known up to one busy wait
late. Research: `docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`,
`docs/research/2026-09-14-a-lost-lease-is-known-by-its-expiry.md`.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lease_renewal import busy_database, renew_until_stopped  # noqa: E402

from tests.sqlite_errors import busy_database_error  # noqa: E402


class _Clock:
    """Time that passes only when the renewer waits or a renewal takes it."""

    def __init__(self, stop_after: float) -> None:
        self.now = 0.0
        self.stop_after = stop_after
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.waits.append(round(seconds, 3))
        self.now += seconds
        return self.now >= self.stop_after


class _Renewal:
    def __init__(self, clock: _Clock, outcomes: list[BaseException | None], cost: float = 0.0) -> None:
        self.clock = clock
        self.outcomes = outcomes
        self.cost = cost

    def __call__(self) -> None:
        self.clock.now += self.cost
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


def _run(
    clock: _Clock,
    renewal: _Renewal,
    lease_seconds: float = 30.0,
    attempt_seconds: float = 0.0,
    held_since: float = 0.0,
):
    return renew_until_stopped(
        renewal,
        interval=10.0,
        lease_seconds=lease_seconds,
        attempt_seconds=attempt_seconds,
        held_since=held_since,
        stop=threading.Event(),
        wait=clock.wait,
        monotonic=clock.monotonic,
    )


def test_the_lease_expires_from_when_it_was_taken():
    """The renewer started 5 s after the lease was written; it must not add them back.

    The lease was taken at 0 and lasts 30 s. Each renewal blocks its full 10 s
    busy wait, so the failure at 25 leaves no room for a retry and the renewer
    gives up there. Counting the 30 s from the thread's own start instead put
    the expiry at 35 and kept retrying for five seconds after the fence had
    lapsed for everybody else. See
    `docs/research/2026-09-18-a-lease-expires-from-when-it-was-taken.md`.
    """
    clock = _Clock(stop_after=600.0)
    clock.now = 5.0
    locked = busy_database_error()

    ended = _run(
        clock,
        _Renewal(clock, [locked] * 100, cost=10.0),
        attempt_seconds=10.0,
        held_since=0.0,
    )

    assert (isinstance(ended, sqlite3.OperationalError), clock.now) == (True, 25.0)


def test_a_slow_locked_renewal_is_retried_before_the_lease_expires():
    """The measured case: one renewal blocked 10 s on a lock, then the lock lifts."""
    clock = _Clock(stop_after=60.0)
    locked = busy_database_error()
    renewal = _Renewal(clock, [None, locked, None])

    ended = _run(clock, renewal)

    assert ended is None
    assert clock.waits[:3] == [10.0, 10.0, 2.0]


def test_a_lock_that_outlasts_the_lease_ends_it():
    clock = _Clock(stop_after=600.0)
    locked = busy_database_error()

    ended = _run(clock, _Renewal(clock, [locked] * 100, cost=1.0))

    assert isinstance(ended, sqlite3.OperationalError)
    assert clock.now <= 40.0


def test_a_fence_taken_by_someone_else_ends_the_lease_at_once():
    clock = _Clock(stop_after=600.0)

    ended = _run(clock, _Renewal(clock, [RuntimeError("owner_fence_lost")]))

    assert (str(ended), clock.now) == ("owner_fence_lost", 10.0)


def test_a_lock_that_outlasts_the_lease_is_known_lost_by_its_expiry():
    """Each renewal blocks the database's full busy wait; no retry is started past the expiry."""
    clock = _Clock(stop_after=600.0)
    locked = busy_database_error()

    ended = _run(clock, _Renewal(clock, [locked] * 100, cost=10.0), attempt_seconds=10.0)

    assert (isinstance(ended, sqlite3.OperationalError), clock.now) == (True, 30.0)


def test_the_measured_lock_still_lets_the_retry_that_fits_renew():
    """One beat blocked its whole busy wait; the retry that still fits before expiry succeeds."""
    clock = _Clock(stop_after=60.0)
    locked = busy_database_error()

    ended = _run(clock, _Renewal(clock, [locked, None], cost=10.0), attempt_seconds=10.0)

    assert ended is None


def test_a_missing_table_is_not_retried_as_a_busy_lease():
    import pytest

    with sqlite3.connect(":memory:") as database:
        with pytest.raises(sqlite3.OperationalError) as caught:
            database.execute("SELECT * FROM locked")
    clock = _Clock(stop_after=60.0)
    error = caught.value
    renewal = _Renewal(clock, [error, None])
    assert _run(clock, renewal) is error
    assert clock.waits == [10.0]
    assert renewal.outcomes == [None]


def test_a_read_only_database_is_not_retried_as_a_busy_lease():
    import pytest

    with sqlite3.connect(":memory:") as database:
        database.execute("PRAGMA query_only=ON")
        with pytest.raises(sqlite3.OperationalError) as caught:
            database.execute("CREATE TABLE example(value)")
    clock = _Clock(stop_after=60.0)
    assert _run(clock, _Renewal(clock, [caught.value, None])) is caught.value
    assert clock.waits == [10.0]


@pytest.mark.parametrize('code, expected', [
    (sqlite3.SQLITE_BUSY, True),
    (sqlite3.SQLITE_BUSY_SNAPSHOT, True),
    (sqlite3.SQLITE_LOCKED, True),
    (sqlite3.SQLITE_LOCKED_SHAREDCACHE, True),
    (sqlite3.SQLITE_IOERR, False),
    (sqlite3.SQLITE_READONLY, False),
])
def test_the_sqlite_code_decides_contention(code, expected):
    error = sqlite3.OperationalError('no such table: locked')
    error.sqlite_errorcode = code
    assert busy_database(error) is expected


@pytest.mark.parametrize('message', ['database is locked', 'database table is locked: tasks'])
def test_an_error_without_a_code_is_never_contention(message):
    assert not busy_database(sqlite3.OperationalError(message))


@pytest.mark.parametrize('code, expected', [(5, True), (6, True), (261, True), (262, True), (10, False)])
def test_primary_error_code_overrides_misleading_message(code, expected):
    error = busy_database_error()
    error.sqlite_errorcode = code
    assert busy_database(error) is expected


@pytest.mark.parametrize('error', [ValueError('database is locked'), PermissionError('denied')])
def test_non_sqlite_errors_are_not_contention(error):
    assert not busy_database(error)


def test_real_contention_is_busy(tmp_path):
    from contextlib import closing

    path = tmp_path / 'lease.sqlite3'
    with closing(sqlite3.connect(path, timeout=0)) as first, closing(sqlite3.connect(path, timeout=0)) as second:
        first.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError) as caught:
            second.execute('BEGIN IMMEDIATE')
    assert busy_database(caught.value)


def test_already_stopped_loop_does_not_renew():
    clock = _Clock(stop_after=60)
    renewal = _Renewal(clock, [RuntimeError('must not be called')])
    stop = threading.Event()
    stop.set()
    assert renew_until_stopped(renewal, interval=10, lease_seconds=30, attempt_seconds=5,
                              held_since=0, stop=stop, monotonic=clock.monotonic) is None
    assert len(renewal.outcomes) == 1


def test_process_control_exception_is_not_converted_to_lease_failure():
    clock = _Clock(stop_after=60)
    with pytest.raises(KeyboardInterrupt):
        _run(clock, _Renewal(clock, [KeyboardInterrupt()]))


_ROOT = Path(__file__).resolve().parents[1]


def _import_roots(node):
    if isinstance(node, ast.Import):
        return {alias.name.split('.')[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {('.' * node.level + (node.module or '')).split('.')[0]}
    return set()


def _nonstdlib_imports(source):
    roots = set().union(*(_import_roots(node) for node in ast.walk(ast.parse(source))))
    return roots - sys.stdlib_module_names


def _project_import_modules(source):
    imports = (node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom))
    return {node.module for node in imports if node.module.partition('.')[0] not in sys.stdlib_module_names}


def test_renewal_depends_only_on_the_sqlite_error_classifier():
    source = (_ROOT / 'scripts/lease_renewal.py').read_text(encoding='utf-8')
    assert _nonstdlib_imports(source) == {'llm_wiki'}
    assert _project_import_modules(source) == {'llm_wiki.sqlite_deadlines'}


@pytest.mark.parametrize('source', [
    'import memory_queue',
    'from markdown_transaction import MarkdownCoordinator',
    'def renew():\n import doctor as executor',
    'from operational_ownership import *',
    'from . import local',
    'from .sqlite3 import connect',
])
def test_boundary_detects_executor_and_relative_imports(source):
    assert _nonstdlib_imports(source)


def test_boundary_accepts_standard_library():
    assert not _nonstdlib_imports('import sqlite3 as database\nfrom threading import Event')


def test_complete_lease_renewal_context_fits_user_ceiling():
    manifest = json.loads((_ROOT / 'docs/modules/lease-renewal-context.json').read_text(encoding='utf-8'))
    paths = manifest['files']
    assert {'scripts/lease_renewal.py', 'tests/test_a_busy_database_is_not_a_lost_lease.py',
            'docs/modules/lease-renewal.md', 'docs/modules/lease-renewal-context.json'} <= set(paths)
    assert len(paths) == len(set(paths))
    assert manifest['max_tokens'] == 100_000
    from tests.module_context import measure_context
    total, files = measure_context(_ROOT, _ROOT / 'docs/modules/lease-renewal-context.json')
    print(f'lease-renewal: {total} tokens; {files} files')
