"""A desired install resource supplies expected bundle authority, not a live hook."""
import ctypes
import json
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import codex_memory
import doctor
import generation_catalog
import install_control as install
import integration_hook_config as hooks
import pytest
from reliable_memory import canonical_json_bytes

from tests.test_codex_installed_provider_bundle_reaches_launch import BUNDLE
from tests.test_install_control import _release

ROOT = Path(__file__).resolve().parents[1]


def _installed(tmp_path):
    root, state, home = tmp_path / 'vault', tmp_path / 'state', tmp_path / 'home'
    root.mkdir()
    template = hooks.codex_hooks_template(ROOT)
    resource = hooks.codex_hooks_resource(home / '.codex/hooks.json', template,
                                          root=root, provider_bundle=BUNDLE)
    install.install_resources(state_root=state, vault_root=root, release=_release(),
                              scheduler_backend='cron', resources=[resource], control_version=2)
    return root, state, home, resource


def test_expected_bundle_comes_from_verified_desired_not_doctor_host_env(tmp_path, monkeypatch):
    root, state, home, _resource = _installed(tmp_path)
    monkeypatch.setenv('MEMORY_CODEX_MODEL', 'different-host-model')
    assert install.installed_codex_provider_bundle(root, state, home) == BUNDLE


def test_live_hook_drift_cannot_supply_a_new_expected_bundle(tmp_path):
    root, state, home, resource = _installed(tmp_path)
    template = hooks.codex_hooks_template(ROOT)
    other = hooks.codex_hooks_resource(home / '.codex/hooks.json', template, root=root,
                                       provider_bundle={'MEMORY_LLM_PROVIDER': 'ollama'})
    resource.write_owned(other.desired)
    assert install.installed_codex_provider_bundle(root, state, home) == BUNDLE
    assert other.desired != resource.desired


@pytest.mark.parametrize('wrong', ['root', 'home'])
def test_verified_snapshot_for_a_different_installation_is_not_authority(tmp_path, wrong):
    root, state, home, _resource = _installed(tmp_path)
    replacements = {'root': (tmp_path / 'other', state, home),
                    'home': (root, state, tmp_path / 'other')}
    with pytest.raises(install.InstallControlError):
        install.installed_codex_provider_bundle(*replacements[wrong])


def test_desired_preimage_byte_tamper_refuses(tmp_path):
    root, state, home, _resource = _installed(tmp_path)
    manifest = json.loads((state / 'run/install/manifest.json').read_bytes())
    path = state / 'run/install' / manifest['resources'][0]['desired']['preimage']
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(install.InstallControlError):
        install.installed_codex_provider_bundle(root, state, home)


def test_manifest_transaction_digest_mismatch_refuses(tmp_path):
    root, state, home, _resource = _installed(tmp_path)
    path = state / 'run/install/manifest.json'
    value = json.loads(path.read_bytes())
    value['request_sha256'] = 'b' * 64
    path.write_bytes(canonical_json_bytes(value))
    with pytest.raises(install.InstallControlError):
        install.installed_codex_provider_bundle(root, state, home)


def test_concurrent_record_change_after_valid_desired_read_refuses(tmp_path, monkeypatch):
    root, state, home, _resource = _installed(tmp_path)
    original = install._codex_desired_bundle
    def changed(desired, actual_root):
        result = original(desired, actual_root)
        path = state / 'run/install/transaction.json'
        path.write_bytes(path.read_bytes() + b'\n')
        return result
    monkeypatch.setattr(install, '_codex_desired_bundle', changed)
    with pytest.raises(install.InstallControlError, match='snapshot_changed'):
        install.installed_codex_provider_bundle(root, state, home)


def test_old_direct_desired_projection_cannot_prove_a_provider_bundle(tmp_path):
    root, state, home, _resource = _installed(tmp_path)
    old = hooks.codex_hooks_template(ROOT)
    projection = canonical_json_bytes({'env': {}, 'hooks': old['hooks']})
    with pytest.raises(install.InstallControlError, match='desired_invalid'):
        install._codex_desired_bundle(projection, root)


def _wanted_runtime(root):
    destination = root / 'integrations/codex/hooks.json'
    destination.parent.mkdir(parents=True)
    destination.write_bytes((ROOT / 'integrations/codex/hooks.json').read_bytes())
    expected = doctor._expected_codex_runtime_hooks(destination, BUNDLE)
    return [dict(wanted, enabled=True, trustStatus='trusted') for wanted in expected]


def test_doctor_uses_shared_renderer_and_independent_native_trust(tmp_path):
    root, state, home, _resource = _installed(tmp_path)
    observed = _wanted_runtime(root)
    assert doctor._codex_hooks_verdict(root, observed, BUNDLE) == (True, 'runtime_hooks_active')
    observed[0]['trustStatus'] = 'modified'
    assert doctor._codex_hooks_verdict(root, observed, BUNDLE) == (False, 'runtime_hooks_modified')


def test_mcp_expected_bundle_does_not_grant_ownership_or_enable_foreign_entry(tmp_path):
    root, _state, home, _resource = _installed(tmp_path)
    config = home / '.codex/config.toml'
    config.write_text('[mcp_servers.llm-wiki]\ncommand="node"\nargs=["foreign.js"]\n', encoding='utf-8')
    assert codex_memory.codex_mcp_config_state(config, root, BUNDLE) == 'conflict'
    assert codex_memory.replace_codex_mcp_entry(config, root, foreign=False) == 'refused-foreign'
    config.write_text('[mcp_servers.llm-wiki]\nenabled=false\ncommand="uv"\nargs=[]\n', encoding='utf-8')
    assert codex_memory.codex_mcp_config_state(config, root, BUNDLE) == 'disabled'


def test_archive_foreign_root_owned_bootstrap_is_not_taken_over(tmp_path):
    root, _state, home, resource = _installed(tmp_path)
    other = hooks.codex_hooks_resource(home / '.codex/hooks.json', hooks.codex_hooks_template(ROOT),
                                       root=tmp_path / 'different-vault', provider_bundle=BUNDLE)
    assert not other.recognizes(resource.desired)
    assert resource.read_owned() == resource.desired
    assert root != tmp_path / 'different-vault'


def test_provider_update_rollback_and_uninstall_restore_exact_preimages(tmp_path):
    root, state, home, resource = _installed(tmp_path)
    original = resource.read_owned()
    other = hooks.codex_hooks_resource(home / '.codex/hooks.json', hooks.codex_hooks_template(ROOT),
                                       root=root, provider_bundle={'MEMORY_LLM_PROVIDER': 'ollama'})
    install.install_resources(state_root=state, vault_root=root, release=_release(),
                              scheduler_backend='cron', resources=[other], control_version=2)
    assert install.installed_codex_provider_bundle(root, state, home) == {'MEMORY_LLM_PROVIDER': 'ollama'}
    install.rollback_resources(state_root=state, resources=[other])
    assert resource.read_owned() == original
    assert install.installed_codex_provider_bundle(root, state, home) == BUNDLE
    install.uninstall_resources(state_root=state, resources=[resource])
    assert not (home / '.codex/hooks.json').exists()


def test_uninstall_preserves_mcp_registration_as_unowned(tmp_path):
    root, state, home, resource = _installed(tmp_path)
    config = home / '.codex/config.toml'
    config.write_text(codex_memory._mcp_block(root), encoding='utf-8')
    before = config.read_bytes()
    install.uninstall_resources(state_root=state, resources=[resource])
    assert config.read_bytes() == before
    assert install.unowned_agent_registrations(home)[0]['agent'] == 'codex'


def test_inconsistent_desired_command_bundles_refuse(tmp_path):
    root, _state, _home, resource = _installed(tmp_path)
    desired = json.loads(resource.desired)
    handler = desired['hooks']['SessionStart'][0]['hooks'][0]
    other = hooks.codex_rendered_template(root, hooks.codex_hooks_template(ROOT),
                                         {'MEMORY_LLM_PROVIDER': 'ollama'})
    handler['commandWindows'] = other['hooks']['SessionStart'][0]['hooks'][0]['commandWindows']
    with pytest.raises(install.InstallControlError, match='desired_invalid'):
        install._codex_desired_bundle(canonical_json_bytes(desired), root)


def test_bootstrap_marker_does_not_own_forged_script_argument_pair(tmp_path):
    import shlex

    from codex_hook_identity import is_our_codex_command

    wrong = ['uv', *hooks.codex_launch_args(tmp_path, 'graph_hint.py', ['hook'], BUNDLE)]
    assert not is_our_codex_command(shlex.join(wrong))


def test_bootstrap_code_drift_does_not_become_owned_legacy_command(tmp_path):
    import shlex

    from codex_hook_identity import is_our_codex_command

    wrong = ['uv', *hooks.codex_launch_args(tmp_path, 'codex_memory.py', ['hook'], BUNDLE)]
    wrong[8] += ';print("unapproved-code")'
    assert not is_our_codex_command(shlex.join(wrong))


def _active_probe(_root, _home, *, deadline, provider_bundle):
    assert deadline > 0
    assert provider_bundle == BUNDLE
    return True, 'runtime_hooks_active'


def _mcp_with_bundle(root, bundle):
    args = ', '.join(json.dumps(item) for item in codex_memory._mcp_expected_args(root, bundle))
    return f'[mcp_servers.llm-wiki]\ncommand="uv"\nargs=[{args}]\n'


def test_actual_host_check_uses_verified_bundle_and_equivalent_mcp(tmp_path, monkeypatch):
    root, state, home, _resource = _installed(tmp_path)
    monkeypatch.setenv('MEMORY_CODEX_MODEL', 'different-host-model')
    monkeypatch.setattr(doctor, '_codex_runtime_hooks_state', _active_probe)
    config = home / '.codex/config.toml'
    config.write_text(_mcp_with_bundle(root, BUNDLE), encoding='utf-8')
    result = doctor._codex_host_result(root, home, 1000, state)
    assert result['status'] == 'ok'
    assert result['provider_bundle'] == 'verified-installed-desired'
    config.write_text(_mcp_with_bundle(root, {'MEMORY_LLM_PROVIDER': 'ollama'}), encoding='utf-8')
    result = doctor._codex_host_result(root, home, 1000, state)
    assert result['status'] == 'ok'
    assert result['capture_mode'] == 'official-hooks'
    assert result['provider_transport'] == {'status': 'degraded', 'reason': 'runtime_mcp_provider_stale'}
    assert doctor._integration_summary({}, {'codex': result})[0] == 'degraded'


def test_missing_install_proof_never_reports_legacy_hooks_healthy(tmp_path, monkeypatch):
    root, state, home = tmp_path / 'vault', tmp_path / 'state', tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    monkeypatch.setattr(doctor, '_codex_runtime_hooks_state',
                        lambda *_args, **_kwargs: (True, 'runtime_hooks_active'))
    result = doctor._codex_host_result(root, home, 1000, state)
    assert result['status'] == 'degraded'
    assert result['reason'] == 'runtime_provider_bundle_unverified'
    assert result['native_hook_status'] == 'ok'
    assert result['capture_mode'] == 'official-hooks'
    assert result['provider_bundle'] == 'unknown'


@pytest.mark.parametrize('reason', ['runtime_hooks_untrusted', 'runtime_hooks_modified',
                                   'runtime_hooks_disabled', 'runtime_hooks_not_completed'])
def test_unknown_bundle_preserves_native_refusal_and_deadline_findings(tmp_path, monkeypatch, reason):
    root, state, home = tmp_path / 'vault', tmp_path / 'state', tmp_path / 'home'
    (home / '.codex').mkdir(parents=True)
    monkeypatch.setattr(doctor, '_codex_runtime_hooks_state',
                        lambda *_args, **_kwargs: (False, reason))
    result = doctor._codex_host_result(root, home, 1000, state)
    assert result['status'] == 'degraded'
    assert result['reason'] == reason
    assert result['capture_mode'] == 'none'
    assert result['provider_bundle'] == 'unknown'
    assert result['provider_transport']['reason'] == 'runtime_provider_bundle_unverified'


def test_placeholder_substitution_preserves_literal_posix_root_and_platform_pair(tmp_path):
    root = tmp_path / 'Café "quote" & $variable vault'
    resource = hooks.codex_hooks_resource(tmp_path / 'hooks.json', hooks.codex_hooks_template(ROOT),
                                         root=root, provider_bundle=BUNDLE)
    desired = json.loads(resource.desired)
    command = desired['hooks']['SessionStart'][0]['hooks'][0]['command']
    assert hooks.codex_command_launch(command) == (root.resolve(), BUNDLE)
    assert hooks.codex_projection_bundle(resource.desired, root) == BUNDLE


def _proof_paths(state):
    install_root = state / 'run/install'
    manifest_path = install_root / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    return {'manifest': manifest_path,
            'transaction': install_root / 'transaction.json',
            'desired': install_root / manifest['resources'][0]['desired']['preimage']}


def _same_bytes_replacement(path):
    replacement = path.with_suffix('.replacement')
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(path.stat().st_mode & 0o777)
    _replace_open_target(replacement, path)


class _RenameInformation(ctypes.Structure):
    # SDK FILE_RENAME_INFO: DWORD Flags union, HANDLE, DWORD, WCHAR[1].
    _fields_ = [('flags', ctypes.c_uint32), ('root', ctypes.c_void_p),
                ('length', ctypes.c_uint32), ('name', ctypes.c_uint16 * 1)]


def _rename_information(path):
    encoded = str(path.resolve()).encode('utf-16-le')
    offset = _RenameInformation.name.offset
    buffer = ctypes.create_string_buffer(max(ctypes.sizeof(_RenameInformation),
                                            offset + len(encoded) + ctypes.sizeof(ctypes.c_uint16)))
    record = _RenameInformation.from_buffer(buffer)
    record.flags = 3  # REPLACE_IF_EXISTS | POSIX_SEMANTICS; no readonly bypass.
    record.length = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    return buffer


def _set_rename_information(handle, buffer):
    function = ctypes.WinDLL('kernel32', use_last_error=True).SetFileInformationByHandle
    function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    if not function(int(handle), 22, buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_replace_open_target(source, target):
    import win32file

    handle = win32file.CreateFile(str(source.resolve()), 0x00010000, 7, None, 3,
                                  0x00200000, None)
    try:
        _observe_native_destination('before', handle, source, target)
        _set_rename_information(handle, _rename_information(target))
        actual_name = win32file.GetFinalPathNameByHandle(handle, 0)
        expected_name = generation_catalog._windows_long_path(target)
        assert actual_name == expected_name, (actual_name, expected_name)
        _observe_native_destination('after', handle, source, target)
    finally:
        handle.Close()


def _native_handle_snapshot(handle):
    import win32file

    return {'final_name': win32file.GetFinalPathNameByHandle(handle, 0),
            'file_information': win32file.GetFileInformationByHandle(handle)}


def _native_path_snapshot(path):
    if not path.exists():
        return {'exists': False}
    handle = generation_catalog._windows_read_handle(path)
    try:
        return _native_handle_snapshot(handle)
    finally:
        _close_native_stage_handle(handle)


def _observe_native_destination(stage, handle, source, target):
    print('native_destination', repr({'stage': stage, 'requested_target': str(target.resolve()),
                                     'source_handle': _native_handle_snapshot(handle),
                                     'target_handle': _native_path_snapshot(target),
                                     'source_path_exists': source.exists()}))


def _replace_open_target(source, target):
    if os.name == 'nt':
        _windows_replace_open_target(source, target)
        return
    source.replace(target)


@pytest.mark.parametrize('name', ['target.json', 'проверка-𐐀.json'])
def test_native_rename_name_stops_before_unrelated_memory(tmp_path, name):
    path = tmp_path / name
    buffer = _rename_information(path)
    record = _RenameInformation.from_buffer(buffer)
    adjacent = 'UNRELATED MEMORY'.encode('utf-16-le') + b'\0\0'
    name_memory = buffer.raw[_RenameInformation.name.offset:] + adjacent
    interpreted = name_memory.decode('utf-16-le').split('\0', 1)[0]
    assert interpreted == str(path.resolve())
    assert record.length == len(str(path.resolve()).encode('utf-16-le'))


@pytest.mark.parametrize('name', ['target.json', 'проверка-𐐀.json'])
def test_native_rename_buffer_keeps_full_utf16_path_and_only_posix_replace_flags(tmp_path, name):
    path = tmp_path / name
    buffer = _rename_information(path)
    record = _RenameInformation.from_buffer(buffer)
    encoded = str(path.resolve()).encode('utf-16-le')
    assert record.flags == 3
    assert record.root is None
    assert record.length == len(encoded)
    offset = _RenameInformation.name.offset
    assert buffer.raw[offset:offset + record.length] == encoded
    assert _RenameInformation.root.offset >= ctypes.sizeof(ctypes.c_uint32)
    assert _RenameInformation.root.offset % ctypes.alignment(ctypes.c_void_p) == 0
    assert _RenameInformation.length.offset == (_RenameInformation.root.offset
                                               + ctypes.sizeof(ctypes.c_void_p))
    assert offset == _RenameInformation.length.offset + ctypes.sizeof(ctypes.c_uint32)


def test_native_fixture_writer_closes_source_handle_when_rename_refuses(tmp_path, monkeypatch):
    observed = []
    handle = SimpleNamespace(Close=lambda: observed.append('closed'))
    def create(*arguments):
        observed.append(arguments)
        return handle
    def refused(actual_handle, buffer):
        assert actual_handle is handle
        assert _RenameInformation.from_buffer(buffer).flags == 3
        raise OSError('native rename refused')
    monkeypatch.setitem(sys.modules, 'win32file', SimpleNamespace(CreateFile=create))
    monkeypatch.setattr(sys.modules[__name__], '_set_rename_information', refused)
    monkeypatch.setattr(sys.modules[__name__], '_observe_native_destination',
                        lambda *_arguments: None)
    source, target = tmp_path / 'source', tmp_path / 'target'
    with pytest.raises(OSError, match='native rename refused'):
        _windows_replace_open_target(source, target)
    assert observed == [(str(source.resolve()), 0x00010000, 7, None, 3, 0x00200000, None),
                        'closed']


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows open-target rename semantics')
@pytest.mark.parametrize('name', ['target.json', 'проверка-𐐀.json'])
def test_native_posix_replacement_reaches_held_identity_while_movefileex_refuses(tmp_path, name):
    path, source = tmp_path / name, tmp_path / 'replacement.json'
    path.write_bytes(b'original')
    source.write_bytes(b'changed')
    with install._provider_binary_file(path) as held:
        previous = os.fstat(held.fileno())
        with pytest.raises(PermissionError) as refusal:
            source.replace(path)
        assert refusal.value.winerror == 5
        assert path.read_bytes() == b'original'
        _windows_replace_open_target(source, path)
        assert held.read() == b'original'
        assert path.read_bytes() == b'changed'
        assert (previous.st_dev, previous.st_ino) != (path.stat().st_dev, path.stat().st_ino)
    assert not source.exists()


@pytest.mark.parametrize('record', ['manifest', 'transaction', 'desired'])
def test_same_byte_proof_file_replacement_is_not_stable_authority(tmp_path, monkeypatch, record):
    root, state, home, _resource = _installed(tmp_path)
    path = _proof_paths(state)[record]
    original = install._codex_desired_bundle

    def replace_after_proof(desired, actual_root):
        result = original(desired, actual_root)
        _same_bytes_replacement(path)
        return result

    monkeypatch.setattr(install, '_codex_desired_bundle', replace_after_proof)
    with pytest.raises(install.InstallControlError, match='snapshot_changed'):
        install.installed_codex_provider_bundle(root, state, home)


@pytest.mark.skipif(__import__('os').name == 'nt', reason='POSIX mode mutation; NT ACL controls separate')
def test_proof_file_permissions_change_is_not_stable_authority(tmp_path, monkeypatch):
    root, state, home, _resource = _installed(tmp_path)
    path = _proof_paths(state)['manifest']
    original = install._codex_desired_bundle

    def chmod_after_proof(desired, actual_root):
        result = original(desired, actual_root)
        path.chmod((path.stat().st_mode & 0o777) ^ 0o040)
        return result

    monkeypatch.setattr(install, '_codex_desired_bundle', chmod_after_proof)
    with pytest.raises(install.InstallControlError, match='snapshot_changed'):
        install.installed_codex_provider_bundle(root, state, home)


def test_unrelated_install_directory_timestamp_is_not_file_identity(tmp_path, monkeypatch):
    root, state, home, _resource = _installed(tmp_path)
    original = install._codex_desired_bundle

    def touch_directory(desired, actual_root):
        result = original(desired, actual_root)
        (state / 'run/install/unrelated').write_bytes(b'not installation authority')
        return result

    monkeypatch.setattr(install, '_codex_desired_bundle', touch_directory)
    assert install.installed_codex_provider_bundle(root, state, home) == BUNDLE


def _observe_proof_handles(monkeypatch):
    handles = []
    original = install._provider_open

    def observed(stack, path):
        handle = original(stack, path)
        handles.append(handle)
        return handle

    monkeypatch.setattr(install, '_provider_open', observed)
    return handles


def test_success_closes_every_proof_handle_and_restores_scope(tmp_path, monkeypatch):
    root, state, home, _resource = _installed(tmp_path)
    handles = _observe_proof_handles(monkeypatch)
    assert install.installed_codex_provider_bundle(root, state, home) == BUNDLE
    assert handles
    assert all(handle.closed for handle in handles)
    assert install._PROVIDER_READ_PROOF.get() is None


def test_refusal_closes_every_proof_handle_and_restores_scope(tmp_path, monkeypatch):
    root, state, home, _resource = _installed(tmp_path)
    handles = _observe_proof_handles(monkeypatch)
    original = install._codex_desired_bundle

    def changed(desired, actual_root):
        result = original(desired, actual_root)
        _same_bytes_replacement(_proof_paths(state)['manifest'])
        return result

    monkeypatch.setattr(install, '_codex_desired_bundle', changed)
    with pytest.raises(install.InstallControlError, match='snapshot_changed'):
        install.installed_codex_provider_bundle(root, state, home)
    assert handles
    assert all(handle.closed for handle in handles)
    assert install._PROVIDER_READ_PROOF.get() is None


def test_nested_proof_scope_restores_outer_without_sharing_records(tmp_path):
    root, state, home, _resource = _installed(tmp_path)
    with install._provider_snapshot_proof():
        outer = install._PROVIDER_READ_PROOF.get()
        assert install.installed_codex_provider_bundle(root, state, home) == BUNDLE
        assert install._PROVIDER_READ_PROOF.get() is outer
        assert outer.reads == []
    assert install._PROVIDER_READ_PROOF.get() is None


@pytest.mark.parametrize('record', ['manifest', 'transaction'])
def test_same_bytes_replacement_during_schema_validation_refuses(tmp_path, monkeypatch, record):
    root, state, home, _resource = _installed(tmp_path)
    path = _proof_paths(state)[record]
    original = install.validate_schema
    changed = []

    def validate_and_replace(value, schema):
        original(value, schema)
        if not changed:
            _same_bytes_replacement(path)
            changed.append(path)

    monkeypatch.setattr(install, 'validate_schema', validate_and_replace)
    with pytest.raises(install.InstallControlError, match='snapshot_changed'):
        install.installed_codex_provider_bundle(root, state, home)


@pytest.mark.skipif(__import__('os').name != 'nt', reason='requires actual Windows file-handle DACL')
def test_native_windows_proof_detects_dacl_change_with_identical_file_bytes(tmp_path, monkeypatch):
    from markdown_transaction import _run_acl_command

    root, state, home, _resource = _installed(tmp_path)
    path = _proof_paths(state)['manifest']
    before = path.read_bytes()
    original = install._codex_desired_bundle

    def grant_after_proof(desired, actual_root):
        result = original(desired, actual_root)
        changed = _run_acl_command(['icacls', str(path), '/grant', '*S-1-1-0:(R)'])
        assert changed.returncode == 0, (changed.stdout, changed.stderr)
        assert path.read_bytes() == before
        return result

    monkeypatch.setattr(install, '_codex_desired_bundle', grant_after_proof)
    with pytest.raises(install.InstallControlError, match='snapshot_changed'):
        install.installed_codex_provider_bundle(root, state, home)


def _buffer_descriptor_platform(monkeypatch, descriptor):
    import sys
    from types import SimpleNamespace

    def get_security(handle, kind, flags):
        assert (handle, kind, flags) == (71, 1, 7)
        return descriptor

    monkeypatch.setattr(install, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(get_osfhandle=lambda fd: 71))
    monkeypatch.setitem(sys.modules, 'pywintypes', SimpleNamespace(error=OSError))
    monkeypatch.setitem(sys.modules, 'windows_workspace',
                        SimpleNamespace(identity=lambda handle, directory: ('file-id', handle)))
    monkeypatch.setitem(sys.modules, 'win32security', SimpleNamespace(
        OWNER_SECURITY_INFORMATION=1, GROUP_SECURITY_INFORMATION=2,
        DACL_SECURITY_INFORMATION=4, SE_FILE_OBJECT=1, GetSecurityInfo=get_security))


def test_security_descriptor_buffer_contract_preserves_entire_binary_snapshot(tmp_path, monkeypatch):
    descriptor = bytearray(b'owner\0group\0DACL\0\xff')
    _buffer_descriptor_platform(monkeypatch, descriptor)
    path = tmp_path / 'resource'
    path.write_bytes(b'same physical bytes')
    with path.open('rb') as handle:
        original = install._provider_security_identity(handle)
    assert original == (('file-id', 71), b'owner\0group\0DACL\0\xff')
    descriptor[0] = ord('O')
    assert original[1] == b'owner\0group\0DACL\0\xff'


@pytest.mark.parametrize('offset', [0, 6, 12], ids=['owner', 'group', 'dacl'])
def test_binary_security_change_refuses_unchanged_file_content(tmp_path, monkeypatch, offset):
    descriptor = bytearray(b'owner\0group\0DACL\0')
    _buffer_descriptor_platform(monkeypatch, descriptor)
    path = tmp_path / 'resource'
    path.write_bytes(b'same physical bytes')
    with path.open('rb') as handle:
        original = install._provider_security_identity(handle)
        descriptor[offset] ^= 1
        changed = install._provider_security_identity(handle)
    assert path.read_bytes() == b'same physical bytes'
    with pytest.raises(install.InstallControlError, match='snapshot_changed'):
        install._require_provider_identity(changed, original)


@pytest.mark.skipif(__import__('os').name != 'nt', reason='requires actual pywin32 security descriptor')
def test_native_security_descriptor_snapshot_uses_the_supported_buffer(tmp_path):
    import msvcrt

    import win32security

    path = tmp_path / 'resource'
    path.write_bytes(b'same physical bytes')
    flags = (win32security.OWNER_SECURITY_INFORMATION | win32security.GROUP_SECURITY_INFORMATION
             | win32security.DACL_SECURITY_INFORMATION)
    with path.open('rb') as handle:
        _identity, snapshot = install._provider_security_identity(handle)
        descriptor = win32security.GetSecurityInfo(
            msvcrt.get_osfhandle(handle.fileno()), win32security.SE_FILE_OBJECT, flags)
        assert snapshot == memoryview(descriptor).tobytes()
        assert snapshot == bytes(descriptor)


def _no_held_reader(_stack, _path):
    return None


def _shared_reader(stack, path):
    return stack.enter_context(install._provider_binary_file(path))


def _security_reader(stack, path):
    handle = install._provider_open(stack, path)
    install._provider_handle_identity(handle)
    return handle


@pytest.mark.parametrize('record', ['manifest', 'transaction', 'desired'])
@pytest.mark.parametrize('boundary', ['unheld', 'shared_reader', 'security_reader'])
def test_installed_authority_replacement_reaches_identity_check(
        tmp_path, monkeypatch, record, boundary):
    _root, state, _home, _resource = _installed(tmp_path)
    path = _proof_paths(state)[record]
    info = path.stat()
    details = (os.name, record, boundary, info.st_mode,
               getattr(info, 'st_file_attributes', None))
    assert info.st_mode & stat.S_IWRITE, details
    assert not getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_READONLY, details
    original = path.read_bytes()
    print("authority_attributes", repr(details))
    calls = _observe_create_file(monkeypatch)
    readers = {'unheld': _no_held_reader, 'shared_reader': _shared_reader,
               'security_reader': _security_reader}
    with ExitStack() as stack:
        handle = readers[boundary](stack, path)
        print("native_open_access_and_sharing", repr(calls))
        _same_bytes_replacement(path)
        assert path.read_bytes() == original
        if handle is not None:
            assert handle.read() == original
    assert path.read_bytes() == original


def _observe_create_file(monkeypatch):
    calls = []
    original = getattr(generation_catalog, '_create_file', None)
    if original is None:
        return calls

    def observed(*arguments):
        calls.append((arguments[1], arguments[2], arguments[4], arguments[5]))
        return original(*arguments)

    monkeypatch.setattr(generation_catalog, '_create_file', observed)
    return calls


def _close_native_stage_handle(handle):
    assert generation_catalog._close_handle(handle)


def _raw_native_reader(stack, path):
    handle = generation_catalog._windows_read_handle(path)
    stack.callback(_close_native_stage_handle, handle)


def _transferred_native_reader(stack, path):
    descriptor = generation_catalog._windows_read_descriptor(path)
    stack.callback(os.close, descriptor)


@pytest.mark.skipif(os.name != 'nt', reason='requires actual Windows raw HANDLE and CRT transfer')
@pytest.mark.parametrize('record', ['manifest', 'transaction', 'desired'])
@pytest.mark.parametrize('boundary', ['raw_handle', 'transferred_descriptor'])
def test_native_replacement_identifies_handle_transfer_boundary(
        tmp_path, monkeypatch, record, boundary):
    _root, state, _home, _resource = _installed(tmp_path)
    path = _proof_paths(state)[record]
    original = path.read_bytes()
    info = path.stat()
    print('authority_attributes', (record, boundary, info.st_mode, info.st_file_attributes))
    assert info.st_mode & stat.S_IWRITE
    assert not info.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    calls = _observe_create_file(monkeypatch)
    readers = {'raw_handle': _raw_native_reader,
               'transferred_descriptor': _transferred_native_reader}
    with ExitStack() as stack:
        readers[boundary](stack, path)
        print('native_open_access_and_sharing', repr(calls))
        _same_bytes_replacement(path)
        assert path.read_bytes() == original
    assert path.read_bytes() == original
