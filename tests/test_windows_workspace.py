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
