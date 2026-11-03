"""Actual files preserve publication ordering, identity and failure evidence."""
import hashlib
import io
import os

import pytest
from llm_wiki import queue_artifacts as artifacts
from llm_wiki.queue_contracts import QueueOperationError


def test_stream_publication_is_create_only_and_checks_existing_bytes(tmp_path):
    target = tmp_path / 'artifact'
    expected = hashlib.sha256(b'onetwo').hexdigest()
    assert artifacts._write_durable_stream(target, iter((b'one', b'two'))) == expected
    assert target.read_bytes() == b'onetwo'
    assert artifacts._write_durable_stream(target, (b'onetwo',)) == expected
    with pytest.raises(QueueOperationError, match='durable_file_conflict'):
        artifacts._write_durable_stream(target, (b'changed',))
    assert target.read_bytes() == b'onetwo'
    assert list(tmp_path.glob('.*.tmp')) == []


def test_failed_producer_leaves_no_published_or_temporary_file(tmp_path):
    def producer(output):
        output.write(b'partial')
        raise RuntimeError('producer failed')

    with pytest.raises(RuntimeError, match='producer failed'):
        artifacts._write_durable_producer(tmp_path / 'artifact', producer)
    assert list(tmp_path.iterdir()) == []


def test_expired_deadline_does_not_create_a_file(tmp_path):
    with pytest.raises(TimeoutError):
        artifacts._write_durable_stream(tmp_path / 'artifact', (b'data',), deadline=0)
    assert list(tmp_path.iterdir()) == []


def test_file_sync_failure_keeps_published_bytes_and_propagates(tmp_path, monkeypatch):
    def fail(path):
        raise OSError('sync failed')

    monkeypatch.setattr(artifacts, 'fsync_directory', fail)
    target = tmp_path / 'artifact'
    with pytest.raises(OSError, match='sync failed'):
        artifacts._write_durable_stream(target, (b'data',))
    assert target.read_bytes() == b'data'
    assert list(tmp_path.glob('.*.tmp')) == []


def test_stable_read_and_digest_keep_bytes_and_bound(tmp_path):
    target = tmp_path / 'artifact'
    artifacts._write_durable_file(target, b'data')
    assert artifacts._read_stable_owner_file(target, 4) == b'data'
    assert artifacts._digest_stable_owner_file(target) == (hashlib.sha256(b'data').hexdigest(), 4)
    with pytest.raises(QueueOperationError, match='export_verification_failed'):
        artifacts._read_stable_owner_file(target, 3)


def test_growth_during_digest_is_refused(tmp_path, monkeypatch):
    target = tmp_path / 'artifact'
    artifacts._write_durable_file(target, b'data')
    chunks = artifacts._artifact_chunks

    def changed(handle, remaining, deadline):
        yield from chunks(handle, remaining, deadline)
        with target.open('ab') as writer:
            writer.write(b'changed')

    monkeypatch.setattr(artifacts, '_artifact_chunks', changed)
    with pytest.raises(QueueOperationError, match='artifact_changed_during_read'):
        artifacts._digest_stable_owner_file(target)


def test_result_write_refuses_existing_path(tmp_path):
    target = tmp_path / 'result'
    artifacts._write_result_file(target, b'original')
    with pytest.raises(FileExistsError):
        artifacts._write_result_file(target, b'changed')
    assert target.read_bytes() == b'original'


def test_hardening_failure_closes_result_descriptor(tmp_path, monkeypatch):
    descriptors = []
    real_open = os.open

    def opened(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def fail(path, mode):
        raise PermissionError('ACL denied')

    monkeypatch.setattr(artifacts.os, 'open', opened)
    monkeypatch.setattr(artifacts, '_harden_owner_only', fail)
    with pytest.raises(PermissionError, match='ACL denied'):
        artifacts._write_result_file(tmp_path / 'result', b'data')
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_sequential_output_tells_size_digest_and_flushes():
    handle = io.BytesIO()
    output = artifacts._ArtifactOutput(handle, None)
    assert output.write(b'data') == 4
    output.flush()
    assert output.tell() == 4
    assert output.digest.hexdigest() == hashlib.sha256(b'data').hexdigest()
    assert handle.getvalue() == b'data'


def test_short_writer_is_refused():
    class ShortWriter:
        def write(self, data):
            return len(data) - 1

    output = artifacts._ArtifactOutput(ShortWriter(), None)
    with pytest.raises(QueueOperationError, match='artifact_short_write'):
        output.write(b'data')
    assert output.tell() == 0


def test_nonbytes_chunk_is_refused_and_temp_is_removed(tmp_path):
    with pytest.raises(TypeError, match='must be bytes'):
        artifacts._write_durable_stream(tmp_path / 'artifact', ('text',))
    assert list(tmp_path.iterdir()) == []


def test_cleanup_removes_only_matching_private_staging(tmp_path):
    staging = tmp_path / '.export.staging-owned'
    staging.mkdir(mode=0o700)
    artifacts._harden_owner_only(staging, 0o700)
    (staging / 'partial').write_bytes(b'data')
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    artifacts._cleanup_export_staging(tmp_path, 'export')
    assert not staging.exists()
    assert unrelated.is_dir()


def test_cleanup_refuses_a_file_instead_of_staging_directory(tmp_path):
    staging = tmp_path / '.export.staging-file'
    staging.write_bytes(b'evidence')
    with pytest.raises(QueueOperationError, match='export_staging_invalid'):
        artifacts._cleanup_export_staging(tmp_path, 'export')
    assert staging.read_bytes() == b'evidence'
