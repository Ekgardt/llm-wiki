"""Service reads must stream and must not confuse unreadable with absent."""
from contextlib import contextmanager

import compile_pipeline
import compile_receipts
import maybe_compile
import pytest
from llm_wiki import compile_pipeline_selection, compile_pipeline_sources
from queue_core import (
    MemoryQueue,
)

from tests.compile_paths import point_compile_path
from tests.compile_transactions_helpers import vault as vault


def _day(root):
    path = root / 'knowledge/daily/2026-10-20.md'
    path.write_bytes(b'## [12:34:56] event\nComplete evidence.\n')
    return path, path.relative_to(root).as_posix()


@pytest.mark.parametrize('error', [PermissionError, OSError, TimeoutError, ValueError])
def test_unreadable_source_never_retires_its_failure(vault, monkeypatch, error):
    root, state = vault
    path, logical = _day(root)
    digest = compile_pipeline.sha256_bytes(path.read_bytes())
    queue = MemoryQueue(state)
    queue.record_source_failure(logical, digest, error_code='ValueError', producer='compile')

    def refuse(*args, **kwargs):
        raise error('source cannot be verified')

    point_compile_path(monkeypatch, '_read_daily_source', refuse)
    point_compile_path(monkeypatch, 'open_daily_source', refuse)
    with pytest.raises(error, match='cannot be verified'):
        compile_pipeline.retire_stale_source_failures(state)
    assert queue.source_failure_keys() == [(logical, digest)]


def test_service_readers_do_not_use_eager_daily_or_part_list(vault, monkeypatch):
    root, state = vault
    path, logical = _day(root)
    coordinator = compile_pipeline.MarkdownCoordinator(root, state)
    selection = compile_receipts.receipt_predicate(coordinator)

    def refuse(*args, **kwargs):
        raise AssertionError('eager daily or parts')

    point_compile_path(monkeypatch, '_read_daily_source', refuse)
    point_compile_path(monkeypatch, 'daily_parts', refuse)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    assert compile_pipeline_selection.explicit_daily(path, coordinator, selection=selection) == [path]
    assert not compile_pipeline_selection.daily_already_compiled(path, selection)
    assert not maybe_compile.daily_receipted(path, selection)
    assert compile_pipeline_selection.whole_daily_digest(logical, selection) is None
    owners = {}
    compile_pipeline_selection.record_receipt_owners(owners, path)
    assert owners
    assert compile_pipeline.current_source_digests(logical, coordinator=coordinator)


def test_optional_source_does_not_swallow_consumer_missing_file(vault):
    root, _ = vault
    path, _ = _day(root)
    with pytest.raises(FileNotFoundError, match='receipt disappeared'):
        with compile_pipeline_sources.open_optional_daily_source(path):
            raise FileNotFoundError('receipt disappeared')


def test_optional_source_only_reports_actual_absence(vault):
    root, _ = vault
    with compile_pipeline_sources.open_optional_daily_source(root / 'knowledge/daily/missing.md') as source:
        assert source is None


def test_final_source_verification_failure_preserves_queue_evidence(vault, monkeypatch):
    root, state = vault
    path, logical = _day(root)
    queue = MemoryQueue(state)
    old_digest = '0' * 64
    queue.record_source_failure(logical, old_digest, error_code='ValueError', producer='compile')
    original = compile_receipts.open_daily_source

    @contextmanager
    def changed_at_exit(*args, **kwargs):
        with original(*args, **kwargs) as source:
            yield source
            raise ValueError('source changed before release')

    point_compile_path(monkeypatch, 'open_daily_source', changed_at_exit)
    with pytest.raises(ValueError, match='changed before release'):
        compile_pipeline.retire_stale_source_failures(state)
    assert path.exists()
    assert queue.source_failure_keys() == [(logical, old_digest)]


@pytest.mark.parametrize('legacy', [False, True])
def test_doctor_daily_check_uses_owned_range(vault, monkeypatch, legacy):
    from llm_wiki import doctor_transactions

    root, state = vault
    _path, logical = _day(root)
    coordinator = compile_pipeline.MarkdownCoordinator(root, state)
    checker = doctor_transactions.CompiledDaySupersession(root, state, database=object())
    checker.selection = compile_receipts.receipt_predicate(coordinator)

    def refuse(*args, **kwargs):
        raise AssertionError('doctor materialized daily')

    monkeypatch.setattr(doctor_transactions, 'read_stable_bytes', refuse)
    if legacy:
        source = {"logical_path": logical, "sha256": compile_pipeline.sha256_bytes(_path.read_bytes())}
        assert not checker._whole_legacy_source_compiled(source)
        return
    assert not checker._day_compiled(logical, set())
