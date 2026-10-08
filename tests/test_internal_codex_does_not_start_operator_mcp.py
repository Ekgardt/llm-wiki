"""Closed memory packets must not start the operator's configured MCP servers."""
import json
import shutil
import subprocess
from dataclasses import replace

import llm_client as lc
import pytest


class RecordingRpc:
    def __init__(self):
        self.started = None

    def send(self, method, params):
        return None

    def request(self, identifier, method, params):
        replies = {
            'initialize': {},
            'config/read': {'layers': [], 'config': {'mcp_servers': {
                'memory': {'command': 'unused'}, 'other.server': {'enabled': True},
            }}},
            'thread/start': {'model': 'model', 'modelProvider': 'openai',
                             'thread': {'id': 'temporary', 'ephemeral': True}},
            'thread/unsubscribe': {},
        }
        if method == 'thread/start':
            self.started = params
        return replies[method]


def test_model_discovery_disables_every_effective_mcp_before_starting_thread():
    rpc = RecordingRpc()
    descriptor = replace(lc.provider_candidates('codex')[0], model='model')
    lc._codex_native_basis(rpc, '/neutral', descriptor)
    assert rpc.started.get('config', {}).get('mcp_servers') == {
        'memory': {'enabled': False}, 'other.server': {'enabled': False},
    }


def test_internal_exec_is_ephemeral_and_does_not_load_apps_or_plugins():
    command = lc._codex_command('codex', 'model', 'medium', '/result')
    assert '--ephemeral' in command
    assert 'features.apps=false' in command
    assert 'features.plugins=false' in command


def _configuration_arguments(command):
    return [part for index, value in enumerate(command[:-1]) if value == '-c'
            for part in ('-c', command[index + 1])]


def test_native_exec_disables_all_operator_servers_without_editing_configuration(monkeypatch, tmp_path):
    binary = shutil.which('codex')
    if binary is None:
        pytest.skip('native Codex CLI is not installed')
    home = tmp_path / 'codex-home'
    home.mkdir()
    config = home / 'config.toml'
    original = '[mcp_servers.memory]\ncommand="false"\n[mcp_servers."other.server"]\ncommand="false"\n'
    config.write_text(original)
    monkeypatch.setenv('CODEX_HOME', str(home))
    output = tmp_path / 'reply'
    output.write_text('ok')
    prompt = tmp_path / 'prompt'
    prompt.write_text('closed packet')
    observed = []

    def inspect_native_configuration(command, **options):
        probe = [binary, *_configuration_arguments(command), 'mcp', 'list', '--json']
        result = subprocess.run(probe, capture_output=True, cwd=options['cwd'],
                                env=options['env'], timeout=options.get('timeout', lc._timeout_s()), check=True)
        observed.extend(json.loads(result.stdout))
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(lc, '_run_cli', inspect_native_configuration)
    executable = lc._bind_codex_executable(binary)
    command = lc._codex_command(binary, None, 'medium', str(output))
    assert lc._codex_last_message(command, str(prompt), str(output), executable=executable).text == 'ok'
    assert {server['name'] for server in observed} == {'memory', 'other.server'}
    assert all(server['enabled'] is False for server in observed)
    assert config.read_text() == original


@pytest.mark.parametrize('raw', ['{}', '[null]', '[{"name":""}]',
                               '[{"name":"a"},{"name":"a"}]'])
def test_malformed_mcp_discovery_is_not_treated_as_no_servers(raw):
    with pytest.raises(ValueError):
        lc._codex_mcp_list_names(raw)


@pytest.mark.parametrize('servers', [None, [], {'': {}}, {'a': None}])
def test_malformed_native_configuration_blocks_model_thread(servers):
    with pytest.raises(ValueError):
        lc._codex_mcp_names({'config': {'mcp_servers': servers}})


def _expired_discovery(*args):
    raise TimeoutError('Codex model-resolution deadline expired')


def test_discovery_deadline_exhaustion_stops_the_provider_chain(monkeypatch, tmp_path):
    binary = tmp_path / 'owned-codex'
    binary.write_bytes(b'owned CLI fixture')
    prompt = tmp_path / 'prompt'
    prompt.write_text('closed packet')
    executable = lc._bind_codex_executable(str(binary))
    monkeypatch.setattr(lc, '_codex_basis_local_command', _expired_discovery)
    with pytest.raises(lc.ProviderTimeout):
        lc._codex_last_message([str(binary)], str(prompt), str(tmp_path / 'reply'), executable=executable)
