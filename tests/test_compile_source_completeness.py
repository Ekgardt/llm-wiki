"""No source receipt may rely on an unreviewed omission or empty draft."""
import json
from unittest.mock import Mock

import compile_memory as cm
from compile_cache import CompileCache
from llm_client import LLMResult

from tests.test_compile_critic_drop_cannot_complete_source_work import QUOTE, _inputs, _proposed
from tests.test_the_compile_decides_what_the_snapshot_already_knows import _provider
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault


def _attempt(root, state, complete):
    provider = _provider()
    attempt = cm._CompileAttempt(_inputs(root), CompileCache(state), None, None)
    def reply(descriptor, prompt, system, schema):
        text = {'reviews': [{'slug': 'cedar-merge-order', 'verdict': 'pass', 'reason': 'Supported.'}]}
        if 'source_review' in schema['properties']:
            text = {'source_review': {'complete': complete, 'reason': 'Check the Cedar merge-order fact.'}}
        return LLMResult(descriptor, json.dumps(text), True, None, 'native')
    attempt._call = Mock(side_effect=reply)
    return attempt, provider


def test_initial_empty_draft_cannot_discard_a_durable_fact(vault):
    root, state = vault
    attempt, provider = _attempt(root, state, False)
    assert attempt._critiqued(provider, attempt._action(provider), []) is None
    assert not list(attempt.cache.cache_dir.glob('*.json'))
    assert 'Cedar merge-order' in '\n'.join(attempt.critic_feedback)


def test_approved_empty_draft_has_source_review_and_critique_cache_identity(vault):
    root, state = vault
    attempt, provider = _attempt(root, state, True)
    actions = attempt._action(provider)
    resolved = attempt._critiqued(provider, actions, [])
    assert resolved is not None
    assert resolved.action == actions
    assert attempt._call.call_count == 1
    assert QUOTE in attempt._call.call_args.args[1]


def test_individually_supported_operations_do_not_prove_source_completeness(vault):
    root, state = vault
    attempt, provider = _attempt(root, state, False)
    assert attempt._critiqued(provider, attempt._action(provider), [_proposed()]) is None
    assert not list(attempt.cache.cache_dir.glob('*.json'))


def test_missing_source_verdict_is_not_success(vault):
    root, state = vault
    attempt, provider = _attempt(root, state, True)
    attempt._call = Mock(return_value=LLMResult(provider, '{"reviews": []}', True, None, 'native'))
    assert attempt._critiqued(provider, attempt._action(provider), []) is None


def test_full_source_review_is_budgeted_before_dispatch(vault):
    root, state = vault
    attempt, provider = _attempt(root, state, True)
    attempt._fits = Mock(return_value=False)
    attempt._call = Mock(side_effect=AssertionError('Oversized source review must not call provider'))
    assert attempt._critiqued(provider, attempt._action(provider), []) is None
    assert QUOTE in attempt._fits.call_args.args[0]
    attempt._call.assert_not_called()


def test_rejected_proposal_can_finish_empty_only_after_complete_source_review(vault):
    root, state = vault
    attempt, provider = _attempt(root, state, True)
    attempt.critic_feedback = (json.dumps({'operations': [_proposed()], 'reviews': [
        {'slug': 'cedar-merge-order', 'verdict': 'drop', 'reason': 'Already preserved.'}]}),)
    resolved = attempt._critiqued(provider, attempt._action(provider), [])
    assert resolved is not None
    assert resolved.plan['operations'] == []
    assert QUOTE in attempt._call.call_args.args[1]


def test_fake_backend_has_explicit_source_review_fixture(monkeypatch):
    from llm_client import _call_fake
    monkeypatch.delenv('MEMORY_LLM_FAKE_RESPONSE', raising=False)
    response = _call_fake(_provider(), 'fixture', cm.CRITIQUE_SYSTEM, cm.SOURCE_REVIEW_SCHEMA)
    assert json.loads(response)['source_review']['complete'] is True
    assert 'fixture' in json.loads(response)['source_review']['reason']
