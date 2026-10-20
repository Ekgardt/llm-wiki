"""Service reads must stream and must not confuse unreadable with absent."""
from contextlib import contextmanager

import compile_memory as compiler
import maybe_compile
import pytest
from memory_queue import MemoryQueue

from tests.test_compile_transactions import vault as vault


def _day(root):
    path = root / 'knowledge/daily/2026-10-20.md'
    path.write_bytes(b'## [12:34:56] event\nComplete evidence.\n')
    return path, path.relative_to(root).as_posix()


@pytest.mark.parametrize('error', [PermissionError, OSError, TimeoutError, ValueError])
def test_unreadable_source_never_retires_its_failure(vault, monkeypatch, error):
    root, state = vault
    path, logical = _day(root)
    digest = compiler.sha256_bytes(path.read_bytes())
    queue = MemoryQueue(state)
    queue.record_source_failure(logical, digest, error_code='ValueError', producer='compile')

    def refuse(*args, **kwargs):
        raise error('source cannot be verified')

    monkeypatch.setattr(compiler, '_read_daily_source', refuse)
    monkeypatch.setattr(compiler, '_open_daily_source', refuse)
    with pytest.raises(error, match='cannot be verified'):
        compiler._retire_stale_source_failures(state)
    assert queue.source_failure_keys() == [(logical, digest)]


def test_service_readers_do_not_use_eager_daily_or_part_list(vault, monkeypatch):
    root, state = vault
    path, logical = _day(root)
    coordinator = compiler.MarkdownCoordinator(root, state)
    selection = compiler._receipt_predicate(coordinator)

    def refuse(*args, **kwargs):
        raise AssertionError('eager daily or parts')

    monkeypatch.setattr(compiler, '_read_daily_source', refuse)
    monkeypatch.setattr(compiler, '_daily_parts', refuse)
    monkeypatch.setattr(maybe_compile, 'ROOT', root)
    assert compiler._explicit_daily(path, coordinator, selection=selection) == [path]
    assert not compiler._daily_already_compiled(path, selection)
    assert not maybe_compile._daily_receipted(path, selection, compiler)
    assert compiler._whole_daily_digest(logical, selection) is None
    owners = {}
    compiler._record_receipt_owners(owners, path)
    assert owners
    assert compiler._current_source_digests(logical, coordinator=coordinator)


def test_optional_source_does_not_swallow_consumer_missing_file(vault):
    root, _ = vault
    path, _ = _day(root)
    with pytest.raises(FileNotFoundError, match='receipt disappeared'):
        with compiler._open_optional_daily_source(path):
            raise FileNotFoundError('receipt disappeared')


def test_optional_source_only_reports_actual_absence(vault):
    root, _ = vault
    with compiler._open_optional_daily_source(root / 'knowledge/daily/missing.md') as source:
        assert source is None


def test_final_source_verification_failure_preserves_queue_evidence(vault, monkeypatch):
    root, state = vault
    path, logical = _day(root)
    queue = MemoryQueue(state)
    old_digest = '0' * 64
    queue.record_source_failure(logical, old_digest, error_code='ValueError', producer='compile')
    original = compiler._open_daily_source

    @contextmanager
    def changed_at_exit(*args, **kwargs):
        with original(*args, **kwargs) as source:
            yield source
            raise ValueError('source changed before release')

    monkeypatch.setattr(compiler, '_open_daily_source', changed_at_exit)
    with pytest.raises(ValueError, match='changed before release'):
        compiler._retire_stale_source_failures(state)
    assert path.exists()
    assert queue.source_failure_keys() == [(logical, old_digest)]


@pytest.mark.parametrize('legacy', [False, True])
def test_doctor_daily_check_uses_owned_range(vault, monkeypatch, legacy):
    import doctor

    root, state = vault
    _path, logical = _day(root)
    coordinator = compiler.MarkdownCoordinator(root, state)
    checker = doctor._CompiledDaySupersession(root, state, database=object())
    checker.selection = compiler._receipt_predicate(coordinator)

    def refuse(*args, **kwargs):
        raise AssertionError('doctor materialized daily')

    monkeypatch.setattr(doctor, 'read_stable_bytes', refuse)
    if legacy:
        source = {"logical_path": logical, "sha256": compiler.sha256_bytes(_path.read_bytes())}
        assert not checker._whole_legacy_source_compiled(source)
        return
    assert not checker._day_compiled(logical, set())
