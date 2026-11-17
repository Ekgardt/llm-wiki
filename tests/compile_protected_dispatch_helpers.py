"""Helpers shared by test_compile_protected_dispatch.py and the test files importing them."""

from dataclasses import replace

import compile_controller as controller
import llm_client as client
import pytest

from tests.compile_call_controller_helpers import work as work


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    return replace(client.provider_candidates('fake')[0], model='test-model')


def _dependencies(seed='fixture'):
    return controller.CompileDependencies.capture(lambda: {'fixture': seed})


@pytest.fixture
def compiler_case(work, monkeypatch):
    from tests.compile_critic_drop_cannot_complete_source_work_helpers import _inputs

    queue, _, _ = work
    root = queue.vault
    state = queue.db_path.parent.parent
    for relative in ('knowledge/daily/receipts', 'knowledge/notes'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in ('knowledge/index.md', 'knowledge/log.md', 'AGENTS.md'):
        (root / relative).write_text('# Test snapshot\n')
    monkeypatch.setenv('LLM_WIKI_ROOT', str(root))
    monkeypatch.setenv('LLM_WIKI_STATE_ROOT', str(state))
    return _inputs(root), state


def _archive_members(inputs):
    import io
    import zipfile

    handle = io.BytesIO()
    digest = controller.compile_inputs_digest(inputs)
    controller.write_archive_snapshot(inputs, digest, handle)
    with zipfile.ZipFile(handle) as archive:
        return {name: archive.read(name) for name in archive.namelist()}
