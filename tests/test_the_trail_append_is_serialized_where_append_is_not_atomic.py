"""Where an append is a seek and then a write, the trail's line is written under a lock.

The locking module is a parameter, so the order and the bounded give-up are
proved on every system with a stand-in for `msvcrt`.

Research: `docs/research/2026-09-17-five-failures-only-the-other-systems-showed.md`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import capture_diagnostics  # noqa: E402

# Untranslated, as the product opens it: text mode would write `\r\n` on Windows.
APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)


class _Locking:
    """What `msvcrt` offers here: `locking(fd, mode, nbytes)` at the file pointer."""

    LK_NBLCK = "lock"
    LK_UNLCK = "unlock"

    def __init__(self, trail: Path, busy_for: int = 0) -> None:
        self.trail = trail
        self.busy_for = busy_for
        self.events: list[tuple] = []

    def locking(self, descriptor: int, mode: str, nbytes: int) -> None:
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        self.events.append((mode, position, nbytes, self.trail.stat().st_size))
        if mode == self.LK_NBLCK and len(self.events) <= self.busy_for:
            raise PermissionError(13, "the byte is locked by another writer")


@pytest.fixture
def trail(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "logs" / "capture-failures.jsonl"
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", path)
    return path


def _append_with(locking: _Locking, monkeypatch) -> bool:
    monkeypatch.setattr(capture_diagnostics, "_append_locking", lambda: locking)
    return capture_diagnostics._append_failure_line({"reason": "second"})


def test_the_line_is_written_between_the_lock_and_the_unlock(trail, monkeypatch):
    trail.parent.mkdir()
    trail.write_bytes(b'{"reason": "first"}\n')
    locking = _Locking(trail)
    offset = capture_diagnostics.TRAIL_LOCK_OFFSET

    written = _append_with(locking, monkeypatch)

    # Sizes seen: 20 bytes when the lock is taken, the new line there at unlock.
    assert (written, locking.events) == (
        True,
        [("lock", offset, 1, 20), ("unlock", offset, 1, trail.stat().st_size)],
    )
    assert trail.stat().st_size > 20


def test_the_line_lands_at_the_end_and_not_at_the_lock_byte(trail, monkeypatch):
    trail.parent.mkdir()
    trail.write_bytes(b'{"reason": "first"}\n')

    _append_with(_Locking(trail), monkeypatch)

    lines = trail.read_bytes().splitlines()
    assert [json.loads(line)["reason"] for line in lines] == ["first", "second"]


def test_a_lock_that_frees_up_is_waited_for(trail):
    trail.parent.mkdir()
    trail.write_bytes(b"")
    locking = _Locking(trail, busy_for=3)
    pauses: list[float] = []
    descriptor = os.open(trail, APPEND_FLAGS)
    try:
        written = capture_diagnostics._write_trail_line(descriptor, b"x\n", locking, pauses.append)
    finally:
        os.close(descriptor)

    assert (written, pauses, trail.read_bytes()) == (
        True,
        [capture_diagnostics.TRAIL_LOCK_PAUSE_SECONDS] * 3,
        b"x\n",
    )


def test_a_lock_that_never_frees_up_is_given_up_without_writing_or_raising(trail):
    trail.parent.mkdir()
    trail.write_bytes(b"")
    attempts = capture_diagnostics.TRAIL_LOCK_ATTEMPTS
    locking = _Locking(trail, busy_for=attempts + 1)
    pauses: list[float] = []
    descriptor = os.open(trail, APPEND_FLAGS)
    try:
        written = capture_diagnostics._write_trail_line(descriptor, b"x\n", locking, pauses.append)
    finally:
        os.close(descriptor)

    assert (written, len(locking.events), len(pauses), trail.read_bytes()) == (
        False,
        attempts,
        attempts,
        b"",
    )


def test_the_whole_wait_fits_well_inside_a_hook_budget():
    import daily_log_append

    wait = capture_diagnostics.TRAIL_LOCK_ATTEMPTS * capture_diagnostics.TRAIL_LOCK_PAUSE_SECONDS

    # Twice: once under the state lock, once in the fallback after it.
    assert 2 * wait + capture_diagnostics.STATE_LOCK_TIMEOUT < (
        daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS
    )


def test_where_append_is_atomic_there_is_no_lock_at_all():
    locking = capture_diagnostics._append_locking()

    assert (locking is None) == (os.name != "nt")
