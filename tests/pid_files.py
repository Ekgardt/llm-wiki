"""A pid file another process never sees half-written, and a bounded wait for it.

A processor killed between `write_text`'s open and its write left an empty pid
file, and the test parsed it (Windows CI run 36023732204, 2026-09-24). See
docs/research/2026-09-24-a-pid-file-read-before-it-is-written.md.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

POLL_SECONDS = 0.05


def write_pid(path: str | Path, pid: int) -> None:
    """Write the pid to a temporary name, then rename it into place atomically."""
    target = Path(path)
    staged = target.with_name(target.name + ".tmp")
    staged.write_text(str(pid), encoding="ascii")
    os.replace(staged, target)


def _whole_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def read_pid(path: str | Path, timeout: float) -> int:
    """The pid in `path`, waiting up to `timeout` seconds for it to appear."""
    target = Path(path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _whole_pid(target)
        if pid is not None:
            return pid
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"no pid appeared in {target} within {timeout} s")
