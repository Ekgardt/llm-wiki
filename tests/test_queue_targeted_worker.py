"""Targeted execution retains the existing parent-owned worker lifecycle."""
import time
from argparse import Namespace

import compile_memory
import memory_queue
import pytest

from tests.slow_machine import LONG_TIMEOUT
from tests.test_compile_call_controller import work as work
from tests.test_compile_leased_entrypoint import _configured_worker
from tests.test_compile_leased_entrypoint import runtime as runtime
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_queue_targeted_claim import _pair


def _run_target(queue, task_id, processor, runner=memory_queue._run_processor_child):
    defaults = memory_queue.DEFAULTS
    return memory_queue._work_one(
        queue, processor, runner, owner='foreground-worker', task_id=task_id,
        deadline=time.monotonic() + LONG_TIMEOUT, monotonic=time.monotonic,
        lease_seconds=defaults.queue_lease_seconds,
        heartbeat_seconds=defaults.queue_heartbeat_seconds,
        max_attempts=defaults.queue_max_attempts,
        retry_base_seconds=defaults.retry_base_seconds,
        retry_cap_seconds=defaults.retry_cap_seconds)


def _requested_query(task):
    return task['payload'] == {'query': 'requested'}


def test_targeted_worker_spawns_and_settles_only_the_requested_task(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    counts = _run_target(queue, target, _requested_query)
    assert counts['ok'] == 1
    assert queue.get(target).state == 'succeeded'
    assert queue.get(unrelated).attempts == 0


def test_unavailable_target_never_starts_a_processor(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    queue.cancel(target)
    counts = _run_target(queue, target, _requested_query,
                         lambda *args: pytest.fail('unavailable task must not execute'))
    assert counts == {'ok': 0, 'failed': 0, 'dead': 0, 'skipped': 0, 'halted': 0}
    assert queue.get(unrelated).attempts == 0


def test_targeted_parent_executes_requested_compile_and_requires_completion(work, runtime):
    queue = work[0]
    queue.cancel(work[1].id)
    unrelated = queue.enqueue('query', 1, {'unrelated': True}, priority=100)
    args = Namespace(file=None, closed_days_only=False, trigger='manual')
    target = queue.enqueue('compile', 1, compile_memory.compile_task_payload(args))
    counts = _run_target(queue, target, memory_queue.LeaseProcessor(_configured_worker))
    assert counts['ok'] == 1
    assert queue.get(target).state == 'succeeded'
    assert queue.get(unrelated).attempts == 0
    assert (queue.state_root / 'leased-call-counts').read_text().splitlines() == ['3']
