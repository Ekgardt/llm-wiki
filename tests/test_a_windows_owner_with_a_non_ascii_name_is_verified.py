"""`icacls` writes its pipe in a code page; an owner's name need not be ASCII.

The process and the code page are the operating-system boundary and are
replaced here; nothing else is.
See docs/research/2026-09-17-windows-names-and-windows-errors-are-read-as-they-are-written.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from llm_wiki import windows_acl  # noqa: E402

from tests.a_windows_owner_with_a_non_ascii_name_is_verified_helpers import OWNER, _icacls_writing


def test_an_owner_named_in_cyrillic_is_verified(tmp_path, monkeypatch):
    path = tmp_path / "artifact"
    path.write_bytes(b"artifact")
    _icacls_writing(monkeypatch, f"artifact {OWNER}:(F)\n", "cp866")

    assert windows_acl.harden_windows_acl(path) is None


def test_a_second_entry_is_still_refused_under_that_code_page(tmp_path, monkeypatch):
    path = tmp_path / "artifact"
    path.write_bytes(b"artifact")
    listing = f"artifact {OWNER}:(F)\n         PC\\Гость:(R)\n"
    _icacls_writing(monkeypatch, listing, "cp866")

    with pytest.raises(PermissionError, match="owner-only ACL"):
        windows_acl.harden_windows_acl(path)
