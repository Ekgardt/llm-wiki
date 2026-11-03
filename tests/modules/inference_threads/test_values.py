"""Tracked inference drains at safe points and names unfinished work."""
import threading

import inference_threads
import pytest

from tests.slow_machine import SHORT_TIMEOUT


def test_settle_stops_inference_at_its_safe_point_and_waits_for_it():
    reached = threading.Event()

    def inference() -> None:
        reached.set()
        inference_threads.stopping.wait(timeout=SHORT_TIMEOUT)

    inference_threads.start(inference, name="test-inference")
    reached.wait(timeout=SHORT_TIMEOUT)

    assert (inference_threads.settle(5.0), inference_threads.running()) == ([], [])



def test_a_thread_that_will_not_stop_is_named_and_the_next_server_still_warms():
    release = threading.Event()
    stuck = inference_threads.start(lambda: release.wait(timeout=SHORT_TIMEOUT), name="stuck-inference")

    left = inference_threads.settle(0.05)
    still_asked = inference_threads.stopping.is_set()
    release.set()
    stuck.join(timeout=SHORT_TIMEOUT)

    assert (left, still_asked, inference_threads.stopping.is_set()) == (["stuck-inference"], True, False)



def _start_failure(self):
    raise OSError('thread start refused')


def test_failed_start_propagates_and_does_not_leave_a_registered_thread(monkeypatch):
    assert inference_threads.running() == []
    monkeypatch.setattr(threading.Thread, 'start', _start_failure)
    with pytest.raises(OSError, match='thread start refused'):
        inference_threads.start(lambda: None, name='failed-start')
    assert inference_threads.running() == []
    assert not inference_threads.stopping.is_set()


def test_idle_settlement_does_not_leave_the_next_caller_stopped():
    assert inference_threads.running() == []
    assert inference_threads.settle(0) == []
    assert not inference_threads.stopping.is_set()


def test_tracked_target_cleanup_runs_before_exception_propagates(monkeypatch):
    forgotten = []
    monkeypatch.setattr(inference_threads, '_forget', forgotten.append)
    with pytest.raises(OSError, match='thread start refused'):
        inference_threads._tracked(lambda: _start_failure(None))()
    assert forgotten == [threading.current_thread()]
