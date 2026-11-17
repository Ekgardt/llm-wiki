"""All foreground compile entries use durable, parent-supervised queue work."""
import sqlite3
import time
from argparse import Namespace
from functools import partial

import compile_lock
import compile_memory as compiler
import pytest
import queue_core
from llm_wiki import compile_pipeline_selection

from tests.compile_call_controller_helpers import work as work
from tests.compile_leased_entrypoint_helpers import _configured_worker
from tests.compile_leased_entrypoint_helpers import runtime as runtime
from tests.compile_paths import point_compile_path
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.slow_machine import LONG_TIMEOUT


def _forbidden(*args, **kwargs):
    pytest.fail('foreground mutation must not use the legacy compiler')


def _fixture_processor(lock_token, owner):
    return queue_core.LeaseProcessor(partial(_configured_worker, lock_token=lock_token, owner=owner))


@pytest.fixture
def foreground(work, runtime, monkeypatch):
    queue = work[0]
    queue.cancel(work[1].id)
    point_compile_path(monkeypatch, 'run', _forbidden)
    monkeypatch.setattr(compiler, 'foreground_processor', _fixture_processor, raising=False)
    monkeypatch.setattr(compiler, 'refresh_generation_after_compile', lambda: None)
    return queue


def _entry(name, monkeypatch):
    if name == 'cli':
        monkeypatch.setattr('sys.argv', ['compile_memory.py'])
        return compiler.main()
    if name == 'mcp':
        import mcp_server
        return mcp_server.trigger_compile(deadline=time.monotonic() + LONG_TIMEOUT)['returncode']
    return compiler.run_pending_compile(deadline=time.monotonic() + LONG_TIMEOUT)


@pytest.mark.parametrize('entry', ['cli', 'pending', 'mcp'])
def test_foreground_entries_publish_through_owned_queue(foreground, monkeypatch, entry):
    assert _entry(entry, monkeypatch) == 0
    tasks = [task for task in foreground.list_tasks() if task.state != 'cancelled']
    assert len(tasks) == 1
    assert tasks[0].state == 'succeeded'
    assert (foreground.state_root / 'leased-call-counts').read_text().splitlines() == ['3']
    assert not compile_lock.compile_lock_file().exists()
    with sqlite3.connect(foreground.db_path) as database:
        assert database.execute('SELECT count(*) FROM queue_ownership').fetchone()[0] == 0
    with sqlite3.connect(foreground.coordinator_path) as database:
        assert database.execute('SELECT count(*) FROM maintenance_owners').fetchone()[0] == 0


def test_blocked_existing_request_is_reported_without_new_work(foreground):
    args = Namespace(file=None, closed_days_only=False, trigger='manual')
    task_id = foreground.enqueue('compile', 1, compile_pipeline_selection.compile_task_payload(args))
    lease = foreground.claim('original', task_id=task_id)
    foreground.fail(lease, queue_core.QueueFailure('unknown', blocked_capability='provider_outcome_unknown'))
    before = foreground.get(task_id)
    assert compiler.run_pending_compile() == 1
    assert foreground.get(task_id) == before
    assert len(foreground.list_tasks()) == 2
    assert not (foreground.state_root / 'leased-call-counts').exists()


def test_cancelled_foreground_does_not_enqueue(foreground):
    with pytest.raises(TimeoutError):
        compiler.run_pending_compile(cancelled=lambda: True)
    assert len(foreground.list_tasks()) == 1
    assert not compile_lock.compile_lock_file().exists()


def test_dry_run_keeps_nonpublishing_preview(foreground, monkeypatch):
    seen = []
    point_compile_path(monkeypatch, 'run', lambda args, **bounds: seen.append(args.dry_run) or 0)
    monkeypatch.setattr('sys.argv', ['compile_memory.py', '--dry-run'])
    assert compiler.main() == 0
    assert seen == [True]
    assert len(foreground.list_tasks()) == 1


def test_later_compile_uses_new_task_without_rebuying_receipted_sources(foreground):
    assert compiler.run_pending_compile() == 0
    assert compiler.run_pending_compile() == 0
    succeeded = [task for task in foreground.list_tasks() if task.state == 'succeeded']
    assert len(succeeded) == 2
    assert (foreground.state_root / 'leased-call-counts').read_text().splitlines() == ['3', '0']


def test_supplied_owner_is_preserved_after_child_publication(foreground):
    registry = foreground.ownership_registry()
    owner = registry.acquire('queue-worker', scope='test-parent')
    try:
        assert compiler.run_pending_compile(owner=owner) == 0
        with sqlite3.connect(foreground.coordinator_path) as database:
            database.row_factory = sqlite3.Row
            registry.require(database, owner)
    finally:
        registry.release(owner)


def test_old_queue_schema_is_refused_before_enqueue(foreground, tmp_path, monkeypatch):
    from tests.queue_v3_corruption_helpers import _queue

    old = _queue(tmp_path / 'old')
    monkeypatch.setattr(queue_core, 'active_memory_queue', lambda *args, **kwargs: old)
    with pytest.raises(ValueError, match='adopted compile journal'):
        compiler.run_pending_compile()
    assert old.list_tasks() == []


def _invalid_answer(*args):
    from llm_client import BackendResponse

    return BackendResponse('invalid JSON')


def _invalid_foreground_processor(lock_token, owner):
    return queue_core.LeaseProcessor(partial(_configured_worker,
        lock_token=lock_token, owner=owner, backend=_invalid_answer))


def test_invalid_model_output_never_marks_sources_compiled(foreground, monkeypatch):
    import memory_state

    before = dict(memory_state.load_state().get('compiled_daily_hashes', {}))
    monkeypatch.setattr(compiler, 'foreground_processor', _invalid_foreground_processor)
    assert compiler.run_pending_compile() == 1
    state = memory_state.load_state()
    assert state.get('compiled_daily_hashes', {}) == before
    assert state['last_compile_status'] == 'error'
    assert not any(task.state == 'succeeded' for task in foreground.list_tasks())


def test_foreground_reserves_work_before_another_worker_can_claim_it(foreground, monkeypatch):
    def checked_processor(lock_token, owner):
        assert foreground.claim('competing-worker') is None
        return _fixture_processor(lock_token, owner)

    monkeypatch.setattr(compiler, 'foreground_processor', checked_processor)
    assert compiler.run_pending_compile() == 0


def test_mcp_compile_keeps_protocol_stdout_clean(foreground, capfd):
    import mcp_server

    result = mcp_server.trigger_compile(deadline=time.monotonic() + LONG_TIMEOUT)
    assert result['status'] == 'completed'
    captured = capfd.readouterr()
    assert captured.out == ''
    assert 'compile_memory:' in captured.err


def _noisy_worker(task):
    import os
    import subprocess
    import sys

    print('python diagnostic', flush=True)
    os.write(1, b'native diagnostic\n')
    subprocess.run([sys.executable, '-c', "print('descendant diagnostic')"], check=True)
    return True


def test_worker_diagnostics_preserve_parent_protocol_channel(capfd):
    result = queue_core.run_processor_child(_noisy_worker, {}, LONG_TIMEOUT)
    assert result is True
    print('parent protocol')
    captured = capfd.readouterr()
    assert captured.out == 'parent protocol\n'
    assert all(message in captured.err for message in (
        'python diagnostic', 'native diagnostic', 'descendant diagnostic'))


def test_run_pending_compile_propagates_supplied_owner(monkeypatch):
    owner = object()
    captured = {}

    def run(args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(compiler, "run_queued_compile", run)

    assert compiler.run_pending_compile(owner=owner) == 0
    assert captured["owner"] is owner
