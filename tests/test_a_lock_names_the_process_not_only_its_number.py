"""A lock file records the identity of the process that took it, not only its PID.

A PID is handed out again; the state lock, the maintenance marker and the
compile lock all judged their owner by that number alone, so a reused PID held
a lock for as long as the unrelated process lived, and a writer that died
holding the state lock blocked everyone for the 30 s staleness window while
their own wait is 10 s (audit Q-L6, Q-L7). Research:
`docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import maybe_compile  # noqa: E402
import memory_state  # noqa: E402
import process_liveness  # noqa: E402

FOREIGN = "test-process:a-number-handed-to-somebody-else"


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


def test_the_state_lock_of_a_reused_pid_is_retired_without_waiting_out_its_age(
    state_dir,
) -> None:
    """Our own PID, somebody else's process: the lock is not ours to wait for."""
    memory_state.LOCK_FILE.write_bytes(f"{os.getpid()}\n{FOREIGN}\n".encode())
    started = time.monotonic()

    memory_state.update_state(_mark)

    assert time.monotonic() - started < memory_state._STALE_LOCK_SECONDS
    assert not memory_state.LOCK_FILE.exists()


def test_a_live_owner_keeps_the_state_lock_it_holds(state_dir) -> None:
    identity = process_liveness.process_start_identity(os.getpid())
    memory_state.LOCK_FILE.write_bytes(f"{os.getpid()}\n{identity}\n".encode())

    with pytest.raises(memory_state.StateLockTimeout):
        memory_state.update_state(_mark, lock_timeout=0.2)

    assert memory_state.LOCK_FILE.exists()


def test_a_lock_written_before_this_release_is_still_judged_by_its_age(
    state_dir,
) -> None:
    """No identity line: the PID probe and the 30 s rule, exactly as before."""
    memory_state.LOCK_FILE.write_bytes(str(os.getpid()).encode())

    with pytest.raises(memory_state.StateLockTimeout):
        memory_state.update_state(_mark, lock_timeout=0.2)

    assert memory_state._lock_owner(memory_state.LOCK_FILE.read_bytes()) == (
        os.getpid(),
        "",
    )


def test_the_state_lock_records_this_process(state_dir) -> None:
    payload = memory_state._lock_payload()

    assert memory_state._lock_owner(payload) == (
        os.getpid(),
        process_liveness.process_start_identity(os.getpid()),
    )
    assert memory_state._named_owner_is_dead(payload) is False


def test_a_compile_lock_of_a_reused_pid_is_stale(tmp_path, monkeypatch) -> None:
    lock = tmp_path / "compile.pid"
    monkeypatch.setattr(maybe_compile, "LOCK_FILE", lock)
    stamp = "2026-09-17T12:00:00"
    lock.write_bytes(f"{os.getpid()}\n{stamp}\ntoken\n{FOREIGN}\n".encode())

    assert maybe_compile._lock_state()[0] == "stale"


def test_a_compile_lock_this_process_holds_is_live(tmp_path, monkeypatch) -> None:
    lock = tmp_path / "compile.pid"
    monkeypatch.setattr(maybe_compile, "LOCK_FILE", lock)
    maybe_compile._write_lock(os.getpid())

    assert maybe_compile._lock_state()[0] == "live"
    assert maybe_compile._read_lock()["identity"] == (
        process_liveness.process_start_identity(os.getpid())
    )
