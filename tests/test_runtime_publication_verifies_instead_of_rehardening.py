"""The publication boundary must verify preserved permissions instead of repairing drift."""
import os
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import reliable_memory

from tests.test_a_windows_owner_with_a_non_ascii_name_is_verified import OWNER, _icacls_writing


def _destination(tmp_path):
    destination = tmp_path / 'run/accepted.json'
    destination.parent.mkdir()
    return destination


def test_same_parent_publication_hardens_only_its_private_staged_file(tmp_path, monkeypatch):
    destination = _destination(tmp_path)
    harden = Mock(wraps=reliable_memory._harden_runtime_owner_only)
    monkeypatch.setattr(reliable_memory, '_harden_runtime_owner_only', harden)

    published = reliable_memory.publish_runtime_file(destination, b'accepted',
        state_root=tmp_path, create_only=True)

    assert harden.call_count == 1
    assert harden.call_args.args[0].parent == destination.parent
    assert harden.call_args.args[0] != destination
    assert destination.read_bytes() == b'accepted'
    assert reliable_memory.capture_runtime_file_identity(destination, state_root=tmp_path) == published


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission-change injection')
def test_permission_drift_after_publication_is_refused_without_remasking_it(tmp_path, monkeypatch):
    destination = _destination(tmp_path)
    publish = reliable_memory._publish_staged

    def broaden(*args):
        publish(*args)
        destination.chmod(0o644)

    monkeypatch.setattr(reliable_memory, '_publish_staged', broaden)

    with pytest.raises(PermissionError, match='mode'):
        reliable_memory.publish_runtime_file(destination, b'accepted', state_root=tmp_path, create_only=True)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert destination.read_bytes() == b'accepted'


def test_initial_permission_failure_cannot_publish_an_artifact(tmp_path, monkeypatch):
    destination = _destination(tmp_path)
    harden = Mock(side_effect=PermissionError('initial private permissions denied'))
    monkeypatch.setattr(reliable_memory, '_harden_runtime_owner_only', harden)

    with pytest.raises(PermissionError, match='initial private permissions denied'):
        reliable_memory.publish_runtime_file(destination, b'accepted', state_root=tmp_path, create_only=True)
    assert not destination.exists()


def test_windows_postpublication_check_reads_the_owner_acl_and_propagates_a_refusal(tmp_path, monkeypatch):
    import markdown_transaction

    destination = _destination(tmp_path)
    verify = Mock(side_effect=PermissionError('owner-only ACL changed'))
    monkeypatch.setattr(reliable_memory, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(markdown_transaction, '_verify_windows_owner_acl', verify)

    with pytest.raises(PermissionError, match='owner-only ACL changed'):
        reliable_memory._verify_published_runtime_permissions(destination, 0o600)
    verify.assert_called_once_with(destination)


@pytest.mark.parametrize('mode', [0o400, 0o600, 0o640])
@pytest.mark.skipif(os.name == 'nt', reason='POSIX requested-mode contract')
def test_explicit_requested_modes_survive_publication(tmp_path, mode):
    destination = _destination(tmp_path)

    reliable_memory.publish_runtime_file(destination, b'accepted', state_root=tmp_path, create_only=True, mode=mode)

    assert stat.S_IMODE(destination.stat().st_mode) == mode


@pytest.mark.skipif(os.name != 'nt', reason='actual Windows ACL verification')
def test_windows_published_artifact_is_owner_only_without_a_second_permission_write(tmp_path, monkeypatch):
    from memory_queue import _is_owner_only

    destination = _destination(tmp_path)
    harden = Mock(wraps=reliable_memory._harden_runtime_owner_only)
    monkeypatch.setattr(reliable_memory, '_harden_runtime_owner_only', harden)

    reliable_memory.publish_runtime_file(destination, b'accepted', state_root=tmp_path, create_only=True)

    assert harden.call_count == 1
    assert _is_owner_only(destination)


def _readonly_acl(monkeypatch, listing):
    import markdown_transaction

    _icacls_writing(monkeypatch, listing, 'cp866')
    reader = Mock(wraps=markdown_transaction._run_acl_command)
    monkeypatch.setattr(markdown_transaction, '_run_acl_command', reader)
    return reader


def test_readonly_acl_verification_uses_the_same_exact_owner_policy_without_granting(tmp_path, monkeypatch):
    import markdown_transaction

    reader = _readonly_acl(monkeypatch, f'artifact {OWNER}:(F)\n')

    markdown_transaction._verify_windows_owner_acl(tmp_path / 'artifact')

    reader.assert_called_once_with(['icacls', str(tmp_path / 'artifact')])


@pytest.mark.parametrize('listing', [
    f'artifact {OWNER}:(I)(F)\n',
    f'artifact {OWNER}:(R)\n',
    f'artifact {OWNER}:(F)\n         PC\\Гость:(R)\n',
    f'artifact {OWNER}:(F)\n         {OWNER}:(R)\n',
], ids=['inherited-grant', 'incomplete-rights', 'another-reader', 'multiple-owner-entries'])
def test_readonly_acl_verification_refuses_every_previously_forbidden_acl(tmp_path, monkeypatch, listing):
    import markdown_transaction

    reader = _readonly_acl(monkeypatch, listing)

    with pytest.raises(PermissionError, match='owner-only ACL'):
        markdown_transaction._verify_windows_owner_acl(tmp_path / 'artifact')
    reader.assert_called_once_with(['icacls', str(tmp_path / 'artifact')])


@pytest.mark.parametrize('principal', [OWNER + '-backup', 'other-' + OWNER])
def test_readonly_acl_cannot_confuse_a_similar_foreign_principal_with_the_owner(tmp_path, monkeypatch, principal):
    import markdown_transaction

    _readonly_acl(monkeypatch, f'artifact {principal}:(F)\n')

    with pytest.raises(PermissionError, match='owner-only ACL'):
        markdown_transaction._verify_windows_owner_acl(tmp_path / 'artifact')


@pytest.mark.parametrize('principal', [OWNER + '-backup', 'other-' + OWNER])
def test_queue_acl_cannot_confuse_a_similar_foreign_principal_with_the_owner(principal):
    import memory_queue

    assert not memory_queue._acl_is_owner_only([f'artifact {principal}:(F)'], OWNER)
