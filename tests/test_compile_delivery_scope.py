"""One uncertain call must not abandon independent work under the same lease."""
from dataclasses import replace

import compile_controller as controller
import memory_queue
import pytest
from llm_client import BackendResponse

from tests.test_compile_call_controller import _source_scope
from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded


def _independent(work):
    queue, lease, first = work
    scope = _source_scope('knowledge/daily/2026-10-21.md', b'independent')
    root = replace(first, work_id='b' * 64, sequence=0, stage='pending',
                   details={'source_scope': scope})
    queue.record_compile_event(lease, root)
    return queue.save_compile_artifact(lease, replace(root, sequence=1, stage='snapshot_ready'), b'independent')


def _uncertain():
    raise TimeoutError('provider may have accepted this call')


def _forbidden():
    pytest.fail('uncertain model call repeated')


def _call(delivery, checkpoint, invoke):
    return delivery.run(checkpoint, request_digest='e' * 64, invoke=invoke)


def test_unknown_call_preserves_lease_for_independent_work_but_blocks_final_success(work):
    queue, lease, first = work
    second = _independent(work)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        with controller.CompileDelivery(queue, lease) as delivery:
            with pytest.raises(controller.ProviderOutcomeUnknown):
                _call(delivery, first, _uncertain)
            assert queue.get(lease.id).state == 'leased'
            assert _call(delivery, second, lambda: BackendResponse('independent answer')).text == 'independent answer'
            with pytest.raises(controller.ProviderOutcomeUnknown):
                _call(delivery, first, _forbidden)
    assert queue.get(lease.id).blocked_capability == 'provider_outcome_unknown'
    assert queue.get_compile_event(first.work_id).details['model_call']['phase'] == 'dispatch'
    assert queue.get_compile_event(second.work_id).details['model_call']['phase'] == 'answer'


def test_successful_delivery_does_not_finish_queue_task_before_publication(work):
    queue, lease, first = work
    with controller.CompileDelivery(queue, lease) as delivery:
        assert _call(delivery, first, lambda: BackendResponse('answer')).text == 'answer'
    assert queue.get(lease.id).state == 'leased'
    assert queue.get(lease.id).result_reference is None


def test_delivery_calls_require_live_context(work):
    queue, lease, first = work
    delivery = controller.CompileDelivery(queue, lease)
    with pytest.raises(ValueError, match='delivery.*active'):
        _call(delivery, first, _forbidden)
    with delivery:
        pass
    with pytest.raises(ValueError, match='delivery.*active'):
        _call(delivery, first, _forbidden)
    with pytest.raises(ValueError, match='delivery.*entered'):
        with delivery:
            pytest.fail('delivery context reused')


def test_lost_lease_cannot_dispatch_independent_work(work):
    queue, lease, first = work
    with controller.CompileDelivery(queue, lease) as delivery:
        queue.cancel(lease.id)
        with pytest.raises(memory_queue.LeaseFenceError):
            _call(delivery, first, _forbidden)


def test_redelivery_does_not_repeat_unknown_or_completed_calls(work):
    queue, lease, first = work
    second = _independent(work)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        with controller.CompileDelivery(queue, lease) as delivery:
            with pytest.raises(controller.ProviderOutcomeUnknown):
                _call(delivery, first, _uncertain)
            _call(delivery, second, lambda: BackendResponse('saved'))
    queue.unblock(lease.id)
    replacement = queue.claim('resume-independent')
    with pytest.raises(controller.ProviderOutcomeUnknown):
        with controller.CompileDelivery(queue, replacement) as delivery:
            with pytest.raises(controller.ProviderOutcomeUnknown):
                _call(delivery, first, _forbidden)
            assert _call(delivery, second, _forbidden).text == 'saved'
    assert queue.get(lease.id).state == 'blocked'


@pytest.mark.parametrize('field,value', [('token', 'wrong'), ('owner', 'other'), ('input_hash', 'a' * 64)])
def test_scope_cannot_be_used_with_another_owner(work, field, value):
    queue, lease, first = work
    with controller.CompileDelivery(queue, lease) as delivery:
        with pytest.raises(ValueError, match='another owner'):
            controller.run_model_call(queue, replace(lease, **{field: value}), first,
                                      request_digest='e' * 64, invoke=_forbidden, delivery=delivery)
    assert queue.get_compile_event(first.work_id) == first


def test_scope_accepts_the_same_renewed_lease(work):
    queue, lease, first = work
    with controller.CompileDelivery(queue, lease) as delivery:
        renewed = queue.heartbeat(lease)
        response = controller.run_model_call(queue, renewed, first, request_digest='e' * 64,
                                             invoke=lambda: BackendResponse('renewed'), delivery=delivery)
    assert response.text == 'renewed'


def test_protected_replay_uses_scope_without_losing_dlp_or_usage(work, monkeypatch):
    import llm_client as client
    from context_budget import TokenUsage
    from model_dlp import DLPPolicy

    from tests.test_compile_protected_dispatch import _dependencies

    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    provider = replace(client.provider_candidates('fake')[0], model='test-model')
    monkeypatch.setattr(client, 'load_policy', lambda: DLPPolicy(literals=('private-literal',)))
    queue, lease, first = work
    second = _independent(work)
    calls = []

    def backend(_descriptor, prompt, _system, _schema):
        calls.append(prompt)
        if len(calls) == 1:
            raise TimeoutError('unknown upstream outcome')
        return BackendResponse('safe answer', TokenUsage(input_tokens=12, output_tokens=3))

    monkeypatch.setitem(client._BACKENDS, 'fake', backend)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        with controller.CompileDelivery(queue, lease) as delivery:
            first_replay = controller.CompileReplay(queue, lease, first, dependencies=_dependencies(), delivery=delivery)
            second_replay = controller.CompileReplay(queue, lease, second, dependencies=_dependencies(), delivery=delivery)
            with pytest.raises(controller.ProviderOutcomeUnknown):
                first_replay(provider, 'private-literal uncertain', 'system')
            result = second_replay(provider, 'private-literal independent', 'system')
            assert result.usage.input_tokens == 12
            assert first_replay.checkpoint == first
            assert second_replay.checkpoint.sequence > second.sequence
    assert len(calls) == 2
    assert all('private-literal' not in prompt for prompt in calls)
