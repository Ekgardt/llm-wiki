"""A supervised compile borrows its parent's lock without persisting ownership."""
import os
import time
from functools import partial

import maybe_compile
import memory_queue
import pytest

from tests.slow_machine import LONG_TIMEOUT
from tests.test_compile_call_controller import work as work
from tests.test_compile_leased_entrypoint import _configured_worker
from tests.test_compile_leased_entrypoint import runtime as runtime
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case


def _configured_lock_worker(queue, lease, deadline, task, *, lock_token):
    with pytest.MonkeyPatch.context() as patch:
        process = partial(memory_queue._manual_lease_processor, lock_token=lock_token)
        patch.setattr(memory_queue, '_manual_lease_processor', process)
        return _configured_worker(queue, lease, deadline, task)


def _spawn_with_token(work, token):
    processor = memory_queue.LeaseProcessor(partial(_configured_lock_worker, lock_token=token))
    deadline = time.monotonic() + LONG_TIMEOUT
    bound = memory_queue._bind_lease_processor(processor, work[0], work[1], deadline)
    return memory_queue._run_processor_child(bound, memory_queue._compat_task(work[1]),
                                             deadline - time.monotonic())


def test_child_borrows_parent_lock_and_leaves_it_owned(work, runtime):
    maybe_compile._write_lock(os.getpid())
    original = maybe_compile._read_lock()
    assert _spawn_with_token(work, original['owner']) is True
    assert maybe_compile._read_lock() == original
    assert (work[0].state_root / 'leased-call-counts').read_text().splitlines() == ['3']
    assert 'lock_token' not in work[0].get(work[1].id).payload
    work[0].publish_result(work[1], operation_id=work[1].id, result=b'complete')
    assert work[0].acknowledge(work[1]).state == 'succeeded'


@pytest.mark.parametrize('token', [None, 'another-owner'])
def test_child_without_parent_authority_never_calls_model(work, runtime, token):
    maybe_compile._write_lock(os.getpid())
    original = maybe_compile._read_lock()
    assert _spawn_with_token(work, token) is False
    assert maybe_compile._read_lock() == original
    assert (work[0].state_root / 'leased-call-counts').read_text().splitlines() == ['0']
