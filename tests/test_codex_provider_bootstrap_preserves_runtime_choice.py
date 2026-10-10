"""The whole provider bundle falls back only for a host with no explicit key."""
import base64
import json
import subprocess
import sys

import integration_hook_config as hooks
import pytest

from tests.test_codex_installed_provider_bundle_reaches_launch import (
    BUNDLE,
    _host_environment,
    _script,
)


def _launch_result(root, env, payload=None):
    _script(root, 'mcp_server.py')
    args = hooks.codex_launch_args(root, 'mcp_server.py', (), BUNDLE)
    if payload is not None:
        args[8] = payload
    return subprocess.run([sys.executable, *args[6:]], cwd=root, env=env,
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize('key', hooks.PROVIDER_ENV_KEYS)
@pytest.mark.parametrize('value', ['', 'explicit-host-value'])
def test_any_explicit_provider_key_preserves_whole_host_environment(tmp_path, key, value):
    env = _host_environment()
    env[key] = value
    result = _launch_result(tmp_path, env)
    assert result.returncode == 0
    observed = json.loads(result.stdout)
    assert observed == {key: value}


def test_multiple_explicit_keys_are_not_completed_from_install_choice(tmp_path):
    env = dict(_host_environment(), MEMORY_LLM_PROVIDER='ollama', MEMORY_CODEX_MODEL='')
    result = _launch_result(tmp_path, env)
    assert json.loads(result.stdout) == {'MEMORY_LLM_PROVIDER': 'ollama', 'MEMORY_CODEX_MODEL': ''}


@pytest.mark.parametrize('value', [
    {'OPENAI_API_KEY': 'test-only-not-a-secret'}, {'MEMORY_CODEX_MODEL': 1},
    {'MEMORY_CODEX_MODEL': False}, {'MEMORY_LLM_PROVIDER': 'fake'}, [],
])
def test_malformed_or_forbidden_payload_refuses_before_target_execution(tmp_path, value):
    payload = base64.b64encode(json.dumps(value).encode()).decode()
    result = _launch_result(tmp_path, _host_environment(), payload)
    assert result.returncode != 0
    assert result.stdout == ''


@pytest.mark.parametrize('payload', ['not base64!', 'e30=\n',
    base64.b64encode(b'{"MEMORY_CODEX_MODEL":"first","MEMORY_CODEX_MODEL":"second"}').decode()])
def test_noncanonical_transport_is_not_accepted(tmp_path, payload):
    result = _launch_result(tmp_path, _host_environment(), payload)
    assert result.returncode != 0
    assert result.stdout == ''


def test_fake_and_secret_values_are_not_persisted(monkeypatch):
    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-not-a-secret')
    assert hooks.provider_environment() == {}
    assert hooks.codex_provider_from_payload(hooks.codex_provider_payload({})) == {}


def test_python_optimization_cannot_disable_payload_validation(tmp_path):
    env = dict(_host_environment(), PYTHONOPTIMIZE='1')
    payload = base64.b64encode(b'{"OPENAI_API_KEY":"not-permitted"}').decode()
    result = _launch_result(tmp_path, env, payload)
    assert result.returncode != 0
    assert result.stdout == ''


def _canonical_payload(bundle):
    return base64.b64encode(json.dumps(bundle, sort_keys=True, separators=(',', ':'),
                                       ensure_ascii=True).encode()).decode()


@pytest.mark.parametrize('selector', [' fake ', '\tFAKE\r\n', '\u2003fAkE\u2003'])
def test_runtime_normalized_fake_is_not_persistable(selector, monkeypatch):
    import llm_client

    monkeypatch.setenv('MEMORY_LLM_PROVIDER', selector)
    assert llm_client.forced_provider() == 'fake'
    with pytest.raises(ValueError, match='test provider'):
        hooks.codex_provider_payload({'MEMORY_LLM_PROVIDER': selector})
    with pytest.raises(ValueError, match='test provider'):
        hooks.codex_provider_from_payload(_canonical_payload({'MEMORY_LLM_PROVIDER': selector}))


@pytest.mark.parametrize('selector', [' fake ', '\tFAKE\r\n', '\u2003fAkE\u2003'])
def test_emitted_bootstrap_refuses_runtime_normalized_fake_before_target(tmp_path, selector):
    payload = _canonical_payload({'MEMORY_LLM_PROVIDER': selector})
    result = _launch_result(tmp_path, _host_environment(), payload)
    assert result.returncode != 0
    assert result.stdout == ''


def test_nonfake_bundle_values_keep_their_exact_original_bytes(tmp_path):
    bundle = {'MEMORY_LLM_PROVIDER': ' Codex ', 'MEMORY_CODEX_MODEL': ' model ',
              'MEMORY_CODEX_REASONING': ''}
    payload = hooks.codex_provider_payload(bundle)
    assert hooks.codex_provider_from_payload(payload) == bundle
    result = _launch_result(tmp_path, _host_environment(), payload)
    assert result.returncode == 0
    assert json.loads(result.stdout) == bundle
