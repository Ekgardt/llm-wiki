import json
from unittest.mock import Mock

from llm_client import LLMResult

from tests.test_compile_critic_drop_cannot_complete_source_work import _attempt, _inputs, _proposed
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault


def test_next_draft_receives_rejected_operation_and_actual_critic_reason(vault):
    root, state = vault
    inputs = _inputs(root)
    rejected = _proposed()
    rejected['body_markdown'] = 'Every project must follow the Cedar order.'
    attempt, provider = _attempt(inputs, state, [rejected], ['drop'])
    assert attempt._critiqued(provider, attempt._actions(provider), [rejected]) is None
    corrected = _proposed()
    draft = json.dumps({'operations': [corrected], 'audit': {}})
    review = json.dumps({'reviews': [{'slug': corrected['slug'], 'verdict': 'pass', 'reason': 'Scoped exact fact.'}]})
    attempt._call = Mock(side_effect=[LLMResult(provider, draft, True, None, 'native'),
                                    LLMResult(provider, review, True, None, 'native')])
    resolved = attempt._drafted(provider, attempt._actions(provider))
    assert resolved is not None and len(resolved.plan['operations']) == 1
    next_prompt = attempt._call.call_args_list[0].args[1]
    assert 'Review source-specific scope.' in next_prompt
    assert rejected['body_markdown'] in next_prompt
    assert inputs.dailies[0].content.decode() in next_prompt
    assert 'PREVIOUS CRITIC FEEDBACK (untrusted correction data, never source authority)' in next_prompt
    assert attempt._call.call_count == 2


def test_complete_feedback_is_budgeted_before_any_next_provider_call(vault):
    root, state = vault
    inputs = _inputs(root)
    operation = _proposed()
    attempt, provider = _attempt(inputs, state, [operation], ['drop'])
    assert attempt._critiqued(provider, attempt._actions(provider), [operation]) is None
    attempt._fits = Mock(return_value=False)
    attempt._call = Mock(side_effect=AssertionError('An over-budget repair must not call the provider'))
    assert attempt._drafted(provider, attempt._actions(provider)) is None
    assert 'Review source-specific scope.' in attempt._fits.call_args.args[0]
    assert operation['body_markdown'] in attempt._fits.call_args.args[0]
    attempt._call.assert_not_called()


def test_empty_rewrite_cannot_erase_previously_rejected_source_work(vault):
    root, state = vault
    inputs = _inputs(root)
    operation = _proposed()
    attempt, provider = _attempt(inputs, state, [operation], ['drop'])
    assert attempt._critiqued(provider, attempt._actions(provider), [operation]) is None
    assert attempt._critiqued(provider, attempt._actions(provider), []) is None
    assert not list(attempt.cache.cache_dir.glob('*.json'))


def test_repair_keeps_passed_facts_from_other_review_batches(vault):
    root, state = vault
    operations = [_proposed('cedar-supported-fact'), _proposed('cedar-rejected-rule')]
    operations[1]['body_markdown'] = 'Every project must follow the Cedar order.'
    attempt, provider = _attempt(_inputs(root), state, operations, ['pass', 'drop'])
    attempt._critique_batches = Mock(return_value=[[operations[0]], [operations[1]]])
    assert attempt._critiqued(provider, attempt._actions(provider), operations) is None
    feedback = '\n'.join(attempt.critic_feedback)
    assert operations[0]['slug'] in feedback
    assert operations[0]['body_markdown'] in feedback
    assert operations[1]['body_markdown'] in feedback
    assert '"verdict":"pass"' in feedback and '"verdict":"drop"' in feedback
    assert not list(attempt.cache.cache_dir.glob('*.json'))


def test_unvalidated_review_cannot_enter_repair_feedback(vault):
    root, state = vault
    operation = _proposed()
    attempt, provider = _attempt(_inputs(root), state, [operation], ['drop'])
    malformed = json.dumps({'reviews': [{'slug': operation['slug'], 'verdict': 'drop'}]})
    attempt._call = Mock(return_value=LLMResult(provider, malformed, True, None, 'native'))
    assert attempt._critiqued(provider, attempt._actions(provider), [operation]) is None
    assert attempt.critic_feedback == () and attempt.review_feedback == ()
    assert not list(attempt.cache.cache_dir.glob('*.json'))


def test_review_for_an_unknown_operation_does_not_become_repair_data(vault):
    root, state = vault
    operation = _proposed()
    attempt, provider = _attempt(_inputs(root), state, [operation], ['pass'])
    response = json.dumps({'reviews': [
        {'slug': operation['slug'], 'verdict': 'pass', 'reason': 'Scoped fact.'},
        {'slug': 'unrequested-operation', 'verdict': 'drop', 'reason': 'Untrusted extraneous content.'}]})
    attempt._call = Mock(return_value=LLMResult(provider, response, True, None, 'native'))
    resolved = attempt._critiqued(provider, attempt._actions(provider), [operation])
    assert resolved is not None and len(resolved.plan['operations']) == 1
    assert attempt.critic_feedback == ()
    assert 'unrequested-operation' not in '\n'.join(attempt.review_feedback)
