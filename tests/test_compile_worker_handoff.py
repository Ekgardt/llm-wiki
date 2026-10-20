"""The existing spawned worker can retain canonical compile lease authority."""
import time
from dataclasses import replace
from functools import partial

import compile_controller as controller
import memory_queue
import pytest
from context_budget import TokenUsage
from llm_client import BackendResponse

from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded


def _invoke(queue, mode):
    with (queue.state_root / 'provider-called').open('x') as marker:
        marker.write('one')
    if mode == 'unknown':
        raise TimeoutError('provider accepted; response unavailable')
    return BackendResponse('saved answer', TokenUsage(input_tokens=123, output_tokens=17))


def _child_compile(mode, queue, lease, deadline, task):
    assert task == memory_queue._compat_task(lease)
    assert time.monotonic() < deadline
    checkpoint = queue.get_compile_event('c' * 64, sequence=1)
    try:
        answer = controller.run_model_call(queue, lease, checkpoint, request_digest='e' * 64,
            invoke=partial(_invoke, queue, mode))
    except (controller.ProviderOutcomeUnknown, memory_queue.LeaseFenceError):
        return False
    return answer.text == 'saved answer'


def _spawn(work, mode):
    queue, lease, _ = work
    # Existing worker child tests use this allowance for hosted-platform spawn.
    deadline = time.monotonic() + 60
    processor = memory_queue.LeaseProcessor(partial(_child_compile, mode))
    bound = memory_queue._bind_lease_processor(processor, queue, lease, deadline)
    return memory_queue._run_processor_child(bound, memory_queue._compat_task(lease),
        deadline - time.monotonic())


def test_separate_process_recovers_answer_without_another_invocation(work):
    assert _spawn(work, 'answer') is True
    assert _spawn(work, 'answer') is True
    assert (work[0].state_root / 'provider-called').read_text() == 'one'
    assert work[0].get(work[1].id).state == 'leased'


def test_stale_transferred_lease_cannot_invoke_provider(work):
    queue, lease, checkpoint = work
    assert _spawn((queue, replace(lease, token='stale'), checkpoint), 'answer') is False
    assert not (queue.state_root / 'provider-called').exists()


def test_unknown_child_outcome_survives_unblock_without_repeat(work):
    queue, lease, checkpoint = work
    assert _spawn(work, 'unknown') is False
    assert queue.get(lease.id).blocked_capability == 'provider_outcome_unknown'
    queue.unblock(lease.id)
    replacement = queue.claim('resumed-worker')
    assert _spawn((queue, replacement, checkpoint), 'answer') is False
    assert queue.get(lease.id).state == 'blocked'


@pytest.mark.parametrize('mode, state, ok, failed, capability', [
    ('unknown', 'blocked', 0, 1, 'provider_outcome_unknown'),
    ('answer', 'succeeded', 1, 0, None),
])
def test_worker_settlement_respects_controller_outcome(work, mode, state, ok, failed, capability):
    queue, lease, _ = work
    counts = {'ok': 0, 'failed': 0, 'dead': 0, 'skipped': 0, 'halted': 0}
    memory_queue._run_and_settle(queue,
        memory_queue.LeaseProcessor(partial(_child_compile, mode)),
        memory_queue._run_processor_child, lease, counts,
        deadline=time.monotonic() + 60, monotonic=time.monotonic,
        lease_seconds=memory_queue.DEFAULTS.queue_lease_seconds,
        heartbeat_seconds=memory_queue.DEFAULTS.queue_heartbeat_seconds,
        max_attempts=memory_queue.DEFAULTS.queue_max_attempts,
        retry_base_seconds=memory_queue.DEFAULTS.retry_base_seconds,
        retry_cap_seconds=memory_queue.DEFAULTS.retry_cap_seconds)
    assert counts['ok'] == ok
    assert counts['failed'] == failed
    assert queue.get(lease.id).state == state
    assert queue.get(lease.id).blocked_capability == capability
