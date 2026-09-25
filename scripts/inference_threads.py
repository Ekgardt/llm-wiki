"""The threads that run model inference, so a process can settle them before it exits.

A daemon thread inside a PyTorch forward pass when the interpreter finalizes aborts
the process (`terminate called without an active exception`, exit 134): the MCP
server returned while its warm-up was still encoding. Threads started here are
recorded while they run, and `settle` stops and joins them before exit. Standard
library only. See `docs/research/2026-09-14-no-model-running-at-exit.md`.
"""
from __future__ import annotations

import atexit
import threading
import time
from collections.abc import Callable

_RUNNING: set[threading.Thread] = set()
_LOCK = threading.Lock()
stopping = threading.Event()
# Every process that starts inference settles it at exit, not only the server
# (audit C-18, docs/research/2026-09-25-every-process-settles-its-inference-at-exit.md).
EXIT_SETTLE_SECONDS = 30.0
_EXIT_HANDLER: list[bool] = []


def _tracked(target: Callable[[], object]) -> Callable[[], None]:
    def run() -> None:
        try:
            target()
        finally:
            _forget(threading.current_thread())

    return run


def _forget(thread: threading.Thread) -> None:
    """Stop tracking a thread; when it was the last, a drain in progress is over."""
    with _LOCK:
        _RUNNING.discard(thread)
        if not _RUNNING:
            stopping.clear()


def start(target: Callable[[], object], *, name: str) -> threading.Thread:
    """Start `target` on a daemon thread that `settle` will wait for."""
    thread = threading.Thread(target=_tracked(target), name=name, daemon=True)
    with _LOCK:
        _RUNNING.add(thread)
        _register_exit_settle()
    try:
        thread.start()
    except BaseException:
        _forget(thread)
        raise
    return thread


def _register_exit_settle() -> None:
    """Once per process; called under `_LOCK`."""
    if _EXIT_HANDLER:
        return
    atexit.register(settle, EXIT_SETTLE_SECONDS)
    _EXIT_HANDLER.append(True)


def running() -> list[threading.Thread]:
    with _LOCK:
        return [thread for thread in _RUNNING if thread.is_alive()]


def settle(timeout_seconds: float) -> list[str]:
    """Ask inference to stop at its next safe point and wait; the names still running."""
    stopping.set()
    deadline = time.monotonic() + timeout_seconds
    try:
        for thread in running():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return sorted(thread.name for thread in running())
    finally:
        # A drain is a moment, not a state: the next server in this process warms.
        # A thread still running keeps being asked to stop; its end clears the flag.
        # See `docs/research/2026-09-14-every-budget-inside-its-step.md`.
        _clear_when_idle()


def _clear_when_idle() -> None:
    with _LOCK:
        if not any(thread.is_alive() for thread in _RUNNING):
            stopping.clear()
