"""Native snapshot contracts and failed-enumeration safety."""
import os

import llm_wiki.queue_process_cleanup as cleanup
import pytest


def test_the_windows_process_tree_comes_from_the_kernel_snapshot(monkeypatch):
    """One source, present on every supported Windows and needing no subprocess."""
    monkeypatch.setattr(cleanup, "_windows_process_pairs", lambda: [(11, 5), (12, 11)])

    assert cleanup._tracked_descendant_pids(5, "nt") == {11, 12}


def test_a_failed_snapshot_reports_unknown_rather_than_empty(monkeypatch):
    """An unknown descendant set must not read as a verified clean tree."""
    monkeypatch.setattr(cleanup, "_windows_process_pairs", lambda: None)

    assert cleanup._tracked_descendant_pids(5, "nt") is None


def _snapshot_api(monkeypatch, rows, error):
    import ctypes
    from types import SimpleNamespace

    remaining = iter(rows)
    closed, seen = [], []

    def step(handle, pointer):
        seen.append(handle)
        row = next(remaining, None)
        if row is None:
            return False
        pointer._obj.th32ProcessID, pointer._obj.th32ParentProcessID = row
        return True

    kernel = SimpleNamespace(CreateToolhelp32Snapshot=lambda *_: 55,
                             Process32First=step, Process32Next=step,
                             CloseHandle=lambda handle: closed.append(handle))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    return kernel, closed, seen


@pytest.mark.parametrize("rows", [[], [(11, 5)]])
@pytest.mark.parametrize("error", [0, 5, 6])
def test_incomplete_windows_process_enumeration_is_unknown(monkeypatch, rows, error):
    kernel, closed, seen = _snapshot_api(monkeypatch, rows, error)
    assert cleanup._windows_process_pairs() is None
    assert closed == [55]


@pytest.mark.parametrize("rows", [[], [(11, 5), (12, 11)]])
def test_windows_process_enumeration_accepts_only_normal_end(monkeypatch, rows):
    kernel, closed, seen = _snapshot_api(monkeypatch, rows, 18)
    assert cleanup._windows_process_pairs() == rows
    assert closed == [55]


def test_windows_snapshot_handle_keeps_native_pointer_width(monkeypatch):
    import ctypes
    from ctypes import wintypes

    kernel, closed, seen = _snapshot_api(monkeypatch, [], 18)
    # Set the sign bit on either pointer width: a default signed C int is wrong.
    handle = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8 - 1)) + 55
    callback = ctypes.CFUNCTYPE(ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD)(lambda *_: handle)
    callback.restype = ctypes.c_int
    kernel.CreateToolhelp32Snapshot = callback
    assert cleanup._windows_process_pairs() == []
    assert seen == [handle]
    assert closed == [handle]


@pytest.mark.skipif(os.name != "nt", reason="native Windows Toolhelp API qualification")
def test_native_windows_snapshot_contains_this_process():
    pairs = cleanup._windows_process_pairs()
    assert pairs is not None
    assert os.getpid() in {pid for pid, _parent in pairs}

