"""The POSIX installer reads its arguments through small functions, and reads them as before.

The argument loop used to run in the installer's body, outside every function, where
no complexity ceiling measured it. These cases run the installer's own preamble and
parsing functions, so each accepted spelling and each refusal is pinned to its words.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.shell_function_source import _shell_functions
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")
PARSING = (
    "take_option_value", "set_installer_flag", "read_installer_argument",
    "parse_installer_arguments", "missing_value_message", "validate_installer_arguments",
)
REPORT = (
    'parse_installer_arguments "$@"\n'
    "validate_installer_arguments\n"
    'printf "scheduler=%s image=%s stopped=%s push=%s replace=%s\\n" "$SCHEDULER_MODE" '
    '"$COMPILE_BACKUP_IMAGE" "$AGENTS_STOPPED" "$PROTECT_PUSH" "$REPLACE_CODEX_MCP"\n'
    'printf "adopt=%s\\n" "${ADOPT_ARGS[*]:-}"\n'
    'printf "model=%s\\n" "${MODEL_ARGS[*]:-}"\n'
)

needs_bash = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX installer")


def _read(*arguments: str) -> subprocess.CompletedProcess[str]:
    preamble = INSTALL_SH.split("# This installer runs on bash 3.2", 1)[0]
    script = f"{preamble}\n{_shell_functions(INSTALL_SH, *PARSING)}\n{REPORT}"
    return subprocess.run(
        ["bash", "-c", script, "install.sh", *arguments],
        capture_output=True, text=True, check=False, timeout=LONG_TIMEOUT,
    )


@needs_bash
@pytest.mark.parametrize(("arguments", "expected"), [
    ((), "scheduler=native image= stopped=0 push=0 replace=0\nadopt=\nmodel=\n"),
    (("--scheduler", "cron"), "scheduler=cron image= stopped=0 push=0 replace=0\nadopt=\nmodel=\n"),
    (("--scheduler=cron",), "scheduler=cron image= stopped=0 push=0 replace=0\nadopt=\nmodel=\n"),
    (("--adopt", "a", "--adopt=b"), "scheduler=native image= stopped=0 push=0 replace=0\nadopt=--adopt a --adopt b\nmodel=\n"),
    (("--model", "x", "--model=y"), "scheduler=native image= stopped=0 push=0 replace=0\nadopt=\nmodel=--model y\n"),
    (("--protect-push", "--replace-codex-mcp"), "scheduler=native image= stopped=0 push=1 replace=1\nadopt=\nmodel=\n"),
    (("--compile-backup-image", "/img", "--confirm-all-agents-stopped"),
     "scheduler=native image=/img stopped=1 push=0 replace=0\nadopt=\nmodel=\n"),
    (("--compile-backup-image=/a=b", "--confirm-all-agents-stopped"),
     "scheduler=native image=/a=b stopped=1 push=0 replace=0\nadopt=\nmodel=\n"),
], ids=["none", "scheduler", "scheduler=", "adopt twice", "last model wins", "flags", "image", "image with ="])
def test_each_accepted_spelling_sets_what_it_names(arguments, expected) -> None:
    done = _read(*arguments)

    assert (done.returncode, done.stdout) == (0, expected), done.stderr


@needs_bash
@pytest.mark.parametrize(("arguments", "refusal"), [
    (("--scheduler",), "--scheduler requires native or cron"),
    (("--adopt",), "--adopt requires a resource id"),
    (("--model",), "--model requires a model name"),
    (("--compile-backup-image",), "--compile-backup-image requires an external image directory"),
    (("--compile-backup-image=/img",), "--compile-backup-image requires --confirm-all-agents-stopped"),
    (("--scheduler=hourly",), "--scheduler requires native or cron"),
    (("--scheduler", "--model"), "--scheduler requires native or cron"),
    (("--bogus",), "Unknown installer argument: --bogus"),
    (("--protect-push=1",), "Unknown installer argument: --protect-push=1"),
])
def test_each_refusal_names_its_cause(arguments, refusal) -> None:
    done = _read(*arguments)

    assert done.returncode == 1
    assert refusal in done.stdout
