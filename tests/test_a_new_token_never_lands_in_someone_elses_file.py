"""Audit 3, B6: minting a token never writes through what already sits at its path.

Research: `docs/research/2026-09-17-a-new-token-never-lands-in-someone-elses-file.md`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_http  # noqa: E402


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_link_at_the_token_path_is_replaced_not_followed(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("", encoding="utf-8")
    # Private and empty: the one shape that passes the reuse check and still
    # yields no token, so a new one is minted at the linked path.
    victim.chmod(0o600)
    token_path = tmp_path / "mcp-http" / "token"
    token_path.parent.mkdir()
    token_path.symlink_to(victim)

    token = mcp_http.ensure_token(token_path)

    found = (
        victim.read_text(encoding="utf-8"),
        token_path.is_symlink(),
        token_path.read_text(encoding="utf-8").strip() == token,
    )
    assert found == ("", False, True)


def test_a_minted_token_is_private_reused_and_leaves_no_scratch(tmp_path: Path) -> None:
    token_path = tmp_path / "mcp-http" / "token"
    first = mcp_http.ensure_token(token_path)
    second = mcp_http.ensure_token(token_path)
    mode = token_path.stat().st_mode & 0o777 if os.name == "posix" else 0o600
    leftovers = sorted(entry.name for entry in token_path.parent.iterdir())
    assert (first == second, mode, leftovers) == (True, 0o600, ["token"])
