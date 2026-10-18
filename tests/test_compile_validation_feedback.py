"""A retry must know which answer failed and why; refusal is not completion."""
import json
from unittest.mock import Mock

import compile_memory as cm
import pytest
from compile_cache import CompileCache
from llm_client import LLMResult

from tests.test_compile_critic_drop_cannot_complete_source_work import (
    QUOTE,
    _inputs,
    _proposed,
)
from tests.test_the_compile_decides_what_the_snapshot_already_knows import _provider
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault


def _invalid_draft(kind):
    operation = _proposed()
    if kind == 'json':
        return '{"operations": ['
    operation[kind] = {'related': [42], 'category': 'unlisted'}[kind]
    return json.dumps({'operations': [operation]})


@pytest.mark.parametrize('kind', ['json', 'related', 'category'])
def test_retry_receives_failed_answer_reason_and_complete_sources(vault, kind):
    root, state = vault
    attempt = cm._CompileAttempt(_inputs(root), CompileCache(state), None, None)
    provider = _provider()
    invalid = _invalid_draft(kind)
    valid = json.dumps({'operations': [_proposed()]})
    review = json.dumps({'reviews': [dict(slug='cedar-merge-order', verdict='pass', reason='Supported.')]})
    attempt._call = Mock(side_effect=[LLMResult(provider, text, True, None, 'native')
                                     for text in (invalid, valid, review)])
    resolved = attempt._drafted_with_retries(provider, attempt._actions(provider))
    assert resolved is not None
    retry_prompt = attempt._call.call_args_list[1].args[1]
    assert json.dumps(invalid, ensure_ascii=False) in retry_prompt
    assert 'validation_error' in retry_prompt
    assert {'json': '0 JSON values of the expected shape',
            'related': 'related[0]: expected string',
            'category': 'category: value is not in enum'}[kind] in retry_prompt
    assert QUOTE in retry_prompt
    assert attempt._call.call_args_list[1].args[2] == cm.DRAFT_SYSTEM
    assert len(resolved.plan['operations']) == 1


def test_invalid_draft_cannot_be_replaced_by_unreviewed_empty_completion(vault):
    root, state = vault
    attempt = cm._CompileAttempt(_inputs(root), CompileCache(state), None, None)
    provider = _provider()
    actions = attempt._actions(provider)
    assert attempt._planned(provider, actions, _invalid_draft('related')) is None
    assert attempt._planned(provider, actions, '{"operations": []}') is None
    assert not list(attempt.cache.cache_dir.glob('*.json'))


def test_validation_feedback_does_not_replace_prior_supported_review_records(vault):
    root, state = vault
    attempt = cm._CompileAttempt(_inputs(root), CompileCache(state), None, None)
    provider = _provider()
    prior = json.dumps({'reviews': [{'slug': 'supported', 'verdict': 'pass'}]})
    attempt.critic_feedback = (prior,)
    assert attempt._planned(provider, attempt._actions(provider), _invalid_draft('json')) is None
    assert len(attempt.critic_feedback) == 2
    assert attempt.critic_feedback[0] == prior


def test_normalization_failure_also_returns_exact_diagnostic(vault, monkeypatch):
    root, state = vault
    attempt = cm._CompileAttempt(_inputs(root), CompileCache(state), None, None)
    provider = _provider()
    monkeypatch.setattr(cm, '_normalize_plan', Mock(side_effect=ValueError('conflicting target')))
    assert attempt._normalized(provider, attempt._actions(provider)[1], [_proposed()], 'normalize') is None
    record = json.loads(attempt.critic_feedback[0])
    assert record['validation_error'] == 'ValueError: conflicting target'
    assert json.loads(record['rejected_response']) == {'operations': [_proposed()]}
    assert not list(attempt.cache.cache_dir.glob('*.json'))


def test_invalid_response_is_budgeted_whole_before_another_call(vault):
    root, state = vault
    attempt = cm._CompileAttempt(_inputs(root), CompileCache(state), None, None)
    provider = _provider()
    invalid = _invalid_draft('related')
    assert attempt._planned(provider, attempt._actions(provider), invalid) is None
    attempt._fits = Mock(return_value=False)
    attempt._call = Mock(side_effect=AssertionError('Over-budget repair must not call provider'))
    assert attempt._drafted(provider, attempt._actions(provider)) is None
    assert json.dumps(invalid) in attempt._fits.call_args.args[0]
    attempt._call.assert_not_called()


def test_refused_draft_does_not_use_a_cached_empty_result(vault):
    root, state = vault
    attempt = cm._CompileAttempt(_inputs(root), CompileCache(state), None, None)
    provider = _provider()
    actions = attempt._actions(provider)
    attempt.cache.get = Mock(side_effect=AssertionError('Unresolved source work must bypass cached completion'))
    assert attempt._planned(provider, actions, _invalid_draft('related')) is None
    assert attempt._cached(actions, provider) is None
    attempt.cache.get.assert_not_called()


def test_draft_cache_identity_invalidates_answers_from_blind_retries():
    old_program = (
        'compile-draft/v19: explicit native ID/text pairs and validated critic feedback, with lossless source choices; '
        'with immutable original-entry context and derived-provenance claims')
    old_hash = cm.sha256_bytes(cm.canonical_json_bytes({
        'program': old_program, 'system': cm.DRAFT_SYSTEM, 'schema': cm.RAW_PLAN_SCHEMA}))
    assert cm.DRAFT_PROGRAM_HASH != old_hash
