"""A migration receipt must bind bytes and identity from the same stable file."""
from __future__ import annotations

import hashlib
import os
import time

import installed_memory_repair as repair
import pytest
import reliable_memory as reliable
from bounded_io import IO_CHUNK_BYTES


def _artifact(tmp_path, content=b'original'):
    path = tmp_path / 'queue.sqlite3'
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_receipt_rejects_path_replaced_after_bytes_read(tmp_path, monkeypatch):
    path = _artifact(tmp_path)
    replacement = tmp_path / 'new'
    replacement.write_bytes(b'replaced')
    replacement.chmod(0o600)
    original_read = os.read

    def replace_after_read(descriptor, count):
        data = original_read(descriptor, count)
        if replacement.exists():
            os.replace(replacement, path)
        return data

    monkeypatch.setattr(os, 'read', replace_after_read)
    with pytest.raises(PermissionError, match='changed'):
        repair._artifact_record(path, tmp_path, 1024)


def test_fingerprint_streams_beyond_the_old_database_ceiling(tmp_path, monkeypatch):
    path = _artifact(tmp_path, b'')
    size = repair._MAX_OPERATIONAL_DB_BYTES + 1
    with path.open('r+b') as stream:
        stream.truncate(size)
    original_read = os.read
    counts = []

    def measured_read(descriptor, count):
        counts.append(count)
        return original_read(descriptor, count)

    monkeypatch.setattr(os, 'read', measured_read)
    record = repair._artifact_record(path, tmp_path, None)
    expected = hashlib.sha256()
    for _ in range(size // IO_CHUNK_BYTES):
        expected.update(bytes(IO_CHUNK_BYTES))
    expected.update(bytes(size % IO_CHUNK_BYTES))
    assert record['sha256'] == expected.hexdigest()
    assert record['identity']['size'] == size
    assert max(counts) <= IO_CHUNK_BYTES


def test_fingerprint_honors_expired_deadline_before_read(tmp_path, monkeypatch):
    path = _artifact(tmp_path)

    def forbidden_read(*_args):
        pytest.fail('expired operation read file bytes')

    monkeypatch.setattr(os, 'read', forbidden_read)
    with pytest.raises(TimeoutError):
        reliable.fingerprint_runtime_file(path, tmp_path, deadline=time.monotonic() - 1)


@pytest.mark.parametrize('content', [b'', b'abc', b'a' * (IO_CHUNK_BYTES + 9)])
def test_short_reads_still_hash_the_complete_file(tmp_path, monkeypatch, content):
    path = _artifact(tmp_path, content)
    original_read = os.read
    monkeypatch.setattr(os, 'read', lambda fd, count: original_read(fd, min(count, 7)))
    digest, identity = reliable.fingerprint_runtime_file(path, tmp_path)
    assert digest == hashlib.sha256(content).hexdigest()
    assert identity == reliable.capture_runtime_file_identity(path, state_root=tmp_path)


def _rewrite_preserving_mtime(path, content):
    metadata = path.stat()
    path.write_bytes(content)
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))


@pytest.mark.parametrize('content', [b'x', b'replaced', b'longer-than-original'])
def test_changed_file_is_refused_even_with_preserved_mtime(tmp_path, monkeypatch, content):
    path = _artifact(tmp_path)
    original_read = os.read
    pending = [content]

    def mutate_during_read(descriptor, count):
        data = original_read(descriptor, count)
        if pending:
            _rewrite_preserving_mtime(path, pending.pop())
        return data

    monkeypatch.setattr(os, 'read', mutate_during_read)
    with pytest.raises(PermissionError, match='changed'):
        reliable.fingerprint_runtime_file(path, tmp_path)


def test_mid_read_deadline_closes_descriptor(tmp_path, monkeypatch):
    path = _artifact(tmp_path, b'a' * (IO_CHUNK_BYTES + 1))
    original_read = os.read
    captured = []
    clock = [0.0]

    def expire_after_read(descriptor, count):
        captured.append(descriptor)
        data = original_read(descriptor, count)
        clock[0] = 2.0
        return data

    monkeypatch.setattr(os, 'read', expire_after_read)
    monkeypatch.setattr(reliable.time, 'monotonic', lambda: clock[0])
    with pytest.raises(TimeoutError):
        reliable.fingerprint_runtime_file(path, tmp_path, deadline=1.0)
    with pytest.raises(OSError):
        os.fstat(captured[0])


def test_symlink_and_outside_root_are_refused(tmp_path):
    path = _artifact(tmp_path)
    link = tmp_path / 'link'
    link.symlink_to(path)
    other_root = tmp_path / 'other-root'
    other_root.mkdir()
    with pytest.raises(PermissionError):
        reliable.fingerprint_runtime_file(link, tmp_path)
    with pytest.raises(PermissionError):
        reliable.fingerprint_runtime_file(path, other_root)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permissions; Windows uses ACLs')
def test_owner_only_permissions_are_required(tmp_path):
    path = _artifact(tmp_path)
    path.chmod(0o644)
    with pytest.raises(PermissionError, match='owner-only'):
        reliable.fingerprint_runtime_file(path, tmp_path)


def test_explicit_small_record_budget_is_preserved(tmp_path):
    path = _artifact(tmp_path)
    with pytest.raises(PermissionError):
        repair._artifact_record(path, tmp_path, 1)


@pytest.mark.parametrize('mode', [0o444, 0o644, 0o600])
def test_regular_dependency_fingerprint_accepts_readable_code(tmp_path, mode):
    path = _artifact(tmp_path, b'implementation')
    path.chmod(mode)
    digest, identity = reliable.fingerprint_regular_file(path, tmp_path)
    assert digest == hashlib.sha256(b'implementation').hexdigest()
    assert identity.size == len(b'implementation')


def test_dependency_rewrite_is_not_hidden_by_same_size_and_mtime(tmp_path, monkeypatch):
    path = _artifact(tmp_path)
    original_read = os.read
    pending = [b'modified']

    def rewrite_after_read(descriptor, count):
        data = original_read(descriptor, count)
        if pending:
            _rewrite_preserving_mtime(path, pending.pop())
        return data

    monkeypatch.setattr(os, 'read', rewrite_after_read)
    with pytest.raises(PermissionError, match='changed'):
        reliable.fingerprint_regular_file(path, tmp_path)


def test_dependency_fingerprint_refuses_symlink_and_escape(tmp_path):
    path = _artifact(tmp_path)
    link = tmp_path / 'link'
    link.symlink_to(path)
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(PermissionError):
        reliable.fingerprint_regular_file(link, tmp_path)
    with pytest.raises(PermissionError):
        reliable.fingerprint_regular_file(path, other)


def test_dependency_fingerprint_preserves_caller_deadline(tmp_path):
    path = _artifact(tmp_path)
    with pytest.raises(TimeoutError):
        reliable.fingerprint_regular_file(path, tmp_path, deadline=time.monotonic() - 1)
