"""Containment still follows current physical authority after allocation changes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import generation_maintenance


def _directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'directory symlinks unavailable: {error}')


@pytest.mark.parametrize('relative,expected', [('vault/child', True), ('vault', True), ('vault-other/child', False), ('elsewhere', False)])
def test_containment_compares_directory_components(tmp_path, relative, expected):
    root = tmp_path / 'vault'
    root.mkdir()
    path = tmp_path / relative
    assert generation_maintenance.within(path, root) is expected


def test_a_retargeted_parent_cannot_keep_its_previous_verdict(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    inside = root / 'inside'
    outside = tmp_path / 'outside'
    inside.mkdir()
    outside.mkdir()
    link = root / 'link'
    _directory_link(link, inside)
    assert generation_maintenance.within(link / 'missing', root)
    link.unlink()
    _directory_link(link, outside)
    assert not generation_maintenance.within(link / 'missing', root)


def test_a_retargeted_root_is_resolved_on_each_call(tmp_path):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    root = tmp_path / 'root'
    _directory_link(root, first)
    assert generation_maintenance.within(first / 'missing', root)
    root.unlink()
    _directory_link(root, second)
    assert not generation_maintenance.within(first / 'missing', root)
    assert generation_maintenance.within(second / 'missing', root)
