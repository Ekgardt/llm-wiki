"""Focused tests for Windows handle-relative mutable-file operations."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import pytest
import windows_workspace


def test_windows_entry_size_is_bounded_nonnegative_and_backward_compatible() -> None:
    entry = windows_workspace.WindowsEntry("page.md", "file", b"a" * 16)

    assert entry.size == 0
    assert windows_workspace.WindowsEntry("page.md", "file", b"a" * 16, 7).size == 7
    for invalid in (-1, True, 2**63):
        with pytest.raises(ValueError, match="size"):
            windows_workspace.WindowsEntry("page.md", "file", b"a" * 16, invalid)


@pytest.mark.skipif(os.name != "nt", reason="Windows short-path boundary")
def test_get_short_path_is_exported_and_returns_existing_local_path(
    tmp_path: Path,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows native workspace APIs unavailable")
    target = tmp_path / "long-existing-workspace-component"
    target.mkdir()

    assert "get_short_path" in windows_workspace.__all__
    short = windows_workspace.get_short_path(target)

    assert isinstance(short, Path)
    assert short.is_absolute()
    assert short.samefile(target)


@pytest.mark.skipif(os.name != "nt", reason="Windows short-path boundary")
def test_get_short_path_rejects_nonlocal_paths_before_bounded_api_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows native workspace APIs unavailable")
    calls: list[tuple[str, int]] = []

    def oversized(path: str, _buffer: object, size: int) -> int:
        calls.append((path, size))
        return 40_000

    monkeypatch.setattr(windows_workspace._API, "get_short_path", oversized, raising=False)
    for path in (
        Path("relative"),
        Path(r"\\server\share\folder"),
        Path(r"\\?\C:\folder"),
        Path(r"\\.\C:\folder"),
    ):
        with pytest.raises(ValueError, match="local absolute"):
            windows_workspace.get_short_path(path)
    assert calls == []

    with pytest.raises(ValueError, match="bounded"):
        windows_workspace.get_short_path(tmp_path)
    assert calls == [(str(PureWindowsPath(tmp_path)), 0)]


@pytest.mark.skipif(os.name != "nt", reason="Windows native handle boundary")
def test_created_directory_handle_supports_durable_metadata_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative_opens: list[tuple[str, dict[str, object]]] = []
    real_relative_handle = windows_workspace._relative_handle

    def tracking_relative_handle(
        parent: int,
        name: str,
        **options: object,
    ) -> int:
        relative_opens.append((name, options))
        return real_relative_handle(parent, name, **options)

    monkeypatch.setattr(windows_workspace, "_relative_handle", tracking_relative_handle)
    writable = windows_workspace.open_writable_directory_path(tmp_path)
    try:
        assert windows_workspace.flush_directory(writable) is True
    finally:
        windows_workspace.close_handle(writable)
    assert relative_opens
    assert relative_opens[-1] == (
        tmp_path.name,
        {"directory": True, "create": False, "writable": True},
    )

    parent = windows_workspace.open_directory_path(tmp_path)
    directory = windows_workspace.create_writable_directory(parent, "durable")
    try:
        assert windows_workspace.flush_directory(directory) is True
    finally:
        windows_workspace.close_handle(directory)
        windows_workspace.close_handle(parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows native handle boundary")
def test_replace_and_delete_file_stay_relative_to_retained_directory(
    tmp_path: Path,
) -> None:
    parent = windows_workspace.open_directory_path(tmp_path)
    temporary = windows_workspace.create_file(parent, "lease.tmp")
    try:
        windows_workspace.write_all(temporary, b"first", chunk_bytes=16)
        windows_workspace.flush_file(temporary)
        windows_workspace.replace_file(temporary, parent, "lease.json")
    finally:
        windows_workspace.close_handle(temporary)

    assert (tmp_path / "lease.json").read_bytes() == b"first"
    held = windows_workspace.open_deletable_file(parent, "lease.json")
    try:
        windows_workspace.delete_handle(held)
    finally:
        windows_workspace.close_handle(held)
        windows_workspace.close_handle(parent)
    assert not (tmp_path / "lease.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows native handle boundary")
def test_shared_readonly_source_file_is_exported_and_cannot_write(
    tmp_path: Path,
) -> None:
    target = tmp_path / "source.py"
    target.write_bytes(b"value = 1\n")
    assert "open_shared_readonly_source_file" in windows_workspace.__all__

    parent = windows_workspace.open_directory_path(tmp_path)
    source = windows_workspace.open_shared_readonly_source_file(parent, target.name)
    try:
        assert b"".join(
            windows_workspace.read_chunks(source, chunk_bytes=4, max_bytes=32)
        ) == target.read_bytes()
        with pytest.raises(OSError):
            windows_workspace.write_all(source, b"changed", chunk_bytes=16)
    finally:
        windows_workspace.close_handle(source)
        windows_workspace.close_handle(parent)

    assert target.read_bytes() == b"value = 1\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows native handle boundary")
def test_shared_readonly_source_file_rejects_reparse_and_closes_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source.py"
    target.write_text("value = 1\n", encoding="utf-8")

    closed: list[int] = []
    real_close = windows_workspace.close_handle
    real_attributes = windows_workspace._attributes

    def tracking_close(handle: int) -> None:
        real_close(handle)
        closed.append(handle)

    def reparse_attributes(handle: int) -> int:
        return real_attributes(handle) | 0x00000400

    monkeypatch.setattr(windows_workspace, "close_handle", tracking_close)
    parent = windows_workspace.open_directory_path(tmp_path)
    closed.clear()
    monkeypatch.setattr(windows_workspace, "_attributes", reparse_attributes)
    try:
        with pytest.raises(PermissionError, match="reparse"):
            windows_workspace.open_shared_readonly_source_file(parent, target.name)
    finally:
        windows_workspace.close_handle(parent)

    assert len(closed) == 2
    assert len(set(closed)) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows durable publication boundary")
def test_flush_file_path_uses_checked_no_follow_handle_and_always_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "staged.json"
    target.write_bytes(b"payload")
    calls: list[tuple[object, ...]] = []

    class FakeApi:
        def create_file(self, *args: object) -> int:
            calls.append(("open", *args))
            return 73

        def flush_file_buffers(self, handle: int) -> bool:
            calls.append(("flush", handle))
            return True

        def close_handle(self, handle: int) -> bool:
            calls.append(("close", handle))
            return True

    monkeypatch.setattr(windows_workspace, "_API", FakeApi())
    monkeypatch.setattr(
        windows_workspace,
        "identity",
        lambda handle, *, directory: calls.append(("identity", handle, directory)),
    )

    windows_workspace.flush_file_path(target)

    opened = calls[0]
    assert opened[0] == "open"
    assert opened[1] == f"\\\\?\\{PureWindowsPath(target)}"
    assert int(opened[2]) & 0x40000000  # GENERIC_WRITE
    assert int(opened[6]) & 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    assert calls[-3:] == [("identity", 73, False), ("flush", 73), ("close", 73)]


@pytest.mark.skipif(os.name != "nt", reason="Windows durable publication boundary")
def test_flush_file_path_propagates_native_failure_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    target = tmp_path / "staged.json"
    target.write_bytes(b"payload")
    closed: list[int] = []

    class FakeApi:
        def create_file(self, *_args: object) -> int:
            return 79

        def flush_file_buffers(self, _handle: int) -> bool:
            ctypes.set_last_error(5)
            return False

        def close_handle(self, handle: int) -> bool:
            closed.append(handle)
            return True

    monkeypatch.setattr(windows_workspace, "_API", FakeApi())
    monkeypatch.setattr(windows_workspace, "identity", lambda *_args, **_kwargs: None)

    with pytest.raises(OSError) as raised:
        windows_workspace.flush_file_path(target)

    assert raised.value.winerror == 5
    assert closed == [79]


@pytest.mark.skipif(os.name != "nt", reason="Windows durable publication boundary")
def test_move_file_write_through_uses_only_checked_move_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int]] = []

    class FakeApi:
        def move_file_ex(self, source: str, destination: str, flags: int) -> bool:
            calls.append((source, destination, flags))
            return True

    monkeypatch.setattr(windows_workspace, "_API", FakeApi())
    source = tmp_path / "stage.json"
    destination = tmp_path / "state.json"

    windows_workspace.move_file_write_through(source, destination, replace=False)
    windows_workspace.move_file_write_through(source, destination, replace=True)

    assert [flags for _source, _destination, flags in calls] == [0x8, 0x9]
    assert all(flags & 0x2 == 0 for _source, _destination, flags in calls)


@pytest.mark.skipif(os.name != "nt", reason="Windows durable publication boundary")
def test_native_write_through_move_creates_and_replaces_exact_bytes(tmp_path: Path) -> None:
    supported, reason = windows_workspace.capability()
    if not supported:
        pytest.skip(reason or "Windows native workspace APIs unavailable")
    destination = tmp_path / "state.json"
    first = tmp_path / "first.tmp"
    first.write_bytes(b"first")
    windows_workspace.flush_file_path(first)
    windows_workspace.move_file_write_through(first, destination, replace=False)
    assert destination.read_bytes() == b"first"
    assert not first.exists()

    second = tmp_path / "second.tmp"
    second.write_bytes(b"second")
    windows_workspace.flush_file_path(second)
    windows_workspace.move_file_write_through(second, destination, replace=True)
    assert destination.read_bytes() == b"second"
    assert not second.exists()

    third = tmp_path / "third.tmp"
    third.write_bytes(b"third")
    with pytest.raises(FileExistsError):
        windows_workspace.move_file_write_through(third, destination, replace=False)
    assert third.read_bytes() == b"third"
    assert destination.read_bytes() == b"second"
