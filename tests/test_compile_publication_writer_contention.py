"""A real competing writer must not discard an already validated compile plan."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import compile_memory as compiler
import markdown_transaction
import pytest
from markdown_transaction import MarkdownCoordinator

from tests import test_compile_transactions
from tests.slow_machine import LONG_TIMEOUT
from tests.test_compile_transactions import _daily, _semantic_plan


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return test_compile_transactions.vault.__wrapped__(tmp_path, monkeypatch)


def _hold_writer(coordinator, entered, release):
    with coordinator.writer_gate():
        entered.set()
        assert release.wait(LONG_TIMEOUT)


def _coordinator_pair(root, state_root, contract):
    if contract == 'legacy':
        return MarkdownCoordinator(root, state_root), MarkdownCoordinator(root, state_root)
    path = state_root / 'run/markdown-transactions-v3.candidate.sqlite3'
    markdown_transaction.initialize_coordinator_v3_candidate(path, source_v2=None)
    return tuple(_v3_coordinator(path, state_root, root) for _ in range(2))


def _v3_coordinator(path, state_root, root):
    coordinator = MarkdownCoordinator._from_v3_candidate(path, state_root=state_root)
    coordinator.vault = root  # Same binding as active_markdown_coordinator.
    return coordinator


@contextmanager
def _contended_publication(vault, monkeypatch, contract='legacy'):
    root, state_root = vault
    inputs = compiler.snapshot_compile_inputs([_daily(root)])
    entered, release = threading.Event(), threading.Event()
    holder, publisher = _coordinator_pair(root, state_root, contract)
    original_gate = publisher.writer_gate
    monkeypatch.setattr(markdown_transaction, '_WRITER_WAIT_SECONDS', 0.01)

    @contextmanager
    def release_after_refusal(**kwargs):
        try:
            with original_gate(**kwargs) as owner:
                yield owner
        except TimeoutError:
            release.set()
            raise

    monkeypatch.setattr(publisher, 'writer_gate', release_after_refusal)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_hold_writer, holder, entered, release)
        assert entered.wait(LONG_TIMEOUT)
        try:
            yield inputs, publisher, release
        finally:
            release.set()
            future.result(timeout=LONG_TIMEOUT)


@pytest.mark.parametrize('contract', ['legacy', 'v3'])
def test_validated_plan_survives_a_real_writer_wait_refusal(vault, monkeypatch, contract):
    with _contended_publication(vault, monkeypatch, contract) as (inputs, coordinator, release):
        result = compiler.apply_compile_plan(
            inputs, _semantic_plan(), action_key='b' * 64, trigger='manual',
            coordinator=coordinator, completed_at='2026-07-14T12:00:00Z',
            deadline=time.monotonic() + LONG_TIMEOUT,
        )
        assert release.is_set()
        assert result.state == 'committed'
    assert (vault[0] / 'knowledge/notes/exact-byte-pattern.md').is_file()


def test_writer_contention_still_obeys_cancellation(vault, monkeypatch):
    with _contended_publication(vault, monkeypatch) as (inputs, coordinator, release):
        with pytest.raises(TimeoutError):
            compiler.apply_compile_plan(
                inputs, _semantic_plan(), action_key='c' * 64, trigger='manual',
                coordinator=coordinator, completed_at='2026-07-14T12:00:00Z',
                cancelled=release.is_set,
            )
    assert not (vault[0] / 'knowledge/notes/exact-byte-pattern.md').exists()


def test_writer_admission_respects_an_expired_caller_deadline(vault):
    coordinator = MarkdownCoordinator(*vault)
    with pytest.raises(TimeoutError, match='compile deadline'):
        with compiler._compile_writer_gate(coordinator, None, time.monotonic(), None):
            pytest.fail('expired compile acquired admission')


def test_timeout_after_admission_is_never_retried(vault):
    coordinator = MarkdownCoordinator(*vault)
    entries = []
    with pytest.raises(TimeoutError, match='global Markdown writer gate'):
        with compiler._compile_writer_gate(coordinator, None, float('inf'), None):
            entries.append('entered')
            raise TimeoutError('timed out waiting for the global Markdown writer gate')
    assert entries == ['entered']


@pytest.mark.parametrize('contract', ['legacy', 'v3'])
def test_writer_refusal_keeps_its_contention_identity(vault, monkeypatch, contract):
    from capture_diagnostics import is_contention
    from integration_adapter import _checkpoint_log_kind

    with _contended_publication(vault, monkeypatch, contract) as (_, coordinator, _):
        with pytest.raises(TimeoutError) as caught:
            with coordinator.writer_gate(wait_seconds=0):
                pytest.fail('competing writer was admitted')
    assert is_contention(caught.value)
    assert _checkpoint_log_kind(caught.value) == 'project checkpoint contention'


def test_same_text_is_not_proof_of_writer_contention():
    from types import SimpleNamespace

    error = TimeoutError('timed out waiting for the global Markdown writer gate')
    calls = []

    def enter(_context):
        calls.append(1)
        if len(calls) == 1:
            raise error
        return 'incorrectly retried'

    coordinator = SimpleNamespace(writer_gate=lambda **kwargs: None)
    stack = SimpleNamespace(enter_context=enter)
    with pytest.raises(TimeoutError) as caught:
        compiler._enter_compile_writer_gate(stack, coordinator, None, float('inf'), None)
    assert caught.value is error
    assert calls == [1]


def test_sqlite_busy_expiry_keeps_the_original_cause():
    import sqlite3

    from capture_diagnostics import is_contention

    error = sqlite3.OperationalError('busy')
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    with pytest.raises(TimeoutError) as caught:
        markdown_transaction._retry_gate_or_fail(error, 0, time.monotonic() - 1)
    assert caught.value.__cause__ is error
    assert is_contention(caught.value)


def test_unrelated_timeout_is_not_classified_as_contention():
    from capture_diagnostics import is_contention

    assert not is_contention(TimeoutError('timed out waiting for the global Markdown writer gate'))
