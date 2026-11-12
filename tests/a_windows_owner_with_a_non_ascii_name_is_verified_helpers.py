"""Helpers shared by test_a_windows_owner_with_a_non_ascii_name_is_verified.py and the test files importing them."""
from __future__ import annotations

import subprocess

from llm_wiki import windows_acl

OWNER = "PC\\Оператор"


def _icacls_writing(monkeypatch, listing: str, code_page: str) -> None:
    def run(command):
        output = listing.encode(code_page) if len(command) == 2 else b""
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(windows_acl, "windows_acl_identity", lambda: OWNER)
    monkeypatch.setattr(windows_acl, "run_acl_command", run)
    monkeypatch.setattr(
        windows_acl,
        "acl_output_encodings",
        lambda: ("cp437", code_page),
        raising=False,
    )
