"""Unknown cost must not be repeated through a different task/work identity."""
import hashlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from threading import Barrier, Event, Lock

import compile_controller as controller
import compile_progress as progress
import llm_client
import memory_queue
import pytest
from llm_client import BackendResponse

from tests.test_compile_call_controller import work as work
from tests.test_compile_leased_entrypoint import _run
from tests.test_compile_leased_entrypoint import runtime as runtime
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case


def _scope(path='knowledge/daily/2026-10-20.md', start=0, end=20, digest='a' * 64):
    return {'schema': 'compile-source-scope/v1', 'sources': [
        {'logical_path': path, 'byte_start': start, 'byte_end': end, 'sha256': digest}]}


def _checkpoint(queue, lease, identity, scope):
    details = {} if scope is None else {'source_scope': scope}
    root = progress.CompileEvent(identity * 64, lease.id, 0, 'd' * 64, 'pending', 'ready', details)
    queue.record_compile_event(lease, root)
    return queue.save_compile_artifact(
        lease, replace(root, sequence=1, stage='snapshot_ready'), b'verified source')


def _pair(upgraded, second_scope, *, scoped_first=True):
    queue = memory_queue.active_memory_queue(*upgraded[:2])
    first = queue.claim('first')
    queue.enqueue('compile', 1, {'source-overlap': 'second'})
    second = queue.claim('second')
    one = _checkpoint(queue, first, 'a', _scope() if scoped_first else None)
    two = _checkpoint(queue, second, 'b', second_scope)
    return (queue, first, one), (queue, second, two)


def _call(work, invoke):
    return controller.run_model_call(*work, request_digest='e' * 64, invoke=invoke)


def _unknown():
    raise TimeoutError('provider may have accepted source')


@pytest.mark.parametrize('scope', [
    _scope(), _scope(start=10, end=30), _scope(start=5, end=10),
    _scope(digest='b' * 64), None,
])
def test_new_task_cannot_repeat_uncertain_overlapping_source(upgraded, scope):
    first, second = _pair(upgraded, scope)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(first, _unknown)
    calls = []

    def duplicate():
        calls.append(True)
        return BackendResponse('duplicate')

    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(second, duplicate)
    assert calls == []
    assert second[0].get_compile_event(second[2].work_id) == second[2]
    assert second[0].get(second[1].id).blocked_capability == 'provider_outcome_unknown'


@pytest.mark.parametrize('scope', [_scope(start=20, end=40), _scope(path='knowledge/daily/2026-10-21.md')])
def test_independent_source_can_continue_after_unknown(upgraded, scope):
    first, second = _pair(upgraded, scope)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(first, _unknown)
    assert _call(second, lambda: BackendResponse('independent')).text == 'independent'


def test_known_durable_answer_is_not_an_unknown_outcome(upgraded):
    first, second = _pair(upgraded, _scope())
    assert _call(first, lambda: BackendResponse('known')).text == 'known'
    assert _call(second, lambda: BackendResponse('new request')).text == 'new request'


def test_lost_answer_does_not_authorize_a_different_work(upgraded):
    first, second = _pair(upgraded, _scope())
    _call(first, lambda: BackendResponse('known before loss'))
    answer = first[0].get_compile_event(first[2].work_id)
    (first[0].state_root / answer.details['artifact']['reference']).unlink()
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(second, lambda: BackendResponse('must not repeat'))


def test_unknown_historical_scope_cannot_prove_independence(upgraded):
    first, second = _pair(upgraded, _scope(path='knowledge/daily/2026-10-21.md'), scoped_first=False)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(first, _unknown)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(second, lambda: BackendResponse('must not guess independence'))


def _racing_call(work, barrier, invoke):
    barrier.wait(timeout=60)
    try:
        _call(work, invoke)
    except controller.ProviderOutcomeUnknown:
        return 'blocked'
    return 'completed'


def test_overlapping_live_dispatches_are_atomically_exclusive(upgraded):
    # Existing hosted-worker test allowance; synchronization uses events, not sleeps.
    first, second = _pair(upgraded, _scope())
    barrier, started, release, lock = Barrier(2), Event(), Event(), Lock()
    calls = []

    def invoke():
        with lock:
            calls.append(True)
        started.set()
        assert release.wait(timeout=60)
        return BackendResponse('one live call')

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_racing_call, work, barrier, invoke) for work in (first, second)]
        try:
            assert started.wait(timeout=60)
            finished, _ = wait(futures, timeout=60, return_when=FIRST_COMPLETED)
            assert len(finished) == 1
            assert next(iter(finished)).result() == 'blocked'
            assert calls == [True]
        finally:
            release.set()
        assert sorted(future.result(timeout=60) for future in futures) == ['blocked', 'completed']


def test_repeat_consent_does_not_release_other_work_before_a_durable_answer(upgraded):
    first, second = _pair(upgraded, _scope())
    queue, lease, checkpoint = first
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(first, _unknown)
    dispatch = queue.get_compile_event(checkpoint.work_id)
    queue.unblock(lease.id)
    replacement = queue.claim('explicit-repeat')
    resumed = controller.authorize_model_repeat(queue, replacement, checkpoint,
        request_digest='e' * 64, authorization={
            'dispatch_digest': progress._event_digest(dispatch), 'actor': 'operator',
            'reason': 'explicit test consent for this call'})
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(second, lambda: BackendResponse('not authorized by another work consent'))
    assert _call((queue, replacement, resumed), lambda: BackendResponse('resolved')).text == 'resolved'
    queue.unblock(second[1].id)
    replacement_second = queue.claim('retry-independent-origin')
    assert _call((queue, replacement_second, second[2]),
                 lambda: BackendResponse('known prior outcome')).text == 'known prior outcome'


def test_leased_worker_binds_actual_sources_and_blocks_a_new_task(work, runtime, monkeypatch):
    calls = []

    def unknown(*args):
        calls.append(True)
        raise TimeoutError('unknown upstream outcome')

    monkeypatch.setitem(llm_client._BACKENDS, 'fake', unknown)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _run(work)
    queue = work[0]
    with queue.connection() as database:
        row = database.execute(
            "SELECT work_id FROM compile_work_events WHERE json_extract(details_json,'$.model_call.phase')='dispatch'"
        ).fetchone()
        root = progress.event_at(database, row['work_id'], 0)
    assert root.details['source_scope']['sources']
    for source in root.details['source_scope']['sources']:
        raw = (queue.vault / source['logical_path']).read_bytes()
        assert hashlib.sha256(raw[source['byte_start']:source['byte_end']]).hexdigest() == source['sha256']
    queue.enqueue('compile', 1, {'new-origin': True})
    other = queue.claim('another-origin')
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _run((queue, other, work[2]))
    assert calls == [True]


def test_published_label_cannot_erase_an_unknown_call(upgraded):
    first, second = _pair(upgraded, _scope())
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(first, _unknown)
    queue, lease, checkpoint = first
    queue.unblock(lease.id)
    replacement = queue.claim('unproved-publication')
    dispatch = queue.get_compile_event(checkpoint.work_id)
    queue.record_compile_event(replacement, replace(dispatch, sequence=dispatch.sequence + 1,
        stage='published', outcome='completed', details={}))
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(second, lambda: BackendResponse('cannot trust a status label'))


def test_orphan_answer_blocks_other_work_until_original_work_recovers_it(upgraded, monkeypatch):
    first, second = _pair(upgraded, _scope())
    queue, _, _ = first
    original = queue.record_compile_event

    def interrupted(lease, event):
        if event.details.get('model_call', {}).get('phase') == 'answer':
            raise OSError('answer event interrupted')
        return original(lease, event)

    with monkeypatch.context() as patch:
        patch.setattr(queue, 'record_compile_event', interrupted)
        with pytest.raises(OSError, match='answer event'):
            _call(first, lambda: BackendResponse('saved answer'))
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(second, lambda: BackendResponse('cannot adopt another work implicitly'))
    assert _call(first, lambda: pytest.fail('must recover saved answer')).text == 'saved answer'
    queue.unblock(second[1].id)
    replacement = queue.claim('after-orphan-recovery')
    assert _call((queue, replacement, second[2]), lambda: BackendResponse('known')).text == 'known'


@pytest.mark.parametrize('foreign_binding', [False, True])
def test_hash_valid_foreign_answer_cannot_clear_uncertainty(upgraded, foreign_binding):
    first, second = _pair(upgraded, _scope())
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(first, _unknown)
    queue, lease, checkpoint = first
    dispatch = queue.get_compile_event(checkpoint.work_id)
    queue.unblock(lease.id)
    replacement = queue.claim('bad-answer-producer')
    data = b'hash-valid bytes that are not a bound model response'
    if foreign_binding:
        data = controller._encoded_answer(replace(dispatch, work_id='f' * 64), BackendResponse('foreign'))
    queue.save_compile_artifact(replacement, controller._answer_event(dispatch, replacement.id), data)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _call(second, lambda: BackendResponse('must not trust a file hash alone'))
