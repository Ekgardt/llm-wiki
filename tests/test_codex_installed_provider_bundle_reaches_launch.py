"""The installed nonsecret choice reaches an old host without mixing overrides."""
import json
import os
import shlex
import subprocess
import sys

import codex_memory
import integration_hook_config as hooks
import pytest

BUNDLE = {'MEMORY_LLM_PROVIDER': 'codex', 'MEMORY_CODEX_MODEL': 'gpt-6-luna',
          'MEMORY_CODEX_REASONING': 'max'}


def _installed_choice(monkeypatch):
    for key in hooks.PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in BUNDLE.items():
        monkeypatch.setenv(key, value)


def _script(root, name):
    path = root / 'scripts' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('import json,os\n' + f'KEYS={hooks.PROVIDER_ENV_KEYS!r}\n' +
                    'print(json.dumps({k:v for k,v in os.environ.items() if k in KEYS}))\n',
                    encoding='utf-8')
    return path


def _host_environment():
    env = dict(os.environ)
    for key in hooks.PROVIDER_ENV_KEYS:
        env.pop(key, None)
    return env


def _python_result(args, root, env):
    index = args.index('python')
    result = subprocess.run([sys.executable, *args[index + 1:]], cwd=root,
                            env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_mcp_normal_render_carries_whole_install_choice_into_old_host(tmp_path, monkeypatch):
    _installed_choice(monkeypatch)
    _script(tmp_path, 'mcp_server.py')
    args = codex_memory._mcp_expected_args(tmp_path)
    assert _python_result(args, tmp_path, _host_environment()) == BUNDLE


def test_owned_hook_normal_render_carries_whole_install_choice_into_old_host(tmp_path, monkeypatch):
    _installed_choice(monkeypatch)
    target = _script(tmp_path, 'codex_memory.py')
    template = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command',
                'command': shlex.join(['uv', 'run', '--locked', '--no-sync', '--directory',
                                      str(tmp_path), 'python', str(target), 'hook'])}]}]}}
    resource = hooks.codex_hooks_resource(tmp_path / 'hooks.json', template)
    wanted = json.loads(resource.desired)['hooks']['SessionStart'][0]['hooks'][0]['command']
    assert _python_result(shlex.split(wanted), tmp_path, _host_environment()) == BUNDLE


def _parity_script(root):
    path = root / 'scripts/mcp_server.py'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('import json,sys,builtins\nfrom local_probe import VALUE\n'
                    'print(json.dumps({"argv":sys.argv,"path":sys.path,"file":__file__,"name":__name__, '
                    '"stdin":sys.stdin.buffer.read().hex(),"local":VALUE, "loader":[type(__loader__).__name__,__loader__.name,__loader__.path], "package":__package__,"spec":__spec__,"cached":globals().get("__cached__","ABSENT"), "builtins":__builtins__ is builtins,"main":sys.modules["__main__"].__dict__ is globals()}))\n', encoding='utf-8')
    (path.parent / 'local_probe.py').write_text('VALUE="Café Ω"\n', encoding='utf-8')
    return path


def _parity_result(args, root):
    result = subprocess.run([sys.executable, *args], cwd=root, env=_host_environment(),
                            input=b'CRLF\r\n\x00\xce\xa9', capture_output=True, check=True)
    return json.loads(result.stdout)


def test_relative_target_preserves_direct_argv_file_stdin_and_local_imports(tmp_path):
    _parity_script(tmp_path)
    tail = ['space value', 'quote"value', 'shell;$value']
    direct = _parity_result(['scripts/mcp_server.py', *tail], tmp_path)
    args = hooks.codex_launch_args(tmp_path, 'mcp_server.py', tail, {}, relative_target=True)
    wrapped = _parity_result(args[6:], tmp_path)
    assert wrapped == direct


def test_absolute_target_preserves_direct_argv_file_stdin_and_local_imports(tmp_path):
    target = _parity_script(tmp_path)
    tail = ['space value', 'quote"value', 'shell;$value']
    direct = _parity_result([str(target), *tail], tmp_path)
    args = hooks.codex_launch_args(tmp_path, 'mcp_server.py', tail, {})
    wrapped = _parity_result(args[6:], tmp_path)
    assert wrapped == direct


def test_target_stdout_stderr_and_system_exit_are_preserved(tmp_path):
    path = _script(tmp_path, 'mcp_server.py')
    path.write_bytes(b'import sys\nsys.stdout.buffer.write(b"output\\r\\n")\n'
                     b'sys.stderr.buffer.write(b"error\\r\\n")\nraise SystemExit(7)\n')
    direct = subprocess.run([sys.executable, 'scripts/mcp_server.py'], cwd=tmp_path,
                            env=_host_environment(), capture_output=True, check=False)
    args = hooks.codex_launch_args(tmp_path, 'mcp_server.py', (), {}, relative_target=True)
    wrapped = subprocess.run([sys.executable, *args[6:]], cwd=tmp_path,
                             env=_host_environment(), capture_output=True, check=False)
    assert (wrapped.returncode, wrapped.stdout, wrapped.stderr) == (
        direct.returncode, direct.stdout, direct.stderr)


def test_target_executes_in_the_original_python_process(tmp_path):
    path = _script(tmp_path, 'mcp_server.py')
    path.write_text('import os\nprint(os.getpid())\n', encoding='utf-8')
    args = hooks.codex_launch_args(tmp_path, 'mcp_server.py', (), {})
    process = subprocess.Popen([sys.executable, *args[6:]], cwd=tmp_path,
                               env=_host_environment(), stdout=subprocess.PIPE)
    output, _error = process.communicate()
    assert (process.returncode, int(output)) == (0, process.pid)


def test_windows_form_quotes_each_argument_without_changing_pair_binding(tmp_path):
    root = tmp_path / 'Café & Ω vault'
    args = ['uv', *hooks.codex_launch_args(root, 'codex_memory.py', ['hook'], BUNDLE)]
    command = hooks._codex_windows_command(args)
    assert command.startswith('uv "run" "--locked" "--no-sync" "--directory" ')
    assert '"-c" "exec(__import__' in command
    actual_root, actual_bundle = hooks.codex_command_launch(command)
    assert (actual_root, actual_bundle) == (root.resolve(), BUNDLE)
    from codex_hook_identity import is_our_codex_command

    assert is_our_codex_command(command)


@pytest.mark.skipif(os.name != 'nt', reason='requires real Windows CommandLineToArgvW')
@pytest.mark.parametrize('argument', ['', 'a&b', 'C:\\vault\\', 'quoted"value', 'Café Ω'])
def test_windows_native_argv_roundtrip_keeps_exact_quoted_values(argument):
    import win32api

    command = hooks._codex_windows_command(['uv', argument])
    assert win32api.CommandLineToArgv(command) == ['uv', argument]


@pytest.mark.skipif(os.name != 'nt', reason='requires real Codex default CMD shell semantics')
def test_windows_default_cmd_executes_full_bootstrap_with_literal_argv_and_stdin(tmp_path):
    root = tmp_path / 'Café & Ω vault'
    _parity_script(root)
    env = dict(_host_environment(), UV_PYTHON=sys.executable)
    args = ['uv', *hooks.codex_launch_args(root, 'mcp_server.py', ['literal value'], {}, relative_target=True)]
    rendered = hooks._codex_windows_command(args)
    command = subprocess.list2cmdline([env['COMSPEC'], '/C']) + ' "' + rendered + '"'
    result = subprocess.run(command, cwd=root, env=env, input=b'CRLF\r\n\x00\xce\xa9',
                            capture_output=True, check=False)
    direct = _parity_result(['scripts/mcp_server.py', 'literal value'], root)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    assert json.loads(result.stdout) == direct


def _path_result(root, arguments, environment):
    return subprocess.run([sys.executable, *arguments], cwd=root, env=environment,
                          capture_output=True, check=True).stdout


def test_existing_isolated_python_import_policy_is_not_relaxed(tmp_path):
    target = _script(tmp_path, 'mcp_server.py')
    target.write_text('import json,sys\nprint(json.dumps(sys.path))\n', encoding='utf-8')
    args = hooks.codex_launch_args(tmp_path, 'mcp_server.py', (), {}, relative_target=True)
    direct = _path_result(tmp_path, ['-I', 'scripts/mcp_server.py'], _host_environment())
    wrapped = _path_result(tmp_path, ['-I', *args[6:]], _host_environment())
    assert wrapped == direct


@pytest.mark.skipif(not hasattr(sys.flags, 'safe_path'), reason='PYTHONSAFEPATH requires Python 3.11+')
def test_existing_safe_path_environment_is_not_relaxed(tmp_path):
    target = _script(tmp_path, 'mcp_server.py')
    target.write_text('import json,sys\nprint(json.dumps(sys.path))\n', encoding='utf-8')
    args = hooks.codex_launch_args(tmp_path, 'mcp_server.py', (), {}, relative_target=True)
    environment = dict(_host_environment(), PYTHONSAFEPATH='1')
    direct = _path_result(tmp_path, ['scripts/mcp_server.py'], environment)
    wrapped = _path_result(tmp_path, args[6:], environment)
    assert wrapped == direct


def _configured_powershell_result(shell, root, bundle, *, arguments=None):
    import shutil

    executable = shutil.which(shell)
    assert executable is not None, shell
    env = dict(_host_environment(), UV_PYTHON=sys.executable)
    args = arguments
    if args is None:
        args = ['uv', *hooks.codex_launch_args(root, 'mcp_server.py', ['literal value'], bundle,
                                             relative_target=True)]
    rendered = hooks._codex_windows_command(args)
    # Codex 0.160 configured-shell branch passes the complete command as one arg.
    return subprocess.run([executable, '-NoProfile', '-NonInteractive', '-Command', rendered],
                          cwd=root, env=env, input=b'CRLF\r\n\x00\xce\xa9',
                          capture_output=True, check=False)


@pytest.mark.skipif(os.name != 'nt', reason='requires actual configured Windows PowerShell shell')
@pytest.mark.parametrize('shell', ['pwsh', 'powershell.exe'])
def test_native_configured_powershell_keeps_direct_script_parity_and_provider_bundle(tmp_path, shell):
    root = tmp_path / 'Café & Ω vault'
    _parity_script(root)
    result = _configured_powershell_result(shell, root, {})
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    assert json.loads(result.stdout) == _parity_result(['scripts/mcp_server.py', 'literal value'], root)
    _script(root, 'mcp_server.py')
    provider = _configured_powershell_result(shell, root, BUNDLE)
    assert provider.returncode == 0, provider.stderr.decode('utf-8', errors='replace')
    assert json.loads(provider.stdout) == BUNDLE
    (root / 'scripts/mcp_server.py').write_bytes(b'import sys\n'
        b'sys.stdout.buffer.write(b"output\\r\\n")\n'
        b'sys.stderr.buffer.write(b"error\\r\\n")\nraise SystemExit(7)\n')
    ended = _configured_powershell_result(shell, root, {})
    direct_args = ['uv', 'run', '--locked', '--no-sync', '--directory', str(root),
                   'python', 'scripts/mcp_server.py', 'literal value']
    direct = _configured_powershell_result(shell, root, {}, arguments=direct_args)
    assert (ended.returncode, ended.stdout, ended.stderr) == (
        direct.returncode, direct.stdout, direct.stderr)
    assert (ended.stdout, ended.stderr) == (b'output\r\n', b'error\r\n')
