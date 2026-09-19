"""The state lock is removed even while another reader holds the file open.

Windows will not delete a file somebody else has open, and every waiter polls
this very lock through `_lock_bytes`. A release that gave up on that sharing
violation left the lock file naming an owner that is alive — this process — so
no staleness rule could retire it, every later writer burned its timeout, and
every capture claim degraded to a unique fallback id: 54 reservations where the
contract allows one (run 35382495391). Research:
`docs/research/2026-09-18-a-lock-is-released-even-while-somebody-is-reading-it.md`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import memory_state  # noqa: E402

# What Windows raises while another handle is open: ERROR_ACCESS_DENIED,
# ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION.
SHARING_VIOLATION = 32


def _sharing_violation() -> PermissionError:
    """The refusal Windows returns while a reader still holds the file."""
    error = PermissionError(13, "The process cannot access the file")
    error.winerror = SHARING_VIOLATION
    return error


class _BlockedUnlink:
    """An `os.unlink` that a reader blocks for the first `refusals` calls.

    The real `os.unlink` is captured at construction: `memory_state.os` is the
    `os` module itself, so patching it replaces the name this would otherwise
    call back into.
    """

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.calls = 0
        self.unlink = os.unlink

    def __call__(self, path) -> None:
        self.calls += 1
        if self.calls <= self.refusals:
            raise _sharing_violation()
        self.unlink(path)


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "run"
    directory.mkdir(parents=True)
    monkeypatch.setattr(memory_state, "STATE_DIR", directory)
    monkeypatch.setattr(memory_state, "STATE_FILE", directory / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", directory / "state.json.lock")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", tmp_path / "logs")
    return directory


def _mark(state: dict) -> None:
    state["written"] = True


def test_a_reader_blocking_the_delete_does_not_leak_the_lock(state_dir, monkeypatch) -> None:
    """The release waits the reader out instead of leaving the lock behind."""
    blocked = _BlockedUnlink(refusals=3)
    monkeypatch.setattr(memory_state.os, "unlink", blocked)

    memory_state.update_state(_mark)

    assert (blocked.calls, memory_state.LOCK_FILE.exists()) == (4, False)


def test_a_second_write_still_gets_the_lock_after_a_blocked_release(
    state_dir, monkeypatch
) -> None:
    """The point of the retry: one unlucky release must not wedge the process.

    Without it the lock file survives naming this live process, and every writer
    after it waits out its whole timeout — which is how one claim became 54.
    """
    monkeypatch.setattr(memory_state.os, "unlink", _BlockedUnlink(refusals=2))
    memory_state.update_state(_mark)

    second = memory_state.update_state(lambda state: state.update(again=True))

    assert (second["written"], second["again"]) == (True, True)


def test_a_refusal_that_is_not_a_reader_is_not_retried(state_dir, monkeypatch) -> None:
    """A real error ends the release; only the sharing violations are waited out."""
    denied = PermissionError(13, "permission denied")
    calls: list[int] = []

    def refuse(_path) -> None:
        calls.append(1)
        raise denied

    monkeypatch.setattr(memory_state.os, "unlink", refuse)

    memory_state.update_state(_mark)

    assert (len(calls), memory_state.LOCK_FILE.exists()) == (1, True)


def test_a_lock_already_gone_counts_as_released(state_dir) -> None:
    """Somebody else's retirement got there first, which is the outcome wanted."""
    assert memory_state._try_unlink_lock_file() is True
