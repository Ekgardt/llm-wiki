"""Faithful loader checks; these are not a native Windows execution claim."""
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest
from llm_wiki import windows_native as native


@pytest.fixture
def loaded(monkeypatch):
    calls = []
    library = SimpleNamespace(**{name: SimpleNamespace() for name, _, _ in native.WINDOWS_KERNEL32_PROTOTYPES})

    def loader(name, **options):
        calls.append((name, options))
        return library

    native.windows_kernel32.cache_clear()
    monkeypatch.setattr(ctypes, 'WinDLL', loader, raising=False)
    try:
        yield library, calls
    finally:
        native.windows_kernel32.cache_clear()


def test_loader_is_lazy_cached_and_keeps_last_error(loaded):
    library, calls = loaded
    assert calls == []
    assert native.windows_kernel32() is library
    assert native.windows_kernel32() is library
    assert calls == [('kernel32', {'use_last_error': True})]


def test_every_signature_is_installed_before_use(loaded):
    library = native.windows_kernel32()
    for name, restype, argtypes in native.WINDOWS_KERNEL32_PROTOTYPES:
        function = getattr(library, name)
        assert function.restype is restype
        assert function.argtypes == argtypes
    assert library.CreateFileW.restype is wintypes.HANDLE
    assert library.CloseHandle.argtypes == (wintypes.HANDLE,)
    assert library.GetFileInformationByHandle.argtypes == (
        wintypes.HANDLE, ctypes.POINTER(native.WindowsFileInformation))
    assert library.GetConsoleOutputCP.restype is wintypes.UINT
    assert library.GetModuleFileNameW.argtypes == (wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
    assert library.GetModuleFileNameW.restype is wintypes.DWORD


def test_structures_preserve_field_names_and_types():
    assert native.WindowsFileInformation._fields_ == [
        ('file_attributes', wintypes.DWORD), ('creation_time', wintypes.FILETIME),
        ('last_access_time', wintypes.FILETIME), ('last_write_time', wintypes.FILETIME),
        ('volume_serial_number', wintypes.DWORD), ('file_size_high', wintypes.DWORD),
        ('file_size_low', wintypes.DWORD), ('number_of_links', wintypes.DWORD),
        ('file_index_high', wintypes.DWORD), ('file_index_low', wintypes.DWORD)]
    assert native.WindowsDispositionInformation._fields_ == [('delete_file', wintypes.BOOL)]
    assert ctypes.sizeof(wintypes.HANDLE) == ctypes.sizeof(ctypes.c_void_p)


def test_import_does_not_load_dll_or_executor():
    root = Path(__file__).resolve().parents[3]
    code = '''
import builtins, ctypes, sys
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'markdown_transaction', 'memory_queue', 'reliable_memory', 'memory_state'}:
        raise AssertionError('runtime import: ' + name)
    return original(name, *args, **kwargs)
def forbidden(*args, **kwargs): raise AssertionError('DLL loaded during import')
builtins.__import__ = guard
ctypes.WinDLL = forbidden
from llm_wiki import windows_native
assert windows_native.windows_kernel32.cache_info().currsize == 0
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root, text=True,
                            capture_output=True, check=False, timeout=30,
                            env={**os.environ, 'PYTHONPATH': str(root / 'scripts')})
    assert result.returncode == 0, result.stderr
