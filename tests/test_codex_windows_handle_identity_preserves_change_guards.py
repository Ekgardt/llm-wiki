"""Handle identity keeps Windows timestamp meanings and file-change guards."""
from pathlib import Path
from types import SimpleNamespace

import llm_client as lc
import pytest


def _windows_stat(monkeypatch, target):
    original = Path.stat

    def stat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path != target:
            return info
        values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
        values['st_ctime_ns'] = info.st_ctime_ns - 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(lc, 'sys', SimpleNamespace(platform='win32'))


def _file(tmp_path):
    path = tmp_path / 'selected-file'
    path.write_bytes(b'qualified local bytes')
    return path


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
def test_stable_windows_handle_survives_different_path_timestamp(monkeypatch, tmp_path, consumer):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    result = _read(path, consumer)
    assert _digest(result) == lc.hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path, consumer):
    if consumer == 'configuration':
        return lc._codex_basis_file_digest(path)
    return lc._bind_codex_executable(path)


def _digest(result):
    if isinstance(result, str):
        return result
    return result.sha256


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
def test_windows_reads_both_bound_handles(monkeypatch, tmp_path, consumer):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    original = lc._codex_stream_digest
    calls = []

    def digest(handle):
        calls.append(handle.fileno())
        return original(handle)

    monkeypatch.setattr(lc, '_codex_stream_digest', digest)
    _read(path, consumer)
    assert len(calls) == 2 and calls[0] != calls[1]


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
@pytest.mark.parametrize('change', ['bytes', 'replacement', 'disappearance'])
def test_change_after_first_read_cannot_pass_rebinding(monkeypatch, tmp_path, consumer, change):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    original = lc._codex_stream_digest
    calls = []

    def digest(handle):
        value = original(handle)
        calls.append(value)
        if len(calls) == 1:
            _change(path, change)
        return value

    monkeypatch.setattr(lc, '_codex_stream_digest', digest)
    with pytest.raises((RuntimeError, OSError)):
        _read(path, consumer)


def _change(path, change):
    if change == 'replacement':
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        return
    if change == 'disappearance':
        path.unlink()
        return
    path.write_bytes(b'changed local bytes')


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
def test_reopened_hash_mismatch_is_not_just_stat_identity(monkeypatch, tmp_path, consumer):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    original = lc._codex_stream_digest
    calls = []

    def digest(handle):
        value = original(handle)
        calls.append(value)
        if len(calls) == 2:
            return '0' * 64
        return value

    monkeypatch.setattr(lc, '_codex_stream_digest', digest)
    with pytest.raises(RuntimeError, match='changed'):
        _read(path, consumer)


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
def test_posix_path_ctime_guard_remains_strict(monkeypatch, tmp_path, consumer):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    monkeypatch.setattr(lc, 'sys', SimpleNamespace(platform='linux'))
    with pytest.raises(RuntimeError, match='changed'):
        _read(path, consumer)


def test_prepared_executable_recheck_rejects_changed_digest(monkeypatch, tmp_path):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    executable = lc._bind_codex_executable(path)
    path.write_bytes(b'different local bytes')
    with pytest.raises(RuntimeError, match='changed'):
        lc._require_codex_executable(executable)


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
def test_both_handles_live_and_closed_after_success(monkeypatch, tmp_path, consumer):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    original = lc._codex_stream_digest
    handles = []

    def digest(handle):
        handles.append(handle)
        assert all(not item.closed for item in handles)
        return original(handle)

    monkeypatch.setattr(lc, '_codex_stream_digest', digest)
    _read(path, consumer)
    assert len(handles) == 2 and all(item.closed for item in handles)


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
def test_change_during_reopened_read_closes_handles_and_refuses(monkeypatch, tmp_path, consumer):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    original = lc._codex_stream_digest
    handles = []

    def digest(handle):
        value = original(handle)
        handles.append(handle)
        if len(handles) == 2:
            path.write_bytes(b'changed after reopened digest')
        return value

    monkeypatch.setattr(lc, '_codex_stream_digest', digest)
    with pytest.raises(RuntimeError, match='changed'):
        _read(path, consumer)
    assert len(handles) == 2 and all(item.closed for item in handles)


def test_symlink_retarget_during_binding_refuses(monkeypatch, tmp_path):
    original = _file(tmp_path)
    other = tmp_path / 'other'
    other.write_bytes(original.read_bytes())
    alias = tmp_path / 'alias'
    _symlink_or_skip(alias, original)
    monkeypatch.setattr(lc, 'sys', SimpleNamespace(platform='win32'))
    digest = lc._codex_stream_digest

    def retarget(handle):
        value = digest(handle)
        alias.unlink()
        alias.symlink_to(other)
        return value

    monkeypatch.setattr(lc, '_codex_stream_digest', retarget)
    with pytest.raises(RuntimeError, match='changed'):
        lc._bind_codex_executable(alias)


def _symlink_or_skip(alias, target):
    try:
        alias.symlink_to(target)
    except OSError as error:
        pytest.skip('symlink not supported: ' + str(error.errno))


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
def test_handle_change_time_alone_still_refuses(monkeypatch, tmp_path, consumer):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    original = lc.os.fstat
    calls = []

    def fstat(descriptor):
        info = original(descriptor)
        calls.append(descriptor)
        if len(calls) != 4:
            return info
        values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
        values['st_ctime_ns'] += 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(lc.os, 'fstat', fstat)
    with pytest.raises(RuntimeError, match='changed'):
        _read(path, consumer)


@pytest.mark.parametrize('consumer', ['executable', 'configuration'])
def test_reopen_oserror_refuses_and_closes_original_handle(monkeypatch, tmp_path, consumer):
    path = _file(tmp_path)
    _windows_stat(monkeypatch, path)
    original = Path.open
    calls = []
    handles = []

    def open_path(candidate, *args, **kwargs):
        if candidate == path:
            calls.append(candidate)
            if len(calls) == 2:
                raise PermissionError('controlled reopen refusal')
        handle = original(candidate, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, 'open', open_path)
    with pytest.raises(PermissionError, match='controlled reopen refusal'):
        _read(path, consumer)
    assert len(handles) == 1 and handles[0].closed
