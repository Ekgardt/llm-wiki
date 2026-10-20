"""All foreground compile entries use durable, parent-supervised queue work."""
import sqlite3
import time
from argparse import Namespace
from functools import partial

import compile_memory as compiler
import maybe_compile
import memory_queue
import pytest

from tests.slow_machine import LONG_TIMEOUT
from tests.test_compile_call_controller import work as work
from tests.test_compile_leased_entrypoint import _configured_worker
from tests.test_compile_leased_entrypoint import runtime as runtime
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case


def _forbidden(*args, **kwargs):
    pytest.fail('foreground mutation must not use the legacy compiler')


def _fixture_processor(lock_token, owner):
    return memory_queue.LeaseProcessor(partial(_configured_worker, lock_token=lock_token, owner=owner))


@pytest.fixture
def foreground(work, runtime, monkeypatch):
    queue = work[0]
    queue.cancel(work[1].id)
    monkeypatch.setattr(compiler, '_run', _forbidden)
    monkeypatch.setattr(compiler, '_foreground_processor', _fixture_processor, raising=False)
    monkeypatch.setattr(compiler, '_refresh_generation_after_compile', lambda: None)
    return queue


def _entry(name, monkeypatch):
    if name == 'cli':
        monkeypatch.setattr('sys.argv', ['compile_memory.py'])
        return compiler.main()
    if name == 'mcp':
        import mcp_server
        return mcp_server._trigger_compile(deadline=time.monotonic() + LONG_TIMEOUT)['returncode']
    return compiler.run_pending_compile(deadline=time.monotonic() + LONG_TIMEOUT)


@pytest.mark.parametrize('entry', ['cli', 'pending', 'mcp'])
def test_foreground_entries_publish_through_owned_queue(foreground, monkeypatch, entry):
    assert _entry(entry, monkeypatch) == 0
    tasks = [task for task in foreground.list_tasks() if task.state != 'cancelled']
    assert len(tasks) == 1
    assert tasks[0].state == 'succeeded'
    assert (foreground.state_root / 'leased-call-counts').read_text().splitlines() == ['3']
    assert not maybe_compile.LOCK_FILE.exists()
    with sqlite3.connect(foreground.db_path) as database:
        assert database.execute('SELECT count(*) FROM queue_ownership').fetchone()[0] == 0
    with sqlite3.connect(foreground.coordinator_path) as database:
        assert database.execute('SELECT count(*) FROM maintenance_owners').fetchone()[0] == 0


def test_blocked_existing_request_is_reported_without_new_work(foreground):
    args = Namespace(file=None, closed_days_only=False, trigger='manual')
    task_id = foreground.enqueue('compile', 1, compiler.compile_task_payload(args))
    lease = foreground.claim('original', task_id=task_id)
    foreground.fail(lease, memory_queue.QueueFailure('unknown', blocked_capability='provider_outcome_unknown'))
    before = foreground.get(task_id)
    assert compiler.run_pending_compile() == 1
    assert foreground.get(task_id) == before
    assert len(foreground.list_tasks()) == 2
    assert not (foreground.state_root / 'leased-call-counts').exists()


def test_cancelled_foreground_does_not_enqueue(foreground):
    with pytest.raises(TimeoutError):
        compiler.run_pending_compile(cancelled=lambda: True)
    assert len(foreground.list_tasks()) == 1
    assert not maybe_compile.LOCK_FILE.exists()


def test_dry_run_keeps_nonpublishing_preview(foreground, monkeypatch):
    seen = []
    monkeypatch.setattr(compiler, '_run', lambda args, **bounds: seen.append(args.dry_run) or 0)
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
    from tests.test_queue_v3_corruption import _queue

    old = _queue(tmp_path / 'old')
    monkeypatch.setattr(memory_queue, 'active_memory_queue', lambda *args, **kwargs: old)
    with pytest.raises(ValueError, match='adopted compile journal'):
        compiler.run_pending_compile()
    assert old.list_tasks() == []


def _invalid_answer(*args):
    from llm_client import BackendResponse

    return BackendResponse('invalid JSON')


def _invalid_foreground_processor(lock_token, owner):
    return memory_queue.LeaseProcessor(partial(_configured_worker,
        lock_token=lock_token, owner=owner, backend=_invalid_answer))


def test_invalid_model_output_never_marks_sources_compiled(foreground, monkeypatch):
    import memory_state

    before = dict(memory_state.load_state().get('compiled_daily_hashes', {}))
    monkeypatch.setattr(compiler, '_foreground_processor', _invalid_foreground_processor)
    assert compiler.run_pending_compile() == 1
    state = memory_state.load_state()
    assert state.get('compiled_daily_hashes', {}) == before
    assert state['last_compile_status'] == 'error'
    assert not any(task.state == 'succeeded' for task in foreground.list_tasks())


def test_foreground_reserves_work_before_another_worker_can_claim_it(foreground, monkeypatch):
    def checked_processor(lock_token, owner):
        assert foreground.claim('competing-worker') is None
        return _fixture_processor(lock_token, owner)

    monkeypatch.setattr(compiler, '_foreground_processor', checked_processor)
    assert compiler.run_pending_compile() == 0


def test_mcp_compile_keeps_protocol_stdout_clean(foreground, capfd):
    import mcp_server

    result = mcp_server._trigger_compile(deadline=time.monotonic() + LONG_TIMEOUT)
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
    result = memory_queue._run_processor_child(_noisy_worker, {}, LONG_TIMEOUT)
    assert result is True
    print('parent protocol')
    captured = capfd.readouterr()
    assert captured.out == 'parent protocol\n'
    assert all(message in captured.err for message in (
        'python diagnostic', 'native diagnostic', 'descendant diagnostic'))
