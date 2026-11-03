"""Windows name-derived execute bits are not descriptor permission changes."""
import hashlib
from types import SimpleNamespace

import pytest
from llm_wiki import runtime_fingerprints as fingerprints


def metadata(mode=0o100666, **changes):
    fields = dict(st_dev=1, st_ino=2, st_mode=mode, st_size=8,
                  st_mtime_ns=100, st_ctime_ns=200, st_birthtime_ns=200)
    fields.update(changes)
    return SimpleNamespace(**fields)


def test_windows_path_execute_bits_do_not_report_false_file_mutation(monkeypatch):
    before, opened = metadata(0o100777), metadata()
    with monkeypatch.context() as patch:
        patch.setattr(fingerprints.os, 'name', 'nt')
        fingerprints._require_opened_fingerprint_metadata(before, opened)


def test_posix_execute_permission_change_remains_a_mutation(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(fingerprints.os, 'name', 'posix')
        with pytest.raises(PermissionError, match='changed'):
            fingerprints._require_opened_fingerprint_metadata(metadata(0o100777), metadata())


@pytest.mark.parametrize('changed', [
    {'st_dev': 2}, {'st_ino': 3}, {'st_size': 9}, {'st_mtime_ns': 101},
    {'st_birthtime_ns': 201}, {'st_mode': 0o100444}, {'st_mode': 0o040666},
])
def test_windows_actual_metadata_changes_remain_refused(monkeypatch, changed):
    before, opened = metadata(0o100777), metadata(**changed)
    with monkeypatch.context() as patch:
        patch.setattr(fingerprints.os, 'name', 'nt')
        with pytest.raises(PermissionError, match='changed'):
            fingerprints._require_opened_fingerprint_metadata(before, opened)


def test_windows_same_api_mode_changes_remain_exact(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(fingerprints.os, 'name', 'nt')
        with pytest.raises(PermissionError, match='changed'):
            fingerprints._require_fingerprint_metadata(metadata(0o100777), metadata())


@pytest.mark.parametrize('suffix', ['.exe', '.bat', '.cmd', '.com', '.py', ''])
def test_real_executable_names_have_stable_fingerprints(tmp_path, suffix):
    path = tmp_path / ('dependency' + suffix)
    content = b'complete dependency bytes'
    path.write_bytes(content)
    digest, identity = fingerprints.fingerprint_regular_file(path, tmp_path)
    assert digest == hashlib.sha256(content).hexdigest()
    assert identity.size == len(content)
