"""Permit real replacement in Windows tests of application identity guards."""
import ctypes
import os

from llm_wiki.windows_native import windows_kernel32


def _replacement_reader(path):
    import msvcrt

    kernel32 = windows_kernel32()
    handle = kernel32.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def allow_file_replacement(monkeypatch, target):
    if os.name != 'nt':
        return
    original_open = os.open

    def open_with_delete_sharing(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            assert flags & (os.O_RDWR | os.O_WRONLY) == 0
            return _replacement_reader(path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', open_with_delete_sharing)
