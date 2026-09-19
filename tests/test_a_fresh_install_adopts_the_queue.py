"""The installer reads `fresh` from a check that exits 1, and the Windows installer adopts too.

Under `pipefail` the fallback after the pipe fired although the parser had answered, the
state became two lines, and no fresh install ever adopted the queue. Research:
`docs/research/2026-09-17-a-fresh-install-adopts-the-queue.md`.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_installer_bootstrap import _bash, _shell_function  # noqa: E402

ROOT = TESTS.parent
FAKE_UV = "#!/bin/sh\nprintf '%s' \"$FAKE_REPORT\"\nexit \"$FAKE_EXIT\"\n"


def _state(tmp_path: Path, report: str, exit_code: int) -> str:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    function = _shell_function((ROOT / "install.sh").read_text(encoding="utf-8"), "adoption_state_of")
    script = f"set -euo pipefail\nVAULT_ROOT=/nowhere\nADOPTION_ERR={tmp_path / 'err.log'}\n{function}\nadoption_state_of\n"
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
           "FAKE_REPORT": report, "FAKE_EXIT": str(exit_code)}
    result = subprocess.run([_bash(), "-c", script], capture_output=True, text=True, env=env, check=False)
    return result.stdout


@pytest.mark.skipif(_bash() is None, reason="bash is not installed")
@pytest.mark.parametrize(
    ("report", "exit_code", "expected"),
    [
        ('{"details": {"adoption_state": "fresh"}}', 1, "fresh\n"),
        ('{"details": {"adoption_state": "adopted"}}', 0, "adopted\n"),
        ("not json", 1, "unknown\n"),
    ],
)
def test_the_state_is_one_line_whatever_the_check_exited_with(tmp_path, report, exit_code, expected) -> None:
    assert _state(tmp_path, report, exit_code) == expected


def test_the_windows_installer_has_the_adoption_step() -> None:
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert ("--adopt-ownership-v3" in text, "function Get-AdoptionState" in text) == (True, True)
