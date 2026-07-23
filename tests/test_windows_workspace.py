"""Focused tests for Windows handle-relative mutable-file operations."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import windows_workspace


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
