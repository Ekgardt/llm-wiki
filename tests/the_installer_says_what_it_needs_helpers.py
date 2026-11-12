"""Helpers shared by test_the_installer_says_what_it_needs.py and the test files importing them."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.installer_bootstrap_helpers import _bash, _pwsh, _shell_function

TESTS = Path(__file__).resolve().parent


ROOT = TESTS.parent


INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")


INSTALL_PS1 = (ROOT / "install.ps1").read_text(encoding="utf-8")


needs_bash = pytest.mark.skipif(_bash() is None, reason="bash is not installed")


needs_pwsh = pytest.mark.skipif(_pwsh() is None, reason="PowerShell is not installed")


_HELPERS = {"fetch_pinned_checkout": ("pinned_fetch", "pinned_branch")}


UV_STUB_SH = (
    "uv() {\n"
    "  while [[ $# -gt 0 && $1 != python ]]; do shift; done\n"
    '  local helper="$TEST_SCRIPTS/${2##*/}"\n'
    "  shift 2\n"
    '  command "$TEST_PYTHON" "$helper" "$@"\n'
    "}\n"
)


UV_STUB_PS1 = (
    "function uv {\n"
    "    $at = [array]::IndexOf($args, 'python')\n"
    "    $helper = Join-Path $env:TEST_SCRIPTS (Split-Path -Leaf $args[$at + 1])\n"
    "    & $env:TEST_PYTHON $helper @($args | Select-Object -Skip ($at + 2))\n"
    "}\n"
)


STUB_ENV = {**os.environ, "TEST_PYTHON": sys.executable, "TEST_SCRIPTS": str(TESTS.parent / "scripts")}


def _call(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    functions = "\n".join(_shell_function(INSTALL_SH, part) for part in (*_HELPERS.get(name, ()), name))
    script = f"set -euo pipefail\n{UV_STUB_SH}{functions}\n{name} \"$@\"\n"
    return subprocess.run(
        [_bash(), "-c", script, name, *arguments], capture_output=True, text=True, check=False, env=STUB_ENV
    )
