"""Actual installation bytes, including unchanged RECORD claims, bind execution."""
import importlib.metadata
import json
from pathlib import Path

import pytest

from tests.module_contract_environment import _distribution, _tree, environment_identity


def _installed(root):
    metadata = root / 'example-1.0.dist-info'
    metadata.mkdir()
    (metadata / 'METADATA').write_text('Name: example\nVersion: 1.0\n', encoding='utf-8')
    (metadata / 'RECORD').write_text('example.py,,\nexample-1.0.dist-info/METADATA,,\n',
                                    encoding='utf-8')
    (root / 'example.py').write_text('VALUE = 1\n', encoding='utf-8')
    return importlib.metadata.Distribution.at(metadata)


def test_dependency_bytes_change_identity_without_version_or_record_change(tmp_path):
    dist = _installed(tmp_path)
    before = _distribution(dist)
    (tmp_path / 'example.py').write_text('VALUE = 2\n', encoding='utf-8')
    after = _distribution(dist)
    assert before['name'] == after['name'] == 'example'
    assert before['version'] == after['version'] == '1.0'
    assert before['files']['example.py'] != after['files']['example.py']


def test_missing_installed_dependency_file_refuses(tmp_path):
    dist = _installed(tmp_path)
    (tmp_path / 'example.py').unlink()
    with pytest.raises(FileNotFoundError):
        _distribution(dist)


def test_missing_distribution_inventory_refuses(tmp_path):
    dist = _installed(tmp_path)
    (tmp_path / 'example-1.0.dist-info/RECORD').unlink()
    with pytest.raises(ValueError, match='inventory'):
        _distribution(dist)


def test_editable_external_sources_are_not_assumed_identified(tmp_path):
    dist = _installed(tmp_path)
    (tmp_path / 'example-1.0.dist-info/direct_url.json').write_text(
        json.dumps({'dir_info': {'editable': True}}), encoding='utf-8')
    with pytest.raises(ValueError, match='Editable'):
        _distribution(dist)


def test_runtime_and_non_distribution_startup_bytes_are_bound(tmp_path):
    (tmp_path / 'python').write_bytes(b'interpreter')
    startup = tmp_path / 'startup.pth'
    startup.write_bytes(b'import startup')
    before = _tree(tmp_path)
    startup.write_bytes(b'import changed')
    assert before != _tree(tmp_path)
    assert set(before['files']) == {'python', 'startup.pth'}


def test_only_derived_bytecode_is_ignored(tmp_path):
    (tmp_path / 'source.py').write_bytes(b'source')
    before = _tree(tmp_path)
    (tmp_path / 'source.pyc').write_bytes(b'derived')
    (tmp_path / '__pycache__').mkdir()
    (tmp_path / '__pycache__/anything').write_bytes(b'derived')
    assert before == _tree(tmp_path)
    (tmp_path / 'native.so').write_bytes(b'extension')
    assert before != _tree(tmp_path)


def test_actual_environment_has_interpreter_and_installed_pytest_bytes():
    identity = environment_identity()
    assert identity['executable']['bytes'] > 0
    pytest_dist = next(d for d in identity['distributions'] if d['name'] == 'pytest')
    assert any(Path(name).name == '__init__.py' for name in pytest_dist['files'])
    assert identity['installations']


def test_directory_alias_and_cycle_retain_target_bytes_once(tmp_path):
    target = tmp_path / 'lib'
    target.mkdir()
    (target / 'module.py').write_bytes(b'original')
    try:
        (tmp_path / 'lib64').symlink_to(target, target_is_directory=True)
        (target / 'parent').symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'Host does not permit directory symlinks: {exc}')
    before = _tree(tmp_path)
    assert list(before['files']) == ['lib/module.py']
    assert before['directories'][str(tmp_path / 'lib64')] == str(target)
    (target / 'module.py').write_bytes(b'changed')
    assert before != _tree(tmp_path)


@pytest.mark.parametrize('record', ['example.py\n', ',,\n', 'example.py,,,\n'])
def test_malformed_record_does_not_hide_files(tmp_path, record):
    dist = _installed(tmp_path)
    (tmp_path / 'example-1.0.dist-info/RECORD').write_text(record, encoding='utf-8')
    with pytest.raises(ValueError, match='Malformed'):
        _distribution(dist)
