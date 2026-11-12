"""Helpers shared by test_a_dead_owner_root_is_swept_when_the_next_server_starts.py and the test files importing them."""
from __future__ import annotations

import subprocess
import sys

from process_liveness import process_start_identity

from tests.slow_machine import SHORT_TIMEOUT


def finished_process() -> tuple[int, str]:
    """A process identifier this machine has finished with and reaped."""
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    identity = process_start_identity(child.pid)
    child.communicate(timeout=SHORT_TIMEOUT)
    assert identity is not None
    return child.pid, identity
