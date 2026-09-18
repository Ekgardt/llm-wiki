"""The processor child's `R` means "the task is done", and its tree is read then.

The audit read `_child_ready_handshake` as a start-up ping and concluded that the
tracked set is always empty. It is not: `_processor_child_entry` evaluates
`_processor_result_frame` — the whole task — before `R` is sent, so the tree
recorded on that signal is the one the finished task left running. This test pins
that ordering, because moving `R` earlier would empty the set and leave
`_cleanup_confirmed` and `_await_cleanup` verifying nothing.

See `docs/research/2026-09-18-a-childs-tree-is-read-when-it-is-killed.md`.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import memory_queue  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def _leaves_a_grandchild(task: dict) -> bool:
    """A processor that shells out and returns without waiting, as a CLI one does."""
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(task["payload"]["pid_path"]).write_text(str(child.pid), encoding="ascii")
    return True


def _started_child(task: dict) -> memory_queue._ChildRun:
    """One processor child, started and handshaken exactly as the worker does."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=True)
    process = context.Process(
        target=memory_queue._processor_child_entry,
        args=(sender, _leaves_a_grandchild, task),
        daemon=False,
    )
    run = memory_queue._ChildRun(
        process, receiver, time.monotonic() + SHORT_TIMEOUT * 2
    )
    process.start()
    sender.close()
    memory_queue._child_ready_handshake(run)
    return run


def _kill(pid: int) -> None:
    """Leave nothing of this test behind, whatever the assertion said."""
    if not memory_queue._pid_is_alive(pid):
        return
    if os.name == "nt":
        memory_queue._taskkill(pid)
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS cannot enumerate a tree")
def test_the_tree_read_at_the_signal_holds_what_the_task_left_running(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "grandchild.pid"
    run = _started_child({"payload": {"pid_path": str(pid_path)}})
    grandchild = int(pid_path.read_text(encoding="ascii"))

    tracked = run.tracked_descendants

    try:
        run.stop()
        run.receiver.close()
        assert (grandchild in tracked, memory_queue._pid_is_alive(grandchild)) == (
            True,
            False,
        )
    finally:
        _kill(grandchild)
