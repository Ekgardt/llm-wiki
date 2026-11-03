"""Original pending-tail policy regressions, preserved from their integration file."""
from datetime import UTC, datetime, timedelta

import codex_turn_end as turns

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _pending(number):
    return {'captured_at': NOW.isoformat(), 'pending': {'cwd': '/project', 'transcript_path': f'/sessions/{number}.jsonl', 'turn_id': f'turn-{number}', 'at': NOW.isoformat()}}


def test_pending_tail_survives_more_than_sixty_four_newer_captures():
    before = _pending('old')
    state = {turns.STATE_KEY: {'old': before}}
    for number in range(80):
        turns.mark_captured(state, f'new-{number}', NOW + timedelta(minutes=1))
    assert state[turns.STATE_KEY]['old'] == before
    claimed = turns.claim_quiet_tail(state, NOW + timedelta(minutes=31), except_session='current')
    assert claimed['session_id'] == 'old'
    assert claimed['turn_id'] == 'turn-old'


def test_captures_inside_the_existing_window_are_not_evicted_by_count():
    state = {}
    for number in range(80):
        turns.mark_captured(state, f'session-{number}', NOW)
    assert len(state[turns.STATE_KEY]) == 80
    raw = {'session_id': 'session-0', 'cwd': '/project', 'transcript_path': '/sessions/0.jsonl', 'turn_id': 'new-turn'}
    assert turns.claim_turn_end(state, raw, NOW + timedelta(minutes=1)) is False


def test_only_expired_capture_bookkeeping_is_retired():
    pending = _pending('old')
    state = {turns.STATE_KEY: {'pending': pending, 'old-capture': {'captured_at': NOW.isoformat()}}}
    turns.mark_captured(state, 'current', NOW + timedelta(minutes=31))
    assert state[turns.STATE_KEY] == {'pending': pending, 'current': {'captured_at': (NOW + timedelta(minutes=31)).isoformat()}}


def test_unknown_entries_and_unreadable_timestamps_are_preserved():
    protected = {'unknown': {'captured_at': 'not-a-date'}, 'pending-null': {'captured_at': NOW.isoformat(), 'pending': None}, 'future': {'captured_at': (NOW + timedelta(days=2)).isoformat()}, 'other': {'captured_at': NOW.isoformat(), 'unrecognized': 'evidence'}}
    state = {turns.STATE_KEY: dict(protected)}
    turns.mark_captured(state, 'current', NOW + timedelta(minutes=31))
    assert all(state[turns.STATE_KEY][key] == value for key, value in protected.items())
