"""A caller budget includes cold database setup and adoption admission."""
from __future__ import annotations

import sqlite3
import time

import markdown_transaction as transactions
import memory_queue
import pytest
import reliable_memory as reliability

from tests.slow_machine import LONG_TIMEOUT
from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


def test_expired_operational_open_creates_no_database(tmp_path):
    path = tmp_path / 'new.sqlite3'
    with pytest.raises(TimeoutError):
        reliability.open_operational_db(path, busy_ms=10_000, deadline=time.monotonic() - 1)
    assert not path.exists()


def test_readonly_open_expired_even_when_database_is_valid(tmp_path):
    path = tmp_path / 'existing.sqlite3'
    reliability.open_operational_db(path, busy_ms=0).close()
    with pytest.raises(TimeoutError):
        reliability.open_readonly_operational_db(path, tmp_path, max_bytes=None, deadline=time.monotonic() - 1)


@pytest.mark.parametrize('factory', [memory_queue.MemoryQueue, transactions.MarkdownCoordinator])
def test_legacy_cold_factory_deadline_bounds_locked_database(tmp_path, factory):
    arguments = (tmp_path,)
    if factory is transactions.MarkdownCoordinator:
        arguments = (tmp_path, tmp_path)
    actor = factory(*arguments)
    path = getattr(actor, 'db_path', getattr(actor, 'database_path', None))
    with sqlite3.connect(path, isolation_level=None) as blocker:
        blocker.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            factory(*arguments, deadline=started + 0.04)
        assert time.monotonic() - started < 0.5
        blocker.execute('ROLLBACK')
    assert factory(*arguments) is not None


def test_adoption_mutex_obeys_caller_deadline(tmp_path):
    vault, state = _vault(tmp_path)
    build_adopted_reliability_v3(vault, state)
    with transactions._ADOPTION_VALIDATION_LOCK:
        with pytest.raises(TimeoutError):
            transactions._require_adopted_once(vault, state, deadline=time.monotonic() + 0.02)


@pytest.mark.parametrize('factory', [memory_queue.active_memory_queue, transactions.active_markdown_coordinator])
def test_adopted_cold_factory_has_a_bounded_retry_and_can_recover(tmp_path, factory):
    vault, state = _vault(tmp_path)
    build_adopted_reliability_v3(vault, state)
    transactions._ADOPTION_VALIDATION_CACHE.clear()
    with sqlite3.connect(state / 'run/queue-v3.sqlite3', isolation_level=None) as blocker:
        blocker.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            factory(vault, state, deadline=started + 0.04)
        assert time.monotonic() - started < 0.5
        assert not transactions._ADOPTION_VALIDATION_CACHE
        blocker.execute('ROLLBACK')
    assert factory(vault, state, deadline=time.monotonic() + LONG_TIMEOUT) is not None


def test_expired_admission_does_not_create_runtime_state(tmp_path):
    for factory in (memory_queue.active_or_legacy_memory_queue, transactions.active_or_legacy_coordinator):
        with pytest.raises(TimeoutError):
            factory(tmp_path, tmp_path, deadline=time.monotonic() - 1)
    assert not (tmp_path / 'run').exists()


def test_shared_readonly_connection_interrupts_sql_not_only_the_open(tmp_path):
    path = tmp_path / 'read.sqlite3'
    reliability.open_operational_db(path, busy_ms=0).close()
    started = time.monotonic()
    database = reliability.open_readonly_operational_db(path, tmp_path, max_bytes=None, deadline=started + 0.04)
    try:
        with pytest.raises(sqlite3.OperationalError, match='interrupted'):
            database.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()
        assert time.monotonic() - started < 0.5
    finally:
        database.close()


def test_cold_to_warm_authority_cycle_preserves_and_settles_failure(tmp_path):
    vault, state = _vault(tmp_path)
    build_adopted_reliability_v3(vault, state)
    deadline = time.monotonic() + 5
    queue = memory_queue.active_or_legacy_memory_queue(vault, state, deadline=deadline)
    coordinator = transactions.active_or_legacy_coordinator(vault, state, deadline=deadline)
    queue.record_source_failure('knowledge/daily/2026-07-14.md', 'a' * 64, error_code='unprocessed', producer='compile')
    assert queue.source_failure_keys(deadline=deadline) == [('knowledge/daily/2026-07-14.md', 'a' * 64)]
    assert coordinator.operation_id_at(1, deadline=deadline) is None
    queue.clear_source_failure('knowledge/daily/2026-07-14.md', 'a' * 64, deadline=deadline)
    assert not queue.source_failure_keys(deadline=deadline)
    assert memory_queue.active_memory_queue(vault, state, deadline=deadline).source_failure_keys(deadline=deadline) == []
