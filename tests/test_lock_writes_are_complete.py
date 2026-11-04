"""Small lock records still require complete writes and owned descriptor cleanup."""
import ast
import errno
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import claims
import install_control
import maybe_compile
import memory_state
import pytest
import scheduled_nightly

from tests.slow_machine import LONG_TIMEOUT

PAYLOAD = b'123\nidentity\n'
ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _descriptor(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        yield fd
    finally:
        os.close(fd)


def _state(path, monkeypatch):
    monkeypatch.setattr(memory_state, 'LOCK_FILE', path)
    fd = memory_state._claim_lock(PAYLOAD)
    os.close(fd)
    return PAYLOAD


def _compile(path, monkeypatch):
    monkeypatch.setattr(maybe_compile, 'LOCK_FILE', path)
    assert maybe_compile._exclusive_write_lock(PAYLOAD)
    return PAYLOAD


def _nightly(path, monkeypatch):
    monkeypatch.setattr(scheduled_nightly, 'marker_payload', lambda: PAYLOAD)
    assert scheduled_nightly._write_marker(path)
    return PAYLOAD


def _claim_token(path, monkeypatch):
    with _descriptor(path) as fd:
        claims._write_lock_token(fd)
    return f'{os.getpid()}:{threading.get_ident()}'.encode()


def _claim_preparation(path, monkeypatch):
    with _descriptor(path) as fd:
        claims._prepare_lock_file(path, fd, True)
    return b'0'


def _install(path, monkeypatch):
    with install_control._install_lock(path, LONG_TIMEOUT):
        pass
    return b'0'


WRITERS = (_state, _compile, _nightly, _claim_token, _claim_preparation, _install)


@pytest.mark.parametrize('writer', WRITERS)
def test_short_writes_complete_every_record(tmp_path, monkeypatch, writer):
    real = os.write
    monkeypatch.setattr(os, 'write', lambda fd, data: real(fd, data[:1]))
    path = tmp_path / 'lock'
    expected = writer(path, monkeypatch)
    assert path.read_bytes() == expected


@pytest.mark.parametrize('writer', WRITERS)
def test_zero_progress_never_reports_a_lock_written(tmp_path, monkeypatch, writer):
    monkeypatch.setattr(os, 'write', lambda fd, data: 0)
    with pytest.raises(OSError, match='no progress'):
        writer(tmp_path / 'lock', monkeypatch)


def _raise_write_error(error, fd, data):
    raise error


@pytest.mark.parametrize('writer', WRITERS)
def test_write_error_propagates_unchanged(tmp_path, monkeypatch, writer):
    error = OSError(errno.ENOSPC, 'injected full disk')
    monkeypatch.setattr(os, 'write', lambda fd, data: _raise_write_error(error, fd, data))
    with pytest.raises(OSError) as caught:
        writer(tmp_path / 'lock', monkeypatch)
    assert caught.value is error


def _close_if_open(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def test_failed_state_claim_closes_its_descriptor(tmp_path, monkeypatch):
    descriptors = []
    error = OSError(errno.ENOSPC, 'injected full disk')

    def failed_write(fd, data):
        descriptors.append(fd)
        raise error

    monkeypatch.setattr(memory_state, 'LOCK_FILE', tmp_path / 'lock')
    monkeypatch.setattr(os, 'write', failed_write)
    try:
        with pytest.raises(OSError) as caught:
            memory_state._claim_lock(PAYLOAD)
        assert caught.value is error
        with pytest.raises(OSError) as closed:
            os.fstat(descriptors[0])
        assert closed.value.errno == errno.EBADF
    finally:
        for fd in descriptors:
            _close_if_open(fd)


def _ignored_raw_write(node):
    if not isinstance(node, ast.Expr):
        return False
    if not isinstance(node.value, ast.Call):
        return False
    return ast.unparse(node.value.func) == 'os.write'


def _unchecked_writes(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return [node.lineno for node in ast.walk(tree) if _ignored_raw_write(node)]


def test_raw_os_write_results_are_not_discarded():
    found = {str(p.relative_to(ROOT)): _unchecked_writes(p)
             for p in (ROOT / 'scripts').rglob('*.py')}
    assert not {p: lines for p, lines in found.items() if lines}


LIMIT_PROBE = '''
import errno, json, os, pathlib, resource, signal, sys
sys.path.insert(0, sys.argv[1])
import memory_state
memory_state.LOCK_FILE = pathlib.Path(sys.argv[2]) / 'lock'
real_open = os.open
opened = []
def tracked_open(*args, **kwargs):
    fd = real_open(*args, **kwargs)
    opened.append(fd)
    return fd
os.open = tracked_open
soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[3]), hard))
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
try:
    fd = memory_state._claim_lock(b'123\\nidentity\\n')
except OSError as error:
    try:
        os.fstat(opened[-1])
    except OSError as closed:
        outcome = {'errno': error.errno, 'closed': closed.errno == errno.EBADF}
    else:
        os.close(opened[-1])
        outcome = {'errno': error.errno, 'closed': False}
else:
    os.close(fd)
    outcome = {'errno': None, 'closed': True}
print(json.dumps(outcome))
'''


@pytest.mark.skipif(os.name != 'posix', reason='RLIMIT_FSIZE is a POSIX kernel facility')
@pytest.mark.parametrize('limit', [0, 3, len(PAYLOAD) - 1])
def test_real_file_size_limit_cannot_publish_a_partial_lock(tmp_path, limit):
    result = subprocess.run(
        [sys.executable, '-c', LIMIT_PROBE, str(ROOT / 'scripts'), str(tmp_path), str(limit)],
        capture_output=True, text=True, timeout=LONG_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'errno': errno.EFBIG, 'closed': True}


@pytest.mark.parametrize('script', ['maybe_compile.py', 'scheduled_nightly.py'])
def test_script_bootstrap_establishes_import_path_before_its_dependencies(script):
    program = "import runpy, sys; runpy.run_path(sys.argv[1], run_name='isolated_bootstrap')"
    result = subprocess.run(
        [sys.executable, '-I', '-c', program, str(ROOT / 'scripts' / script)],
        capture_output=True, text=True, timeout=LONG_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
