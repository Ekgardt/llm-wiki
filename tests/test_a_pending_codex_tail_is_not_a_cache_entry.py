import json
from datetime import datetime, timedelta, timezone

import codex_turn_end as turns
import memory_state
import pytest

from tests.adopted_capture_vault import host_transcript, intent_records
from tests.test_a_codex_turn_is_not_a_session import _stop
from tests.test_a_codex_turn_is_not_a_session import codex as codex

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


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


def _state_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_state, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(memory_state, 'STATE_FILE', tmp_path/'state.json')
    monkeypatch.setattr(memory_state, 'LOCK_FILE', tmp_path/'state.json.lock')


def _too_many_pending(state):
    state[turns.STATE_KEY] = {f'session-{number}': _pending(number) for number in range(1400)}


def test_oversized_protected_state_is_refused_without_replacing_either_copy(tmp_path, monkeypatch):
    _state_paths(tmp_path, monkeypatch)
    memory_state.update_state(lambda state: state.update({'existing': 'first'}))
    memory_state.update_state(lambda state: state.update({'existing': 'second'}))
    current = memory_state.STATE_FILE.read_bytes()
    previous = memory_state._previous_state_file().read_bytes()
    with pytest.raises(OSError, match='state.*budget|state.*large'):
        memory_state.update_state(_too_many_pending)
    assert memory_state.STATE_FILE.read_bytes() == current
    assert memory_state._previous_state_file().read_bytes() == previous
    assert json.loads(current)['existing'] == 'second'


def test_capture_plan_falls_back_to_durable_capture_when_state_cannot_admit_a_tail(tmp_path, monkeypatch):
    import codex_memory

    _state_paths(tmp_path, monkeypatch)
    memory_state.save_state({turns.STATE_KEY: {'current': {'captured_at': NOW.isoformat()}}})
    before = memory_state.STATE_FILE.read_bytes()
    monkeypatch.setattr(memory_state, 'MAX_STATE_TARGET_BYTES', len(before))
    raw = {'session_id': 'current', 'cwd': '/project', 'transcript_path': '/sessions/current.jsonl', 'turn_id': 'current-turn', 'hook_event_name': 'Stop'}
    plan = codex_memory._turn_end_plan(raw, NOW + timedelta(minutes=1))
    assert plan == {'capture': True, 'claimed': False, 'before': None, 'tail': None}
    assert memory_state.STATE_FILE.read_bytes() == before


def test_refused_tail_plan_still_publishes_a_complete_durable_capture(codex, monkeypatch):
    module, state_root, project = codex
    transcript = host_transcript(state_root, 'fallback.jsonl', 'The complete conversation remains durable.\n')
    raw = _stop(project, transcript, 'current', 'fallback-turn')
    memory_state.save_state({turns.STATE_KEY: {'current': {'captured_at': NOW.isoformat()}}})
    before = memory_state.STATE_FILE.read_bytes()
    writer_budget = memory_state.MAX_STATE_TARGET_BYTES
    monkeypatch.setattr(memory_state, 'MAX_STATE_TARGET_BYTES', len(before))
    plan = module._turn_end_plan(raw, NOW + timedelta(minutes=1))
    assert plan['capture'] is True and plan['claimed'] is False
    monkeypatch.setattr(memory_state, 'MAX_STATE_TARGET_BYTES', writer_budget)
    envelope = module.normalize_codex_hook(raw, stop_event='session_end')
    module._ingest_claimed(envelope, raw['session_id'], plan)
    records = intent_records(state_root)
    assert len(records) == 1
    assert records[0]['session'] == 'current'
    assert records[0]['source_event_id'] == 'fallback-turn'
    assert records[0]['evidence'][0]['parts'][0]['text'] == transcript.read_text()
