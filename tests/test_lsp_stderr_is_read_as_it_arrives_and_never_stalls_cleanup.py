"""The server's stderr is read as it arrives, and reading it never stalls cleanup.

Two defects had one cause: the drain thread sat in `stream.read(65537)`. That
read returns only after 64 KiB or end-of-file, so nothing a running server
printed was visible; and while it sat there `stream.close()` from cleanup
waited for it, behind a process that had left the group and kept the write end
open. See `docs/research/2026-09-17-lsp-a-server-that-dies-leaves-its-last-words.md`.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from scripts.lsp_process import LspProcess
from tests.slow_machine import SHORT_TIMEOUT

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="a process leaves its group with setsid() on POSIX only"
)

OWNER_NONCE = "c" * 32
ESCAPED_LIFETIME = 2 * SHORT_TIMEOUT

# Prints one line, starts a child that leaves the group holding our stderr, and
# then lives until its stdin ends. Its two arguments are the pid file and how
# long the escaped child lives.
_SERVER = """
import os, subprocess, sys
escaped = "import os, time; os.setsid(); time.sleep(float(" + sys.argv[2] + "))"
child = subprocess.Popen(
    [sys.executable, "-c", escaped],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
)
sys.stderr.write("first words\\n")
sys.stderr.flush()
pid_file = sys.argv[1]
with open(pid_file + ".tmp", "w") as handle:
    handle.write(str(child.pid))
os.replace(pid_file + ".tmp", pid_file)
sys.stdin.buffer.read()
"""


def _start(tmp_path: Path) -> tuple[LspProcess, Path]:
    pid_file = tmp_path / "escaped.pid"
    process = LspProcess.start(
        [sys.executable, "-c", _SERVER, str(pid_file), str(ESCAPED_LIFETIME)],
        cwd=tmp_path,
        owner_root=tmp_path / OWNER_NONCE,
    )
    return process, pid_file


def _await(condition, what: str) -> None:
    deadline = time.monotonic() + SHORT_TIMEOUT
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen in time")
        time.sleep(0.01)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_escaped(pid_file: Path) -> None:
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_line_the_running_server_printed_is_already_in_the_ring(tmp_path: Path) -> None:
    process, pid_file = _start(tmp_path)
    try:
        _await(lambda: b"first words\n" in process.stderr_bytes(), "the first line")
        assert process.process.poll() is None
    finally:
        process.close(time.monotonic() + SHORT_TIMEOUT)
        _kill_escaped(pid_file)


def test_cleanup_does_not_wait_for_a_process_that_left_the_group(tmp_path: Path) -> None:
    process, pid_file = _start(tmp_path)
    try:
        _await(pid_file.exists, "the escaped child")
        escaped = int(pid_file.read_text())
        process.close(time.monotonic() + SHORT_TIMEOUT)
        still_holding_stderr = _alive(escaped)
        drain = process._coordinator.cleanup_result
        assert (still_holding_stderr, drain.generation_joins) == (True, "success")
    finally:
        _kill_escaped(pid_file)
