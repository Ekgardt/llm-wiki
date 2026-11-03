"""Ancestry metadata is a policy predicate, not a path-opening sandbox."""
import os
import stat
from types import SimpleNamespace

import pytest
from llm_wiki import path_ancestry


def test_real_directory_file_and_missing_path(tmp_path):
    assert path_ancestry._acceptable_ancestor(tmp_path)
    file = tmp_path / 'file'
    file.touch()
    assert not path_ancestry._acceptable_ancestor(file)
    with pytest.raises(FileNotFoundError):
        path_ancestry._acceptable_ancestor(tmp_path / 'missing')


@pytest.mark.parametrize(('uid', 'mode', 'expected'), [
    (0, 0o755, True), (0, 0o775, False), (0, 0o757, False), (1000, 0o755, False),
])
def test_root_control_requires_owner_and_no_shared_write(uid, mode, expected):
    info = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)
    assert path_ancestry._root_controlled(info) is expected


def test_reparse_flag_refuses_before_following():
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_file_attributes=0x400))
    assert not path_ancestry._acceptable_ancestor(path)


def test_metadata_error_refuses_system_link(monkeypatch):
    monkeypatch.setattr(path_ancestry, 'os', SimpleNamespace(name='posix'))

    def missing():
        raise FileNotFoundError('missing')

    assert not path_ancestry._system_symlink(SimpleNamespace(lstat=missing))


def test_windows_never_accepts_system_link(monkeypatch):
    monkeypatch.setattr(path_ancestry, 'os', SimpleNamespace(name='nt'))
    assert not path_ancestry._system_symlink(object())


@pytest.mark.parametrize(('link_uid', 'parent_uid', 'mode', 'expected'), [
    (0, 0, 0o755, True), (1, 0, 0o755, False),
    (0, 1, 0o755, False), (0, 0, 0o777, False),
])
def test_system_link_ownership(link_uid, parent_uid, mode, expected, monkeypatch):
    monkeypatch.setattr(path_ancestry, 'os', SimpleNamespace(name='posix'))
    parent = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=parent_uid, st_mode=mode))
    link = SimpleNamespace(lstat=lambda: SimpleNamespace(st_uid=link_uid), parent=parent)
    assert path_ancestry._system_symlink(link) is expected


@pytest.mark.skipif(os.name == 'nt', reason='POSIX system symlink policy')
def test_allowed_link_still_must_point_to_directory(tmp_path, monkeypatch):
    target = tmp_path / 'target'
    target.touch()
    link = tmp_path / 'link'
    link.symlink_to(target)
    monkeypatch.setattr(path_ancestry, '_system_symlink', lambda path: True)
    assert not path_ancestry._acceptable_ancestor(link)
    target.unlink()
    target.mkdir()
    assert path_ancestry._acceptable_ancestor(link)
