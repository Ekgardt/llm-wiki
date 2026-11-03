"""Real SQL steps keep caller transactions, lease fences and attempt accounting."""
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from llm_wiki import queue_attempts as attempts
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.queue_contracts import LeaseFenceError, QueueFailure, QueueLease
from llm_wiki.queue_schema import _QUEUE_V3_TABLE_SQL
from llm_wiki.queue_timing import _timestamp
from llm_wiki.sqlite_transactions import begin_immediate

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(seconds=60)


@pytest.fixture
def database():
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        definitions = dict(_QUEUE_V3_TABLE_SQL)
        for name in ('tasks', 'attempt_history'):
            db.execute(definitions[name])
        db.execute('''INSERT INTO tasks(id,kind,handler_version,payload_blob,input_hash,
                   state,created_at,updated_at,available_at,attempts,lease_owner,
                   lease_token,lease_expires_at,attempt_started_at,error_code,blocked_capability)
                   VALUES ('task','query',1,?,?,'leased',?,?,?,2,'owner','token',?,?,
                           'previous_error','previous_capability')''',
                   (b'{}', sha256_bytes(b'{}'), _timestamp(NOW), _timestamp(NOW),
                    _timestamp(NOW), _timestamp(EXPIRES), _timestamp(NOW)))
        yield db


def _lease():
    return QueueLease('task', 'query', 1, {}, sha256_bytes(b'{}'), 'owner', 'token',
                      EXPIRES, 2, NOW, None, 1)


def _row(database):
    return database.execute("SELECT * FROM tasks WHERE id='task'").fetchone()


@pytest.mark.parametrize('failure,count,limit,expected', [
    (QueueFailure('failed'), 1, 2, ('ready', 1)),
    (QueueFailure('failed'), 2, 2, ('dead', 2)),
    (QueueFailure('invalid_input'), 1, 2, ('dead', 1)),
    (QueueFailure('unsupported_version'), 1, 2, ('dead', 1)),
    (QueueFailure('failed', permanent=True), 1, 2, ('dead', 1)),
    (QueueFailure('failed', permanent=True, blocked_capability='provider'), 2, 2, ('blocked', 1)),
])
def test_failure_disposition_preserves_blocked_priority_and_exhaustion(failure, count, limit, expected):
    assert attempts._failure_state({'attempts': count}, failure, limit) == expected


def test_history_and_failure_state_rollback_together_under_caller_transaction(database):
    row, lease = _row(database), _lease()
    failure = QueueFailure('failed', permanent=True)
    with pytest.raises(RuntimeError, match='caller abort'):
        with begin_immediate(database):
            attempts._record_failed_attempt(database, lease, row, failure, NOW)
            attempts._apply_failure_state(database, lease, row, failure, NOW)
            raise RuntimeError('caller abort')
    assert _row(database)['state'] == 'leased'
    assert database.execute('SELECT COUNT(*) FROM attempt_history').fetchone()[0] == 0
    with begin_immediate(database):
        attempts._record_failed_attempt(database, lease, row, failure, NOW)
        attempts._apply_failure_state(database, lease, row, failure, NOW)
    assert (_row(database)['state'], _row(database)['lease_token']) == ('dead', None)
    assert database.execute('SELECT COUNT(*) FROM attempt_history').fetchone()[0] == 1


def test_failure_with_stale_token_cannot_change_current_lease(database):
    old = _row(database)
    database.execute("UPDATE tasks SET lease_token='replacement'")
    with pytest.raises(LeaseFenceError):
        attempts._apply_failure_state(database, _lease(), old, QueueFailure('failed'), NOW)
    assert (_row(database)['state'], _row(database)['lease_token']) == ('leased', 'replacement')


@pytest.mark.parametrize('claim,expiry_name,limit_name,expected', [
    (attempts._take_task_lease, 'expires_at', 'max_attempts', ('previous_error', 'previous_capability')),
    (attempts._take_v3_task_lease, 'expires', 'attempt_limit', (None, None)),
])
def test_claim_compare_and_swap_preserves_legacy_and_v3_difference(database, claim, expiry_name, limit_name, expected):
    database.execute("UPDATE tasks SET state='ready',lease_owner=NULL,lease_token=NULL")
    row = _row(database)
    keywords = {'owner': 'new-owner', 'token': 'new-token', 'now': NOW,
                expiry_name: EXPIRES, limit_name: 8}
    assert claim(database, row, **keywords)
    assert not claim(database, row, **keywords)
    current = _row(database)
    assert (current['attempts'], current['lease_token']) == (3, 'new-token')
    assert (current['error_code'], current['blocked_capability']) == expected


def test_lease_must_be_owned_and_strictly_unexpired(database):
    row = attempts._require_leased_row(database, _lease(), NOW)
    assert row['id'] == 'task'
    with pytest.raises(LeaseFenceError):
        attempts._require_leased_row(database, _lease(), EXPIRES)
    with pytest.raises(LeaseFenceError):
        attempts._require_leased_row(database, replace(_lease(), owner='other'), NOW)


@pytest.mark.parametrize('change', [{'kind': 'other'}, {'payload': {'changed': True}}])
def test_lease_input_is_bound_to_both_header_and_payload(database, change):
    with pytest.raises(LeaseFenceError):
        attempts._require_lease_input(_row(database), replace(_lease(), **change))


def test_attempt_history_reader_uses_one_query(database):
    attempts._record_failed_attempt(database, _lease(), _row(database), QueueFailure('failed'), NOW)
    row = _row(database)
    queries = []
    database.set_trace_callback(queries.append)
    histories = attempts._attempt_histories(database, [row])
    assert histories['task'][0]['outcome'] == 'failed'
    assert len(queries) == 1
    assert attempts._attempt_histories(database, []) == {}
    assert len(queries) == 1
