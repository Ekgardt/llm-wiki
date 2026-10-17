"""The nightly code update keeps installed extras and says why a fetch failed.

`uv sync --locked --no-dev` would have uninstalled 93 packages from the live
environment on the first night that pulled a commit. Research:
`docs/research/2026-09-14-an-update-that-keeps-what-is-installed.md`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import self_update  # noqa: E402


def test_only_a_sync_that_names_every_extra_is_exact():
    """The update names every extra and group the vault has, so it may remove the rest;
    `sync_memory`'s baseline step names none, so it must stay inexact
    (docs/research/2026-09-29-a-sync-removes-what-the-lock-no-longer-names.md)."""
    import sync_memory

    assert ("--inexact" in self_update.BASELINE_SYNC_COMMAND, "--inexact" in sync_memory._SYNC_STEP.command) == (
        False,
        True,
    )


def test_a_failed_fetch_names_what_git_said(tmp_path, monkeypatch):
    def run(command, *, cwd, timeout):
        return subprocess.CompletedProcess(command, 128, "", "fatal: unable to access 'https://x/': Could not resolve host\n")

    monkeypatch.setattr(self_update, "_run", run)

    failure = self_update._fetch_failure(tmp_path, "origin", "work")

    assert failure == "fatal: unable to access 'https://x/': Could not resolve host"
