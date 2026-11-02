"""Renewal owns its threads and error state, while callers own storage."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import lease_renewal
import pytest
from llm_wiki import queue_heartbeat as owner


class Queue:
    def __init__(self):
        self.updated = SimpleNamespace(id='next', daily_id='next')
        self.error = None
        self.calls = []

    def heartbeat(self, record, *, lease_seconds):
        self.calls.append((record, lease_seconds))
        if self.error is not None:
            raise self.error
        return self.updated

    heartbeat_source_fence = heartbeat

    def _heartbeat_wait(self, stop, seconds):
        self.calls.append((stop, seconds))
        return stop.is_set()


def make_heartbeat(kind):
    queue = Queue()
    record = SimpleNamespace(id='lease', daily_id='source')
    heartbeat = kind(queue, record, heartbeat_seconds=2, lease_seconds=9)
    return queue, record, heartbeat


@pytest.mark.parametrize('kind', [owner._LeaseHeartbeat, owner._SourceFenceHeartbeat])
def test_renewal_publishes_returned_record(kind):
    queue, record, heartbeat = make_heartbeat(kind)
    heartbeat._renew()
    assert queue.calls == [(record, 9)]
    assert vars(heartbeat).get('_lease', vars(heartbeat).get('_fence')) is queue.updated
    assert heartbeat._thread.daemon
    assert not heartbeat._thread.is_alive()


@pytest.mark.parametrize('ended', [None, RuntimeError('lost')])
@pytest.mark.parametrize('kind', [owner._LeaseHeartbeat, owner._SourceFenceHeartbeat])
def test_scheduler_receives_original_budget_and_loss_is_retained(monkeypatch, kind, ended):
    queue, _, heartbeat = make_heartbeat(kind)
    calls = []

    def renew(callback, **kwargs):
        calls.append((callback, kwargs))
        return ended

    monkeypatch.setattr(lease_renewal, 'renew_until_stopped', renew)
    heartbeat._run()
    callback, kwargs = calls[0]
    assert callback == heartbeat._renew
    assert kwargs['interval'] == 2
    assert kwargs['lease_seconds'] == 9
    assert kwargs['attempt_seconds'] == owner.DEFAULTS.queue_busy_ms / 1000
    assert kwargs['held_since'] == heartbeat._held_since
    assert kwargs['stop'] is heartbeat._stop
    assert kwargs['wait'](0.25) == heartbeat._stop.is_set()
    assert queue.calls == [(heartbeat._stop, 0.25)]
    assert heartbeat.error is ended
    assert heartbeat._stop.is_set() == (ended is not None)


def test_refresh_failure_retains_cause_and_blocks_current_and_future_refresh():
    queue, record, heartbeat = make_heartbeat(owner._SourceFenceHeartbeat)
    assert heartbeat.current() is record
    assert heartbeat.refresh() is queue.updated
    queue.error = RuntimeError('lost')
    for action in (heartbeat.refresh, heartbeat.current, heartbeat.refresh):
        with pytest.raises(owner.QueueOperationError, match='source_fence_lost') as caught:
            action()
        assert caught.value.__cause__ is queue.error
    assert heartbeat.error is queue.error
    assert heartbeat._stop.is_set()
    assert len(queue.calls) == 2


def test_fresh_import_and_thread_need_no_executor_or_durability_module():
    root = Path(__file__).resolve().parents[3]
    code = '''
import builtins, sys
from types import SimpleNamespace
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'memory_queue', 'reliable_memory', 'memory_state', 'markdown_transaction'}:
        raise AssertionError('runtime import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from llm_wiki.queue_heartbeat import _LeaseHeartbeat, _SourceFenceHeartbeat
class Queue:
    def _heartbeat_wait(self, stop, seconds): return stop.wait(seconds)
    def heartbeat(self, record, *, lease_seconds): return record
    heartbeat_source_fence = heartbeat
for kind in (_LeaseHeartbeat, _SourceFenceHeartbeat):
    heartbeat = kind(Queue(), SimpleNamespace(id='lease', daily_id='source'), heartbeat_seconds=1, lease_seconds=9)
    heartbeat.start()
    heartbeat.stop()
    assert not heartbeat._thread.is_alive()
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, text=True,
                            capture_output=True, timeout=30, check=False,
                            env={**os.environ, 'PYTHONPATH': str(root / 'scripts')})
    assert result.returncode == 0, result.stderr
