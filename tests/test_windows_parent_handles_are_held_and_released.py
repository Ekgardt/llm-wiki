from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any

import pytest
from llm_wiki import markdown_targets
from llm_wiki.markdown_records import TargetBoundaryFailure
from markdown_transaction import MarkdownCoordinator

_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _WindowsNamedOs:
    name = "nt"

    def __getattr__(self, attribute: str) -> object:
        return getattr(os, attribute)


class _FakeKernel32:
    def __init__(self, handle: object, identified: bool, attributes: int) -> None:
        self.handle = handle
        self.identified = identified
        self.attributes = attributes
        self.closed: list[object] = []

    def CreateFileW(self, *arguments: object) -> object:
        return self.handle

    def GetFileInformationByHandle(self, handle: object, information: Any) -> bool:
        information._obj.file_attributes = self.attributes
        return self.identified

    def CloseHandle(self, handle: object) -> bool:
        self.closed.append(handle)
        return True


class _HandleLedger:
    def __init__(self, failing_open: Path | None, failing_close: set[int]) -> None:
        self.failing_open = failing_open
        self.failing_close = failing_close
        self.opened: dict[int, Path] = {}
        self.closed: list[int] = []

    def open(self, path: Path) -> int:
        if path == self.failing_open:
            raise OSError(5, "access denied")
        handle = 100 + len(self.opened)
        self.opened[handle] = path
        return handle

    def close(self, handle: int) -> None:
        self.closed.append(handle)
        if handle in self.failing_close:
            raise OSError(6, f"cannot close {handle}")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    return root


@pytest.fixture
def coordinator(vault: Path, tmp_path: Path) -> MarkdownCoordinator:
    return MarkdownCoordinator(vault, tmp_path / "state")


def _on_windows_with(monkeypatch: pytest.MonkeyPatch, ledger: _HandleLedger) -> None:
    monkeypatch.setattr(markdown_targets, "os", _WindowsNamedOs())
    monkeypatch.setattr(markdown_targets, "_open_windows_directory", ledger.open)
    monkeypatch.setattr(markdown_targets, "close_windows_handle", ledger.close)


@pytest.mark.skipif(os.name == "nt", reason="the refusal is the behaviour off Windows")
def test_holding_a_windows_parent_is_refused_off_windows(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    with (
        pytest.raises(RuntimeError, match="requires Windows directory handles"),
        coordinator._hold_windows_parent(vault / "knowledge/notes"),
    ):
        pass


def test_every_directory_from_the_vault_down_is_held_then_closed_in_reverse(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _HandleLedger(None, set())
    _on_windows_with(monkeypatch, ledger)

    with coordinator._hold_windows_parent(vault / "knowledge/notes"):
        held = coordinator._local.windows_parent_handle

    assert (list(ledger.opened.values()), held, ledger.closed) == (
        [vault, vault / "knowledge", vault / "knowledge/notes"],
        102,
        [102, 101, 100],
    )
    assert coordinator._local.windows_parent_handle is None


def test_a_failed_close_of_the_vault_handle_is_raised_after_the_others_close(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _HandleLedger(None, {100})
    _on_windows_with(monkeypatch, ledger)

    with (
        pytest.raises(OSError, match="cannot close 100"),
        coordinator._hold_windows_parent(vault / "knowledge/notes"),
    ):
        pass

    assert ledger.closed == [102, 101, 100]


def test_a_failed_open_closes_the_handles_already_held(
    coordinator: MarkdownCoordinator, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _HandleLedger(vault / "knowledge/notes", set())
    _on_windows_with(monkeypatch, ledger)

    with (
        pytest.raises(OSError, match="access denied"),
        coordinator._hold_windows_parent(vault / "knowledge/notes"),
    ):
        pass

    assert ledger.closed == [101, 100]


def test_a_parent_outside_the_vault_is_a_boundary_failure(
    coordinator: MarkdownCoordinator, tmp_path: Path
) -> None:
    with pytest.raises(TargetBoundaryFailure, match="target parent is outside the vault"):
        coordinator._vault_chain(tmp_path / "elsewhere")


@pytest.mark.parametrize(
    ("code", "error", "message"),
    [
        (2, TargetBoundaryFailure, "parent identity is not a stable directory"),
        (3, TargetBoundaryFailure, "parent identity is not a stable directory"),
        (267, TargetBoundaryFailure, "parent identity is not a stable directory"),
        (5, OSError, "cannot lock Windows directory"),
    ],
)
def test_a_directory_that_cannot_be_opened_is_named_by_its_windows_error(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    error: type[Exception],
    message: str,
) -> None:
    kernel32 = _FakeKernel32(_INVALID_HANDLE, True, 0)
    monkeypatch.setattr(markdown_targets, "windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: code, raising=False)

    with pytest.raises(error, match=message):
        markdown_targets._open_windows_directory(vault)


def test_an_identified_plain_directory_handle_is_returned_open(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel32 = _FakeKernel32(42, True, 0x10)
    monkeypatch.setattr(markdown_targets, "windows_kernel32", lambda: kernel32)

    assert (markdown_targets._open_windows_directory(vault), kernel32.closed) == (42, [])


def test_a_directory_handle_on_a_reparse_point_is_closed_and_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel32 = _FakeKernel32(42, True, 0x400)
    monkeypatch.setattr(markdown_targets, "windows_kernel32", lambda: kernel32)

    with pytest.raises(TargetBoundaryFailure, match="resolves to a reparse point"):
        markdown_targets._open_windows_directory(vault)
    assert kernel32.closed == [42]


def test_a_directory_handle_that_cannot_be_identified_is_closed_and_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel32 = _FakeKernel32(42, False, 0)
    monkeypatch.setattr(markdown_targets, "windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 6, raising=False)

    with pytest.raises(OSError, match="cannot identify Windows directory") as raised:
        markdown_targets._open_windows_directory(vault)
    assert (raised.value.errno, kernel32.closed) == (6, [42])
