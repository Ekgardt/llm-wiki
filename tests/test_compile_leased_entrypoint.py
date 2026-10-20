"""The default schema4 worker must execute and prove its own compile lineage."""
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import compile_controller as controller
import compile_memory as compiler
import llm_client
import maybe_compile
import memory_queue
import memory_state
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_durable_stream_capture import _paths
from tests.test_compile_execution_inventory import _components
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_compile_protected_dispatch import compiler_case as compiler_case
from tests.test_compile_saved_context_refresh import _semantic_backend
from tests.test_the_compile_decides_what_the_snapshot_already_knows import _provider


@pytest.fixture
def runtime(work, compiler_case, monkeypatch):
    root, state = work[0].vault, work[0].state_root
    monkeypatch.setenv('LLM_WIKI_ROOT', str(root))
    monkeypatch.setenv('LLM_WIKI_STATE_ROOT', str(state))
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    paths = {'ROOT': root, 'STATE_ROOT': state, 'STATE_DIR': state / 'run',
             'STATE_FILE': state / 'run/state.json', 'LOCK_FILE': state / 'run/state.json.lock'}
    for name, value in paths.items():
        monkeypatch.setattr(memory_state, name, value)
    for name, value in {'ROOT': root, 'STATE_ROOT': state, 'LOCK_FILE': state / 'run/compile.pid'}.items():
        monkeypatch.setattr(maybe_compile, name, value)
    _components(monkeypatch, root)
    _paths(compiler_case)
    monkeypatch.setattr(compiler, 'provider_candidates', lambda *args, **kwargs: [_provider()])
    return _semantic_backend(monkeypatch)


def _run(work):
    return memory_queue._manual_lease_processor(work[0], work[1], float('inf'),
                                               memory_queue._compat_task(work[1]))


def _forbidden(*args, **kwargs):
    raise AssertionError('leased work used legacy subprocess or recaptured saved inputs')


def test_default_leased_compile_uses_controller_and_parent_can_settle(work, runtime, monkeypatch):
    monkeypatch.setattr(memory_queue, '_manual_compile', _forbidden)
    assert _run(work) is True
    assert len(runtime) == 3
    assert not maybe_compile.LOCK_FILE.exists()
    assert memory_state.load_state()['last_compile_status'] == 'ok'
    work[0].publish_result(work[1], operation_id=work[1].id, result=b'complete')
    assert work[0].acknowledge(work[1]).state == 'succeeded'


def test_leased_recovery_uses_saved_sources_without_resolving_again(work, runtime, monkeypatch):
    assert _run(work) is True
    next(compiler.DAILY_DIR.glob('????-??-??.md')).write_bytes(b'changed live source')
    monkeypatch.setattr(compiler, 'select_dailies', _forbidden)
    monkeypatch.setattr(compiler, 'resolve_compile_plan', _forbidden)
    assert _run(work) is True
    assert len(runtime) == 3


def test_other_live_compile_owner_prevents_source_or_model_work(work, runtime, monkeypatch):
    maybe_compile._write_lock(os.getppid())
    monkeypatch.setattr(compiler, 'prepare_compile_planning', _forbidden)
    assert _run(work) is False
    assert runtime == []
    assert maybe_compile._read_lock()['pid'] == os.getppid()
    assert 'last_compile_status' not in memory_state.load_state()


def test_stale_lease_is_refused_before_compile_start(work, runtime):
    stale = replace(work[1], token='stale')
    with pytest.raises(memory_queue.LeaseFenceError):
        _run((work[0], stale, work[2]))
    assert runtime == []
    assert not maybe_compile.LOCK_FILE.exists()


def test_invalid_answer_never_finishes_task_or_leaves_lock(work, runtime, monkeypatch):
    monkeypatch.setitem(llm_client._BACKENDS, 'fake', lambda *args: llm_client.BackendResponse('invalid JSON'))
    assert _run(work) is False
    assert memory_state.load_state()['last_compile_status'] == 'error'
    assert not maybe_compile.LOCK_FILE.exists()
    with pytest.raises(memory_queue.CompileCompletionError):
        work[0].publish_result(work[1], operation_id=work[1].id, result=b'claimed success')


def test_uncertain_provider_blocks_task_and_releases_compile_lock(work, runtime, monkeypatch):
    def unknown(*args):
        raise TimeoutError('provider may have run')

    monkeypatch.setitem(llm_client._BACKENDS, 'fake', unknown)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _run(work)
    assert work[0].get(work[1].id).blocked_capability == 'provider_outcome_unknown'
    assert not maybe_compile.LOCK_FILE.exists()


@pytest.mark.parametrize('version,kind', [(3, 'compile'), (4, 'query')])
def test_unmigrated_or_other_kind_preserves_existing_processor(monkeypatch, version, kind):
    task = {'type': kind, 'payload': {}}
    monkeypatch.setattr(memory_queue, '_manual_processor', lambda received: received == task)
    queue = SimpleNamespace(expected_contract=SimpleNamespace(user_version=version))
    lease = SimpleNamespace(kind=kind)
    assert memory_queue._manual_lease_processor(queue, lease, time.monotonic() + 60, task) is True


def test_queue_cli_selects_lease_aware_default_processor(monkeypatch):
    observed = []

    def worker(processor, **kwargs):
        observed.append(processor)
        return memory_queue.WorkerSummary(0, 0, 0, 0, 0)

    monkeypatch.setattr(memory_queue, 'run_worker', worker)
    parser = memory_queue._build_cli_parser()
    assert memory_queue._cli_work(parser.parse_args(['work']), parser) == 0
    assert isinstance(observed[0], memory_queue.LeaseProcessor)
    assert observed[0].process is memory_queue._manual_lease_processor


def _configured_worker(queue, lease, deadline, task):
    """Child-side fixture adapters; ownership, compiler and settlement stay real."""
    with pytest.MonkeyPatch.context() as patch:
        _components(patch, queue.vault)
        patch.setattr(compiler, 'provider_candidates', lambda *args, **kwargs: [_provider()])
        calls = _semantic_backend(patch)
        result = memory_queue._manual_lease_processor(queue, lease, deadline, task)
        with (queue.state_root / 'leased-call-counts').open('a') as handle:
            handle.write(str(len(calls)) + '\n')
        return result


def test_spawned_lease_compiles_recovers_and_parent_settles(work, runtime):
    # Same hosted-platform allowance used by the existing worker handoff tests.
    deadline = time.monotonic() + 60
    processor = memory_queue.LeaseProcessor(_configured_worker)
    bound = memory_queue._bind_lease_processor(processor, work[0], work[1], deadline)
    assert memory_queue._run_processor_child(bound, memory_queue._compat_task(work[1]),
                                             deadline - time.monotonic()) is True
    counts = {'ok': 0, 'failed': 0, 'dead': 0, 'skipped': 0, 'halted': 0}
    memory_queue._run_and_settle(work[0], processor, memory_queue._run_processor_child,
        work[1], counts, deadline=deadline, monotonic=time.monotonic,
        lease_seconds=memory_queue.DEFAULTS.queue_lease_seconds,
        heartbeat_seconds=memory_queue.DEFAULTS.queue_heartbeat_seconds,
        max_attempts=memory_queue.DEFAULTS.queue_max_attempts,
        retry_base_seconds=memory_queue.DEFAULTS.retry_base_seconds,
        retry_cap_seconds=memory_queue.DEFAULTS.retry_cap_seconds)
    assert counts['ok'] == 1
    assert work[0].get(work[1].id).state == 'succeeded'
    assert (work[0].state_root / 'leased-call-counts').read_text().splitlines() == ['3', '0']


def test_worker_rejects_changed_compatibility_payload(work, runtime):
    task = {**memory_queue._compat_task(work[1]), 'payload': {'changed': True}}
    with pytest.raises(memory_queue.LeaseFenceError, match='compatibility payload'):
        memory_queue._manual_lease_processor(work[0], work[1], float('inf'), task)
    assert runtime == []
    assert not maybe_compile.LOCK_FILE.exists()


def test_unknown_compile_handler_version_never_runs_as_current(work, runtime):
    work[0].cancel(work[1].id)
    work[0].enqueue('compile', 2, {})
    lease = work[0].claim('future-version')
    with pytest.raises(ValueError, match='handler version'):
        compiler.run_leased_compile(work[0], lease)
    assert runtime == []
    assert not maybe_compile.LOCK_FILE.exists()


def test_empty_pending_sources_still_prove_completion_without_model_calls(work, runtime):
    for path in compiler.DAILY_DIR.glob('????-??-??.md'):
        path.unlink()
    assert _run(work) is True
    assert runtime == []
    assert memory_state.load_state()['last_compile_outcome'] == 'nothing'
    work[0].publish_result(work[1], operation_id=work[1].id, result=b'empty complete')
    assert work[0].acknowledge(work[1]).state == 'succeeded'
