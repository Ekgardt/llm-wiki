"""The installers name what they need, take back what they left, and report what they did.

Research: `docs/research/2026-09-17-the-installer-says-what-it-needs-and-what-it-did.md`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_installer_bootstrap import (  # noqa: E402
    _bash,
    _powershell_functions,
    _pwsh,
    _shell_function,
)

ROOT = TESTS.parent
INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")
INSTALL_PS1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
needs_bash = pytest.mark.skipif(_bash() is None, reason="bash is not installed")
needs_pwsh = pytest.mark.skipif(_pwsh() is None, reason="PowerShell is not installed")


def _call(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    script = f"set -euo pipefail\n{_shell_function(INSTALL_SH, name)}\n{name} \"$@\"\n"
    return subprocess.run(
        [_bash(), "-c", script, name, *arguments], capture_output=True, text=True, check=False
    )


@needs_bash
@pytest.mark.parametrize(
    ("major", "minor", "accepted"),
    [("3", "2", False), ("4", "3", False), ("4", "4", True), ("5", "0", True)],
)
def test_a_shell_older_than_the_installer_needs_is_refused(major, minor, accepted) -> None:
    assert (_call("bash_runs_this_installer", major, minor).returncode == 0) is accepted


def test_the_shell_is_asked_before_the_first_construct_it_lacks() -> None:
    guard = INSTALL_SH.index('bash_runs_this_installer "${SHELL_VERSION%%.*}"')

    assert guard < min(INSTALL_SH.index("mapfile -t"), INSTALL_SH.index('for argument in "$@"'))


@pytest.mark.parametrize("text", [INSTALL_SH, INSTALL_PS1], ids=["install.sh", "install.ps1"])
def test_every_uv_run_is_pinned_to_the_lock(text: str) -> None:
    unpinned = [line.strip() for line in text.splitlines() if "uv run" in line and "uv run --locked --no-sync" not in line]

    assert unpinned == []


def _names_the_adoptable_states(line: str) -> bool:
    if line.lstrip().startswith("#"):
        return False
    return "upgrade-required" in line and "fresh" in line


@pytest.mark.parametrize("text", [INSTALL_SH, INSTALL_PS1], ids=["install.sh", "install.ps1"])
def test_an_interrupted_adoption_is_resumed(text: str) -> None:
    adoptable = list(filter(_names_the_adoptable_states, text.splitlines()))

    assert ["partial" in line for line in adoptable] == [True]


@needs_bash
def test_a_failed_fetch_leaves_nothing_behind(tmp_path: Path) -> None:
    target = tmp_path / "LLM-wiki"

    result = _call("fetch_pinned_checkout", str(target), str(tmp_path / "no-such-repository"), "a" * 40)

    assert (result.returncode, target.exists()) == (1, False)


@needs_bash
def test_an_existing_checkout_is_named_with_the_way_forward(tmp_path: Path) -> None:
    (tmp_path / "install.sh").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    empty = tmp_path / "other"
    empty.mkdir()

    advice = (_call("existing_target_advice", str(tmp_path)).stdout, _call("existing_target_advice", str(empty)).stdout)

    assert (f'bash "{tmp_path}/install.sh"' in advice[0], "move it away" in advice[1]) == (True, True)


def _claude_config(tmp_path: Path, servers: dict[str, object] | None) -> Path:
    path = tmp_path / "claude.json"
    if servers is not None:
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


@needs_bash
@pytest.mark.parametrize(
    ("servers", "expected"),
    [
        (None, "missing"),
        ({}, "absent"),
        ({"llm-wiki": {"args": ["run", "--directory", "/vault", "python"]}}, "current"),
        ({"llm-wiki": {"args": ["run", "--directory", "/old-vault", "python"]}}, "elsewhere"),
    ],
)
def test_the_claude_entry_is_read_not_assumed(tmp_path, servers, expected) -> None:
    result = _call("claude_mcp_state", str(_claude_config(tmp_path, servers)), "/vault")

    assert result.stdout.strip() == expected


@needs_bash
def test_an_unreadable_claude_file_is_not_called_active(tmp_path: Path) -> None:
    broken = tmp_path / "claude.json"
    broken.write_text("{not json", encoding="utf-8")

    state = _call("claude_mcp_state", str(broken), "/vault").stdout.strip()

    assert (state, "active automatic" in _call("claude_status_line", state).stdout) == ("unreadable", False)


def _git(directory: Path, *arguments: str) -> None:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
    subprocess.run(["git", "-C", str(directory), *identity, *arguments], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "one")
    return tmp_path


@needs_bash
def test_a_pinned_checkout_is_told_it_will_not_update(checkout: Path) -> None:
    on_branch = _call("code_update_note", str(checkout)).stdout
    _git(checkout, "checkout", "-q", "--detach")

    pinned = _call("code_update_note", str(checkout)).stdout

    assert ("pinned" in on_branch, "pinned" in pinned) == (False, True)


@needs_pwsh
def test_the_windows_installer_says_the_same(checkout: Path, tmp_path: Path) -> None:
    names = ("Get-PinnedCheckout", "Get-ExistingTargetAdvice", "Get-CodeUpdateNote")
    target = tmp_path / "LLM-wiki"
    command = _powershell_functions(ROOT / "install.ps1", names) + (
        f"$fetched = Get-PinnedCheckout -Target {json.dumps(str(target))} "
        f"-Url {json.dumps(str(tmp_path / 'no-such-repository'))} -Commit {'a' * 40} 6>$null 2>$null\n"
        f"$note = Get-CodeUpdateNote {json.dumps(str(checkout))}\n"
        f"$advice = Get-ExistingTargetAdvice {json.dumps(str(checkout))}\n"
        "ConvertTo-Json -Compress @([bool]$fetched, $note.Contains('pinned'), $advice.Contains('move it away'))\n"
    )

    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=120, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (json.loads(result.stdout.splitlines()[-1]), target.exists()) == ([False, False, True], False)
