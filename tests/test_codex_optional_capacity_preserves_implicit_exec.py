"""Missing optional discovery cannot disable an otherwise supported CLI."""
import subprocess
import sys
import time

import llm_client as lc
import pytest

from tests.slow_machine import LONG_TIMEOUT


def _selected(monkeypatch, tmp_path):
    path = tmp_path / 'codex'
    path.write_bytes(b'owned binary')
    executable = lc._bind_codex_executable(str(path))
    monkeypatch.setattr(lc, '_selected_codex_executable', lambda: executable)
    return executable


def test_unknown_version_keeps_implicit_exec_without_native_bootstrap(monkeypatch, tmp_path):
    _selected(monkeypatch, tmp_path)
    spawned = []
    monkeypatch.setattr(lc, '_codex_basis_local_command', lambda *args: b'codex-cli 0.159.0\n')
    monkeypatch.setattr(lc, '_spawn_codex_basis_rpc', lambda *args: spawned.append(True))
    descriptor = lc.provider_candidates('codex')[0]
    assert lc.resolve_codex_planning_basis(descriptor, deadline=time.monotonic() + LONG_TIMEOUT) is None
    assert spawned == []
    assert descriptor.model is None


def _unsupported_peer():
    return "import json,sys\nfor line in sys.stdin:\n r=json.loads(line)\n if 'id' in r: print(json.dumps({'id':r['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True)\n"


def test_unsupported_native_method_is_unknown_only_after_owned_cleanup(monkeypatch, tmp_path):
    _selected(monkeypatch, tmp_path)
    monkeypatch.setattr(lc, '_codex_basis_local_command', lambda *args: b'codex-cli 0.160.0\n')
    spawn = lc._spawn_codex_basis_rpc
    owned = []

    def peer(command, cwd, environment, deadline):
        rpc = spawn([sys.executable, '-u', '-c', _unsupported_peer()], cwd, environment, deadline)
        owned.append(rpc)
        return rpc

    monkeypatch.setattr(lc, '_spawn_codex_basis_rpc', peer)
    descriptor = lc.provider_candidates('codex')[0]
    assert lc.resolve_codex_planning_basis(descriptor, deadline=time.monotonic() + LONG_TIMEOUT) is None
    assert owned[0].tree.process.poll() is not None
    assert not any(reader.is_alive() for reader in owned[0].readers)


@pytest.mark.parametrize('reply', [
    {'error': {'code': -32602, 'message': 'Invalid params'}},
    {'error': {'code': -32603, 'message': 'Internal error'}},
    {'error': {'code': '-32601', 'message': 'Method not found'}},
    {'error': {'code': -32601}},
    {'error': {'code': -32601, 'message': 'Method not found'}, 'result': {}},
])
def test_other_or_malformed_native_errors_never_downgrade(reply):
    with pytest.raises((RuntimeError, ValueError)) as caught:
        lc._codex_basis_reply_result(reply)
    assert type(caught.value).__name__ != '_CodexCapabilityUnavailable'


def test_catalog_parser_capability_failure_retains_model_and_binary(monkeypatch, tmp_path):
    executable = _selected(monkeypatch, tmp_path)
    environment = lc.provider_environment()
    result = {'model': 'resolved-model', 'modelProvider': 'openai'}
    unsupported = subprocess.CompletedProcess([], 2, b'', b"error: unrecognized subcommand 'models'\n")
    monkeypatch.setattr(lc, '_codex_basis_command_result', lambda *args: unsupported, raising=False)
    descriptor = lc.provider_candidates('codex')[0]
    basis = lc._resolved_codex_basis(descriptor, executable, result, {'layers': [], 'config': {}},
                                    str(tmp_path), environment, time.monotonic() + LONG_TIMEOUT,
                                    version='codex-cli 0.160.0')
    assert basis.model == 'resolved-model'
    assert basis.executable == executable
    assert basis.planning_window is None


def test_absent_catalog_still_checks_loaded_configuration(monkeypatch, tmp_path):
    executable = _selected(monkeypatch, tmp_path)
    path = tmp_path / 'config.toml'
    path.write_text('model = "changed"\n')
    config = {'layers': [{'name': {'file': str(path)}, 'config': {'model': 'old'},
                          'version': 'sha256:' + 'a' * 64}], 'config': {}}
    unsupported = subprocess.CompletedProcess([], 2, b'', b"error: unrecognized subcommand 'models'\n")
    monkeypatch.setattr(lc, '_codex_basis_command_result', lambda *args: unsupported, raising=False)
    descriptor = lc.provider_candidates('codex')[0]
    result = {'model': 'resolved-model', 'modelProvider': 'openai'}
    with pytest.raises(RuntimeError, match='configuration changed'):
        lc._resolved_codex_basis(descriptor, executable, result, config, str(tmp_path),
                                 lc.provider_environment(), time.monotonic() + LONG_TIMEOUT,
                                 version='codex-cli 0.160.0')


def test_unknown_layer_does_not_hide_changed_loaded_file(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('model = "changed"\n')
    unknown = {'name': {'type': 'enterpriseManaged'}}
    changed = {'name': {'file': str(path)}, 'config': {'model': 'old'},
               'version': 'sha256:' + 'a' * 64}
    with pytest.raises(RuntimeError, match='configuration changed'):
        lc._codex_config_layers_verified({'layers': [unknown, changed]})


@pytest.mark.parametrize('code,stderr', [
    (1, b"error: unrecognized subcommand 'models'\n"),
    (2, b'authentication failed\n'),
    (-9, b"error: unrecognized subcommand 'models'\n"),
])
def test_catalog_operational_failures_are_not_capability_absence(code, stderr):
    result = subprocess.CompletedProcess([], code, b'', stderr)
    assert not lc._codex_catalog_command_unsupported(['codex', 'debug', 'models'], result)


def test_boolean_reply_id_is_protocol_corruption():
    with pytest.raises(ValueError, match='identifier'):
        lc._codex_basis_reply_matches({'id': True}, 1)


def test_unavailable_method_does_not_hide_binary_drift(monkeypatch, tmp_path):
    executable = _selected(monkeypatch, tmp_path)
    monkeypatch.setattr(lc, '_codex_basis_local_command', lambda *args: b'codex-cli 0.160.0\n')
    spawn = lc._spawn_codex_basis_rpc
    owned = []

    def peer(command, cwd, environment, deadline):
        rpc = spawn([sys.executable, '-u', '-c', _unsupported_peer()], cwd, environment, deadline)
        lc.Path(executable.path).write_bytes(b'changed binary')
        owned.append(rpc)
        return rpc

    monkeypatch.setattr(lc, '_spawn_codex_basis_rpc', peer)
    with pytest.raises(RuntimeError, match='executable changed'):
        lc.resolve_codex_planning_basis(lc.provider_candidates('codex')[0],
                                        deadline=time.monotonic() + LONG_TIMEOUT)
    assert owned[0].tree.process.poll() is not None


def test_cleanup_failure_cannot_become_unknown(monkeypatch):
    class Rpc:
        def close(self):
            raise RuntimeError('cleanup unverified')

    def unavailable(*args):
        raise lc._CodexCapabilityUnavailable('unavailable')

    monkeypatch.setattr(lc, '_codex_native_basis', unavailable)
    with pytest.raises(RuntimeError, match='cleanup unverified'):
        lc._codex_optional_native_basis(Rpc(), '/neutral', lc.provider_candidates('codex')[0])


def test_timeout_cannot_become_unknown(monkeypatch):
    closed = []

    class Rpc:
        def close(self):
            closed.append(True)

    def expired(*args):
        raise TimeoutError('expired')

    monkeypatch.setattr(lc, '_codex_native_basis', expired)
    with pytest.raises(TimeoutError):
        lc._codex_optional_native_basis(Rpc(), '/neutral', lc.provider_candidates('codex')[0])
    assert closed == [True]


def test_unknown_config_does_not_suppress_corrupt_successful_catalog():
    result = {'model': 'resolved-model', 'modelProvider': 'openai'}
    with pytest.raises(ValueError):
        lc._codex_verified_catalog_window(b'not JSON', result, 'codex-cli 0.160.0', False)


def test_unavailable_after_config_read_still_rechecks_loaded_file(monkeypatch, tmp_path):
    _selected(monkeypatch, tmp_path)
    path = tmp_path / 'config.toml'
    path.write_text('model = "old"\n')
    config = {'layers': [{'name': {'file': str(path)}, 'config': {'model': 'old'},
                          'version': 'sha256:' + 'a' * 64}]}
    monkeypatch.setattr(lc, '_codex_basis_local_command', lambda *args: b'codex-cli 0.160.0\n')
    spawn = lc._spawn_codex_basis_rpc

    def peer(command, cwd, environment, deadline):
        return spawn([sys.executable, '-u', '-c', _unsupported_peer()], cwd, environment, deadline)

    def changed(rpc, neutral, descriptor):
        rpc.config = config
        rpc.config_files = lc._codex_config_files(config)
        path.write_text('model = "changed"\n')
        raise lc._CodexCapabilityUnavailable('unsupported thread/start')

    monkeypatch.setattr(lc, '_spawn_codex_basis_rpc', peer)
    monkeypatch.setattr(lc, '_codex_native_basis', changed)
    with pytest.raises(RuntimeError, match='configuration changed'):
        lc.resolve_codex_planning_basis(lc.provider_candidates('codex')[0],
                                        deadline=time.monotonic() + LONG_TIMEOUT)


def test_actual_catalog_parser_refusal_does_not_disable_resolved_model(monkeypatch, tmp_path):
    import sync_memory

    executable = _selected(monkeypatch, tmp_path)

    def command_result(command, **kwargs):
        if command[-1] == '--version':
            return subprocess.CompletedProcess(command, 0, b'codex-cli 0.160.0\n', b'')
        return subprocess.CompletedProcess(command, 2, b'', b"error: unrecognized subcommand 'models'\n")

    monkeypatch.setattr(sync_memory, '_run_process_tree', command_result)
    result = {'model': 'resolved-model', 'modelProvider': 'openai'}
    basis = lc._resolved_codex_basis(lc.provider_candidates('codex')[0], executable, result,
                                    {'layers': [], 'config': {}}, str(tmp_path),
                                    lc.provider_environment(), time.monotonic() + LONG_TIMEOUT)
    assert basis.model == 'resolved-model'
    assert basis.planning_window is None


def test_unavailable_method_followed_by_failed_exit_is_not_unknown(monkeypatch, tmp_path):
    _selected(monkeypatch, tmp_path)
    monkeypatch.setattr(lc, '_codex_basis_local_command', lambda *args: b'codex-cli 0.160.0\n')
    spawn = lc._spawn_codex_basis_rpc

    def peer(command, cwd, environment, deadline):
        program = _unsupported_peer() + 'sys.exit(1)\n'
        return spawn([sys.executable, '-u', '-c', program], cwd, environment, deadline)

    monkeypatch.setattr(lc, '_spawn_codex_basis_rpc', peer)
    with pytest.raises(lc.ProviderExited):
        lc.resolve_codex_planning_basis(lc.provider_candidates('codex')[0],
                                        deadline=time.monotonic() + LONG_TIMEOUT)
