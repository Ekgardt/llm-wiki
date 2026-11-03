"""Shared existing Windows ABI declarations; kernel32 loading remains lazy."""
from __future__ import annotations

import ctypes
import functools
from ctypes import wintypes


class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _WindowsDispositionInformation(ctypes.Structure):
    _fields_ = [("delete_file", wintypes.BOOL)]


_WINDOWS_KERNEL32_PROTOTYPES = (
    (
        "CreateFileW",
        wintypes.HANDLE,
        (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ),
    ),
    ("CloseHandle", wintypes.BOOL, (wintypes.HANDLE,)),
    (
        "GetFileInformationByHandle",
        wintypes.BOOL,
        (wintypes.HANDLE, ctypes.POINTER(_WindowsFileInformation)),
    ),
    (
        "SetFileInformationByHandle",
        wintypes.BOOL,
        (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD),
    ),
    ("GetConsoleOutputCP", wintypes.UINT, ()),
    ("GetModuleFileNameW", wintypes.DWORD, (wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)),
)


@functools.lru_cache(maxsize=1)
def _windows_kernel32():
    """kernel32 with its last error kept by ctypes and every handle at full width.

    `ctypes.windll` keeps no last error and returns a C int, so a sharing
    violation read as 0 and an invalid 64-bit handle went unnoticed. See
    `docs/research/2026-09-17-windows-names-and-windows-errors-are-read-as-they-are-written.md`.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, restype, argtypes in _WINDOWS_KERNEL32_PROTOTYPES:
        function = getattr(kernel32, name)
        function.restype = restype
        function.argtypes = argtypes
    return kernel32


def loaded_module_filename(handle: int) -> str:
    """Resolve an already loaded module; zero selects the current executable."""
    kernel32 = _windows_kernel32()
    # Start at Win32 MAX_PATH; this is an allocation size, not a path limit.
    capacity = 260
    while True:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = kernel32.GetModuleFileNameW(handle, buffer, capacity)
        if length == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < capacity:
            return buffer.value
        capacity *= 2


