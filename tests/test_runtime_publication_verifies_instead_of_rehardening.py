"""The publication boundary must verify preserved permissions instead of repairing drift."""
import os
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import queue_core
import reliable_memory
from llm_wiki import runtime_environment, runtime_publication

from tests.a_windows_owner_with_a_non_ascii_name_is_verified_helpers import OWNER, _icacls_writing


def _destination(tmp_path):
    destination = tmp_path / 'run/accepted.json'
    destination.parent.mkdir()
    return destination


def test_same_parent_publication_hardens_only_its_private_staged_file(tmp_path, monkeypatch):
    destination = _destination(tmp_path)
    harden = Mock(wraps=reliable_memory.harden_runtime_owner_only)
    monkeypatch.setattr(runtime_publication, 'harden_runtime_owner_only', harden)

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
    publish = reliable_memory.publish_staged

    def broaden(*args):
        publish(*args)
        destination.chmod(0o644)

    monkeypatch.setattr(runtime_publication, 'publish_staged', broaden)

    with pytest.raises(PermissionError, match='mode'):
        reliable_memory.publish_runtime_file(destination, b'accepted', state_root=tmp_path, create_only=True)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert destination.read_bytes() == b'accepted'


def test_initial_permission_failure_cannot_publish_an_artifact(tmp_path, monkeypatch):
    destination = _destination(tmp_path)
    harden = Mock(side_effect=PermissionError('initial private permissions denied'))
    monkeypatch.setattr(runtime_publication, 'harden_runtime_owner_only', harden)

    with pytest.raises(PermissionError, match='initial private permissions denied'):
        reliable_memory.publish_runtime_file(destination, b'accepted', state_root=tmp_path, create_only=True)
    assert not destination.exists()


def test_windows_postpublication_check_reads_the_owner_acl_and_propagates_a_refusal(tmp_path, monkeypatch):
    from llm_wiki import windows_acl

    destination = _destination(tmp_path)
    verify = Mock(side_effect=PermissionError('owner-only ACL changed'))
    monkeypatch.setattr(runtime_publication, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(windows_acl, 'verify_windows_owner_acl', verify)

    with pytest.raises(PermissionError, match='owner-only ACL changed'):
        reliable_memory.verify_published_runtime_permissions(destination, 0o600)
    verify.assert_called_once_with(destination)


@pytest.mark.parametrize('mode', [0o400, 0o600, 0o640])
@pytest.mark.skipif(os.name == 'nt', reason='POSIX requested-mode contract')
def test_explicit_requested_modes_survive_publication(tmp_path, mode):
    destination = _destination(tmp_path)

    reliable_memory.publish_runtime_file(destination, b'accepted', state_root=tmp_path, create_only=True, mode=mode)

    assert stat.S_IMODE(destination.stat().st_mode) == mode


@pytest.mark.skipif(os.name != 'nt', reason='actual Windows ACL verification')
def test_windows_published_artifact_is_owner_only_without_a_second_permission_write(tmp_path, monkeypatch):
    from llm_wiki.queue_artifacts import is_owner_only

    destination = _destination(tmp_path)
    harden = Mock(wraps=reliable_memory.harden_runtime_owner_only)
    monkeypatch.setattr(runtime_publication, 'harden_runtime_owner_only', harden)

    reliable_memory.publish_runtime_file(destination, b'accepted', state_root=tmp_path, create_only=True)

    assert harden.call_count == 1
    assert is_owner_only(destination)


def _readonly_acl(monkeypatch, listing):
    from llm_wiki import windows_acl

    _icacls_writing(monkeypatch, listing, 'cp866')
    reader = Mock(wraps=windows_acl.run_acl_command)
    monkeypatch.setattr(windows_acl, 'run_acl_command', reader)
    return reader


def test_readonly_acl_verification_uses_the_same_exact_owner_policy_without_granting(tmp_path, monkeypatch):
    from llm_wiki import windows_acl

    reader = _readonly_acl(monkeypatch, f'artifact {OWNER}:(F)\n')

    windows_acl.verify_windows_owner_acl(tmp_path / 'artifact')

    reader.assert_called_once_with(['icacls', str(tmp_path / 'artifact')])


@pytest.mark.parametrize('listing', [
    f'artifact {OWNER}:(I)(F)\n',
    f'artifact {OWNER}:(R)\n',
    f'artifact {OWNER}:(F)\n         PC\\Гость:(R)\n',
    f'artifact {OWNER}:(F)\n         {OWNER}:(R)\n',
], ids=['inherited-grant', 'incomplete-rights', 'another-reader', 'multiple-owner-entries'])
def test_readonly_acl_verification_refuses_every_previously_forbidden_acl(tmp_path, monkeypatch, listing):
    from llm_wiki import windows_acl

    reader = _readonly_acl(monkeypatch, listing)

    with pytest.raises(PermissionError, match='owner-only ACL'):
        windows_acl.verify_windows_owner_acl(tmp_path / 'artifact')
    reader.assert_called_once_with(['icacls', str(tmp_path / 'artifact')])


@pytest.mark.parametrize('principal', [OWNER + '-backup', 'other-' + OWNER])
def test_readonly_acl_cannot_confuse_a_similar_foreign_principal_with_the_owner(tmp_path, monkeypatch, principal):
    from llm_wiki import windows_acl

    _readonly_acl(monkeypatch, f'artifact {principal}:(F)\n')

    with pytest.raises(PermissionError, match='owner-only ACL'):
        windows_acl.verify_windows_owner_acl(tmp_path / 'artifact')


@pytest.mark.parametrize('principal', [OWNER + '-backup', 'other-' + OWNER])
def test_queue_acl_cannot_confuse_a_similar_foreign_principal_with_the_owner(principal):

    assert not queue_core.acl_is_owner_only([f'artifact {principal}:(F)'], OWNER)


def test_windows_initial_permissions_call_the_acl_owner_directly(tmp_path, monkeypatch):
    from llm_wiki import windows_acl

    harden = Mock()
    monkeypatch.setattr(runtime_environment, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(windows_acl, 'harden_windows_acl', harden)
    reliable_memory.harden_runtime_owner_only(tmp_path / 'artifact', 0o600)
    harden.assert_called_once_with(tmp_path / 'artifact')


def test_windows_runtime_reader_refuses_the_acl_owners_negative_answer(tmp_path, monkeypatch):
    from llm_wiki import windows_acl

    verify = Mock(return_value=False)
    monkeypatch.setattr(windows_acl, 'is_owner_only_windows', verify)
    with pytest.raises(PermissionError, match='runtime file must be owner-only'):
        reliable_memory.require_windows_owner_only(tmp_path / 'artifact')
    verify.assert_called_once_with(tmp_path / 'artifact')


def _markdown_compatibility_names():
    import markdown_transaction

    return dict(
        acl_entry_lines=markdown_transaction.acl_entry_lines,
        acl_failure=markdown_transaction.acl_failure,
        acl_line_names_owner=markdown_transaction.acl_line_names_owner,
        acl_lines_naming=markdown_transaction.acl_lines_naming,
        acl_output_encodings=markdown_transaction.acl_output_encodings,
        acl_output_text=markdown_transaction.acl_output_text,
        acl_permission=markdown_transaction.acl_permission,
        apply_windows_acl=markdown_transaction.apply_windows_acl,
        console_code_page=markdown_transaction.console_code_page,
        harden_windows_acl=markdown_transaction.harden_windows_acl,
        names_identity=markdown_transaction.names_identity,
        read_windows_acl=markdown_transaction.read_windows_acl,
        require_verified_windows_owner_acl=markdown_transaction.require_verified_windows_owner_acl,
        run_acl_command=markdown_transaction.run_acl_command,
        verified_owner_acl_line=markdown_transaction.verified_owner_acl_line,
        verify_no_other_acl=markdown_transaction.verify_no_other_acl,
        verify_windows_owner_acl=markdown_transaction.verify_windows_owner_acl,
        windows_acl_identity=markdown_transaction.windows_acl_identity,
    )


def _queue_compatibility_names():
    return dict(
        acl_is_owner_only=queue_core.acl_is_owner_only,
        acl_owner_lines=queue_core.acl_owner_lines,
        is_owner_only_windows=queue_core.is_owner_only_windows,
        windows_acl_lines=queue_core.windows_acl_lines,
    )


def _canonical_markdown_names():
    from llm_wiki import windows_acl

    return dict(
        acl_entry_lines=windows_acl.acl_entry_lines,
        acl_failure=windows_acl.acl_failure,
        acl_line_names_owner=windows_acl.acl_line_names_owner,
        acl_lines_naming=windows_acl.acl_lines_naming,
        acl_output_encodings=windows_acl.acl_output_encodings,
        acl_output_text=windows_acl.acl_output_text,
        acl_permission=windows_acl.acl_permission,
        apply_windows_acl=windows_acl.apply_windows_acl,
        console_code_page=windows_acl.console_code_page,
        harden_windows_acl=windows_acl.harden_windows_acl,
        names_identity=windows_acl.names_identity,
        read_windows_acl=windows_acl.read_windows_acl,
        require_verified_windows_owner_acl=windows_acl.require_verified_windows_owner_acl,
        run_acl_command=windows_acl.run_acl_command,
        verified_owner_acl_line=windows_acl.verified_owner_acl_line,
        verify_no_other_acl=windows_acl.verify_no_other_acl,
        verify_windows_owner_acl=windows_acl.verify_windows_owner_acl,
        windows_acl_identity=windows_acl.windows_acl_identity,
    )


def _canonical_queue_names():
    from llm_wiki import windows_acl

    return dict(
        acl_is_owner_only=windows_acl.acl_is_owner_only,
        acl_owner_lines=windows_acl.acl_owner_lines,
        is_owner_only_windows=windows_acl.is_owner_only_windows,
        windows_acl_lines=windows_acl.windows_acl_lines,
    )


def _assert_same_objects(compatibility, canonical):
    assert compatibility.keys() == canonical.keys()
    for name, value in compatibility.items():
        assert value is canonical[name]


def test_windows_acl_compatibility_names_are_identical():
    from tests.modules.windows_acl.boundary_helpers import NAMES

    markdown_names = _markdown_compatibility_names()
    queue_names = _queue_compatibility_names()
    assert set(markdown_names).union(queue_names) == NAMES
    _assert_same_objects(markdown_names, _canonical_markdown_names())
    _assert_same_objects(queue_names, _canonical_queue_names())
