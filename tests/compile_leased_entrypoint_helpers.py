"""Helpers shared by test_compile_leased_entrypoint.py and the test files importing them."""

import llm_client
import memory_queue
import pytest
import queue_core

from tests.compile_call_controller_helpers import work as work
from tests.compile_durable_stream_capture_helpers import _paths
from tests.compile_execution_inventory_helpers import _components
from tests.compile_paths import point_compile_path
from tests.compile_protected_dispatch_helpers import compiler_case as compiler_case
from tests.compile_saved_context_refresh_helpers import _semantic_backend
from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import _provider


@pytest.fixture
def runtime(work, compiler_case, monkeypatch):
    root, state = work[0].vault, work[0].state_root
    monkeypatch.setenv('LLM_WIKI_ROOT', str(root))
    monkeypatch.setenv('LLM_WIKI_STATE_ROOT', str(state))
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    _components(monkeypatch, root)
    _paths(compiler_case)
    point_compile_path(monkeypatch, 'provider_candidates', lambda *args, **kwargs: [_provider()])
    return _semantic_backend(monkeypatch)


def _run(work):
    return memory_queue.manual_lease_processor(work[0], work[1], float('inf'),
                                               queue_core.compat_task(work[1]))


def _configured_worker(queue, lease, deadline, task, *, backend=None, **options):
    """Child-side fixture adapters; ownership, compiler and settlement stay real."""
    with pytest.MonkeyPatch.context() as patch:
        _components(patch, queue.vault)
        point_compile_path(patch, 'provider_candidates', lambda *args, **kwargs: [_provider()])
        calls = _semantic_backend(patch)
        if backend is not None:
            patch.setitem(llm_client.BACKENDS, 'fake', backend)
        result = memory_queue.manual_lease_processor(queue, lease, deadline, task, **options)
        with (queue.state_root / 'leased-call-counts').open('a') as handle:
            handle.write(str(len(calls)) + '\n')
        return result
