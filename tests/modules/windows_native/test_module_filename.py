"""Loaded module paths retain full handles, Unicode and native failure semantics."""
import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from llm_wiki import windows_native as native


def test_module_path_grows_until_complete_without_truncation(monkeypatch):
    expected = 'C:\\' + 'long-name\\' * 100 + 'python.dll'
    calls = []
    handle = 1 << 40

    def filename(value, buffer, capacity):
        calls.append((value, capacity))
        buffer.value = expected[:capacity - 1]
        return min(len(expected), capacity)

    monkeypatch.setattr(native, '_windows_kernel32', lambda: SimpleNamespace(GetModuleFileNameW=filename))
    assert native.loaded_module_filename(handle) == expected
    assert len(calls) > 1
    assert all(value == handle for value, _ in calls)
    assert calls[-1][1] > len(expected)


def test_module_path_preserves_unicode(monkeypatch):
    expected = 'C:\\Питон\\python.dll'

    def filename(handle, buffer, capacity):
        buffer.value = expected
        return len(expected)

    monkeypatch.setattr(native, '_windows_kernel32', lambda: SimpleNamespace(GetModuleFileNameW=filename))
    assert native.loaded_module_filename(123) == expected


def test_zero_native_result_propagates_saved_os_error(monkeypatch):
    api = SimpleNamespace(GetModuleFileNameW=lambda *args: 0)
    error = OSError('native error 126')
    monkeypatch.setattr(native, '_windows_kernel32', lambda: api)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 126, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda code: error, raising=False)
    with pytest.raises(OSError, match='native error 126') as raised:
        native.loaded_module_filename(123)
    assert raised.value is error


@pytest.mark.skipif(os.name != 'nt', reason='Real loaded Windows module handle')
def test_native_loaded_python_module_has_a_real_path():
    import sys

    path = Path(native.loaded_module_filename(sys.dllhandle))
    assert path.is_absolute()
    assert path.is_file()
    assert path.stat().st_size > 0
