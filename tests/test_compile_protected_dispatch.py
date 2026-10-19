"""Only protected, prepared calls cross the durable compile dispatch boundary."""
from dataclasses import replace

import compile_controller as controller
import llm_client as client
import pytest
from context_budget import TokenUsage
from model_dlp import DLPPolicy, DLPPolicyError

from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    return replace(client.provider_candidates('fake')[0], model='test-model')


def _call(work, provider, **options):
    queue, lease, checkpoint = work
    return controller.run_candidate_call(
        queue, lease, checkpoint, dependency_digest='f' * 64,
        descriptor=provider, prompt='private-literal task', system_prompt='system', **options,
    )


def test_protected_answer_survives_restart_without_provider_repetition(work, provider, monkeypatch):
    calls = []
    monkeypatch.setattr(client, 'load_policy', lambda: DLPPolicy(literals=('private-literal',)))

    def backend(descriptor, prompt, system, schema):
        calls.append(prompt)
        return client.BackendResponse('safe answer', TokenUsage(input_tokens=11, output_tokens=2))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = _call(work, provider)
    second = _call(work, provider)
    assert first == second
    assert first.usage.input_tokens == 11
    assert len(calls) == 1
    assert 'private-literal' not in calls[0]


def test_input_policy_failure_creates_no_dispatch(work, provider, monkeypatch):
    def broken():
        raise DLPPolicyError('required policy changed')

    monkeypatch.setattr(client, 'load_policy', broken)
    result = _call(work, provider)
    assert result.failure_class == 'dlp_policy_error'
    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_timeout_blocks_instead_of_recording_completed_response(work, provider, monkeypatch):
    calls = []

    def backend(*args):
        calls.append(True)
        raise TimeoutError('potentially accepted upstream')

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(work, provider)
    queue, lease, checkpoint = work
    assert queue.get(lease.id).state == 'blocked'
    queue.unblock(lease.id)
    replacement = queue.claim('replacement')
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call((queue, replacement, checkpoint), provider)
    assert calls == [True]


def test_output_policy_refusal_keeps_cost_without_persisting_secret(work, provider, monkeypatch):
    monkeypatch.setattr(client, 'load_policy', lambda: DLPPolicy(literals=('private-literal',)))
    calls = []

    def backend(*args):
        calls.append(True)
        return client.BackendResponse('private-literal', TokenUsage(input_tokens=9, output_tokens=3))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = _call(work, provider)
    second = _call(work, provider)
    assert first == second
    assert first.failure_class == 'dlp_output_blocked'
    assert first.usage.output_tokens == 3
    queue, _, checkpoint = work
    saved = queue.read_compile_artifact(queue.get_compile_event(checkpoint.work_id))
    assert b'private-literal' not in saved
    assert calls == [True]


def test_changed_model_cannot_reuse_previous_answer(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    _call(work, provider)
    with pytest.raises(ValueError, match='identity'):
        _call(work, replace(provider, model='another-model'))


def test_changed_policy_cannot_reuse_answer_from_previous_policy(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    _call(work, provider)
    monkeypatch.setattr(client, 'load_policy', lambda: DLPPolicy(literals=('new-private-word',)))
    with pytest.raises(ValueError, match='identity'):
        _call(work, provider)


def test_implicit_model_refuses_before_reservation(work, provider):
    with pytest.raises(ValueError, match='explicit resolved model'):
        _call(work, replace(provider, model=None))
    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_unavailable_provider_creates_no_dispatch(work, provider, monkeypatch):
    monkeypatch.setattr(client, 'probe_candidate', lambda descriptor: False)
    result = _call(work, provider)
    assert result.failure_class == 'unavailable'
    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id) == checkpoint


def test_changed_dependency_identity_cannot_reuse_answer(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    _call(work, provider)
    queue, lease, checkpoint = work
    with pytest.raises(ValueError, match='identity'):
        controller.run_candidate_call(
            queue, lease, checkpoint, dependency_digest='0' * 64,
            descriptor=provider, prompt='private-literal task', system_prompt='system',
        )


def test_empty_completed_answer_retains_usage_and_is_not_called_again(work, provider, monkeypatch):
    calls = []

    def backend(*args):
        calls.append(True)
        return client.BackendResponse('', TokenUsage(input_tokens=8, output_tokens=0))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = _call(work, provider)
    second = _call(work, provider)
    assert first == second
    assert first.failure_class == 'empty_response'
    assert second.usage.input_tokens == 8
    assert calls == [True]


def test_replay_reads_earlier_answer_after_later_calls(work, provider, monkeypatch):
    calls = []

    def backend(descriptor, prompt, system, schema):
        calls.append(prompt)
        return client.BackendResponse(prompt, TokenUsage(input_tokens=1, output_tokens=1))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = controller.CompileReplay(*work, dependency_digest='f' * 64)
    assert first(provider, 'draft', 'system').text == 'draft'
    assert first(provider, 'review', 'system').text == 'review'
    resumed = controller.CompileReplay(*work, dependency_digest='f' * 64)
    assert resumed(provider, 'draft', 'system').text == 'draft'
    assert resumed(provider, 'review', 'system').text == 'review'
    assert calls == ['draft', 'review']


def test_replay_refuses_changed_order_before_another_call(work, provider, monkeypatch):
    calls = []

    def backend(*args):
        calls.append(True)
        return 'answer'

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = controller.CompileReplay(*work, dependency_digest='f' * 64)
    first(provider, 'draft', 'system')
    first(provider, 'review', 'system')
    resumed = controller.CompileReplay(*work, dependency_digest='f' * 64)
    with pytest.raises(ValueError, match='identity'):
        resumed(provider, 'different first request', 'system')
    assert len(calls) == 2


@pytest.fixture
def compiler_case(work, monkeypatch):
    import compile_memory as compiler

    from tests.test_compile_critic_drop_cannot_complete_source_work import _inputs

    queue, _, _ = work
    root = queue.vault
    state = queue.db_path.parent.parent
    for relative in ('knowledge/daily/receipts', 'knowledge/notes'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in ('knowledge/index.md', 'knowledge/log.md', 'AGENTS.md'):
        (root / relative).write_text('# Test snapshot\n')
    paths = {'ROOT': root, 'STATE_ROOT': state, 'MEMORY': root / 'knowledge',
             'DAILY_DIR': root / 'knowledge/daily', 'KNOWLEDGE': root / 'knowledge/notes',
             'INDEX': root / 'knowledge/index.md', 'LOG': root / 'knowledge/log.md',
             'AGENTS': root / 'AGENTS.md'}
    for name, value in paths.items():
        monkeypatch.setattr(compiler, name, value)
    return compiler, _inputs(root), state


def test_real_compile_validation_resumes_saved_draft_without_new_draft_call(
    work, compiler_case, monkeypatch,
):
    import json

    from compile_cache import CompileCache
    from markdown_transaction import active_markdown_coordinator

    from tests.test_compile_critic_drop_cannot_complete_source_work import _proposed
    from tests.test_the_compile_decides_what_the_snapshot_already_knows import (
        _provider,
        _source_review,
    )

    compiler, inputs, state = compiler_case
    provider = _provider()
    queue, _, _ = work
    coordinator = active_markdown_coordinator(queue.vault, state)
    monkeypatch.setattr(compiler, '_compile_candidate_chain', lambda batch: [provider])
    monkeypatch.setattr(compiler, 'probe_candidate', lambda descriptor: True)
    replies = iter((json.dumps({'operations': [_proposed()]}),
                    json.dumps({'reviews': [{'slug': 'cedar-merge-order', 'verdict': 'pass',
                                             'reason': 'Supported by source.'}]}), _source_review()))
    calls = []

    def backend(*args):
        calls.append(True)
        return client.BackendResponse(next(replies), TokenUsage(input_tokens=7, output_tokens=3))

    def interrupted(*args):
        raise SystemExit('process stopped after durable draft answer, before validation')

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    with monkeypatch.context() as patch:
        patch.setattr(compiler._CompileAttempt, '_planned', interrupted)
        with pytest.raises(SystemExit):
            compiler.resolve_compile_plan(
                inputs, CompileCache(state), coordinator=coordinator,
                call_runner=controller.CompileReplay(*work, dependency_digest='f' * 64),
            )
    result = compiler.resolve_compile_plan(
        inputs, CompileCache(state), coordinator=coordinator,
        call_runner=controller.CompileReplay(*work, dependency_digest='f' * 64),
    )
    assert result.plan['operations'][0]['path'] == 'knowledge/notes/cedar-merge-order.md'
    assert len(calls) == 3  # One draft, operation review and source-completeness review.


def test_preflight_refusal_during_replay_does_not_skip_saved_call(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    controller.CompileReplay(*work, dependency_digest='f' * 64)(provider, 'draft', 'system')
    resumed = controller.CompileReplay(*work, dependency_digest='f' * 64)
    monkeypatch.setattr(client, 'probe_candidate', lambda descriptor: False)
    result = resumed(provider, 'draft', 'system')
    assert result.failure_class == 'unavailable'
    assert resumed.checkpoint == work[2]


def test_replay_historical_answer_still_requires_current_lease(work, provider, monkeypatch):
    monkeypatch.setitem(client._BACKENDS, 'fake', lambda *args: 'answer')
    first = controller.CompileReplay(*work, dependency_digest='f' * 64)
    first(provider, 'draft', 'system')
    first(provider, 'review', 'system')
    queue, lease, checkpoint = work
    import memory_queue

    stale = controller.CompileReplay(queue, replace(lease, token='stale'), checkpoint,
                                    dependency_digest='f' * 64)
    with pytest.raises(memory_queue.LeaseFenceError):
        stale(provider, 'draft', 'system')


def test_redriven_replay_reuses_all_saved_calls(work, provider, monkeypatch):
    import memory_queue

    calls = []

    def backend(*args):
        calls.append(True)
        return 'answer'

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    first = controller.CompileReplay(*work, dependency_digest='f' * 64)
    first(provider, 'draft', 'system')
    first(provider, 'review', 'system')
    queue, lease, checkpoint = work
    queue.fail(lease, memory_queue.QueueFailure('handler_failed', permanent=True))
    queue.redrive(lease.id)
    replacement = queue.claim('redriven')
    resumed = controller.CompileReplay(queue, replacement, checkpoint, dependency_digest='f' * 64)
    resumed(provider, 'draft', 'system')
    resumed(provider, 'review', 'system')
    assert calls == [True, True]


def test_historical_read_checks_sequence_and_original_digest(work):
    import compile_progress

    queue, _, checkpoint = work
    assert queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence) == checkpoint
    assert queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 1) is None
    with pytest.raises(ValueError, match='nonnegative'):
        queue.get_compile_event(checkpoint.work_id, sequence=-1)
    with queue.connection() as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
        database.execute('UPDATE compile_work_events SET details_json=? WHERE sequence=?',
                         ('{}', checkpoint.sequence))
    with pytest.raises(compile_progress.ProgressCorrupt):
        queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence)
