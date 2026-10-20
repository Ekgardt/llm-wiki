"""Caller cancellation stops owned work without erasing uncertain model dispatch."""
import multiprocessing
import sqlite3
import threading
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import compile_controller as controller
import memory_queue as queue_module
import pytest

from tests.slow_machine import LONG_TIMEOUT
from tests.test_compile_call_controller import work as work
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_queue_targeted_claim import _pair


def _forbidden(*args, **kwargs):
    pytest.fail('cancelled or expired work must not execute')


def _claim(queue, *, deadline, cancelled=None):
    options = {}
    if cancelled is not None:
        options['cancelled'] = cancelled
    return queue_module._claim_when_reachable(queue, owner='test', lease_seconds=120,
        max_attempts=queue_module.DEFAULTS.queue_max_attempts, deadline=deadline,
        monotonic=time.monotonic, **options)


def test_expired_claim_does_not_touch_queue():
    assert _claim(SimpleNamespace(claim=_forbidden), deadline=0) is None


def test_cancelled_claim_does_not_touch_queue():
    assert _claim(SimpleNamespace(claim=_forbidden), deadline=float('inf'),
                  cancelled=lambda: True) is None


def test_cancellation_during_busy_claim_prevents_another_attempt():
    calls = []

    def busy(*args, **kwargs):
        calls.append(True)
        raise sqlite3.OperationalError('database is locked')

    assert _claim(SimpleNamespace(claim=busy), deadline=time.monotonic() + LONG_TIMEOUT,
                  cancelled=lambda: bool(calls)) is None
    assert calls == [True]


def _wait_after_marker(marker, task):
    Path(marker).write_text('started')
    threading.Event().wait()
    return True


def test_cancelled_child_never_allocates_process(monkeypatch):
    monkeypatch.setattr(multiprocessing, 'get_context', _forbidden)
    with pytest.raises(queue_module.QueueOperationError, match='worker_cancelled'):
        queue_module._run_processor_child(_forbidden, {}, LONG_TIMEOUT, cancelled=lambda: True)


def test_running_child_is_stopped_when_caller_cancels(tmp_path):
    marker = tmp_path / 'started'
    before = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(queue_module.QueueOperationError, match='worker_cancelled'):
        queue_module._run_processor_child(partial(_wait_after_marker, str(marker)), {},
                                         LONG_TIMEOUT, cancelled=marker.exists)
    assert marker.read_text() == 'started'
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_cancel_callback_does_not_replace_lease_loss_check():
    heartbeat = SimpleNamespace(error=None)
    runner = queue_module._interruptible(queue_module._run_processor_child, heartbeat,
                                         cancelled=lambda: True)
    assert runner.keywords['cancelled']() is True
    assert runner.keywords['lease_lost']() is False
    heartbeat.error = RuntimeError('lost')
    assert runner.keywords['lease_lost']() is True


def test_public_worker_propagates_cancellation_and_blocks_task(tmp_path, monkeypatch):
    queue, target, unrelated = _pair(tmp_path)
    queue.cancel(unrelated)
    monkeypatch.setattr(queue_module, '_queue', lambda **kwargs: queue)
    marker = tmp_path / 'started'
    summary = queue_module.run_worker(partial(_wait_after_marker, str(marker)),
        max_tasks=1, max_seconds=LONG_TIMEOUT, cancelled=marker.exists)
    assert summary.failed == 1
    task = queue.get(target)
    assert (task.state, task.blocked_capability) == ('blocked', 'caller_cancelled')
    assert queue.claim('later') is None


def _accepted_call(marker):
    Path(marker).write_text('accepted')
    threading.Event().wait()


def _dispatched_child(queue, lease, deadline, task):
    checkpoint = queue.get_compile_event('c' * 64, sequence=1)
    return controller.run_model_call(queue, lease, checkpoint, request_digest='e' * 64,
        invoke=partial(_accepted_call, str(queue.state_root / 'accepted')))


def test_cancel_after_dispatch_blocks_automatic_repeat_even_after_unblock(work):
    queue, lease, checkpoint = work
    marker = queue.state_root / 'accepted'
    counts = {'ok': 0, 'failed': 0, 'dead': 0, 'skipped': 0, 'halted': 0}
    defaults = queue_module.DEFAULTS
    queue_module._run_and_settle(queue, queue_module.LeaseProcessor(_dispatched_child),
        queue_module._run_processor_child, lease, counts,
        deadline=time.monotonic() + LONG_TIMEOUT, monotonic=time.monotonic,
        lease_seconds=defaults.queue_lease_seconds, heartbeat_seconds=defaults.queue_heartbeat_seconds,
        max_attempts=defaults.queue_max_attempts, retry_base_seconds=defaults.retry_base_seconds,
        retry_cap_seconds=defaults.retry_cap_seconds, cancelled=marker.exists)
    assert counts['failed'] == 1
    assert queue.get(lease.id).blocked_capability == 'caller_cancelled'
    queue.unblock(lease.id)
    replacement = queue.claim('resumed')
    with pytest.raises(controller.ProviderOutcomeUnknown):
        controller.run_model_call(queue, replacement, checkpoint, request_digest='e' * 64,
                                  invoke=_forbidden)
    assert queue.get(lease.id).blocked_capability == 'provider_outcome_unknown'


def test_targeted_worker_cancels_before_claim_without_spending_attempt(tmp_path):
    queue, target, unrelated = _pair(tmp_path)
    defaults = queue_module.DEFAULTS
    counts = queue_module._work_one(queue, _forbidden, _forbidden, owner='cancelled',
        task_id=target, cancelled=lambda: True, deadline=float('inf'), monotonic=time.monotonic,
        lease_seconds=defaults.queue_lease_seconds, heartbeat_seconds=defaults.queue_heartbeat_seconds,
        max_attempts=defaults.queue_max_attempts, retry_base_seconds=defaults.retry_base_seconds,
        retry_cap_seconds=defaults.retry_cap_seconds)
    assert counts == {'ok': 0, 'failed': 0, 'dead': 0, 'skipped': 0, 'halted': 0}
    assert queue.get(target).attempts == 0
    assert queue.get(unrelated).attempts == 0


def test_cancellation_interrupts_exit_wait_without_discarding_received_result():
    stopped = []
    process = SimpleNamespace(is_alive=lambda: True, join=_forbidden)
    run = queue_module._ChildRun(process, None, time.monotonic() + LONG_TIMEOUT,
                                  cancelled=lambda: True)
    run.stop = lambda: stopped.append(True)
    queue_module._settle_child_exit(run)
    assert stopped == [True]
