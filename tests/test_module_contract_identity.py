"""Stale helper, source, semantic and test bytes cannot reuse evidence identity."""
import copy

import pytest

from tests.module_contract_identity import artifact_identity, require_artifact_identity


@pytest.fixture
def artifacts(tmp_path):
    names = {'provider.py', 'consumer.py', 'interface.pyi', 'semantics.md',
             'test_integration.py', 'checker.py', 'uv.lock', 'runtime.json'}
    for name in names:
        (tmp_path / name).write_bytes(b'original\n')
    return tmp_path, names


def test_unchanged_complete_bytes_have_stable_identity(artifacts):
    root, names = artifacts
    evidence = artifact_identity(root, names)
    assert set(evidence['files']) == names
    require_artifact_identity(root, names, copy.deepcopy(evidence))


@pytest.mark.parametrize('name', ['provider.py', 'consumer.py', 'interface.pyi',
                                'semantics.md', 'test_integration.py', 'checker.py',
                                'uv.lock', 'runtime.json'])
def test_each_relevant_artifact_change_invalidates_evidence(artifacts, name):
    root, names = artifacts
    evidence = artifact_identity(root, names)
    (root / name).write_bytes(b'changed!\n')
    with pytest.raises(ValueError, match='requalification'):
        require_artifact_identity(root, names, evidence)


@pytest.mark.parametrize('name', ['../outside', '/absolute', './file', 'a/../file',
                                'a//file', 'a\\file', '', '.'])
def test_noncanonical_paths_are_refused(tmp_path, name):
    with pytest.raises(ValueError):
        artifact_identity(tmp_path, {name})


def test_escape_through_symlink_is_refused(tmp_path):
    root = tmp_path / 'checkout'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'private')
    try:
        (root / 'link').symlink_to(outside)
    except OSError as exc:
        pytest.skip(f'Host does not allow symlink creation: {exc}')
    with pytest.raises(ValueError, match='escapes'):
        artifact_identity(root, {'link'})


def test_missing_artifact_cannot_reuse_evidence(artifacts):
    root, names = artifacts
    evidence = artifact_identity(root, names)
    (root / 'semantics.md').unlink()
    with pytest.raises(FileNotFoundError):
        require_artifact_identity(root, names, evidence)


@pytest.mark.parametrize('change', ['extra', 'missing', 'schema', 'digest', 'size'])
def test_malformed_or_different_evidence_is_refused(artifacts, change):
    root, names = artifacts
    evidence = artifact_identity(root, names)
    mutations = {
        'extra': lambda: evidence.update(passed=True),
        'missing': lambda: evidence['files'].pop('semantics.md'),
        'schema': lambda: evidence.update(schema='unknown'),
        'digest': lambda: evidence['files']['checker.py'].update(sha256='0' * 64),
        'size': lambda: evidence['files']['checker.py'].update(bytes=0),
    }
    mutations[change]()
    with pytest.raises(ValueError, match='requalification'):
        require_artifact_identity(root, names, evidence)


def test_new_required_file_cannot_be_hidden_by_old_membership(artifacts):
    root, names = artifacts
    evidence = artifact_identity(root, names)
    (root / 'new_test.py').write_bytes(b'new assertion')
    with pytest.raises(ValueError, match='requalification'):
        require_artifact_identity(root, names | {'new_test.py'}, evidence)


def test_boolean_cannot_impersonate_a_one_byte_count(tmp_path):
    (tmp_path / 'source.py').write_bytes(b'x')
    evidence = artifact_identity(tmp_path, {'source.py'})
    evidence['files']['source.py']['bytes'] = True
    with pytest.raises(ValueError, match='requalification'):
        require_artifact_identity(tmp_path, {'source.py'}, evidence)


def test_empty_identity_and_directories_are_refused(tmp_path):
    with pytest.raises(ValueError, match='empty'):
        artifact_identity(tmp_path, set())
    (tmp_path / 'folder').mkdir()
    with pytest.raises(ValueError, match='not a file'):
        artifact_identity(tmp_path, {'folder'})
