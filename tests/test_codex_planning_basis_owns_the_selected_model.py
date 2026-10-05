"""An attempt retains its actual model and executable without changing defaults."""
import hashlib
import json
import sys
import time
from dataclasses import replace

import llm_client as lc
import pytest

from tests.slow_machine import LONG_TIMEOUT


def _descriptor():
    return lc.provider_candidates('codex')[0]


def _basis(tmp_path, descriptor=None, files=()):
    descriptor = descriptor or _descriptor()
    binary = tmp_path / 'selected-codex'
    binary.write_bytes(b'owned executable')
    return lc.CodexPlanningBasis(
        descriptor, lc._bind_codex_executable(str(binary)), 'resolved-model',
        'openai', 258400, 'a' * 64, files,
        lc._codex_basis_digest(lc.provider_environment()), 'codex-cli 0.160.0',
    )


def test_prepared_call_does_not_reselect_a_planning_owned_executable(monkeypatch, tmp_path):
    basis = _basis(tmp_path)
    calls = []

    def reselect():
        calls.append(True)
        raise AssertionError('must retain the executable selected before packing')

    monkeypatch.setattr(lc, '_selected_codex_executable', reselect)
    prepared = lc._prepare_codex(basis.descriptor, 'complete prompt', 'system')
    assert prepared.executable == basis.executable
    assert calls == []
    assert prepared.model == basis.model
    assert prepared.basis is basis


def test_implicit_call_without_basis_keeps_its_unknown_model(monkeypatch, tmp_path):
    monkeypatch.delenv('MEMORY_CODEX_MODEL', raising=False)
    basis = _basis(tmp_path)
    monkeypatch.setattr(lc, '_selected_codex_executable', lambda: basis.executable)
    prepared = lc._prepare_codex(_descriptor(), 'prompt', '')
    assert prepared.model is None
    assert prepared.basis is None


@pytest.mark.parametrize('field,value', [('model', 'other-model'), ('provider', 'claude'),
                                       ('inference_settings', {'reasoning': 'high'})])
def test_changed_descriptor_is_refused(tmp_path, field, value):
    basis = _basis(tmp_path)
    descriptor = replace(basis.descriptor, **{field: value})
    with pytest.raises(RuntimeError, match='descriptor|model'):
        lc._prepare_codex(descriptor, 'prompt', '')


def test_environment_change_is_refused_before_dispatch(monkeypatch, tmp_path):
    basis = _basis(tmp_path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'changed-home'))
    with pytest.raises(RuntimeError, match='environment'):
        lc._prepare_codex(basis.descriptor, 'prompt', '')


def test_binary_change_is_refused_before_dispatch(tmp_path):
    basis = _basis(tmp_path)
    lc.Path(basis.executable.path).write_bytes(b'changed binary')
    with pytest.raises(RuntimeError, match='executable changed'):
        lc._prepare_codex(basis.descriptor, 'prompt', '')


def test_configuration_change_after_prepare_never_invokes_backend(monkeypatch, tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('model = "resolved-model"\n')
    files = ((str(config), lc._codex_basis_file_digest(config)),)
    basis = _basis(tmp_path, files=files)
    prepared = lc._prepare_codex(basis.descriptor, 'prompt', '')
    config.write_text('model = "changed-model"\n')
    invoked = []
    monkeypatch.setattr(lc, '_run_cli', lambda *a, **k: invoked.append(True))
    with pytest.raises(RuntimeError, match='configuration changed'):
        lc._call_prepared_codex(basis.descriptor, prepared)
    assert invoked == []


def _layer(path, document):
    encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return {'name': {'type': 'user', 'file': str(path)}, 'config': document,
            'version': 'sha256:' + hashlib.sha256(encoded).hexdigest()}


def test_loaded_layer_must_match_actual_config_bytes(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('model = "changed"\n')
    layer = _layer(path, {'model': 'old'})
    with pytest.raises(RuntimeError, match='configuration changed during'):
        lc._codex_config_layers_verified({'layers': [layer]})


def test_verified_layer_accepts_non_ascii_without_normalization(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('name = "е́"\n', encoding='utf-8')
    layer = _layer(path, {'name': 'е́'})
    assert lc._codex_config_layers_verified({'layers': [layer]})


@pytest.mark.parametrize('name', [{'type': 'mdm'}, {'type': 'enterpriseManaged'},
                                {'type': 'user', 'file': '/unknown', 'profile': 'custom'}])
def test_unverified_managed_layer_does_not_invent_capacity(name):
    assert not lc._codex_config_layers_verified({'layers': [{'name': name}]})


def test_catalog_capacity_is_only_matching_native_advertisement():
    row = {'slug': 'resolved-model', 'context_window': 272000,
           'effective_context_window_percent': 95}
    assert lc._codex_advertised_window([row], 'resolved-model', 'openai', 'codex-cli 0.160.0') == 258400
    assert lc._codex_advertised_window([row], 'other-model', 'openai', 'codex-cli 0.160.0') is None
    assert lc._codex_advertised_window([row], 'resolved-model', 'custom', 'codex-cli 0.160.0') is None
    assert lc._codex_advertised_window([row], 'resolved-model', 'openai', 'codex-cli unknown') is None


@pytest.mark.parametrize('deadline', [float('nan'), float('inf'), -1.0])
def test_invalid_or_expired_clock_never_resolves_binary(monkeypatch, deadline):
    resolved = []
    monkeypatch.setattr(lc, '_selected_codex_executable', lambda: resolved.append(True))
    with pytest.raises((ValueError, TimeoutError)):
        lc.resolve_codex_planning_basis(_descriptor(), deadline=deadline)
    assert resolved == []


def test_other_provider_remains_unknown_without_bootstrap():
    descriptor = lc.provider_candidates('fake')[0]
    assert lc.resolve_codex_planning_basis(descriptor, deadline=-1) is None


def test_real_owned_protocol_process_closes_without_a_turn(tmp_path):
    peer = "import json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n if 'id' in r: print(json.dumps({'id':r['id'],'result':{'accepted':r['method']}}),flush=True)\n"
    deadline = time.monotonic() + LONG_TIMEOUT
    rpc = lc._spawn_codex_basis_rpc([sys.executable, '-u', '-c', peer], tmp_path,
                                  lc.provider_environment(), deadline)
    result = rpc.request(1, 'initialize', {})
    rpc.close()
    assert result == {'accepted': 'initialize'}
    assert rpc.tree.process.returncode == 0
    assert not any(reader.is_alive() for reader in rpc.readers)


def test_expired_owned_rpc_still_settles_process_and_readers(tmp_path):
    peer = "import json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({'id':r['id'],'result':{}}),flush=True)\n"
    rpc = lc._spawn_codex_basis_rpc([sys.executable, '-u', '-c', peer], tmp_path,
                                  lc.provider_environment(), time.monotonic() + LONG_TIMEOUT)
    rpc.request(1, 'initialize', {})
    rpc.deadline = -1.0
    with pytest.raises(TimeoutError):
        rpc.close()
    assert rpc.tree.process.poll() is not None
    assert not any(reader.is_alive() for reader in rpc.readers)


def test_malformed_owned_rpc_does_not_become_a_basis(tmp_path):
    peer = "import sys\nfor line in sys.stdin: print('not JSON',flush=True)\n"
    rpc = lc._spawn_codex_basis_rpc([sys.executable, '-u', '-c', peer], tmp_path,
                                  lc.provider_environment(), time.monotonic() + LONG_TIMEOUT)
    try:
        with pytest.raises(ValueError):
            rpc.request(1, 'initialize', {})
    finally:
        rpc.close()
    assert rpc.tree.process.poll() is not None
    assert not any(reader.is_alive() for reader in rpc.readers)


def _native_peer():
    return "import json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n m=r['method']\n results={'initialize':{},'config/read':{'layers':[{'name':{'type':'sessionFlags'}}],'config':{}},'thread/start':{'model':'resolved-model','modelProvider':'openai','thread':{'id':'ephemeral','ephemeral':True}},'thread/unsubscribe':{}}\n if 'id' in r: print(json.dumps({'id':r['id'],'result':results[m]}),flush=True)\n"


def test_complete_resolver_pins_actual_response_only_once(monkeypatch, tmp_path):
    selected = _basis(tmp_path).executable
    selected_calls = []
    stages = []
    spawn = lc._spawn_codex_basis_rpc

    def select():
        selected_calls.append(True)
        return selected

    def owned_peer(command, cwd, environment, deadline):
        stages.append(tuple(command))
        return spawn([sys.executable, '-u', '-c', _native_peer()], cwd, environment, deadline)

    def local_command(executable, command, neutral, environment, deadline):
        stages.append(tuple(command))
        lc._codex_basis_remaining(deadline)
        return _local_catalog_reply(command)

    monkeypatch.setattr(lc, '_selected_codex_executable', select)
    monkeypatch.setattr(lc, '_spawn_codex_basis_rpc', owned_peer)
    monkeypatch.setattr(lc, '_codex_basis_local_command', local_command)
    basis = lc.resolve_codex_planning_basis(_descriptor(), deadline=time.monotonic() + LONG_TIMEOUT)
    prepared = lc._prepare_codex(basis.descriptor, 'unchanged user payload', '')
    assert basis.model == 'resolved-model'
    assert basis.planning_window == 258400
    assert prepared.executable == selected
    assert selected_calls == [True]
    assert len(stages) == 3
    assert 'model="resolved-model"' in stages[2]
    assert prepared.payload == lc._codex_prompt('', 'unchanged user payload')
    assert basis.descriptor.capabilities['max_tokens_enforced'] is False
    assert basis.descriptor.inference_settings['max_tokens'] == 'backend_default'


def _local_catalog_reply(command):
    if command[-1] == '--version':
        return b'codex-cli 0.160.0\n'
    return json.dumps({'models': [{'slug': 'resolved-model', 'context_window': 272000,
                                 'effective_context_window_percent': 95}]}).encode()


def test_explicit_model_disagreement_is_visible():
    descriptor = replace(_descriptor(), model='configured')
    reply = {'model': 'different', 'modelProvider': 'openai', 'thread': {'ephemeral': True}}
    with pytest.raises(RuntimeError, match='explicit configuration'):
        lc._require_codex_native_model(reply, descriptor)


def test_configured_window_does_not_claim_unqualified_effective_capacity():
    assert lc._codex_configured_window(258400, {'config': {'model_context_window': 100000}}) is None
    assert lc._codex_configured_window(258400, {'config': {}}) == 258400


def test_fallback_provenance_does_not_change_owned_provider_configuration(tmp_path):
    basis = _basis(tmp_path)
    descriptor = replace(basis.descriptor, fallback_from=('opencode:unavailable',))
    prepared = lc._prepare_codex(descriptor, 'prompt', '')
    assert prepared.model == basis.model
    assert prepared.executable == basis.executable
    assert descriptor.fallback_from == ('opencode:unavailable',)


def test_basis_is_transient_in_existing_descriptor_json(tmp_path):
    basis = _basis(tmp_path)
    payload = basis.descriptor.canonical()
    assert payload['model'] == basis.model
    assert set(payload) == {'provider', 'model', 'capabilities', 'inference_settings',
                            'candidate_index', 'fallback_from'}


def test_parsed_config_float_does_not_depend_on_rust_python_json_spelling(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('temperature = 0.0000001\n')
    layer = _layer(path, {'temperature': 1e-7})
    layer['version'] = 'sha256:' + 'b' * 64
    assert lc._codex_config_layers_verified({'layers': [layer]})


def test_unknown_config_version_does_not_invent_capacity(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('model = "known"\n')
    layer = _layer(path, {'model': 'known'})
    layer['version'] = 'unknown version'
    assert not lc._codex_config_layers_verified({'layers': [layer]})
