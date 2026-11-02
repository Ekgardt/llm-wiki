"""ACL policy at a faithful OS boundary; native Windows remains separate."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from llm_wiki import windows_acl as acl

OWNER = 'PC\\Оператор'


@pytest.mark.parametrize(('listing', 'accepted'), [
    ([f'artifact {OWNER}:(F)'], True), ([f'artifact {OWNER}:(I)(F)'], True),
    ([f'artifact {OWNER}:(R)'], False), ([], False),
    ([f'artifact {OWNER}:(F)', 'PC\\Гость:(R)'], False),
    ([f'artifact {OWNER}:(F)', f'{OWNER}:(F)'], False),
    ([f'artifact {OWNER}-other:(F)'], False),
])
def test_boolean_queue_policy_is_preserved(listing, accepted):
    assert acl._acl_is_owner_only(listing, OWNER) is accepted


@pytest.mark.parametrize('listing', [f'artifact {OWNER}:(I)(F)', f'artifact {OWNER}:(R)',
                                     f'artifact {OWNER}:(F)\nPC\\Гость:(R)', ''])
def test_strict_verifier_keeps_its_stronger_explicit_acl_contract(listing):
    result = subprocess.CompletedProcess(['icacls'], 0, listing, '')
    with pytest.raises(PermissionError, match='owner-only ACL'):
        acl._require_verified_windows_owner_acl(Path('artifact'), result, OWNER)


def test_strict_verifier_accepts_one_explicit_full_owner_entry():
    result = subprocess.CompletedProcess(['icacls'], 0, f'artifact {OWNER}:(F)', '')
    acl._require_verified_windows_owner_acl(Path('artifact'), result, OWNER)


def test_subprocess_contract_and_arguments_are_unchanged(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr(acl.subprocess, 'run', run)
    command = ['icacls', 'artifact']
    acl._run_acl_command(command)
    assert calls == [(command, {'check': False, 'capture_output': True, 'timeout': 5})]


@pytest.mark.parametrize('failure', [OSError('missing'), subprocess.TimeoutExpired('icacls', 5)])
def test_command_errors_refuse_strict_read_and_boolean_check(monkeypatch, failure):
    def fail(_command):
        raise failure

    monkeypatch.setattr(acl, '_run_acl_command', fail)
    assert acl._windows_acl_lines(Path('artifact'), OWNER) is None
    with pytest.raises(PermissionError, match='could not verify owner-only ACL'):
        acl._read_windows_acl(Path('artifact'))


def test_nonzero_exit_refuses_both_read_and_write(monkeypatch):
    monkeypatch.setattr(acl, '_run_acl_command', lambda cmd: subprocess.CompletedProcess(cmd, 5, b'', b'denied'))
    assert acl._windows_acl_lines(Path('artifact'), OWNER) is None
    with pytest.raises(PermissionError, match='denied'):
        acl._apply_windows_acl(Path('artifact'), OWNER + ':(F)')


def test_console_codec_falls_back_when_unknown(monkeypatch):
    kernel = SimpleNamespace(GetConsoleOutputCP=lambda: 999999)
    monkeypatch.setattr(acl, '_windows_kernel32', lambda: kernel)
    monkeypatch.setattr(acl, 'sys', SimpleNamespace(platform='win32'))
    assert acl._console_code_page() == ()
    assert acl._acl_output_encodings() == ('oem', 'mbcs')


def test_console_codec_is_preferred_and_deduplicated(monkeypatch):
    kernel = SimpleNamespace(GetConsoleOutputCP=lambda: 866)
    monkeypatch.setattr(acl, '_windows_kernel32', lambda: kernel)
    monkeypatch.setattr(acl, 'sys', SimpleNamespace(platform='win32'))
    assert acl._acl_output_encodings() == ('cp866', 'oem', 'mbcs')


def test_identity_uses_domain_and_username(monkeypatch):
    monkeypatch.setenv('USERNAME', 'Оператор')
    monkeypatch.setenv('USERDOMAIN', 'PC')
    assert acl._windows_acl_identity() == OWNER
    monkeypatch.delenv('USERDOMAIN')
    assert acl._windows_acl_identity() == 'Оператор'


def test_import_and_parsing_do_not_load_runtime_or_launch_commands():
    root = Path(__file__).resolve().parents[3]
    code = '''
import builtins, ctypes, subprocess
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'markdown_transaction', 'memory_queue', 'reliable_memory', 'memory_state'}:
        raise AssertionError('runtime import: ' + name)
    return original(name, *args, **kwargs)
def forbidden(*args, **kwargs): raise AssertionError('unexpected side effect')
builtins.__import__ = guard
ctypes.WinDLL = forbidden
subprocess.run = forbidden
from llm_wiki import windows_acl
assert windows_acl._acl_is_owner_only(['PC\\\\user:(F)'], 'PC\\\\user')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, text=True,
                            capture_output=True, check=False, timeout=30,
                            env={**os.environ, 'PYTHONPATH': str(root / 'scripts')})
    assert result.returncode == 0, result.stderr
