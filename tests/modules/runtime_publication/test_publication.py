"""Real publication preserves duplicate recognition, evidence and refusal semantics."""
import hashlib

import pytest
from llm_wiki import runtime_publication as publication
from llm_wiki.runtime_records import capture_runtime_file_identity


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def test_descriptor_writer_finishes_short_writes_and_refuses_no_progress(monkeypatch):
    written = []

    def two_bytes_at_a_time(_descriptor, view):
        written.append(bytes(view[:2]))
        return len(written[-1])

    with monkeypatch.context() as patched:
        patched.setattr(publication.os, 'write', two_bytes_at_a_time)
        publication.write_all(3, b'abcde')
    assert written == [b'ab', b'cd', b'e']
    with monkeypatch.context() as patched, pytest.raises(OSError, match='no progress'):
        patched.setattr(publication.os, 'write', lambda _descriptor, _view: 0)
        publication.write_all(3, b'x')


def test_create_duplicate_and_replacement_keep_identity_and_bytes(tmp_path):
    path = tmp_path / 'record'
    first = publication.publish_runtime_file(path, b'first', state_root=tmp_path, create_only=True)
    duplicate = publication.publish_runtime_file(path, b'first', state_root=tmp_path, create_only=True)
    assert duplicate == first
    assert list(tmp_path.iterdir()) == [path]
    replaced = publication.publish_runtime_file(path, b'second', state_root=tmp_path,
        create_only=False, expected=first, expected_sha256=_hash(b'first'))
    assert replaced == capture_runtime_file_identity(path, state_root=tmp_path)
    assert path.read_bytes() == b'second'
    with pytest.raises(PermissionError):
        publication.publish_runtime_file(path, b'third', state_root=tmp_path,
            create_only=False, expected=first, expected_sha256=_hash(b'first'))
    assert path.read_bytes() == b'second'


def test_wrong_digest_cannot_replace_even_when_identity_is_current(tmp_path):
    path = tmp_path / 'record'
    current = publication.publish_runtime_file(path, b'first', state_root=tmp_path, create_only=True)
    with pytest.raises(PermissionError):
        publication.publish_runtime_file(path, b'changed', state_root=tmp_path,
            create_only=False, expected=current, expected_sha256=_hash(b'wrong'))
    assert path.read_bytes() == b'first'


def test_staged_data_is_not_published_when_permission_hardening_fails(tmp_path, monkeypatch):
    path = tmp_path / 'record'
    seen = []

    def denied(staged, mode):
        seen.append(staged)
        raise PermissionError('private staging refused')

    monkeypatch.setattr(publication, 'harden_runtime_owner_only', denied)
    with pytest.raises(PermissionError, match='private staging refused'):
        publication.publish_runtime_file(path, b'first', state_root=tmp_path, create_only=True)
    assert len(seen) == 1
    assert seen[0].parent == tmp_path
    assert seen[0] != path
    assert not path.exists()
