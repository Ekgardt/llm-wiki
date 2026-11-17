"""The operator command that settles a compile whose answer is uncertain."""
from __future__ import annotations

import threading
from contextlib import closing, contextmanager
from functools import partial
from types import SimpleNamespace

import compile_progress
from llm_wiki.queue_contracts import LeaseFenceError, QueueOperationError
from llm_wiki.queue_heartbeat import LeaseHeartbeat


def operator_compile_recovery(queue, task_id, dispatch, resolve_call, policy, hold_owner):
    with _compile_recovery_owner(queue, task_id, hold_owner) as (owner, lost):
        _require_compile_recovery_owner(queue, owner, lost)
        queue.recover_expired_compile_recovery(task_id, dispatch)
        observed = queue.inspect_compile_progress(task_id)
        work = _inspected_compile_work(observed, dispatch.work_id)
        if not _needs_compile_recovery(observed['state'], work, dispatch):
            return {'outcome': 'not_blocked', 'task_state': observed['state'], 'work': work}
        return _owned_compile_recovery(queue, task_id, dispatch, resolve_call, policy, owner, lost)


def _needs_compile_recovery(state, work, dispatch):
    return state == 'blocked' or (state == 'ready' and work['sequence'] == dispatch.sequence)


def _inspected_compile_work(observed, work_id):
    for work in observed['works']:
        if work['work_id'] == work_id:
            return work
    raise QueueOperationError('compile_recovery_work_not_in_task')


@contextmanager
def _compile_recovery_owner(queue, task_id, hold_owner):
    lost = threading.Event()
    renewer = SimpleNamespace(heartbeat=queue.heartbeat_queue_owner)
    with queue.queue_owner(role='queue-operator', scope=f'compile-recovery:{task_id}') as owner:
        with hold_owner(owner, registry=renewer, lost=lost):
            yield owner, lost


def _require_compile_recovery_owner(queue, owner, lost):
    if lost.is_set():
        raise QueueOperationError('compile_recovery_owner_lost')
    registry = queue.ownership_registry()
    with closing(registry.connect()) as database:
        registry.require(database, owner)


def _owned_compile_recovery(queue, task_id, dispatch, resolve_call, policy, owner, lost):
    _require_compile_recovery_owner(queue, owner, lost)
    lease = queue.claim_uncertain_compile(
        owner.actor_id, task_id=task_id, work_id=dispatch.work_id,
        dispatch_digest=compile_progress.event_digest(dispatch),
        lease_seconds=policy.lease_seconds, max_attempts=policy.max_attempts)
    try:
        resolution, state = _heartbeat_compile_resolution(
            queue, lease, dispatch, resolve_call, policy, owner, lost)
    except BaseException:
        _abort_owned_compile_recovery(queue, lease, owner, lost)
        raise
    outcome, event = resolution
    return {'outcome': outcome, 'task_state': state, 'work': compile_progress.operator_summary(event)}


def _heartbeat_compile_resolution(queue, lease, dispatch, resolve_call, policy, owner, lost):
    heartbeat = LeaseHeartbeat(queue, lease, heartbeat_seconds=policy.heartbeat_seconds,
                                 lease_seconds=policy.lease_seconds)
    heartbeat.start()
    try:
        result = _finish_compile_recovery(queue, lease, dispatch, resolve_call, owner, lost)
    finally:
        heartbeat.stop()
    _require_settled_recovery_heartbeat(heartbeat)
    return result


def _finish_compile_recovery(queue, lease, dispatch, resolve_call, owner, lost):
    resolution = resolve_call(queue, lease, dispatch)
    state = queue.settle_compile_resolution(
        lease, dispatch, resolution,
        check_owner=partial(_require_compile_recovery_owner, queue, owner, lost))
    return resolution, state


def _require_settled_recovery_heartbeat(heartbeat):
    # Called only after fenced settlement committed. A concurrent renewal may
    # then observe that intentionally released lease; no work continues under it.
    if heartbeat.error is None or isinstance(heartbeat.error, LeaseFenceError):
        return
    raise heartbeat.error


def _abort_owned_compile_recovery(queue, lease, owner, lost):
    task = queue.get(lease.id)
    if (task.state, task.lease_token) != ('leased', lease.token):
        return
    _require_compile_recovery_owner(queue, owner, lost)
    queue.abort_compile_recovery(lease)
