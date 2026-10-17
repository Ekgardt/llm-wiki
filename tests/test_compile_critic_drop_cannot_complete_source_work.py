"""A rejected draft is not proof that its source has no durable information."""
import json
from unittest.mock import Mock

import compile_memory as cm
import pytest
from compile_cache import CompileCache
from llm_client import LLMResult

from tests.test_the_compile_decides_what_the_snapshot_already_knows import _operation, _provider
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault

QUOTE = 'Project Cedar merged change C after changes A and B.'


def _inputs(root):
    path = root / 'knowledge/daily/2026-07-14.md'
    path.write_text('## [10:00:00] session-end | manual\n' + QUOTE + '\n')
    return cm.snapshot_compile_inputs([path])


def _proposed(slug='cedar-merge-order'):
    operation = _operation('create', slug)
    operation['summary'] = 'A project-specific merge order was observed.'
    operation['body_markdown'] = 'Project Cedar merged change C after changes A and B.'
    operation['evidence'][0].update(quoted_text=QUOTE, claim=QUOTE)
    return operation


def _attempt(inputs, state, operations, verdicts):
    provider = _provider()
    attempt = cm._CompileAttempt(inputs, CompileCache(state), None, None)
    response = json.dumps({'reviews': [dict(slug=item['slug'], verdict=verdict, reason='Review source-specific scope.')
                                      for item, verdict in zip(operations, verdicts)]})
    attempt._call = Mock(return_value=LLMResult(provider, response, True, None, 'native'))
    cm._critique_prompt(inputs, operations)
    return attempt, provider


def test_dropped_source_bound_operation_is_not_an_accepted_empty_plan(vault, capsys):
    root, state = vault
    inputs = _inputs(root)
    operation = _proposed()
    operation['body_markdown'] = 'Every project must apply this reusable merge order.'
    attempt, provider = _attempt(inputs, state, [operation], ['drop'])
    resolved = attempt._critiqued(provider, attempt._actions(provider), [operation])
    assert resolved is None
    assert not list(attempt.cache.cache_dir.glob('*.json'))
    assert 'source work remains unresolved' in capsys.readouterr().err


def test_a_mixed_pass_and_drop_cannot_complete_the_related_source(vault):
    root, state = vault
    inputs = _inputs(root)
    operations = [_proposed(), _proposed('cedar-unproven-generalization')]
    attempt, provider = _attempt(inputs, state, operations, ['pass', 'drop'])
    resolved = attempt._critiqued(provider, attempt._actions(provider), operations)
    assert resolved is None
    assert not list(attempt.cache.cache_dir.glob('*.json'))
    assert len(operations) == 2


def test_same_evidence_with_distinct_supported_facts_is_not_auto_dropped(vault):
    root, state = vault
    inputs = _inputs(root)
    operations = [_proposed(), _proposed('cedar-change-c-predecessors')]
    operations[1]['summary'] = 'Changes A and B preceded change C in this project.'
    attempt, provider = _attempt(inputs, state, operations, ['pass', 'pass'])
    resolved = attempt._critiqued(provider, attempt._actions(provider), operations)
    assert resolved is not None
    assert len(resolved.plan['operations']) == 2
    assert len(operations) == 2


def test_a_direct_empty_draft_still_needs_no_critic(vault):
    root, state = vault
    inputs = _inputs(root)
    attempt = cm._CompileAttempt(inputs, CompileCache(state), None, None)
    provider = _provider()
    attempt._call = Mock(side_effect=AssertionError('Empty draft must not call a critic'))
    resolved = attempt._critiqued(provider, attempt._actions(provider), [])
    assert resolved is not None
    assert resolved.plan['operations'] == []
    attempt._call.assert_not_called()


def test_existing_same_fact_and_quote_do_not_turn_drop_into_no_content_authority(vault):
    root, state = vault
    (root / 'knowledge/notes/existing-cedar-fact.md').write_text('---\ntype: pattern\n---\n# Existing fact\n' + QUOTE + '\n')
    inputs = _inputs(root)
    operation = _proposed()
    attempt, provider = _attempt(inputs, state, [operation], ['drop'])
    resolved = attempt._critiqued(provider, attempt._actions(provider), [operation])
    assert resolved is None
    assert not list(attempt.cache.cache_dir.glob('*.json'))


def test_dropped_operations_do_not_change_the_original_semantic_records(vault):
    root, state = vault
    inputs = _inputs(root)
    operations = [_proposed(), _proposed('cedar-specific-fact')]
    before = json.dumps(operations, sort_keys=True)
    attempt, provider = _attempt(inputs, state, operations, ['drop', 'pass'])
    assert attempt._critiqued(provider, attempt._actions(provider), operations) is None
    assert json.dumps(operations, sort_keys=True) == before


def test_wrong_evidence_still_fails_before_critic(vault):
    root, state = vault
    inputs = _inputs(root)
    operation = _proposed()
    operation['evidence'][0]['quoted_text'] = 'This text does not occur in the source.'
    attempt = cm._CompileAttempt(inputs, CompileCache(state), None, None)
    attempt._call = Mock(side_effect=AssertionError('Invalid evidence must not be reviewed'))
    with pytest.raises(ValueError):
        cm._critique_prompt(inputs, [operation])
    attempt._call.assert_not_called()


def test_normal_batch_never_publishes_or_receipts_a_critique_drop(vault, monkeypatch):
    import math
    from types import SimpleNamespace

    from markdown_transaction import MarkdownCoordinator

    from tests.test_the_compile_decides_what_the_snapshot_already_knows import _replies

    root, state = vault
    inputs = _inputs(root)
    operation = _proposed()
    draft = json.dumps({'operations': [operation], 'audit': {}})
    review = json.dumps({'reviews': [{'slug': operation['slug'], 'verdict': 'drop', 'reason': 'Scope is not supported.'}]})
    prompts = _replies(monkeypatch, [draft, review] * (cm.VALIDATION_RETRIES + 1))
    apply = Mock(side_effect=AssertionError('Rejected source work must not reach publication'))
    monkeypatch.setattr(cm, '_apply_batch', apply)
    batch = cm.pack_compile_batches(inputs, model=None)[0]
    result = cm._run_batch(batch, SimpleNamespace(dry_run=False, trigger='manual'),
                           coordinator=MarkdownCoordinator(root, state), deadline=math.inf,
                           cancelled=None, owner=None)
    assert result.status != 0
    assert len(prompts) == 2 * (cm.VALIDATION_RETRIES + 1)
    apply.assert_not_called()
    assert not list((root / 'knowledge/daily/receipts').iterdir())
    assert not list((root / 'knowledge/notes').iterdir())
    assert (root / 'knowledge/daily/2026-07-14.md').read_bytes() == inputs.dailies[0].content


def test_draft_keeps_specific_project_facts_without_inventing_reusable_rules(vault):
    root, _state = vault
    inputs = _inputs(root)
    prompt = cm._draft_prompt_text(inputs)
    assert 'Keep durable evidenced project facts scoped;' in prompt
    assert 'invent no reusable rules.' in prompt
    assert QUOTE in prompt
