"""`icacls` writes its pipe in a code page; an owner's name need not be ASCII.

The process and the code page are the operating-system boundary and are
replaced here; nothing else is.
See docs/research/2026-09-17-windows-names-and-windows-errors-are-read-as-they-are-written.md.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import markdown_transaction  # noqa: E402

OWNER = "PC\\Оператор"


def _icacls_writing(monkeypatch, listing: str, code_page: str) -> None:
    def run(command):
        output = listing.encode(code_page) if len(command) == 2 else b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(markdown_transaction, "_windows_acl_identity", lambda: OWNER)
    monkeypatch.setattr(markdown_transaction, "_run_acl_command", run)
    monkeypatch.setattr(
        markdown_transaction,
        "_acl_output_encodings",
        lambda: ("cp437", code_page),
        raising=False,
    )


def test_an_owner_named_in_cyrillic_is_verified(tmp_path, monkeypatch):
    path = tmp_path / "artifact"
    path.write_bytes(b"artifact")
    _icacls_writing(monkeypatch, f"artifact {OWNER}:(F)\n", "cp866")

    assert markdown_transaction._harden_windows_acl(path) is None


def test_a_second_entry_is_still_refused_under_that_code_page(tmp_path, monkeypatch):
    path = tmp_path / "artifact"
    path.write_bytes(b"artifact")
    listing = f"artifact {OWNER}:(F)\n         PC\\Гость:(R)\n"
    _icacls_writing(monkeypatch, listing, "cp866")

    with pytest.raises(PermissionError, match="owner-only ACL"):
        markdown_transaction._harden_windows_acl(path)
