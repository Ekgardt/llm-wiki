"""Peak memory, RAM class and machine description for the qualification report."""

from __future__ import annotations

import ctypes
import os
import platform
import sys


def _windows_peak_rss() -> tuple[float, str]:
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    handle = get_current_process()
    if not get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return counters.PeakWorkingSetSize / (1024.0 * 1024.0), "measured-windows-peak-working-set"


def _posix_peak_rss() -> tuple[float, str]:
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    multiplier = 1 if sys.platform == "darwin" else 1024
    return usage * multiplier / (1024.0 * 1024.0), "measured-posix-ru-maxrss"


def _peak_rss() -> tuple[float | None, str]:
    if os.name == "nt":
        probe, failures = _windows_peak_rss, (AttributeError, OSError, TypeError, ValueError)
    else:
        probe, failures = _posix_peak_rss, (ImportError, OSError, ValueError)
    try:
        return probe()
    except failures:
        return None, "unavailable"


def _windows_total_physical_bytes() -> int | None:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return int(status.total_physical)
    return None


def _ram_bytes() -> int | None:
    if os.name == "nt":
        try:
            total_physical = _windows_total_physical_bytes()
        except (AttributeError, OSError, ValueError):
            return None
        if total_physical is not None:
            return total_physical
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size)
    except (AttributeError, OSError, ValueError):
        return None


def _ram_class() -> str:
    total = _ram_bytes()
    if total is None:
        return "unavailable"
    gib = total / (1024**3)
    for lower, upper in ((0, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128)):
        if lower <= gib < upper:
            return f"{lower}-{upper - 1} GiB"
    return "128+ GiB"


def _first_available(*values: str) -> str:
    for value in values:
        if value:
            return value
    return "unavailable"


def _environment() -> dict[str, object]:
    return {
        "os": platform.system() or os.name,
        "os_version": _first_available(platform.version(), platform.release()),
        "architecture": _first_available(platform.machine()),
        "cpu_model": _first_available(platform.processor(), platform.machine()),
        "cpu_core_count": max(1, os.cpu_count() or 1),
        "ram_class": _ram_class(),
    }
