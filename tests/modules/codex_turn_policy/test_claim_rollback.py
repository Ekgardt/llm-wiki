"""Time thresholds and rollback preserve caller-owned capture bookkeeping."""
from datetime import timedelta

import codex_turn_end as turns
import pytest

from tests.modules.codex_turn_policy.policy_helpers import NOW, _pending


@pytest.mark.parametrize(('elapsed', 'expected'), [(-1, False), (1799, False), (1800, True)])
def test_claim_respects_exact_existing_window(elapsed, expected):
    state = {}
    turns.mark_captured(state, 'session', NOW)
    raw = {'session_id': 'session', 'turn_id': 'next'}
    assert turns.claim_turn_end(state, raw, NOW + timedelta(seconds=elapsed)) is expected
    assert ('pending' in state[turns.STATE_KEY]['session']) is (not expected)


def test_quiet_tail_selection_and_rollback_retain_original_entry():
    entries = {name: _pending(name) for name in ('z', 'a', 'b')}
    state = {turns.STATE_KEY: dict(entries)}
    assert turns.claim_quiet_tail(state, NOW + timedelta(seconds=1799), except_session='a') is None
    claim = turns.claim_quiet_tail(state, NOW + timedelta(seconds=1800), except_session='a')
    assert claim['session_id'] == 'b'
    assert claim['before'] is entries['b']
    assert state[turns.STATE_KEY]['a'] is entries['a']
    assert state[turns.STATE_KEY]['z'] is entries['z']
    turns.restore(state, 'b', claim['before'])
    assert turns.snapshot(state, 'b') is entries['b']


def test_failed_initial_claim_can_be_removed_without_losing_other_tails():
    other = _pending('other')
    state = {turns.STATE_KEY: {'other': other}}
    before = turns.snapshot(state, 'new')
    assert before is None
    assert turns.claim_turn_end(state, {'session_id': 'new'}, NOW)
    turns.restore(state, 'new', before)
    assert state[turns.STATE_KEY] == {'other': other}
    assert state[turns.STATE_KEY]['other'] is other


@pytest.mark.parametrize('state', [{}, {turns.STATE_KEY: None}, {turns.STATE_KEY: []}])
def test_snapshot_does_not_mutate_absent_or_malformed_registry(state):
    before = dict(state)
    assert turns.snapshot(state, 'missing') is None
    assert state == before
