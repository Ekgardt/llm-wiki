"""Audit 3, B7: a deadline that passes mid-read is a timeout, not a damaged install.

Research: `docs/research/2026-09-17-a-slow-read-is-not-a-corrupt-install.md`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import lsp_identity  # noqa: E402
from lsp_profiles import GOPLS_PROFILE  # noqa: E402


def _expired() -> float:
    return time.monotonic() - 1


def test_a_server_read_past_its_deadline_is_a_timeout(tmp_path: Path) -> None:
    server = tmp_path / "server"
    server.write_bytes(b"binary")
    with pytest.raises(TimeoutError):
        lsp_identity._digest_of(server, 1024, _expired())


def test_a_manifest_read_past_its_deadline_is_a_timeout(tmp_path: Path) -> None:
    (tmp_path / lsp_identity.INSTALL_MANIFEST_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(TimeoutError):
        lsp_identity._manifest_codes(GOPLS_PROFILE, tmp_path, "", "", _expired())


def test_the_real_read_failures_keep_their_names(tmp_path: Path) -> None:
    server = tmp_path / "server"
    server.write_bytes(b"binary")
    deadline = time.monotonic() + 10
    reasons = (
        lsp_identity._digest_of(tmp_path / "absent", 1024, deadline)[1],
        lsp_identity._digest_of(server, 2, deadline)[1],
        lsp_identity._digest_of(server, 1024, deadline)[1],
    )
    assert reasons == ("missing", "oversized", "")
