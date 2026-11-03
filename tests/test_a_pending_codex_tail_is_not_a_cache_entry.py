import json
from datetime import timedelta

import codex_turn_end as turns
import memory_state
import pytest

from tests.adopted_capture_vault import host_transcript, intent_records
from tests.modules.codex_turn_policy.test_policy import NOW, _pending
from tests.test_a_codex_turn_is_not_a_session import _stop
from tests.test_a_codex_turn_is_not_a_session import codex as codex


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
    assert records[0]['evidence'][0]['parts'][0]['text'] == transcript.read_bytes().decode('utf-8')
