"""The default schema4 worker must execute and prove its own compile lineage."""
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import compile_controller as controller
import compile_leased_run
import compile_lock
import llm_client
import memory_queue
import memory_state
import pytest
import queue_core
from llm_wiki import runtime_roots

from tests.compile_call_controller_helpers import work as work
from tests.compile_leased_entrypoint_helpers import (
    _configured_worker,
    _run,
    runtime,  # noqa: F401
)
from tests.compile_paths import point_compile_path
from tests.compile_progress_admission_helpers import upgraded as upgraded
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case


def _forbidden(*args, **kwargs):
    raise AssertionError('leased work used legacy subprocess or recaptured saved inputs')


def test_default_leased_compile_uses_controller_and_parent_can_settle(work, runtime, monkeypatch):  # noqa: F811
    monkeypatch.setattr(memory_queue, 'manual_compile', _forbidden)
    assert _run(work) is True
    assert len(runtime) == 3
    assert not compile_lock.compile_lock_file().exists()
    assert memory_state.load_state()['last_compile_status'] == 'ok'
    work[0].publish_result(work[1], operation_id=work[1].id, result=b'complete')
    assert work[0].acknowledge(work[1]).state == 'succeeded'


def test_leased_recovery_uses_saved_sources_without_resolving_again(work, runtime, monkeypatch):  # noqa: F811
    assert _run(work) is True
    next(runtime_roots.daily_dir().glob('????-??-??.md')).write_bytes(b'changed live source')
    point_compile_path(monkeypatch, 'select_dailies', _forbidden)
    point_compile_path(monkeypatch, 'resolve_compile_plan', _forbidden)
    assert _run(work) is True
    assert len(runtime) == 3


def test_other_live_compile_owner_prevents_source_or_model_work(work, runtime, monkeypatch):  # noqa: F811
    compile_lock.write_lock(os.getppid())
    point_compile_path(monkeypatch, 'prepare_compile_planning', _forbidden)
    assert _run(work) is False
    assert runtime == []
    assert compile_lock.read_lock()['pid'] == os.getppid()
    assert 'last_compile_status' not in memory_state.load_state()


def test_stale_lease_is_refused_before_compile_start(work, runtime):  # noqa: F811
    stale = replace(work[1], token='stale')
    with pytest.raises(queue_core.LeaseFenceError):
        _run((work[0], stale, work[2]))
    assert runtime == []
    assert not compile_lock.compile_lock_file().exists()


def test_invalid_answer_never_finishes_task_or_leaves_lock(work, runtime, monkeypatch):  # noqa: F811
    monkeypatch.setitem(llm_client.BACKENDS, 'fake', lambda *args: llm_client.BackendResponse('invalid JSON'))
    assert _run(work) is False
    assert memory_state.load_state()['last_compile_status'] == 'error'
    assert not compile_lock.compile_lock_file().exists()
    with pytest.raises(queue_core.CompileCompletionError):
        work[0].publish_result(work[1], operation_id=work[1].id, result=b'claimed success')


def test_uncertain_provider_blocks_task_and_releases_compile_lock(work, runtime, monkeypatch):  # noqa: F811
    def unknown(*args):
        raise TimeoutError('provider may have run')

    monkeypatch.setitem(llm_client.BACKENDS, 'fake', unknown)
    with pytest.raises(controller.ProviderOutcomeUnknown):
        _run(work)
    assert work[0].get(work[1].id).blocked_capability == 'provider_outcome_unknown'
    assert not compile_lock.compile_lock_file().exists()


@pytest.mark.parametrize('version,kind', [(3, 'compile'), (4, 'query')])
def test_unmigrated_or_other_kind_preserves_existing_processor(monkeypatch, version, kind):
    task = {'type': kind, 'payload': {}}
    monkeypatch.setattr(memory_queue, 'manual_processor', lambda received: received == task)
    queue = SimpleNamespace(expected_contract=SimpleNamespace(user_version=version))
    lease = SimpleNamespace(kind=kind)
    assert memory_queue.manual_lease_processor(queue, lease, time.monotonic() + 60, task) is True


def test_queue_cli_selects_lease_aware_default_processor(monkeypatch):
    observed = []

    def worker(processor, **kwargs):
        observed.append(processor)
        return queue_core.WorkerSummary(0, 0, 0, 0, 0)

    monkeypatch.setattr(queue_core, 'run_worker', worker)
    parser = memory_queue.build_cli_parser()
    assert memory_queue.cli_work(parser.parse_args(['work']), parser) == 0
    assert isinstance(observed[0], queue_core.LeaseProcessor)
    assert observed[0].process is memory_queue.manual_lease_processor


def test_spawned_lease_compiles_recovers_and_parent_settles(work, runtime):  # noqa: F811
    # Same hosted-platform allowance used by the existing worker handoff tests.
    deadline = time.monotonic() + 60
    processor = queue_core.LeaseProcessor(_configured_worker)
    bound = queue_core.bind_lease_processor(processor, work[0], work[1], deadline)
    assert queue_core.run_processor_child(bound, queue_core.compat_task(work[1]),
                                             deadline - time.monotonic()) is True
    counts = {'ok': 0, 'failed': 0, 'dead': 0, 'skipped': 0, 'halted': 0}
    queue_core.run_and_settle(work[0], processor, queue_core.run_processor_child,
        work[1], counts, deadline=deadline, monotonic=time.monotonic,
        lease_seconds=queue_core.DEFAULTS.queue_lease_seconds,
        heartbeat_seconds=queue_core.DEFAULTS.queue_heartbeat_seconds,
        max_attempts=queue_core.DEFAULTS.queue_max_attempts,
        retry_base_seconds=queue_core.DEFAULTS.retry_base_seconds,
        retry_cap_seconds=queue_core.DEFAULTS.retry_cap_seconds)
    assert counts['ok'] == 1
    assert work[0].get(work[1].id).state == 'succeeded'
    assert (work[0].state_root / 'leased-call-counts').read_text().splitlines() == ['3', '0']


def test_worker_rejects_changed_compatibility_payload(work, runtime):  # noqa: F811
    task = {**queue_core.compat_task(work[1]), 'payload': {'changed': True}}
    with pytest.raises(queue_core.LeaseFenceError, match='compatibility payload'):
        memory_queue.manual_lease_processor(work[0], work[1], float('inf'), task)
    assert runtime == []
    assert not compile_lock.compile_lock_file().exists()


def test_unknown_compile_handler_version_never_runs_as_current(work, runtime):  # noqa: F811
    work[0].cancel(work[1].id)
    work[0].enqueue('compile', 2, {})
    lease = work[0].claim('future-version')
    with pytest.raises(ValueError, match='handler version'):
        compile_leased_run.run_leased_compile(work[0], lease)
    assert runtime == []
    assert not compile_lock.compile_lock_file().exists()


def test_empty_pending_sources_still_prove_completion_without_model_calls(work, runtime):  # noqa: F811
    for path in runtime_roots.daily_dir().glob('????-??-??.md'):
        path.unlink()
    assert _run(work) is True
    assert runtime == []
    assert memory_state.load_state()['last_compile_outcome'] == 'nothing'
    work[0].publish_result(work[1], operation_id=work[1].id, result=b'empty complete')
    assert work[0].acknowledge(work[1]).state == 'succeeded'


def test_changed_lease_input_is_refused_before_compile_lock(work, runtime, monkeypatch):  # noqa: F811
    observed = []
    point_compile_path(monkeypatch, 'compile_under_lock', lambda *args, **kwargs: observed.append(True))
    changed = replace(work[1], payload={'compile_request': {'file': 'another-day'}})
    with pytest.raises(queue_core.LeaseFenceError, match='input'):
        _run((work[0], changed, work[2]))
    assert observed == []
    assert runtime == []
    assert work[0].get(work[1].id).state == 'leased'
