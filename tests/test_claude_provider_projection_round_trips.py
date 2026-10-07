"""The installed nonsecret provider bundle is the owned Claude projection."""
from __future__ import annotations

import json
from pathlib import Path

import install_control as control
import integration_hook_config as hooks
import pytest

CHOICE = {
    'MEMORY_LLM_PROVIDER': 'codex', 'MEMORY_CLAUDE_MODEL': 'claude-sonnet-5',
    'MEMORY_CODEX_MODEL': 'gpt-6-luna', 'MEMORY_CODEX_REASONING': 'max',
}


def _choice(monkeypatch, values):
    for key in hooks.PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _resource(tmp_path):
    root = tmp_path / 'vault'
    return hooks.claude_settings_resource(
        tmp_path / 'settings.json', hooks.claude_settings_template(Path(__file__).resolve().parents[1]), root, root)


def _install(tmp_path, resource):
    return control.install_resources(
        state_root=tmp_path / 'state', vault_root=tmp_path / 'vault',
        release={'project_version': '5.0.0', 'source_mode': 'local_checkout',
                 'commit_oid': 'a' * 40, 'worktree_clean': True, 'uv_lock_sha256': 'b' * 64},
        scheduler_backend='cron', resources=[resource], control_version=2)


def test_complete_six_key_projection_survives_actual_write_and_verify(tmp_path, monkeypatch):
    _choice(monkeypatch, CHOICE)
    resource = _resource(tmp_path)
    expected = json.loads(resource.desired)
    assert len(expected['env']) == 6
    resource.write_owned(resource.desired)
    assert resource.read_owned() == resource.desired
    assert resource.read_projections((resource.desired,)) == resource.desired
    assert _install(tmp_path, resource)['resources'][0]['state'] == 'verified'


@pytest.mark.parametrize('key', hooks.PROVIDER_ENV_KEYS)
def test_each_supported_provider_key_is_owned_but_secrets_and_foreign_values_are_not(
    tmp_path, monkeypatch, key,
):
    _choice(monkeypatch, {key: 'selected'})
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'env': {'OPENAI_API_KEY': 'private-fixture', 'FOREIGN': 'keep'}}))
    resource = _resource(tmp_path)
    resource.write_owned(resource.desired)
    assert json.loads(resource.read_owned())['env'][key] == 'selected'
    resource.write_owned(None)
    assert json.loads(path.read_text())['env'] == {'OPENAI_API_KEY': 'private-fixture', 'FOREIGN': 'keep'}


def test_legacy_four_key_update_and_rollback_keep_complete_previous_bundle(tmp_path, monkeypatch):
    _choice(monkeypatch, {'MEMORY_LLM_PROVIDER': 'claude', 'MEMORY_CLAUDE_MODEL': 'old-model'})
    old = _resource(tmp_path)
    first = _install(tmp_path, old)
    original = old.read_owned()
    _choice(monkeypatch, CHOICE)
    new = _resource(tmp_path)
    updated = _install(tmp_path, new)
    assert updated['generation'] == first['generation'] + 1
    assert new.read_owned() == new.desired
    assert control.rollback_resources(state_root=tmp_path / 'state', resources=[new])['state'] == 'committed'
    assert new.read_owned() == original


def test_historical_reader_update_preserves_unrecorded_existing_provider_values(tmp_path, monkeypatch):
    _choice(monkeypatch, {'MEMORY_LLM_PROVIDER': 'claude', 'MEMORY_CLAUDE_MODEL': 'old-model'})
    old = _resource(tmp_path)
    family = hooks._HookFamily(hooks.CLAUDE_FAMILY.name, hooks.CLAUDE_FAMILY.handler_is_ours, (
        'LLM_WIKI_ROOT', 'LLM_WIKI_STATE_ROOT', 'MEMORY_LLM_PROVIDER', 'MEMORY_CLAUDE_MODEL'))
    legacy = hooks._hook_family_resource(
        resource_id=old.resource_id, kind=old.kind, path=tmp_path / 'settings.json',
        family=family, desired=old.desired, config_existed=False,
        finish=hooks._claude_finish(hooks.claude_settings_template(Path(__file__).resolve().parents[1])))
    _install(tmp_path, legacy)
    path = tmp_path / 'settings.json'
    config = json.loads(path.read_text())
    config['env'].update(MEMORY_CODEX_MODEL='previous-codex', MEMORY_CODEX_REASONING='low')
    path.write_text(json.dumps(config))
    _choice(monkeypatch, CHOICE)
    new = _resource(tmp_path)
    original = path.read_bytes()
    with pytest.raises(control.InstallControlError, match='install_resource_drift'):
        _install(tmp_path, new)
    assert path.read_bytes() == original
    adopted = control._marked_adopted([new], ['claude-user-settings'])[0]
    _install(tmp_path, adopted)
    assert adopted.read_owned() == adopted.desired
    control.rollback_resources(state_root=tmp_path / 'state', resources=[adopted])
    restored = json.loads(path.read_text())['env']
    assert restored['MEMORY_CODEX_MODEL'] == 'previous-codex'
    assert restored['MEMORY_CODEX_REASONING'] == 'low'


def test_explicit_projection_preimage_drift_refuses_before_writing(tmp_path, monkeypatch):
    _choice(monkeypatch, CHOICE)
    resource = _resource(tmp_path)
    resource.write_owned(resource.desired)
    original = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(control.InstallControlError, match='integration_hook_config_changed'):
        resource.write_projection(b'wrong-preimage', resource.desired, resource.metadata)
    assert (tmp_path / 'settings.json').read_bytes() == original


def test_fake_is_not_persisted_and_foreign_root_remains_unowned(tmp_path, monkeypatch):
    _choice(monkeypatch, {'MEMORY_LLM_PROVIDER': 'fake', 'MEMORY_CODEX_MODEL': 'ignored'})
    resource = _resource(tmp_path)
    assert set(json.loads(resource.desired)['env']) == {'LLM_WIKI_ROOT', 'LLM_WIKI_STATE_ROOT'}
    foreign = json.loads(resource.desired)
    foreign['env']['LLM_WIKI_ROOT'] = str(tmp_path / 'other-vault')
    assert not resource.recognizes(hooks.canonical_json_bytes(foreign))
