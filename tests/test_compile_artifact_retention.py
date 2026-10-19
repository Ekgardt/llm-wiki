"""Ordinary retention must not break durable compile recovery evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import compile_progress as progress
import memory_queue
import pytest

from tests.test_compile_progress_admission import upgraded as upgraded


@pytest.fixture
def queue(upgraded):
    root, state, _ = upgraded
    return memory_queue.active_memory_queue(root, state)


def _shared_answer(queue):
    owner = queue.claim('compile-owner')
    query_id = queue.enqueue('query', 1, {'prompt': 'shared answer'})
    query = queue.claim('query-owner')
    reference = queue.publish_result(query, operation_id='shared-answer', result=b'answer')
    queue.acknowledge(query)
    event = progress.CompileEvent(
        'c' * 64, owner.id, 0, 'd' * 64, 'pending', 'ready',
        {'artifact': {'reference': reference, 'sha256': memory_queue.sha256_bytes(b'answer')}},
    )
    return owner, query_id, event


def _age(queue, task_id):
    before = datetime.now(timezone.utc) - timedelta(
        days=memory_queue.DEFAULTS.queue_result_retention_days + 1,
    )
    with queue.connection() as database:
        database.execute('UPDATE tasks SET updated_at=? WHERE id=?',
                         (before.isoformat(timespec='microseconds'), task_id))
    return before + timedelta(hours=1)


def test_purge_keeps_answer_named_by_an_older_compile_event(queue, tmp_path):
    owner, query_id, event = _shared_answer(queue)
    queue.record_compile_event(owner, event)
    queue.record_compile_event(owner, replace(event, sequence=1, details={}))
    cutoff = _age(queue, query_id)
    receipt = queue.purge(terminal_before=cutoff, export_path=tmp_path / 'export')
    assert receipt.task_ids == (query_id,)
    assert queue.read_compile_artifact(event) == b'answer'
    assert queue.get(owner.id).state == 'leased'


def test_checkpoint_revalidates_bytes_inside_its_write_transaction(queue, monkeypatch):
    owner, _, event = _shared_answer(queue)
    original = queue._with_verified_lease

    def lose_file_before_transaction(lease, action):
        (queue.state_root / event.details['artifact']['reference']).unlink()
        return original(lease, action)

    monkeypatch.setattr(queue, '_with_verified_lease', lose_file_before_transaction)
    with pytest.raises(progress.ProgressCorrupt, match='unavailable'):
        queue.record_compile_event(owner, event)
    assert queue.get_compile_event(event.work_id) is None


def test_unreferenced_result_is_deleted_under_the_queue_write_lock(queue, monkeypatch):
    import sqlite3
    from pathlib import Path

    reference, _ = queue._store_result_bytes('unreferenced', b'answer')
    path = queue.state_root / reference
    original = Path.unlink
    observed = []

    def unlink_with_competing_writer(target, *args, **kwargs):
        if target == path:
            _require_other_writer_blocked(queue.db_path, sqlite3)
            observed.append(target)
        return original(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', unlink_with_competing_writer)
        queue._cleanup_ordinary_result({'result': {'reference': reference}}, set())
    assert observed == [path]
    assert not path.exists()


def _require_other_writer_blocked(path, sqlite3):
    from contextlib import closing

    with closing(sqlite3.connect(path, timeout=0)) as other:
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.execute('BEGIN IMMEDIATE')


def _capture_export(queue, tmp_path, event):
    reference = event.details['artifact']['reference']
    archive = memory_queue._capture_purge_archive_path(event.task_id, reference)
    export = tmp_path / 'capture-export'
    target = export / archive
    target.parent.mkdir(parents=True)
    target.write_bytes(queue.read_compile_artifact(event))
    target.chmod(0o600)
    artifact = memory_queue._CapturePurgeArtifact(
        reference, archive, event.details['artifact']['sha256'],
    )
    return export, artifact


def test_capture_source_cleanup_also_keeps_a_compile_reference(queue, tmp_path):
    owner = queue.claim('compile-owner')
    event = queue.save_compile_artifact(owner, progress.CompileEvent(
        'c' * 64, owner.id, 0, 'd' * 64, 'pending', 'ready', {},
    ), b'answer')
    export, artifact = _capture_export(queue, tmp_path, event)
    queue._unlink_capture_purge_artifact(export, owner.id, artifact)
    assert queue.read_compile_artifact(event) == b'answer'


def test_corrupt_journal_blocks_unreferenced_file_deletion(queue):
    owner, _, event = _shared_answer(queue)
    queue.record_compile_event(owner, event)
    reference, _ = queue._store_result_bytes('unreferenced', b'unrelated')
    with queue.connection() as database:
        database.execute('DROP TRIGGER compile_work_events_no_update')
        database.execute("UPDATE compile_work_events SET event_digest=?", ('0' * 64,))
        database.execute(progress.SCHEMA_STATEMENTS[1])
    with pytest.raises(progress.ProgressCorrupt, match='digest mismatch'):
        queue._cleanup_ordinary_result({'result': {'reference': reference}}, set())
    assert (queue.state_root / reference).read_bytes() == b'unrelated'


def test_task_reference_remains_protected_without_compile_events(queue):
    _, _, event = _shared_answer(queue)
    reference = event.details['artifact']['reference']
    queue._cleanup_ordinary_result({'result': {'reference': reference}}, set())
    assert (queue.state_root / reference).read_bytes() == b'answer'


@pytest.mark.parametrize('terminal', ['cancelled', 'succeeded'])
def test_retained_compile_history_does_not_block_unrelated_task_purge(queue, tmp_path, terminal):
    owner, query_id, event = _shared_answer(queue)
    queue.record_compile_event(owner, event)
    _finish_compile(queue, owner, terminal)
    _age(queue, owner.id)
    cutoff = _age(queue, query_id)
    receipt = queue.purge(terminal_before=cutoff, export_path=tmp_path / 'export')
    assert receipt.task_ids == (query_id,)
    assert receipt.retained == (owner.id,)
    assert queue.get(owner.id).state == terminal
    assert queue.get_compile_event(event.work_id) == event
    assert queue.read_compile_artifact(event) == b'answer'


def _finish_compile(queue, owner, terminal):
    if terminal == 'cancelled':
        queue.cancel(owner.id)
        return
    queue.publish_result(owner, operation_id='compile-final', result=b'final')
    queue.acknowledge(owner)


def test_slow_artifact_validation_cannot_commit_after_lease_expiry(queue, monkeypatch):
    owner, _, event = _shared_answer(queue)
    original = queue.read_compile_artifact

    def read_past_expiry(value):
        data = original(value)
        monkeypatch.setattr(memory_queue, '_utc_now', lambda: owner.expires_at + timedelta(seconds=1))
        return data

    monkeypatch.setattr(queue, 'read_compile_artifact', read_past_expiry)
    with pytest.raises(memory_queue.LeaseFenceError):
        queue.record_compile_event(owner, event)
    assert queue.get_compile_event(event.work_id) is None
