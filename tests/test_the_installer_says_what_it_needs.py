"""The installers name what they need, take back what they left, and report what they did.

Research: `docs/research/2026-09-17-the-installer-says-what-it-needs-and-what-it-did.md`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.the_installer_says_what_it_needs_helpers import TESTS

if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from powershell_literal import ps_literal  # noqa: E402

from tests.installer_bootstrap_helpers import (  # noqa: E402
    _bash,  # noqa: E402
    _powershell_functions,
    _pwsh,
)
from tests.the_installer_says_what_it_needs_helpers import (
    INSTALL_PS1,
    INSTALL_SH,
    ROOT,
    STUB_ENV,
    UV_STUB_PS1,
    _call,
    needs_bash,
    needs_pwsh,
)

# The helpers a function under test calls, loaded with it.


# `uv run ... python <vault>/scripts/<helper> <args>` becomes this Python running the
# repository's own helper: the checkouts under test are bare git trees, not vaults.


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def test_the_installer_holds_no_construct_the_bash_of_macos_lacks() -> None:
    """bash 3.2: no `mapfile`, and an array that may be empty is expanded with the `+` form.

    Research: `docs/research/2026-09-17-the-installer-runs-on-the-bash-macos-ships.md`.
    """
    newer = ("mapfile", "readarray", "declare -A", '  "${IDE_HOOK_ARGS[@]}"')
    found = [word for word in newer for line in _code_lines(INSTALL_SH) if word in line]

    assert found == []


@needs_bash
def test_an_empty_array_in_the_plus_form_is_no_argument_under_nounset() -> None:
    script = 'set -euo pipefail\nEMPTY=()\nset -- before ${EMPTY[@]+"${EMPTY[@]}"} after\necho "$#"\n'

    result = subprocess.run([_bash(), "-c", script], capture_output=True, text=True, check=False)

    assert (result.returncode, result.stdout.strip()) == (0, "2")


@pytest.mark.parametrize("text", [INSTALL_SH, INSTALL_PS1], ids=["install.sh", "install.ps1"])
def test_every_uv_run_is_pinned_to_the_lock(text: str) -> None:
    unpinned = [line.strip() for line in text.splitlines() if "uv run" in line and "uv run --locked --no-sync" not in line]

    assert unpinned == []


# That an interrupted adoption (`partial`) is resumed is now asked of the installers' own
# plan function: `tests/test_the_installer_does_not_vouch_for_agents_it_cannot_see.py`.


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


# What the Claude entry reads as, in both installers, is asked of
# tests/test_both_installers_read_the_claude_file_alike.py.


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
    command = UV_STUB_PS1 + _powershell_functions(ROOT / "install.ps1", names) + (
        f"$fetched = Get-PinnedCheckout -Target {ps_literal(str(target))} "
        f"-Url {ps_literal(str(tmp_path / 'no-such-repository'))} -Commit {'a' * 40} 6>$null 2>$null\n"
        f"$note = Get-CodeUpdateNote {ps_literal(str(checkout))}\n"
        f"$advice = Get-ExistingTargetAdvice {ps_literal(str(checkout))}\n"
        "ConvertTo-Json -Compress @([bool]$fetched, $note.Contains('pinned'), $advice.Contains('move it away'))\n"
    )

    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=120, check=False, env=STUB_ENV,
    )

    assert result.returncode == 0, result.stderr
    assert (json.loads(result.stdout.splitlines()[-1]), target.exists()) == ([False, False, True], False)
