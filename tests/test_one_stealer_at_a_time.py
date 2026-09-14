"""A stale lock is checked and removed under one OS lock, so two stealers cannot make two holders.

A stealer renamed a fresh lock aside before checking it, and a failed put-back left two
processes each holding the state lock. Research:
`docs/research/2026-09-14-one-stealer-at-a-time.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import memory_state  # noqa: E402


def test_a_late_stealer_leaves_the_winners_fresh_lock_in_place(tmp_path, monkeypatch):
    lock = tmp_path / "state.json.lock"
    lock.write_bytes(b"4242")
    assert memory_state.retire_stale_lock(lock, b"4242") is True
    lock.write_bytes(b"1111")  # the first stealer now holds a fresh lock

    def name_taken(*_args):
        raise FileExistsError("a third process created a lock meanwhile")

    # The race the audit forced: any put-back finds the name taken.
    monkeypatch.setattr(memory_state.os, "link", name_taken)
    late = memory_state.retire_stale_lock(lock, b"4242")

    assert (late, lock.read_bytes(), sorted(path.name for path in tmp_path.iterdir())) == (
        False,
        b"1111",
        ["state.json.lock", "state.json.lock.steal"],
    )


def test_a_stealer_waits_for_the_one_already_judging(tmp_path, monkeypatch):
    import pytest

    lock = tmp_path / "state.json.lock"
    lock.write_bytes(b"4242")
    monkeypatch.setattr(memory_state, "STEAL_GUARD_SECONDS", 0.05)

    with memory_state._steal_guard(lock):
        with pytest.raises(memory_state.StateLockTimeout):
            _other_process_retires(lock)

    assert lock.read_bytes() == b"4242"


def _other_process_retires(lock: Path) -> None:
    """A second descriptor stands in for a second process: flock locks are per open file."""
    memory_state.retire_stale_lock(lock, b"4242")
