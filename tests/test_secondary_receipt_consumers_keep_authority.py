"""Secondary consumers preserve receipts and honor actual caller deadlines."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import compile_memory as compiler
import maybe_compile
import memory_queue
import memory_state
import pytest
from reliable_memory import canonical_json_bytes

from tests.test_a_continuation_keeps_its_original_entry import _published_continuation
from tests.test_compile_transactions import vault as vault
from tests.test_doctor_another_legacy_receipt_is_not_the_staged_receipt import race as race


def test_unsupported_version_stops_discard_before_any_deletion(vault):
    root, _state = vault
    directory = root / "knowledge/daily/receipts"
    corrupt = directory / "a-corrupt.md"
    future = directory / "z-future.md"
    corrupt.write_bytes(b"malformed historical receipt")
    record = {"schema_version": "compile-receipt/v999", "source": {"logical_path": "knowledge/daily/2026-07-14.md", "sha256": "a" * 64}}
    future.write_bytes(b"```json\n" + canonical_json_bytes(record) + b"\n```\n")
    with pytest.raises(compiler.UnsupportedCompileReceiptVersion):
        compiler.discard_unusable_receipts()
    assert corrupt.read_bytes() == b"malformed historical receipt"
    assert json.loads(future.read_bytes().split(b"\n")[1]) == record


def test_context_receipt_owner_is_the_actual_daily(vault):
    daily, _coordinator, part = _published_continuation(vault)
    path = compiler._context_receipt_path(compiler._v4_source_descriptor(part))
    assert compiler._receipt_owners()[path.name] == daily.name


def test_discard_invalidates_retained_context_owner_before_unlink(vault, monkeypatch):
    daily, _coordinator, part = _published_continuation(vault)
    path = compiler._context_receipt_path(compiler._v4_source_descriptor(part))
    raw = path.read_bytes()
    record = json.loads(raw.split(b"```json\n", 1)[1].split(b"\n```", 1)[0])
    record['operations'] = []
    path.write_bytes(b'```json\n' + canonical_json_bytes(record) + b'\n```\n')
    daily.write_bytes(daily.read_bytes().replace(b'10:00:00', b'12:00:00', 1))
    mirror = {'compiled_daily_hashes': {daily.name: 'a' * 64}}
    monkeypatch.setattr(compiler, 'update_state', lambda mutator: mutator(mirror))
    assert compiler.discard_unusable_receipts() == [path.name]
    assert not path.exists()
    assert daily.name not in mirror['compiled_daily_hashes']


def test_copied_context_filename_cannot_declare_a_daily_owner(vault):
    _daily, _coordinator, part = _published_continuation(vault)
    path = compiler._context_receipt_path(compiler._v4_source_descriptor(part))
    copied = path.with_name('v4-' + 'f' * 64 + '.md')
    copied.write_bytes(path.read_bytes())
    assert copied.name not in compiler._receipt_owners()


def test_expired_mirror_and_source_read_do_not_mutate(vault, monkeypatch):
    _daily, coordinator, part = _published_continuation(vault)
    monkeypatch.setattr(compiler, "update_state", lambda *_args: pytest.fail("expired mirror wrote state"))
    with pytest.raises(TimeoutError):
        compiler._repair_compile_mirror(coordinator, deadline=time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        compiler._current_source_digests(part.logical_path, coordinator=coordinator, deadline=time.monotonic() - 1)


def _legacy_queue(root):
    return memory_queue.MemoryQueue(root)


def _v3_queue(root):
    path = root / "run/queue-v3.candidate.sqlite3"
    memory_queue.initialize_queue_v3_candidate(path, source_v2=None)
    return memory_queue.MemoryQueue._from_v3_candidate(path, state_root=root)


@pytest.fixture(params=[_legacy_queue, _v3_queue])
def queue(request, tmp_path):
    return request.param(tmp_path)


def _record_failure(queue):
    queue.record_source_failure("knowledge/daily/2026-07-14.md", "a" * 64, error_code="unprocessed", producer="compile")


def test_expired_failure_read_and_clear_preserve_evidence(queue):
    _record_failure(queue)
    with pytest.raises(TimeoutError):
        queue.source_failure_keys(deadline=time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        queue.clear_source_failure("knowledge/daily/2026-07-14.md", "a" * 64, deadline=time.monotonic() - 1)
    assert queue.source_failure_keys() == [("knowledge/daily/2026-07-14.md", "a" * 64)]


def test_locked_failure_clear_honors_short_caller_deadline(queue):
    _record_failure(queue)
    blocker = sqlite3.connect(queue.db_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            queue.clear_source_failure("knowledge/daily/2026-07-14.md", "a" * 64, deadline=started + 0.05)
    finally:
        blocker.rollback()
        blocker.close()
    assert time.monotonic() - started < 1
    assert queue.source_failure_keys() == [("knowledge/daily/2026-07-14.md", "a" * 64)]


def test_expired_failure_record_writes_nothing(queue):
    with pytest.raises(TimeoutError):
        queue.record_source_failure("knowledge/daily/2026-07-14.md", "a" * 64, error_code="unprocessed", producer="compile", deadline=time.monotonic() - 1)
    assert queue.source_failure_keys() == []


def test_failure_record_rolls_back_when_deadline_expires_before_commit(queue, monkeypatch):
    def expired(_connection, _deadline):
        raise TimeoutError("caller expired before commit")

    monkeypatch.setattr(memory_queue, "_source_failure_commit_active", expired)
    with pytest.raises(TimeoutError, match="before commit"):
        queue.record_source_failure("knowledge/daily/2026-07-14.md", "a" * 64, error_code="unprocessed", producer="compile", deadline=time.monotonic() + 2)
    assert queue.source_failure_keys() == []


def test_expired_cleanup_keeps_published_receipt_and_failure(vault):
    _daily, _coordinator, part = _published_continuation(vault)
    root, state = vault
    queue = memory_queue.active_or_legacy_memory_queue(root, state)
    queue.record_source_failure(part.logical_path, part.sha256, error_code="unprocessed", producer="compile")
    path = root / compiler._context_receipt_path(compiler._v4_source_descriptor(part))
    published = path.read_bytes()
    with pytest.raises(TimeoutError):
        compiler._clear_compile_source_failures(None, state, deadline=time.monotonic() - 1)
    assert path.read_bytes() == published
    assert queue.source_failure_keys() == [(part.logical_path, part.sha256)]


def test_corrupt_matching_legacy_receipt_remains_visible_beside_valid_context_receipt(vault):
    _daily, coordinator, part = _published_continuation(vault)
    legacy = compiler.ROOT / compiler.compile_receipt_path(compiler.compile_source_identity(part.logical_path, part.sha256))
    legacy.write_bytes(b"corrupt matching historical receipt")
    with pytest.raises(ValueError):
        compiler._receipt_predicate(coordinator).matches(part)


def test_new_matching_legacy_receipt_invalidates_missing_record_cache(vault):
    _daily, coordinator, part = _published_continuation(vault)
    selection = compiler._receipt_predicate(coordinator)
    assert selection.matches(part)
    legacy = compiler.ROOT / compiler.compile_receipt_path(compiler.compile_source_identity(part.logical_path, part.sha256))
    legacy.write_bytes(b"corrupt matching historical receipt")
    with pytest.raises(ValueError):
        selection.matches(part)


def test_valid_historical_receipt_is_cached_but_never_current_authority(race, monkeypatch):
    root, state = Path(race['root']), Path(race['state'])
    content = (root / 'knowledge/daily/2026-07-14.md').read_bytes()
    part = compiler._daily_parts('knowledge/daily/2026-07-14.md', content)[-1]
    coordinator = compiler.MarkdownCoordinator(root, state)
    calls = []
    original = compiler.read_compile_receipt_v3

    def observed(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, 'read_compile_receipt_v3', observed)
    selection = compiler._receipt_predicate(coordinator, deadline=time.monotonic() + 2)
    assert not selection.matches(part)
    assert not selection.matches(part)
    assert len(calls) == 1
    path = root / 'knowledge/daily/receipts' / f'v3-{compiler.compile_source_identity(part.logical_path, part.sha256)}.md'
    path.write_bytes(b'corrupt changed legacy authority')
    with pytest.raises(ValueError):
        selection.matches(part)
    assert len(calls) == 2


def test_mirror_lock_wait_uses_remaining_caller_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_state, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(memory_state, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(memory_state, 'LOCK_FILE', tmp_path / 'state.lock')
    with memory_state._state_lock():
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            compiler._update_state_under_clock(lambda state: state.update(last_compile_error='test'), started + 0.03)
        assert time.monotonic() - started < 0.5
    assert not (tmp_path / 'state.json').exists()


def test_expired_commit_sequence_lookup_is_refused(vault):
    _daily, coordinator, _part = _published_continuation(vault)
    with pytest.raises(TimeoutError):
        coordinator.operation_id_at(1, deadline=time.monotonic() - 1)


def test_authority_trigger_detects_unprocessed_parts_despite_matching_mirror(vault, monkeypatch):
    daily, _coordinator, _part = _published_continuation(vault)
    root, state = vault
    monkeypatch.setattr(maybe_compile, "ROOT", root)
    monkeypatch.setattr(maybe_compile, "STATE_ROOT", state)
    assert maybe_compile._has_pending_work(deadline=time.monotonic() + 2)
    assert daily.exists()


def test_saved_source_uses_context_and_canonical_authority(vault):
    daily, coordinator, part = _published_continuation(vault)
    source = compiler._v4_source_descriptor(part)
    selection = compiler._receipt_predicate(coordinator, deadline=time.monotonic() + 2)
    assert selection.matches_saved_source(source)
    daily.write_bytes(daily.read_bytes() + b"## [12:00:00] session-end | manual\n" + b"A later observation.\n" * 1000)
    assert selection.matches_saved_source(source)
    daily.write_bytes(daily.read_bytes().replace(b"10:00:00", b"12:00:00", 1))
    assert not selection.matches_saved_source(source)


def test_saved_source_rejects_malformed_and_expired(vault):
    _daily, coordinator, part = _published_continuation(vault)
    source = compiler._v4_source_descriptor(part)
    selection = compiler._receipt_predicate(coordinator)
    with pytest.raises(ValueError):
        selection.matches_saved_source({**source, "original_sha256": "not-a-digest"})
    expired = compiler._receipt_predicate(coordinator, deadline=time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        expired.matches_saved_source(source)


def test_locked_failure_read_honors_short_caller_deadline(queue):
    _record_failure(queue)
    blocker = sqlite3.connect(queue.db_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            queue.source_failure_keys(deadline=started + 0.05)
    finally:
        blocker.rollback()
        blocker.close()
    assert time.monotonic() - started < 1


def test_locked_commit_sequence_lookup_honors_deadline(vault):
    _daily, coordinator, _part = _published_continuation(vault)
    blocker = sqlite3.connect(coordinator.database_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            coordinator.operation_id_at(1, deadline=started + 0.05)
    finally:
        blocker.rollback()
        blocker.close()
    assert time.monotonic() - started < 1


def test_complete_context_receipt_stops_nonforce_trigger(vault, monkeypatch):
    from markdown_transaction import MarkdownCoordinator

    from tests.test_compile_transactions import _daily, _semantic_plan

    root, state = vault
    daily = _daily(root)
    inputs = compiler.snapshot_compile_inputs([daily])
    batch = compiler.pack_compile_batches(inputs, model="fake-v1")[0]
    coordinator = MarkdownCoordinator(root, state)
    compiler.apply_compile_plan(inputs, _semantic_plan(), action_key="d" * 64, trigger="manual", coordinator=coordinator, completed_at="2026-07-14T12:00:00Z", batch=batch, provider_budget={"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000})
    monkeypatch.setattr(maybe_compile, "ROOT", root)
    monkeypatch.setattr(maybe_compile, "STATE_ROOT", state)
    assert not maybe_compile._has_pending_work(deadline=time.monotonic() + 2)
    assert compiler.daily_is_compiled(daily.relative_to(root).as_posix(), daily.read_bytes(), compiler._receipt_predicate(coordinator))


def test_expired_trigger_refuses_even_before_receipts_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(maybe_compile, "ROOT", tmp_path)
    with pytest.raises(TimeoutError):
        maybe_compile._has_pending_work(deadline=time.monotonic() - 1)


def test_unknown_version_without_current_source_fields_is_preserved(vault):
    root, _state = vault
    path = root / 'knowledge/daily/receipts/future-layout.md'
    raw = b'```json\n' + canonical_json_bytes({'schema_version': 'compile-receipt/v999', 'different_future_source': {'digest': 'a' * 64}}) + b'\n```\n'
    path.write_bytes(raw)
    with pytest.raises(compiler.UnsupportedCompileReceiptVersion):
        compiler.discard_unusable_receipts()
    assert path.read_bytes() == raw
