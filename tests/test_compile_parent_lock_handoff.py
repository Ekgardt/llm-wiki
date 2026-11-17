"""A supervised compile borrows its parent's lock without persisting ownership."""
import os
import time
from functools import partial

import compile_lock
import memory_queue
import pytest
import queue_core

from tests.compile_call_controller_helpers import work as work
from tests.compile_leased_entrypoint_helpers import _configured_worker
from tests.compile_leased_entrypoint_helpers import runtime as runtime
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.slow_machine import LONG_TIMEOUT


def _configured_lock_worker(queue, lease, deadline, task, *, lock_token):
    with pytest.MonkeyPatch.context() as patch:
        process = partial(memory_queue.manual_lease_processor, lock_token=lock_token)
        patch.setattr(memory_queue, 'manual_lease_processor', process)
        return _configured_worker(queue, lease, deadline, task)


def _spawn_with_token(work, token):
    processor = queue_core.LeaseProcessor(partial(_configured_lock_worker, lock_token=token))
    deadline = time.monotonic() + LONG_TIMEOUT
    bound = queue_core.bind_lease_processor(processor, work[0], work[1], deadline)
    return queue_core.run_processor_child(bound, queue_core.compat_task(work[1]),
                                             deadline - time.monotonic())


def test_child_borrows_parent_lock_and_leaves_it_owned(work, runtime):
    compile_lock.write_lock(os.getpid())
    original = compile_lock.read_lock()
    assert _spawn_with_token(work, original['owner']) is True
    assert compile_lock.read_lock() == original
    assert (work[0].state_root / 'leased-call-counts').read_text().splitlines() == ['3']
    assert 'lock_token' not in work[0].get(work[1].id).payload
    work[0].publish_result(work[1], operation_id=work[1].id, result=b'complete')
    assert work[0].acknowledge(work[1]).state == 'succeeded'


@pytest.mark.parametrize('token', [None, 'another-owner'])
def test_child_without_parent_authority_never_calls_model(work, runtime, token):
    compile_lock.write_lock(os.getpid())
    original = compile_lock.read_lock()
    assert _spawn_with_token(work, token) is False
    assert compile_lock.read_lock() == original
    assert (work[0].state_root / 'leased-call-counts').read_text().splitlines() == ['0']
