"""The installers name the branch the nightly follows, the checks behind a warning, and the smoke's own bound.

They promised a fast-forward of whatever branch was checked out while the update
follows only the default branch; a fresh install ended "with warnings" and the
sync's doctor line said only "requires attention"; and the smoke was always given
120 s, so raising LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS could not give it longer.
See docs/research/2026-09-27-the-installers-say-which-branch-and-which-warning.md.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from self_update import update_target_note
from sync_memory import attention_summary, final_doctor_message

from tests.installer_bootstrap_helpers import _bash
from tests.shell_function_source import _shell_function
from tests.slow_machine import LONG_TIMEOUT
from tests.the_installer_says_what_it_needs_helpers import (
    INSTALL_PS1,
    INSTALL_SH,
    _call,
    needs_bash,
)

IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def _git(directory: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(directory), *IDENTITY, *arguments], check=True, capture_output=True, timeout=LONG_TIMEOUT)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A checkout of a remote whose default branch is `main`, as an installed vault is."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "commit", "-q", "--allow-empty", "-m", "one")
    _git(origin, "branch", "work")
    subprocess.run(["git", "clone", "-q", str(origin), str(tmp_path / "vault")], check=True, timeout=LONG_TIMEOUT)
    return tmp_path / "vault"


def test_the_default_branch_is_the_one_named(clone: Path) -> None:
    assert update_target_note(clone) == "nightly fast-forward of main, the default branch of origin"


def test_another_branch_is_told_it_is_not_followed(clone: Path) -> None:
    _git(clone, "checkout", "-q", "work")

    note = update_target_note(clone)

    assert note.startswith("none while work is checked out") and "fast-forward" not in note


def test_a_detached_checkout_is_told_it_is_pinned(clone: Path) -> None:
    _git(clone, "checkout", "-q", "--detach")

    assert update_target_note(clone).startswith("none - this checkout is pinned")


@needs_bash
def test_install_sh_prints_what_the_update_code_decides(clone: Path) -> None:
    _git(clone, "checkout", "-q", "work")

    assert _call("code_update_note", str(clone)).stdout.strip() == update_target_note(clone)


def test_neither_installer_promises_the_checked_out_branch() -> None:
    promise = "fast-forward of the checked-out branch"

    assert [promise in text for text in (INSTALL_SH, INSTALL_PS1)] == [False, False]


def test_a_final_doctor_warning_names_its_checks() -> None:
    report = {
        "checks": [
            {"id": "queue", "status": "ok", "message": "fine"},
            {"id": "backup", "status": "degraded", "message": "No knowledge snapshot has been taken yet."},
            {"id": "scheduler", "status": "skipped", "message": "Nightly maintenance status is unknown."},
        ]
    }

    message = final_doctor_message("skipped", attention_summary(report))

    assert message == (
        "Final doctor check requires attention - backup: No knowledge snapshot has been taken yet.; "
        "scheduler: Nightly maintenance status is unknown."
    )


def test_both_installers_say_where_the_warning_came_from() -> None:
    pointer = "For the state now: uv run --locked --no-sync python scripts/doctor.py"

    assert [pointer in text for text in (INSTALL_SH, INSTALL_PS1)] == [True, True]


@needs_bash
@pytest.mark.parametrize(("bound", "deadline"), [("180", "120"), ("600", "400"), ("5", "4")])
def test_the_smoke_deadline_follows_the_configured_bound(bound: str, deadline: str) -> None:
    script = "\n".join([_shell_function(INSTALL_SH, "read_smoke_bound"), "read_smoke_bound", 'echo "$smokeDeadlineSeconds"'])

    result = subprocess.run(
        [_bash(), "-c", script], capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
        env={"LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS": bound},
    )

    assert result.stdout.strip() == deadline


def test_neither_installer_fixes_the_smoke_deadline() -> None:
    fixed = "install_smoke.py --deadline-seconds 120"

    assert [fixed in text for text in (INSTALL_SH, INSTALL_PS1)] == [False, False]
