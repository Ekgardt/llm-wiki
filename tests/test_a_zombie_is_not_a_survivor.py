"""A process that is dead but not yet reaped is not a survivor.

`kill -0` answers for a zombie as for a live process, so the installer tests'
survivor checks reported a child the installer had killed whenever the machine
was slow to reap it. See docs/research/2026-09-27-a-zombie-is-not-a-survivor.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration_injection_helpers import _ALIVE_SHELL
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    os.name != "posix" or BASH is None or not hasattr(os, "waitid"),
    reason="needs a POSIX shell and waitid to hold a zombie",
)

# The parent kills its child, then waits with WNOWAIT: the call returns once the child
# has exited and leaves it unreaped, so the zombie exists by a condition, not a sleep.
# It prints the pid and holds the zombie until its stdin closes.
_ZOMBIE_HOLDER = """
import os, signal, subprocess, sys
child = subprocess.Popen(["sleep", "300"])
os.kill(child.pid, signal.SIGKILL)
os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
print(child.pid, flush=True)
sys.stdin.read()
"""


def _alive(pid: int) -> str:
    script = _ALIVE_SHELL + f'if alive {pid}; then echo alive; else echo dead; fi\n'
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=LONG_TIMEOUT, check=True)
    return result.stdout.strip()


def test_a_zombie_is_dead_although_kill_zero_accepts_it() -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", _ZOMBIE_HOLDER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        zombie = int(holder.stdout.readline())
        os.kill(zombie, 0)
        assert _alive(zombie) == "dead"
    finally:
        holder.communicate(timeout=LONG_TIMEOUT)


def test_a_running_process_is_alive_and_a_vanished_one_is_not() -> None:
    process = subprocess.Popen(["sleep", "300"])
    try:
        assert _alive(process.pid) == "alive"
    finally:
        process.kill()
        process.wait(timeout=LONG_TIMEOUT)
    assert _alive(process.pid) == "dead"


def test_no_survivor_check_in_the_installer_tests_uses_bare_kill_zero() -> None:
    source = (ROOT / "tests" / "test_integration_injection.py").read_text(encoding="utf-8")
    outside_the_helper = source.replace(_ALIVE_SHELL, "")
    assert 'kill -0 "$' not in outside_the_helper
