"""Peak client memory measurement of the Python navigation benchmark on Windows and POSIX."""

from __future__ import annotations

import ctypes
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import navigation_benchmark_environment as environment  # noqa: E402


class _FakeWindowsFunction:
    def __init__(self, callback) -> None:
        self._callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._callback(*args)


def _install_fake_windows_rss_api(
    monkeypatch: pytest.MonkeyPatch,
    memory_callback,
) -> tuple[object, _FakeWindowsFunction, _FakeWindowsFunction, list[tuple[str, bool]]]:
    handle = object()
    get_current_process = _FakeWindowsFunction(lambda: handle)
    get_process_memory_info = _FakeWindowsFunction(memory_callback)
    libraries = {
        "kernel32": SimpleNamespace(GetCurrentProcess=get_current_process),
        "psapi": SimpleNamespace(GetProcessMemoryInfo=get_process_memory_info),
    }
    loads: list[tuple[str, bool]] = []

    def load(name: str, *, use_last_error: bool = False):
        loads.append((name, use_last_error))
        return libraries[name]

    monkeypatch.setattr(environment.os, "name", "nt")
    monkeypatch.setattr(environment.ctypes, "WinDLL", load, raising=False)
    return handle, get_current_process, get_process_memory_info, loads


def test_windows_peak_rss_uses_pointer_sized_win32_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def memory_info(handle: object, counters: object, size: int) -> int:
        observed.update(handle=handle, counters=counters, size=size)
        counters._obj.PeakWorkingSetSize = 96 * 1024 * 1024
        return 1

    handle, get_current_process, get_process_memory_info, loads = (
        _install_fake_windows_rss_api(monkeypatch, memory_info)
    )
    value, method = environment._peak_rss()

    from ctypes import wintypes

    assert loads == [("kernel32", True), ("psapi", True)]
    assert get_current_process.argtypes == []
    assert get_current_process.restype is wintypes.HANDLE
    assert get_process_memory_info.argtypes[0] is wintypes.HANDLE
    assert get_process_memory_info.argtypes[1]._type_ is type(observed["counters"]._obj)
    assert get_process_memory_info.argtypes[2] is wintypes.DWORD
    assert get_process_memory_info.restype is wintypes.BOOL
    assert observed["handle"] is handle
    assert observed["size"] == ctypes.sizeof(observed["counters"]._obj)
    assert value == 96.0
    assert method == "measured-windows-peak-working-set"


def test_windows_peak_rss_api_failure_uses_last_error_and_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_error_calls: list[bool] = []
    _handle, _current, _memory, loads = _install_fake_windows_rss_api(
        monkeypatch,
        lambda handle, counters, size: 0,
    )
    monkeypatch.setattr(
        environment.ctypes,
        "get_last_error",
        lambda: last_error_calls.append(True) or 5,
        raising=False,
    )

    assert environment._peak_rss() == (None, "unavailable")
    assert loads == [("kernel32", True), ("psapi", True)]
    assert last_error_calls == [True]


@pytest.mark.skipif(os.name != "nt", reason="requires the real Win32 process APIs")
def test_windows_peak_rss_integration_is_finite_positive_and_measured() -> None:
    value, method = environment._peak_rss()

    assert value is not None
    assert math.isfinite(value)
    assert value > 0.0
    assert method == "measured-windows-peak-working-set"
