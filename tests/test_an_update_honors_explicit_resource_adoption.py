"""Explicit takeover during an update retains the reviewed current baseline."""
import json
from dataclasses import replace

import pytest
from install_control import (
    PROFILE_END,
    InstallControlError,
    profile_resource,
    rollback_resources,
    uninstall_resources,
)

from tests.test_a_changed_file_is_named_and_can_be_taken_over import _install, _shared

CURRENT = b"ours\nthe operator's retained change\n"


def _changed_install(tmp_path):
    (tmp_path / 'state').mkdir()
    _install(tmp_path, '4.0.0', _shared(tmp_path, b'ours\n'))
    (tmp_path / 'shared.conf').write_bytes(CURRENT)
    return replace(_shared(tmp_path, b'new release\n'), adopt_current=True)


def test_default_update_still_refuses_unapproved_drift(tmp_path):
    resource = _changed_install(tmp_path)
    with pytest.raises(InstallControlError, match='install_resource_drift'):
        _install(tmp_path, '4.1.0', replace(resource, adopt_current=False))
    assert (tmp_path / 'shared.conf').read_bytes() == CURRENT


def test_explicit_update_adoption_retains_current_bytes_for_rollback(tmp_path):
    resource = _changed_install(tmp_path)
    _install(tmp_path, '4.1.0', resource)
    assert (tmp_path / 'shared.conf').read_bytes() == b'new release\n'
    rollback_resources(state_root=tmp_path / 'state', resources=[resource])
    assert (tmp_path / 'shared.conf').read_bytes() == CURRENT


def test_explicit_update_adoption_retains_current_bytes_for_uninstall(tmp_path):
    resource = _changed_install(tmp_path)
    _install(tmp_path, '4.1.0', resource)
    uninstall_resources(state_root=tmp_path / 'state', resources=[resource])
    assert (tmp_path / 'shared.conf').read_bytes() == CURRENT


def test_explicit_adoption_can_keep_the_current_projection_unchanged(tmp_path):
    resource = replace(_changed_install(tmp_path), desired=CURRENT)
    _install(tmp_path, '4.1.0', resource)
    assert (tmp_path / 'shared.conf').read_bytes() == CURRENT
    uninstall_resources(state_root=tmp_path / 'state', resources=[resource])
    assert (tmp_path / 'shared.conf').read_bytes() == CURRENT


def test_explicit_adoption_does_not_accept_a_damaged_prior_preimage(tmp_path):
    resource = _changed_install(tmp_path)
    install_root = tmp_path / 'state/run/install'
    manifest = json.loads((install_root / 'manifest.json').read_bytes())
    snapshot = manifest['resources'][0]['desired']
    (install_root / snapshot['preimage']).write_bytes(b'damaged preimage')
    with pytest.raises(InstallControlError):
        _install(tmp_path, '4.1.0', resource)
    assert (tmp_path / 'shared.conf').read_bytes() == CURRENT


def test_adopted_profile_keeps_foreign_lines_and_current_preferences(tmp_path):
    (tmp_path / 'state').mkdir()
    profile = tmp_path / 'profile'
    profile.write_bytes(b'# foreign before\n')
    resource = profile_resource(profile, tmp_path / 'vault', tmp_path / 'state')
    _install(tmp_path, '4.0.0', resource)
    changed = resource.desired.replace(
        PROFILE_END, b'export MEMORY_CODEX_MODEL=operator-choice\n' + PROFILE_END,
    )
    profile.write_bytes(b'# foreign before\n' + changed + b'\n# foreign after\n')
    resource = replace(resource, desired=changed, adopt_current=True)
    _install(tmp_path, '4.1.0', resource)
    uninstall_resources(state_root=tmp_path / 'state', resources=[resource])
    assert profile.read_bytes() == b'# foreign before\n' + changed + b'\n# foreign after\n'
