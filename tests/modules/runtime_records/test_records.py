"""Runtime reads retain containment, bounds, identity and SQLite lock safety."""

import pytest
from llm_wiki import runtime_records as records
from llm_wiki.runtime_environment import _harden_runtime_owner_only


def test_read_and_identity_agree_for_a_private_regular_file(tmp_path):
    path = tmp_path / 'record'
    path.write_bytes(b'complete bytes')
    _harden_runtime_owner_only(path, 0o600)
    assert records.read_runtime_bytes(path, tmp_path, max_bytes=14) == b'complete bytes'
    identity = records.capture_runtime_file_identity(path, state_root=tmp_path)
    assert identity.size == 14
    with pytest.raises(PermissionError, match='bounded'):
        records.read_runtime_bytes(path, tmp_path, max_bytes=13)


def test_database_validation_never_opens_or_closes_a_file_descriptor(tmp_path, monkeypatch):
    path = tmp_path / 'database'
    path.write_bytes(b'header')
    path.chmod(0o600)

    def forbidden(*args, **kwargs):
        pytest.fail('metadata-only validation opened or closed a descriptor')

    monkeypatch.setattr(records.os, 'open', forbidden)
    monkeypatch.setattr(records.os, 'close', forbidden)
    assert records.validate_operational_db_file(path, tmp_path, max_bytes=None).st_size == 6


def test_reader_refuses_an_external_parent(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    path = tmp_path / 'outside'
    path.write_bytes(b'external')
    with pytest.raises(PermissionError, match='outside'):
        records.read_runtime_bytes(path, root, max_bytes=100)
