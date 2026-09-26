"""A remote bootstrap pins the first commit and then follows the default branch.

The checkout used to stay detached, and the nightly update skips a detached head, so a vault
installed the advertised remote way never updated.

Research: `docs/research/2026-09-17-a-verified-first-install-then-follows-main.md`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for entry in (str(TESTS), str(ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import self_update  # noqa: E402
from slow_machine import LONG_TIMEOUT  # noqa: E402
from test_installer_bootstrap import (  # noqa: E402
    _bash,
    _powershell_functions,
    _pwsh,
    _shell_function,
)

INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")
needs_bash = pytest.mark.skipif(_bash() is None, reason="bash is not installed")
needs_pwsh = pytest.mark.skipif(_pwsh() is None, reason="PowerShell is not installed")


def _git(directory: Path, *arguments: str) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
    done = subprocess.run(
        ["git", "-C", str(directory), *identity, *arguments], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _commit(upstream: Path, name: str) -> str:
    (upstream / name).write_bytes(b"x\n")
    _git(upstream, "add", name)
    _git(upstream, "commit", "-q", "-m", name)
    return _git(upstream, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A repository that serves a commit by its OID, as the hosting service does."""
    path = tmp_path / "upstream"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    _commit(path, "one")
    return path


def _bash_bootstrap(target: Path, url: str, commit: str) -> int:
    name = "fetch_pinned_checkout"
    script = f"set -euo pipefail\n{_shell_function(INSTALL_SH, name)}\n{name} \"$@\"\n"
    done = subprocess.run(
        [_bash(), "-c", script, name, str(target), url, commit],
        capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
    )
    return done.returncode


def _pwsh_bootstrap(target: Path, url: str, commit: str) -> int:
    command = _powershell_functions(ROOT / "install.ps1", ("Get-PinnedCheckout",)) + (
        f"$ok = Get-PinnedCheckout -Target {json.dumps(str(target))} -Url {json.dumps(url)} "
        f"-Commit {commit} 6>$null 2>$null\n"
        "if ($ok) { exit 0 }\nexit 1\n"
    )
    done = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
    )
    return done.returncode


def _followed(bootstrap, upstream: Path, target: Path, monkeypatch) -> tuple:
    """Pin the second commit, publish a third, and ask the nightly update what it did."""
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root, _extras: True)
    pinned = _commit(upstream, "two")
    code = bootstrap(target, upstream.as_uri(), pinned)
    installed = _git(target, "rev-parse", "HEAD")
    newest = _commit(upstream, "three")
    outcome = self_update.update_checkout(target)
    return (code, installed == pinned, outcome["status"], outcome.get("commit") == newest, (target / "three").is_file())


@needs_bash
def test_a_remote_install_runs_the_pinned_commit_and_then_updates(upstream, tmp_path, monkeypatch) -> None:
    assert _followed(_bash_bootstrap, upstream, tmp_path / "LLM-wiki", monkeypatch) == (0, True, "updated", True, True)


@needs_pwsh
def test_the_windows_installer_follows_the_same_way(upstream, tmp_path, monkeypatch) -> None:
    assert _followed(_pwsh_bootstrap, upstream, tmp_path / "LLM-wiki", monkeypatch) == (0, True, "updated", True, True)


@needs_bash
def test_a_pin_outside_the_default_branch_is_left_where_it_is(upstream, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root, _extras: True)
    _git(upstream, "checkout", "-q", "-b", "side")
    pinned = _commit(upstream, "aside")
    _git(upstream, "checkout", "-q", "main")
    _commit(upstream, "two")
    target = tmp_path / "LLM-wiki"

    code = _bash_bootstrap(target, upstream.as_uri(), pinned)
    outcome = self_update.update_checkout(target)

    assert (code, outcome["status"], _git(target, "rev-parse", "HEAD")) == (0, "skipped", pinned)
