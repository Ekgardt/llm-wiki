"""Collect semantic defects together; repair review format in the review role."""
import json
from unittest.mock import Mock

import compile_memory as cm
from llm_client import LLMResult

from tests.test_compile_critic_drop_cannot_complete_source_work import _attempt, _inputs, _proposed
from tests.test_the_compile_decides_what_the_snapshot_already_knows import _source_review
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault


def _reply(provider, text):
    return LLMResult(provider, text, True, None, 'native')


def test_rejected_operation_does_not_hide_missing_source_facts(vault):
    root, state = vault
    operation = _proposed()
    attempt, provider = _attempt(_inputs(root), state, [operation], ['drop'])
    review = json.dumps({'reviews': [{'slug': operation['slug'], 'verdict': 'drop', 'reason': 'Unsupported scope.'}]})
    missing = json.dumps({'source_review': {'complete': False, 'reason': 'Missing the deployment dependency.'}})
    attempt._call = Mock(side_effect=[_reply(provider, review), _reply(provider, missing)])
    assert attempt._critiqued(provider, attempt._action(provider), [operation]) is None
    feedback = '\n'.join(attempt.critic_feedback)
    assert 'Unsupported scope.' in feedback and 'Missing the deployment dependency.' in feedback
    assert not list(attempt.cache.cache_dir.glob('*.json'))


def test_malformed_operation_review_is_repaired_without_redrafting(vault):
    root, state = vault
    operation = _proposed()
    attempt, provider = _attempt(_inputs(root), state, [operation], ['pass'])
    malformed = '{"reviews":[{"slug":"cedar-merge-order","verdict":"pass"}]}'
    attempt._call = Mock(return_value=_reply(provider, malformed))
    assert attempt._critiqued(provider, attempt._action(provider), [operation]) is None
    assert attempt.critic_feedback == ()
    valid = json.dumps({'reviews': [{'slug': operation['slug'], 'verdict': 'pass', 'reason': 'Exact fact.'}]})
    attempt._call = Mock(side_effect=[_reply(provider, valid), _reply(provider, _source_review())])
    assert attempt._drafted(provider, attempt._action(provider)) is not None
    first = attempt._call.call_args_list[0].args
    assert first[2:] == (cm.CRITIQUE_SYSTEM, cm.CRITIQUE_SCHEMA)
    assert json.loads(first[1].rsplit('\n', 1)[1])['rejected_response'] == malformed
    assert 'reason' in first[1]
    assert 'never a draft' in first[1]
    assert all(call.args[2] == cm.CRITIQUE_SYSTEM for call in attempt._call.call_args_list)


def test_malformed_empty_source_review_keeps_empty_draft_pending(vault):
    root, state = vault
    attempt, provider = _attempt(_inputs(root), state, [], [])
    malformed = '{"source_review":{"complete":"yes"}}'
    attempt._call = Mock(return_value=_reply(provider, malformed))
    assert attempt._critiqued(provider, attempt._action(provider), []) is None
    attempt._call = Mock(return_value=_reply(provider, _source_review()))
    result = attempt._drafted(provider, attempt._action(provider))
    assert result is not None and result.plan['operations'] == []
    attempt._call.assert_called_once()
    assert attempt._call.call_args.args[3] == cm.SOURCE_REVIEW_SCHEMA
    assert json.loads(attempt._call.call_args.args[1].rsplit('\n', 1)[1])['rejected_response'] == malformed


def test_missing_verdict_has_no_hidden_retry_beyond_operator_budget(vault, monkeypatch):
    root, state = vault
    operation = _proposed()
    attempt, provider = _attempt(_inputs(root), state, [operation], ['pass'])
    monkeypatch.setenv('LLM_WIKI_COMPILE_VALIDATION_RETRIES', '0')
    attempt._call = Mock(side_effect=[_reply(provider, json.dumps({'operations': [operation]})),
                                     _reply(provider, '{"reviews":[]}')])
    assert attempt._drafted_with_retries(provider, attempt._action(provider)) is None
    assert attempt._call.call_count == 2
    assert attempt.pending_review_operations == [operation]
    assert attempt.critic_feedback == ()


def test_review_correction_is_sized_whole_before_calling_provider(vault):
    root, state = vault
    operation = _proposed()
    attempt, provider = _attempt(_inputs(root), state, [operation], ['pass'])
    attempt._call = Mock(return_value=_reply(provider, '{"reviews":[]}'))
    assert attempt._critiqued(provider, attempt._action(provider), [operation]) is None
    attempt._fits = Mock(return_value=False)
    attempt._call = Mock(side_effect=AssertionError('Over-budget review correction reached provider'))
    assert attempt._drafted(provider, attempt._action(provider)) is None
    attempt._call.assert_not_called()
