"""The threads that run model inference, so a process can settle them before it exits.

A daemon thread inside a PyTorch forward pass when the interpreter finalizes aborts
the process (`terminate called without an active exception`, exit 134): the MCP
server returned while its warm-up was still encoding. Threads started here are
recorded while they run, and `settle` stops and joins them before exit. Standard
library only. See `docs/research/2026-09-14-no-model-running-at-exit.md`.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

_RUNNING: set[threading.Thread] = set()
_LOCK = threading.Lock()
stopping = threading.Event()


def _tracked(target: Callable[[], object]) -> Callable[[], None]:
    def run() -> None:
        try:
            target()
        finally:
            with _LOCK:
                _RUNNING.discard(threading.current_thread())

    return run


def start(target: Callable[[], object], *, name: str) -> threading.Thread:
    """Start `target` on a daemon thread that `settle` will wait for."""
    thread = threading.Thread(target=_tracked(target), name=name, daemon=True)
    with _LOCK:
        _RUNNING.add(thread)
    try:
        thread.start()
    except BaseException:
        with _LOCK:
            _RUNNING.discard(thread)
        raise
    return thread


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
        stopping.clear()
