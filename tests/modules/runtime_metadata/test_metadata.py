"""Existing metadata policy, exercised before extraction from runtime_records."""
import pytest
from llm_wiki import runtime_metadata as metadata
from llm_wiki.runtime_environment import harden_runtime_owner_only


@pytest.mark.parametrize('budget', [None, 6, 7])
def test_metadata_accepts_existing_size_contract(tmp_path, budget):
    path = tmp_path / 'record'
    path.write_bytes(b'abcdef')
    harden_runtime_owner_only(path, 0o600)
    observed = metadata.validate_operational_db_file(path, tmp_path, max_bytes=budget, owner_only=True)
    assert observed.st_size == 6
    assert observed.st_ino == path.lstat().st_ino


def test_oversized_file_is_refused(tmp_path):
    path = tmp_path / 'record'
    path.write_bytes(b'abcdef')
    with pytest.raises(PermissionError, match='bounded regular file'):
        metadata.validate_operational_db_file(path, tmp_path, max_bytes=5)


def test_negative_budget_is_refused_before_path_lookup(tmp_path):
    path = tmp_path / 'absent'
    with pytest.raises(ValueError, match='max_bytes must be non-negative'):
        metadata.validate_operational_db_file(path, tmp_path, max_bytes=-1)
    assert not path.exists()


def test_external_parent_is_refused(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    outside = tmp_path / 'record'
    outside.write_bytes(b'abcdef')
    with pytest.raises(PermissionError, match='outside the configured state root'):
        metadata.validate_operational_db_file(outside, root, max_bytes=None)


def test_directory_is_not_a_database_file(tmp_path):
    path = tmp_path / 'directory'
    path.mkdir()
    with pytest.raises(PermissionError, match='bounded regular file'):
        metadata.validate_operational_db_file(path, tmp_path, max_bytes=None)


def test_symbolic_link_is_not_followed(tmp_path):
    target = tmp_path / 'target'
    target.write_bytes(b'abcdef')
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(PermissionError, match='bounded regular file'):
        metadata.validate_operational_db_file(link, tmp_path, max_bytes=None)
