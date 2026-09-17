"""Every reader of an `icacls` listing reads it under the owner's code page.

The archive seal and the queue's owner-only check still decoded the listing
under one code page, so an owner whose name is not ASCII failed verification
they were meant to pass. Same class as Q-M20, other instances. Research:
`docs/research/2026-09-17-the-last-three-windows-readers-of-a-name-and-a-handle.md`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import archive_daily  # noqa: E402
import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402

OWNER = "PC\\Оператор"
CODE_PAGE = "cp866"


def _icacls_writing(monkeypatch, listing: str) -> None:
    """The boundary: a process that writes its pipe in the OEM code page."""

    def run(command):
        output = listing.encode(CODE_PAGE) if len(command) == 2 else b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(markdown_transaction, "_windows_acl_identity", lambda: OWNER)
    monkeypatch.setattr(markdown_transaction, "_run_acl_command", run)
    monkeypatch.setattr(archive_daily, "_run_acl_command", run)
    monkeypatch.setattr(
        markdown_transaction,
        "_acl_output_encodings",
        lambda: ("cp437", CODE_PAGE),
        raising=False,
    )


def test_the_archive_seal_verifies_an_owner_named_in_cyrillic(tmp_path, monkeypatch):
    path = tmp_path / "archive"
    path.write_bytes(b"archive")
    _icacls_writing(monkeypatch, f"archive {OWNER}:(R)\n")
    monkeypatch.setattr(archive_daily, "_windows_acl_identity", lambda: OWNER)

    assert archive_daily.DailyArchiver._windows_read_only_acl(path) is None


def test_the_archive_seal_still_refuses_a_second_principal(tmp_path, monkeypatch):
    path = tmp_path / "archive"
    path.write_bytes(b"archive")
    _icacls_writing(monkeypatch, f"archive {OWNER}:(R)\n         PC\\Гость:(R)\n")
    monkeypatch.setattr(archive_daily, "_windows_acl_identity", lambda: OWNER)

    with pytest.raises(PermissionError, match="principal other than"):
        archive_daily.DailyArchiver._windows_read_only_acl(path)


def test_the_archive_seal_still_refuses_a_write_grant(tmp_path, monkeypatch):
    path = tmp_path / "archive"
    path.write_bytes(b"archive")
    _icacls_writing(monkeypatch, f"archive {OWNER}:(M)\n")
    monkeypatch.setattr(archive_daily, "_windows_acl_identity", lambda: OWNER)

    with pytest.raises(PermissionError, match="archive read-only ACL"):
        archive_daily.DailyArchiver._windows_read_only_acl(path)


def test_the_queue_reads_an_owner_only_file_of_a_cyrillic_owner(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    path.write_bytes(b"queue")
    _icacls_writing(monkeypatch, f"queue.sqlite3 {OWNER}:(F)\n")

    assert memory_queue._is_owner_only_windows(path) is True


def test_the_queue_still_reads_a_shared_file_as_shared(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    path.write_bytes(b"queue")
    _icacls_writing(
        monkeypatch, f"queue.sqlite3 {OWNER}:(F)\n              PC\\Гость:(R)\n"
    )

    assert memory_queue._is_owner_only_windows(path) is False
