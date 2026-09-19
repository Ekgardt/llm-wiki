"""kernel32 is loaded so that its last error is kept and a handle is not cut to an int.

There is no Windows here: the loader is the operating-system boundary and is
replaced by one that records how it was asked.
See docs/research/2026-09-17-windows-names-and-windows-errors-are-read-as-they-are-written.md.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import markdown_transaction  # noqa: E402

INVALID_HANDLE = ctypes.c_void_p(-1).value
SHARING_VIOLATION = 32


class _Function:
    def __init__(self, result: object) -> None:
        self.result = result
        self.restype = ctypes.c_int
        self.argtypes = None

    def __call__(self, *_arguments: object) -> object:
        return self.result


def _refusing_kernel32() -> SimpleNamespace:
    return SimpleNamespace(
        CreateFileW=_Function(INVALID_HANDLE),
        CloseHandle=_Function(0),
        GetFileInformationByHandle=_Function(0),
        SetFileInformationByHandle=_Function(0),
        GetConsoleOutputCP=_Function(866),
    )


@pytest.fixture
def loaded(monkeypatch) -> dict[str, object]:
    """A fake loader; `windll`, which keeps no last error, is not offered at all."""
    seen: dict[str, object] = {"kernel32": _refusing_kernel32()}

    def load(name: str, *, use_last_error: bool = False):
        seen["request"] = (name, use_last_error)
        return seen["kernel32"]

    loader = getattr(markdown_transaction, "_windows_kernel32", None)
    monkeypatch.setattr(markdown_transaction.ctypes, "WinDLL", load, raising=False)
    monkeypatch.setattr(
        markdown_transaction.ctypes,
        "get_last_error",
        lambda: SHARING_VIOLATION if seen.get("request") == ("kernel32", True) else 0,
        raising=False,
    )
    if loader is not None:
        loader.cache_clear()
    yield seen
    if loader is not None:
        loader.cache_clear()


def test_a_refused_mutation_handle_is_noticed_and_reads_as_contention(loaded, tmp_path):
    with pytest.raises(OSError) as refused:
        markdown_transaction._open_windows_file_for_mutation(tmp_path / "page.md")

    assert refused.value.errno == SHARING_VIOLATION
    assert markdown_transaction._is_transient_writer_contention(refused.value)
    assert loaded["kernel32"].CreateFileW.restype is wintypes.HANDLE


def test_every_handle_function_is_declared_before_it_is_called(loaded, tmp_path):
    with pytest.raises(OSError):
        markdown_transaction._close_windows_handle(7)

    kernel32 = loaded["kernel32"]
    assert kernel32.CloseHandle.argtypes == (wintypes.HANDLE,)
    assert kernel32.SetFileInformationByHandle.argtypes[0] is wintypes.HANDLE
    assert kernel32.GetFileInformationByHandle.restype is wintypes.BOOL


def test_a_failed_delete_carries_the_kept_error(loaded):
    with pytest.raises(OSError) as refused:
        markdown_transaction._delete_windows_handle(7)

    assert refused.value.errno == SHARING_VIOLATION
