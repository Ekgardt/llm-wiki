"""Windows path ctime and descriptor ctime can name different timestamps."""
import hashlib
import os
from types import SimpleNamespace

import bounded_io
import pytest
from llm_wiki import runtime_fingerprints


def _observations(path, monkeypatch, *, platform='nt'):
    before = path.stat()
    fields = {name: getattr(before, name) for name in dir(before) if name.startswith('st_')}
    fields.update(st_birthtime_ns=getattr(before, "st_birthtime_ns", before.st_ctime_ns),
                  st_ctime_ns=before.st_ctime_ns + 100)
    opened = SimpleNamespace(**fields)
    proxy = SimpleNamespace(**vars(os))
    proxy.name = platform
    proxy.fstat = lambda descriptor: SimpleNamespace(**vars(opened))
    monkeypatch.setattr(bounded_io, 'os', proxy)
    monkeypatch.setattr(runtime_fingerprints, 'os', proxy)
    return opened


def _source(tmp_path):
    path = tmp_path / 'source'
    path.write_bytes(b'unchanged')
    return path


def test_range_accepts_distinct_windows_path_and_descriptor_ctime(tmp_path, monkeypatch):
    path = _source(tmp_path)
    _observations(path, monkeypatch)
    with bounded_io.open_stable_range(path, 1024) as source:
        assert source[:] == b'unchanged'


def test_fingerprint_accepts_distinct_windows_path_and_descriptor_ctime(tmp_path, monkeypatch):
    path = _source(tmp_path)
    _observations(path, monkeypatch)
    digest, identity = runtime_fingerprints.fingerprint_regular_file(path, tmp_path)
    assert digest == hashlib.sha256(b'unchanged').hexdigest()
    assert identity.size == len(b'unchanged')


def test_range_still_rejects_changed_descriptor_ctime(tmp_path, monkeypatch):
    path = _source(tmp_path)
    opened = _observations(path, monkeypatch)
    with pytest.raises(bounded_io.SourceChangedDuringRead):
        with bounded_io.open_stable_range(path, 1024) as source:
            opened.st_ctime_ns += 1
            source[:1]


@pytest.mark.parametrize('reader', ['range', 'fingerprint'])
def test_unix_cross_api_ctime_difference_is_still_refused(tmp_path, monkeypatch, reader):
    path = _source(tmp_path)
    _observations(path, monkeypatch, platform='posix')
    with pytest.raises(PermissionError, match='changed'):
        _read(reader, path, tmp_path)


def _read(reader, path, root):
    if reader == 'fingerprint':
        return runtime_fingerprints.fingerprint_regular_file(path, root)
    with bounded_io.open_stable_range(path, 1024) as source:
        return source[:]


@pytest.mark.parametrize('reader', ['range', 'fingerprint'])
def test_changed_creation_identity_is_refused(tmp_path, monkeypatch, reader):
    path = _source(tmp_path)
    opened = _observations(path, monkeypatch)
    opened.st_birthtime_ns += 1
    with pytest.raises(PermissionError, match='changed'):
        _read(reader, path, tmp_path)


def test_fingerprint_still_rejects_changed_descriptor_ctime(tmp_path, monkeypatch):
    path = _source(tmp_path)
    opened = _observations(path, monkeypatch)
    original_read = os.read

    def changed_after_read(descriptor, size):
        content = original_read(descriptor, size)
        opened.st_ctime_ns += 1
        return content

    monkeypatch.setattr(runtime_fingerprints.os, 'read', changed_after_read)
    with pytest.raises(PermissionError, match='changed'):
        runtime_fingerprints.fingerprint_regular_file(path, tmp_path)
