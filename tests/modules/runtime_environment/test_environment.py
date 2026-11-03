"""Locality and permission refusals precede resource acquisition."""
import errno
import os
from pathlib import Path

import pytest
from llm_wiki import runtime_environment as environment
from llm_wiki.runtime_contracts import UnsafeStateRoot


def test_reparse_refusal_precedes_network_inspection(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, '_windows_reparse_point', lambda path: True)

    def forbidden(path):
        pytest.fail('network inspection preceded reparse refusal')

    monkeypatch.setattr(environment, '_known_network_path', forbidden)
    with pytest.raises(UnsafeStateRoot, match='reparse'):
        environment._require_local_non_reparse_root(tmp_path)


def test_mount_parsing_preserves_escaped_spaces_and_component_boundaries():
    rows = environment._parse_posix_mounts(
        'server:/share /mnt/team\\040share nfs4 rw 0 0\nmalformed\n', is_mountinfo=False)
    assert rows == [('/mnt/team share', 'nfs4')]
    assert environment._path_is_under('/mnt/team share/file', rows[0][0])
    assert not environment._path_is_under('/mnt/team shared/file', rows[0][0])
    assert environment._is_network_filesystem(rows[0][1])


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permission bits')
def test_unsupported_permissions_warn_without_claiming_applied_mode(tmp_path, monkeypatch):
    path = tmp_path / 'record'
    path.write_bytes(b'content')
    path.chmod(0o644)

    def unsupported(self, mode):
        raise OSError(errno.ENOTSUP, 'unsupported')

    monkeypatch.setattr(Path, 'chmod', unsupported)
    with pytest.warns(RuntimeWarning, match='unsupported'):
        assert environment._set_owner_only(path, 0o600) is False
    with pytest.raises(PermissionError, match='owner-only'):
        environment._require_owner_only_file(path, path.stat())
