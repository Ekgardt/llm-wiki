"""Real cooperating search scopes preempt full-path warmup, without fake costs."""
from __future__ import annotations

import threading
import time

import inference_threads
import mcp_server
import pytest
import retrieval
import search_memory

from tests.slow_machine import SHORT_TIMEOUT


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_OBSERVED', {})
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_OBSERVED_AT', {})
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_KIND_SLOTS', {
        kind: threading.BoundedSemaphore(1) for kind in retrieval.OPTIONAL_STAGE_KINDS
    })
    monkeypatch.setattr(mcp_server, '_warm_reranker', lambda: None)
    monkeypatch.setattr(search_memory, '_resolved_generation_embedder', lambda *_args: (None, None, None))
    yield
    assert inference_threads.settle(SHORT_TIMEOUT) == []


class Searches:
    def __init__(self):
        self.warmup_started = threading.Event()
        self.foreground_started = threading.Event()
        self.observed = threading.Event()
        self.cancelled = []

    def run(self, _query, **options):
        if options['source_tool'] == 'warmup':
            return self.warmup(options['cancelled'])
        return self.foreground()

    def warmup(self, cancelled):
        self.warmup_started.set()
        assert self.foreground_started.wait(SHORT_TIMEOUT)
        interrupted = cancelled is not None and cancelled()
        self.cancelled.append(interrupted)
        self.observed.set()
        if interrupted:
            raise TimeoutError('warmup yielded to foreground')
        return []

    def foreground(self):
        self.foreground_started.set()
        assert self.observed.wait(SHORT_TIMEOUT)
        return []


def test_running_full_path_warmup_gets_a_real_foreground_cancellation(boundary, monkeypatch):
    searches = Searches()
    monkeypatch.setattr(retrieval, 'retrieve_via_search_memory', searches.run)
    warmup = threading.Thread(target=mcp_server.warmup_retrieval_path)
    warmup.start()
    try:
        assert searches.warmup_started.wait(SHORT_TIMEOUT)
        assert search_memory.search('foreground', semantic=False, deadline_monotonic=time.monotonic() + SHORT_TIMEOUT) == []
    finally:
        searches.foreground_started.set()
        searches.observed.set()
        warmup.join(SHORT_TIMEOUT)
    assert not warmup.is_alive()
    assert searches.cancelled == [True] + [False] * mcp_server.WARMUP_PASSES
    assert mcp_server.warmup_state()['status'] == 'warm'


@pytest.mark.parametrize('cancelled,expired', [(True, False), (False, True)])
def test_cancelled_or_expired_optional_work_never_starts(boundary, cancelled, expired):
    called = threading.Event()
    deadline = time.monotonic() + SHORT_TIMEOUT
    if expired:
        deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        retrieval._call_dense(lambda **_kw: called.set(), {}, deadline_monotonic=deadline,
                              cancelled=lambda: cancelled)
    assert inference_threads.settle(SHORT_TIMEOUT) == []
    assert not called.is_set()


def _observed_wait(monkeypatch):
    reached = threading.Event()
    original = retrieval._FOREGROUND_CONDITION.wait

    def wait(timeout):
        reached.set()
        return original(timeout)

    monkeypatch.setattr(retrieval._FOREGROUND_CONDITION, 'wait', wait)
    return reached


def _blocked(release, started, completed):
    def operation():
        started.set()
        assert release.wait(SHORT_TIMEOUT)
        completed.set()
        return ['complete']

    return operation


def _short_optional(operation, cancelled=None):
    with pytest.raises(retrieval.OptionalStageNotAdmitted):
        retrieval._run_optional_bounded(operation,
                                       deadline=time.monotonic() + retrieval.OPTIONAL_STAGE_MAX_SECONDS / 2,
                                       cancelled=cancelled, kind='dense')


def test_foreground_optional_owner_outlives_the_fallback_answer(boundary, monkeypatch):
    release, started, completed = threading.Event(), threading.Event(), threading.Event()
    operation = _blocked(release, started, completed)
    with retrieval.foreground_retrieval('recall'):
        _short_optional(operation)
        assert started.wait(SHORT_TIMEOUT)
    waiting = _observed_wait(monkeypatch)
    warmed = threading.Event()
    worker = threading.Thread(target=mcp_server._warmup_stage, args=('controlled', warmed.set))
    worker.start()
    try:
        assert waiting.wait(SHORT_TIMEOUT)
        assert not warmed.is_set()
    finally:
        release.set()
        worker.join(SHORT_TIMEOUT)
    assert completed.is_set() and warmed.is_set()
    assert not worker.is_alive()
    assert retrieval._observed_optional_stage_cost('dense') is not None


def test_detached_warmup_worker_is_cancelled_and_never_records_a_partial_cost(boundary):
    release, started, completed = threading.Event(), threading.Event(), threading.Event()
    operation = _blocked(release, started, completed)
    with retrieval.warmup_priority(time.monotonic() + SHORT_TIMEOUT, lambda: False) as cancelled:
        _short_optional(operation, cancelled)
        assert started.wait(SHORT_TIMEOUT)
    with retrieval.foreground_retrieval('recall'):
        assert cancelled() is True
        assert not retrieval._optional_stage_slots('dense').acquire(blocking=False)
        release.set()
        assert inference_threads.settle(SHORT_TIMEOUT) == []
    assert completed.is_set()
    assert retrieval._observed_optional_stage_cost('dense') is None
    assert not retrieval._BACKGROUND_WORKERS


def test_shutdown_wakes_idle_wait_without_starting_model_work(boundary, monkeypatch):
    waiting = _observed_wait(monkeypatch)
    called = threading.Event()
    results = []

    def stage():
        results.append(mcp_server._warmup_stage('controlled', called.set))

    with retrieval.foreground_retrieval('recall'):
        worker = threading.Thread(target=stage)
        worker.start()
        try:
            assert waiting.wait(SHORT_TIMEOUT)
            inference_threads.stopping.set()
            with retrieval._FOREGROUND_CONDITION:
                retrieval._FOREGROUND_CONDITION.notify_all()
            worker.join(SHORT_TIMEOUT)
        finally:
            inference_threads.stopping.clear()
    assert not worker.is_alive()
    assert results == [False] and not called.is_set()


def test_failed_worker_start_releases_priority_and_capacity(boundary, monkeypatch):
    def fail_start(*_args, **_kwargs):
        raise RuntimeError('thread start refused')

    monkeypatch.setattr(inference_threads, 'start', fail_start)
    with pytest.raises(RuntimeError, match='thread start refused'):
        retrieval._run_optional_bounded(lambda: [], deadline=time.monotonic() + SHORT_TIMEOUT,
                                       cancelled=None, kind='dense')
    assert not retrieval._FOREGROUND_OWNERS
    slots = retrieval._optional_stage_slots('dense')
    assert slots.acquire(blocking=False)
    slots.release()


def test_foreground_exception_leaves_no_priority_owner(boundary, monkeypatch):
    def broken(_query, **_kwargs):
        raise ValueError('mandatory work refused')

    monkeypatch.setattr(retrieval, 'retrieve_via_search_memory', broken)
    with pytest.raises(ValueError, match='mandatory work refused'):
        search_memory.search('foreground', semantic=False)
    assert not retrieval._FOREGROUND_OWNERS


def test_original_warmup_stage_clock_cancels_retry_and_detached_work(boundary, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(retrieval.time, 'monotonic', lambda: clock[0])
    with retrieval.warmup_priority(101.0, lambda: False) as cancelled:
        assert cancelled() is False
        clock[0] = 101.0
        assert cancelled() is True
        called = threading.Event()
        with pytest.raises(retrieval.OptionalStageTimeout):
            retrieval._run_optional_bounded(called.set, deadline=200.0,
                                           cancelled=cancelled, kind='dense')
        assert not called.is_set()
    assert retrieval._observed_optional_stage_cost('dense') is None


def test_telemetry_label_does_not_grant_background_authority(boundary):
    with retrieval.warmup_priority(time.monotonic() + SHORT_TIMEOUT, lambda: False) as cancelled:
        token = retrieval._BACKGROUND_EVENT.set(None)
        try:
            with retrieval.foreground_retrieval('warmup'):
                assert cancelled()
        finally:
            retrieval._BACKGROUND_EVENT.reset(token)


def test_bound_warmup_owner_is_background_regardless_of_telemetry(boundary):
    with retrieval.warmup_priority(time.monotonic() + SHORT_TIMEOUT, lambda: False) as cancelled:
        with retrieval.foreground_retrieval('recall'):
            assert not cancelled()
            assert not retrieval._FOREGROUND_OWNERS
