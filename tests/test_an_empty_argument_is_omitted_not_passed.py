"""The Windows installer never hands a native command an empty argument.

Windows PowerShell 5.1 drops `""` on the way to a native command, so
`--environment ""` reached argparse as a bare `--environment` and the install
stopped. See docs/research/2026-09-25-an-empty-argument-is-omitted-not-passed.md.
"""

from __future__ import annotations

import re
from pathlib import Path

INSTALLER = (Path(__file__).resolve().parent.parent / "install.ps1").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = INSTALLER.index(f"function {name} {{")
    return INSTALLER[start : INSTALLER.index("\n}\n", start)]


def test_the_native_command_runner_refuses_an_empty_argument() -> None:
    assert "IsNullOrEmpty($_)" in _function("Invoke-NativeCommand")


def test_no_argument_is_an_environment_variable_cast_to_a_possibly_empty_string() -> None:
    assert re.findall(r"\[string\]\$env:\w+", INSTALLER) == []


def test_the_environment_flag_is_added_only_with_a_value() -> None:
    guarded = INSTALLER.index("if (-not [string]::IsNullOrEmpty($env:UV_PROJECT_ENVIRONMENT))")

    assert INSTALLER.index('"--environment"', guarded) > guarded
