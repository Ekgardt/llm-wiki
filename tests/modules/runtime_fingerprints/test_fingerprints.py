"""Fingerprints bind bytes and identity without imposing a whole-file memory bound."""
import hashlib

import pytest
from llm_wiki import runtime_fingerprints as fingerprints
from llm_wiki.runtime_environment import _harden_runtime_owner_only
from llm_wiki.runtime_records import capture_runtime_file_identity


def test_offline_fingerprint_matches_private_file_bytes_and_identity(tmp_path):
    path = tmp_path / 'record'
    payload = b'complete bytes'
    path.write_bytes(payload)
    _harden_runtime_owner_only(path, 0o600)
    digest, identity = fingerprints.fingerprint_runtime_file(path, tmp_path, max_bytes=len(payload))
    assert digest == hashlib.sha256(payload).hexdigest()
    assert identity == capture_runtime_file_identity(path, state_root=tmp_path)
    with pytest.raises(PermissionError, match='bounded'):
        fingerprints.fingerprint_runtime_file(path, tmp_path, max_bytes=len(payload)-1)


def test_expired_deadline_refuses_before_opening_an_artifact(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('opened after the caller deadline expired')

    monkeypatch.setattr(fingerprints.os, 'open', forbidden)
    with pytest.raises(TimeoutError):
        fingerprints.fingerprint_regular_file(tmp_path / 'absent', tmp_path, deadline=0.0)
